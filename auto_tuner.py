"""Auto-tuner — paper data istatistik analiziyle parametre öneri üretir.

Counterfactual simulation yapmıyor (price history saklamıyoruz).
Bunun yerine summary istatistiklerden parametre yönü çıkarıyor:
  - TP3 trigger: kazananların ort zirvesi vs mevcut TP3 → çok düşükse yükselt
  - Trailing stop: yıkanan pozisyonlarda peak'ten ort drawdown → mevcuttan
    daha sıkıysa daralt
  - Min score to alert: skor bucket'larında WR inflection point
  - Sizing bandit önerisi: best arm'a göre adaptive_sizing ayarı

Öneriler `/tune` ile Telegram'a düşer; otomatik uygulanmaz (kullanıcı manuel
env değiştirip restart eder).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from config import config
from pnl import bucket_label, summarize
from storage import Position

log = logging.getLogger(__name__)


@dataclass
class TuningSuggestion:
    param: str
    current: float
    suggested: float
    reason: str
    confidence: str = "medium"  # low / medium / high


@dataclass
class TuningReport:
    n_samples: int
    suggestions: list[TuningSuggestion] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _pnl_pct(p: Position) -> float:
    if p.pnl_pct is not None:
        return p.pnl_pct
    if p.sol_spent > 0:
        return (p.sol_received_total - p.sol_spent) / p.sol_spent * 100
    return 0.0


def _peak_pct_estimate(p: Position) -> float:
    """Pozisyonun gördüğü en yüksek pnl tahmini.
    peak_price_usd / entry_price_usd üzerinden.
    """
    if p.entry_price_usd <= 0:
        return 0.0
    return (p.peak_price_usd - p.entry_price_usd) / p.entry_price_usd * 100


def analyze(positions: list[Position]) -> TuningReport:
    closed = [p for p in positions if p.status == "closed" and p.closed_at]
    n = len(closed)
    report = TuningReport(n_samples=n)
    if n < config.autotune_min_samples:
        report.notes.append(
            f"Yetersiz sample: {n} (min {config.autotune_min_samples}). "
            "Paper biriktirmeye devam et."
        )
        return report

    winners = [p for p in closed if _pnl_pct(p) > 0]
    losers = [p for p in closed if _pnl_pct(p) <= 0]

    # 1. TP3 öneri — kazananların avg peak'i mevcut TP3'ten N% fazlaysa yükselt
    if len(winners) >= 5:
        avg_winner_peak = sum(_peak_pct_estimate(p) for p in winners) / len(winners)
        if avg_winner_peak > config.tp3_trigger * 1.5:
            new_tp3 = round(min(avg_winner_peak * 0.7, config.tp3_trigger * 2.0))
            report.suggestions.append(TuningSuggestion(
                param="TP3_TRIGGER_PCT",
                current=config.tp3_trigger,
                suggested=new_tp3,
                reason=(
                    f"Kazananların ort. zirvesi %{avg_winner_peak:.0f} — "
                    f"TP3 mevcut %{config.tp3_trigger:.0f} çok düşük, "
                    f"para masada bırakılıyor"
                ),
                confidence="high" if len(winners) >= 15 else "medium",
            ))
        elif avg_winner_peak < config.tp3_trigger * 0.7:
            new_tp3 = round(max(avg_winner_peak * 1.1, config.tp2_trigger * 1.3))
            report.suggestions.append(TuningSuggestion(
                param="TP3_TRIGGER_PCT",
                current=config.tp3_trigger,
                suggested=new_tp3,
                reason=(
                    f"Kazananların ort. zirvesi sadece %{avg_winner_peak:.0f} — "
                    f"TP3 %{config.tp3_trigger:.0f} hiç tetiklenmiyor"
                ),
                confidence="medium",
            ))

    # 2. Trailing stop — yıkananların peak'ten avg drawdown'u küçükse daralt
    if len(losers) >= 5:
        avg_loser_dd = []
        for p in losers:
            peak_pct = _peak_pct_estimate(p)
            final_pct = _pnl_pct(p)
            if peak_pct > 5:  # pozitif bölgeye dokundu sonra döndü
                dd = peak_pct - final_pct
                avg_loser_dd.append(dd)
        if avg_loser_dd:
            avg_dd = sum(avg_loser_dd) / len(avg_loser_dd)
            if avg_dd < config.trailing_stop * 0.7:
                new_trail = round(max(10, avg_dd * 1.1))
                report.suggestions.append(TuningSuggestion(
                    param="TRAILING_STOP_PCT",
                    current=config.trailing_stop,
                    suggested=new_trail,
                    reason=(
                        f"Yıkanan pozisyonlarda peak'ten ort drawdown "
                        f"%{avg_dd:.0f} — trailing %{config.trailing_stop:.0f} "
                        f"çok gevşek, daha sıkı tut"
                    ),
                    confidence="medium",
                ))

    # 3. Min score to alert — skor bucket'larında WR inflection
    by_bucket: dict[str, list[Position]] = {}
    for p in closed:
        bk = bucket_label(p.score)
        by_bucket.setdefault(bk, []).append(p)

    # Düşük bucket'larda WR <30% ise eşiği yükselt önerisi
    for lo, hi, label in [
        (50, 60, "50-60"), (60, 70, "60-70"), (70, 80, "70-80"),
    ]:
        bucket_pos = [p for p in closed if lo <= p.score < hi]
        if len(bucket_pos) < 5:
            continue
        wr = sum(1 for p in bucket_pos if _pnl_pct(p) > 0) / len(bucket_pos) * 100
        if wr < 30 and config.min_score_to_alert <= lo:
            new_min = hi
            report.suggestions.append(TuningSuggestion(
                param="MIN_SCORE_TO_ALERT",
                current=config.min_score_to_alert,
                suggested=new_min,
                reason=(
                    f"Bucket {label}: n={len(bucket_pos)} WR={wr:.0f}% "
                    f"— min score eşiği yükseltilirse bu zayıf bucket eliminasyona uğrar"
                ),
                confidence="medium" if len(bucket_pos) >= 10 else "low",
            ))
            break  # tek öneri yeter
        elif wr >= 60 and config.min_score_to_alert > lo:
            # Yüksek WR olan düşük bucket — eşik aşırı yüksek?
            report.suggestions.append(TuningSuggestion(
                param="MIN_SCORE_TO_ALERT",
                current=config.min_score_to_alert,
                suggested=lo,
                reason=(
                    f"Bucket {label}: WR={wr:.0f}% iyi — eşik aşırı sıkı, "
                    f"daha düşük bucket'lar da fırsat sunabilir"
                ),
                confidence="low",
            ))
            break

    # 4. Profile dağılımı — bir profile sürekli kaybediyorsa o profile filtrelerini sıkı
    early_pos = [p for p in closed if p.profile == "early"]
    trend_pos = [p for p in closed if p.profile == "trend"]
    if len(early_pos) >= 10 and len(trend_pos) >= 10:
        early_wr = sum(1 for p in early_pos if _pnl_pct(p) > 0) / len(early_pos) * 100
        trend_wr = sum(1 for p in trend_pos if _pnl_pct(p) > 0) / len(trend_pos) * 100
        diff = abs(early_wr - trend_wr)
        if diff > 25:
            weak = "early" if early_wr < trend_wr else "trend"
            report.notes.append(
                f"⚠️ Profile dengesizliği: early WR={early_wr:.0f}% vs "
                f"trend WR={trend_wr:.0f}% — {weak} profile filtrelerini sıkmayı "
                f"düşün (EARLY_MIN_SCORE ya da TREND_MIN_VOL_H6 gibi)"
            )

    # 5. Net SOL özet
    total_pnl = sum(
        (p.sol_received_total - p.sol_spent) for p in closed
    )
    win_rate = (
        sum(1 for p in closed if _pnl_pct(p) > 0) / len(closed) * 100
        if closed else 0
    )
    report.notes.append(
        f"📈 Genel: n={n} WR={win_rate:.0f}% net={total_pnl:+.4f} SOL"
    )

    return report


def format_report(report: TuningReport) -> str:
    lines = [f"🔧 <b>Auto-tune raporu</b>  (sample n=<code>{report.n_samples}</code>)"]
    if not report.suggestions and not report.notes:
        lines.append("Henüz öneri yok.")
        return "\n".join(lines)
    for note in report.notes:
        lines.append(note)
    if report.suggestions:
        lines.append("\n<b>Öneriler:</b>")
        for s in report.suggestions:
            conf_emoji = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(
                s.confidence, "🟡"
            )
            lines.append(
                f"{conf_emoji} <code>{s.param}</code>: "
                f"<code>{s.current}</code> → <b>{s.suggested}</b>\n"
                f"  <i>{s.reason}</i>"
            )
        lines.append(
            "\n<i>Öneriler otomatik uygulanmaz. Render env'i güncelle, restart et.</i>"
        )
    return "\n".join(lines)

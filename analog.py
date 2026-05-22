"""Analog regime backtest.

Sinyal kayıtlarına gömülü makro snapshot'ları kullanarak: bugünkü makroya
en benzeyen geçmiş ortamlarda sinyallerin ortalama 24h zirve performansını
çıkarır. Forward simulation'a (paper) ek olarak hızlı bir 'şu an alacak
mıyız?' refleksi sunar.

Benzerlik:
  - 4 boyutlu özellik vektörü (SOL Δ24h, BTC dom, F&G, pump.fun aktivite)
  - Her özellik kendi tipik aralığına normalize edilir
  - Ağırlıklı euclidean distance → similarity = 1 / (1 + dist)
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from math import sqrt

from config import config
from macro import MacroSnapshot, latest_snapshot
from signal_log import LoggedSignal, SignalLog

log = logging.getLogger(__name__)


# Normalize ölçekleri: bir "birim" ne kadar farklılığa karşılık gelsin
NORM = {
    "sol_change_24h": 10.0,           # 10% gün hareketi = 1 birim
    "btc_dominance": 5.0,             # 5% kayma = 1 birim
    "fear_greed": 25.0,               # 0-100 ölçek, çeyrek = 1 birim
    "pump_graduated_recent": 25.0,    # tipik dalgalanma ~25
}

# Ağırlıklar: F&G memecoin senti için en güçlü gösterge
WEIGHTS = {
    "sol_change_24h": 1.0,
    "btc_dominance": 0.7,
    "fear_greed": 1.2,
    "pump_graduated_recent": 1.0,
}


def _features(snap: dict | MacroSnapshot) -> dict[str, float]:
    if isinstance(snap, MacroSnapshot):
        d = asdict(snap)
    else:
        d = snap or {}
    return {k: float(d.get(k) or 0) for k in NORM}


def similarity(a: dict | MacroSnapshot, b: dict | MacroSnapshot) -> float:
    """0..1 — 1 = aynı durum, 0'a yaklaşan değer = uzak."""
    fa = _features(a)
    fb = _features(b)
    sq = 0.0
    for k, n in NORM.items():
        d = (fa[k] - fb[k]) / n
        sq += WEIGHTS[k] * d * d
    return 1.0 / (1.0 + sqrt(sq))


def _signal_macro(s: LoggedSignal) -> dict | None:
    if isinstance(s.macro, dict) and s.macro:
        return s.macro
    return None


def analog_report(signal_log: SignalLog, top_n: int | None = None) -> str:
    current = latest_snapshot()
    if current is None:
        return (
            "📊 <b>Analog regime</b>\n"
            "Henüz makro snapshot yok — bot 1 saat çalıştırılınca biriker."
        )

    tagged = [
        s for s in signal_log.signals
        if s.final_24h and _signal_macro(s) is not None
    ]
    if len(tagged) < 5:
        return (
            f"📊 <b>Analog regime</b>\n"
            f"Yeterli makro etiketli finalize sinyal yok "
            f"(<code>{len(tagged)}</code>/5). Birikme 1-2 hafta sürer."
        )

    top_n = top_n or config.analog_top_n
    scored = [(similarity(s.macro, current), s) for s in tagged]
    scored.sort(key=lambda x: -x[0])
    top = scored[:top_n]

    n = len(top)
    avg_peak_1h = sum(s.peak_pct_1h for _, s in top) / n
    avg_peak_24h = sum(s.peak_pct_24h for _, s in top) / n
    hit_30 = sum(1 for _, s in top if s.peak_pct_24h >= 30) / n * 100
    hit_100 = sum(1 for _, s in top if s.peak_pct_24h >= 100) / n * 100

    overall_avg = sum(s.peak_pct_24h for s in tagged) / len(tagged)
    delta = avg_peak_24h - overall_avg

    # Profile dağılımı (early vs trend) benzer ortamlarda
    early_n = sum(1 for _, s in top if s.profile == "early")
    trend_n = sum(1 for _, s in top if s.profile == "trend")

    # Ortalama similarity (regim ne kadar iyi eşleşti)
    avg_sim = sum(sim for sim, _ in top) / n

    return (
        f"📊 <b>Analog regime</b>  (en benzer "
        f"<code>{n}/{len(tagged)}</code> sinyal, ort sim "
        f"<code>{avg_sim:.2f}</code>)\n\n"
        f"<b>Şu anki makro:</b>\n"
        f"SOL Δ24h <code>{current.sol_change_24h:+.1f}%</code>  "
        f"BTC.D <code>{current.btc_dominance:.1f}%</code>  "
        f"F&amp;G <code>{current.fear_greed}</code>\n"
        f"pump grad <code>{current.pump_graduated_recent}</code>\n\n"
        f"<b>Benzer ortamlarda sinyaller:</b>\n"
        f"Ort. zirve 1h:  <code>{avg_peak_1h:+.1f}%</code>\n"
        f"Ort. zirve 24h: <code>{avg_peak_24h:+.1f}%</code>  "
        f"<i>(genel ort {delta:+.1f}%)</i>\n"
        f"+30% isabet:  <code>{hit_30:.0f}%</code>\n"
        f"+100% isabet: <code>{hit_100:.0f}%</code>\n"
        f"Profile dağılımı: 🌱 <code>{early_n}</code>  📈 <code>{trend_n}</code>"
    )

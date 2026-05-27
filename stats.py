"""EV (Expected Value) hesaplayıcı — bot'un asıl başarı metriği.

Matematik:
  EV = p_win × E[gain|win] - p_loss × E[loss|loss]

Memecoin için bu pozitif olmalı; değilse strateji yanlış. Her N kapanan
trade sonrası tekrar hesaplanıp /stats komutunda gösterilir.

z-skoru: EV pozitif mi yoksa şans eseri mi sorusunun istatistiksel cevabı
  z = mean(pnl) / (std(pnl) / sqrt(n))
  |z| > 2 → %95+ güven, kesin sinyal var
  |z| < 1 → şans olabilir, az veri
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from storage import Position


@dataclass
class TradeStats:
    n: int                  # toplam kapanan trade
    n_win: int
    n_loss: int
    p_win: float            # win rate (0-1)
    avg_pnl_pct: float      # tüm trade'lerin ortalama %
    avg_win_pct: float      # kazanan ortalama %
    avg_loss_pct: float     # kaybeden ortalama % (negatif)
    ev_pct: float           # = p_win × avg_win + p_loss × avg_loss
    total_pnl_sol: float    # net SOL
    z_score: float          # mean(pnl) / SE — istatistiksel güven


def _pnl_pct(p: Position) -> float:
    if p.pnl_pct is not None:
        return p.pnl_pct
    if p.sol_spent > 0:
        return (p.sol_received_total - p.sol_spent) / p.sol_spent * 100
    return 0.0


def _pnl_sol(p: Position) -> float:
    return p.sol_received_total - p.sol_spent


def compute(positions: list[Position], last_n: int | None = None) -> TradeStats | None:
    """Son N kapanan trade'den TradeStats hesapla. n=0 → None."""
    closed = sorted(
        [p for p in positions if p.status == "closed" and p.closed_at],
        key=lambda p: p.closed_at or 0,
    )
    if last_n is not None and last_n > 0:
        closed = closed[-last_n:]
    n = len(closed)
    if n == 0:
        return None

    pnls_pct = [_pnl_pct(p) for p in closed]
    pnls_sol = [_pnl_sol(p) for p in closed]
    wins = [x for x in pnls_pct if x > 0]
    losses = [x for x in pnls_pct if x <= 0]
    n_win = len(wins)
    n_loss = len(losses)
    p_win = n_win / n
    avg_pnl = sum(pnls_pct) / n
    avg_win = sum(wins) / n_win if wins else 0.0
    avg_loss = sum(losses) / n_loss if losses else 0.0
    ev = p_win * avg_win + (1 - p_win) * avg_loss

    # z-skoru: mean / standard error
    if n > 1:
        variance = sum((x - avg_pnl) ** 2 for x in pnls_pct) / (n - 1)
        std = math.sqrt(variance)
        se = std / math.sqrt(n) if std > 0 else 0.0
        z = avg_pnl / se if se > 0 else 0.0
    else:
        z = 0.0

    return TradeStats(
        n=n,
        n_win=n_win,
        n_loss=n_loss,
        p_win=p_win,
        avg_pnl_pct=avg_pnl,
        avg_win_pct=avg_win,
        avg_loss_pct=avg_loss,
        ev_pct=ev,
        total_pnl_sol=sum(pnls_sol),
        z_score=z,
    )


def format_stats(positions: list[Position]) -> str:
    """/stats komutu için: tüm zaman + son 30 EV özeti."""
    all_time = compute(positions)
    if all_time is None:
        return (
            "📊 <b>Stratejiyi ölç</b>\n"
            "Henüz kapanmış trade yok. EV ölçümü için en az 10+ trade gerekir."
        )
    recent = compute(positions, last_n=30) or all_time

    def _box(s: TradeStats, label: str) -> str:
        ev_emoji = "🟢" if s.ev_pct > 0 else "🔴"
        z_quality = (
            "yüksek güven" if abs(s.z_score) > 2
            else "orta" if abs(s.z_score) > 1
            else "düşük (az veri)"
        )
        return (
            f"<b>{label}</b> (n=<code>{s.n}</code>)\n"
            f"  WR: <code>{s.p_win * 100:.0f}%</code>  "
            f"({s.n_win}W / {s.n_loss}L)\n"
            f"  Ort kazanan: <code>{s.avg_win_pct:+.1f}%</code>  "
            f"ort kaybeden: <code>{s.avg_loss_pct:+.1f}%</code>\n"
            f"  {ev_emoji} EV: <code>{s.ev_pct:+.2f}%</code> per trade  "
            f"<i>(z={s.z_score:+.2f}, {z_quality})</i>\n"
            f"  Net SOL: <code>{s.total_pnl_sol:+.4f}</code>"
        )

    return (
        f"📊 <b>Stratejik EV ölçümü</b>\n\n"
        f"{_box(recent, '🕐 Son 30 trade')}\n\n"
        f"{_box(all_time, '🗓 Tüm zaman')}\n\n"
        f"<i>EV &gt; 0 + |z| &gt; 2 → matematiksel olarak strateji çalışıyor</i>"
    )

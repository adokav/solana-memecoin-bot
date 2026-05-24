"""Telegram chart generator (matplotlib).

PNG bytes döner — TelegramHub.send_photo ile yollanır. Default açık.
matplotlib import edilemezse modül no-op olur.

Mevcut chartlar:
  - equity_curve_png: tüm kapanmış pozisyonların kümülatif net SOL grafik
  - daily_pnl_png: günlük PnL bar chart
  - score_dist_png: kapanmış pozisyonların skor dağılımı (winners vs losers)
"""
from __future__ import annotations

import io
import logging
import time
from collections import defaultdict

from storage import Position

log = logging.getLogger(__name__)

try:
    import matplotlib
    matplotlib.use("Agg")  # headless
    import matplotlib.pyplot as plt
    _CHARTS_OK = True
except Exception:
    _CHARTS_OK = False


def charts_available() -> bool:
    return _CHARTS_OK


def _pnl_sol(p: Position) -> float:
    return p.sol_received_total - p.sol_spent


def _closed(positions: list[Position]) -> list[Position]:
    return sorted(
        [p for p in positions if p.status == "closed" and p.closed_at],
        key=lambda p: p.closed_at or 0,
    )


def equity_curve_png(positions: list[Position], title_suffix: str = "") -> bytes | None:
    if not _CHARTS_OK:
        return None
    closed = _closed(positions)
    if not closed:
        return None
    ts_seq = []
    cum = 0.0
    cum_seq = []
    for p in closed:
        cum += _pnl_sol(p)
        ts_seq.append(p.closed_at)
        cum_seq.append(cum)

    fig, ax = plt.subplots(figsize=(8, 4), facecolor="#0d1117")
    ax.set_facecolor("#0d1117")
    ax.plot(
        [time.strftime("%m-%d %H:%M", time.gmtime(t)) for t in ts_seq],
        cum_seq,
        color="#58a6ff", linewidth=2,
    )
    ax.set_title(f"Equity curve {title_suffix}".strip(), color="#c9d1d9")
    ax.set_ylabel("Cumulative SOL", color="#c9d1d9")
    ax.tick_params(colors="#8b949e", labelrotation=45)
    ax.grid(True, color="#30363d", alpha=0.3)
    for spine in ax.spines.values():
        spine.set_color("#30363d")
    # X axis: çok fazla label varsa sadece her N'inciyi göster
    if len(ts_seq) > 12:
        for i, lbl in enumerate(ax.get_xticklabels()):
            if i % max(1, len(ts_seq) // 12) != 0:
                lbl.set_visible(False)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def daily_pnl_png(positions: list[Position], days: int = 14) -> bytes | None:
    if not _CHARTS_OK:
        return None
    closed = _closed(positions)
    if not closed:
        return None
    by_day: dict[str, float] = defaultdict(float)
    cutoff = time.time() - days * 86400
    for p in closed:
        if (p.closed_at or 0) < cutoff:
            continue
        day = time.strftime("%m-%d", time.gmtime(p.closed_at))
        by_day[day] += _pnl_sol(p)
    if not by_day:
        return None
    days_sorted = sorted(by_day.keys())
    values = [by_day[d] for d in days_sorted]
    colors = ["#3fb950" if v >= 0 else "#f85149" for v in values]

    fig, ax = plt.subplots(figsize=(8, 4), facecolor="#0d1117")
    ax.set_facecolor("#0d1117")
    ax.bar(days_sorted, values, color=colors)
    ax.axhline(0, color="#8b949e", linewidth=0.5)
    ax.set_title(f"Daily PnL — son {days} gün", color="#c9d1d9")
    ax.set_ylabel("Net SOL", color="#c9d1d9")
    ax.tick_params(colors="#8b949e", labelrotation=45)
    ax.grid(True, color="#30363d", alpha=0.3, axis="y")
    for spine in ax.spines.values():
        spine.set_color("#30363d")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def score_distribution_png(positions: list[Position]) -> bytes | None:
    if not _CHARTS_OK:
        return None
    closed = _closed(positions)
    if not closed:
        return None
    win_scores = [p.score for p in closed if _pnl_sol(p) > 0]
    loss_scores = [p.score for p in closed if _pnl_sol(p) <= 0]
    if not win_scores and not loss_scores:
        return None

    fig, ax = plt.subplots(figsize=(8, 4), facecolor="#0d1117")
    ax.set_facecolor("#0d1117")
    bins = list(range(40, 121, 5))
    if win_scores:
        ax.hist(win_scores, bins=bins, color="#3fb950", alpha=0.7, label=f"win ({len(win_scores)})")
    if loss_scores:
        ax.hist(loss_scores, bins=bins, color="#f85149", alpha=0.7, label=f"loss ({len(loss_scores)})")
    ax.set_title("Skor dağılımı — kapanmış pozisyonlar", color="#c9d1d9")
    ax.set_xlabel("Score", color="#c9d1d9")
    ax.set_ylabel("Count", color="#c9d1d9")
    ax.tick_params(colors="#8b949e")
    ax.grid(True, color="#30363d", alpha=0.3)
    ax.legend(facecolor="#161b22", edgecolor="#30363d", labelcolor="#c9d1d9")
    for spine in ax.spines.values():
        spine.set_color("#30363d")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()

"""Kapanan pozisyon PnL analitiği.

storage.py'daki kapanmış pozisyonlardan agrege istatistik çıkarır:
  - genel win-rate, net SOL PnL, ROI
  - profile bazlı (early/trend) karşılaştırma
  - skor aralığı bazlı (hangi skor band'i gerçekten para kazandırıyor)
  - close_reason bazlı (TP3/trailing/SL/breakeven dağılımı)
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from statistics import mean, median
from typing import Iterable

from storage import Position


SCORE_BUCKETS: list[tuple[float, float, str]] = [
    (0, 55, "<55"),
    (55, 65, "55-65"),
    (65, 75, "65-75"),
    (75, 85, "75-85"),
    (85, 999, "85+"),
]


@dataclass
class TradeStats:
    count: int
    wins: int
    losses: int
    win_rate: float
    total_pnl_sol: float
    avg_pnl_pct: float
    median_pnl_pct: float
    best_pnl_pct: float
    worst_pnl_pct: float
    avg_winner_pct: float
    avg_loser_pct: float


def _pnl_pct(p: Position) -> float:
    if p.pnl_pct is not None:
        return p.pnl_pct
    if p.sol_spent > 0:
        return ((p.sol_received_total - p.sol_spent) / p.sol_spent) * 100
    return 0.0


def _pnl_sol(p: Position) -> float:
    return p.sol_received_total - p.sol_spent


def _stats(positions: list[Position]) -> TradeStats | None:
    if not positions:
        return None
    pnls_pct = [_pnl_pct(p) for p in positions]
    pnls_sol = [_pnl_sol(p) for p in positions]
    winners = [x for x in pnls_pct if x > 0]
    losers = [x for x in pnls_pct if x <= 0]
    return TradeStats(
        count=len(positions),
        wins=len(winners),
        losses=len(losers),
        win_rate=(len(winners) / len(positions)) * 100,
        total_pnl_sol=sum(pnls_sol),
        avg_pnl_pct=mean(pnls_pct),
        median_pnl_pct=median(pnls_pct),
        best_pnl_pct=max(pnls_pct),
        worst_pnl_pct=min(pnls_pct),
        avg_winner_pct=mean(winners) if winners else 0.0,
        avg_loser_pct=mean(losers) if losers else 0.0,
    )


def bucket_label(score: float) -> str:
    for lo, hi, label in SCORE_BUCKETS:
        if lo <= score < hi:
            return label
    return "?"


_bucket_label = bucket_label  # geri uyumluluk


def _closed(positions: Iterable[Position], since_ts: float) -> list[Position]:
    out = []
    for p in positions:
        if p.status != "closed":
            continue
        if p.closed_at is None:
            continue
        if since_ts and p.closed_at < since_ts:
            continue
        out.append(p)
    return out


def summarize(positions: list[Position], days: int = 0) -> dict:
    """days=0 → tüm zaman; days>0 → son N gün."""
    since_ts = (time.time() - days * 86400) if days > 0 else 0
    closed = _closed(positions, since_ts)
    if not closed:
        return {"total": 0, "days": days}

    overall = _stats(closed)

    by_profile: dict[str, TradeStats] = {}
    for prof in ("early", "trend"):
        subset = [p for p in closed if p.profile == prof]
        s = _stats(subset)
        if s:
            by_profile[prof] = s

    by_score: dict[str, TradeStats] = {}
    for lo, hi, label in SCORE_BUCKETS:
        subset = [p for p in closed if lo <= p.score < hi]
        s = _stats(subset)
        if s:
            by_score[label] = s

    by_reason: dict[str, int] = {}
    for p in closed:
        key = (p.close_reason or "unknown").split(" ")[0]
        by_reason[key] = by_reason.get(key, 0) + 1

    return {
        "total": overall.count,
        "days": days,
        "overall": overall,
        "by_profile": by_profile,
        "by_score": by_score,
        "by_reason": by_reason,
    }


def format_report(summary: dict) -> str:
    days = summary.get("days", 0)
    period = f"son {days}g" if days > 0 else "tüm zaman"

    if summary.get("total", 0) == 0:
        return f"📭 <b>PnL ({period})</b>\nKapanmış pozisyon yok."

    o: TradeStats = summary["overall"]
    sign = "🟢" if o.total_pnl_sol >= 0 else "🔴"

    lines = [
        f"💼 <b>PnL özeti</b> ({period})",
        f"İşlem: <code>{o.count}</code>  W/L: <code>{o.wins}/{o.losses}</code>  "
        f"WR: <code>{o.win_rate:.0f}%</code>",
        f"{sign} Net: <code>{o.total_pnl_sol:+.4f} SOL</code>",
        f"Ort: <code>{o.avg_pnl_pct:+.1f}%</code>  "
        f"Medyan: <code>{o.median_pnl_pct:+.1f}%</code>",
        f"En iyi: <code>{o.best_pnl_pct:+.1f}%</code>  "
        f"En kötü: <code>{o.worst_pnl_pct:+.1f}%</code>",
        f"Ort kazanan: <code>{o.avg_winner_pct:+.1f}%</code>  "
        f"Ort kaybeden: <code>{o.avg_loser_pct:+.1f}%</code>",
    ]

    by_profile = summary.get("by_profile") or {}
    if by_profile:
        lines.append("\n<b>Profile</b>")
        for prof, s in by_profile.items():
            tag = "🌱" if prof == "early" else "📈"
            lines.append(
                f"{tag} {prof}: <code>{s.count}</code>  "
                f"WR <code>{s.win_rate:.0f}%</code>  "
                f"PnL <code>{s.total_pnl_sol:+.4f} SOL</code>  "
                f"ort <code>{s.avg_pnl_pct:+.1f}%</code>"
            )

    by_score = summary.get("by_score") or {}
    if by_score:
        lines.append("\n<b>Skor aralığı</b>")
        for label, s in by_score.items():
            lines.append(
                f"  {label}: <code>{s.count}</code>  "
                f"WR <code>{s.win_rate:.0f}%</code>  "
                f"ort <code>{s.avg_pnl_pct:+.1f}%</code>"
            )

    by_reason = summary.get("by_reason") or {}
    if by_reason:
        lines.append("\n<b>Kapanış sebebi</b>")
        for key, count in sorted(by_reason.items(), key=lambda x: -x[1]):
            lines.append(f"  {key}: <code>{count}</code>")

    return "\n".join(lines)

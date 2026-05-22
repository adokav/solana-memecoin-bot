"""Adaptive pozisyon büyüklüğü — paper trading verisinden öğrenir.

Default kapalı (ADAPTIVE_SIZING_ENABLED=false). Paper veri 1-2 hafta
biriktikten sonra açılır: her skor bucket'ının paper PnL'ine göre çarpan
hesaplar, BUY_AMOUNT_SOL × multiplier kadar alır.

Çarpan formülü (yarım-Kelly mantığı, yüksek varyansa karşı muhafazakar):
  avg_pnl_pct  ≤ -20%   →  0.0  (tamamen pas)
  avg_pnl_pct  ≤  0%    →  0.5  (küçük poz, doğrulama)
  avg_pnl_pct  ≤ 30%    →  1.0
  avg_pnl_pct  ≤ 80%    →  1.5
  avg_pnl_pct  >  80%   →  2.0

Bucket'ta min örnek sayısının altındaysa varsayılan 1.0 (yeterli kanıt yok).
"""
from __future__ import annotations

import logging

from config import config
from pnl import TradeStats, bucket_label, summarize
from storage import Position

log = logging.getLogger(__name__)


def _multiplier_from_stats(stats: TradeStats | None) -> float:
    if stats is None or stats.count < config.adaptive_sizing_min_samples:
        return 1.0
    ev = stats.avg_pnl_pct
    if ev <= -20:
        return 0.0
    if ev <= 0:
        return 0.5
    if ev <= 30:
        return 1.0
    if ev <= 80:
        return 1.5
    return 2.0


def size_for_candidate(
    score_total: float,
    paper_positions: list[Position] | None,
    base_amount_sol: float,
) -> tuple[float, str]:
    """Aday için SOL miktarı + açıklama döner.

    Adaptive kapalıysa veya paper verisi yoksa base miktar + 'flat' döner.
    """
    if not config.adaptive_sizing_enabled or paper_positions is None:
        return base_amount_sol, "flat"

    label = bucket_label(score_total)
    summary = summarize(paper_positions, days=0)
    bucket = (summary.get("by_score") or {}).get(label)
    mult = _multiplier_from_stats(bucket)
    sized = base_amount_sol * mult

    if bucket is None or bucket.count < config.adaptive_sizing_min_samples:
        note = f"bucket {label}: yetersiz veri (default 1.0x)"
    else:
        note = (
            f"bucket {label}: n={bucket.count} avg={bucket.avg_pnl_pct:+.1f}% "
            f"-> {mult:.1f}x"
        )

    log.info("adaptive sizing | score=%.1f | %s | %.4f SOL", score_total, note, sized)
    return sized, note

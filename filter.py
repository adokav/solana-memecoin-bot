"""5 hard gate — EV>0 hipotezi için minimum koşullar.

Felsefe: skor sistemi YOK. Bir aday ya geçer (BUY) ya geçmez (SKIP).
Sıralama gereksiz çünkü filtreyi geçen her aday alınmaya değer kabul edilir.

Gate'ler:
  1. Tradeable: liq >= MIN_LIQ_USD ($1k) — çıkışta makul slippage
  2. Fresh:     MIN_AGE_H <= age <= MAX_AGE_H — yeni momentum bölgesi
  3. Alive:     txns_h1 >= MIN_TXNS_H1 — gerçek aktivite var
  4. Bullish:   buy_ratio >= MIN_BUY_RATIO — alıcılar baskın
  5. Not crash: price_h1 >= MIN_PRICE_H1 — açık çöküşte değil
"""
from __future__ import annotations

from config import config
from candidate import Candidate


def passes(c: Candidate) -> tuple[bool, str]:
    """5 gate. Hepsi geçerse (True, "ok"); değilse (False, reason)."""

    # Quote token check
    if c.quote_symbol.upper() not in {"SOL", "WSOL", "USDC"}:
        return False, f"quote not SOL/USDC: {c.quote_symbol}"

    # Gate 1: tradeable
    if c.liquidity_usd < config.min_liq_usd:
        return False, f"liq ${c.liquidity_usd:.0f} < ${config.min_liq_usd:.0f}"
    if c.liquidity_usd > config.max_liq_usd:
        return False, f"liq ${c.liquidity_usd:.0f} > ${config.max_liq_usd:.0f}"

    # Gate 2: fresh
    if c.pair_age_h < config.min_age_h:
        return False, f"too fresh: {c.pair_age_h:.2f}h"
    if c.pair_age_h > config.max_age_h:
        return False, f"too old: {c.pair_age_h:.1f}h"

    # Gate 3: alive
    if c.txns_h1 < config.min_txns_h1:
        return False, f"low activity: {c.txns_h1} tx/h"

    # Honeypot heuristic: çok tx, sıfır sell
    if c.txns_h1 >= 20 and c.sells_h1 == 0:
        return False, "zero sells with activity (honeypot suspect)"

    # Gate 4: bullish
    buy_ratio = c.buys_h1 / max(c.txns_h1, 1)
    if buy_ratio < config.min_buy_ratio:
        return False, f"low buy ratio: {buy_ratio:.0%}"

    # Gate 5: not crashing
    if c.price_change_h1 < config.min_price_h1:
        return False, f"price h1 crashing: {c.price_change_h1:.1f}%"

    return True, "ok"

"""DexScreener pair dict → Candidate dataclass.

Sade, sadece filter.py'ın ihtiyacı olan alanlar.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    chain: str
    pair_address: str
    base_token: str
    base_symbol: str
    quote_symbol: str
    dex: str
    price_usd: float
    liquidity_usd: float
    volume_h1: float
    price_change_h1: float
    price_change_h6: float
    txns_h1: int
    buys_h1: int
    sells_h1: int
    pair_age_h: float
    url: str


def parse(p: dict) -> Candidate | None:
    """DexScreener pair dict'inden Candidate çıkar. Hatalıysa None."""
    try:
        if p.get("chainId") != "solana":
            return None
        liq = (p.get("liquidity") or {}).get("usd") or 0
        vol = p.get("volume") or {}
        chg = p.get("priceChange") or {}
        h1_txns = (p.get("txns") or {}).get("h1") or {}
        buys_h1 = int(h1_txns.get("buys", 0))
        sells_h1 = int(h1_txns.get("sells", 0))

        created_ms = p.get("pairCreatedAt") or 0
        age_h = (time.time() * 1000 - created_ms) / 3_600_000 if created_ms else 9999

        return Candidate(
            chain=p.get("chainId", ""),
            pair_address=p.get("pairAddress", ""),
            base_token=(p.get("baseToken") or {}).get("address", ""),
            base_symbol=(p.get("baseToken") or {}).get("symbol", "?"),
            quote_symbol=(p.get("quoteToken") or {}).get("symbol", "?"),
            dex=p.get("dexId", ""),
            price_usd=float(p.get("priceUsd") or 0),
            liquidity_usd=float(liq),
            volume_h1=float(vol.get("h1") or 0),
            price_change_h1=float(chg.get("h1") or 0),
            price_change_h6=float(chg.get("h6") or 0),
            txns_h1=buys_h1 + sells_h1,
            buys_h1=buys_h1,
            sells_h1=sells_h1,
            pair_age_h=age_h,
            url=p.get("url", ""),
        )
    except (TypeError, ValueError) as e:
        log.debug("candidate parse error: %s", e)
        return None

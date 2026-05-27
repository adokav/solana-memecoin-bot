"""Post-alert watchlist: warns when the setup starts breaking down."""
from __future__ import annotations

import time
from dataclasses import dataclass

from candidate import Candidate, parse as parse_candidate
from config import config
from dexscreener import DexScreener
from filter import buy_ratio
from storage import Store, WatchedToken


@dataclass
class WatchWarning:
    token_mint: str
    symbol: str
    pair_address: str
    url: str
    reasons: list[str]
    price_usd: float
    drawdown_pct: float
    liquidity_drop_pct: float


class WatchList:
    def __init__(self, store: Store, ds: DexScreener) -> None:
        self.store = store
        self.ds = ds

    def add_candidate(self, c: Candidate) -> None:
        self.store.upsert_watch(WatchedToken(
            pair_address=c.pair_address,
            base_token=c.base_token,
            symbol=c.base_symbol,
            url=c.url,
            first_price_usd=c.price_usd,
            peak_price_usd=c.price_usd,
            first_liquidity_usd=c.liquidity_usd,
            last_price_usd=c.price_usd,
            last_liquidity_usd=c.liquidity_usd,
            alerted_at=time.time(),
        ))

    def ignore(self, token_mint: str) -> bool:
        return self.store.ignore(token_mint)

    async def tick(self) -> list[WatchWarning]:
        warnings: list[WatchWarning] = []
        for watched in self.store.active_watches():
            pair = await self.ds.pair("solana", watched.pair_address)
            c = parse_candidate(pair or {})
            if c is None:
                continue

            if c.price_usd > watched.peak_price_usd:
                watched.peak_price_usd = c.price_usd

            watched.last_price_usd = c.price_usd
            watched.last_liquidity_usd = c.liquidity_usd
            watched.last_checked_at = time.time()

            drawdown = ((watched.peak_price_usd - c.price_usd) / max(watched.peak_price_usd, 1e-12)) * 100
            liq_drop = ((watched.first_liquidity_usd - c.liquidity_usd) / max(watched.first_liquidity_usd, 1.0)) * 100
            reasons: list[str] = []

            if drawdown >= config.warn_drawdown_from_peak_pct:
                reasons.append(f"Peak'ten -{drawdown:.1f}% geri çekilme")
            if liq_drop >= config.warn_liq_drop_pct:
                reasons.append(f"Likidite -{liq_drop:.1f}% azaldı")
            if buy_ratio(c) < config.warn_buy_ratio_below and c.txns_h1 >= 10:
                reasons.append(f"Buy pressure zayıfladı ({buy_ratio(c):.0%})")
            if c.price_change_h1 <= config.warn_price_h1_below:
                reasons.append(f"H1 momentum negatif ({c.price_change_h1:.1f}%)")

            if reasons and not watched.warned:
                watched.warned = True
                warnings.append(WatchWarning(
                    token_mint=watched.base_token,
                    symbol=watched.symbol,
                    pair_address=watched.pair_address,
                    url=watched.url,
                    reasons=reasons[:5],
                    price_usd=c.price_usd,
                    drawdown_pct=drawdown,
                    liquidity_drop_pct=liq_drop,
                ))

        self.store.save()
        return warnings

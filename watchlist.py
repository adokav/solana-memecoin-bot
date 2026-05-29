"""Post-alert watchlist: warns when the setup strengthens or breaks down."""
from __future__ import annotations

import time
from dataclasses import dataclass

from candidate import Candidate, parse as parse_candidate
from config import config
from dexscreener import DexScreener
from filter import buy_ratio, volume_liquidity_ratio
from opportunity import Opportunity, score as opportunity_score
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
    kind: str = "break"  # "strength" or "break"


class WatchList:
    def __init__(self, store: Store, ds: DexScreener) -> None:
        self.store = store
        self.ds = ds

    def add_candidate(self, c: Candidate, op: Opportunity | None = None) -> None:
        br = buy_ratio(c)
        vl = volume_liquidity_ratio(c)
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
            first_buy_ratio=br,
            first_volume_liq_ratio=vl,
            first_h1=c.price_change_h1,
            last_buy_ratio=br,
            last_volume_liq_ratio=vl,
            last_h1=c.price_change_h1,
            mode=getattr(op, "mode", "UNKNOWN") if op else "UNKNOWN",
            opportunity_score=getattr(op, "opportunity_score", 0) if op else 0,
            risk_score=getattr(op, "risk_score", 0) if op else 0,
            exit_score=getattr(op, "exit_score", 0) if op else 0,
            survival_score=getattr(op, "survival_score", 0) if op else 0,
            expansion_score=getattr(op, "expansion_score", 0) if op else 0,
            timing_score=getattr(op, "timing_score", 0) if op else 0,
            confidence_score=getattr(op, "confidence_score", 0) if op else 0,
            edge_score=getattr(op, "edge_score", 0) if op else 0,
            radar_score=getattr(op, "radar_score", 0) if op else 0,
            decision=getattr(op, "decision", "İZLE") if op else "İZLE",
        ))

    def ignore(self, token_mint: str) -> bool:
        return self.store.ignore(token_mint)

    async def tick(self) -> list[WatchWarning]:
        warnings: list[WatchWarning] = []
        now = time.time()

        for watched in self.store.active_watches():
            pair = await self.ds.pair("solana", watched.pair_address)
            c = parse_candidate(pair or {})
            if c is None:
                continue

            old_peak = watched.peak_price_usd
            if c.price_usd > watched.peak_price_usd:
                watched.peak_price_usd = c.price_usd

            br = buy_ratio(c)
            vl = volume_liquidity_ratio(c)
            current_op = opportunity_score(c, "watchlist rescore")
            previous_edge = int(getattr(watched, "edge_score", 0) or 0)
            previous_conf = int(getattr(watched, "confidence_score", 0) or 0)
            previous_survival = int(getattr(watched, "survival_score", 0) or 0)
            drawdown = ((watched.peak_price_usd - c.price_usd) / max(watched.peak_price_usd, 1e-12)) * 100
            liq_drop = ((watched.first_liquidity_usd - c.liquidity_usd) / max(watched.first_liquidity_usd, 1.0)) * 100

            # 1) Strengthening signal: not a buy command; it says the setup is improving.
            strength_reasons: list[str] = []
            price_gain = ((c.price_usd - watched.first_price_usd) / max(watched.first_price_usd, 1e-12)) * 100
            liq_change = ((c.liquidity_usd - watched.first_liquidity_usd) / max(watched.first_liquidity_usd, 1.0)) * 100

            if c.price_usd > old_peak and price_gain >= 8:
                strength_reasons.append(f"Yeni high denemesi; ilk radardan +{price_gain:.1f}%")
            if br >= max(0.55, watched.first_buy_ratio + 0.06) and c.txns_h1 >= 20:
                strength_reasons.append(f"Buy pressure güçlendi ({br:.0%})")
            if vl >= max(0.50, watched.first_volume_liq_ratio * 1.25):
                strength_reasons.append(f"Hacim/Likidite ivmesi arttı ({vl:.2f}x)")
            if liq_change >= 12:
                strength_reasons.append(f"Likidite büyüyor (+{liq_change:.1f}%)")
            if c.price_change_h1 >= 15 and c.price_change_h1 > watched.first_h1:
                strength_reasons.append(f"H1 momentum güçlendi ({c.price_change_h1:+.1f}%)")
            if current_op.edge_score >= previous_edge + 10 and current_op.confidence_score >= max(50, previous_conf):
                strength_reasons.append(f"Edge güçlendi ({previous_edge} → {current_op.edge_score})")
            if current_op.survival_score >= previous_survival + 8:
                strength_reasons.append(f"Survival güçlendi ({previous_survival} → {current_op.survival_score})")

            # Send strength at most every 15 min per token and only with at least 2 confirmations.
            if (
                len(strength_reasons) >= 2
                and (now - watched.last_strength_alert_at) >= 15 * 60
                and drawdown < 18
                and liq_drop < 15
            ):
                watched.last_strength_alert_at = now
                warnings.append(WatchWarning(
                    token_mint=watched.base_token,
                    symbol=watched.symbol,
                    pair_address=watched.pair_address,
                    url=watched.url,
                    reasons=strength_reasons[:5],
                    price_usd=c.price_usd,
                    drawdown_pct=drawdown,
                    liquidity_drop_pct=liq_drop,
                    kind="strength",
                ))

            # 2) Breakdown signal.
            break_reasons: list[str] = []
            if drawdown >= config.warn_drawdown_from_peak_pct:
                break_reasons.append(f"Peak'ten -{drawdown:.1f}% geri çekilme")
            if liq_drop >= config.warn_liq_drop_pct:
                break_reasons.append(f"Likidite -{liq_drop:.1f}% azaldı")
            if br < config.warn_buy_ratio_below and c.txns_h1 >= 10:
                break_reasons.append(f"Buy pressure zayıfladı ({br:.0%})")
            if c.price_change_h1 <= config.warn_price_h1_below:
                break_reasons.append(f"H1 momentum negatife döndü ({c.price_change_h1:.1f}%)")
            if vl < max(0.05, watched.first_volume_liq_ratio * 0.45):
                break_reasons.append(f"Hacim/Likidite ivmesi söndü ({vl:.2f}x)")
            if previous_edge and current_op.edge_score <= previous_edge - 18:
                break_reasons.append(f"Edge skoru düştü ({previous_edge} → {current_op.edge_score})")
            if previous_conf and current_op.confidence_score <= previous_conf - 15:
                break_reasons.append(f"Confidence düştü ({previous_conf} → {current_op.confidence_score})")
            if current_op.decision == "UZAK DUR":
                break_reasons.append("Karar UZAK DUR seviyesine indi")

            # Send breakdown every 10 min max, not only once. Memecoin decay can happen fast.
            if break_reasons and (now - watched.last_break_alert_at) >= 10 * 60:
                watched.warned = True
                watched.last_break_alert_at = now
                warnings.append(WatchWarning(
                    token_mint=watched.base_token,
                    symbol=watched.symbol,
                    pair_address=watched.pair_address,
                    url=watched.url,
                    reasons=break_reasons[:5],
                    price_usd=c.price_usd,
                    drawdown_pct=drawdown,
                    liquidity_drop_pct=liq_drop,
                    kind="break",
                ))

            watched.last_price_usd = c.price_usd
            watched.last_liquidity_usd = c.liquidity_usd
            watched.last_buy_ratio = br
            watched.last_volume_liq_ratio = vl
            watched.last_h1 = c.price_change_h1
            watched.opportunity_score = current_op.opportunity_score
            watched.risk_score = current_op.risk_score
            watched.exit_score = current_op.exit_score
            watched.survival_score = current_op.survival_score
            watched.expansion_score = current_op.expansion_score
            watched.timing_score = current_op.timing_score
            watched.confidence_score = current_op.confidence_score
            watched.edge_score = current_op.edge_score
            watched.radar_score = current_op.radar_score
            watched.decision = current_op.decision
            watched.mode = current_op.mode
            watched.last_checked_at = now

        self.store.save()
        return warnings

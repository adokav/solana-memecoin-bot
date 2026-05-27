"""Strict but simple candidate filters for manual memecoin alerts."""
from __future__ import annotations

from candidate import Candidate
from config import config


def buy_ratio(c: Candidate) -> float:
    return c.buys_h1 / max(c.txns_h1, 1)


def volume_liquidity_ratio(c: Candidate) -> float:
    return c.volume_h1 / max(c.liquidity_usd, 1.0)


def passes(c: Candidate) -> tuple[bool, str]:
    if c.quote_symbol.upper() not in {"SOL", "WSOL", "USDC"}:
        return False, f"quote not SOL/USDC: {c.quote_symbol}"

    if c.price_usd <= 0:
        return False, "invalid price"

    if c.liquidity_usd < config.min_liq_usd:
        return False, f"liq ${c.liquidity_usd:.0f} < ${config.min_liq_usd:.0f}"
    if c.liquidity_usd > config.max_liq_usd:
        return False, f"liq ${c.liquidity_usd:.0f} > ${config.max_liq_usd:.0f}"

    if c.pair_age_h < config.min_age_h:
        return False, f"too fresh: {c.pair_age_h:.2f}h"
    if c.pair_age_h > config.max_age_h:
        return False, f"too old: {c.pair_age_h:.1f}h"

    if c.txns_h1 < config.min_txns_h1:
        return False, f"low activity: {c.txns_h1} tx/h"
    if c.volume_h1 < config.min_volume_h1_usd:
        return False, f"low h1 volume: ${c.volume_h1:.0f}"
    if c.sells_h1 < config.min_sells_h1:
        return False, f"too few sells: {c.sells_h1}"

    ratio = buy_ratio(c)
    if ratio < config.min_buy_ratio:
        return False, f"low buy ratio: {ratio:.0%}"
    if ratio > config.max_buy_ratio and c.sells_h1 < 3:
        return False, f"one-sided flow: buy ratio {ratio:.0%}, sells {c.sells_h1}"

    vlr = volume_liquidity_ratio(c)
    if vlr < config.min_volume_liq_ratio:
        return False, f"low volume/liquidity: {vlr:.2f}"
    if vlr > config.max_volume_liq_ratio:
        return False, f"wash/noisy volume-liquidity: {vlr:.1f}"

    if c.price_change_h1 < config.min_price_h1:
        return False, f"h1 crashing: {c.price_change_h1:.1f}%"
    if c.price_change_h1 > config.max_price_h1:
        return False, f"overextended h1: {c.price_change_h1:.1f}%"

    if c.price_change_h6 < config.min_price_h6:
        return False, f"h6 weak: {c.price_change_h6:.1f}%"
    if c.price_change_h6 > config.max_price_h6:
        return False, f"overextended h6: {c.price_change_h6:.1f}%"

    return True, "ok"

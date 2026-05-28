"""Hard noise/scam gate for the memecoin radar.

Bu katman "alınır/alınmaz" kararı vermez.
Sadece matematiksel olarak ölçülemeyen veya çıkışı zayıf bariz çöpleri eler.
Asıl seçim opportunity.score() içindeki olasılıksal skorlama ile yapılır.
"""
from __future__ import annotations

from candidate import Candidate
from config import config


def buy_ratio(c: Candidate) -> float:
    return c.buys_h1 / max(c.txns_h1, 1)


def volume_liquidity_ratio(c: Candidate) -> float:
    return c.volume_h1 / max(c.liquidity_usd, 1.0)


def passes(c: Candidate) -> tuple[bool, str]:
    # 1) Trade edilebilir quote.
    if c.quote_symbol.upper() not in {"SOL", "WSOL", "USDC"}:
        return False, f"quote not SOL/USDC: {c.quote_symbol}"

    # 2) Geçerli fiyat/likidite. Likidite alt sınırı erken fırsatı kaçırmamak için düşük;
    # alım sinyali daha sonra exit_score ile ayrılır.
    if c.price_usd <= 0:
        return False, "invalid price"
    if c.liquidity_usd < config.early_min_liq_usd:
        return False, f"liq ${c.liquidity_usd:.0f} < early ${config.early_min_liq_usd:.0f}"
    if c.liquidity_usd > config.max_liq_usd:
        return False, f"liq ${c.liquidity_usd:.0f} > ${config.max_liq_usd:.0f}"

    # 3) Çok yeni tokenlarda veri yoktur; çok eski tokenlar radarın edge penceresi dışında kalır.
    if c.pair_age_h < config.early_min_age_h:
        return False, f"too fresh: {c.pair_age_h:.2f}h"
    if c.pair_age_h > config.max_age_h:
        return False, f"too old: {c.pair_age_h:.1f}h"

    # 4) Minimum organik aktivite. Tek tx/tek cüzdan pump'larını ele.
    if c.txns_h1 < config.early_min_txns_h1:
        return False, f"low early activity: {c.txns_h1} tx/h"
    if c.volume_h1 < config.early_min_volume_h1_usd:
        return False, f"low early h1 volume: ${c.volume_h1:.0f}"
    if c.sells_h1 < config.early_min_sells_h1:
        return False, f"too few sells: {c.sells_h1}"

    ratio = buy_ratio(c)
    # Çok zayıf akış veya satışsız tek taraflı akış bariz risk.
    if ratio < 0.38:
        return False, f"weak buy ratio: {ratio:.0%}"
    if ratio > 0.94 and c.sells_h1 < 3:
        return False, f"one-sided flow: buy ratio {ratio:.0%}, sells {c.sells_h1}"

    vlr = volume_liquidity_ratio(c)
    if vlr < config.early_min_volume_liq_ratio:
        return False, f"low early volume/liquidity: {vlr:.2f}"
    # Eskiden 8x üstü hard reject idi; bu bazı erken hot coinleri kaçırıyordu.
    # Artık sadece aşırı anormal/noisy durumları eliyoruz; geri kalanı risk skoruna bırakıyoruz.
    if vlr > config.max_volume_liq_ratio:
        return False, f"extreme volume/liquidity: {vlr:.1f}"

    # Aşırı çöküş hard reject; aşırı pump ise skor katmanında risk olarak ele alınır.
    if c.price_change_h1 < config.min_price_h1:
        return False, f"h1 crashing: {c.price_change_h1:.1f}%"
    if c.price_change_h6 < config.min_price_h6:
        return False, f"h6 weak: {c.price_change_h6:.1f}%"

    return True, "ok"

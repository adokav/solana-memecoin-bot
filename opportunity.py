"""Opportunity and risk scoring for Telegram alerts.

The score is not a buy signal. It is a compact way to rank manually reviewed
memecoins after hard scam/rug filters have passed.
"""
from __future__ import annotations

from dataclasses import dataclass

from candidate import Candidate
from filter import buy_ratio, volume_liquidity_ratio


@dataclass
class Opportunity:
    opportunity_score: int
    risk_score: int
    reasons: list[str]
    cautions: list[str]
    safety: str


def _clamp(value: float, lo: float = 0, hi: float = 100) -> int:
    return int(max(lo, min(hi, round(value))))


def score(c: Candidate, safety_reason: str = "ok") -> Opportunity:
    br = buy_ratio(c)
    vlr = volume_liquidity_ratio(c)

    opportunity = 35.0
    reasons: list[str] = []
    cautions: list[str] = []

    if c.liquidity_usd >= 20_000:
        opportunity += 12
        reasons.append("Likidite giriş/çıkış için daha sağlıklı")
    elif c.liquidity_usd >= 8_000:
        opportunity += 7
        reasons.append("Likidite minimum eşiğin üstünde")
    else:
        cautions.append("Likidite hâlâ ince; çıkış kayabilir")

    if 0.55 <= br <= 0.75:
        opportunity += 18
        reasons.append("Alıcı/satıcı dengesi doğal görünüyor")
    elif br > 0.75:
        opportunity += 8
        cautions.append("Alım baskısı çok tek taraflı; fake pump olabilir")
    else:
        opportunity += 4

    if 0.7 <= vlr <= 5:
        opportunity += 18
        reasons.append("Hacim/likidite oranı canlı ama aşırı değil")
    elif 0.3 <= vlr < 0.7:
        opportunity += 8
        reasons.append("Hacim/likidite oranı kabul edilebilir")
    else:
        opportunity += 4
        cautions.append("Hacim/likidite oranı gürültülü olabilir")

    if 5 <= c.price_change_h1 <= 120:
        opportunity += 12
        reasons.append("Momentum var, aşırı uzamamış")
    elif c.price_change_h1 > 120:
        opportunity += 4
        cautions.append("H1 pump yüksek; FOMO riski var")
    elif c.price_change_h1 >= -5:
        opportunity += 6
        reasons.append("Fiyat h1 bazında stabil")

    if 0.25 <= c.pair_age_h <= 24:
        opportunity += 5
        reasons.append("Fresh ama ilk dakikaların kaosu geçmiş")

    risk = 20.0
    if c.liquidity_usd < 10_000:
        risk += 18
    if c.txns_h1 < 50:
        risk += 12
    if br > 0.85:
        risk += 15
    if c.price_change_h1 > 150:
        risk += 18
    if c.price_change_h6 < -20:
        risk += 12
    if vlr > 8:
        risk += 12

    if not cautions:
        cautions.append("Memecoin riski devam eder; manuel onay şart")

    return Opportunity(
        opportunity_score=_clamp(opportunity),
        risk_score=_clamp(risk),
        reasons=reasons[:5] or ["Hard filtreler ve safety kontrolleri geçti"],
        cautions=cautions[:4],
        safety=safety_reason,
    )

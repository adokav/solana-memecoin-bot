
"""Opportunity/risk scoring for memecoin radar.

Compatible with the existing TelegramHub/main.py contract:
- main.py imports score
- telegram_hub.py imports Opportunity
- send_opportunity expects opportunity_score, risk_score, reasons, cautions
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from candidate import Candidate


SignalMode = Literal["EARLY WATCH", "CONFIRMED SIGNAL"]


@dataclass
class Opportunity:
    opportunity_score: int
    risk_score: int
    exit_score: int
    mode: SignalMode
    reasons: list[str]
    cautions: list[str]


def _clamp(value: float, lo: int = 0, hi: int = 100) -> int:
    return int(max(lo, min(hi, round(value))))


def score(c: Candidate, safety_reason: str = "safety ok") -> Opportunity:
    """Return a two-stage radar score.

    EARLY WATCH keeps promising fresh tokens visible without calling them a buy candidate.
    CONFIRMED SIGNAL requires stronger liquidity/activity/exit assumptions.
    """
    liq = float(c.liquidity_usd or 0)
    tx = int(c.txns_h1 or 0)
    sells = int(c.sells_h1 or 0)
    buy_ratio_pct = (c.buys_h1 / max(tx, 1)) * 100.0
    vol_liq = c.volume_h1 / max(liq, 1.0)
    h1 = float(c.price_change_h1 or 0)
    h6 = float(c.price_change_h6 or 0)
    age_min = float(c.pair_age_h or 0) * 60.0

    opportunity = 35.0
    risk = 35.0
    exit_score = 45.0
    reasons: list[str] = []
    cautions: list[str] = []

    # Early freshness: good for opportunity, but also riskier.
    if 2 <= age_min <= 30:
        opportunity += 18
        risk += 12
        reasons.append(f"Erken faz: {age_min:.0f} dk")
        cautions.append("Erken faz; veri hâlâ sınırlı")
    elif 30 < age_min <= 180:
        opportunity += 10
        risk -= 3
        reasons.append(f"Veri oluşmuş erken token: {age_min/60:.1f} saat")
    elif age_min > 180:
        opportunity -= 8
        cautions.append("Erken fırsat penceresinden uzaklaşıyor")

    # Liquidity.
    if 2_000 <= liq < 5_000:
        opportunity += 7
        risk += 14
        exit_score -= 8
        reasons.append(f"İlk likidite oluşmuş: ${liq:,.0f}")
        cautions.append("Likidite düşük; çıkış kayabilir")
    elif 5_000 <= liq <= 150_000:
        opportunity += 14
        risk -= 8
        exit_score += 15
        reasons.append(f"Likidite sağlıklı: ${liq:,.0f}")
    elif liq > 150_000:
        opportunity += 4
        risk -= 5
        exit_score += 10
        reasons.append(f"Likidite yüksek: ${liq:,.0f}")

    # Activity.
    if 10 <= tx < 30:
        opportunity += 7
        reasons.append(f"Erken işlem aktivitesi: {tx} tx/h")
    elif tx >= 30:
        opportunity += 14
        exit_score += 5
        reasons.append(f"İşlem yoğunluğu güçlü: {tx} tx/h")

    # Buy/sell balance.
    if sells <= 0 and tx >= 10:
        risk += 25
        exit_score -= 20
        cautions.append("Sell işlemi görünmüyor; honeypot/çıkış riski")
    elif sells > 0:
        exit_score += 8
        reasons.append(f"Sell işlemleri mevcut: {sells}/h")

    if 52 <= buy_ratio_pct <= 78:
        opportunity += 14
        risk -= 6
        reasons.append(f"Buy pressure sağlıklı: %{buy_ratio_pct:.0f}")
    elif 78 < buy_ratio_pct <= 90:
        opportunity += 7
        risk += 8
        cautions.append(f"Buy ratio yüksek: %{buy_ratio_pct:.0f}")
    elif buy_ratio_pct > 90:
        opportunity -= 6
        risk += 22
        cautions.append(f"Buy ratio aşırı tek taraflı: %{buy_ratio_pct:.0f}")
    elif buy_ratio_pct < 48:
        opportunity -= 12
        risk += 8
        cautions.append(f"Buy pressure zayıf: %{buy_ratio_pct:.0f}")

    # Volume/liquidity.
    if 1.2 <= vol_liq <= 8:
        opportunity += 12
        reasons.append(f"Hacim/Likidite aktif: {vol_liq:.2f}x")
    elif 0.4 <= vol_liq < 1.2:
        opportunity += 4
        reasons.append(f"Hacim/Likidite oluşuyor: {vol_liq:.2f}x")
    elif vol_liq > 8:
        risk += 12
        cautions.append(f"Hacim/Likidite çok yüksek: {vol_liq:.1f}x")

    # Momentum.
    if 10 <= h1 <= 180:
        opportunity += 12
        reasons.append(f"h1 momentum uygun: %{h1:+.1f}")
    elif h1 > 180:
        opportunity -= 4
        risk += 10
        cautions.append(f"h1 aşırı şişmiş: %{h1:+.1f}")
    elif h1 < -15:
        opportunity -= 15
        risk += 8
        cautions.append(f"h1 zayıf/çöküşte: %{h1:+.1f}")

    if h6 > 400:
        risk += 15
        cautions.append(f"h6 aşırı şişmiş: %{h6:+.1f}")

    if safety_reason:
        reasons.append(str(safety_reason))

    # Two-stage mode.
    confirmed = (
        liq >= 5_000
        and tx >= 30
        and sells >= 2
        and 48 <= buy_ratio_pct <= 85
        and risk <= 65
        and exit_score >= 45
    )
    mode: SignalMode = "CONFIRMED SIGNAL" if confirmed else "EARLY WATCH"

    if not reasons:
        reasons.append("Temel radar eşiğini geçti")
    if not cautions:
        cautions.append("Memecoin riski yüksek; manuel onay şart")

    return Opportunity(
        opportunity_score=_clamp(opportunity),
        risk_score=_clamp(risk),
        exit_score=_clamp(exit_score),
        mode=mode,
        reasons=reasons[:8],
        cautions=cautions[:8],
    )

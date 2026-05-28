"""Opportunity/risk/exit scoring for memecoin radar.

Felsefe:
- Hard filter sadece bariz çöpleri eler.
- Bu dosya coinleri olasılıksal olarak puanlar.
- Telegram'a "ALINABİLİR RADAR" yalnızca risk/exit/opportunity dengesi yeterliyse gider.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from candidate import Candidate
from config import config


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
    liq = float(c.liquidity_usd or 0)
    tx = int(c.txns_h1 or 0)
    sells = int(c.sells_h1 or 0)
    buy_ratio_pct = (c.buys_h1 / max(tx, 1)) * 100.0
    vol_liq = c.volume_h1 / max(liq, 1.0)
    h1 = float(c.price_change_h1 or 0)
    h6 = float(c.price_change_h6 or 0)
    age_min = float(c.pair_age_h or 0) * 60.0

    opportunity = 30.0
    risk = 38.0
    exit_score = 42.0
    reasons: list[str] = []
    cautions: list[str] = []

    # 1) Zaman penceresi: erken ama verisiz olmayan coin daha değerlidir.
    if 3 <= age_min <= 25:
        opportunity += 18
        risk += 9
        reasons.append(f"Erken fırsat penceresi: {age_min:.0f} dk")
        cautions.append("Çok erken faz; veri hızlı değişebilir")
    elif 25 < age_min <= 180:
        opportunity += 12
        risk -= 3
        reasons.append(f"Erken ama veri oluşmuş: {age_min/60:.1f} saat")
    elif 180 < age_min <= 720:
        opportunity += 2
        risk += 4
        cautions.append(f"Erken pencere zayıflıyor: {age_min/60:.1f} saat")
    else:
        opportunity -= 8
        risk += 8
        cautions.append(f"Radar için yaşlı token: {age_min/60:.1f} saat")

    # 2) Likidite ve çıkılabilirlik.
    if 1_500 <= liq < 5_000:
        opportunity += 8
        risk += 14
        exit_score -= 8
        reasons.append(f"İlk likidite oluşmuş: ${liq:,.0f}")
        cautions.append("Likidite düşük; emir kayabilir")
    elif 5_000 <= liq <= 75_000:
        opportunity += 16
        risk -= 9
        exit_score += 18
        reasons.append(f"Likidite/erkenlik dengesi iyi: ${liq:,.0f}")
    elif 75_000 < liq <= 250_000:
        opportunity += 10
        risk -= 8
        exit_score += 16
        reasons.append(f"Likidite güçlü: ${liq:,.0f}")
    else:
        opportunity += 3
        risk -= 2
        exit_score += 8
        cautions.append(f"Likidite yüksek; erken çarpan potansiyeli düşebilir: ${liq:,.0f}")

    # 3) İşlem sayısı: gürültü değil, örneklem büyüklüğü.
    if 10 <= tx < 30:
        opportunity += 7
        reasons.append(f"Erken aktivite var: {tx} tx/h")
    elif 30 <= tx < 250:
        opportunity += 14
        exit_score += 5
        reasons.append(f"Organik aktivite güçlü: {tx} tx/h")
    elif tx >= 250:
        opportunity += 10
        exit_score += 8
        risk += 3
        reasons.append(f"Yüksek piyasa ilgisi: {tx} tx/h")

    # 4) Satış varlığı: çıkışın canlı kanıtı.
    if sells <= 0 and tx >= 10:
        risk += 28
        exit_score -= 25
        cautions.append("Sell görünmüyor; çıkış/honeypot riski")
    elif 1 <= sells < 3:
        exit_score += 4
        risk += 5
        reasons.append(f"Sell var ama örneklem düşük: {sells}/h")
    else:
        exit_score += 11
        risk -= 4
        reasons.append(f"Sell akışı mevcut: {sells}/h")

    # 5) Buy/sell dengesi: ne ölü, ne de yapay tek taraflı.
    if 54 <= buy_ratio_pct <= 76:
        opportunity += 16
        risk -= 8
        reasons.append(f"Buy pressure dengeli: %{buy_ratio_pct:.0f}")
    elif 48 <= buy_ratio_pct < 54:
        opportunity += 6
        risk += 2
        cautions.append(f"Buy pressure sınırda: %{buy_ratio_pct:.0f}")
    elif 76 < buy_ratio_pct <= 88:
        opportunity += 8
        risk += 7
        reasons.append(f"Alıcı ilgisi yüksek: %{buy_ratio_pct:.0f}")
        cautions.append("Buy ratio yüksek; tepeden mal verme riski izlenmeli")
    elif buy_ratio_pct > 88:
        opportunity -= 4
        risk += 20
        cautions.append(f"Aşırı tek taraflı buy flow: %{buy_ratio_pct:.0f}")
    else:
        opportunity -= 10
        risk += 8
        cautions.append(f"Buy pressure zayıf: %{buy_ratio_pct:.0f}")

    # 6) Hacim/Likidite: canlılık ve manipülasyon dengesi.
    if 0.35 <= vol_liq < 1.2:
        opportunity += 6
        reasons.append(f"Hacim/Likidite oluşuyor: {vol_liq:.2f}x")
    elif 1.2 <= vol_liq <= 6:
        opportunity += 15
        reasons.append(f"Hacim/Likidite güçlü: {vol_liq:.2f}x")
    elif 6 < vol_liq <= 12:
        opportunity += 8
        risk += 8
        reasons.append(f"Çok sıcak hacim: {vol_liq:.1f}x")
        cautions.append("Hacim/likidite yüksek; wash veya kalabalık trade olabilir")
    elif vol_liq > 12:
        opportunity -= 5
        risk += 18
        cautions.append(f"Aşırı hacim/likidite: {vol_liq:.1f}x")

    # 7) Momentum: erken ivme iyi, parabolik aşırılık risk.
    if -5 <= h1 < 10:
        opportunity += 3
        reasons.append(f"H1 sakin/toparlanma bölgesi: %{h1:+.1f}")
    elif 10 <= h1 <= 120:
        opportunity += 14
        reasons.append(f"H1 momentum sağlıklı: %{h1:+.1f}")
    elif 120 < h1 <= 300:
        opportunity += 5
        risk += 8
        cautions.append(f"H1 hızlı koşmuş: %{h1:+.1f}")
    elif h1 > 300:
        opportunity -= 8
        risk += 18
        cautions.append(f"H1 parabolik/aşırı: %{h1:+.1f}")
    elif h1 < -5:
        opportunity -= 8
        risk += 6
        cautions.append(f"H1 zayıf: %{h1:+.1f}")

    if h6 > 800:
        risk += 16
        cautions.append(f"H6 aşırı şişmiş: %{h6:+.1f}")
    elif 0 <= h6 <= 400:
        opportunity += 3

    if safety_reason:
        if "unknown" in safety_reason.lower() or "unreachable" in safety_reason.lower():
            risk += 8
            cautions.append(str(safety_reason))
        else:
            exit_score += 8
            reasons.append(str(safety_reason))

    opportunity_i = _clamp(opportunity)
    risk_i = _clamp(risk)
    exit_i = _clamp(exit_score)

    confirmed = (
        opportunity_i >= config.min_alert_opportunity_score
        and risk_i <= config.max_alert_risk_score
        and exit_i >= config.min_alert_exit_score
        and liq >= config.min_liq_usd
        and tx >= config.min_txns_h1
        and sells >= config.min_sells_h1
        and 45 <= buy_ratio_pct <= 88
    )
    mode: SignalMode = "CONFIRMED SIGNAL" if confirmed else "EARLY WATCH"

    if not reasons:
        reasons.append("Olasılıksal radar eşiğine yaklaştı")
    if not cautions:
        cautions.append("Memecoin riski yüksek; manuel onay şart")

    return Opportunity(
        opportunity_score=opportunity_i,
        risk_score=risk_i,
        exit_score=exit_i,
        mode=mode,
        reasons=reasons[:8],
        cautions=cautions[:8],
    )


def is_actionable(op: Opportunity) -> bool:
    """True ise Telegram'a ALINABİLİR RADAR bildirimi gönderilir."""
    return (
        op.mode == "CONFIRMED SIGNAL"
        and op.opportunity_score >= config.min_alert_opportunity_score
        and op.risk_score <= config.max_alert_risk_score
        and op.exit_score >= config.min_alert_exit_score
    )

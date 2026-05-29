"""Probability-first scoring engine for the Solana memecoin radar.

V11 yaklaşımı:
- Hard filter sadece bariz veri/likidite çöpünü eler.
- Alınabilir kararını burada, olasılıksal skorlar verir.
- "İyi görünen" tek metrikleri kör ödüllendirmez; özellikle wash/crowded/late riskini cezalandırır.
- Eski alanlar korunur: opportunity_score, risk_score, exit_score, mode, reasons, cautions.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from candidate import Candidate
from config import config


SignalMode = Literal["EARLY WATCH", "CONFIRMED SIGNAL"]
Decision = Literal["ALINABİLİR", "İZLE", "UZAK DUR"]


@dataclass
class Opportunity:
    # Backward-compatible fields.
    opportunity_score: int
    risk_score: int
    exit_score: int
    mode: SignalMode
    reasons: list[str]
    cautions: list[str]

    # Probability model fields.
    survival_score: int = 0
    expansion_score: int = 0
    timing_score: int = 0
    confidence_score: int = 0
    edge_score: int = 0
    radar_score: int = 0
    decision: Decision = "İZLE"


def _clamp(value: float, lo: int = 0, hi: int = 100) -> int:
    return int(max(lo, min(hi, round(value))))


def _interval_score(x: float, ideal_low: float, ideal_high: float, floor_low: float, floor_high: float) -> float:
    """0..100 score: ideal aralık 100; dışarıda lineer düşer."""
    if ideal_low <= x <= ideal_high:
        return 100.0
    if x < ideal_low:
        if x <= floor_low:
            return 0.0
        return 100.0 * (x - floor_low) / max(ideal_low - floor_low, 1e-9)
    if x >= floor_high:
        return 0.0
    return 100.0 * (floor_high - x) / max(floor_high - ideal_high, 1e-9)


def _safe_ratio(num: float, den: float, default: float = 0.0) -> float:
    return default if den <= 0 else num / den


def _risk_label(risk: int) -> str:
    if risk <= 25:
        return "düşük"
    if risk <= 45:
        return "orta"
    if risk <= 65:
        return "yüksek"
    return "çok yüksek"


def score(c: Candidate, safety_reason: str = "safety ok") -> Opportunity:
    liq = float(c.liquidity_usd or 0)
    tx = int(c.txns_h1 or 0)
    buys = int(c.buys_h1 or 0)
    sells = int(c.sells_h1 or 0)
    buy_ratio = _safe_ratio(buys, max(tx, 1))
    sell_ratio = _safe_ratio(sells, max(tx, 1))
    buy_ratio_pct = buy_ratio * 100.0
    vol_h1 = float(c.volume_h1 or 0)
    vol_liq = _safe_ratio(vol_h1, max(liq, 1.0))
    h1 = float(c.price_change_h1 or 0)
    h6 = float(c.price_change_h6 or 0)
    age_min = float(c.pair_age_h or 0) * 60.0
    safety_text = (safety_reason or "").lower()

    reasons: list[str] = []
    cautions: list[str] = []

    # ------------------------------------------------------------------
    # Market microstructure risk flags.
    # ------------------------------------------------------------------
    wash_risk = 0.0
    crowd_risk = 0.0

    if tx >= 800 and vol_liq < 1.2:
        wash_risk += 28
        cautions.append(f"Tx çok yüksek ama hacim/likidite düşük; mikro-wash riski ({tx}/h, {vol_liq:.2f}x)")
    elif tx >= 1200:
        wash_risk += 18
        cautions.append(f"Aşırı yoğun akış; bot/crowded trade riski ({tx}/h)")

    if vol_liq > 12:
        wash_risk += 24
        cautions.append(f"Aşırı hacim/likidite; wash veya dağıtım riski ({vol_liq:.1f}x)")
    elif vol_liq > 7:
        wash_risk += 10
        cautions.append(f"Hacim çok sıcak; sürdürülebilirlik izlenmeli ({vol_liq:.1f}x)")

    if buy_ratio > 0.88:
        wash_risk += 22
        cautions.append(f"Aşırı tek taraflı buy flow: %{buy_ratio_pct:.0f}")
    if sells <= 0 and tx >= 8:
        wash_risk += 35
        cautions.append("Sell akışı yok; honeypot/exit riski")
    if h1 > 300:
        crowd_risk += 24
        cautions.append(f"H1 parabolik; tepeden giriş riski: %{h1:+.1f}")
    elif h1 > 160:
        crowd_risk += 12
        cautions.append(f"H1 hızlı koşmuş: %{h1:+.1f}")
    if h6 > 700:
        crowd_risk += 18
        cautions.append(f"H6 aşırı şişmiş: %{h6:+.1f}")
    if age_min > 360:
        crowd_risk += min(25, (age_min - 360) / 60 * 3)
        cautions.append(f"Erkenlik avantajı azalıyor: {age_min/60:.1f} saat")

    # ------------------------------------------------------------------
    # Survival: tokenın ölmeden/çıkışı kilitlemeden yaşama ihtimali.
    # ------------------------------------------------------------------
    liq_survival = _interval_score(liq, 8_000, 220_000, 1_000, 900_000)
    sell_survival = _interval_score(sells, 3, 140, 0, 500)
    buy_balance = _interval_score(buy_ratio, 0.54, 0.76, 0.35, 0.94)
    age_survival = _interval_score(age_min, 8, 360, 1, 4_320)

    survival = (
        0.35 * liq_survival
        + 0.25 * sell_survival
        + 0.25 * buy_balance
        + 0.15 * age_survival
    )

    if "honeypot" in safety_text or "no token->sol" in safety_text or "no route" in safety_text:
        survival -= 45
        cautions.append(str(safety_reason))
    elif "mint authority" in safety_text or "freeze authority" in safety_text:
        survival -= 35
        cautions.append(str(safety_reason))
    elif "unknown" in safety_text or "unreachable" in safety_text:
        survival -= 8
        cautions.append(str(safety_reason))
    elif safety_reason and safety_reason != "ok":
        survival += 6
        reasons.append(str(safety_reason))

    survival -= 0.55 * wash_risk
    survival_i = _clamp(survival)

    if liq >= 5_000:
        reasons.append(f"Likidite survival için yeterli: ${liq:,.0f}")
    if sells >= 3:
        reasons.append(f"Satış akışı mevcut: {sells}/h")
    if 0.54 <= buy_ratio <= 0.76:
        reasons.append(f"Buy/sell dengesi sağlıklı: %{buy_ratio_pct:.0f}")

    # ------------------------------------------------------------------
    # Expansion: büyüme potansiyeli. Salt kalabalık değil, sürdürülebilir sıcaklık.
    # ------------------------------------------------------------------
    tx_heat = _interval_score(tx, 25, 450, 5, 1600)
    vol_heat = _interval_score(vol_liq, 0.6, 5.5, 0.05, 18)
    momentum_heat = _interval_score(h1, 8, 150, -25, 420)
    h6_heat = _interval_score(h6, -10, 420, -80, 1_200)

    expansion = 0.32 * tx_heat + 0.33 * vol_heat + 0.25 * momentum_heat + 0.10 * h6_heat
    expansion -= 0.35 * wash_risk
    expansion -= 0.20 * crowd_risk
    expansion_i = _clamp(expansion)

    if tx >= 25:
        reasons.append(f"İşlem akışı var: {tx}/h")
    if 0.6 <= vol_liq <= 5.5:
        reasons.append(f"Hacim/Likidite aktif: {vol_liq:.2f}x")
    if 8 <= h1 <= 150:
        reasons.append(f"H1 momentum sağlıklı: %{h1:+.1f}")
    elif -8 <= h1 < 8:
        reasons.append(f"H1 sakin/toparlanma bölgesi: %{h1:+.1f}")

    # ------------------------------------------------------------------
    # Exit: girildiğinde çıkılabilirlik.
    # ------------------------------------------------------------------
    liq_exit = _interval_score(liq, 10_000, 280_000, 1_000, 1_000_000)
    sells_exit = _interval_score(sells, 4, 180, 0, 600)
    route_exit = 65.0
    if "jupiter exit ok" in safety_text or "exit ok" in safety_text:
        route_exit = 100.0
        reasons.append("Jupiter çıkış testi geçti")
    elif "unknown" in safety_text or "unreachable" in safety_text:
        route_exit = 50.0
    elif "honeypot" in safety_text or "no route" in safety_text:
        route_exit = 0.0

    exit_score = 0.45 * liq_exit + 0.30 * sells_exit + 0.25 * route_exit
    exit_score -= 0.30 * wash_risk
    if buy_ratio > 0.90:
        exit_score -= 15
    exit_i = _clamp(exit_score)

    # ------------------------------------------------------------------
    # Timing: fırsat penceresi. İdeal: 10 dk - 4 saat, 4-8 saat izlenebilir.
    # ------------------------------------------------------------------
    timing = _interval_score(age_min, 10, 240, 2, 1_440)
    if 3 <= age_min < 10:
        reasons.append(f"Çok erken radar penceresi: {age_min:.0f} dk")
        cautions.append("Erken faz; veri güveni düşük")
    elif 10 <= age_min <= 240:
        reasons.append(f"İdeal erkenlik penceresi: {age_min:.0f} dk")
    timing_i = _clamp(timing)

    # ------------------------------------------------------------------
    # Confidence: kararın istatistiksel güveni. Wash/crowded riski güveni düşürür.
    # ------------------------------------------------------------------
    confidence = (
        0.30 * _interval_score(tx, 35, 500, 8, 1800)
        + 0.30 * _interval_score(liq, 8_000, 220_000, 1_000, 900_000)
        + 0.20 * _interval_score(sells, 4, 180, 0, 600)
        + 0.20 * _interval_score(age_min, 12, 360, 2, 1_440)
    )
    confidence -= 0.35 * wash_risk
    confidence -= 0.20 * crowd_risk
    confidence_i = _clamp(confidence)

    # ------------------------------------------------------------------
    # Risk / Edge / Radar.
    # ------------------------------------------------------------------
    risk = (
        0.42 * (100 - survival_i)
        + 0.22 * (100 - exit_i)
        + 0.18 * wash_risk
        + 0.13 * crowd_risk
        + 0.05 * max(0, 45 - confidence_i)
    )
    risk_i = _clamp(risk)

    radar = 0.35 * survival_i + 0.30 * expansion_i + 0.20 * exit_i + 0.15 * timing_i
    radar_i = _clamp(radar)

    # Edge = yükselme ihtimali + çıkış + timing - risk; confidence düşükse edge bastırılır.
    edge = 0.36 * expansion_i + 0.25 * exit_i + 0.20 * timing_i + 0.19 * survival_i - 0.34 * risk_i
    if confidence_i < 55:
        edge -= (55 - confidence_i) * 0.35
    edge_i = _clamp(edge)

    opportunity_i = _clamp(0.55 * expansion_i + 0.25 * timing_i + 0.20 * confidence_i)

    # Decision gates. Not every "candidate" is actionable.
    actionable = (
        edge_i >= config.min_alert_edge_score
        and confidence_i >= config.min_alert_confidence_score
        and survival_i >= config.min_alert_survival_score
        and exit_i >= config.min_alert_exit_score
        and risk_i <= config.max_alert_risk_score
        and liq >= config.min_liq_usd
        and tx >= config.min_txns_h1
        and sells >= config.min_sells_h1
        and config.min_buy_ratio <= buy_ratio <= 0.88
    )

    if actionable:
        decision: Decision = "ALINABİLİR"
        mode: SignalMode = "CONFIRMED SIGNAL"
    elif edge_i >= 48 and survival_i >= 42 and exit_i >= 32:
        decision = "İZLE"
        mode = "EARLY WATCH"
    else:
        decision = "UZAK DUR"
        mode = "EARLY WATCH"

    reasons.insert(0, f"Edge {edge_i}/100, Confidence {confidence_i}/100")
    reasons.insert(1, f"Survival {survival_i}/100, Expansion {expansion_i}/100, Exit {exit_i}/100")
    if decision != "ALINABİLİR":
        cautions.insert(0, f"Karar {decision}; risk {_risk_label(risk_i)} veya doğrulama eksik")
    if wash_risk >= 20:
        cautions.insert(1, f"Wash/crowded risk skoru yüksek: {_clamp(wash_risk)}/100")

    if not reasons:
        reasons.append("Ölçülebilir radar verisi oluştu")
    if not cautions:
        cautions.append("Memecoin riski yüksek; manuel onay şart")

    return Opportunity(
        opportunity_score=opportunity_i,
        risk_score=risk_i,
        exit_score=exit_i,
        mode=mode,
        reasons=reasons[:9],
        cautions=cautions[:9],
        survival_score=survival_i,
        expansion_score=expansion_i,
        timing_score=timing_i,
        confidence_score=confidence_i,
        edge_score=edge_i,
        radar_score=radar_i,
        decision=decision,
    )


def is_actionable(op: Opportunity) -> bool:
    """True ise Telegram'a ALINABİLİR RADAR bildirimi gönderilir."""
    return (
        getattr(op, "decision", "") == "ALINABİLİR"
        and getattr(op, "mode", "") == "CONFIRMED SIGNAL"
        and getattr(op, "edge_score", 0) >= config.min_alert_edge_score
        and getattr(op, "confidence_score", 0) >= config.min_alert_confidence_score
        and getattr(op, "survival_score", 0) >= config.min_alert_survival_score
        and op.risk_score <= config.max_alert_risk_score
        and op.exit_score >= config.min_alert_exit_score
    )

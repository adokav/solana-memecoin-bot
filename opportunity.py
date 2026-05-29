"""Probability-first scoring engine for the Solana memecoin radar.

Bu motorun amacı "iyi görünen metrikleri" kör şekilde ödüllendirmek değil;
vahşi memecoin piyasasında üç soruyu birlikte yanıtlamaktır:

1) Survival: Bu coin rug/honeypot/ölü akış olmadan hayatta kalabilir mi?
2) Expansion: Akış büyüyor mu, yoksa sadece anlık gürültü mü?
3) Exit: Girersek çıkmak matematiksel olarak mümkün mü?
4) Timing: Erkenlik/olgunluk penceresi risk-getiri açısından nerede?

Notlar:
- DexScreener tek snapshot verdiği için "growth" metrikleri proxy olarak hesaplanır.
- Watchlist katmanı zaman içindeki delta'ları izleyerek daha gerçek ivme üretir.
- Bu dosya eski Opportunity alanlarını korur; Telegram/watchlist eski kodla kırılmaz.
"""""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from candidate import Candidate
from config import config


SignalMode = Literal["EARLY WATCH", "CONFIRMED SIGNAL"]
Decision = Literal["ALINABİLİR", "İZLE", "UZAK DUR"]


@dataclass
class Opportunity:
    # Backward-compatible public fields.
    opportunity_score: int
    risk_score: int
    exit_score: int
    mode: SignalMode
    reasons: list[str]
    cautions: list[str]

    # V10 probability model fields.
    survival_score: int = 0
    expansion_score: int = 0
    timing_score: int = 0
    confidence_score: int = 0
    edge_score: int = 0
    radar_score: int = 0
    decision: Decision = "İZLE"


def _clamp(value: float, lo: int = 0, hi: int = 100) -> int:
    return int(max(lo, min(hi, round(value))))


def _bell(x: float, ideal_low: float, ideal_high: float, min_x: float, max_x: float) -> float:
    """0..100 score for being inside an ideal interval, with linear decay outside."""
    if ideal_low <= x <= ideal_high:
        return 100.0
    if x < ideal_low:
        if x <= min_x:
            return 0.0
        return 100.0 * (x - min_x) / max(ideal_low - min_x, 1e-9)
    if x >= max_x:
        return 0.0
    return 100.0 * (max_x - x) / max(max_x - ideal_high, 1e-9)


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
    sells = int(c.sells_h1 or 0)
    buys = int(c.buys_h1 or 0)
    buy_ratio = buys / max(tx, 1)           # 0..1
    buy_ratio_pct = buy_ratio * 100.0
    vol_liq = float(c.volume_h1 or 0) / max(liq, 1.0)
    h1 = float(c.price_change_h1 or 0)
    h6 = float(c.price_change_h6 or 0)
    age_min = float(c.pair_age_h or 0) * 60.0
    safety_text = (safety_reason or "").lower()

    reasons: list[str] = []
    cautions: list[str] = []

    # ------------------------------------------------------------------
    # SURVIVAL: Rug/ölüm ihtimalini azaltan sinyaller.
    # ------------------------------------------------------------------
    survival = 45.0

    # Likidite: çok düşük exit'i öldürür; çok yüksek erken çarpanı azaltır ama survival'a iyi gelir.
    if liq < 1_500:
        survival -= 35
        cautions.append(f"Likidite çok düşük: ${liq:,.0f}")
    elif liq < 5_000:
        survival += 2
        cautions.append(f"Likidite erken/düşük: ${liq:,.0f}")
    elif liq <= 150_000:
        survival += 22
        reasons.append(f"Likidite survival için yeterli: ${liq:,.0f}")
    elif liq <= 500_000:
        survival += 18
        reasons.append(f"Likidite güçlü: ${liq:,.0f}")
    else:
        survival += 8
        cautions.append(f"Likidite yüksek; erken çarpan azalabilir: ${liq:,.0f}")

    # Sell activity: satış yoksa honeypot veya tek taraflı manipülasyon riski.
    if sells <= 0 and tx >= 8:
        survival -= 35
        cautions.append("Sell akışı görünmüyor; honeypot/exit riski")
    elif sells < 3:
        survival -= 8
        cautions.append(f"Sell örneklemi düşük: {sells}/h")
    else:
        survival += 12
        reasons.append(f"Sell akışı mevcut: {sells}/h")

    # Buy/sell dengesi: %55-75 organik erken akış için daha sağlıklı.
    if 0.54 <= buy_ratio <= 0.76:
        survival += 14
        reasons.append(f"Buy pressure dengeli: %{buy_ratio_pct:.0f}")
    elif 0.76 < buy_ratio <= 0.88:
        survival += 2
        cautions.append(f"Buy ratio yüksek; kalabalık trade riski: %{buy_ratio_pct:.0f}")
    elif buy_ratio > 0.88:
        survival -= 22
        cautions.append(f"Aşırı tek taraflı buy flow: %{buy_ratio_pct:.0f}")
    elif buy_ratio < 0.42:
        survival -= 18
        cautions.append(f"Buy pressure zayıf: %{buy_ratio_pct:.0f}")
    else:
        survival -= 2

    # Safety reason: RugCheck/Jupiter authority/exit bilgisi.
    if "honeypot" in safety_text or "no token->sol" in safety_text or "no route" in safety_text:
        survival -= 40
        cautions.append(str(safety_reason))
    elif "mint authority" in safety_text or "freeze authority" in safety_text:
        survival -= 35
        cautions.append(str(safety_reason))
    elif "unknown" in safety_text or "unreachable" in safety_text:
        survival -= 10
        cautions.append(str(safety_reason))
    elif safety_reason and safety_reason != "ok":
        survival += 8
        reasons.append(str(safety_reason))

    # ------------------------------------------------------------------
    # EXPANSION: Büyüme/ilgi potansiyeli. Seviye değil, erken sıcaklık proxy'leri.
    # ------------------------------------------------------------------
    expansion = 35.0

    # Tx örneklemi: 30-250/h ideal; çok yüksek ise crowded/wash riskiyle kısılır.
    if tx < 10:
        expansion -= 22
        cautions.append(f"Tx örneklemi zayıf: {tx}/h")
    elif tx < 30:
        expansion += 8
        reasons.append(f"Erken aktivite var: {tx}/h")
    elif tx <= 250:
        expansion += 24
        reasons.append(f"Organik işlem yoğunluğu: {tx}/h")
    else:
        expansion += 16
        cautions.append(f"Çok yoğun akış; kalabalık trade olabilir: {tx}/h")

    # Volume/Liquidity: 1.2-6x hot ama hâlâ makul. >12x wash/rotation riski.
    if vol_liq < 0.15:
        expansion -= 18
        cautions.append(f"Hacim/Likidite zayıf: {vol_liq:.2f}x")
    elif vol_liq < 1.2:
        expansion += 8
        reasons.append(f"Hacim/Likidite oluşuyor: {vol_liq:.2f}x")
    elif vol_liq <= 6:
        expansion += 24
        reasons.append(f"Hacim/Likidite canlı: {vol_liq:.2f}x")
    elif vol_liq <= 12:
        expansion += 12
        cautions.append(f"Hacim çok sıcak; wash riski izlenmeli: {vol_liq:.1f}x")
    else:
        expansion -= 8
        cautions.append(f"Aşırı hacim/likidite: {vol_liq:.1f}x")

    # Momentum: h1 pozitifliği iyi, parabolik aşırılık riskli.
    if -5 <= h1 < 10:
        expansion += 5
        reasons.append(f"H1 toparlanma/sakin bölge: %{h1:+.1f}")
    elif 10 <= h1 <= 140:
        expansion += 20
        reasons.append(f"H1 momentum sağlıklı: %{h1:+.1f}")
    elif 140 < h1 <= 300:
        expansion += 8
        cautions.append(f"H1 hızlı koşmuş: %{h1:+.1f}")
    elif h1 > 300:
        expansion -= 15
        cautions.append(f"H1 parabolik/aşırı: %{h1:+.1f}")
    else:
        expansion -= 12
        cautions.append(f"H1 zayıf: %{h1:+.1f}")

    if h6 > 800:
        expansion -= 10
        cautions.append(f"H6 aşırı şişmiş: %{h6:+.1f}")
    elif 0 <= h6 <= 400:
        expansion += 4

    # ------------------------------------------------------------------
    # EXIT: Girildikten sonra çıkılabilirlik.
    # ------------------------------------------------------------------
    exit_score = 35.0

    if liq < 3_000:
        exit_score -= 25
    elif liq < 8_000:
        exit_score += 4
    elif liq <= 250_000:
        exit_score += 24
    else:
        exit_score += 18

    # Sell ve tx örneklemi exit'in gerçek zamanlı kanıtı.
    if sells >= 3:
        exit_score += 16
    elif sells >= 1:
        exit_score += 5
    else:
        exit_score -= 25

    if tx >= 30:
        exit_score += 8
    if buy_ratio > 0.9:
        exit_score -= 18
    if vol_liq > 12:
        exit_score -= 12

    if "jupiter exit ok" in safety_text or "exit ok" in safety_text:
        exit_score += 16
        reasons.append("Jupiter çıkış testi geçti")
    elif "unknown" in safety_text or "unreachable" in safety_text:
        exit_score -= 6
    elif "honeypot" in safety_text:
        exit_score -= 45

    # ------------------------------------------------------------------
    # TIMING: Çok erken/verisiz değil, çok geç/dağıtım değil.
    # ------------------------------------------------------------------
    # Ideal: 12 dk - 4 saat. Early-watch için 3-12 dk kabul ama confidence düşük.
    timing = _bell(age_min, ideal_low=12, ideal_high=240, min_x=2, max_x=4_320)
    if 3 <= age_min < 12:
        reasons.append(f"Çok erken radar penceresi: {age_min:.0f} dk")
        cautions.append("Erken faz; metrikler hızlı bozulabilir")
    elif 12 <= age_min <= 240:
        reasons.append(f"İdeal erkenlik penceresi: {age_min:.0f} dk")
    elif age_min > 240:
        cautions.append(f"Erkenlik avantajı azalıyor: {age_min/60:.1f} saat")

    # ------------------------------------------------------------------
    # CONFIDENCE: Verinin karar almak için yeterliliği. Düşük confidence'da edge sinyali bastırılır.
    # ------------------------------------------------------------------
    confidence = 20.0
    confidence += _bell(tx, 35, 220, 5, 800) * 0.35
    confidence += _bell(liq, 8_000, 180_000, 1_000, 750_000) * 0.30
    confidence += _bell(age_min, 15, 360, 2, 4_320) * 0.20
    confidence += _bell(sells, 3, 90, 0, 250) * 0.15

    survival_i = _clamp(survival)
    expansion_i = _clamp(expansion)
    exit_i = _clamp(exit_score)
    timing_i = _clamp(timing)
    confidence_i = _clamp(confidence)

    # Weighted radar score: hayatta kalma ve çıkış skoru "alpha"dan önce gelir.
    radar = (
        0.35 * survival_i
        + 0.30 * expansion_i
        + 0.20 * exit_i
        + 0.15 * timing_i
    )

    # Edge: expansion + exit + timing - risk. Confidence düşükse edge bastırılır.
    risk_i = _clamp(100 - survival_i + max(0, 50 - exit_i) * 0.35 + max(0, 45 - confidence_i) * 0.25)
    raw_edge = 0.38 * expansion_i + 0.27 * exit_i + 0.20 * timing_i + 0.15 * survival_i - 0.28 * risk_i
    edge = _clamp(raw_edge + 20)
    if confidence_i < 45:
        edge = _clamp(edge - (45 - confidence_i) * 0.45)

    radar_i = _clamp(radar)

    # Backward-compatible opportunity: now maps to expansion/timing blend.
    opportunity_i = _clamp(0.55 * expansion_i + 0.25 * timing_i + 0.20 * confidence_i)

    # Decision gates. Survival/exit floors prevent pretty pump metrics from becoming "buyable".
    actionable = (
        edge >= config.min_alert_edge_score
        and confidence_i >= config.min_alert_confidence_score
        and survival_i >= config.min_alert_survival_score
        and exit_i >= config.min_alert_exit_score
        and risk_i <= config.max_alert_risk_score
        and liq >= config.min_liq_usd
        and tx >= config.min_txns_h1
        and sells >= config.min_sells_h1
        and 0.45 <= buy_ratio <= 0.88
    )

    if actionable:
        decision: Decision = "ALINABİLİR"
        mode: SignalMode = "CONFIRMED SIGNAL"
    elif edge >= 55 and survival_i >= 45 and exit_i >= 35:
        decision = "İZLE"
        mode = "EARLY WATCH"
    else:
        decision = "UZAK DUR"
        mode = "EARLY WATCH"

    # High-level rationale.
    reasons.insert(0, f"Edge {edge}/100, Confidence {confidence_i}/100")
    reasons.insert(1, f"Survival {survival_i}/100, Expansion {expansion_i}/100, Exit {exit_i}/100")
    if decision != "ALINABİLİR":
        cautions.insert(0, f"Karar {decision}; risk {_risk_label(risk_i)} veya doğrulama eksik")

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
        edge_score=edge,
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

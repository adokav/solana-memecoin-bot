
from dataclasses import dataclass

@dataclass
class RadarResult:
    mode: str
    opportunity: int
    safety: int
    exit_score: int
    reasons: list
    risks: list

def classify_candidate(c):
    liq = getattr(c, "liq_usd", 0)
    tx = getattr(c, "txns_h1", 0)
    buy_ratio = getattr(c, "buy_ratio", 0)
    vol_ratio = getattr(c, "volume_liq_ratio", 0)
    h1 = getattr(c, "h1", 0)
    h6 = getattr(c, "h6", 0)

    opportunity = 50
    safety = 50
    exit_score = 50
    reasons = []
    risks = []

    if liq > 5000:
        opportunity += 10
        safety += 8
        reasons.append(f"Likidite güçlü: ${liq:,.0f}")

    if tx > 30:
        opportunity += 10
        reasons.append(f"İşlem yoğunluğu yüksek: {tx}/h")

    if 52 <= buy_ratio <= 78:
        opportunity += 10
        safety += 5
        reasons.append(f"Buy pressure sağlıklı: %{buy_ratio}")

    if vol_ratio > 1.2:
        opportunity += 8
        reasons.append(f"Hacim/Likidite aktif: {vol_ratio:.2f}x")

    if 10 <= h1 <= 180:
        opportunity += 8
        reasons.append(f"h1 momentum uygun: %{h1}")

    if h6 > 400:
        safety -= 15
        risks.append("h6 aşırı şişmiş")

    if buy_ratio > 90:
        safety -= 20
        risks.append("Buy ratio aşırı yüksek")

    if liq < 3000:
        risks.append("Likidite düşük")

    mode = "CONFIRMED SIGNAL"
    if liq < 5000 or tx < 30:
        mode = "EARLY WATCH"

    return RadarResult(
        mode=mode,
        opportunity=min(max(opportunity,0),100),
        safety=min(max(safety,0),100),
        exit_score=min(max(exit_score,0),100),
        reasons=reasons,
        risks=risks
    )

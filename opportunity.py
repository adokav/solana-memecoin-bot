
from dataclasses import dataclass

@dataclass
class Opportunity:
    score:int
    risk:int
    reasons:list[str]

def evaluate(c):
    score=50
    risk=50
    reasons=[]
    vol_ratio = (getattr(c,'volume_h1_usd',0) / max(getattr(c,'liquidity_usd',1),1))
    if c.liquidity_usd > 5000:
        score += 10; reasons.append("Likidite yeterli")
    if vol_ratio > 1.2:
        score += 15; reasons.append("Hacim/liquidity güçlü")
    if 52 <= c.buy_ratio <= 75:
        score += 10; reasons.append("Buy ratio sağlıklı")
    if c.txns_h1 >= 30:
        score += 10; reasons.append("Tx yoğunluğu canlı")
    if 10 <= c.price_h1 <= 180:
        score += 5; reasons.append("Momentum erken faz")
    risk=max(5,100-score)
    return Opportunity(score=min(score,99), risk=risk, reasons=reasons)

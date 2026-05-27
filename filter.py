
from config import config

def passes(c):
    if c.quote_symbol.upper() not in {"SOL","WSOL","USDC"}:
        return False,"bad quote"

    if c.liquidity_usd < max(config.min_liq_usd,5000):
        return False,"low liquidity"

    if c.txns_h1 < max(config.min_txns_h1,30):
        return False,"low tx"

    if not (52 <= c.buy_ratio <= 85):
        return False,"bad buy ratio"

    if getattr(c,'price_h1',0) > 250:
        return False,"overextended"

    sells=getattr(c,'sells_h1',1)
    if sells == 0:
        return False,"zero sells"

    return True,"ok"

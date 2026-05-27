
import asyncio
from dexscreener import DexScreener
from pumpfun import PumpFun
from screener import Screener
from filter import passes
from telegram_hub import TelegramHub
from opportunity import evaluate

class Bot:
    def __init__(self):
        self.ds=DexScreener()
        self.pf=PumpFun()
        self.screener=Screener(self.ds,self.pf)
        self.tg=TelegramHub()

    async def scan_loop(self):
        while True:
            coins=await self.screener.scan()
            for c in coins:
                ok,_=passes(c)
                if not ok:
                    continue
                opp=evaluate(c)
                await self.tg.send_opportunity(c,opp)
            await asyncio.sleep(30)

async def main():
    bot=Bot()
    await bot.scan_loop()

if __name__=="__main__":
    asyncio.run(main())

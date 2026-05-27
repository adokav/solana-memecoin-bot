
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application

from config import config

class TelegramHub:
    def __init__(self):
        self.app=Application.builder().token(config.telegram_token).build()

    async def send_opportunity(self,c,opp):
        text=f"""🟢 RADAR: ${c.base_symbol}

Fırsat: {opp.score}/100
Risk: {opp.risk}/100

Nedenler:
""" + "\n".join([f"✅ {r}" for r in opp.reasons])

        kb=InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 AL 0.01 SOL", callback_data=f"buy:{c.base_mint}")],
            [InlineKeyboardButton("DexScreener", url=c.url)]
        ])

        await self.app.bot.send_message(
            chat_id=config.telegram_chat_id,
            text=text,
            reply_markup=kb
        )

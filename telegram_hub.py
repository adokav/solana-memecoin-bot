"""Telegram interface for manual memecoin alerts and watch warnings."""
from __future__ import annotations

import html
import logging
from typing import Awaitable, Callable

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from candidate import Candidate
from config import config
from opportunity import Opportunity
from storage import AlertEvent, Store
from watchlist import WatchWarning

log = logging.getLogger(__name__)

CloseHandler = Callable[[str], Awaitable[tuple[bool, str]]]


BOT_COMMANDS = [
    ("start", "Bot durumu ve komutlar"),
    ("status", "Aktif izleme listesi"),
    ("scan_stats", "Son tarama istatistikleri"),
    ("ignore", "Token izlemeyi bırak: /ignore <mint>"),
]

PERSISTENT_KEYBOARD = ReplyKeyboardMarkup(
    [["/status", "/scan_stats"]],
    resize_keyboard=True,
    is_persistent=True,
)


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


class TelegramHub:
    def __init__(self, store: Store, close_handler: CloseHandler | None = None) -> None:
        self.store = store
        self.close_handler = close_handler
        self.app: Application = Application.builder().token(config.telegram_token).build()
        self.app.add_handler(CommandHandler("start", self._start))
        self.app.add_handler(CommandHandler("status", self._status))
        self.app.add_handler(CommandHandler("scan_stats", self._scan_stats))
        self.app.add_handler(CommandHandler("ignore", self._ignore))
        self.app.add_handler(CallbackQueryHandler(self._callback))
        self._chat_id = config.telegram_chat_id

        self.status_cb: Callable[[], Awaitable[str]] | None = None
        self.scan_stats_cb: Callable[[], Awaitable[str]] | None = None
        self.ignore_cb: Callable[[str], Awaitable[str]] | None = None

    async def _start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "🤖 Alert-only memecoin bot aktif.\n\n"
            "Bot otomatik alım yapmaz. Adayları filtreler, Telegram'a gönderir, "
            "formasyon bozulursa uyarır. Cüzdan tanımlıysa kapatma butonu satış emri gönderebilir.",
            reply_markup=PERSISTENT_KEYBOARD,
        )

    async def _reply(self, update: Update, text: str) -> None:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    async def _status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = await self.status_cb() if self.status_cb else self.store.status_text()
        await self._reply(update, text)

    async def _scan_stats(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = await self.scan_stats_cb() if self.scan_stats_cb else "Henüz tarama yok."
        await self._reply(update, text)

    async def _ignore(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        token = " ".join(ctx.args).strip()
        if not token:
            await self._reply(update, "Kullanım: <code>/ignore &lt;token_mint&gt;</code>")
            return
        text = await self.ignore_cb(token) if self.ignore_cb else "Ignore callback hazır değil."
        await self._reply(update, text)

    async def _callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        data = query.data or ""
        action, _, token = data.partition(":")
        if action == "ignore":
            text = await self.ignore_cb(token) if self.ignore_cb else "Ignore callback hazır değil."
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(text, parse_mode=ParseMode.HTML)
            return

        if action == "close":
            if not self.close_handler:
                await query.message.reply_text(
                    "⚠️ Hızlı kapatma pasif: WALLET_PRIVATE_KEY tanımlı değil veya bot satış yetkisine sahip değil.",
                    parse_mode=ParseMode.HTML,
                )
                return
            ok, msg = await self.close_handler(token)
            prefix = "✅" if ok else "❌"
            await query.message.reply_text(f"{prefix} {msg}", parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            return

    async def send_opportunity(self, c: Candidate, op: Opportunity) -> None:
        reasons = "\n".join(f"• {_esc(x)}" for x in op.reasons)
        cautions = "\n".join(f"• {_esc(x)}" for x in op.cautions)
        text = (
            f"🟢 <b>ALIM ADAYI: ${_esc(c.base_symbol)}</b>\n\n"
            f"Fırsat: <code>{op.opportunity_score}/100</code>\n"
            f"Risk: <code>{op.risk_score}/100</code>\n\n"
            f"<b>Neden geçti</b>\n{reasons}\n\n"
            f"<b>Dikkat</b>\n{cautions}\n\n"
            f"Likidite: <code>${c.liquidity_usd:,.0f}</code>\n"
            f"Hacim/Liq h1: <code>{(c.volume_h1 / max(c.liquidity_usd, 1)):.2f}</code>\n"
            f"Tx h1: <code>{c.txns_h1}</code> | Buy: <code>{(c.buys_h1 / max(c.txns_h1, 1)):.0%}</code>\n"
            f"H1: <code>{c.price_change_h1:+.1f}%</code> | H6: <code>{c.price_change_h6:+.1f}%</code>\n"
            f"Mint: <code>{_esc(c.base_token)}</code>"
        )
        buttons = [
            [InlineKeyboardButton("DexScreener", url=c.url or f"https://dexscreener.com/solana/{c.pair_address}")],
            [
                InlineKeyboardButton("Solscan", url=f"https://solscan.io/token/{c.base_token}"),
                InlineKeyboardButton("🚫 Yoksay", callback_data=f"ignore:{c.base_token}"),
            ],
        ]
        await self.app.bot.send_message(
            chat_id=self._chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        self.store.add_alert(AlertEvent(
            ts=__import__("time").time(),
            symbol=c.base_symbol,
            base_token=c.base_token,
            pair_address=c.pair_address,
            opportunity_score=op.opportunity_score,
            risk_score=op.risk_score,
        ))

    async def send_watch_warning(self, w: WatchWarning) -> None:
        reasons = "\n".join(f"• {_esc(x)}" for x in w.reasons)
        text = (
            f"⚠️ <b>${_esc(w.symbol)} FORMASYON BOZULUYOR</b>\n\n"
            f"{reasons}\n\n"
            f"Fiyat: <code>${w.price_usd:.8f}</code>\n"
            f"Peak DD: <code>-{w.drawdown_pct:.1f}%</code>\n"
            f"Likidite düşüşü: <code>-{w.liquidity_drop_pct:.1f}%</code>\n\n"
            f"Mint: <code>{_esc(w.token_mint)}</code>"
        )
        buttons = [
            [InlineKeyboardButton("🚨 Pozisyonu Kapat", callback_data=f"close:{w.token_mint}")],
            [
                InlineKeyboardButton("DexScreener", url=w.url or f"https://dexscreener.com/solana/{w.pair_address}"),
                InlineKeyboardButton("🚫 Yoksay", callback_data=f"ignore:{w.token_mint}"),
            ],
        ]
        await self.app.bot.send_message(
            chat_id=self._chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def info(self, text: str, with_keyboard: bool = False) -> None:
        kwargs = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": ParseMode.HTML,
            "disable_web_page_preview": True,
        }
        if with_keyboard:
            kwargs["reply_markup"] = PERSISTENT_KEYBOARD
        await self.app.bot.send_message(**kwargs)

    async def run(self) -> None:
        await self.app.initialize()
        await self.app.start()
        try:
            await self.app.bot.set_my_commands([BotCommand(c, d) for c, d in BOT_COMMANDS])
        except Exception:
            log.exception("set_my_commands failed")
        await self.app.updater.start_polling(drop_pending_updates=True)
        log.info("telegram started")

    async def stop(self) -> None:
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()

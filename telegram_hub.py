"""Telegram hub — alert + 8 essential komut.

Komutlar:
  /start    — karşılama
  /status   — açık pozisyonlar + canlı PnL
  /pnl      — kapanan pozisyon özeti (kısa)
  /stats    — matematik EV ölçümü (asıl başarı metriği)
  /scan_stats — son tarama diagnostic (filter cut'ları)
  /halt     — yeni alımları durdur
  /resume   — alımları aç
  /close <symbol> — pozisyonu manuel kapat
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from telegram import (
    BotCommand,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from config import config

log = logging.getLogger(__name__)


BOT_COMMANDS = [
    ("start", "Karşılama"),
    ("status", "Açık pozisyonlar + canlı PnL"),
    ("pnl", "Kapanan pozisyon özeti"),
    ("stats", "Matematik EV ölçümü (asıl başarı metriği)"),
    ("scan_stats", "Son tarama diagnostic (cut'lar nerede)"),
    ("halt", "Yeni alımları durdur"),
    ("resume", "Alımları aç"),
    ("close", "Pozisyonu manuel kapat: /close <symbol>"),
]


PERSISTENT_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["/status", "/pnl", "/stats"],
        ["/scan_stats"],
        ["/halt", "/resume"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


class TelegramHub:
    def __init__(self) -> None:
        self.app: Application = Application.builder().token(config.telegram_token).build()
        self.app.add_handler(CommandHandler("start", self._start))
        self.app.add_handler(CommandHandler("status", self._status))
        self.app.add_handler(CommandHandler("pnl", self._pnl))
        self.app.add_handler(CommandHandler("stats", self._stats))
        self.app.add_handler(CommandHandler("scan_stats", self._scan_stats))
        self.app.add_handler(CommandHandler("halt", self._halt))
        self.app.add_handler(CommandHandler("resume", self._resume))
        self.app.add_handler(CommandHandler("close", self._close))
        self._chat_id = config.telegram_chat_id

        # Callbacks (main.py wire eder)
        self.status_cb: Callable[[], Awaitable[str]] | None = None
        self.pnl_cb: Callable[[], Awaitable[str]] | None = None
        self.stats_cb: Callable[[], Awaitable[str]] | None = None
        self.scan_stats_cb: Callable[[], Awaitable[str]] | None = None
        self.halt_cb: Callable[[str], Awaitable[str]] | None = None
        self.resume_cb: Callable[[], Awaitable[str]] | None = None
        self.close_cb: Callable[[str], Awaitable[str]] | None = None

    # ---------- Command handlers ----------

    async def _start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "🤖 Memecoin Sniper Bot aktif.\n\n"
            "Strateji: detect → safety → auto-buy → TP1 anapara kurtar → "
            "pyramid winner → trailing exit\n\n"
            "Komutlar alttaki butonlardan veya menu ikonundan.",
            reply_markup=PERSISTENT_KEYBOARD,
        )

    async def _reply(self, update: Update, text: str) -> None:
        await update.message.reply_text(
            text, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )

    async def _status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = await self.status_cb() if self.status_cb else "Hazır değil."
        await self._reply(update, text)

    async def _pnl(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = await self.pnl_cb() if self.pnl_cb else "Hazır değil."
        await self._reply(update, text)

    async def _stats(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = await self.stats_cb() if self.stats_cb else "Hazır değil."
        await self._reply(update, text)

    async def _scan_stats(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = await self.scan_stats_cb() if self.scan_stats_cb else "Hazır değil."
        await self._reply(update, text)

    async def _halt(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        reason = " ".join(ctx.args) if ctx.args else "manual"
        text = await self.halt_cb(reason) if self.halt_cb else "Hazır değil."
        await self._reply(update, text)

    async def _resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = await self.resume_cb() if self.resume_cb else "Hazır değil."
        await self._reply(update, text)

    async def _close(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        arg = " ".join(ctx.args) if ctx.args else ""
        text = await self.close_cb(arg) if self.close_cb else "Hazır değil."
        await self._reply(update, text)

    # ---------- Public API ----------

    async def info(self, text: str, with_keyboard: bool = False) -> None:
        """Genel mesaj atma."""
        kwargs: dict = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": ParseMode.HTML,
            "disable_web_page_preview": True,
        }
        if with_keyboard:
            kwargs["reply_markup"] = PERSISTENT_KEYBOARD
        await self.app.bot.send_message(**kwargs)

    async def start(self) -> None:
        await self.app.initialize()
        await self.app.start()
        try:
            await self.app.bot.set_my_commands(
                [BotCommand(c, d) for c, d in BOT_COMMANDS]
            )
        except Exception:
            log.exception("set_my_commands failed")
        await self.app.updater.start_polling(drop_pending_updates=True)
        log.info("telegram started")

    async def stop(self) -> None:
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()

"""Telegram bot: inline butonlu onay + zengin mesaj."""
import logging
import time
from typing import Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from config import config
from rugcheck import SafetyReport
from screener import Candidate

log = logging.getLogger(__name__)

# Onay bekleyen fırsatlar: callback_key -> (Candidate, SafetyReport)
_pending: dict[str, tuple[Candidate, SafetyReport]] = {}

BuyCallback = Callable[[Candidate, SafetyReport], Awaitable[None]]
_on_buy: BuyCallback | None = None


def set_buy_callback(cb: BuyCallback) -> None:
    global _on_buy
    _on_buy = cb


def _confidence_emoji(score: float) -> str:
    if score >= config.high_confidence_score:
        return "🟢"
    if score >= config.min_score_to_alert:
        return "🟡"
    return "🔴"


def _format_alert(c: Candidate, s: SafetyReport) -> str:
    total_score = c.score + s.score
    emoji = _confidence_emoji(total_score)
    profile_label = "🌱 ERKEN" if c.profile == "early" else "📈 TREND"

    buy_ratio = (c.buys_h1 / c.txns_h1 * 100) if c.txns_h1 else 0

    bd = c.score_breakdown
    score_lines = "  ".join([
        f"mom <code>{bd.get('momentum', 0):.1f}</code>",
        f"v/l <code>{bd.get('vol_liq', 0):.1f}</code>",
        f"buy <code>{bd.get('buy_pressure', 0):.1f}</code>",
        f"acc <code>{bd.get('acceleration', 0):.1f}</code>",
    ])
    score_lines2 = "  ".join([
        f"soc <code>{bd.get('social', 0):.1f}</code>",
        f"age <code>{bd.get('age_fit', 0):.1f}</code>",
        f"liq <code>{bd.get('liq_quality', 0):.1f}</code>",
        f"safe <code>{s.score:.1f}</code>",
    ])

    safety_line = "✓ mint revoke, freeze revoke"
    if s.lp_locked_pct is not None:
        safety_line += f", LP %{s.lp_locked_pct:.0f} kilitli"
    if s.top10_pct is not None:
        safety_line += f", top10 <code>%{s.top10_pct:.1f}</code>"
    if s.holder_count:
        safety_line += f", <code>{s.holder_count}</code> holder"

    notes_line = ""
    if s.notes:
        notes_line = "⚠️ <i>" + "; ".join(s.notes[:3]) + "</i>\n"

    return (
        f"{emoji} <b>${c.base_symbol}</b> — {profile_label}  "
        f"<b>Skor: {total_score:.0f}/110</b>\n\n"
        f"💰 <code>${c.price_usd:.8f}</code>  "
        f"FDV <code>${c.fdv:,.0f}</code>\n"
        f"💧 Liq <code>${c.liquidity_usd:,.0f}</code>  "
        f"⏱ <code>{c.pair_age_h:.1f}h</code>\n"
        f"📊 24h <code>${c.volume_h24:,.0f}</code>  "
        f"1h <code>${c.volume_h1:,.0f}</code>\n"
        f"📈 m5 <code>{c.price_change_m5:+.1f}%</code>  "
        f"h1 <code>{c.price_change_h1:+.1f}%</code>  "
        f"h6 <code>{c.price_change_h6:+.1f}%</code>\n"
        f"🔁 <code>{c.txns_h1}</code> tx/1h  alıcı oranı <code>%{buy_ratio:.0f}</code>\n\n"
        f"🛡 {safety_line}\n"
        f"{notes_line}"
        f"<i>{score_lines}</i>\n"
        f"<i>{score_lines2}</i>\n\n"
        f"<a href=\"{c.url}\">DexScreener</a> · "
        f"<a href=\"https://solscan.io/token/{c.base_token}\">Solscan</a>\n"
        f"<code>{c.base_token}</code>"
    )


def _keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ AL ({config.buy_amount_sol} SOL)", callback_data=f"buy:{key}"),
        InlineKeyboardButton("❌ Geç", callback_data=f"skip:{key}"),
    ]])


class TelegramHub:
    def __init__(self) -> None:
        self.app: Application = Application.builder().token(config.telegram_token).build()
        self.app.add_handler(CommandHandler("start", self._start))
        self.app.add_handler(CommandHandler("status", self._status_cmd))
        self.app.add_handler(CommandHandler("health", self._health_cmd))
        self.app.add_handler(CommandHandler("perf", self._perf_cmd))
        self.app.add_handler(CommandHandler("pnl", self._pnl_cmd))
        self.app.add_handler(CallbackQueryHandler(self._on_button))
        self._status_cb: Callable[[], Awaitable[str]] | None = None
        self._health_cb: Callable[[], Awaitable[str]] | None = None
        self._perf_cb: Callable[[], Awaitable[str]] | None = None
        self._pnl_cb: Callable[[int], Awaitable[str]] | None = None
        self._chat_id = config.telegram_chat_id

    def set_status_callback(self, cb: Callable[[], Awaitable[str]]) -> None:
        self._status_cb = cb

    def set_health_callback(self, cb: Callable[[], Awaitable[str]]) -> None:
        self._health_cb = cb

    def set_perf_callback(self, cb: Callable[[], Awaitable[str]]) -> None:
        self._perf_cb = cb

    def set_pnl_callback(self, cb: Callable[[int], Awaitable[str]]) -> None:
        self._pnl_cb = cb

    async def _start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "🤖 Memecoin Sniper Bot aktif.\n\n"
            "Komutlar:\n"
            "  /status — açık pozisyonlar\n"
            "  /health — bot canlı mı, son tarama\n"
            "  /perf — sinyal performans özeti\n"
            "  /pnl [gün] — kapanan pozisyon kâr/zarar raporu"
        )

    async def _status_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = await self._status_cb() if self._status_cb else "Hazır değil."
        await update.message.reply_text(text, parse_mode=ParseMode.HTML,
                                        disable_web_page_preview=True)

    async def _health_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = await self._health_cb() if self._health_cb else "Hazır değil."
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _perf_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = await self._perf_cb() if self._perf_cb else "Hazır değil."
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _pnl_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        days = 0
        if ctx.args:
            try:
                days = max(0, int(ctx.args[0]))
            except (ValueError, TypeError):
                days = 0
        text = await self._pnl_cb(days) if self._pnl_cb else "Hazır değil."
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _on_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer()
        action, _, key = q.data.partition(":")
        item = _pending.pop(key, None)
        if not item:
            try:
                await q.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await q.message.reply_text("⌛ Bu fırsatın süresi geçti.")
            return

        candidate, safety = item

        if action == "skip":
            await q.edit_message_text(
                q.message.text_html + "\n\n❌ <i>Atlandı.</i>",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return

        if action == "buy":
            await q.edit_message_text(
                q.message.text_html + "\n\n⏳ <i>Alım gönderiliyor...</i>",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            if _on_buy is None:
                await q.message.reply_text("⚠️ Buy callback bağlı değil.")
                return
            try:
                await _on_buy(candidate, safety)
            except Exception as e:
                log.exception("buy callback error")
                await q.message.reply_text(
                    f"❌ Alım hatası: <code>{e}</code>",
                    parse_mode=ParseMode.HTML,
                )

    async def alert(self, c: Candidate, s: SafetyReport) -> None:
        key = f"{c.pair_address[:16]}-{int(time.time())}"
        _pending[key] = (c, s)
        await self.app.bot.send_message(
            chat_id=self._chat_id,
            text=_format_alert(c, s),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=_keyboard(key),
        )

    async def info(self, text: str) -> None:
        await self.app.bot.send_message(
            chat_id=self._chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    async def start(self) -> None:
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        log.info("telegram started")

    async def stop(self) -> None:
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()

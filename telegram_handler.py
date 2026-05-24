"""Telegram bot: inline butonlu onay + zengin mesaj."""
import logging
import time
from typing import Awaitable, Callable

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
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

# Pre-grad pump fırsatları: callback_key -> (mint, symbol, sol_amount)
_pending_pump: dict[str, tuple[str, str, float]] = {}

BuyCallback = Callable[[Candidate, SafetyReport], Awaitable[None]]
PumpBuyCallback = Callable[[str, str, float], Awaitable[None]]
_on_buy: BuyCallback | None = None
_on_pump_buy: PumpBuyCallback | None = None


# Telegram menu button'u için (yazma alanının yanındaki ikon)
BOT_COMMANDS: list[tuple[str, str]] = [
    ("start", "Karşılama"),
    ("status", "Açık pozisyonlar + PnL"),
    ("health", "Bot sağlığı + devre kesici"),
    ("perf", "Sinyal performansı (1h/24h zirve)"),
    ("pnl", "Kapanan pozisyon PnL (örn /pnl 7)"),
    ("paper", "Paper trading raporu"),
    ("macro", "Son makro snapshot"),
    ("analog", "Benzer geçmiş makro ortam performansı"),
    ("halt", "Yeni alımları durdur"),
    ("resume", "Alımları tekrar serbest bırak"),
    ("close", "Pozisyonu manuel kapat (örn /close SHIB)"),
    ("wallets", "Takip edilen smart wallet'ları listele"),
    ("addwallet", "Smart wallet ekle: /addwallet <adres> [label]"),
    ("rmwallet", "Smart wallet çıkar: /rmwallet <adres>"),
    ("candidates", "Otomatik keşfedilen aday wallet'ları göster"),
    ("train", "ML modelini paper + real kapanan trade'lerden eğit"),
    ("mlstatus", "ML model durumu (sample, accuracy)"),
    ("pin", "Parametre + perf snapshot (örn /pin v2_after_smart)"),
    ("bandit", "Thompson sampling sizing arm durumları"),
    ("walletpool", "Multi-wallet pool durumu"),
    ("chart", "PnL chart (örn /chart pnl, /chart daily, /chart score)"),
    ("mev", "MEV / sandwich istatistikleri per-DEX"),
    ("twitter", "Twitter influencer mention'ları (son 6h)"),
    ("tune", "Auto-tuner önerileri (paper data analizi)"),
    ("tgchannels", "Telegram alpha channel mention'ları (son 6h)"),
]

# Klavyenin üzerinde sabit duran komut buton grid'i
# Mantıksal gruplar:
#   1. satır: anlık durum / sağlık
#   2. satır: performans + analog karşılaştırma
#   3. satır: edge bileşenleri (smart wallet + makro)
#   4. satır: acil kontrol
PERSISTENT_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["/status", "/health", "/perf"],
        ["/pnl", "/paper", "/analog"],
        ["/wallets", "/candidates", "/macro"],
        ["/halt", "/resume"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


def set_buy_callback(cb: BuyCallback) -> None:
    global _on_buy
    _on_buy = cb


def set_pump_buy_callback(cb: PumpBuyCallback) -> None:
    global _on_pump_buy
    _on_pump_buy = cb


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
        self.app.add_handler(CommandHandler("paper", self._paper_cmd))
        self.app.add_handler(CommandHandler("macro", self._macro_cmd))
        self.app.add_handler(CommandHandler("halt", self._halt_cmd))
        self.app.add_handler(CommandHandler("resume", self._resume_cmd))
        self.app.add_handler(CommandHandler("close", self._close_cmd))
        self.app.add_handler(CommandHandler("analog", self._analog_cmd))
        self.app.add_handler(CommandHandler("wallets", self._wallets_cmd))
        self.app.add_handler(CommandHandler("addwallet", self._addwallet_cmd))
        self.app.add_handler(CommandHandler("rmwallet", self._rmwallet_cmd))
        self.app.add_handler(CommandHandler("candidates", self._candidates_cmd))
        self.app.add_handler(CommandHandler("train", self._train_cmd))
        self.app.add_handler(CommandHandler("mlstatus", self._mlstatus_cmd))
        self.app.add_handler(CommandHandler("pin", self._pin_cmd))
        self.app.add_handler(CommandHandler("bandit", self._bandit_cmd))
        self.app.add_handler(CommandHandler("walletpool", self._walletpool_cmd))
        self.app.add_handler(CommandHandler("chart", self._chart_cmd))
        self.app.add_handler(CommandHandler("mev", self._mev_cmd))
        self.app.add_handler(CommandHandler("twitter", self._twitter_cmd))
        self.app.add_handler(CommandHandler("tune", self._tune_cmd))
        self.app.add_handler(CommandHandler("tgchannels", self._tgchannels_cmd))
        self.app.add_handler(CallbackQueryHandler(self._on_button))
        self._status_cb: Callable[[], Awaitable[str]] | None = None
        self._health_cb: Callable[[], Awaitable[str]] | None = None
        self._perf_cb: Callable[[], Awaitable[str]] | None = None
        self._pnl_cb: Callable[[int], Awaitable[str]] | None = None
        self._paper_cb: Callable[[int], Awaitable[str]] | None = None
        self._macro_cb: Callable[[], Awaitable[str]] | None = None
        self._halt_cb: Callable[[str], Awaitable[str]] | None = None
        self._resume_cb: Callable[[], Awaitable[str]] | None = None
        self._close_cb: Callable[[str], Awaitable[str]] | None = None
        self._analog_cb: Callable[[], Awaitable[str]] | None = None
        self._wallets_cb: Callable[[], Awaitable[str]] | None = None
        self._addwallet_cb: Callable[[str, str], Awaitable[str]] | None = None
        self._rmwallet_cb: Callable[[str], Awaitable[str]] | None = None
        self._candidates_cb: Callable[[], Awaitable[str]] | None = None
        self._train_cb: Callable[[], Awaitable[str]] | None = None
        self._mlstatus_cb: Callable[[], Awaitable[str]] | None = None
        self._pin_cb: Callable[[str], Awaitable[str]] | None = None
        self._bandit_cb: Callable[[], Awaitable[str]] | None = None
        self._walletpool_cb: Callable[[], Awaitable[str]] | None = None
        self._chart_cb: Callable[[str], Awaitable[tuple[bytes | None, str]]] | None = None
        self._mev_cb: Callable[[], Awaitable[str]] | None = None
        self._twitter_cb: Callable[[], Awaitable[str]] | None = None
        self._tune_cb: Callable[[], Awaitable[str]] | None = None
        self._tgchannels_cb: Callable[[], Awaitable[str]] | None = None
        self._chat_id = config.telegram_chat_id

    def set_status_callback(self, cb: Callable[[], Awaitable[str]]) -> None:
        self._status_cb = cb

    def set_health_callback(self, cb: Callable[[], Awaitable[str]]) -> None:
        self._health_cb = cb

    def set_perf_callback(self, cb: Callable[[], Awaitable[str]]) -> None:
        self._perf_cb = cb

    def set_pnl_callback(self, cb: Callable[[int], Awaitable[str]]) -> None:
        self._pnl_cb = cb

    def set_paper_callback(self, cb: Callable[[int], Awaitable[str]]) -> None:
        self._paper_cb = cb

    def set_macro_callback(self, cb: Callable[[], Awaitable[str]]) -> None:
        self._macro_cb = cb

    def set_halt_callback(self, cb: Callable[[str], Awaitable[str]]) -> None:
        self._halt_cb = cb

    def set_resume_callback(self, cb: Callable[[], Awaitable[str]]) -> None:
        self._resume_cb = cb

    def set_close_callback(self, cb: Callable[[str], Awaitable[str]]) -> None:
        self._close_cb = cb

    def set_analog_callback(self, cb: Callable[[], Awaitable[str]]) -> None:
        self._analog_cb = cb

    def set_wallets_callback(self, cb: Callable[[], Awaitable[str]]) -> None:
        self._wallets_cb = cb

    def set_addwallet_callback(self, cb: Callable[[str, str], Awaitable[str]]) -> None:
        self._addwallet_cb = cb

    def set_rmwallet_callback(self, cb: Callable[[str], Awaitable[str]]) -> None:
        self._rmwallet_cb = cb

    def set_candidates_callback(self, cb: Callable[[], Awaitable[str]]) -> None:
        self._candidates_cb = cb

    def set_train_callback(self, cb: Callable[[], Awaitable[str]]) -> None:
        self._train_cb = cb

    def set_mlstatus_callback(self, cb: Callable[[], Awaitable[str]]) -> None:
        self._mlstatus_cb = cb

    def set_pin_callback(self, cb: Callable[[str], Awaitable[str]]) -> None:
        self._pin_cb = cb

    def set_bandit_callback(self, cb: Callable[[], Awaitable[str]]) -> None:
        self._bandit_cb = cb

    def set_walletpool_callback(self, cb: Callable[[], Awaitable[str]]) -> None:
        self._walletpool_cb = cb

    def set_chart_callback(self, cb) -> None:
        self._chart_cb = cb

    def set_mev_callback(self, cb: Callable[[], Awaitable[str]]) -> None:
        self._mev_cb = cb

    def set_twitter_callback(self, cb: Callable[[], Awaitable[str]]) -> None:
        self._twitter_cb = cb

    def set_tune_callback(self, cb: Callable[[], Awaitable[str]]) -> None:
        self._tune_cb = cb

    def set_tgchannels_callback(self, cb: Callable[[], Awaitable[str]]) -> None:
        self._tgchannels_cb = cb

    async def _start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "🤖 Memecoin Sniper Bot aktif.\n\n"
            "Komutlar:\n"
            "  /status — açık pozisyonlar\n"
            "  /health — bot canlı mı, son tarama\n"
            "  /perf — sinyal performans özeti\n"
            "  /pnl [gün] — kapanan pozisyon kâr/zarar raporu\n"
            "  /paper [gün] — paper trading raporu (sanal pozisyonlar)\n"
            "  /macro — son makro snapshot (SOL, BTC dom, F&amp;G)\n"
            "  /halt [sebep] — yeni alımları durdur\n"
            "  /resume — alımları tekrar serbest bırak\n"
            "  /close &lt;symbol&gt; — açık pozisyonu manuel kapat\n"
            "  /analog — bugüne benzer geçmiş ortamlarda sinyal performansı\n"
            "  /wallets — smart wallet listesi\n"
            "  /addwallet &lt;adres&gt; [label] — smart wallet ekle\n"
            "  /rmwallet &lt;adres&gt; — smart wallet çıkar\n"
            "  /candidates — otomatik keşfedilen aday wallet'lar",
            reply_markup=PERSISTENT_KEYBOARD,
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

    async def _paper_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        days = 0
        if ctx.args:
            try:
                days = max(0, int(ctx.args[0]))
            except (ValueError, TypeError):
                days = 0
        text = await self._paper_cb(days) if self._paper_cb else "Hazır değil."
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _macro_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = await self._macro_cb() if self._macro_cb else "Hazır değil."
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _halt_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        reason = " ".join(ctx.args) if ctx.args else "manual"
        text = await self._halt_cb(reason) if self._halt_cb else "Hazır değil."
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _resume_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = await self._resume_cb() if self._resume_cb else "Hazır değil."
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _close_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        arg = " ".join(ctx.args) if ctx.args else ""
        text = await self._close_cb(arg) if self._close_cb else "Hazır değil."
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _analog_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = await self._analog_cb() if self._analog_cb else "Hazır değil."
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _wallets_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = await self._wallets_cb() if self._wallets_cb else "Hazır değil."
        await update.message.reply_text(
            text, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )

    async def _addwallet_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not ctx.args:
            await update.message.reply_text(
                "Kullanım: <code>/addwallet &lt;adres&gt; [label]</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        addr = ctx.args[0].strip()
        label = " ".join(ctx.args[1:]).strip() if len(ctx.args) > 1 else ""
        text = await self._addwallet_cb(addr, label) if self._addwallet_cb else "Hazır değil."
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _rmwallet_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not ctx.args:
            await update.message.reply_text(
                "Kullanım: <code>/rmwallet &lt;adres&gt;</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        addr = ctx.args[0].strip()
        text = await self._rmwallet_cb(addr) if self._rmwallet_cb else "Hazır değil."
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _candidates_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = await self._candidates_cb() if self._candidates_cb else "Hazır değil."
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _train_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = await self._train_cb() if self._train_cb else "Hazır değil."
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _mlstatus_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = await self._mlstatus_cb() if self._mlstatus_cb else "Hazır değil."
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _pin_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        arg = " ".join(ctx.args) if ctx.args else ""
        text = await self._pin_cb(arg) if self._pin_cb else "Hazır değil."
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _bandit_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = await self._bandit_cb() if self._bandit_cb else "Hazır değil."
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _walletpool_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = await self._walletpool_cb() if self._walletpool_cb else "Hazır değil."
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _chart_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        which = ctx.args[0].lower() if ctx.args else "pnl"
        if self._chart_cb is None:
            await update.message.reply_text("Chart callback bağlı değil.")
            return
        png, caption = await self._chart_cb(which)
        if not png:
            await update.message.reply_text(
                f"⚠️ Chart üretilemedi: {caption or 'veri yok'}",
                parse_mode=ParseMode.HTML,
            )
            return
        await self.app.bot.send_photo(
            chat_id=self._chat_id,
            photo=png,
            caption=caption,
            parse_mode=ParseMode.HTML,
        )

    async def _mev_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = await self._mev_cb() if self._mev_cb else "Hazır değil."
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _twitter_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = await self._twitter_cb() if self._twitter_cb else "Hazır değil."
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _tune_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = await self._tune_cb() if self._tune_cb else "Hazır değil."
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _tgchannels_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = await self._tgchannels_cb() if self._tgchannels_cb else "Hazır değil."
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _on_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer()
        action, _, key = q.data.partition(":")

        # Pre-grad pump butonları
        if action in ("pumpbuy", "pumpskip"):
            pump_item = _pending_pump.pop(key, None)
            if not pump_item:
                try:
                    await q.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    pass
                await q.message.reply_text("⌛ Bu pre-grad fırsatının süresi geçti.")
                return
            mint, symbol, sol_amount = pump_item
            if action == "pumpskip":
                await q.edit_message_text(
                    q.message.text_html + "\n\n❌ <i>Atlandı.</i>",
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                return
            # pumpbuy
            await q.edit_message_text(
                q.message.text_html + "\n\n⏳ <i>PumpPortal alımı gönderiliyor...</i>",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            if _on_pump_buy is None:
                await q.message.reply_text("⚠️ Pump-buy callback bağlı değil.")
                return
            try:
                await _on_pump_buy(mint, symbol, sol_amount)
            except Exception as e:
                log.exception("pump-buy callback error")
                await q.message.reply_text(
                    f"❌ Pump alım hatası: <code>{e}</code>",
                    parse_mode=ParseMode.HTML,
                )
            return

        # Klasik (Raydium) alım butonları
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

    async def pump_alert(
        self, mint: str, symbol: str, text: str, sol_amount: float,
    ) -> None:
        """Pre-grad pump alert — PumpPortal üzerinden satın alma butonuyla."""
        key = f"pump-{mint[:16]}-{int(time.time())}"
        _pending_pump[key] = (mint, symbol, sol_amount)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"🐸 AL via PumpPortal ({sol_amount} SOL)",
                callback_data=f"pumpbuy:{key}",
            ),
            InlineKeyboardButton("❌ Geç", callback_data=f"pumpskip:{key}"),
        ]])
        await self.app.bot.send_message(
            chat_id=self._chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )

    async def info(self, text: str, with_keyboard: bool = False) -> None:
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
        # Telegram menu butonu — yazma alanının yanındaki komut listesi
        try:
            await self.app.bot.set_my_commands(
                [BotCommand(cmd, desc) for cmd, desc in BOT_COMMANDS]
            )
        except Exception:
            log.exception("set_my_commands failed (non-fatal)")
        await self.app.updater.start_polling(drop_pending_updates=True)
        log.info("telegram started")

    async def stop(self) -> None:
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()

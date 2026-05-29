"""Telegram interface for manual memecoin alerts and watch warnings.

Robust v3 notes:
- Supports both scan_stats and scan_status aliases.
- Supports command menu, reply-keyboard text and inline callback buttons.
- Does not rely on exact button label; emoji labels are normalized.
"""
from __future__ import annotations

import html
import logging
from typing import Awaitable, Callable

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters, ApplicationHandlerStop

from candidate import Candidate
from config import config
from opportunity import Opportunity
from storage import AlertEvent, Store
from watchlist import WatchWarning

log = logging.getLogger(__name__)

CloseHandler = Callable[[str], Awaitable[tuple[bool, str]]]
BuyHandler = Callable[[str], Awaitable[tuple[bool, str]]]
RadarHandler = Callable[[str], Awaitable[str]]


BOT_COMMANDS = [
    ("start", "Bot durumu ve komutlar"),
    ("status", "Bot durumu"),
    ("scan_stats", "Son tarama istatistikleri"),
    ("radar", "Manuel token analizi: /radar <mint>"),
    ("analyze", "Manuel token analizi: /analyze <mint>"),
    ("ignore", "Token izlemeyi bırak: /ignore <mint>"),
]

PERSISTENT_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["/status", "/scan_stats"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _norm_button_text(value: str | None) -> str:
    """Normalize command/reply/callback text coming from different Telegram clients."""
    text = (value or "").strip().lower()
    text = text.split("@", 1)[0]          # /status@BotName
    text = text.lstrip("/")
    # Keep letters, digits and underscores; remove emojis/spaces/punctuation.
    compact = "".join(ch for ch in text if ch.isalnum() or ch == "_")
    aliases = {
        "status": "status",
        "scanstats": "scan_stats",
        "scan_stats": "scan_stats",
        "scanstatus": "scan_stats",
        "scan_status": "scan_stats",
        "stats": "scan_stats",
        "start": "start",
    }
    return aliases.get(compact, compact)


def _extract_token_from_text(text: str | None, commands: tuple[str, ...] = ("radar", "analyze")) -> str:
    """Extract a mint from /radar or /analyze messages.

    Telegram users often paste as:
      /radar
      <mint>
    or include angle brackets/backticks. This helper accepts all of them.
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    # Normalize common wrappers and invisible chars.
    raw = raw.replace("\u200b", "").replace("\ufeff", "")
    raw = raw.replace("`", " ").replace("<", " ").replace(">", " ")
    parts = raw.replace("\n", " ").replace("\t", " ").split()
    cleaned: list[str] = []
    command_set = {c.lower().lstrip("/") for c in commands}
    for part in parts:
        p = part.strip().strip(",;:()[]{}")
        if not p:
            continue
        low = p.lower().split("@", 1)[0].lstrip("/")
        if low in command_set:
            continue
        cleaned.append(p)
    # Prefer a Solana-like mint length; otherwise first non-command token.
    for p in cleaned:
        if 32 <= len(p) <= 60 and all(ch.isalnum() for ch in p):
            return p
    return cleaned[0] if cleaned else ""


class TelegramHub:
    def __init__(self, store: Store, close_handler: CloseHandler | None = None, buy_handler: BuyHandler | None = None, radar_handler: RadarHandler | None = None) -> None:
        self.store = store
        self.close_handler = close_handler
        self.buy_handler = buy_handler
        self.radar_handler = radar_handler
        self.app: Application = Application.builder().token(config.telegram_token).build()

        # Universal text router MUST be first. Telegram reply-keyboard buttons can be sent
        # as either plain text or slash commands; this catches both before command handlers.
        self.app.add_handler(MessageHandler(filters.TEXT, self._universal_text_router), group=-10)

        # Commands as fallback.
        self.app.add_handler(CommandHandler("start", self._start), group=0)
        self.app.add_handler(CommandHandler("status", self._status), group=0)
        self.app.add_handler(CommandHandler(["scan_stats", "scan_status", "stats"], self._scan_stats), group=0)
        self.app.add_handler(CommandHandler(["radar", "analyze"], self._radar), group=0)
        self.app.add_handler(CommandHandler("ignore", self._ignore), group=0)

        # Inline buttons.
        self.app.add_handler(CallbackQueryHandler(self._callback), group=1)

        self._chat_id = config.telegram_chat_id
        self.status_cb: Callable[[], Awaitable[str]] | None = None
        self.scan_stats_cb: Callable[[], Awaitable[str]] | None = None
        self.ignore_cb: Callable[[str], Awaitable[str]] | None = None

    async def _universal_text_router(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle reply-keyboard texts and slash commands reliably.

        This fixes Telegram clients where the persistent menu sends /scan_stats as a
        command; normal MessageHandler routes used earlier ignored commands.
        """
        if not update.message or not update.message.text:
            return

        action = _norm_button_text(update.message.text)
        if action == "status":
            await self._status(update, ctx)
            raise ApplicationHandlerStop
        if action == "scan_stats":
            await self._scan_stats(update, ctx)
            raise ApplicationHandlerStop

        if action in {"radar", "analyze"} or update.message.text.strip().lower().startswith(("/radar", "/analyze")):
            await self._radar(update, ctx)
            raise ApplicationHandlerStop

        if action == "ignore" or update.message.text.strip().lower().startswith("/ignore"):
            await self._ignore(update, ctx)
            raise ApplicationHandlerStop

    async def _start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        await update.message.reply_text(
            "🤖 Memecoin radar bot aktif.\n\n"
            "Bot adayları filtreler, radara girme nedenlerini gösterir ve formasyon bozulursa uyarır. "
            "Alım/satım yalnızca Telegram'da çift onayla çalışır.",
            reply_markup=PERSISTENT_KEYBOARD,
        )
        await update.message.reply_text(
            "Hızlı menü:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📊 Scan Stats", callback_data="scan_stats"),
                InlineKeyboardButton("📌 Status", callback_data="status"),
            ]]),
        )

    async def _reply(self, update: Update, text: str) -> None:
        if update.message:
            await update.message.reply_text(
                text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=PERSISTENT_KEYBOARD,
            )
            return
        if update.callback_query and update.callback_query.message:
            await update.callback_query.message.reply_text(
                text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=PERSISTENT_KEYBOARD,
            )

    async def _status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            text = await self.status_cb() if self.status_cb else self.store.status_text()
        except Exception as e:
            log.exception("status failed")
            text = f"⚠️ Status okunamadı: <code>{_esc(e)}</code>"
        await self._reply(update, text)

    async def _scan_stats(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            text = await self.scan_stats_cb() if self.scan_stats_cb else "🔍 <b>Tarama istatistikleri</b>\nHenüz tarama yok."
        except Exception as e:
            log.exception("scan_stats failed")
            text = f"⚠️ Tarama istatistiği okunamadı: <code>{_esc(e)}</code>"
        await self._reply(update, text)

    async def _radar(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        token = " ".join(getattr(ctx, "args", []) or []).strip()
        if not token and update.message:
            token = _extract_token_from_text(update.message.text, ("radar", "analyze"))
        token = _extract_token_from_text(token, ("radar", "analyze")).strip()
        if not token:
            await self._reply(update, "Kullanım: <code>/radar &lt;token_mint&gt;</code> veya <code>/analyze &lt;token_mint&gt;</code>\n\nÖrnek: <code>/radar CcZShPVDmsVfWVToqiM2zKSgfeXJjn38XkG1TuLtkpump</code>")
            return
        if not self.radar_handler:
            await self._reply(update, "Manuel radar callback hazır değil.")
            return
        try:
            text = await self.radar_handler(token)
        except Exception as e:
            log.exception("manual radar failed")
            text = f"⚠️ Manuel radar analizi yapılamadı: <code>{_esc(e)}</code>"
        await self._reply(update, text)

        # Manual radar action panel. Even if the analysis says "İZLE", the user can
        # prepare a Telegram-confirmed buy or close flow without copying the mint again.
        if update.message and token:
            await update.message.reply_text(
                "Aksiyon paneli:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"🚀 AL {config.buy_amount_sol:.3f} SOL", callback_data=f"buy:{token}")],
                    [InlineKeyboardButton("🚨 Pozisyonu Kapat", callback_data=f"close:{token}")],
                    [InlineKeyboardButton("Solscan", url=f"https://solscan.io/token/{token}")],
                ]),
            )

    async def _ignore(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        token = " ".join(getattr(ctx, "args", []) or []).strip()
        if not token and update.message:
            token = _extract_token_from_text(update.message.text, ("ignore",))
        token = _extract_token_from_text(token, ("ignore",)).strip()
        if not token:
            await self._reply(update, "Kullanım: <code>/ignore &lt;token_mint&gt;</code>")
            return
        text = await self.ignore_cb(token) if self.ignore_cb else "Ignore callback hazır değil."
        await self._reply(update, text)

    async def _callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query:
            return
        try:
            await query.answer()
        except Exception:
            log.exception("callback answer failed")

        data = query.data or ""
        action, _, token = data.partition(":")
        normalized_action = _norm_button_text(action)

        if normalized_action == "scan_stats":
            try:
                text = await self.scan_stats_cb() if self.scan_stats_cb else "🔍 <b>Tarama istatistikleri</b>\nHenüz tarama yok."
            except Exception as e:
                log.exception("scan_stats callback failed")
                text = f"⚠️ Tarama istatistiği okunamadı: <code>{_esc(e)}</code>"
            if query.message:
                await query.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=PERSISTENT_KEYBOARD)
            return

        if normalized_action == "status":
            try:
                text = await self.status_cb() if self.status_cb else self.store.status_text()
            except Exception as e:
                log.exception("status callback failed")
                text = f"⚠️ Status okunamadı: <code>{_esc(e)}</code>"
            if query.message:
                await query.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=PERSISTENT_KEYBOARD)
            return

        if action == "ignore":
            text = await self.ignore_cb(token) if self.ignore_cb else "Ignore callback hazır değil."
            if query.message:
                await query.edit_message_reply_markup(reply_markup=None)
                await query.message.reply_text(text, parse_mode=ParseMode.HTML)
            return

        if action == "buy":
            if query.message:
                await query.message.reply_text(
                    (
                        f"⚠️ <code>{_esc(token)}</code> için <b>{config.buy_amount_sol:.4f} SOL</b> alım emri hazırlanacak.\n\n"
                        "Bu işlem gerçek para kullanır. Onaylıyor musun?"
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ ONAYLA VE AL", callback_data=f"confirm_buy:{token}"),
                        InlineKeyboardButton("❌ İPTAL", callback_data=f"cancel:{token}"),
                    ]]),
                )
            return

        if action == "confirm_buy":
            if not query.message:
                return
            if not self.buy_handler:
                await query.message.reply_text("⚠️ Alım pasif: WALLET_PRIVATE_KEY tanımlı değil.", parse_mode=ParseMode.HTML)
                return
            ok, msg = await self.buy_handler(token)
            await query.message.reply_text(("✅ " if ok else "❌ ") + msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            return

        if action == "close":
            if query.message:
                await query.message.reply_text(
                    (
                        f"⚠️ <code>{_esc(token)}</code> için bot cüzdanındaki tüm bakiye satılacak.\n\n"
                        "Onaydan sonra satış ön kontrolü yapılır: token bakiyesi, SOL fee bakiyesi, Jupiter route, beklenen SOL çıkışı ve price impact raporlanır.\n\n"
                        "Onaylıyor musun?"
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ ONAYLA VE SAT", callback_data=f"confirm_close:{token}"),
                        InlineKeyboardButton("❌ İPTAL", callback_data=f"cancel:{token}"),
                    ]]),
                )
            return

        if action == "confirm_close":
            if not query.message:
                return
            if not self.close_handler:
                await query.message.reply_text("⚠️ Hızlı kapatma pasif: WALLET_PRIVATE_KEY tanımlı değil.", parse_mode=ParseMode.HTML)
                return
            ok, msg = await self.close_handler(token)
            await query.message.reply_text(("✅ " if ok else "❌ ") + msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            return

        if action == "cancel" and query.message:
            await query.message.reply_text("İşlem iptal edildi.")
            return

        if query.message:
            await query.message.reply_text(f"⚠️ Bilinmeyen buton: <code>{_esc(data)}</code>", parse_mode=ParseMode.HTML)

    async def send_opportunity(self, c: Candidate, op: Opportunity) -> None:
        reasons = "\n".join(f"✅ {_esc(x)}" for x in op.reasons) or "✅ Radar eşiğini geçti"
        cautions = "\n".join(f"⚠️ {_esc(x)}" for x in op.cautions) or "⚠️ Memecoin riski yüksek; manuel onay şart"
        mode = getattr(op, "mode", "CONFIRMED SIGNAL")
        is_confirmed = mode == "CONFIRMED SIGNAL"

        emoji = "🟢" if is_confirmed else "🟡"
        title = "ALINABİLİR RADAR" if is_confirmed else "ERKEN RADAR / İZLEME"
        amount_line = (
            f"Önerilen alım: <b>{config.buy_amount_sol:.4f} SOL</b>\n"
            if is_confirmed else
            "Öneri: <b>Henüz izleme modu</b>\n"
        )

        text = (
            f"{emoji} <b>{title}: ${_esc(c.base_symbol)}</b>\n\n"
            f"{amount_line}"
            f"Karar: <b>{getattr(op, 'decision', 'İZLE')}</b>\n"
            f"Radar: <code>{getattr(op, 'radar_score', op.opportunity_score)}/100</code> | "
            f"Edge: <code>{getattr(op, 'edge_score', 0)}/100</code> | "
            f"Confidence: <code>{getattr(op, 'confidence_score', 0)}/100</code>\n"
            f"Survival: <code>{getattr(op, 'survival_score', 0)}/100</code> | "
            f"Expansion: <code>{getattr(op, 'expansion_score', op.opportunity_score)}/100</code> | "
            f"Exit: <code>{getattr(op, 'exit_score', 0)}/100</code> | "
            f"Timing: <code>{getattr(op, 'timing_score', 0)}/100</code>\n"
            f"Risk: <code>{op.risk_score}/100</code>\n\n"
            f"<b>Neden radara girdi?</b>\n{reasons}\n\n"
            f"<b>Risk notları</b>\n{cautions}\n\n"
            f"Likidite: <code>${c.liquidity_usd:,.0f}</code>\n"
            f"Hacim/Liq h1: <code>{(c.volume_h1 / max(c.liquidity_usd, 1)):.2f}x</code>\n"
            f"Tx h1: <code>{c.txns_h1}</code> | Buy: <code>{(c.buys_h1 / max(c.txns_h1, 1)):.0%}</code>\n"
            f"H1: <code>{c.price_change_h1:+.1f}%</code> | H6: <code>{c.price_change_h6:+.1f}%</code>\n"
            f"Mint: <code>{_esc(c.base_token)}</code>"
        )

        buttons: list[list[InlineKeyboardButton]] = []
        if is_confirmed:
            buttons.append([InlineKeyboardButton(f"🚀 AL {config.buy_amount_sol:.3f} SOL", callback_data=f"buy:{c.base_token}")])
        buttons.extend([
            [
                InlineKeyboardButton("👁 Takipte", callback_data="status"),
                InlineKeyboardButton("📊 Scan Stats", callback_data="scan_stats"),
            ],
            [InlineKeyboardButton("DexScreener", url=c.url or f"https://dexscreener.com/solana/{c.pair_address}")],
            [
                InlineKeyboardButton("Solscan", url=f"https://solscan.io/token/{c.base_token}"),
                InlineKeyboardButton("🚫 Yoksay", callback_data=f"ignore:{c.base_token}"),
            ],
        ])

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
            exit_score=getattr(op, "exit_score", 0),
            survival_score=getattr(op, "survival_score", 0),
            expansion_score=getattr(op, "expansion_score", 0),
            timing_score=getattr(op, "timing_score", 0),
            confidence_score=getattr(op, "confidence_score", 0),
            edge_score=getattr(op, "edge_score", 0),
            radar_score=getattr(op, "radar_score", 0),
            decision=getattr(op, "decision", "İZLE"),
            mode=getattr(op, "mode", "UNKNOWN"),
        ))

    async def send_watch_warning(self, w: WatchWarning) -> None:
        reasons = "\n".join(f"• {_esc(x)}" for x in w.reasons)
        if getattr(w, "kind", "break") == "strength":
            title = f"📈 <b>${_esc(w.symbol)} FORMASYON GÜÇLENİYOR</b>"
            text = (
                f"{title}\n\n"
                f"{reasons}\n\n"
                f"Fiyat: <code>${w.price_usd:.8f}</code>\n"
                f"Peak DD: <code>-{w.drawdown_pct:.1f}%</code>\n"
                f"Mint: <code>{_esc(w.token_mint)}</code>"
            )
            buttons = [
                [InlineKeyboardButton(f"🚀 AL {config.buy_amount_sol:.3f} SOL", callback_data=f"buy:{w.token_mint}")],
                [
                    InlineKeyboardButton("DexScreener", url=w.url or f"https://dexscreener.com/solana/{w.pair_address}"),
                    InlineKeyboardButton("🚫 Yoksay", callback_data=f"ignore:{w.token_mint}"),
                ],
            ]
        else:
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
        if self.app.updater is None:
            raise RuntimeError("Telegram polling updater is not available. Check python-telegram-bot installation.")
        await self.app.updater.start_polling(drop_pending_updates=True)
        log.info("telegram started")

    async def stop(self) -> None:
        if self.app.updater is not None:
            await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()

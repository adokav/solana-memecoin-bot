"""KADEMELİ ÇIKIŞ izleyicisi.

Mantık:
  - TP1 (+30%): pozisyonun %30'unu sat → kalan %70
  - TP2 (+80%): kalanın %40'ını sat (toplam %58 satıldı, %42 kaldı)
  - TP3 (+200%): kalanın %50'sini sat (toplam %79 satıldı, %21 kaldı)
  - Moon bag (%21): trailing stop ile takip
  - SL (-35%): tüm pozisyon kapanır
  - TP1 sonrası SL breakeven (0%)'a çekilir → kayıp imkansız
"""
import logging
import time

from config import config
from dexscreener import DexScreener
from jupiter import Jupiter, JupiterError, LAMPORTS_PER_SOL
from rugcheck import RugCheckClient
from smart_wallets import SmartWalletStore
from storage import Position, PyramidAdd, Store, TpHit
from telegram_handler import TelegramHub

log = logging.getLogger(__name__)


class Monitor:
    def __init__(
        self,
        ds: DexScreener,
        jup: Jupiter,
        store: Store,
        tg: TelegramHub,
        smart: SmartWalletStore | None = None,
        rug: RugCheckClient | None = None,
    ) -> None:
        self.ds = ds
        self.jup = jup
        self.store = store
        self.tg = tg
        self.smart = smart
        self.rug = rug

    # ---------- Kısmi satış (TP seviyesi) ----------

    async def _partial_sell(
        self,
        pos: Position,
        level: int,
        trigger_pct: float,
        sell_pct: float,
        current_price: float,
    ) -> None:
        # Kalan miktarın sell_pct'i kadar sat
        sell_amount = int(pos.remaining_raw * (sell_pct / 100))
        if sell_amount <= 0:
            log.warning("sell amount zero for %s TP%d", pos.symbol, level)
            return

        try:
            sig, lamports_out = await self.jup.sell(pos.base_token, sell_amount)
        except JupiterError as e:
            await self.tg.info(f"⚠️ TP{level} satışı başarısız ${pos.symbol}: <code>{e}</code>")
            return
        except Exception as e:
            log.exception("TP%d sell failed", level)
            await self.tg.info(f"❌ TP{level} hatası ${pos.symbol}: <code>{e}</code>")
            return

        sol_received = lamports_out / LAMPORTS_PER_SOL
        pos.remaining_raw -= sell_amount
        pos.sol_received_total += sol_received
        pos.tp_hits.append(TpHit(
            level=level,
            trigger_pct=trigger_pct,
            sold_pct=sell_pct,
            sold_amount_raw=sell_amount,
            sol_received=sol_received,
            tx_sig=sig,
            ts=time.time(),
        ))

        # TP1 sonrası breakeven kilidini aç
        if level == 1 and config.breakeven_after_tp1:
            pos.breakeven_armed = True

        self.store.update()

        await self.tg.info(
            f"🎯 <b>TP{level} HIT</b> ${pos.symbol} <code>+{trigger_pct:.0f}%</code>\n"
            f"Satılan: <code>%{sell_pct:.0f}</code> kalanın\n"
            f"Kazanılan: <code>{sol_received:.4f} SOL</code>\n"
            f"Şimdiki fiyat: <code>${current_price:.8f}</code>\n"
            f"Toplam tahsil: <code>{pos.sol_received_total:.4f} SOL</code> "
            f"(giriş <code>{pos.sol_spent:.4f} SOL</code>)\n"
            + ("🔒 SL artık breakeven'da — kayıp riski sıfır.\n" if pos.breakeven_armed and level == 1 else "")
            + f"<a href=\"https://solscan.io/tx/{sig}\">solscan</a>"
        )

    # ---------- Tam kapanış ----------

    async def _close_all(self, pos: Position, current_price: float, reason: str) -> None:
        if pos.remaining_raw <= 0:
            pos.status = "closed"
            pos.closed_at = time.time()
            pos.close_reason = reason
            self.store.update()
            return

        try:
            sig, lamports_out = await self.jup.sell(pos.base_token, pos.remaining_raw)
        except Exception as e:
            log.exception("close-all sell failed")
            await self.tg.info(f"❌ Final satış başarısız ${pos.symbol}: <code>{e}</code>")
            return

        sol_received = lamports_out / LAMPORTS_PER_SOL
        pos.sol_received_total += sol_received
        pos.remaining_raw = 0

        pnl_pct = ((pos.sol_received_total - pos.sol_spent) / pos.sol_spent) * 100
        pos.pnl_pct = pnl_pct
        pos.status = "closed"
        pos.closed_at = time.time()
        pos.close_reason = reason
        self.store.update()

        emoji = "🟢" if pnl_pct >= 0 else "🔴"
        tp_summary = "  ".join(
            [f"TP{h.level}+{h.trigger_pct:.0f}%" for h in pos.tp_hits]
        ) or "—"

        await self.tg.info(
            f"{emoji} <b>${pos.symbol}</b> KAPANDI ({reason})\n"
            f"PnL: <code>{pnl_pct:+.2f}%</code>\n"
            f"Giriş: <code>{pos.sol_spent:.4f} SOL</code> → "
            f"Çıkış toplam: <code>{pos.sol_received_total:.4f} SOL</code>\n"
            f"TP'ler: {tp_summary}\n"
            f"Son fiyat: <code>${current_price:.8f}</code>\n"
            f"<a href=\"https://solscan.io/tx/{sig}\">solscan</a>"
        )

    # ---------- Pyramid / DCA: TP1 sonrası kazanan trende ekle ----------

    async def _try_pyramid(self, pos: Position, price: float) -> bool:
        """True dönerse bu tick için başka aksiyon alma."""
        if not config.pyramid_enabled:
            return False
        # Sadece TP1 sonrası (en az bir miktar kar realize edildi)
        hit_levels = {h.level for h in pos.tp_hits}
        if 1 not in hit_levels:
            return False
        if len(pos.pyramid_adds) >= config.pyramid_max_adds:
            return False

        orig = pos.original_entry_price_usd or pos.entry_price_usd
        if orig <= 0:
            return False
        pnl_from_orig = (price - orig) / orig * 100

        # Sıradaki add tetiği: TP1 trigger + (add_idx+1) × step
        next_idx = len(pos.pyramid_adds)
        trigger_pct = config.tp1_trigger + (next_idx + 1) * config.pyramid_trigger_step_pct
        if pnl_from_orig < trigger_pct:
            return False

        add_sol = config.buy_amount_sol * config.pyramid_size_ratio
        # Exposure cap: yine de toplam riski koru
        current_exposure = sum(p.sol_spent for p in self.store.open_positions())
        if current_exposure + add_sol > config.max_total_exposure_sol:
            log.info("pyramid skip %s: exposure cap", pos.symbol)
            return False

        try:
            sig, tokens_bought = await self.jup.buy(pos.base_token, add_sol)
        except JupiterError as e:
            await self.tg.info(
                f"⚠️ Pyramid ekleme başarısız ${pos.symbol}: <code>{e}</code>"
            )
            return False
        except Exception as e:
            log.exception("pyramid buy failed")
            await self.tg.info(
                f"❌ Pyramid hatası ${pos.symbol}: <code>{e}</code>"
            )
            return False

        # Blended entry: (eski USD bazis + yeni USD bazis) / yeni token toplamı
        old_basis = pos.amount_raw * pos.entry_price_usd
        add_basis = tokens_bought * price
        new_amount = pos.amount_raw + tokens_bought
        new_entry = (
            (old_basis + add_basis) / new_amount if new_amount > 0 else pos.entry_price_usd
        )

        pos.amount_raw = new_amount
        pos.remaining_raw += tokens_bought
        pos.sol_spent += add_sol
        pos.entry_price_usd = new_entry
        pos.peak_price_usd = price  # trailing referansı sıfırlansın
        pos.pyramid_adds.append(PyramidAdd(
            pct_at_add=pnl_from_orig,
            price_usd=price,
            amount_raw=tokens_bought,
            sol_spent=add_sol,
            tx_sig=sig,
            ts=time.time(),
        ))
        self.store.update()

        await self.tg.info(
            f"➕ <b>Pyramid #{len(pos.pyramid_adds)}</b> ${pos.symbol}  "
            f"<code>+{pnl_from_orig:.0f}%</code> (orig)\n"
            f"Eklenen: <code>{add_sol:.4f} SOL</code> @ <code>${price:.8f}</code>\n"
            f"Yeni blended entry: <code>${new_entry:.8f}</code>\n"
            f"Toplam harcanan: <code>{pos.sol_spent:.4f} SOL</code>\n"
            f"<a href=\"https://solscan.io/tx/{sig}\">solscan</a>"
        )
        return True

    # ---------- Hold-time exit/safety check'leri ----------

    async def _check_smart_exit(self, pos: Position, price: float) -> bool:
        if self.smart is None or not config.smart_exit_signals_enabled:
            return False
        exits = self.smart.recent_exits_for(pos.base_token)
        if len(exits) >= 2:
            wallets_str = ", ".join(
                f"{e.wallet[:6]}..{e.wallet[-4:]}" for e in exits[:3]
            )
            await self._close_all(
                pos, price,
                f"smart wallet exodus ({len(exits)}: {wallets_str})",
            )
            return True
        if len(exits) == 1 and pos.trailing_stop_override_pct is None:
            new_trail = max(5.0, config.trailing_stop / 2)
            pos.trailing_stop_override_pct = new_trail
            self.store.update()
            ew = exits[0].wallet
            await self.tg.info(
                f"⚠️ <b>${pos.symbol}</b> — smart wallet çıkış "
                f"<code>{ew[:6]}..{ew[-4:]}</code> "
                f"({exits[0].sol_value:.2f} SOL)\n"
                f"Trailing %{config.trailing_stop:.0f} → %{new_trail:.0f} daraltıldı"
            )
        return False

    async def _check_liquidity_drain(
        self, pos: Position, pair: dict, price: float,
    ) -> bool:
        if not config.hold_safety_check_enabled:
            return False
        if pos.entry_liquidity_usd is None or pos.entry_liquidity_usd <= 0:
            return False
        try:
            current_liq = float((pair.get("liquidity") or {}).get("usd") or 0)
        except (TypeError, ValueError):
            return False
        if current_liq <= 0:
            return False
        drop_pct = (pos.entry_liquidity_usd - current_liq) / pos.entry_liquidity_usd * 100
        if drop_pct >= config.hold_liq_drain_pct:
            await self.tg.info(
                f"🚨 <b>${pos.symbol}</b> — likidite çekildi "
                f"(<code>${pos.entry_liquidity_usd:,.0f}</code> → "
                f"<code>${current_liq:,.0f}</code>, -{drop_pct:.0f}%)"
            )
            await self._close_all(
                pos, price, f"liquidity drain -{drop_pct:.0f}%",
            )
            return True
        return False

    async def _check_holder_spike(self, pos: Position, price: float) -> bool:
        if (
            not config.hold_safety_check_enabled
            or self.rug is None
            or pos.entry_top10_pct is None
        ):
            return False
        now = time.time()
        if now - pos.last_safety_check_ts < config.hold_safety_check_interval:
            return False
        pos.last_safety_check_ts = now
        self.store.update()

        try:
            report = await self.rug.check(pos.base_token)
        except Exception:
            log.exception("hold-time safety check failed for %s", pos.symbol)
            return False
        if report.top10_pct is None:
            return False

        spike_pp = report.top10_pct - pos.entry_top10_pct
        if spike_pp >= config.hold_top10_spike_pp:
            await self.tg.info(
                f"🚨 <b>${pos.symbol}</b> — top10 holder konsantrasyonu sıçradı "
                f"(<code>%{pos.entry_top10_pct:.1f}</code> → "
                f"<code>%{report.top10_pct:.1f}</code>, +{spike_pp:.1f}pp)"
            )
            await self._close_all(
                pos, price, f"top10 holder spike +{spike_pp:.1f}pp",
            )
            return True
        return False

    # ---------- Tek bir pozisyon için tick ----------

    async def _tick_one(self, pos: Position) -> None:
        if pos.remaining_raw <= 0:
            pos.status = "closed"
            self.store.update()
            return

        pair = await self.ds.pair("solana", pos.pair_address)
        if not pair:
            log.warning("no pair data for %s", pos.symbol)
            return

        try:
            price = float(pair.get("priceUsd") or 0)
        except (TypeError, ValueError):
            return
        if price <= 0:
            return

        if price > pos.peak_price_usd:
            pos.peak_price_usd = price
            self.store.update()

        # --- Smart wallet exit signal (en güçlü erken çıkış uyarısı) ---
        if await self._check_smart_exit(pos, price):
            return

        # --- Hold-time KATMAN 2: likidite çekilmiş mi ---
        if await self._check_liquidity_drain(pos, pair, price):
            return

        # --- Hold-time KATMAN 2: top10 holder sıçraması (her N saniyede bir) ---
        if await self._check_holder_spike(pos, price):
            return

        pnl_pct = ((price - pos.entry_price_usd) / pos.entry_price_usd) * 100
        drawdown_from_peak = ((pos.peak_price_usd - price) / pos.peak_price_usd) * 100

        hit_levels = {h.level for h in pos.tp_hits}

        # --- Pyramid (TP1 sonrası, TP3 öncesi) ---
        if 1 in hit_levels and 3 not in hit_levels:
            if await self._try_pyramid(pos, price):
                return

        # --- TP3 ---
        if 3 not in hit_levels and pnl_pct >= config.tp3_trigger:
            await self._partial_sell(pos, 3, config.tp3_trigger, config.tp3_sell, price)
            return

        # --- TP2 ---
        if 2 not in hit_levels and pnl_pct >= config.tp2_trigger:
            await self._partial_sell(pos, 2, config.tp2_trigger, config.tp2_sell, price)
            return

        # --- TP1 ---
        if 1 not in hit_levels and pnl_pct >= config.tp1_trigger:
            await self._partial_sell(pos, 1, config.tp1_trigger, config.tp1_sell, price)
            return

        # --- Trailing stop (TP1 sonrası aktif; smart exit signal'i daraltabilir) ---
        trail_pct = pos.trailing_stop_override_pct or config.trailing_stop
        if pos.tp_hits and drawdown_from_peak >= trail_pct:
            await self._close_all(pos, price, f"trailing -{drawdown_from_peak:.1f}% from peak")
            return

        # --- Breakeven SL (TP1 sonrası) ---
        if pos.breakeven_armed and pnl_pct <= 0:
            await self._close_all(pos, price, "breakeven SL")
            return

        # --- Standart SL ---
        if pnl_pct <= -config.stop_loss:
            await self._close_all(pos, price, f"SL {pnl_pct:.1f}%")
            return

        log.debug(
            "%s pnl %.2f%% peak_dd %.2f%% rem %d",
            pos.symbol, pnl_pct, drawdown_from_peak, pos.remaining_raw,
        )

    async def tick(self) -> None:
        for pos in list(self.store.open_positions()):
            try:
                await self._tick_one(pos)
            except Exception:
                log.exception("monitor tick error for %s", pos.symbol)

    # ---------- Manuel kapanış (/close komutu için) ----------

    async def manual_close(self, symbol_or_addr: str) -> tuple[bool, str]:
        """Symbol veya base_token adresi ile eşleşen ilk açık pozisyonu kapatır."""
        needle = symbol_or_addr.lstrip("$").strip()
        if not needle:
            return False, "kullanım: /close &lt;symbol&gt;"
        needle_up = needle.upper()
        for pos in self.store.open_positions():
            if pos.symbol.upper() == needle_up or pos.base_token == needle:
                pair = await self.ds.pair("solana", pos.pair_address)
                try:
                    price = float((pair or {}).get("priceUsd") or 0)
                except (TypeError, ValueError):
                    price = 0.0
                if price <= 0:
                    price = pos.entry_price_usd
                await self._close_all(pos, price, "manual /close")
                return True, f"${pos.symbol} kapatıldı."
        return False, f"${needle} için açık pozisyon yok."

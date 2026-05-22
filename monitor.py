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
from storage import Position, Store, TpHit
from telegram_handler import TelegramHub

log = logging.getLogger(__name__)


class Monitor:
    def __init__(self, ds: DexScreener, jup: Jupiter, store: Store, tg: TelegramHub) -> None:
        self.ds = ds
        self.jup = jup
        self.store = store
        self.tg = tg

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

        pnl_pct = ((price - pos.entry_price_usd) / pos.entry_price_usd) * 100
        drawdown_from_peak = ((pos.peak_price_usd - price) / pos.peak_price_usd) * 100

        hit_levels = {h.level for h in pos.tp_hits}

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

        # --- Trailing stop (TP1 sonrası aktif) ---
        if pos.tp_hits and drawdown_from_peak >= config.trailing_stop:
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

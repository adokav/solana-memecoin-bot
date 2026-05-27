"""Position monitor — TP1 dinamik + pyramid + trailing + SL + liquidity drain.

Matematik temeli:
  - TP1 dynamic: sell_pct = 1/(1 + trigger/100) × 1.05 → anapara + buffer kasada
  - Post-TP1: SL → breakeven (kayıp matematiksel olarak imkansız)
  - Pyramid (anti-martingale): TP1 sonrası ATH yapan winner'lara ekle
  - TP2/TP3: moon bag küçültme (büyük profit lock)
  - Trailing: peak'ten %25 düşüş → exit
  - Liquidity drain: LP %40 çekildi → rug in progress, çık
"""
from __future__ import annotations

import logging
import time

from config import config
from dexscreener import DexScreener
from jupiter import Jupiter, JupiterError, LAMPORTS_PER_SOL
from storage import Position, PyramidAdd, Store, TpHit
from telegram_hub import TelegramHub

log = logging.getLogger(__name__)


def _compute_tp1_sell_pct(trigger_pct: float) -> float:
    """TP1 anapara kurtarma sell %.

    sell_pct = 1 / (1 + trigger/100) × 1.05  (5% slippage buffer)
    Cap: %95 max (her zaman moon bag bırak).
    """
    if trigger_pct <= 0:
        return 70.0
    raw = 100.0 / (1.0 + trigger_pct / 100.0) * 1.05
    return max(10.0, min(95.0, raw))


class Monitor:
    def __init__(
        self,
        ds: DexScreener,
        jup: Jupiter,
        store: Store,
        tg: TelegramHub,
    ) -> None:
        self.ds = ds
        self.jup = jup
        self.store = store
        self.tg = tg

    # ---------- Partial sell ----------

    async def _partial_sell(
        self,
        pos: Position,
        level: int,
        trigger_pct: float,
        sell_pct: float,
        current_price: float,
    ) -> None:
        sell_amount = int(pos.remaining_raw * (sell_pct / 100))
        if sell_amount <= 0:
            return
        try:
            sig, lamports_out = await self.jup.sell(pos.base_token, sell_amount)
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
        # TP1 sonrası breakeven SL
        if level == 1:
            pos.breakeven_armed = True
        self.store.update()

        await self.tg.info(
            f"🎯 <b>TP{level} HIT</b> ${pos.symbol} <code>+{trigger_pct:.0f}%</code>\n"
            f"Satılan: <code>%{sell_pct:.0f}</code> kalanın\n"
            f"Kazanılan: <code>{sol_received:.4f} SOL</code>\n"
            f"Toplam tahsil: <code>{pos.sol_received_total:.4f} SOL</code> "
            f"(giriş <code>{pos.sol_spent:.4f} SOL</code>)\n"
            + ("🔒 SL → breakeven. Kayıp riski sıfır.\n" if level == 1 else "")
            + f"<a href=\"https://solscan.io/tx/{sig}\">solscan</a>"
        )

    # ---------- Close all ----------

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
        pnl_pct = (
            ((pos.sol_received_total - pos.sol_spent) / pos.sol_spent) * 100
            if pos.sol_spent > 0 else 0
        )
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
            f"Çıkış: <code>{pos.sol_received_total:.4f} SOL</code>\n"
            f"TP'ler: {tp_summary}\n"
            f"<a href=\"https://solscan.io/tx/{sig}\">solscan</a>"
        )

    # ---------- Pyramid (anti-martingale) ----------

    async def _try_pyramid(self, pos: Position, price: float) -> bool:
        """TP1 sonrası, peak yeni ATH yapıyorsa pyramid ekle."""
        if not config.pyramid_enabled:
            return False
        if not pos.tp_hits:  # TP1 hit olmamış
            return False
        if len(pos.pyramid_adds) >= config.pyramid_max_adds:
            return False
        orig = pos.original_entry_price_usd or pos.entry_price_usd
        if orig <= 0:
            return False
        pnl_orig = (price - orig) / orig * 100
        next_idx = len(pos.pyramid_adds)
        # TP1 trigger + (idx+1) × step; örn TP1=50, step=50 → 100, 150
        trigger_pct = config.tp1_trigger + (next_idx + 1) * config.pyramid_trigger_step_pct
        if pnl_orig < trigger_pct:
            return False

        add_sol = config.buy_amount_sol * config.pyramid_size_ratio
        # Exposure cap saygılı
        current_exp = sum(p.sol_spent for p in self.store.open_positions())
        if current_exp + add_sol > config.max_total_exposure_sol:
            return False

        try:
            sig, tokens_bought = await self.jup.buy(pos.base_token, add_sol)
        except JupiterError as e:
            await self.tg.info(f"⚠️ Pyramid başarısız ${pos.symbol}: <code>{e}</code>")
            return False
        except Exception as e:
            log.exception("pyramid buy failed")
            return False

        # Blended entry hesabı
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
        pos.peak_price_usd = price  # trailing referansı sıfırla
        pos.pyramid_adds.append(PyramidAdd(
            pct_at_add=pnl_orig,
            price_usd=price,
            amount_raw=tokens_bought,
            sol_spent=add_sol,
            tx_sig=sig,
            ts=time.time(),
        ))
        self.store.update()
        await self.tg.info(
            f"➕ <b>Pyramid #{len(pos.pyramid_adds)}</b> ${pos.symbol}  "
            f"<code>+{pnl_orig:.0f}%</code> orig\n"
            f"Eklenen: <code>{add_sol:.4f} SOL</code>\n"
            f"Yeni blended entry: <code>${new_entry:.8f}</code>\n"
            f"<a href=\"https://solscan.io/tx/{sig}\">solscan</a>"
        )
        return True

    # ---------- Hold-time liquidity drain ----------

    def _check_liquidity_drain(self, pos: Position, pair: dict) -> tuple[bool, float]:
        """Liq giriş anına göre %X düştüyse True döner."""
        if pos.entry_liquidity_usd is None or pos.entry_liquidity_usd <= 0:
            return False, 0.0
        try:
            current_liq = float((pair.get("liquidity") or {}).get("usd") or 0)
        except (TypeError, ValueError):
            return False, 0.0
        if current_liq <= 0:
            return False, 0.0
        drop_pct = (pos.entry_liquidity_usd - current_liq) / pos.entry_liquidity_usd * 100
        return drop_pct >= config.hold_liq_drain_pct, drop_pct

    # ---------- Per-position tick ----------

    async def _tick_one(self, pos: Position) -> None:
        if pos.remaining_raw <= 0:
            pos.status = "closed"
            self.store.update()
            return
        pair = await self.ds.pair("solana", pos.pair_address)
        if not pair:
            return
        try:
            price = float(pair.get("priceUsd") or 0)
        except (TypeError, ValueError):
            return
        if price <= 0:
            return

        # Peak güncelle
        if price > pos.peak_price_usd:
            pos.peak_price_usd = price
            self.store.update()

        # Liquidity drain (gerçek rug)
        drained, drop_pct = self._check_liquidity_drain(pos, pair)
        if drained:
            await self.tg.info(
                f"🚨 <b>${pos.symbol}</b> LP çekiliyor "
                f"(-<code>{drop_pct:.0f}%</code>) — exit"
            )
            await self._close_all(pos, price, f"liquidity drain -{drop_pct:.0f}%")
            return

        pnl_pct = ((price - pos.entry_price_usd) / pos.entry_price_usd) * 100
        drawdown = ((pos.peak_price_usd - price) / pos.peak_price_usd) * 100
        hit_levels = {h.level for h in pos.tp_hits}

        # Pyramid (TP1 sonrası, TP3 öncesi)
        if 1 in hit_levels and 3 not in hit_levels:
            if await self._try_pyramid(pos, price):
                return

        # TP3 — moon bag küçültme
        if 3 not in hit_levels and pnl_pct >= config.tp3_trigger:
            await self._partial_sell(pos, 3, config.tp3_trigger, config.tp3_sell, price)
            return
        # TP2 — büyük profit lock
        if 2 not in hit_levels and pnl_pct >= config.tp2_trigger:
            await self._partial_sell(pos, 2, config.tp2_trigger, config.tp2_sell, price)
            return
        # TP1 — anapara kurtarma (dinamik)
        if 1 not in hit_levels and pnl_pct >= config.tp1_trigger:
            tp1_sell = _compute_tp1_sell_pct(config.tp1_trigger)
            await self._partial_sell(pos, 1, config.tp1_trigger, tp1_sell, price)
            return

        # Trailing (sadece TP1 sonrası)
        if pos.tp_hits and drawdown >= config.trailing_stop:
            await self._close_all(pos, price, f"trailing -{drawdown:.1f}% from peak")
            return

        # Breakeven SL (TP1 sonrası — kayıp imkansız)
        if pos.breakeven_armed and pnl_pct <= 0:
            await self._close_all(pos, price, "breakeven SL")
            return

        # Hard SL (sadece pre-TP1)
        if not pos.tp_hits and pnl_pct <= -config.stop_loss:
            await self._close_all(pos, price, f"SL {pnl_pct:.1f}%")
            return

    async def tick(self) -> None:
        for pos in list(self.store.open_positions()):
            try:
                await self._tick_one(pos)
            except Exception:
                log.exception("monitor tick error for %s", pos.symbol)

    async def manual_close(self, symbol_or_addr: str) -> tuple[bool, str]:
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

"""Lean orchestrator — detect → safety → buy → monitor.

3 async loop:
  - scan_loop: kaynakları çek, filtreyi geçenleri safety + auto-buy
  - monitor_loop: açık pozisyonları izle (TP1/2/3, trailing, SL, pyramid)
  - telegram polling: komut dinleme (TelegramHub içinde)

Tüm strateji parametre kararları matematik temelli (config.py docstring).
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time

from candidate import Candidate
from config import config
from dexscreener import DexScreener
from jupiter import Jupiter, LAMPORTS_PER_SOL
from monitor import Monitor
from pumpfun import PumpFun
from risk import Risk
from safety import Safety
from screener import Screener
from stats import compute as stats_compute, format_stats
from storage import Position, Store
from telegram_hub import TelegramHub
from wallet import load_keypair

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-12s | %(message)s",
)
log = logging.getLogger("main")


class Bot:
    def __init__(self) -> None:
        kp = load_keypair()
        self.wallet_pubkey = str(kp.pubkey())
        log.info("wallet: %s", self.wallet_pubkey)

        self.ds = DexScreener()
        self.pf = PumpFun()
        self.jup = Jupiter(kp)
        self.safety = Safety(self.jup)
        self.store = Store.load()
        self.tg = TelegramHub()
        self.risk = Risk()
        self.screener = Screener(self.ds, self.pf)
        self.monitor = Monitor(self.ds, self.jup, self.store, self.tg)

        # Wire Telegram callbacks
        self.tg.status_cb = self.status_text
        self.tg.pnl_cb = self.pnl_text
        self.tg.stats_cb = self.stats_text
        self.tg.scan_stats_cb = self.scan_stats_text
        self.tg.halt_cb = self.halt_text
        self.tg.resume_cb = self.resume_text
        self.tg.close_cb = self.close_text

        self._stop = asyncio.Event()
        self._last_scan_ts: float = 0.0
        self._last_scan_count: int = 0

    # ---------- Telegram text callbacks ----------

    async def status_text(self) -> str:
        opens = self.store.open_positions()
        if not opens:
            return "📭 Açık pozisyon yok."
        lines = [f"📂 <b>{len(opens)} açık pozisyon</b>\n"]
        for p in opens:
            pair = await self.ds.pair("solana", p.pair_address)
            price_now = float((pair or {}).get("priceUsd") or 0) if pair else 0
            tps = ",".join(str(h.level) for h in p.tp_hits) or "—"
            if price_now > 0 and p.entry_price_usd > 0:
                pnl = ((price_now - p.entry_price_usd) / p.entry_price_usd) * 100
                lines.append(
                    f"• <b>${p.symbol}</b>  <code>{pnl:+.1f}%</code>  "
                    f"TP:[{tps}]  "
                    f"kalan <code>{p.remaining_raw / max(p.amount_raw, 1) * 100:.0f}%</code>"
                )
            else:
                lines.append(f"• ${p.symbol} (fiyat alınamadı)")
        return "\n".join(lines)

    async def pnl_text(self) -> str:
        s = stats_compute(self.store.positions)
        if s is None:
            return "📭 Kapanan pozisyon yok."
        emoji = "🟢" if s.total_pnl_sol >= 0 else "🔴"
        return (
            f"💼 <b>PnL özeti</b>\n"
            f"İşlem: <code>{s.n}</code>  W/L: <code>{s.n_win}/{s.n_loss}</code>  "
            f"WR: <code>{s.p_win * 100:.0f}%</code>\n"
            f"{emoji} Net: <code>{s.total_pnl_sol:+.4f} SOL</code>\n"
            f"Ort kazanan: <code>{s.avg_win_pct:+.1f}%</code>  "
            f"ort kaybeden: <code>{s.avg_loss_pct:+.1f}%</code>\n"
            f"EV/trade: <code>{s.ev_pct:+.2f}%</code>"
        )

    async def stats_text(self) -> str:
        return format_stats(self.store.positions)

    async def scan_stats_text(self) -> str:
        return self.screener.format_scan_stats()

    async def halt_text(self, reason: str) -> str:
        self.risk.halt(reason or "manual", until_ts=0.0)
        return self.risk.status_text(self.store.positions)

    async def resume_text(self) -> str:
        self.risk.resume("manual")
        return self.risk.status_text(self.store.positions)

    async def close_text(self, arg: str) -> str:
        ok, msg = await self.monitor.manual_close(arg)
        return ("✅ " if ok else "⚠️ ") + msg

    # ---------- Buy flow ----------

    async def _try_buy(self, c: Candidate) -> None:
        """5-gate ve safety geçti varsayılıyor. Risk gate + Jupiter."""
        if self.store.find_by_pair(c.pair_address):
            return
        allowed, why = self.risk.check_pre_buy(self.store.positions)
        if not allowed:
            await self.tg.info(
                f"⛔ <b>${c.base_symbol}</b> atlandı — {why}"
            )
            return

        log.info("BUY %s amount=%.4f SOL", c.base_symbol, config.buy_amount_sol)
        try:
            sig, tokens_raw = await self.jup.buy(c.base_token, config.buy_amount_sol)
        except Exception as e:
            log.exception("buy failed")
            await self.tg.info(f"❌ Alım hatası ${c.base_symbol}: <code>{e}</code>")
            return

        pos = Position(
            pair_address=c.pair_address,
            base_token=c.base_token,
            symbol=c.base_symbol,
            entry_price_usd=c.price_usd,
            peak_price_usd=c.price_usd,
            amount_raw=tokens_raw,
            remaining_raw=tokens_raw,
            sol_spent=config.buy_amount_sol,
            opened_at=time.time(),
            tx_open=sig,
            original_entry_price_usd=c.price_usd,
            entry_liquidity_usd=c.liquidity_usd,
        )
        self.store.add(pos)
        self.screener.mark_seen(c.base_token, passed=True)

        await self.tg.info(
            f"✅ <b>${c.base_symbol}</b> ALINDI\n"
            f"Giriş: <code>${c.price_usd:.8f}</code>  "
            f"liq <code>${c.liquidity_usd:,.0f}</code>\n"
            f"Harcanan: <code>{config.buy_amount_sol:.4f} SOL</code>\n\n"
            f"Strateji:\n"
            f"• TP1 +{config.tp1_trigger:.0f}% → dinamik anapara kurtarma\n"
            f"• Pyramid: ATH'lere ekle (post-TP1)\n"
            f"• TP2/TP3: moon bag küçültme\n"
            f"• Trailing %{config.trailing_stop:.0f}\n"
            f"• Pre-TP1 SL %{config.stop_loss:.0f}\n\n"
            f"<a href=\"https://solscan.io/tx/{sig}\">solscan</a> · "
            f"<a href=\"{c.url}\">dexscreener</a>"
        )

    # ---------- Loops ----------

    async def scan_loop(self) -> None:
        while not self._stop.is_set():
            try:
                candidates, result = await self.screener.scan()
                self._last_scan_ts = time.time()
                self._last_scan_count = result.passed

                for c in candidates:
                    # KATMAN 2 safety
                    safe, reason = await self.safety.check(c.base_token)
                    if not safe:
                        log.info("SAFETY SKIP %s: %s", c.base_symbol, reason)
                        self.screener.mark_seen(c.base_token, passed=False)
                        continue
                    # Auto-buy
                    await self._try_buy(c)
            except Exception:
                log.exception("scan loop error")
            await asyncio.sleep(config.scan_interval)

    async def monitor_loop(self) -> None:
        prev_closed: set[str] = {
            p.pair_address for p in self.store.positions if p.status == "closed"
        }
        while not self._stop.is_set():
            try:
                await self.monitor.tick()
                # Risk circuit breaker: yeni kapanmış varsa eşik kontrol
                halted, reason = self.risk.check_post_close(self.store.positions)
                if halted:
                    await self.tg.info(
                        f"⛔ <b>Risk gate kapatıldı</b>\n"
                        f"Sebep: <code>{reason}</code>\n"
                        f"<i>/resume ile aç.</i>"
                    )
            except Exception:
                log.exception("monitor loop error")
            await asyncio.sleep(config.monitor_interval)

    # ---------- Lifecycle ----------

    async def run(self) -> None:
        await self.tg.start()
        await self.tg.info(
            f"🤖 <b>Memecoin Sniper başladı</b> (lean v2)\n"
            f"Cüzdan: <code>{self.wallet_pubkey[:8]}...{self.wallet_pubkey[-6:]}</code>\n"
            f"Tarama her {config.scan_interval}s\n"
            f"BUY_AMOUNT: {config.buy_amount_sol} SOL  "
            f"max_open: {config.max_open_positions}\n"
            f"5-gate filter: liq>${config.min_liq_usd:.0f} · "
            f"age {config.min_age_h}-{config.max_age_h}h · "
            f"txns≥{config.min_txns_h1} · buy≥{config.min_buy_ratio:.0%}\n"
            f"Strateji: TP1 dinamik anapara → pyramid → trailing\n"
            f"Komutlar alttaki butonlardan.",
            with_keyboard=True,
        )

        # Signal handlers
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:
                pass

        tasks = [
            asyncio.create_task(self.scan_loop(), name="scan"),
            asyncio.create_task(self.monitor_loop(), name="monitor"),
        ]
        await self._stop.wait()
        log.info("shutting down...")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        try:
            await self.tg.info("🛑 Bot durduruldu.")
        except Exception:
            pass
        await self.tg.stop()
        await self.ds.close()
        await self.pf.close()
        await self.jup.close()
        await self.safety.close()


def main() -> None:
    asyncio.run(Bot().run())


if __name__ == "__main__":
    main()

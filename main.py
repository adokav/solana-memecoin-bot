"""Ana orkestratör.

Akış:
  scan_loop: DexScreener → KATMAN 1 (profil + skor) → KATMAN 2 (RugCheck + honeypot)
             → Telegram alert (buton)
  monitor_loop: açık pozisyonları kademeli çıkış mantığı ile yönet
  heartbeat: kendi sağlığını kontrol et, /health komutuna cevap için
"""
import asyncio
import logging
import signal
import time

from config import config
from dexscreener import DexScreener
from jupiter import Jupiter, LAMPORTS_PER_SOL
from monitor import Monitor
from rugcheck import RugCheckClient, SafetyReport
from screener import Candidate, Screener
from storage import Position, Store
from telegram_handler import TelegramHub, set_buy_callback
from wallet import load_keypair

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-15s | %(message)s",
)
log = logging.getLogger("main")


class Bot:
    def __init__(self) -> None:
        kp = load_keypair()
        self.ds = DexScreener()
        self.jup = Jupiter(kp)
        self.rug = RugCheckClient()
        self.store = Store.load()
        self.tg = TelegramHub()
        self.screener = Screener(self.ds)
        self.monitor = Monitor(self.ds, self.jup, self.store, self.tg)
        self._stop = asyncio.Event()
        self._last_scan_ts: float = 0
        self._last_scan_count: int = 0
        self._last_alert_ts: float = 0
        self.wallet_pubkey = str(kp.pubkey())
        log.info("wallet: %s", self.wallet_pubkey)

    # ---------- Buy callback (Telegram butonu basıldığında) ----------

    async def on_buy(self, c: Candidate, safety: SafetyReport) -> None:
        if self.store.find_by_pair(c.pair_address):
            await self.tg.info(f"⚠️ ${c.base_symbol} için zaten açık pozisyon var.")
            return

        log.info("BUY %s amount=%s SOL", c.base_symbol, config.buy_amount_sol)
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
            profile=c.profile,
            score=c.score + safety.score,
        )
        self.store.add(pos)

        await self.tg.info(
            f"✅ <b>${c.base_symbol}</b> ALINDI!\n"
            f"Giriş: <code>${c.price_usd:.8f}</code>\n"
            f"Harcanan: <code>{config.buy_amount_sol} SOL</code>\n\n"
            f"<b>Kademeli çıkış planı:</b>\n"
            f"• TP1 +{config.tp1_trigger:.0f}% → kalanın %{config.tp1_sell:.0f}'i\n"
            f"• TP2 +{config.tp2_trigger:.0f}% → kalanın %{config.tp2_sell:.0f}'i\n"
            f"• TP3 +{config.tp3_trigger:.0f}% → kalanın %{config.tp3_sell:.0f}'i\n"
            f"• Moon bag: trailing %{config.trailing_stop:.0f}\n"
            f"• SL: -{config.stop_loss:.0f}% (TP1 sonrası breakeven)\n\n"
            f"<a href=\"https://solscan.io/tx/{sig}\">solscan</a>"
        )

    # ---------- /status ----------

    async def status_text(self) -> str:
        opens = self.store.open_positions()
        if not opens:
            return "📭 Açık pozisyon yok."
        lines = [f"📂 <b>{len(opens)} açık pozisyon</b>\n"]
        for p in opens:
            pair = await self.ds.pair("solana", p.pair_address)
            price_now = float((pair or {}).get("priceUsd") or 0) if pair else 0
            tps = ",".join(str(h.level) for h in p.tp_hits) or "—"
            if price_now > 0:
                pnl = ((price_now - p.entry_price_usd) / p.entry_price_usd) * 100
                lines.append(
                    f"• <b>${p.symbol}</b>  <code>{pnl:+.1f}%</code>  "
                    f"TP:[{tps}]  "
                    f"kalan <code>{p.remaining_raw / max(p.amount_raw, 1) * 100:.0f}%</code>"
                )
            else:
                lines.append(f"• ${p.symbol} (fiyat alınamadı)")
        return "\n".join(lines)

    # ---------- /health ----------

    async def health_text(self) -> str:
        last_scan_ago = time.time() - self._last_scan_ts if self._last_scan_ts else -1
        return (
            f"💓 <b>Bot sağlığı</b>\n"
            f"Cüzdan: <code>{self.wallet_pubkey[:8]}...{self.wallet_pubkey[-6:]}</code>\n"
            f"Son tarama: <code>{last_scan_ago:.0f}s</code> önce "
            f"({self._last_scan_count} aday)\n"
            f"Açık pozisyon: <code>{len(self.store.open_positions())}</code>\n"
            f"Tarama: her <code>{config.scan_interval}s</code>\n"
            f"Pozisyon takip: her <code>{config.monitor_interval}s</code>"
        )

    # ---------- Loop: tarama ----------

    async def scan_loop(self) -> None:
        while not self._stop.is_set():
            try:
                candidates = await self.screener.scan()
                self._last_scan_ts = time.time()
                self._last_scan_count = len(candidates)
                log.info("scan: %d candidate(s) pass layer-1", len(candidates))

                sent = 0
                for c in candidates:
                    if sent >= config.max_alerts_per_scan:
                        break

                    # KATMAN 2A: RugCheck + holder dağılımı
                    safety = await self.rug.check(c.base_token)
                    if not safety.passed:
                        log.info("RUG SKIP %s: %s", c.base_symbol, "; ".join(safety.reasons))
                        self.screener.mark_alerted(c.base_token)  # cooldown'a koy ki tekrar gelmesin
                        continue

                    # Skoru güncelle (safety katkısı)
                    c.score_breakdown["holder_health"] = round(safety.score, 1)

                    # KATMAN 2B: Honeypot simülasyonu
                    ok, reason, loss_pct, impact = await self.jup.roundtrip_sim(c.base_token)
                    if not ok:
                        log.info("HONEYPOT SKIP %s: %s", c.base_symbol, reason)
                        self.screener.mark_alerted(c.base_token)
                        continue
                    log.info(
                        "PASS %s score=%.1f+%.1f loss=%.1f%% impact=%.2f%%",
                        c.base_symbol, c.score, safety.score, loss_pct, impact,
                    )

                    await self.tg.alert(c, safety)
                    self.screener.mark_alerted(c.base_token, c.score + safety.score)
                    self._last_alert_ts = time.time()
                    sent += 1

            except Exception:
                log.exception("scan loop error")

            await asyncio.sleep(config.scan_interval)

    # ---------- Loop: pozisyon takibi ----------

    async def monitor_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.monitor.tick()
            except Exception:
                log.exception("monitor loop error")
            await asyncio.sleep(config.monitor_interval)

    # ---------- Loop: heartbeat ----------

    async def heartbeat_loop(self) -> None:
        # Sadece iç log; spam olmaması için Telegram'a göndermez.
        while not self._stop.is_set():
            await asyncio.sleep(config.heartbeat_interval)
            log.info(
                "♥ heartbeat | open=%d last_scan=%.0fs ago",
                len(self.store.open_positions()),
                time.time() - self._last_scan_ts if self._last_scan_ts else -1,
            )

    # ---------- Lifecycle ----------

    async def run(self) -> None:
        set_buy_callback(self.on_buy)
        self.tg.set_status_callback(self.status_text)
        self.tg.set_health_callback(self.health_text)

        await self.tg.start()
        await self.tg.info(
            f"🤖 <b>Memecoin Sniper başladı</b>\n"
            f"Cüzdan: <code>{self.wallet_pubkey[:8]}...{self.wallet_pubkey[-6:]}</code>\n"
            f"Tarama her {config.scan_interval}s, min skor {config.min_score_to_alert}\n"
            f"Komutlar: /status /health"
        )

        # Sinyal yakalama (Render restart için graceful shutdown)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:
                pass

        tasks = [
            asyncio.create_task(self.scan_loop(), name="scan"),
            asyncio.create_task(self.monitor_loop(), name="monitor"),
            asyncio.create_task(self.heartbeat_loop(), name="heartbeat"),
        ]

        await self._stop.wait()
        log.info("shutting down...")

        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        try:
            await self.tg.info("🛑 Bot durduruldu (graceful).")
        except Exception:
            pass

        await self.tg.stop()
        await self.ds.close()
        await self.jup.close()
        await self.rug.close()


def main() -> None:
    asyncio.run(Bot().run())


if __name__ == "__main__":
    main()

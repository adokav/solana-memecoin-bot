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
from dataclasses import asdict

from analog import analog_report
from circuit_breaker import CircuitBreaker
from config import config
from dexscreener import DexScreener
from jupiter import Jupiter, LAMPORTS_PER_SOL
from macro import MacroCollector, append_snapshot, format_snapshot, latest_snapshot
from monitor import Monitor
from paper import PaperMonitor, PaperStore
from pnl import format_report, summarize
from pumpfun import PumpFun
from rugcheck import RugCheckClient, SafetyReport
from screener import Candidate, Screener
from signal_log import SignalLog
from sizing import size_for_candidate
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
        self.pf = PumpFun() if config.pumpfun_enabled else None
        self.jup = Jupiter(kp)
        self.rug = RugCheckClient()
        self.store = Store.load()
        self.tg = TelegramHub()
        self.screener = Screener(self.ds, self.pf)
        self.signal_log = SignalLog()
        self.monitor = Monitor(self.ds, self.jup, self.store, self.tg)
        self.paper_store = PaperStore.load() if config.paper_trading_enabled else None
        self.paper_monitor = (
            PaperMonitor(self.ds, self.paper_store)
            if self.paper_store is not None else None
        )
        self.macro = MacroCollector(self.pf) if config.macro_snapshot_enabled else None
        self.breaker = CircuitBreaker()
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

        # Devre kesici: önceden açıksa veya post-trade check yeni tetiklerse iptal
        if self.breaker.is_open():
            await self.tg.info(
                f"⛔ Alım iptal — devre kesici açık.\n"
                f"Sebep: <code>{self.breaker.state.reason}</code>"
            )
            return
        halted_now, reason = self.breaker.check_post_close(self.store.positions)
        if halted_now:
            await self.tg.info(f"⛔ Devre kesici tetiklendi: <code>{reason}</code>")
            return

        open_positions = self.store.open_positions()
        if len(open_positions) >= config.max_open_positions:
            await self.tg.info(
                f"⛔ Yeni alım engellendi: açık pozisyon limiti dolu "
                f"(<code>{len(open_positions)}/{config.max_open_positions}</code>)."
            )
            return

        # Adaptive sizing: paper verisinden skor bucket çarpanı (kapalıysa flat)
        paper_positions = self.paper_store.positions if self.paper_store else None
        buy_amount, size_note = size_for_candidate(
            c.score + safety.score, paper_positions, config.buy_amount_sol,
        )
        if buy_amount <= 0:
            await self.tg.info(
                f"⏭ <b>${c.base_symbol}</b> pas — adaptive size 0\n"
                f"<i>{size_note}</i>"
            )
            return

        current_exposure = sum(p.sol_spent for p in open_positions)
        projected_exposure = current_exposure + buy_amount
        if projected_exposure > config.max_total_exposure_sol:
            await self.tg.info(
                "⛔ Yeni alım engellendi: toplam risk limiti aşılacak.\n"
                f"Mevcut: <code>{current_exposure:.4f} SOL</code>\n"
                f"Yeni sonrası: <code>{projected_exposure:.4f} SOL</code>\n"
                f"Limit: <code>{config.max_total_exposure_sol:.4f} SOL</code>"
            )
            return

        log.info("BUY %s amount=%.4f SOL (%s)", c.base_symbol, buy_amount, size_note)
        try:
            sig, tokens_raw = await self.jup.buy(c.base_token, buy_amount)
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
            sol_spent=buy_amount,
            opened_at=time.time(),
            tx_open=sig,
            profile=c.profile,
            score=c.score + safety.score,
            original_entry_price_usd=c.price_usd,
        )
        self.store.add(pos)

        await self.tg.info(
            f"✅ <b>${c.base_symbol}</b> ALINDI!\n"
            f"Giriş: <code>${c.price_usd:.8f}</code>\n"
            f"Harcanan: <code>{buy_amount:.4f} SOL</code>  <i>({size_note})</i>\n\n"
            f"<b>Kademeli çıkış planı:</b>\n"
            f"• TP1 +{config.tp1_trigger:.0f}% → kalanın %{config.tp1_sell:.0f}'i\n"
            f"• TP2 +{config.tp2_trigger:.0f}% → kalanın %{config.tp2_sell:.0f}'i\n"
            f"• TP3 +{config.tp3_trigger:.0f}% → kalanın %{config.tp3_sell:.0f}'i\n"
            f"• Moon bag: trailing %{config.trailing_stop:.0f}\n"
            f"• SL: -{config.stop_loss:.0f}% (TP1 sonrası breakeven)\n\n"
            f"<a href=\"https://solscan.io/tx/{sig}\">solscan</a>"
        )

    # ---------- Auto-trade helpers ----------

    def _auto_eligible(self, c: Candidate, safety: SafetyReport, impact: float) -> bool:
        if not config.auto_trade_enabled:
            return False
        total = c.score + safety.score
        if total < config.auto_trade_min_score:
            return False
        if safety.score < config.auto_trade_min_safety_score:
            return False
        if impact > config.auto_trade_max_price_impact:
            return False
        if self.breaker.is_open():
            return False
        return True

    async def _auto_buy(self, c: Candidate, safety: SafetyReport) -> None:
        try:
            await self.tg.info(
                f"🤖 <b>AUTO-BUY</b> tetiklendi: <b>${c.base_symbol}</b>  "
                f"<code>skor {c.score + safety.score:.0f}</code>"
            )
            await self.on_buy(c, safety)
        except Exception:
            log.exception("auto-buy error for %s", c.base_symbol)

    # ---------- /halt /resume ----------

    async def halt_text(self, reason: str) -> str:
        self.breaker.halt(reason, until_ts=0.0)
        return self.breaker.status_text(self.store.positions)

    async def resume_text(self) -> str:
        self.breaker.resume("manual")
        return self.breaker.status_text(self.store.positions)

    # ---------- /close ----------

    async def close_text(self, arg: str) -> str:
        ok, msg = await self.monitor.manual_close(arg)
        return ("✅ " if ok else "⚠️ ") + msg

    # ---------- /analog ----------

    async def analog_text(self) -> str:
        return analog_report(self.signal_log)

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

    # ---------- /perf ----------

    async def perf_text(self) -> str:
        s = self.signal_log.stats()
        if s.get("total", 0) == 0:
            pending = len(self.signal_log.pending())
            return (
                f"📊 <b>Sinyal performansı</b>\n"
                f"Henüz finalize sinyal yok (24h beklenir).\n"
                f"Beklemede: <code>{pending}</code>"
            )
        return (
            f"📊 <b>Sinyal performansı</b> (finalize: {s['total']})\n"
            f"Ort. zirve 1h: <code>{s['avg_peak_1h']:+.1f}%</code>\n"
            f"Ort. zirve 24h: <code>{s['avg_peak_24h']:+.1f}%</code>\n"
            f"+30% isabet (24h): <code>{s['hit_rate_30pct_24h']:.0f}%</code>\n"
            f"+100% isabet (24h): <code>{s['hit_rate_100pct_24h']:.0f}%</code>\n"
            f"Beklemede: <code>{s['pending']}</code>"
        )

    # ---------- /pnl ----------

    async def pnl_text(self, days: int) -> str:
        summary = summarize(self.store.positions, days=days)
        return format_report(summary)

    # ---------- /paper ----------

    async def paper_text(self, days: int) -> str:
        if self.paper_store is None:
            return "📭 Paper trading kapalı (PAPER_TRADING_ENABLED=false)."
        summary = summarize(self.paper_store.positions, days=days)
        text = format_report(summary)
        return "🧪 <b>PAPER</b>\n" + text

    # ---------- /macro ----------

    async def macro_text(self) -> str:
        return format_snapshot(latest_snapshot())

    # ---------- /health ----------

    async def health_text(self) -> str:
        last_scan_ago = time.time() - self._last_scan_ts if self._last_scan_ts else -1
        auto = "AÇIK 🤖" if config.auto_trade_enabled else "kapalı"
        return (
            f"💓 <b>Bot sağlığı</b>\n"
            f"Cüzdan: <code>{self.wallet_pubkey[:8]}...{self.wallet_pubkey[-6:]}</code>\n"
            f"Son tarama: <code>{last_scan_ago:.0f}s</code> önce "
            f"({self._last_scan_count} aday)\n"
            f"Açık pozisyon: <code>{len(self.store.open_positions())}</code>\n"
            f"Tarama: her <code>{config.scan_interval}s</code>\n"
            f"Pozisyon takip: her <code>{config.monitor_interval}s</code>\n"
            f"Auto-trade: <code>{auto}</code> "
            f"(min skor <code>{config.auto_trade_min_score:.0f}</code>)\n\n"
            f"{self.breaker.status_text(self.store.positions)}"
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
                    if self.paper_store is not None:
                        self.paper_store.open(c, safety, config.buy_amount_sol)

                    # Auto-trade: yüksek güven + safety + düşük impact ise
                    # Telegram tap'i beklemeden otomatik al
                    if self._auto_eligible(c, safety, impact):
                        asyncio.create_task(self._auto_buy(c, safety))

                    if config.signal_tracking_enabled:
                        macro_now = latest_snapshot()
                        macro_dict = asdict(macro_now) if macro_now else None
                        self.signal_log.add(
                            token=c.base_token,
                            pair=c.pair_address,
                            symbol=c.base_symbol,
                            profile=c.profile,
                            entry_price_usd=c.price_usd,
                            score=c.score,
                            safety_score=safety.score,
                            score_breakdown=c.score_breakdown,
                            macro=macro_dict,
                        )
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
                # Pozisyon kapanmış olabilir → devre kesici eşiklerini değerlendir
                halted_now, reason = self.breaker.check_post_close(self.store.positions)
                if halted_now:
                    await self.tg.info(
                        f"⛔ <b>Devre kesici tetiklendi</b>\n"
                        f"Sebep: <code>{reason}</code>\n"
                        f"<i>/resume ile elle açabilirsin.</i>"
                    )
            except Exception:
                log.exception("monitor loop error")
            await asyncio.sleep(config.monitor_interval)

    # ---------- Loop: paper trading takibi ----------

    async def paper_monitor_loop(self) -> None:
        while not self._stop.is_set():
            if self.paper_monitor is not None:
                try:
                    await self.paper_monitor.tick()
                except Exception:
                    log.exception("paper monitor loop error")
            await asyncio.sleep(config.monitor_interval)

    # ---------- Loop: makro snapshot ----------

    async def macro_loop(self) -> None:
        # İlk snapshot biraz beklesin — diğer init işleri bitsin
        await asyncio.sleep(15)
        while not self._stop.is_set():
            if self.macro is not None:
                try:
                    snap = await self.macro.collect()
                    append_snapshot(snap)
                    log.info(
                        "macro: SOL=%.2f (%.1f%%) BTC.D=%.1f F&G=%d pump=%d",
                        snap.sol_price_usd, snap.sol_change_24h,
                        snap.btc_dominance, snap.fear_greed,
                        snap.pump_graduated_recent,
                    )
                except Exception:
                    log.exception("macro loop error")
            await asyncio.sleep(config.macro_snapshot_interval)

    # ---------- Loop: sinyal performans takibi (backtest data) ----------

    async def tracking_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(config.signal_tracking_interval)
            if not config.signal_tracking_enabled:
                continue
            try:
                pending = self.signal_log.pending()
                if not pending:
                    continue
                updated = 0
                for sig in pending:
                    pair = await self.ds.pair("solana", sig.pair)
                    price = float((pair or {}).get("priceUsd") or 0) if pair else 0
                    if price > 0:
                        self.signal_log.update_with_price(sig, price)
                        updated += 1
                if updated:
                    self.signal_log.save()
                    log.info("signal tracking: updated %d/%d pending", updated, len(pending))
            except Exception:
                log.exception("tracking loop error")

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
        self.tg.set_perf_callback(self.perf_text)
        self.tg.set_pnl_callback(self.pnl_text)
        self.tg.set_paper_callback(self.paper_text)
        self.tg.set_macro_callback(self.macro_text)
        self.tg.set_halt_callback(self.halt_text)
        self.tg.set_resume_callback(self.resume_text)
        self.tg.set_close_callback(self.close_text)
        self.tg.set_analog_callback(self.analog_text)

        await self.tg.start()
        await self.tg.info(
            f"🤖 <b>Memecoin Sniper başladı</b>\n"
            f"Cüzdan: <code>{self.wallet_pubkey[:8]}...{self.wallet_pubkey[-6:]}</code>\n"
            f"Tarama her {config.scan_interval}s, min skor {config.min_score_to_alert}\n"
            f"Auto-trade: <code>{'AÇIK' if config.auto_trade_enabled else 'kapalı'}</code>  "
            f"Devre kesici: <code>{'açık' if self.breaker.is_open() else 'kapalı'}</code>\n"
            f"Komutlar: /status /health /perf /pnl /paper /macro /halt /resume /close /analog"
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
            asyncio.create_task(self.tracking_loop(), name="tracking"),
        ]
        if self.paper_monitor is not None:
            tasks.append(asyncio.create_task(self.paper_monitor_loop(), name="paper"))
        if self.macro is not None:
            tasks.append(asyncio.create_task(self.macro_loop(), name="macro"))

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
        if self.pf is not None:
            await self.pf.close()
        if self.macro is not None:
            await self.macro.close()


def main() -> None:
    asyncio.run(Bot().run())


if __name__ == "__main__":
    main()

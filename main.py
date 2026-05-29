"""Alert-only Solana memecoin watcher.

Flow:
1. Discover fresh tokens from DexScreener and pump.fun.
2. Apply strict hard filters and RugCheck/Jupiter quote safety.
3. Send Telegram opportunity alerts; no automatic buying.
4. Watch alerted tokens and warn when the setup breaks.
5. Optional close button sells the wallet's full token balance if WALLET_PRIVATE_KEY is set.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress

import base58
from solders.keypair import Keypair

from candidate import parse as parse_candidate
from config import config
from dexscreener import DexScreener
from jupiter import Jupiter, JupiterError, LAMPORTS_PER_SOL
from opportunity import is_actionable, score as opportunity_score
from pumpfun import PumpFun
from safety import Safety
from screener import Screener
from storage import Store
from telegram_hub import TelegramHub
from watchlist import WatchList

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("memecoin-alert-bot")


def _load_keypair_optional() -> Keypair | None:
    if not config.wallet_private_key:
        return None
    try:
        return Keypair.from_bytes(base58.b58decode(config.wallet_private_key.strip()))
    except Exception as e:
        log.error("WALLET_PRIVATE_KEY invalid; close button disabled: %s", e)
        return None


class Bot:
    def __init__(self) -> None:
        self.store = Store.load()
        self.ds = DexScreener()
        self.pf = PumpFun()
        self.keypair = _load_keypair_optional()
        self.jup = Jupiter(self.keypair)
        self.safety = Safety(self.jup)
        self.screener = Screener(self.ds, self.pf)
        self.watchlist = WatchList(self.store, self.ds)
        self.tg = TelegramHub(self.store, close_handler=self.quick_close if self.keypair else None, buy_handler=self.quick_buy if self.keypair else None, radar_handler=self.manual_radar)
        self._stop = asyncio.Event()

        self.tg.status_cb = self.status_text
        self.tg.scan_stats_cb = self.scan_stats_text
        self.tg.ignore_cb = self.ignore_token

    async def status_text(self) -> str:
        close_state = "aktif" if self.keypair else "pasif"
        return self.store.status_text() + f"\nHızlı kapatma: <b>{close_state}</b>"

    async def scan_stats_text(self) -> str:
        return self.screener.format_scan_stats()

    async def ignore_token(self, token_mint: str) -> str:
        ok = self.watchlist.ignore(token_mint.strip())
        return "🚫 Token izleme listesinden çıkarıldı." if ok else "Token izleme listesinde bulunamadı."

    async def _best_candidate_for_token(self, token_mint: str):
        """Fetch the most liquid Solana pair for a manually supplied mint/symbol."""
        pairs = await self.ds.pairs_for_token("solana", token_mint)
        if not pairs:
            pairs = await self.ds.search(token_mint)
        candidates = [parse_candidate(p) for p in pairs]
        candidates = [c for c in candidates if c is not None and c.base_token]
        if not candidates:
            return None
        return max(candidates, key=lambda c: c.liquidity_usd)

    async def manual_radar(self, token_mint: str) -> str:
        """Manual token analysis. Adds DEVAM/IZLE tokens to watchlist."""
        token_mint = token_mint.strip()
        c = await self._best_candidate_for_token(token_mint)
        if c is None:
            return (
                "❌ <b>MANUEL RADAR</b>\n\n"
                f"<code>{token_mint}</code> için DexScreener üzerinde Solana pair bulunamadı.\n"
                "Karar: <b>UZAK DUR / veri yok</b>"
            )

        ok, safety_reason = await self.safety.check(c.base_token)
        op = opportunity_score(c, safety_reason if ok else f"safety fail: {safety_reason}")

        # Manual radar is intentionally softer than auto alerts:
        # it evaluates probability, explains risk, and watches if the setup is not clearly toxic.
        decision = "UZAK DUR"
        if ok and getattr(op, "decision", "") == "ALINABİLİR":
            decision = "DEVAM"
        elif ok and getattr(op, "decision", "") == "İZLE":
            decision = "İZLE"
        elif ok and op.exit_score >= 35 and op.risk_score <= 82:
            decision = "İZLE"
        else:
            decision = "UZAK DUR"

        if decision in {"DEVAM", "İZLE"}:
            self.watchlist.add_candidate(c, op)
            follow_line = "👁 <b>Takibe alındı.</b> Formasyon bozulursa gerekçeli uyarı göndereceğim."
        else:
            follow_line = "⛔ <b>Takibe alınmadı.</b> Risk/exit profili zayıf."

        reasons = "\n".join(f"✅ {x}" for x in op.reasons[:6]) or "✅ Ölçülebilir veri var"
        cautions = "\n".join(f"⚠️ {x}" for x in op.cautions[:6]) or "⚠️ Memecoin riski yüksek"

        return (
            f"🔍 <b>MANUEL RADAR ANALİZİ: ${c.base_symbol}</b>\n\n"
            f"Karar: <b>{decision}</b>\n"
            f"Radar: <code>{getattr(op, 'radar_score', op.opportunity_score)}/100</code> | Edge: <code>{getattr(op, 'edge_score', 0)}/100</code>\n"
            f"Survival: <code>{getattr(op, 'survival_score', 0)}/100</code> | Expansion: <code>{getattr(op, 'expansion_score', op.opportunity_score)}/100</code>\n"
            f"Exit: <code>{op.exit_score}/100</code> | Timing: <code>{getattr(op, 'timing_score', 0)}/100</code> | Confidence: <code>{getattr(op, 'confidence_score', 0)}/100</code>\n"
            f"Risk: <code>{op.risk_score}/100</code>\n\n"
            f"<b>Devam gerekçeleri</b>\n{reasons}\n\n"
            f"<b>Riskler</b>\n{cautions}\n\n"
            f"Likidite: <code>${c.liquidity_usd:,.0f}</code> | Tx h1: <code>{c.txns_h1}</code> | Buy: <code>{(c.buys_h1/max(c.txns_h1,1)):.0%}</code>\n"
            f"Hacim/Liq: <code>{(c.volume_h1/max(c.liquidity_usd,1)):.2f}x</code> | H1: <code>{c.price_change_h1:+.1f}%</code>\n"
            f"Mint: <code>{c.base_token}</code>\n"
            f"<a href=\"{c.url or ('https://dexscreener.com/solana/' + c.pair_address)}\">DexScreener</a>\n\n"
            f"{follow_line}"
        )

    async def quick_buy(self, token_mint: str) -> tuple[bool, str]:
        if not self.keypair:
            return False, "Alım pasif: WALLET_PRIVATE_KEY tanımlı değil."
        try:
            sig, lamports, out_raw = await self.jup.buy(token_mint, config.buy_amount_sol)
            sol = lamports / LAMPORTS_PER_SOL
            watched = self.store.find_watch(token_mint)
            self.store.record_buy(token_mint, watched.symbol if watched else "?", sol, out_raw, sig)
            bal_lamports = await self.jup.sol_balance()
            return True, (
                "Alım emri gönderildi.\n"
                f"Harcanan SOL: <code>{sol:.5f}</code>\n"
                f"Tahmini token raw: <code>{out_raw}</code>\n"
                f"Güncel SOL bakiye: <code>{bal_lamports / LAMPORTS_PER_SOL:.5f}</code>\n"
                f"https://solscan.io/tx/{sig}"
            )
        except JupiterError as e:
            return False, f"Alım başarısız: <code>{e}</code>"
        except Exception as e:
            log.exception("quick buy error")
            return False, f"Alım hatası: <code>{e}</code>"

    async def quick_close(self, token_mint: str) -> tuple[bool, str]:
        if not self.keypair:
            return False, "Hızlı kapatma pasif: WALLET_PRIVATE_KEY tanımlı değil."
        try:
            sig, sold_raw, out_lamports = await self.jup.sell_all(token_mint)
            sol = out_lamports / LAMPORTS_PER_SOL
            pos = self.store.record_close(token_mint, sol, sig)
            bal_lamports = await self.jup.sol_balance()
            pnl_line = "PnL: <code>giriş maliyeti kaydı yok</code>"
            if pos and pos.entry_sol > 0:
                pnl = sol - pos.entry_sol
                pnl_pct = (pnl / pos.entry_sol) * 100
                pnl_line = f"PnL: <code>{pnl:+.5f} SOL ({pnl_pct:+.1f}%)</code>"
            watched = self.store.find_watch(token_mint)
            if watched:
                watched.ignored = True
                self.store.save()
            return True, (
                "Pozisyon kapatma emri gönderildi.\n"
                f"Satılan token raw: <code>{sold_raw}</code>\n"
                f"Çıkan SOL: <code>{sol:.5f}</code>\n"
                f"{pnl_line}\n"
                f"Güncel SOL bakiye: <code>{bal_lamports / LAMPORTS_PER_SOL:.5f}</code>\n"
                f"https://solscan.io/tx/{sig}"
            )
        except JupiterError as e:
            return False, f"Satış başarısız: <code>{e}</code>"
        except Exception as e:
            log.exception("quick close error")
            return False, f"Satış hatası: <code>{e}</code>"

    async def scan_loop(self) -> None:
        while not self._stop.is_set():
            try:
                candidates, _ = await self.screener.scan()
                for c in candidates:
                    ok, safety_reason = await self.safety.check(c.base_token)
                    if not ok:
                        self.screener.mark_seen(c.base_token, passed=False)
                        log.info("safety reject %s: %s", c.base_symbol, safety_reason)
                        continue

                    op = opportunity_score(c, safety_reason)

                    # Alert policy:
                    # - Alınabilir radar: Telegram bildirimi + AL butonu.
                    # - Early watch: İstenirse sessiz izleme; güçlenirse watch_loop haber verir.
                    if is_actionable(op):
                        await self.tg.send_opportunity(c, op)
                        if config.watch_after_alert:
                            self.watchlist.add_candidate(c, op)
                        self.screener.mark_seen(c.base_token, passed=True)
                        await asyncio.sleep(0.5)
                    else:
                        if config.silent_watch_early and config.watch_after_alert:
                            self.watchlist.add_candidate(c, op)
                        self.screener.mark_seen(c.base_token, passed=False)
                        log.info(
                            "watch-only %s: O=%s R=%s X=%s mode=%s",
                            c.base_symbol, op.opportunity_score, op.risk_score, op.exit_score, op.mode
                        )
            except Exception:
                log.exception("scan loop error")

            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=config.scan_interval)

    async def watch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                warnings = await self.watchlist.tick()
                for warning in warnings:
                    await self.tg.send_watch_warning(warning)
                    await asyncio.sleep(0.3)
            except Exception:
                log.exception("watch loop error")

            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=config.monitor_interval)

    async def run(self) -> None:
        if config.auto_buy_enabled:
            log.warning("AUTO_BUY_ENABLED is ignored. This build uses Telegram-confirmed buys only.")
        await self.tg.run()
        await self.tg.info("🟢 Memecoin radar bot başladı. Alım sadece Telegram çift onayıyla çalışır.", with_keyboard=True)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._stop.set)

        tasks = [
            asyncio.create_task(self.scan_loop(), name="scan_loop"),
            asyncio.create_task(self.watch_loop(), name="watch_loop"),
        ]
        await self._stop.wait()

        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        await self.close()

    async def close(self) -> None:
        with suppress(Exception):
            await self.tg.info("🔴 Bot kapanıyor.")
        await self.tg.stop()
        await self.safety.close()
        await self.jup.close()
        await self.ds.close()
        await self.pf.close()


async def main() -> None:
    await Bot().run()


if __name__ == "__main__":
    asyncio.run(main())

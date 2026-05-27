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

from config import config
from dexscreener import DexScreener
from jupiter import Jupiter, JupiterError, LAMPORTS_PER_SOL
from opportunity import score as opportunity_score
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
        self.tg = TelegramHub(self.store, close_handler=self.quick_close if self.keypair else None)
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

    async def quick_close(self, token_mint: str) -> tuple[bool, str]:
        if not self.keypair:
            return False, "Hızlı kapatma pasif: WALLET_PRIVATE_KEY tanımlı değil."
        try:
            sig, sold_raw, out_lamports = await self.jup.sell_all(token_mint)
            sol = out_lamports / LAMPORTS_PER_SOL
            return True, (
                "Satış emri gönderildi.\n"
                f"Token raw: <code>{sold_raw}</code>\n"
                f"Tahmini SOL: <code>{sol:.5f}</code>\n"
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
                    await self.tg.send_opportunity(c, op)
                    if config.watch_after_alert:
                        self.watchlist.add_candidate(c)
                    self.screener.mark_seen(c.base_token, passed=True)
                    await asyncio.sleep(0.5)
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
            log.warning("AUTO_BUY_ENABLED is ignored. This build is alert-only.")
        await self.tg.run()
        await self.tg.info("🟢 Alert-only memecoin bot başladı. Otomatik alım kapalı.", with_keyboard=True)

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

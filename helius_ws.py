"""Helius WebSocket subscription — smart wallet pollu real-time tetikler.

Polling (60s) yerine: smart wallet'lardan biri swap yaptığında WS bize
anında bildirim atar, smart_wallet_loop hemen uyanır ve poll çalıştırır.

Mantık:
  - logsSubscribe with mentions filter — bir wallet'ı mention eden herhangi
    bir tx'e abone ol
  - Notification gelince smart_wakeup_event'i set et
  - smart_wallet_loop bu event'i ya da timer'ı bekler (hangisi önce olursa)
  - Poll idempotent (last_processed_sig dedup), gereksiz çağrı problem değil

Reconnect logic dahil (exponential backoff). Bağlantı kopuk iken polling
normal interval'da devam eder — graceful degradation.

NOT: Helius free tier WS bağlantısı destekler. logsSubscribe rate-limit'i
yoktur ama abonelik sayısı sınırlı olabilir (~100 wallet için yeterli).
"""
from __future__ import annotations

import asyncio
import json
import logging

import aiohttp

from config import config

log = logging.getLogger(__name__)


class HeliusWs:
    def __init__(
        self,
        smart_store,
        wakeup_event: asyncio.Event,
    ) -> None:
        self.smart_store = smart_store
        self.wakeup = wakeup_event
        self._sub_id_to_wallet: dict[int, str] = {}
        self._stopped = asyncio.Event()

    async def stop(self) -> None:
        self._stopped.set()

    def _ws_url(self) -> str:
        return f"wss://mainnet.helius-rpc.com/?api-key={config.helius_api_key}"

    async def _subscribe_wallet(self, ws: aiohttp.ClientWebSocketResponse, addr: str, req_id: int) -> None:
        await ws.send_json({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [addr]},
                {"commitment": "confirmed"},
            ],
        })

    async def _run_session(self) -> None:
        if not config.helius_api_key:
            log.warning("helius_ws: HELIUS_API_KEY yok, WS başlatılmıyor")
            return
        if not self.smart_store or not self.smart_store.wallets:
            log.info("helius_ws: izlenecek smart wallet yok, beklemede")
            return

        url = self._ws_url()
        log.info("helius_ws: connecting (%d wallet)", len(self.smart_store.wallets))
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(url, heartbeat=30) as ws:
                # Her aktif (non-disabled) wallet için subscribe
                req_id = 0
                active = [
                    addr for addr, w in self.smart_store.wallets.items()
                    if not w.disabled
                ]
                for addr in active:
                    req_id += 1
                    await self._subscribe_wallet(ws, addr, req_id)
                    self._sub_id_to_wallet[req_id] = addr

                log.info("helius_ws: subscribed to %d wallet logs", len(active))

                async for msg in ws:
                    if self._stopped.is_set():
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            data = json.loads(msg.data)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        # logsNotification → params.subscription + params.result
                        method = data.get("method")
                        if method == "logsNotification":
                            # Smart wallet üzerinde bir tx gerçekleşti → poll wake up
                            self.wakeup.set()
                            log.debug("helius_ws: wakeup triggered")
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break

    async def run(self) -> None:
        """Reconnect-loop'lu çalışma. Bağlantı koparsa exponential backoff."""
        if not config.helius_ws_enabled:
            return
        backoff = 5
        while not self._stopped.is_set():
            try:
                await self._run_session()
                # Normal kapanma — kısa bekle, tekrar bağlan
                backoff = 5
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("helius_ws: connection failed (%s), retry in %ds", e, backoff)
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(60, backoff * 2)

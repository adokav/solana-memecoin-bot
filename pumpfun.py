"""Pump.fun graduate kaynağı — tek metod, recently_graduated().

Bonding curve tamamlanan tokenlar Raydium'a göç eder. DexScreener
indexlemesi 2-5dk sürer; biz pump.fun'dan direkt çekersek o gecikmeyi
aşarız.
"""
from __future__ import annotations

import logging

import httpx

from config import config

log = logging.getLogger(__name__)

BASE = "https://frontend-api.pump.fun"


class PumpFun:
    def __init__(self, timeout: float = 10.0) -> None:
        self._http = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 memecoin-bot/2.0",
                "Origin": "https://pump.fun",
                "Referer": "https://pump.fun/",
            },
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def recently_graduated(self) -> list[str]:
        """Son graduate olan token'ların mint adreslerini döner."""
        if not config.pumpfun_enabled:
            return []
        params = {
            "offset": 0,
            "limit": config.pumpfun_fetch_limit,
            "sort": "last_trade_timestamp",
            "order": "DESC",
            "includeNsfw": "false",
            "complete": "true",
        }
        try:
            r = await self._http.get(f"{BASE}/coins", params=params)
            if r.status_code != 200:
                log.warning("pump.fun -> %d", r.status_code)
                return []
            data = r.json() or []
            mints: list[str] = []
            for item in data if isinstance(data, list) else []:
                mint = item.get("mint") or item.get("address")
                if mint and isinstance(mint, str):
                    mints.append(mint)
            return mints
        except (httpx.HTTPError, ValueError) as e:
            log.warning("pump.fun error: %s", e)
            return []

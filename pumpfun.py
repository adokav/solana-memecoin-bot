"""Pump.fun graduation kaynağı.

Pump.fun'da bonding curve tamamlanan ("graduated") token'lar Raydium'a
otomatik göç eder. DexScreener bunları indexlemesi 2-5 dakika sürer.
Bu modül pump.fun frontend API'sinden son graduate olan token'ları doğrudan
çeker, screener'a ek kaynak olarak feed eder.

API resmi belge yayınlanmamış (frontend reverse-engineered). Hata olursa
sessizce boş döner — DexScreener kaynakları çalışmaya devam eder.
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
                "User-Agent": "Mozilla/5.0 memecoin-bot/1.0",
                "Origin": "https://pump.fun",
                "Referer": "https://pump.fun/",
            },
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict | None = None) -> list[dict]:
        try:
            r = await self._http.get(f"{BASE}{path}", params=params)
            if r.status_code != 200:
                log.warning("pump.fun %s -> %d", path, r.status_code)
                return []
            data = r.json()
            return data if isinstance(data, list) else []
        except (httpx.HTTPError, ValueError) as e:
            log.warning("pump.fun %s error: %s", path, e)
            return []

    async def recently_graduated(self, limit: int | None = None) -> list[str]:
        """Son graduate olan token'ların mint adreslerini döner."""
        params = {
            "offset": 0,
            "limit": limit or config.pumpfun_fetch_limit,
            "sort": "last_trade_timestamp",
            "order": "DESC",
            "includeNsfw": "false",
            "complete": "true",  # bonding curve tamamlandı = graduated
        }
        items = await self._get("/coins", params=params)
        mints: list[str] = []
        for it in items:
            mint = it.get("mint") or it.get("address")
            if mint and isinstance(mint, str):
                mints.append(mint)
        log.info("pump.fun graduated: %d mint", len(mints))
        return mints

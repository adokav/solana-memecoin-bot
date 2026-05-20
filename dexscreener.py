"""DexScreener API istemcisi.

Rate limits:
  /token-boosts/*  -> 60 req/min
  /token-profiles/* -> 60 req/min
  /latest/dex/*    -> 300 req/min
  /tokens/v1/*     -> 300 req/min
"""
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE = "https://api.dexscreener.com"


class DexScreener:
    def __init__(self, timeout: float = 15.0) -> None:
        self._http = httpx.AsyncClient(
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "memecoin-bot/1.0"},
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str) -> Any:
        try:
            r = await self._http.get(f"{BASE}{path}")
            if r.status_code == 429:
                log.warning("DexScreener rate limited on %s", path)
                return None
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            log.warning("DexScreener %s error: %s", path, e)
            return None

    async def latest_boosted(self) -> list[dict]:
        data = await self._get("/token-boosts/latest/v1")
        return data if isinstance(data, list) else []

    async def top_boosted(self) -> list[dict]:
        data = await self._get("/token-boosts/top/v1")
        return data if isinstance(data, list) else []

    async def latest_profiles(self) -> list[dict]:
        data = await self._get("/token-profiles/latest/v1")
        return data if isinstance(data, list) else []

    async def pairs_for_token(self, chain: str, token_address: str) -> list[dict]:
        """Bir token mint için tüm pair'leri döner (fiyat/likidite/hacim dahil)."""
        data = await self._get(f"/tokens/v1/{chain}/{token_address}")
        return data if isinstance(data, list) else []

    async def pair(self, chain: str, pair_address: str) -> dict | None:
        """Tek pair detayı (pozisyon takibi için)."""
        data = await self._get(f"/latest/dex/pairs/{chain}/{pair_address}")
        if not data:
            return None
        pairs = data.get("pairs") or []
        return pairs[0] if pairs else None

    async def search(self, query: str) -> list[dict]:
        data = await self._get(f"/latest/dex/search?q={query}")
        if not data:
            return []
        return data.get("pairs") or []

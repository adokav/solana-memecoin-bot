"""LunarCrush social analytics.

LunarCrush memecoin'lerin galaxy_score, alt_rank, social_volume gibi
metriklerini sağlar. Twitter mention velocity için en yakın ücretsiz/uygun
fiyatlı alternatif. Free tier rate limit'i olduğu için sadece zaten
filtreleri geçen üst aday'lar için sorgulanır.

API: https://lunarcrush.com/developers
Coin endpoint: /api4/public/coins/<symbol_or_id>/v1
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from config import config

log = logging.getLogger(__name__)

BASE = "https://lunarcrush.com/api4/public"


@dataclass
class LunarMetrics:
    symbol: str
    galaxy_score: float = 0.0       # 0-100 overall health
    alt_rank: int = 0
    social_volume: int = 0          # post + comment count
    social_score: float = 0.0
    social_contributors: int = 0


class LunarCrush:
    def __init__(self, timeout: float = 8.0) -> None:
        self._http = httpx.AsyncClient(
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
        # symbol -> (ts, metrics) cache (1 saat TTL — free tier API budget'ı koru)
        self._cache: dict[str, tuple[float, LunarMetrics | None]] = {}
        self._cache_ttl = 3600

    async def close(self) -> None:
        await self._http.aclose()

    async def coin_metrics(self, symbol: str) -> LunarMetrics | None:
        if not config.lunarcrush_api_key or not symbol:
            return None
        key = symbol.upper().lstrip("$")
        cached = self._cache.get(key)
        if cached and (time.time() - cached[0]) < self._cache_ttl:
            return cached[1]
        try:
            r = await self._http.get(
                f"{BASE}/coins/{key}/v1",
                headers={
                    "Authorization": f"Bearer {config.lunarcrush_api_key}",
                },
            )
            if r.status_code != 200:
                # Coin LunarCrush takibinde değil — yeni memecoin'lerde normal
                log.debug("lunarcrush %s -> %d", key, r.status_code)
                self._cache[key] = (time.time(), None)
                return None
            payload = r.json() or {}
            data = payload.get("data") or {}
            if not data:
                self._cache[key] = (time.time(), None)
                return None
            metrics = LunarMetrics(
                symbol=key,
                galaxy_score=float(data.get("galaxy_score") or 0),
                alt_rank=int(data.get("alt_rank") or 0),
                social_volume=int(data.get("social_volume_24h") or data.get("social_volume") or 0),
                social_score=float(data.get("social_score") or 0),
                social_contributors=int(data.get("social_contributors") or 0),
            )
            self._cache[key] = (time.time(), metrics)
            return metrics
        except (httpx.HTTPError, ValueError, TypeError) as e:
            log.warning("lunarcrush error %s: %s", key, e)
            return None

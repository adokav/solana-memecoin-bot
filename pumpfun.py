"""Pump.fun graduation kaynağı + pre-graduation detector.

Pump.fun'da bonding curve tamamlanan ("graduated") token'lar Raydium'a
otomatik göç eder. DexScreener bunları indexlemesi 2-5 dakika sürer.
Bu modül pump.fun frontend API'sinden son graduate olan token'ları doğrudan
çeker, screener'a ek kaynak olarak feed eder.

Ek olarak: graduate olmamış aktif coin'leri ve sosyal engagement
metriklerini (reply_count) sağlar. PrePumpDetector bu veriyi kullanır.

API resmi belge yayınlanmamış (frontend reverse-engineered). Hata olursa
sessizce boş döner — DexScreener kaynakları çalışmaya devam eder.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from config import config

log = logging.getLogger(__name__)

BASE = "https://frontend-api.pump.fun"
# Bonding curve grad threshold (~85 SOL, USD karşılığı SOL/USD'a göre değişir)
PUMP_GRADUATION_MC_USD = 69_000


@dataclass
class PumpCoin:
    mint: str
    name: str
    symbol: str
    usd_market_cap: float
    progress_pct: float       # 0..100, graduation eşiğine ne kadar yakın
    reply_count: int
    complete: bool            # True = graduate oldu
    created_ts: float         # token oluşum unix ts

    @property
    def pump_url(self) -> str:
        return f"https://pump.fun/{self.mint}"


def _parse_coin(item: dict) -> PumpCoin | None:
    mint = item.get("mint") or item.get("address")
    if not mint or not isinstance(mint, str):
        return None
    try:
        mc = float(item.get("usd_market_cap") or 0)
    except (TypeError, ValueError):
        mc = 0.0
    progress = (
        min(100.0, mc / PUMP_GRADUATION_MC_USD * 100) if mc > 0 else 0.0
    )
    try:
        created = float(item.get("created_timestamp") or 0) / 1000.0
    except (TypeError, ValueError):
        created = 0.0
    return PumpCoin(
        mint=str(mint),
        name=str(item.get("name") or ""),
        symbol=str(item.get("symbol") or ""),
        usd_market_cap=mc,
        progress_pct=progress,
        reply_count=int(item.get("reply_count") or 0),
        complete=bool(item.get("complete")),
        created_ts=created,
    )


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

    async def active_coins(self, limit: int | None = None) -> list[PumpCoin]:
        """Henüz graduate olmamış aktif coin'leri MC desc döner.

        Pre-graduation detection ve reply velocity tracking için kaynak.
        """
        params = {
            "offset": 0,
            "limit": limit or config.pumpfun_active_fetch_limit,
            "sort": "usd_market_cap",
            "order": "DESC",
            "includeNsfw": "false",
            "complete": "false",  # aktif (bonding curve'de) coin'ler
        }
        items = await self._get("/coins", params=params)
        coins: list[PumpCoin] = []
        for it in items:
            coin = _parse_coin(it)
            if coin and not coin.complete:
                coins.append(coin)
        return coins

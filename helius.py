"""Helius enhanced transactions API ince istemcisi.

Bot zaten Helius RPC URL'i kullanıyor (rugcheck.py içinde holder verisi).
Buradaki istemci adres-bazlı enhanced transactions endpoint'ine vurur —
swap olaylarını parsed olarak döner, manuel decode gerekmez.

Docs: https://docs.helius.dev/api-reference/endpoints/enhanced-transactions
"""
from __future__ import annotations

import logging

import httpx

from config import config

log = logging.getLogger(__name__)

BASE = "https://api.helius.xyz/v0"


class Helius:
    def __init__(self, timeout: float = 15.0) -> None:
        self._http = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._http.aclose()

    async def address_transactions(
        self,
        address: str,
        before_sig: str | None = None,
        limit: int = 25,
        tx_type: str | None = None,
    ) -> list[dict]:
        if not config.helius_api_key:
            return []
        params: dict[str, str] = {
            "api-key": config.helius_api_key,
            "limit": str(limit),
        }
        if tx_type:
            params["type"] = tx_type
        if before_sig:
            params["before"] = before_sig
        try:
            r = await self._http.get(
                f"{BASE}/addresses/{address}/transactions", params=params
            )
            if r.status_code == 200:
                data = r.json()
                return data if isinstance(data, list) else []
            log.warning("helius txs %s -> %d", address[:8], r.status_code)
        except httpx.HTTPError as e:
            log.warning("helius txs error %s: %s", address[:8], e)
        return []

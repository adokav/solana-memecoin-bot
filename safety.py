"""KATMAN 2: gerçek rug barrier — 3 check sırasıyla.

Felsefe: holder count / LP lock gibi soft check'ler ATILDI. Onlar fresh
launch'larda hiçbir zaman mevcut değil, gerçek rug indikatörü değiller.

Gerçek rug barrier'lar:
  1. mint authority revoked → infinite mint yapılamaz
  2. freeze authority revoked → wallet'lar dondurulamaz
  3. honeypot sim (jupiter SOL→token→SOL) → satılabilirlik testi

Üçü de geçerse SAFE. Biri fail → SKIP.

RugCheck API'sini sadece authority info için kullanıyoruz.
"""
from __future__ import annotations

import logging

import httpx

from config import config

log = logging.getLogger(__name__)

RUGCHECK_BASE = "https://api.rugcheck.xyz/v1"


class Safety:
    def __init__(self, jupiter, timeout: float = 15.0) -> None:
        self.jup = jupiter  # Jupiter instance — honeypot sim için
        self._http = httpx.AsyncClient(
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "memecoin-bot/2.0"},
        )
        # token → (ts, passed) cache 10dk
        self._cache: dict[str, tuple[float, bool, str]] = {}
        self._cache_ttl = 600

    async def close(self) -> None:
        await self._http.aclose()

    async def _rugcheck_summary(self, mint: str) -> dict | None:
        """RugCheck mint/freeze authority bilgisini döner."""
        try:
            r = await self._http.get(f"{RUGCHECK_BASE}/tokens/{mint}/report/summary")
            if r.status_code == 200:
                return r.json()
            log.debug("rugcheck %s -> %d", mint[:8], r.status_code)
        except httpx.HTTPError as e:
            log.debug("rugcheck %s error: %s", mint[:8], e)
        return None

    async def check(self, mint: str) -> tuple[bool, str]:
        """3 hard check. (passed, reason) döner."""
        import time
        cached = self._cache.get(mint)
        if cached and time.time() - cached[0] < self._cache_ttl:
            return cached[1], cached[2]

        # 1 & 2: mint + freeze authority revoke
        summary = await self._rugcheck_summary(mint)
        authority_note = "ok"
        if summary is None:
            # RugCheck bazen rate-limit/timeout verebilir. Bunu tek başına hard reject
            # yapmak radarın körleşmesine yol açar. Jupiter roundtrip geçerse aday
            # skorlanır ama risk notuna "authority unknown" olarak düşer.
            if not config.safety_allow_rugcheck_unreachable:
                reason = "rugcheck unreachable"
                self._cache[mint] = (time.time(), False, reason)
                return False, reason
            authority_note = "authority unknown; rugcheck unreachable"
        else:
            risks = summary.get("risks") or []
            risk_names = {(r.get("name") or "").lower() for r in risks}
            # RugCheck'in raporladığı kritik authority risk'leri
            if config.require_mint_revoked:
                if any("mint authority" in n for n in risk_names):
                    reason = "mint authority not revoked"
                    self._cache[mint] = (time.time(), False, reason)
                    return False, reason
            if config.require_freeze_revoked:
                if any("freeze authority" in n for n in risk_names):
                    reason = "freeze authority not revoked"
                    self._cache[mint] = (time.time(), False, reason)
                    return False, reason

        # 3: honeypot sim — Jupiter roundtrip
        ok, hp_reason, loss_pct, impact = await self.jup.roundtrip_sim(mint)
        if not ok:
            self._cache[mint] = (time.time(), False, f"honeypot: {hp_reason}")
            return False, f"honeypot: {hp_reason}"

        passed_reason = "jupiter exit ok" if authority_note == "ok" else f"{authority_note}; jupiter exit ok"
        self._cache[mint] = (time.time(), True, passed_reason)
        return True, passed_reason

"""Jito bundle desteği — priority fee yetmediği memecoin yarışları için.

Akış:
  1. Jupiter normal swap tx'i build eder
  2. Aynı blockhash ile küçük bir 'tip transfer' tx imzalanır
     (rastgele bir Jito tip account'a gönderir)
  3. İki tx beraber Jito Block Engine'a sendBundle ile atılır
  4. Validator'lar tip miktarına göre bundle'ı sıralar — priority fee
     yarışını bypass eder

Tip account listesi `getTipAccounts` RPC ile dinamik çekilir (hardcoded
adres ezberlemeden). Bundle başarısız olursa direkt RPC fallback'i
jupiter.py içinde işlenir.
"""
from __future__ import annotations

import logging
import random

import httpx
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

log = logging.getLogger(__name__)

DEFAULT_BLOCK_ENGINE = "https://mainnet.block-engine.jito.wtf"


class JitoError(Exception):
    pass


class JitoClient:
    def __init__(self, base_url: str = DEFAULT_BLOCK_ENGINE, timeout: float = 20.0) -> None:
        self._http = httpx.AsyncClient(
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )
        self.base = base_url.rstrip("/")
        self._tip_accounts: list[Pubkey] = []

    async def close(self) -> None:
        await self._http.aclose()

    async def _rpc(self, method: str, params: list) -> dict:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        r = await self._http.post(f"{self.base}/api/v1/bundles", json=payload)
        if r.status_code != 200:
            raise JitoError(f"jito http {r.status_code}: {r.text[:200]}")
        data = r.json()
        if "error" in data:
            raise JitoError(f"jito rpc error: {data['error']}")
        return data

    async def ensure_tip_accounts(self) -> bool:
        if self._tip_accounts:
            return True
        try:
            data = await self._rpc("getTipAccounts", [])
            for addr in data.get("result") or []:
                try:
                    self._tip_accounts.append(Pubkey.from_string(addr))
                except Exception:
                    log.warning("invalid jito tip account: %s", addr)
            return bool(self._tip_accounts)
        except (JitoError, httpx.HTTPError) as e:
            log.warning("jito getTipAccounts failed: %s", e)
            return False

    def random_tip_account(self) -> Pubkey | None:
        return random.choice(self._tip_accounts) if self._tip_accounts else None

    def build_tip_tx(
        self,
        kp: Keypair,
        amount_lamports: int,
        recent_blockhash: Hash,
        tip_account: Pubkey,
    ) -> VersionedTransaction:
        ix = transfer(TransferParams(
            from_pubkey=kp.pubkey(),
            to_pubkey=tip_account,
            lamports=int(amount_lamports),
        ))
        msg = MessageV0.try_compile(
            payer=kp.pubkey(),
            instructions=[ix],
            address_lookup_table_accounts=[],
            recent_blockhash=recent_blockhash,
        )
        return VersionedTransaction(msg, [kp])

    async def send_bundle(self, txs_b64: list[str]) -> str:
        if not 1 <= len(txs_b64) <= 5:
            raise JitoError(f"bundle size must be 1-5 (got {len(txs_b64)})")
        data = await self._rpc("sendBundle", [txs_b64])
        return data.get("result") or ""

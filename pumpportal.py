"""PumpPortal API client — pump.fun bonding curve trading.

Local mode: PumpPortal serialized unsigned tx döner, biz lokalde imzalayıp
RPC'ye gönderiyoruz. Lightning mode (private key paylaşmak) kullanılmıyor.

1% PumpPortal trading fee tx'in içinde, ek hesaplama gerekmiyor.

Docs: https://pumpportal.fun/api-trading
"""
from __future__ import annotations

import logging

import httpx
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

log = logging.getLogger(__name__)

BASE = "https://pumpportal.fun/api"


class PumpPortalError(Exception):
    pass


class PumpPortal:
    def __init__(self, keypair: Keypair, rpc: AsyncClient) -> None:
        self.kp = keypair
        self.rpc = rpc
        self._http = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._http.aclose()

    async def _trade_local(
        self,
        action: str,
        mint: str,
        amount,
        denominated_in_sol: bool,
        slippage_pct: int,
        priority_fee_sol: float,
        pool: str = "pump",
        keypair: Keypair | None = None,
    ) -> bytes:
        kp = keypair or self.kp
        body = {
            "publicKey": str(kp.pubkey()),
            "action": action,
            "mint": mint,
            "amount": amount,
            "denominatedInSol": "true" if denominated_in_sol else "false",
            "slippage": slippage_pct,
            "priorityFee": priority_fee_sol,
            "pool": pool,
        }
        r = await self._http.post(f"{BASE}/trade-local", json=body)
        if r.status_code != 200:
            raise PumpPortalError(
                f"pumpportal {action} {r.status_code}: {r.text[:200]}"
            )
        # Local mode raw transaction bytes döner
        return r.content

    async def _sign_and_send(
        self, tx_bytes: bytes, keypair: Keypair | None = None,
    ) -> str:
        kp = keypair or self.kp
        tx = VersionedTransaction.from_bytes(tx_bytes)
        signed = VersionedTransaction(tx.message, [kp])
        opts = TxOpts(
            skip_preflight=True,
            preflight_commitment=Confirmed,
            max_retries=3,
        )
        resp = await self.rpc.send_raw_transaction(bytes(signed), opts=opts)
        sig = str(resp.value)
        log.info("pumpportal tx sent: %s", sig)
        await self.rpc.confirm_transaction(resp.value, commitment=Confirmed)
        return sig

    async def buy(
        self,
        mint: str,
        sol_amount: float,
        slippage_pct: int = 15,
        priority_fee_sol: float = 0.001,
        keypair: Keypair | None = None,
    ) -> str:
        """Bonding curve'den SOL ile token al. Tx imzası döner."""
        tx_bytes = await self._trade_local(
            "buy", mint, sol_amount, True,
            slippage_pct, priority_fee_sol, "pump",
            keypair=keypair,
        )
        return await self._sign_and_send(tx_bytes, keypair=keypair)

    async def sell(
        self,
        mint: str,
        percent: float = 100,
        slippage_pct: int = 15,
        priority_fee_sol: float = 0.001,
        keypair: Keypair | None = None,
    ) -> str:
        """Bonding curve'e token'ı sat (yüzde olarak, %100 = tüm pozisyon)."""
        amount_str = f"{int(percent)}%"
        tx_bytes = await self._trade_local(
            "sell", mint, amount_str, False,
            slippage_pct, priority_fee_sol, "pump",
            keypair=keypair,
        )
        return await self._sign_and_send(tx_bytes, keypair=keypair)

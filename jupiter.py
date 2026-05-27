"""Jupiter quote/sell client.

Quotes work without a wallet. Swaps require WALLET_PRIVATE_KEY.
"""
from __future__ import annotations

import base64
import logging

import httpx
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TokenAccountOpts, TxOpts
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from config import config

log = logging.getLogger(__name__)

JUP_QUOTE = "https://quote-api.jup.ag/v6/quote"
JUP_SWAP = "https://quote-api.jup.ag/v6/swap"
LAMPORTS_PER_SOL = 1_000_000_000


class JupiterError(Exception):
    pass


class Jupiter:
    def __init__(self, keypair: Keypair | None = None) -> None:
        self.kp = keypair
        self.rpc = AsyncClient(config.rpc_url, commitment=Confirmed)
        self._http = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self.rpc.close()
        await self._http.aclose()

    async def quote(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int | None = None) -> dict | None:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(slippage_bps or config.buy_slippage_bps),
            "onlyDirectRoutes": "false",
            "asLegacyTransaction": "false",
        }
        try:
            r = await self._http.get(JUP_QUOTE, params=params)
            if r.status_code == 400:
                return None
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            log.warning("quote error: %s", e)
            return None

    async def roundtrip_sim(self, token_mint: str) -> tuple[bool, str, float, float]:
        lamports_in = int(config.quote_test_sol * LAMPORTS_PER_SOL)
        q1 = await self.quote(config.sol_mint, token_mint, lamports_in, config.buy_slippage_bps)
        if not q1:
            return False, "no SOL->token route", 100.0, 100.0
        tokens_out = int(q1.get("outAmount") or 0)
        impact_in = float(q1.get("priceImpactPct") or 0) * 100
        if tokens_out <= 0:
            return False, "zero tokens out", 100.0, impact_in
        if impact_in > config.max_price_impact_pct:
            return False, f"buy impact {impact_in:.2f}%", 100.0, impact_in

        q2 = await self.quote(token_mint, config.sol_mint, tokens_out, config.sell_slippage_bps)
        if not q2:
            return False, "no token->SOL route", 100.0, impact_in
        lamports_back = int(q2.get("outAmount") or 0)
        impact_out = float(q2.get("priceImpactPct") or 0) * 100
        if lamports_back <= 0:
            return False, "zero SOL back", 100.0, max(impact_in, impact_out)

        loss_pct = ((lamports_in - lamports_back) / lamports_in) * 100
        worst_impact = max(impact_in, impact_out)
        if loss_pct > config.max_roundtrip_loss_pct:
            return False, f"roundtrip loss {loss_pct:.1f}%", loss_pct, worst_impact
        return True, "ok", loss_pct, worst_impact

    def _swap_body(self, quote_resp: dict) -> dict:
        if self.kp is None:
            raise JupiterError("wallet keypair is required for swaps")
        body: dict = {
            "quoteResponse": quote_resp,
            "userPublicKey": str(self.kp.pubkey()),
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": {
                "priorityLevelWithMaxLamports": {
                    "maxLamports": int(config.max_priority_fee_lamports),
                    "priorityLevel": config.priority_fee_level,
                }
            },
        }
        if config.dynamic_slippage_enabled:
            body["dynamicSlippage"] = {"maxBps": int(config.dynamic_slippage_max_bps)}
        return body

    async def _build_and_send(self, quote_resp: dict) -> str:
        if self.kp is None:
            raise JupiterError("wallet keypair is required for swaps")
        r = await self._http.post(JUP_SWAP, json=self._swap_body(quote_resp))
        if r.status_code != 200:
            raise JupiterError(f"swap build failed: {r.status_code} {r.text[:200]}")
        tx_b64 = r.json()["swapTransaction"]
        unsigned = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        signed = VersionedTransaction(unsigned.message, [self.kp])
        opts = TxOpts(skip_preflight=True, preflight_commitment=Confirmed, max_retries=3)
        resp = await self.rpc.send_raw_transaction(bytes(signed), opts=opts)
        sig = str(resp.value)
        await self.rpc.confirm_transaction(resp.value, commitment=Confirmed)
        return sig

    async def sell(self, token_mint: str, token_amount_raw: int) -> tuple[str, int]:
        q = await self.quote(token_mint, config.sol_mint, token_amount_raw, config.sell_slippage_bps)
        if not q:
            raise JupiterError("no route for sell")
        out_lamports = int(q.get("outAmount") or 0)
        sig = await self._build_and_send(q)
        return sig, out_lamports

    async def token_balance_raw(self, token_mint: str) -> int:
        if self.kp is None:
            raise JupiterError("wallet keypair is required")
        mint = Pubkey.from_string(token_mint)
        resp = await self.rpc.get_token_accounts_by_owner_json_parsed(
            self.kp.pubkey(),
            TokenAccountOpts(mint=mint),
        )
        total = 0
        for item in resp.value:
            try:
                total += int(item.account.data.parsed["info"]["tokenAmount"]["amount"])
            except Exception:
                continue
        return total

    async def sell_all(self, token_mint: str) -> tuple[str, int, int]:
        amount = await self.token_balance_raw(token_mint)
        if amount <= 0:
            raise JupiterError("wallet has zero token balance")
        sig, out_lamports = await self.sell(token_mint, amount)
        return sig, amount, out_lamports

"""Jupiter v6 entegrasyonu.

İşlevler:
  - Round-trip simülasyonu (honeypot kontrolü): SOL→token→SOL quote, kayıp ölç
  - Gerçek alım: SOL→token
  - Kısmi satış: token miktarının %X'i → SOL
"""
import base64
import logging

import httpx
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from config import config
from jito import JitoClient, JitoError

log = logging.getLogger(__name__)

JUP_QUOTE = "https://quote-api.jup.ag/v6/quote"
JUP_SWAP = "https://quote-api.jup.ag/v6/swap"

LAMPORTS_PER_SOL = 1_000_000_000


class JupiterError(Exception):
    pass


class Jupiter:
    def __init__(self, keypair: Keypair) -> None:
        self.kp = keypair
        self.rpc = AsyncClient(config.rpc_url, commitment=Confirmed)
        self._http = httpx.AsyncClient(timeout=30.0)
        self.jito: JitoClient | None = (
            JitoClient(config.jito_block_engine_url) if config.jito_enabled else None
        )

    async def close(self) -> None:
        await self.rpc.close()
        await self._http.aclose()
        if self.jito is not None:
            await self.jito.close()

    # ---------- Quote ----------

    async def quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: int | None = None,
    ) -> dict | None:
        params: dict[str, str] = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "onlyDirectRoutes": "false",
            "asLegacyTransaction": "false",
        }
        # dynamicSlippage'ı swap aşamasında veriyoruz; quote'a slippageBps yine
        # tavan olarak konuluyor — boş bırakmak quote'u "no slippage" sayıp
        # impact filtresini saptırabiliyor.
        params["slippageBps"] = str(slippage_bps or config.slippage_bps)
        try:
            r = await self._http.get(JUP_QUOTE, params=params)
            if r.status_code == 400:
                # route yok = satılamıyor
                return None
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            log.warning("quote error: %s", e)
            return None

    # ---------- Honeypot simülasyonu ----------

    async def roundtrip_sim(self, token_mint: str) -> tuple[bool, str, float, float]:
        """SOL -> token -> SOL quote yap. (passed, reason, loss_pct, price_impact_pct).

        Gerçek işlem GÖNDERMEZ, sadece quote'lar (ücretsiz).
        """
        # Bizim normalde harcayacağımız miktar
        lamports_in = int(config.buy_amount_sol * LAMPORTS_PER_SOL)

        q1 = await self.quote(config.sol_mint, token_mint, lamports_in)
        if not q1:
            return False, "no SOL->token route", 100.0, 100.0
        tokens_out = int(q1.get("outAmount") or 0)
        impact_in = float(q1.get("priceImpactPct") or 0) * 100
        if tokens_out <= 0:
            return False, "zero tokens out", 100.0, impact_in
        if impact_in > config.max_price_impact_pct:
            return False, f"buy impact {impact_in:.2f}%", 100.0, impact_in

        # Geri sat
        q2 = await self.quote(token_mint, config.sol_mint, tokens_out)
        if not q2:
            return False, "no token->SOL route (HONEYPOT)", 100.0, impact_in
        lamports_back = int(q2.get("outAmount") or 0)
        impact_out = float(q2.get("priceImpactPct") or 0) * 100
        if lamports_back <= 0:
            return False, "zero SOL back (HONEYPOT)", 100.0, impact_in

        loss_pct = ((lamports_in - lamports_back) / lamports_in) * 100
        worst_impact = max(impact_in, impact_out)

        if loss_pct > config.max_roundtrip_loss_pct:
            return False, f"roundtrip loss {loss_pct:.1f}%", loss_pct, worst_impact

        return True, "ok", loss_pct, worst_impact

    # ---------- TX gönderim ----------

    def _swap_body(self, quote_resp: dict) -> dict:
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
        body = self._swap_body(quote_resp)
        r = await self._http.post(JUP_SWAP, json=body)
        if r.status_code != 200:
            raise JupiterError(f"swap build failed: {r.status_code} {r.text}")
        tx_b64 = r.json()["swapTransaction"]

        raw = base64.b64decode(tx_b64)
        unsigned = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(unsigned.message, [self.kp])

        # Jito path: bundle swap + tip ile validator sıralamasını bypass et
        if self.jito is not None:
            jito_sig = await self._try_jito_send(signed)
            if jito_sig is not None:
                return jito_sig

        # Direkt RPC (fallback veya Jito kapalıysa default)
        opts = TxOpts(skip_preflight=True, preflight_commitment=Confirmed, max_retries=3)
        resp = await self.rpc.send_raw_transaction(bytes(signed), opts=opts)
        sig = str(resp.value)
        log.info("tx sent (rpc): %s", sig)
        await self.rpc.confirm_transaction(resp.value, commitment=Confirmed)
        return sig

    async def _try_jito_send(self, signed_swap: VersionedTransaction) -> str | None:
        """Bundle başarılıysa swap signature döner, değilse None (RPC fallback)."""
        if self.jito is None:
            return None
        try:
            if not await self.jito.ensure_tip_accounts():
                log.warning("jito tip accounts unavailable, falling back to RPC")
                return None
            tip_account = self.jito.random_tip_account()
            if tip_account is None:
                return None

            recent_bh = signed_swap.message.recent_blockhash
            tip_tx = self.jito.build_tip_tx(
                self.kp,
                config.jito_tip_lamports,
                recent_bh,
                tip_account,
            )

            swap_b64 = base64.b64encode(bytes(signed_swap)).decode()
            tip_b64 = base64.b64encode(bytes(tip_tx)).decode()
            bundle_id = await self.jito.send_bundle([swap_b64, tip_b64])

            sig_obj = signed_swap.signatures[0]
            sig_str = str(sig_obj)
            log.info("jito bundle sent: %s | swap sig: %s", bundle_id, sig_str)
            await self.rpc.confirm_transaction(sig_obj, commitment=Confirmed)
            return sig_str
        except JitoError as e:
            log.warning("jito bundle failed (%s), falling back to RPC", e)
            return None
        except Exception:
            log.exception("jito bundle unexpected error, falling back to RPC")
            return None

    # ---------- High level ----------

    async def buy(self, token_mint: str, sol_amount: float) -> tuple[str, int]:
        """SOL -> token. Dönüş: (tx_sig, alınan_raw_token_miktarı)."""
        lamports = int(sol_amount * LAMPORTS_PER_SOL)
        q = await self.quote(
            config.sol_mint, token_mint, lamports,
            slippage_bps=config.buy_slippage_bps,
        )
        if not q:
            raise JupiterError("no route for buy")
        out_amount = int(q["outAmount"])
        sig = await self._build_and_send(q)
        return sig, out_amount

    async def sell(self, token_mint: str, token_amount_raw: int) -> tuple[str, int]:
        """Token -> SOL (tam tutar). Dönüş: (tx_sig, alınan_lamports)."""
        q = await self.quote(
            token_mint, config.sol_mint, token_amount_raw,
            slippage_bps=config.sell_slippage_bps,
        )
        if not q:
            raise JupiterError("no route for sell")
        out_lamports = int(q["outAmount"])
        sig = await self._build_and_send(q)
        return sig, out_lamports

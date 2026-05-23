"""Makro durum snapshot — saatte bir SOL fiyat / BTC dom / F&G + pump.fun aktivitesi.

Gelecekte 'bugünkü makro' ile geçmiş benzer günleri analog-backtest için
biriken arşiv. JSONL formatında append edilir, append-only güvenli.

Kaynaklar (hepsi ücretsiz, anahtarsız):
  - CoinGecko global + simple price (SOL)
  - Alternative.me Fear & Greed
  - pump.fun frontend (graduation rate proxy)
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass

import httpx

from config import config
from pumpfun import PumpFun

log = logging.getLogger(__name__)

DB_PATH = config.data_dir / "macro.jsonl"


@dataclass
class MacroSnapshot:
    ts: float
    sol_price_usd: float = 0.0
    sol_change_24h: float = 0.0
    btc_dominance: float = 0.0
    total_market_cap_usd: float = 0.0
    fear_greed: int = 0
    fear_greed_label: str = ""
    pump_graduated_recent: int = 0  # pump.fun "complete=true" listedeki son N — sektör aktivite proxy'si


class MacroCollector:
    def __init__(self, pf: PumpFun | None = None, timeout: float = 10.0) -> None:
        # CoinGecko free tier bazen datacenter UA'larını reddediyor — browser UA kullan
        self._http = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Accept": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
                ),
            },
        )
        self.pf = pf

    async def close(self) -> None:
        await self._http.aclose()

    def _cg_headers(self) -> dict:
        if config.coingecko_api_key:
            return {"x-cg-demo-api-key": config.coingecko_api_key}
        return {}

    async def _coingecko_global(self) -> dict:
        try:
            r = await self._http.get(
                "https://api.coingecko.com/api/v3/global",
                headers=self._cg_headers(),
            )
            if r.status_code == 200:
                return (r.json() or {}).get("data") or {}
            log.warning("coingecko global -> %d", r.status_code)
        except httpx.HTTPError as e:
            log.warning("coingecko global error: %s", e)
        return {}

    async def _coingecko_sol(self) -> dict:
        try:
            r = await self._http.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={
                    "ids": "solana",
                    "vs_currencies": "usd",
                    "include_24hr_change": "true",
                },
                headers=self._cg_headers(),
            )
            if r.status_code == 200:
                return (r.json() or {}).get("solana") or {}
            log.warning("coingecko sol -> %d", r.status_code)
        except httpx.HTTPError as e:
            log.warning("coingecko sol error: %s", e)
        return {}

    async def _jupiter_sol_price(self) -> float:
        """CoinGecko başarısızsa Jupiter Price API fallback (SOL/USD)."""
        try:
            r = await self._http.get(
                "https://api.jup.ag/price/v2",
                params={"ids": "So11111111111111111111111111111111111111112"},
            )
            if r.status_code == 200:
                data = (r.json() or {}).get("data") or {}
                sol = data.get("So11111111111111111111111111111111111111112") or {}
                price = sol.get("price")
                if price:
                    return float(price)
        except (httpx.HTTPError, ValueError, TypeError) as e:
            log.warning("jupiter price fallback error: %s", e)
        return 0.0

    async def _fear_greed(self) -> dict:
        try:
            r = await self._http.get("https://api.alternative.me/fng/?limit=1")
            if r.status_code == 200:
                data = (r.json() or {}).get("data") or []
                return data[0] if data else {}
        except httpx.HTTPError as e:
            log.warning("fear&greed error: %s", e)
        return {}

    async def collect(self) -> MacroSnapshot:
        snap = MacroSnapshot(ts=time.time())
        glob = await self._coingecko_global()
        sol = await self._coingecko_sol()
        fg = await self._fear_greed()
        try:
            snap.sol_price_usd = float(sol.get("usd") or 0)
            snap.sol_change_24h = float(sol.get("usd_24h_change") or 0)
            btc_pct = (glob.get("market_cap_percentage") or {}).get("btc")
            snap.btc_dominance = float(btc_pct or 0)
            tmc = (glob.get("total_market_cap") or {}).get("usd")
            snap.total_market_cap_usd = float(tmc or 0)
            snap.fear_greed = int(fg.get("value") or 0)
            snap.fear_greed_label = str(fg.get("value_classification") or "")
        except (TypeError, ValueError) as e:
            log.warning("macro parse error: %s", e)

        # CoinGecko SOL fiyatı vermediyse Jupiter Price'a düş
        if snap.sol_price_usd <= 0:
            jup_price = await self._jupiter_sol_price()
            if jup_price > 0:
                snap.sol_price_usd = jup_price
                log.info("macro: sol_price from jupiter fallback = $%.2f", jup_price)

        if self.pf is not None and config.pumpfun_enabled:
            try:
                grads = await self.pf.recently_graduated(limit=50)
                snap.pump_graduated_recent = len(grads)
            except Exception as e:
                log.warning("pump grad proxy error: %s", e)

        return snap


def append_snapshot(snap: MacroSnapshot) -> None:
    line = json.dumps(asdict(snap))
    with DB_PATH.open("a") as f:
        f.write(line + "\n")


def latest_snapshot() -> MacroSnapshot | None:
    if not DB_PATH.exists():
        return None
    try:
        with DB_PATH.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return None
            seek_back = min(size, 4096)
            f.seek(-seek_back, 2)
            tail = f.read().decode(errors="ignore")
        last = tail.strip().split("\n")[-1]
        data = json.loads(last)
        return MacroSnapshot(**data)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
        log.warning("macro latest read error: %s", e)
        return None


def format_snapshot(snap: MacroSnapshot | None) -> str:
    if snap is None:
        return "📊 <b>Makro</b>\nHenüz snapshot yok (1 saat içinde gelir)."
    age_min = (time.time() - snap.ts) / 60
    return (
        f"📊 <b>Makro durum</b>  (snapshot <code>{age_min:.0f}dk</code> önce)\n"
        f"SOL: <code>${snap.sol_price_usd:,.2f}</code>  "
        f"<code>{snap.sol_change_24h:+.1f}%</code> 24h\n"
        f"BTC dom: <code>{snap.btc_dominance:.1f}%</code>\n"
        f"Toplam piyasa: <code>${snap.total_market_cap_usd/1e12:.2f}T</code>\n"
        f"F&amp;G: <code>{snap.fear_greed}</code> ({snap.fear_greed_label})\n"
        f"Pump.fun graduated (son 50): <code>{snap.pump_graduated_recent}</code>"
    )

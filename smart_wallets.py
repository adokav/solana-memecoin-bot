"""Smart wallet tracking — bot'un en güçlü erken sinyal kaynağı.

Tutarlı kazanan cüzdanların SOL→memecoin alımlarını Helius enhanced
transactions API ile periyodik olarak çekeriz. Aynı tokene 1 saat içinde
N+ smart wallet alım yaparsa screener:
  - Skor sistemine güçlü bonus ekler (smart_signal komponenti)
  - Token DexScreener'da yoksa bile scan'e enjekte eder

Cüzdan listesi:
  - data/smart_wallets.json — kalıcı liste
  - İlk run: SMART_WALLETS env var "addr:label,addr:label,..." ile seed
  - /addwallet, /rmwallet komutlarıyla canlıda yönetilir
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from config import config
from helius import Helius

log = logging.getLogger(__name__)

DB_PATH = config.data_dir / "smart_wallets.json"

# Memecoin alımı saymayacağımız temel tokenlar (quote/stablecoin)
EXCLUDED_OUTPUT_MINTS = {
    "So11111111111111111111111111111111111111112",   # WSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


@dataclass
class SmartWallet:
    address: str
    label: str = ""
    added_ts: float = 0.0
    last_processed_sig: Optional[str] = None
    last_seen_ts: float = 0.0
    hit_count: int = 0


@dataclass
class SmartBuy:
    wallet: str
    label: str
    token_mint: str
    ts: float
    sol_value: float


@dataclass
class SmartWalletStore:
    wallets: dict[str, SmartWallet] = field(default_factory=dict)
    # Bellek-içi: token_mint -> son SmartBuy'lar. Restart'ta sıfırlanır,
    # poll loop birkaç tur sonra repopulate eder.
    recent_buys: dict[str, list[SmartBuy]] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "SmartWalletStore":
        store = cls()
        if not DB_PATH.exists():
            seed = (config.smart_wallets_seed or "").strip()
            if seed:
                now = time.time()
                for entry in seed.split(","):
                    parts = [p.strip() for p in entry.strip().split(":", 1)]
                    if parts and parts[0]:
                        addr = parts[0]
                        label = parts[1] if len(parts) > 1 else ""
                        store.wallets[addr] = SmartWallet(
                            address=addr, label=label, added_ts=now,
                        )
            store._save()
            return store
        try:
            data = json.loads(DB_PATH.read_text())
            for w in data.get("wallets", []):
                store.wallets[w["address"]] = SmartWallet(**w)
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            log.error("smart wallets load error: %s", e)
        return store

    def _save(self) -> None:
        DB_PATH.write_text(json.dumps(
            {"wallets": [asdict(w) for w in self.wallets.values()]},
            indent=2,
        ))

    def add_wallet(self, address: str, label: str = "") -> bool:
        if not address or address in self.wallets:
            return False
        self.wallets[address] = SmartWallet(
            address=address, label=label, added_ts=time.time(),
        )
        self._save()
        return True

    def remove_wallet(self, address: str) -> bool:
        if address not in self.wallets:
            return False
        del self.wallets[address]
        self._save()
        return True

    def cleanup_recent_buys(self) -> None:
        cutoff = time.time() - config.smart_buy_window_min * 60
        for mint in list(self.recent_buys.keys()):
            self.recent_buys[mint] = [
                b for b in self.recent_buys[mint] if b.ts > cutoff
            ]
            if not self.recent_buys[mint]:
                del self.recent_buys[mint]

    def record_buy(self, buy: SmartBuy) -> None:
        self.recent_buys.setdefault(buy.token_mint, []).append(buy)

    def buys_for(self, token_mint: str) -> list[SmartBuy]:
        self.cleanup_recent_buys()
        return list(self.recent_buys.get(token_mint, []))

    def unique_wallets_for(self, token_mint: str) -> int:
        return len({b.wallet for b in self.buys_for(token_mint)})

    def tokens_with_min_buys(self, min_buys: int) -> list[str]:
        self.cleanup_recent_buys()
        out = []
        for mint, buys in self.recent_buys.items():
            if len({b.wallet for b in buys}) >= min_buys:
                out.append(mint)
        return out


# -------- Swap parse helper --------

def _extract_buy_mint(tx: dict, owner: str) -> tuple[str | None, float]:
    """Helius parsed SWAP tx'inden: owner SOL/stablecoin yatırıp aldığı
    memecoin mintini ve harcadığı SOL'u döner.
    """
    events = tx.get("events") or {}
    swap = events.get("swap") or {}

    sol_value = 0.0
    native_in = swap.get("nativeInput") or {}
    if isinstance(native_in, dict):
        try:
            sol_value = float(native_in.get("amount") or 0) / 1_000_000_000
        except (TypeError, ValueError):
            sol_value = 0.0

    # Owner'a giden ilk excluded olmayan çıkış mintleri = memecoin alımı
    for to in (swap.get("tokenOutputs") or []):
        if to.get("userAccount") != owner:
            continue
        mint = to.get("mint")
        if not mint or mint in EXCLUDED_OUTPUT_MINTS:
            continue
        return mint, sol_value

    return None, 0.0


# -------- Tracker (polling loop) --------

class SmartWalletTracker:
    def __init__(self, store: SmartWalletStore, helius: Helius) -> None:
        self.store = store
        self.helius = helius

    async def poll_wallet(self, w: SmartWallet) -> int:
        if not config.helius_api_key:
            return 0
        txs = await self.helius.address_transactions(
            w.address, limit=10, tx_type="SWAP",
        )
        if not txs:
            return 0

        new_buys = 0
        window_ts = time.time() - config.smart_buy_window_min * 60
        latest_sig = None
        for tx in txs:  # Helius default: newest-first
            sig = tx.get("signature")
            if not sig:
                continue
            if latest_sig is None:
                latest_sig = sig
            if w.last_processed_sig and sig == w.last_processed_sig:
                break  # bu noktadan geri kalanı önceden işledik
            if tx.get("type") != "SWAP":
                continue
            ts = float(tx.get("timestamp") or 0)
            if ts == 0 or ts < window_ts:
                continue
            mint, sol_value = _extract_buy_mint(tx, w.address)
            if not mint:
                continue
            self.store.record_buy(SmartBuy(
                wallet=w.address, label=w.label, token_mint=mint,
                ts=ts, sol_value=sol_value,
            ))
            new_buys += 1

        if latest_sig:
            w.last_processed_sig = latest_sig
            w.last_seen_ts = time.time()
            self.store._save()
        return new_buys

    async def poll_all(self) -> int:
        if not self.store.wallets:
            return 0
        total = 0
        for w in list(self.store.wallets.values()):
            try:
                total += await self.poll_wallet(w)
            except Exception:
                log.exception("smart wallet poll error %s", w.address[:8])
            await asyncio.sleep(0.5)  # rate limit nezaketi
        if total > 0:
            self.store.cleanup_recent_buys()
            log.info(
                "smart wallets: %d new buys recorded, %d tracked tokens",
                total, len(self.store.recent_buys),
            )
        return total


def format_wallets_text(store: SmartWalletStore) -> str:
    if not store.wallets:
        return (
            "📭 Henüz kayıtlı smart wallet yok.\n"
            "<code>/addwallet &lt;adres&gt; [label]</code> ile ekle."
        )
    lines = [f"🤖 <b>Smart Wallets</b> ({len(store.wallets)})"]
    now = time.time()
    sorted_wallets = sorted(
        store.wallets.values(),
        key=lambda w: -w.hit_count,
    )
    for i, w in enumerate(sorted_wallets[:30], 1):
        short = f"{w.address[:6]}..{w.address[-4:]}"
        label = f" [{w.label}]" if w.label else ""
        if w.last_seen_ts > 0:
            ago_min = (now - w.last_seen_ts) / 60
            seen = f"{ago_min:.0f}dk önce" if ago_min < 60 else f"{ago_min/60:.1f}sa önce"
        else:
            seen = "henüz tarama yok"
        lines.append(
            f"{i}. <code>{short}</code>{label}  "
            f"hit <code>{w.hit_count}</code>  ·  {seen}"
        )
    if len(store.wallets) > 30:
        lines.append(f"<i>...ve {len(store.wallets) - 30} daha</i>")
    return "\n".join(lines)

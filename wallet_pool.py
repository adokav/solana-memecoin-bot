"""Multi-wallet pool (anti-detection scaffolding).

Default kapalı (WALLET_POOL_ENABLED=false). Aktifken ana cüzdana ek
olarak WALLET_POOL_KEYS env'inden virgülle ayrılmış private key'ler
yüklenir. Buy işlemleri rastgele bir cüzdanla yapılır; sell aynı
cüzdandan çıkar (Position.wallet_pubkey saklanır).

MEV bot'larının bot'umuzu pattern tanımasını zorlaştırır. Şu an ölçeğimizde
gerçek MEV hedefimiz olmayabilir ama altyapı hazır — büyüdükçe açılır.

Buy/sell entegrasyonu Jupiter ve PumpPortal client'larında — şu an
sadece pool yönetimi scaffolding.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass

import base58
from solders.keypair import Keypair

from config import config

log = logging.getLogger(__name__)


@dataclass
class WalletEntry:
    keypair: Keypair
    pubkey: str
    last_used_ts: float = 0.0
    n_used: int = 0


class WalletPool:
    def __init__(self, primary: Keypair) -> None:
        self.primary = WalletEntry(
            keypair=primary, pubkey=str(primary.pubkey()),
        )
        self.extras: list[WalletEntry] = []
        if config.wallet_pool_enabled and config.wallet_pool_keys:
            for raw in config.wallet_pool_keys.split(","):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    kp = self._load_keypair_b58(raw)
                    self.extras.append(WalletEntry(
                        keypair=kp, pubkey=str(kp.pubkey()),
                    ))
                except Exception:
                    log.exception("wallet pool: failed to load secondary key")
        log.info(
            "wallet pool: primary + %d extras (enabled=%s)",
            len(self.extras), config.wallet_pool_enabled,
        )

    @staticmethod
    def _load_keypair_b58(raw: str) -> Keypair:
        secret = base58.b58decode(raw)
        return Keypair.from_bytes(secret)

    def all_wallets(self) -> list[WalletEntry]:
        return [self.primary] + self.extras

    def pick_for_buy(self) -> WalletEntry:
        """Buy için cüzdan seç. Pool kapalıysa primary, açıksa rastgele."""
        if not config.wallet_pool_enabled or not self.extras:
            self.primary.last_used_ts = _now()
            self.primary.n_used += 1
            return self.primary
        # Tüm cüzdanlardan rastgele seç (load balance)
        all_wallets = self.all_wallets()
        chosen = random.choice(all_wallets)
        chosen.last_used_ts = _now()
        chosen.n_used += 1
        return chosen

    def find_by_pubkey(self, pubkey: str) -> WalletEntry | None:
        for w in self.all_wallets():
            if w.pubkey == pubkey:
                return w
        return None


def _now() -> float:
    import time
    return time.time()


def format_pool_status(pool: WalletPool) -> str:
    if not config.wallet_pool_enabled:
        return (
            "💼 <b>Wallet pool</b>\n"
            "Tek cüzdan (pool kapalı).\n"
            f"Primary: <code>{pool.primary.pubkey[:8]}..{pool.primary.pubkey[-6:]}</code>"
        )
    lines = [
        f"💼 <b>Wallet pool</b> ({1 + len(pool.extras)} cüzdan)",
    ]
    for i, w in enumerate(pool.all_wallets()):
        tag = "🔑 primary" if i == 0 else f"🗝️  extra {i}"
        short = f"{w.pubkey[:8]}..{w.pubkey[-6:]}"
        lines.append(
            f"{tag}  <code>{short}</code>  used=<code>{w.n_used}</code>"
        )
    return "\n".join(lines)

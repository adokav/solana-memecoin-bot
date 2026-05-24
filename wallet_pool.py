"""Multi-wallet pool — risk-profile aware allocation.

Default kapalı (WALLET_POOL_ENABLED=false). Aktifken WALLET_POOL_KEYS
env'inden virgülle ayrılmış key'ler yüklenir. Format:
  - "key1,key2"  → her ikisi de balanced profile
  - "key1:aggressive,key2:conservative" → profile belirtilmiş
  - "key1:aggressive:1.5,key2" → profile + custom size multiplier

Profiles ve default sizing weight'leri:
  - aggressive: 1.5×  (yüksek skor adaylara yönlendirilir)
  - balanced:   1.0×  (mid-skor)
  - conservative: 0.5× (düşük skor / her zaman güvenli)

Picker (pick_for_buy(score_total)) skor bantına göre profil seçer:
  - score ≥ 85 (high conviction): aggressive havuzundan rastgele
  - 70 ≤ score < 85: balanced havuzdan
  - score < 70: conservative havuzdan
  - İlgili profile yoksa fallback primary
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field

import base58
from solders.keypair import Keypair

from config import config

log = logging.getLogger(__name__)


PROFILE_DEFAULT_SIZE = {
    "aggressive": 1.5,
    "balanced": 1.0,
    "conservative": 0.5,
}


@dataclass
class WalletEntry:
    keypair: Keypair
    pubkey: str
    profile: str = "balanced"
    size_multiplier: float = 1.0
    last_used_ts: float = 0.0
    n_used: int = 0


class WalletPool:
    def __init__(self, primary: Keypair) -> None:
        self.primary = WalletEntry(
            keypair=primary, pubkey=str(primary.pubkey()),
            profile="balanced", size_multiplier=1.0,
        )
        self.extras: list[WalletEntry] = []
        if config.wallet_pool_enabled and config.wallet_pool_keys:
            for raw in config.wallet_pool_keys.split(","):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = self._parse_entry(raw)
                    if entry:
                        self.extras.append(entry)
                except Exception:
                    log.exception("wallet pool: failed to load secondary key")
        log.info(
            "wallet pool: primary + %d extras (enabled=%s)",
            len(self.extras), config.wallet_pool_enabled,
        )

    def _parse_entry(self, raw: str) -> WalletEntry | None:
        """Format: key | key:profile | key:profile:multiplier"""
        parts = raw.split(":")
        key_b58 = parts[0].strip()
        profile = (parts[1].strip().lower() if len(parts) > 1 else "balanced")
        if profile not in PROFILE_DEFAULT_SIZE:
            log.warning("wallet pool: unknown profile '%s', using balanced", profile)
            profile = "balanced"
        if len(parts) > 2:
            try:
                multiplier = float(parts[2].strip())
            except ValueError:
                multiplier = PROFILE_DEFAULT_SIZE[profile]
        else:
            multiplier = PROFILE_DEFAULT_SIZE[profile]
        try:
            secret = base58.b58decode(key_b58)
            kp = Keypair.from_bytes(secret)
        except Exception:
            log.exception("wallet pool: invalid key")
            return None
        return WalletEntry(
            keypair=kp, pubkey=str(kp.pubkey()),
            profile=profile, size_multiplier=multiplier,
        )

    def all_wallets(self) -> list[WalletEntry]:
        return [self.primary] + self.extras

    def _wallets_by_profile(self, profile: str) -> list[WalletEntry]:
        return [w for w in self.all_wallets() if w.profile == profile]

    def pick_for_buy(self, score_total: float = 0.0) -> WalletEntry:
        """Skor bantına göre profil seçer, ilgili profil havuzundan rastgele."""
        if not config.wallet_pool_enabled or not self.extras:
            self.primary.last_used_ts = time.time()
            self.primary.n_used += 1
            return self.primary

        if score_total >= 85:
            preferred = "aggressive"
        elif score_total >= 70:
            preferred = "balanced"
        else:
            preferred = "conservative"

        candidates = self._wallets_by_profile(preferred)
        if not candidates:
            # Fallback chain: balanced -> primary
            candidates = self._wallets_by_profile("balanced") or [self.primary]

        chosen = random.choice(candidates)
        chosen.last_used_ts = time.time()
        chosen.n_used += 1
        return chosen

    def find_by_pubkey(self, pubkey: str) -> WalletEntry | None:
        for w in self.all_wallets():
            if w.pubkey == pubkey:
                return w
        return None


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
    # Profile dağılımı
    counts: dict[str, int] = {"aggressive": 0, "balanced": 0, "conservative": 0}
    for w in pool.all_wallets():
        counts[w.profile] = counts.get(w.profile, 0) + 1
    lines.append(
        f"<i>Profile dağılımı: 🔥 {counts.get('aggressive', 0)} agg / "
        f"⚖️ {counts.get('balanced', 0)} bal / "
        f"🛡 {counts.get('conservative', 0)} cons</i>\n"
    )
    for i, w in enumerate(pool.all_wallets()):
        tag = "🔑" if i == 0 else "🗝️"
        emoji = {"aggressive": "🔥", "balanced": "⚖️", "conservative": "🛡"}.get(
            w.profile, "?"
        )
        short = f"{w.pubkey[:8]}..{w.pubkey[-6:]}"
        lines.append(
            f"{tag} <code>{short}</code>  {emoji} {w.profile} "
            f"×<code>{w.size_multiplier:.1f}</code>  "
            f"used=<code>{w.n_used}</code>"
        )
    lines.append(
        "\n<i>Picker: skor ≥85 → aggressive, 70-85 → balanced, "
        "&lt;70 → conservative</i>"
    )
    return "\n".join(lines)

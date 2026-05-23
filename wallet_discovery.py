"""Smart wallet auto-discovery.

Bot kendi geçmişinden kazanan token'ları tespit eder; her kazananın
ilk N saatindeki alıcılarını Helius enhanced transactions API'siyle
çeker, "candidate wallet" havuzuna alır. Aynı candidate K+ kazananda
görünürse otomatik smart_wallets listesine terfi eder.

Kötü picker'lar (bot, MEV, MM) zamanla terfi edebilir; quality scorer
(smart_wallets.py:update_wallet_stats) 15+ finalize sample sonrası
düşük performansları otomatik disable eder. Yani self-cleaning.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field

from config import config
from dexscreener import DexScreener
from helius import Helius
from signal_log import SignalLog
from smart_wallets import EXCLUDED_OUTPUT_MINTS, SmartWalletStore

log = logging.getLogger(__name__)

DB_PATH = config.data_dir / "wallet_candidates.json"

# Bot/MM/exchange'lerin kıyıdaki adresleri — alıcı listesinden çıkar
SYSTEM_ADDRESSES = {
    "11111111111111111111111111111111",                # System program
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",     # SPL Token program
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",   # ATA program
    "ComputeBudget111111111111111111111111111111",     # Compute budget
}


@dataclass
class WalletCandidate:
    address: str
    first_seen_ts: float = 0.0
    last_seen_ts: float = 0.0
    # Hangi kazanan tokenlarda yakalandı (mint listesi)
    winners_seen: list[str] = field(default_factory=list)

    @property
    def hit_count(self) -> int:
        return len(set(self.winners_seen))


@dataclass
class CandidateStore:
    candidates: dict[str, WalletCandidate] = field(default_factory=dict)
    # Daha önce discovery turunda işlenmiş kazanan mint'ler — tekrar etmesin
    processed_winners: set[str] = field(default_factory=set)

    @classmethod
    def load(cls) -> "CandidateStore":
        store = cls()
        if not DB_PATH.exists():
            return store
        try:
            data = json.loads(DB_PATH.read_text())
            for c in data.get("candidates", []):
                store.candidates[c["address"]] = WalletCandidate(**c)
            store.processed_winners = set(data.get("processed_winners", []))
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            log.error("candidate store load error: %s", e)
        return store

    def save(self) -> None:
        DB_PATH.write_text(json.dumps({
            "candidates": [asdict(c) for c in self.candidates.values()],
            "processed_winners": sorted(self.processed_winners),
        }, indent=2))

    def record(self, address: str, token_mint: str) -> None:
        c = self.candidates.get(address)
        if c is None:
            c = WalletCandidate(address=address, first_seen_ts=time.time())
            self.candidates[address] = c
        c.last_seen_ts = time.time()
        if token_mint not in c.winners_seen:
            c.winners_seen.append(token_mint)


class WalletDiscovery:
    def __init__(
        self,
        helius: Helius,
        ds: DexScreener,
        signal_log: SignalLog,
        candidate_store: CandidateStore,
        smart_store: SmartWalletStore,
    ) -> None:
        self.helius = helius
        self.ds = ds
        self.signal_log = signal_log
        self.candidates = candidate_store
        self.smart = smart_store

    # -------- Winner detection --------

    def _winners(self) -> list[tuple[str, str]]:
        """signal_log'tan +X% zirve yapan, henüz işlenmemiş kazananları döner."""
        out: list[tuple[str, str]] = []
        threshold = config.discovery_winner_threshold_pct
        for s in self.signal_log.signals:
            if not s.final_24h:
                continue
            if s.peak_pct_24h < threshold:
                continue
            if s.token in self.candidates.processed_winners:
                continue
            if not s.pair:
                continue
            out.append((s.token, s.pair))
        return out

    # -------- Early buyer extraction --------

    async def _extract_early_buyers(
        self, mint: str, pair_addr: str,
    ) -> list[str]:
        """Token'ın ilk N saatindeki alıcılarını döner (wallet adres listesi)."""
        pair_data = await self.ds.pair("solana", pair_addr)
        if not pair_data:
            return []
        created_ms = pair_data.get("pairCreatedAt") or 0
        if not created_ms:
            return []
        created_ts = float(created_ms) / 1000.0
        window_end = created_ts + config.discovery_early_window_h * 3600

        # Helius mint adresine attığımız sorgu → o mint'i içeren swap tx'lerini
        # döner. limit=100 (Helius max). İlk saatte 100'den fazla buy olursa
        # ilk batch zaten yeterli sinyal — pagination yok.
        txs = await self.helius.address_transactions(
            mint, limit=100, tx_type="SWAP",
        )
        if not txs:
            return []

        buyers: list[str] = []
        seen: set[str] = set()
        for tx in txs:
            ts = float(tx.get("timestamp") or 0)
            if ts < created_ts or ts > window_end:
                continue
            events = tx.get("events") or {}
            swap = events.get("swap") or {}
            for to in (swap.get("tokenOutputs") or []):
                if to.get("mint") != mint:
                    continue
                wallet = to.get("userAccount")
                if not wallet:
                    continue
                if wallet in SYSTEM_ADDRESSES:
                    continue
                if wallet in seen:
                    continue
                seen.add(wallet)
                buyers.append(wallet)
        return buyers

    # -------- Main loop --------

    async def run_once(self) -> tuple[int, list[tuple[str, str, int]]]:
        """Bir tur discovery koş.

        Dönüş: (yeni terfi sayısı, terfi edenler [(addr, label, hit_count), ...]).
        """
        winners = self._winners()
        if not winners:
            return 0, []

        promoted: list[tuple[str, str, int]] = []
        # Her turda max N winner — API budget koruması
        for mint, pair in winners[: config.discovery_max_winners_per_run]:
            try:
                buyers = await self._extract_early_buyers(mint, pair)
            except Exception:
                log.exception("early buyers fetch failed for %s", mint[:8])
                continue

            for buyer in buyers:
                if buyer in self.smart.wallets:
                    continue  # zaten takipte
                self.candidates.record(buyer, mint)

                cand = self.candidates.candidates[buyer]
                if (
                    cand.hit_count >= config.discovery_min_winners_to_promote
                    and buyer not in self.smart.wallets
                ):
                    label = f"auto-discovered (n={cand.hit_count})"
                    if self.smart.add_wallet(buyer, label):
                        promoted.append((buyer, label, cand.hit_count))
                        log.info(
                            "auto-promoted smart wallet: %s (winners=%d)",
                            buyer[:8], cand.hit_count,
                        )

            self.candidates.processed_winners.add(mint)

        self.candidates.save()
        return len(promoted), promoted


def format_candidates_text(store: CandidateStore, top_n: int = 20) -> str:
    if not store.candidates:
        return (
            "📭 Henüz aday smart wallet yok.\n"
            "<i>Discovery, signal_log'da +"
            f"{config.discovery_winner_threshold_pct:.0f}% zirve yapan finalize "
            "sinyaller olduğunda çalışır.</i>"
        )
    threshold = config.discovery_min_winners_to_promote
    sorted_c = sorted(
        store.candidates.values(),
        key=lambda c: -c.hit_count,
    )
    lines = [
        f"🔍 <b>Wallet candidates</b> ({len(store.candidates)})",
        f"Terfi eşiği: <code>{threshold}</code> kazanan",
        "",
    ]
    for c in sorted_c[:top_n]:
        short = f"{c.address[:6]}..{c.address[-4:]}"
        bar = "✅" if c.hit_count >= threshold else "·"
        lines.append(
            f"{bar} <code>{short}</code>  "
            f"hit <code>{c.hit_count}</code>"
        )
    return "\n".join(lines)

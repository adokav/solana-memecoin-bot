"""Smart wallet tracking + wallet quality scoring.

Tutarlı kazanan cüzdanların SOL→memecoin alımlarını Helius enhanced
transactions API ile periyodik olarak çekeriz. Aynı tokene 1 saat içinde
N+ smart wallet alım yaparsa screener:
  - Skor sistemine güçlü bonus ekler (smart_signal komponenti)
  - Token DexScreener'da yoksa bile scan'e enjekte eder

Wallet quality:
  - Her alım için 24h sonra fiyat zirvesi takip edilir (data/wallet_outcomes.json)
  - Cüzdan başına ortalama peak + +30% hit rate'den quality skoru (0-100)
  - Quality < WALLET_AUTO_DISABLE_QUALITY ve n >= WALLET_AUTO_DISABLE_MIN_SAMPLES
    olan cüzdanlar otomatik disable edilir — polling'den ve smart_signal
    skorundan çıkartılır

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
from dexscreener import DexScreener
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
    # Quality tracking (finalized outcome'lardan hesaplanır)
    total_outcomes: int = 0       # 24h finalize sayısı
    avg_peak_24h: float = 0.0     # finalize'lerin ortalama zirvesi (%)
    hit_rate_30: float = 0.0      # %30+ vuran finalize oranı (0-100)
    quality_score: float = 50.0   # 0-100, neutral=50
    disabled: bool = False
    disabled_reason: str = ""


@dataclass
class SmartBuy:
    wallet: str
    label: str
    token_mint: str
    ts: float
    sol_value: float


@dataclass
class SmartSell:
    wallet: str
    label: str
    token_mint: str
    ts: float
    sol_value: float  # tahmini SOL eline geçen


@dataclass
class WalletBuyOutcome:
    """Bir smart wallet'in tek alımının 24h sonrası performansı."""
    wallet: str
    token_mint: str
    pair_address: str
    entry_price_usd: float
    entry_ts: float
    peak_price_24h: float = 0.0
    peak_pct_24h: float = 0.0
    last_check_ts: float = 0.0
    final_24h: bool = False


@dataclass
class SmartWalletStore:
    wallets: dict[str, SmartWallet] = field(default_factory=dict)
    # Bellek-içi: token_mint -> son SmartBuy'lar. Restart'ta sıfırlanır,
    # poll loop birkaç tur sonra repopulate eder.
    recent_buys: dict[str, list[SmartBuy]] = field(default_factory=dict)
    # Aynı şekilde son satışlar — exit signal için
    recent_sells: dict[str, list[SmartSell]] = field(default_factory=dict)

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

    def record_sell(self, sell: SmartSell) -> None:
        self.recent_sells.setdefault(sell.token_mint, []).append(sell)

    def cleanup_recent_sells(self) -> None:
        cutoff = time.time() - config.smart_exit_window_min * 60
        for mint in list(self.recent_sells.keys()):
            self.recent_sells[mint] = [
                s for s in self.recent_sells[mint] if s.ts > cutoff
            ]
            if not self.recent_sells[mint]:
                del self.recent_sells[mint]

    def recent_exits_for(self, token_mint: str) -> list[SmartSell]:
        """Bu token için 'full exit' kabul edilen aktif smart wallet satışları.

        Eğer wallet'in alımını recent_buys'ta görmüşsek, satışın SOL değeri
        alımın %80'i veya üstüyse → exit (partial profit-taking değil).
        Görmemişsek SMART_EXIT_MIN_SOL eşiğini geçen satışlar exit sayılır.
        """
        self.cleanup_recent_sells()
        sells = self.recent_sells.get(token_mint, [])
        if not sells:
            return []
        active = self._active_addrs()
        buys_for_token = self.recent_buys.get(token_mint, [])
        out: list[SmartSell] = []
        for sell in sells:
            if sell.wallet not in active:
                continue
            matching = next(
                (b for b in buys_for_token if b.wallet == sell.wallet), None,
            )
            if matching is None:
                if sell.sol_value >= config.smart_exit_min_sol:
                    out.append(sell)
            else:
                if sell.sol_value >= matching.sol_value * 0.8:
                    out.append(sell)
        # Aynı cüzdandan birden fazla sell varsa tekilleştir
        seen: set[str] = set()
        unique: list[SmartSell] = []
        for s in out:
            if s.wallet in seen:
                continue
            seen.add(s.wallet)
            unique.append(s)
        return unique

    def _active_addrs(self) -> set[str]:
        return {a for a, w in self.wallets.items() if not w.disabled}

    def buys_for(self, token_mint: str) -> list[SmartBuy]:
        self.cleanup_recent_buys()
        return list(self.recent_buys.get(token_mint, []))

    def unique_wallets_for(self, token_mint: str) -> int:
        active = self._active_addrs()
        buys = self.buys_for(token_mint)
        return len({b.wallet for b in buys if b.wallet in active})

    def weighted_smart_signal(self, token_mint: str) -> float:
        """Quality-weighted katkı. Her aktif cüzdan kendi quality_score/50
        ağırlığıyla katılır (Q=50 nötr → 1.0 ağırlık, Q=80 → 1.6, Q=20 → 0.4).
        """
        active = self._active_addrs()
        buys = self.buys_for(token_mint)
        seen: dict[str, float] = {}
        for b in buys:
            if b.wallet not in active:
                continue
            if b.wallet in seen:
                continue
            w = self.wallets.get(b.wallet)
            if w is None:
                continue
            seen[b.wallet] = max(0.0, min(2.0, w.quality_score / 50.0))
        return sum(seen.values())

    def tokens_with_min_buys(self, min_buys: int) -> list[str]:
        self.cleanup_recent_buys()
        active = self._active_addrs()
        out = []
        for mint, buys in self.recent_buys.items():
            unique = {b.wallet for b in buys if b.wallet in active}
            if len(unique) >= min_buys:
                out.append(mint)
        return out


# -------- Outcome store + quality scoring --------

OUTCOMES_PATH = config.data_dir / "wallet_outcomes.json"


@dataclass
class WalletOutcomeStore:
    outcomes: list[WalletBuyOutcome] = field(default_factory=list)

    @classmethod
    def load(cls) -> "WalletOutcomeStore":
        store = cls()
        if not OUTCOMES_PATH.exists():
            return store
        try:
            data = json.loads(OUTCOMES_PATH.read_text())
            for o in data.get("outcomes", []):
                store.outcomes.append(WalletBuyOutcome(**o))
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            log.error("wallet outcomes load error: %s", e)
        return store

    def save(self) -> None:
        # 30 günden eski + finalize olmuş outcome'ları kırp
        cutoff = time.time() - 30 * 86400
        self.outcomes = [
            o for o in self.outcomes
            if not o.final_24h or o.entry_ts > cutoff
        ]
        OUTCOMES_PATH.write_text(json.dumps(
            {"outcomes": [asdict(o) for o in self.outcomes]},
            indent=2,
        ))

    def add(self, o: WalletBuyOutcome) -> None:
        self.outcomes.append(o)
        self.save()

    def pending(self) -> list[WalletBuyOutcome]:
        return [o for o in self.outcomes if not o.final_24h]


def compute_wallet_quality(
    finalized: list[WalletBuyOutcome],
) -> tuple[float, float, float]:
    """(quality, avg_peak_24h, hit_rate_30) döner. finalized boşsa nötr."""
    n = len(finalized)
    if n == 0:
        return 50.0, 0.0, 0.0
    avg_peak = sum(o.peak_pct_24h for o in finalized) / n
    hits = sum(1 for o in finalized if o.peak_pct_24h >= 30)
    hit_rate = hits / n * 100
    # avg_peak 0% -> 50, 30% -> 65, 100% -> 100; -20% -> 40
    peak_component = max(0.0, min(100.0, 50.0 + avg_peak * 0.5))
    # 60% peak + 40% hit rate
    quality = round(0.6 * peak_component + 0.4 * hit_rate, 1)
    return max(0.0, min(100.0, quality)), round(avg_peak, 1), round(hit_rate, 1)


def update_wallet_stats(
    smart_store: SmartWalletStore,
    outcome_store: WalletOutcomeStore,
) -> int:
    """Her cüzdan için quality stats'ı yeniden hesapla, auto-disable kararı ver.
    Dönüş: bu turda yeni disabled edilen cüzdan sayısı.
    """
    by_wallet: dict[str, list[WalletBuyOutcome]] = {}
    for o in outcome_store.outcomes:
        if o.final_24h:
            by_wallet.setdefault(o.wallet, []).append(o)

    newly_disabled = 0
    for addr, w in smart_store.wallets.items():
        outs = by_wallet.get(addr, [])
        quality, avg_peak, hit_rate = compute_wallet_quality(outs)
        w.total_outcomes = len(outs)
        w.avg_peak_24h = avg_peak
        w.hit_rate_30 = hit_rate
        w.quality_score = quality
        if (
            not w.disabled
            and len(outs) >= config.wallet_auto_disable_min_samples
            and quality < config.wallet_auto_disable_quality
        ):
            w.disabled = True
            w.disabled_reason = (
                f"quality {quality:.0f} < {config.wallet_auto_disable_quality:.0f} "
                f"(n={len(outs)})"
            )
            log.warning("auto-disabled wallet %s: %s", addr[:8], w.disabled_reason)
            newly_disabled += 1
    smart_store._save()
    return newly_disabled


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


def _extract_sell_mint(tx: dict, owner: str) -> tuple[str | None, float]:
    """Helius parsed SWAP tx'inden: owner memecoin satıp aldığı
    SOL'u döner. (sold_mint, sol_value_received).
    """
    events = tx.get("events") or {}
    swap = events.get("swap") or {}

    sol_value = 0.0
    native_out = swap.get("nativeOutput") or {}
    if isinstance(native_out, dict):
        try:
            sol_value = float(native_out.get("amount") or 0) / 1_000_000_000
        except (TypeError, ValueError):
            sol_value = 0.0

    # Owner'dan giden ilk excluded olmayan token = sattığı memecoin
    for ti in (swap.get("tokenInputs") or []):
        if ti.get("userAccount") != owner:
            continue
        mint = ti.get("mint")
        if not mint or mint in EXCLUDED_OUTPUT_MINTS:
            continue
        return mint, sol_value

    return None, 0.0


# -------- Tracker (polling loop) --------

class SmartWalletTracker:
    def __init__(
        self,
        store: SmartWalletStore,
        helius: Helius,
        ds: DexScreener,
        outcomes: WalletOutcomeStore,
    ) -> None:
        self.store = store
        self.helius = helius
        self.ds = ds
        self.outcomes = outcomes

    async def _lookup_pair_price(self, mint: str) -> tuple[str | None, float]:
        """Token mint için en likit pair + anlık fiyatı döner."""
        try:
            pairs = await self.ds.pairs_for_token("solana", mint)
        except Exception:
            return None, 0.0
        if not pairs:
            return None, 0.0
        pairs.sort(
            key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0),
            reverse=True,
        )
        top = pairs[0]
        try:
            price = float(top.get("priceUsd") or 0)
        except (TypeError, ValueError):
            price = 0.0
        return top.get("pairAddress"), price

    async def _record_outcome(self, w: SmartWallet, mint: str, ts: float) -> None:
        pair_addr, entry_price = await self._lookup_pair_price(mint)
        if not pair_addr or entry_price <= 0:
            return  # outcome takip edemeyiz, skip
        self.outcomes.add(WalletBuyOutcome(
            wallet=w.address,
            token_mint=mint,
            pair_address=pair_addr,
            entry_price_usd=entry_price,
            entry_ts=ts,
        ))

    async def poll_wallet(self, w: SmartWallet) -> int:
        if w.disabled:
            return 0
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
                break
            if tx.get("type") != "SWAP":
                continue
            ts = float(tx.get("timestamp") or 0)
            if ts == 0 or ts < window_ts:
                continue
            # Alım?
            buy_mint, buy_sol = _extract_buy_mint(tx, w.address)
            if buy_mint:
                self.store.record_buy(SmartBuy(
                    wallet=w.address, label=w.label, token_mint=buy_mint,
                    ts=ts, sol_value=buy_sol,
                ))
                new_buys += 1
                try:
                    await self._record_outcome(w, buy_mint, ts)
                except Exception:
                    log.exception(
                        "outcome record failed %s/%s",
                        w.address[:8], buy_mint[:8],
                    )
                continue

            # Satış?
            sell_mint, sell_sol = _extract_sell_mint(tx, w.address)
            if sell_mint:
                self.store.record_sell(SmartSell(
                    wallet=w.address, label=w.label, token_mint=sell_mint,
                    ts=ts, sol_value=sell_sol,
                ))

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
            if w.disabled:
                continue
            try:
                total += await self.poll_wallet(w)
            except Exception:
                log.exception("smart wallet poll error %s", w.address[:8])
            await asyncio.sleep(0.5)
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
    total = len(store.wallets)
    active = sum(1 for w in store.wallets.values() if not w.disabled)
    lines = [f"🤖 <b>Smart Wallets</b> ({active} aktif / {total} toplam)"]
    now = time.time()
    # Aktifler önce, içlerinde quality desc; sonra disabled'lar
    sorted_wallets = sorted(
        store.wallets.values(),
        key=lambda w: (w.disabled, -w.quality_score, -w.hit_count),
    )
    for i, w in enumerate(sorted_wallets[:30], 1):
        short = f"{w.address[:6]}..{w.address[-4:]}"
        label = f" [{w.label}]" if w.label else ""
        if w.disabled:
            lines.append(
                f"✗ <code>{short}</code>{label}  "
                f"<i>DISABLED — {w.disabled_reason}</i>"
            )
            continue
        if w.total_outcomes >= 3:
            q_str = (
                f"Q<code>{w.quality_score:.0f}</code> "
                f"(n=<code>{w.total_outcomes}</code>, "
                f"avg <code>{w.avg_peak_24h:+.0f}%</code>, "
                f"hit <code>{w.hit_rate_30:.0f}%</code>)"
            )
        else:
            q_str = f"Q<code>{w.quality_score:.0f}</code> <i>(n={w.total_outcomes})</i>"
        if w.last_seen_ts > 0:
            ago_min = (now - w.last_seen_ts) / 60
            seen = f"{ago_min:.0f}dk" if ago_min < 60 else f"{ago_min/60:.1f}sa"
        else:
            seen = "—"
        lines.append(
            f"{i}. <code>{short}</code>{label}  {q_str}  ·  "
            f"hit <code>{w.hit_count}</code> · seen {seen}"
        )
    if len(store.wallets) > 30:
        lines.append(f"<i>...ve {len(store.wallets) - 30} daha</i>")
    return "\n".join(lines)

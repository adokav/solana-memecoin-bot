"""MEV / sandwich attack detection.

Her başarılı alımdan sonra:
  - Quote'ta beklenen outAmount vs. cüzdana gerçekten gelen token miktarını
    karşılaştırır.
  - Fill ratio < MEV_DETECT_FILL_THRESHOLD ise potansiyel sandwich olarak
    işaretlenir.
  - Per-DEX istatistik tutar; bir DEX'in son N alımdaki suspect oranı
    eşiği geçerse o DEX X saatliğine cooldown'a alınır (Screener cooldown'a
    geçici alır).

Sandwich tipik etki:
  - Bot ön-koşar (front-run), bizim fiyatımız kötüleşir, ardından çıkar.
  - Quote'taki priceImpact'ten daha kötü fiil
  - Slippage tolerance'ımızın tavanına dayanmış fill = klasik signature
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from config import config

log = logging.getLogger(__name__)

DB_PATH = config.data_dir / "mev_stats.json"
MAX_RECENT_PER_DEX = 50  # sliding window


@dataclass
class MevDexStats:
    dex: str
    recent_results: list[bool] = field(default_factory=list)  # True=suspect
    total_swaps: int = 0
    total_suspect: int = 0
    cooldown_until: float = 0.0  # unix ts, 0 = not cooled

    def suspect_rate_recent(self) -> float:
        if not self.recent_results:
            return 0.0
        return sum(1 for r in self.recent_results if r) / len(self.recent_results)


@dataclass
class MevStore:
    by_dex: dict[str, MevDexStats] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "MevStore":
        store = cls()
        if not DB_PATH.exists():
            return store
        try:
            data = json.loads(DB_PATH.read_text())
            for s in data.get("by_dex", []):
                stats = MevDexStats(**s)
                store.by_dex[stats.dex] = stats
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            log.error("mev store load error: %s", e)
        return store

    def save(self) -> None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        DB_PATH.write_text(json.dumps(
            {"by_dex": [asdict(s) for s in self.by_dex.values()]},
            indent=2,
        ))

    def record(self, dex: str, suspect: bool) -> MevDexStats:
        s = self.by_dex.get(dex)
        if s is None:
            s = MevDexStats(dex=dex)
            self.by_dex[dex] = s
        s.recent_results.append(suspect)
        if len(s.recent_results) > MAX_RECENT_PER_DEX:
            s.recent_results = s.recent_results[-MAX_RECENT_PER_DEX:]
        s.total_swaps += 1
        if suspect:
            s.total_suspect += 1
        # Cooldown kararı
        if (
            len(s.recent_results) >= config.mev_min_swaps_for_cooldown
            and s.suspect_rate_recent() >= config.mev_cooldown_threshold_pct / 100
        ):
            cooldown_h = config.mev_cooldown_hours
            s.cooldown_until = time.time() + cooldown_h * 3600
            log.warning(
                "MEV cooldown: dex=%s suspect_rate=%.0f%% → %s hours",
                dex, s.suspect_rate_recent() * 100, cooldown_h,
            )
        self.save()
        return s

    def is_dex_cooled(self, dex: str) -> bool:
        s = self.by_dex.get(dex)
        if s is None:
            return False
        if s.cooldown_until <= 0:
            return False
        if time.time() >= s.cooldown_until:
            s.cooldown_until = 0.0
            self.save()
            return False
        return True


def evaluate_fill(
    expected_out: int, actual_out: int, slippage_bps: int,
) -> tuple[bool, float]:
    """(suspect, fill_ratio) döner.

    suspect: actual fill quote'a göre slippage tolerance'ın da altında kaldı mı
    fill_ratio: actual / expected (0.92 = beklenenin %92'sini aldık)
    """
    if expected_out <= 0:
        return False, 0.0
    ratio = actual_out / expected_out
    # Slippage tolerance'ın hemen üstünde ya da altında dolma = klasik sandwich
    # tolerance %5 ise threshold 0.95 - extra margin
    threshold = max(
        config.mev_detect_fill_threshold,
        (10000 - slippage_bps) / 10000 - 0.01,  # tolerance limit - 1pp
    )
    return ratio < threshold, ratio


def format_mev_status(store: MevStore) -> str:
    if not store.by_dex:
        return "🛡 <b>MEV monitor</b>\nHenüz veri yok."
    lines = ["🛡 <b>MEV monitor</b>"]
    sorted_dexes = sorted(
        store.by_dex.values(),
        key=lambda s: -s.suspect_rate_recent(),
    )
    for s in sorted_dexes[:15]:
        rate_recent = s.suspect_rate_recent() * 100
        rate_total = (
            s.total_suspect / s.total_swaps * 100 if s.total_swaps > 0 else 0.0
        )
        cd_str = ""
        if s.cooldown_until > 0:
            remaining = max(0, (s.cooldown_until - time.time()) / 3600)
            cd_str = f" 🚫 cooldown {remaining:.1f}h"
        lines.append(
            f"• <code>{s.dex}</code>  "
            f"recent <code>{rate_recent:.0f}%</code> "
            f"(<code>{len(s.recent_results)}</code> swap)  "
            f"total <code>{rate_total:.0f}%</code> "
            f"(<code>{s.total_swaps}</code>){cd_str}"
        )
    return "\n".join(lines)

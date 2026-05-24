"""Thompson sampling sizing bandit.

Mevcut adaptive_sizing (sizing.py) deterministic — paper PnL'inin
ortalamasına göre çarpan seçer. Bu modül **online RL** yaklaşımı:

Her (profile, score_bucket, multiplier) için Beta(α, β) tutar.
  - α: bu arm'da win sayısı + 1
  - β: bu arm'da loss sayısı + 1
  - Karar verirken her arm'dan Beta sample alıp en yüksekini seçer
    (Thompson sampling → exploration/exploitation otomatik dengelenir)

Avantajı:
  - Yeterli sample yokken bile keşfeder (random sample yüksek çıkarsa dener)
  - Online — her trade kapandığında arm güncellenir, manuel re-train yok
  - Sample sayısı çoğaldıkça posterior daralır, kararlar daha kesin olur

Default kapalı. SIZING_BANDIT_ENABLED=true ile devreye girer.
ADAPTIVE_SIZING_ENABLED + SIZING_BANDIT_ENABLED ikisi açıksa bandit öncelik.
"""
from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import asdict, dataclass, field

from config import config
from pnl import bucket_label
from storage import Position

log = logging.getLogger(__name__)

DB_PATH = config.data_dir / "sizing_bandit.json"

# Test edilecek çarpanlar
DEFAULT_MULTIPLIERS = [0.5, 1.0, 1.5, 2.0]


@dataclass
class Arm:
    profile: str
    bucket: str
    multiplier: float
    alpha: float = 1.0     # prior + observed wins
    beta: float = 1.0      # prior + observed losses
    n_pulls: int = 0
    last_pull_ts: float = 0.0

    def sample(self, rng: random.Random) -> float:
        # Beta sample
        return rng.betavariate(self.alpha, self.beta)

    @property
    def estimated_win_rate(self) -> float:
        if self.alpha + self.beta < 0.001:
            return 0.5
        return self.alpha / (self.alpha + self.beta)


@dataclass
class BanditStore:
    arms: dict[str, Arm] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "BanditStore":
        store = cls()
        if not DB_PATH.exists():
            return store
        try:
            data = json.loads(DB_PATH.read_text())
            for a in data.get("arms", []):
                arm = Arm(**a)
                store.arms[_arm_key(arm.profile, arm.bucket, arm.multiplier)] = arm
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            log.error("sizing bandit load error: %s", e)
        return store

    def save(self) -> None:
        DB_PATH.write_text(json.dumps(
            {"arms": [asdict(a) for a in self.arms.values()]},
            indent=2,
        ))

    def ensure_arms(self, profile: str, bucket: str) -> list[Arm]:
        """Bu (profile, bucket) için tüm multiplier'lar Beta(1,1) ile var olsun."""
        arms = []
        for m in DEFAULT_MULTIPLIERS:
            key = _arm_key(profile, bucket, m)
            if key not in self.arms:
                self.arms[key] = Arm(
                    profile=profile, bucket=bucket, multiplier=m,
                )
            arms.append(self.arms[key])
        return arms

    def pick(self, profile: str, bucket: str, rng: random.Random | None = None) -> Arm:
        rng = rng or random.Random()
        arms = self.ensure_arms(profile, bucket)
        # Thompson sampling: her arm'dan sample al, en yükseğini seç
        best: tuple[float, Arm] | None = None
        for arm in arms:
            s = arm.sample(rng)
            if best is None or s > best[0]:
                best = (s, arm)
        chosen = best[1] if best else arms[0]
        chosen.n_pulls += 1
        chosen.last_pull_ts = time.time()
        self.save()
        return chosen

    def update(self, profile: str, bucket: str, multiplier: float, won: bool) -> None:
        key = _arm_key(profile, bucket, multiplier)
        arm = self.arms.get(key)
        if arm is None:
            # Init with prior + this obs
            arm = Arm(profile=profile, bucket=bucket, multiplier=multiplier)
            self.arms[key] = arm
        if won:
            arm.alpha += 1
        else:
            arm.beta += 1
        self.save()
        log.info(
            "bandit update: %s/%s/x%.1f → %s (α=%.0f β=%.0f WR=%.0f%%)",
            profile, bucket, multiplier,
            "WIN" if won else "loss",
            arm.alpha, arm.beta, arm.estimated_win_rate * 100,
        )


def _arm_key(profile: str, bucket: str, multiplier: float) -> str:
    return f"{profile}|{bucket}|{multiplier}"


def choose_sizing(
    score_total: float,
    profile: str,
    paper_positions: list[Position] | None,
    base_amount_sol: float,
    store: BanditStore,
    rng: random.Random | None = None,
) -> tuple[float, str, float]:
    """Thompson sampling ile sizing seç.

    Dönüş: (sized_sol, note, multiplier).
    """
    if not config.sizing_bandit_enabled:
        return base_amount_sol, "bandit_off", 1.0

    bucket = bucket_label(score_total)
    chosen = store.pick(profile, bucket, rng=rng)
    sized = base_amount_sol * chosen.multiplier
    note = (
        f"bandit {profile}/{bucket}: x{chosen.multiplier:.1f} "
        f"(n={chosen.n_pulls}, est WR={chosen.estimated_win_rate * 100:.0f}%)"
    )
    return sized, note, chosen.multiplier


def update_from_position(store: BanditStore, pos: Position) -> bool:
    """Closed position'dan ilgili arm'ı güncelle. True = güncelleme yapıldı."""
    if pos.status != "closed":
        return False
    if pos.sizing_multiplier is None:
        return False
    if pos.pnl_pct is None:
        # Pnl_pct hesaplanmamışsa hesapla
        if pos.sol_spent > 0:
            pnl = ((pos.sol_received_total - pos.sol_spent) / pos.sol_spent) * 100
        else:
            return False
    else:
        pnl = pos.pnl_pct
    won = pnl >= config.ml_win_threshold_pct  # ML'le aynı eşik (30%)
    bucket = bucket_label(pos.score)
    store.update(pos.profile, bucket, pos.sizing_multiplier, won)
    return True


def format_bandit_status(store: BanditStore) -> str:
    if not store.arms:
        return "🎰 <b>Sizing bandit</b>\nHenüz veri yok."
    lines = [
        f"🎰 <b>Sizing bandit</b> ({len(store.arms)} arm)",
        f"<i>Win threshold: +{config.ml_win_threshold_pct:.0f}% pnl</i>\n",
    ]
    # Profile + bucket bazında grupla
    by_pb: dict[tuple[str, str], list[Arm]] = {}
    for a in store.arms.values():
        by_pb.setdefault((a.profile, a.bucket), []).append(a)
    for (prof, bucket), arms in sorted(by_pb.items()):
        arms.sort(key=lambda a: a.multiplier)
        lines.append(f"<b>{prof} / {bucket}</b>")
        for a in arms:
            n = a.n_pulls
            wr = a.estimated_win_rate * 100
            lines.append(
                f"  ×{a.multiplier:.1f}  n=<code>{n}</code>  "
                f"WR≈<code>{wr:.0f}%</code>"
            )
        lines.append("")
    return "\n".join(lines).rstrip()

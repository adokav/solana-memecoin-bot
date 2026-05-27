"""Risk yönetimi — circuit breaker + caps tek dosyada.

Matematik temeli:
  - Daily loss cap: tek bir kötü gün portföyü gömmesin (gambler's ruin)
  - Consecutive loss: ardışık N kayıp = strateji bozulmuş, dur ve değerlendir
  - Exposure cap: variance kontrolü, max açık SOL miktarı sınırlı
  - Open positions cap: çoklu pozisyon → concurrent risk
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass

from config import config
from storage import Position

log = logging.getLogger(__name__)

DB_PATH = config.data_dir / "risk_state.json"


@dataclass
class RiskState:
    halted: bool = False
    reason: str = ""
    halted_at: float = 0.0
    halted_until: float = 0.0  # 0 = manuel resume


def _utc_day_start() -> float:
    now = time.time()
    return now - (now % 86400)


def realized_pnl_today(positions: list[Position]) -> float:
    """Bugün (UTC) kapanmış pozisyonların net SOL PnL toplamı."""
    day_start = _utc_day_start()
    return sum(
        (p.sol_received_total - p.sol_spent)
        for p in positions
        if p.status == "closed" and p.closed_at and p.closed_at >= day_start
    )


def consecutive_losses(positions: list[Position]) -> int:
    """Sondaki ardışık kayıp sayısı."""
    closed = sorted(
        [p for p in positions if p.status == "closed" and p.closed_at],
        key=lambda p: p.closed_at or 0,
    )
    count = 0
    for p in reversed(closed):
        if (p.sol_received_total - p.sol_spent) < 0:
            count += 1
        else:
            break
    return count


class Risk:
    """Tüm risk gate'lerini ve circuit breaker'ı yönetir."""

    def __init__(self) -> None:
        self.state = self._load()

    def _load(self) -> RiskState:
        if not DB_PATH.exists():
            return RiskState()
        try:
            return RiskState(**json.loads(DB_PATH.read_text()))
        except (json.JSONDecodeError, TypeError):
            return RiskState()

    def _save(self) -> None:
        DB_PATH.write_text(json.dumps(asdict(self.state), indent=2))

    def is_halted(self) -> bool:
        if not self.state.halted:
            return False
        # Timeout dolduysa otomatik aç
        if self.state.halted_until and time.time() >= self.state.halted_until:
            self.resume("auto-resume (timeout)")
            return False
        return True

    def halt(self, reason: str, until_ts: float = 0.0) -> None:
        self.state.halted = True
        self.state.reason = reason
        self.state.halted_at = time.time()
        self.state.halted_until = until_ts
        self._save()
        log.warning("RISK HALT: %s", reason)

    def resume(self, reason: str = "manual") -> None:
        if self.state.halted:
            log.info("RISK RESUME: %s (was: %s)", reason, self.state.reason)
        self.state.halted = False
        self.state.reason = ""
        self.state.halted_at = 0.0
        self.state.halted_until = 0.0
        self._save()

    def check_pre_buy(self, positions: list[Position]) -> tuple[bool, str]:
        """Yeni alımdan ÖNCE çağrılır. (allowed, reason) döner."""
        if self.is_halted():
            return False, f"halted: {self.state.reason}"
        # Açık pozisyon sayısı
        open_n = sum(1 for p in positions if p.status == "open")
        if open_n >= config.max_open_positions:
            return False, f"open positions full ({open_n}/{config.max_open_positions})"
        # Toplam exposure
        current_exp = sum(p.sol_spent for p in positions if p.status == "open")
        if current_exp + config.buy_amount_sol > config.max_total_exposure_sol:
            return False, (
                f"exposure cap: {current_exp:.4f} + {config.buy_amount_sol:.4f} > "
                f"{config.max_total_exposure_sol:.4f} SOL"
            )
        return True, "ok"

    def check_post_close(self, positions: list[Position]) -> tuple[bool, str]:
        """Pozisyon kapandıktan sonra çağrılır. Halt tetiklendiyse (True, reason)."""
        if self.state.halted:
            return False, ""
        today = realized_pnl_today(positions)
        if today <= -config.daily_loss_stop_sol:
            reason = (
                f"daily loss cap aşıldı: {today:+.4f} SOL "
                f"(limit -{config.daily_loss_stop_sol})"
            )
            # Gün sonuna kadar halt
            self.halt(reason, until_ts=_utc_day_start() + 86400)
            return True, reason
        losses = consecutive_losses(positions)
        if losses >= config.max_consecutive_losses:
            reason = (
                f"{losses} ardışık kayıp (limit {config.max_consecutive_losses})"
            )
            # Manuel resume'a kadar halt
            self.halt(reason, until_ts=0.0)
            return True, reason
        return False, ""

    def status_text(self, positions: list[Position]) -> str:
        today = realized_pnl_today(positions)
        losses = consecutive_losses(positions)
        head = (
            f"Bugün PnL: <code>{today:+.4f} SOL</code>  "
            f"(limit <code>-{config.daily_loss_stop_sol}</code>)\n"
            f"Ardışık kayıp: <code>{losses}/{config.max_consecutive_losses}</code>"
        )
        if not self.state.halted:
            return f"🟢 Risk gate: <b>açık</b>\n{head}"
        age_min = (time.time() - self.state.halted_at) / 60
        until_str = ""
        if self.state.halted_until:
            remaining_min = max(0, (self.state.halted_until - time.time()) / 60)
            until_str = f", <code>{remaining_min:.0f}dk</code> kaldı"
        else:
            until_str = ", <i>manuel /resume bekliyor</i>"
        return (
            f"🔴 Risk gate: <b>KAPALI</b>{until_str}\n"
            f"Sebep: <code>{self.state.reason}</code>\n"
            f"Açılalı <code>{age_min:.0f}dk</code>\n"
            f"{head}"
        )

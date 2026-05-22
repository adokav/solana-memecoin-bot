"""Devre kesici / killswitch.

Bot otomatik veya manuel olarak duraklatılabilir:
  - Manuel: /halt komutu
  - Otomatik: günlük PnL eşiği aşıldığında veya N ardışık kayıp geldiğinde
  - Resume: /resume komutu (otomatik halt'larda gün sonu da serbest bırakır)

Durum data/circuit_breaker.json'da kalır, restart sonrası devam eder.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass

from config import config
from storage import Position

log = logging.getLogger(__name__)

DB_PATH = config.data_dir / "circuit_breaker.json"


@dataclass
class CircuitState:
    halted: bool = False
    reason: str = ""
    halted_at: float = 0.0
    halted_until: float = 0.0  # 0 = manuel resume'a kadar


def end_of_day_ts() -> float:
    """Şu anki UTC günün sonu (sonraki gece yarısı)."""
    now = time.time()
    return now - (now % 86400) + 86400


def realized_pnl_today(positions: list[Position]) -> float:
    """Bugün (UTC) kapanmış pozisyonların net SOL PnL'i."""
    day_start = time.time() - (time.time() % 86400)
    pnl = 0.0
    for p in positions:
        if p.status != "closed":
            continue
        if not p.closed_at or p.closed_at < day_start:
            continue
        pnl += (p.sol_received_total - p.sol_spent)
    return pnl


def consecutive_losses(positions: list[Position]) -> int:
    """Sondaki ardışık kayıp sayısı."""
    closed = [p for p in positions if p.status == "closed" and p.closed_at]
    closed.sort(key=lambda p: p.closed_at or 0)
    count = 0
    for p in reversed(closed):
        pnl = (p.sol_received_total - p.sol_spent)
        if pnl < 0:
            count += 1
        else:
            break
    return count


class CircuitBreaker:
    def __init__(self) -> None:
        self.state = self._load()

    def _load(self) -> CircuitState:
        if not DB_PATH.exists():
            return CircuitState()
        try:
            data = json.loads(DB_PATH.read_text())
            return CircuitState(**data)
        except (json.JSONDecodeError, TypeError) as e:
            log.error("circuit breaker load error: %s", e)
            return CircuitState()

    def _save(self) -> None:
        DB_PATH.write_text(json.dumps(asdict(self.state), indent=2))

    def is_open(self) -> bool:
        """True ise yeni alım engelli."""
        if not self.state.halted:
            return False
        if self.state.halted_until and time.time() >= self.state.halted_until:
            self.resume("auto-resume (timeout expired)")
            return False
        return True

    def halt(self, reason: str, until_ts: float = 0.0) -> None:
        self.state.halted = True
        self.state.reason = reason
        self.state.halted_at = time.time()
        self.state.halted_until = until_ts
        self._save()
        log.warning("circuit breaker HALT: %s", reason)

    def resume(self, reason: str = "manual") -> None:
        if self.state.halted:
            log.info("circuit breaker RESUME: %s (was: %s)", reason, self.state.reason)
        self.state.halted = False
        self.state.reason = ""
        self.state.halted_at = 0.0
        self.state.halted_until = 0.0
        self._save()

    def check_post_close(self, positions: list[Position]) -> tuple[bool, str]:
        """Pozisyon kapandıktan sonra otomatik halt koşullarını değerlendir.

        Dönüş: (halted_now, reason). Zaten halted ise (False, "") döner.
        """
        if self.state.halted:
            return False, ""

        today = realized_pnl_today(positions)
        if today <= -config.daily_loss_stop_sol:
            reason = (
                f"günlük kayıp limiti aşıldı: {today:+.4f} SOL "
                f"(limit -{config.daily_loss_stop_sol})"
            )
            self.halt(reason, until_ts=end_of_day_ts())
            return True, reason

        losses = consecutive_losses(positions)
        if losses >= config.max_consecutive_losses:
            reason = f"{losses} ardışık kayıp (limit {config.max_consecutive_losses})"
            self.halt(reason, until_ts=0.0)
            return True, reason

        return False, ""

    def status_text(self, positions: list[Position] | None = None) -> str:
        today = realized_pnl_today(positions) if positions else 0.0
        losses = consecutive_losses(positions) if positions else 0
        head = (
            f"Bugün PnL: <code>{today:+.4f} SOL</code>  "
            f"(limit <code>-{config.daily_loss_stop_sol}</code>)\n"
            f"Ardışık kayıp: <code>{losses}/{config.max_consecutive_losses}</code>"
        )
        if not self.state.halted:
            return f"🟢 Devre kesici: <b>kapalı</b>\n{head}"
        age_min = (time.time() - self.state.halted_at) / 60
        if self.state.halted_until:
            remaining_min = max(0, (self.state.halted_until - time.time()) / 60)
            until = f", <code>{remaining_min:.0f}dk</code> kaldı"
        else:
            until = ", <i>manuel /resume bekliyor</i>"
        return (
            f"🔴 Devre kesici: <b>AÇIK</b>{until}\n"
            f"Sebep: <code>{self.state.reason}</code>\n"
            f"Açılalı <code>{age_min:.0f}dk</code>\n"
            f"{head}"
        )

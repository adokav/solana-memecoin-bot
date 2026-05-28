"""Small JSON storage for alert-only operation."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from config import config

log = logging.getLogger(__name__)
DB_PATH = config.data_dir / "alerts.json"


@dataclass
class WatchedToken:
    pair_address: str
    base_token: str
    symbol: str
    url: str
    first_price_usd: float
    peak_price_usd: float
    first_liquidity_usd: float
    last_price_usd: float
    last_liquidity_usd: float
    alerted_at: float
    last_checked_at: float = 0.0
    warned: bool = False
    ignored: bool = False

    # Radar V6 watch-state fields. Defaults keep old alerts.json compatible.
    first_buy_ratio: float = 0.0          # fraction 0..1
    first_volume_liq_ratio: float = 0.0
    first_h1: float = 0.0
    last_buy_ratio: float = 0.0
    last_volume_liq_ratio: float = 0.0
    last_h1: float = 0.0
    mode: str = "UNKNOWN"
    opportunity_score: int = 0
    risk_score: int = 0
    exit_score: int = 0
    last_strength_alert_at: float = 0.0
    last_break_alert_at: float = 0.0


@dataclass
class AlertEvent:
    ts: float
    symbol: str
    base_token: str
    pair_address: str
    opportunity_score: int
    risk_score: int
    exit_score: int = 0
    mode: str = "UNKNOWN"


@dataclass
class Store:
    watched: list[WatchedToken] = field(default_factory=list)
    alerts: list[AlertEvent] = field(default_factory=list)

    @classmethod
    def load(cls) -> "Store":
        if not DB_PATH.exists():
            return cls()
        try:
            raw = json.loads(DB_PATH.read_text())
            watched: list[WatchedToken] = []
            for x in raw.get("watched", []):
                # Backward-compatible load: ignore unknown keys, fill missing defaults.
                allowed = WatchedToken.__dataclass_fields__
                clean = {k: v for k, v in x.items() if k in allowed}
                watched.append(WatchedToken(**clean))
            alerts = []
            for x in raw.get("alerts", []):
                allowed = AlertEvent.__dataclass_fields__
                clean = {k: v for k, v in x.items() if k in allowed}
                alerts.append(AlertEvent(**clean))
            return cls(watched=watched, alerts=alerts)
        except Exception as e:
            log.error("storage load error: %s", e)
            return cls()

    def save(self) -> None:
        DB_PATH.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False))

    def add_alert(self, event: AlertEvent) -> None:
        self.alerts.append(event)
        self.alerts = self.alerts[-500:]
        self.save()

    def upsert_watch(self, token: WatchedToken) -> None:
        for i, old in enumerate(self.watched):
            if old.base_token == token.base_token:
                # Preserve already-triggered alert timestamps to avoid spam.
                token.last_strength_alert_at = old.last_strength_alert_at
                token.last_break_alert_at = old.last_break_alert_at
                token.warned = old.warned
                token.ignored = old.ignored
                token.peak_price_usd = max(old.peak_price_usd, token.peak_price_usd)
                self.watched[i] = token
                self.save()
                return
        self.watched.append(token)
        self.save()

    def find_watch(self, token_mint: str) -> WatchedToken | None:
        return next((w for w in self.watched if w.base_token == token_mint), None)

    def ignore(self, token_mint: str) -> bool:
        item = self.find_watch(token_mint)
        if not item:
            return False
        item.ignored = True
        self.save()
        return True

    def active_watches(self) -> list[WatchedToken]:
        max_age = config.watch_ttl_hours * 3600
        now = time.time()
        return [
            w for w in self.watched
            if not w.ignored and (now - w.alerted_at) <= max_age
        ]

    def status_text(self) -> str:
        active = self.active_watches()
        early = sum(1 for a in self.alerts if getattr(a, "mode", "") == "EARLY WATCH")
        confirmed = sum(1 for a in self.alerts if getattr(a, "mode", "") == "CONFIRMED SIGNAL")
        lines = [
            "📡 <b>Memecoin radar durumu</b>",
            f"Aktif izleme: <code>{len(active)}</code>",
            f"Toplam radar bildirimi: <code>{len(self.alerts)}</code>",
            f"Early/Alınabilir: <code>{early}/{confirmed}</code>",
            f"Otomatik alım: <b>kapalı</b>",
        ]
        for w in active[:8]:
            dd = ((w.peak_price_usd - w.last_price_usd) / max(w.peak_price_usd, 1e-12)) * 100
            icon = "🟢" if w.mode == "CONFIRMED SIGNAL" else "🟡"
            lines.append(
                f"{icon} ${w.symbol} "
                f"O:<code>{w.opportunity_score}</code> R:<code>{w.risk_score}</code> X:<code>{w.exit_score}</code> "
                f"DD:<code>{dd:.1f}%</code>"
            )
        return "\n".join(lines)

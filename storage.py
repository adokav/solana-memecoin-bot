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


@dataclass
class AlertEvent:
    ts: float
    symbol: str
    base_token: str
    pair_address: str
    opportunity_score: int
    risk_score: int


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
            watched = [WatchedToken(**x) for x in raw.get("watched", [])]
            alerts = [AlertEvent(**x) for x in raw.get("alerts", [])]
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
        lines = [
            "📡 <b>Alert-only bot durumu</b>",
            f"Aktif izleme: <code>{len(active)}</code>",
            f"Toplam alert: <code>{len(self.alerts)}</code>",
            f"Otomatik alım: <b>kapalı</b>",
        ]
        for w in active[:10]:
            dd = ((w.peak_price_usd - w.last_price_usd) / max(w.peak_price_usd, 1e-12)) * 100
            lines.append(
                f"• ${w.symbol} price=<code>${w.last_price_usd:.8f}</code> "
                f"peak DD=<code>{dd:.1f}%</code>"
            )
        return "\n".join(lines)

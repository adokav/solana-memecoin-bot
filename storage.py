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
    survival_score: int = 0
    expansion_score: int = 0
    timing_score: int = 0
    confidence_score: int = 0
    edge_score: int = 0
    radar_score: int = 0
    decision: str = "İZLE"
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
    survival_score: int = 0
    expansion_score: int = 0
    timing_score: int = 0
    confidence_score: int = 0
    edge_score: int = 0
    radar_score: int = 0
    decision: str = "İZLE"
    mode: str = "UNKNOWN"


@dataclass
class PositionRecord:
    token_mint: str
    symbol: str = "?"
    opened_at: float = 0.0
    entry_sol: float = 0.0
    entry_token_raw: int = 0
    buy_sig: str = ""
    closed_at: float = 0.0
    exit_sol: float = 0.0
    sell_sig: str = ""
    status: str = "open"  # open/closed/manual


@dataclass
class Store:
    watched: list[WatchedToken] = field(default_factory=list)
    alerts: list[AlertEvent] = field(default_factory=list)
    positions: list[PositionRecord] = field(default_factory=list)

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
            positions = []
            for x in raw.get("positions", []):
                allowed = PositionRecord.__dataclass_fields__
                clean = {k: v for k, v in x.items() if k in allowed}
                positions.append(PositionRecord(**clean))
            return cls(watched=watched, alerts=alerts, positions=positions)
        except Exception as e:
            log.error("storage load error: %s", e)
            return cls()

    def save(self) -> None:
        DB_PATH.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False))

    def add_alert(self, event: AlertEvent) -> None:
        self.alerts.append(event)
        self.alerts = self.alerts[-500:]
        self.save()

    def record_buy(self, token_mint: str, symbol: str, entry_sol: float, entry_token_raw: int, buy_sig: str = "") -> None:
        """Record a bot-executed buy so close reports can compute PnL."""
        self.positions.append(PositionRecord(
            token_mint=token_mint,
            symbol=symbol or "?",
            opened_at=time.time(),
            entry_sol=float(entry_sol or 0.0),
            entry_token_raw=int(entry_token_raw or 0),
            buy_sig=buy_sig or "",
            status="open",
        ))
        self.positions = self.positions[-500:]
        self.save()

    def latest_open_position(self, token_mint: str) -> PositionRecord | None:
        for p in reversed(self.positions):
            if p.token_mint == token_mint and p.status == "open":
                return p
        return None

    def record_close(self, token_mint: str, exit_sol: float, sell_sig: str = "") -> PositionRecord | None:
        p = self.latest_open_position(token_mint)
        if p:
            p.closed_at = time.time()
            p.exit_sol = float(exit_sol or 0.0)
            p.sell_sig = sell_sig or ""
            p.status = "closed"
            self.save()
        return p

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
                f"Edge:<code>{getattr(w, 'edge_score', 0)}</code> Conf:<code>{getattr(w, 'confidence_score', 0)}</code> "
                f"S:<code>{getattr(w, 'survival_score', 0)}</code> X:<code>{w.exit_score}</code> "
                f"DD:<code>{dd:.1f}%</code>"
            )
        return "\n".join(lines)

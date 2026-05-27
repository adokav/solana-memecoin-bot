"""Position storage. JSON disk persist.

Lean: sadece gerekli alanlar. Eski JSON dosyalarındaki extra field'lar
forward-compat ile sessizce ignore edilir.
"""
import json
import logging
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Optional

from config import config

log = logging.getLogger(__name__)

DB_PATH = config.data_dir / "positions.json"


@dataclass
class TpHit:
    level: int
    trigger_pct: float
    sold_pct: float
    sold_amount_raw: int
    sol_received: float
    tx_sig: str
    ts: float


@dataclass
class PyramidAdd:
    pct_at_add: float
    price_usd: float
    amount_raw: int
    sol_spent: float
    tx_sig: str
    ts: float


@dataclass
class Position:
    pair_address: str
    base_token: str
    symbol: str
    entry_price_usd: float           # add'lerden sonra blended
    peak_price_usd: float
    amount_raw: int                  # alımda alınan toplam (add ile artar)
    remaining_raw: int               # partial sell'lerden sonra kalan
    sol_spent: float                 # toplam harcanan (add dahil)
    sol_received_total: float = 0.0
    opened_at: float = 0.0
    tx_open: str = ""
    tp_hits: list[TpHit] = field(default_factory=list)
    breakeven_armed: bool = False    # TP1 sonrası SL → 0%
    status: str = "open"             # open | closed
    closed_at: Optional[float] = None
    pnl_pct: Optional[float] = None
    close_reason: Optional[str] = None
    # Pyramid (anti-martingale)
    original_entry_price_usd: Optional[float] = None  # pyramid trigger için referans
    pyramid_adds: list[PyramidAdd] = field(default_factory=list)
    # Hold-time safety: entry'deki likidite snapshot (drain check için)
    entry_liquidity_usd: Optional[float] = None


_KNOWN_FIELDS = {f.name for f in fields(Position)}


@dataclass
class Store:
    positions: list[Position] = field(default_factory=list)

    @classmethod
    def load(cls) -> "Store":
        if not DB_PATH.exists():
            return cls()
        try:
            data = json.loads(DB_PATH.read_text())
            positions = []
            for p in data.get("positions", []):
                # Forward-compat: eski JSON'larda olabilecek extra field'ları ignore et
                tp_hits_raw = p.pop("tp_hits", [])
                pyr_raw = p.pop("pyramid_adds", [])
                p_filtered = {k: v for k, v in p.items() if k in _KNOWN_FIELDS}
                tp_hits = [TpHit(**h) for h in tp_hits_raw]
                pyramid_adds = [PyramidAdd(**a) for a in pyr_raw]
                positions.append(Position(
                    tp_hits=tp_hits, pyramid_adds=pyramid_adds, **p_filtered,
                ))
            return cls(positions=positions)
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            log.error("storage load error: %s", e)
            return cls()

    def save(self) -> None:
        DB_PATH.write_text(json.dumps(
            {"positions": [asdict(p) for p in self.positions]},
            indent=2,
        ))

    def open_positions(self) -> list[Position]:
        return [p for p in self.positions if p.status == "open"]

    def find_by_pair(self, pair: str) -> Optional[Position]:
        return next(
            (p for p in self.positions if p.pair_address == pair and p.status == "open"),
            None,
        )

    def add(self, pos: Position) -> None:
        self.positions.append(pos)
        self.save()

    def update(self) -> None:
        self.save()

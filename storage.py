"""Pozisyon storage. Render Disk ile kalıcı (DATA_DIR=/data)."""
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from config import config

log = logging.getLogger(__name__)

DB_PATH = config.data_dir / "positions.json"


@dataclass
class TpHit:
    level: int           # 1, 2, 3
    trigger_pct: float
    sold_pct: float
    sold_amount_raw: int
    sol_received: float
    tx_sig: str
    ts: float


@dataclass
class PyramidAdd:
    pct_at_add: float    # orijinal entry'e göre fiyat değişim % (add anında)
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
    entry_price_usd: float           # blended ortalama (add'lerden sonra güncellenir)
    peak_price_usd: float
    amount_raw: int          # alımda alınan toplam token (raw) — add ile artar
    remaining_raw: int       # kademeli satışlardan sonra kalan
    sol_spent: float         # toplam harcanan SOL (add'ler dahil)
    sol_received_total: float = 0.0  # şu ana kadar kapatılan kısmın geliri
    opened_at: float = 0.0
    tx_open: str = ""
    profile: str = "early"
    score: float = 0.0
    tp_hits: list[TpHit] = field(default_factory=list)
    breakeven_armed: bool = False  # TP1 sonrası SL breakeven'a çekildi mi
    status: str = "open"     # open | closed
    closed_at: Optional[float] = None
    pnl_pct: Optional[float] = None
    close_reason: Optional[str] = None
    # Pyramid / DCA — opsiyonel, eski pozisyonlarda yok
    original_entry_price_usd: Optional[float] = None  # add tetiği için referans
    pyramid_adds: list[PyramidAdd] = field(default_factory=list)


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
                tp_hits = [TpHit(**h) for h in p.pop("tp_hits", [])]
                pyramid_adds = [PyramidAdd(**a) for a in p.pop("pyramid_adds", [])]
                positions.append(Position(
                    tp_hits=tp_hits, pyramid_adds=pyramid_adds, **p,
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

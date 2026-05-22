"""Paper trading: her alert için sanal pozisyon aç, monitor mantığıyla kapat.

Gerçek para riske atmadan kendi stratejinin istatistiğini biriktir.
Real positions Store'dan ayrı tutulur (paper_positions.json), aynı /pnl
agregasyonu kullanır.

Fill modeli:
  - Entry: DexScreener fiyatı + buy_slippage_bps kötüleştirme
  - TP/SL exit: DexScreener fiyatı + sell_slippage_bps kötüleştirme
  - Jupiter çağrısı YOK, gerçek fee/MEV yansıtılmaz; muhafazakar tahmin
    için slippage'ı bilerek yüksek tutuyoruz.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from config import config
from dexscreener import DexScreener
from rugcheck import SafetyReport
from screener import Candidate
from storage import Position, PyramidAdd, TpHit

log = logging.getLogger(__name__)

DB_PATH = config.data_dir / "paper_positions.json"


@dataclass
class PaperStore:
    positions: list[Position] = field(default_factory=list)

    @classmethod
    def load(cls) -> "PaperStore":
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
            log.error("paper store load error: %s", e)
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

    def open(self, c: Candidate, safety: SafetyReport, sol_amount: float) -> Position | None:
        if self.find_by_pair(c.pair_address):
            return None
        if c.price_usd <= 0:
            return None
        slip_bps = config.buy_slippage_bps
        entry_price = c.price_usd * (1 + slip_bps / 10000)
        # Nominal token miktarı — sadece oran hesabı için kullanılır,
        # decimals'a bağımlı olmadan satışlarda fraction çıkarmak yeterli.
        nominal_tokens = int(sol_amount * 1_000_000 / max(entry_price, 1e-12))
        if nominal_tokens <= 0:
            nominal_tokens = 1_000_000
        pos = Position(
            pair_address=c.pair_address,
            base_token=c.base_token,
            symbol=c.base_symbol,
            entry_price_usd=entry_price,
            peak_price_usd=entry_price,
            amount_raw=nominal_tokens,
            remaining_raw=nominal_tokens,
            sol_spent=sol_amount,
            opened_at=time.time(),
            tx_open="paper",
            profile=c.profile,
            score=c.score + safety.score,
            original_entry_price_usd=entry_price,
        )
        self.positions.append(pos)
        self.save()
        return pos


class PaperMonitor:
    """Real Monitor'ın paper karşılığı. Jupiter çağırmaz, DS fiyatından
    sanal fill yapar; tetikler ve eşikler bire bir monitor.py ile aynı.
    """

    def __init__(self, ds: DexScreener, store: PaperStore) -> None:
        self.ds = ds
        self.store = store

    def _sim_sell_sol(self, pos: Position, sell_amount: int, price: float) -> float:
        """Simüle satıştan dönen SOL — entry fiyatına göre oran + slippage."""
        if pos.amount_raw <= 0 or pos.entry_price_usd <= 0 or pos.sol_spent <= 0:
            return 0.0
        slip_bps = config.sell_slippage_bps
        eff_price = price * (1 - slip_bps / 10000)
        if eff_price <= 0:
            return 0.0
        fraction = sell_amount / pos.amount_raw
        return pos.sol_spent * fraction * (eff_price / pos.entry_price_usd)

    def _partial_sell(
        self, pos: Position, level: int, trigger_pct: float, sell_pct: float, price: float
    ) -> None:
        sell_amount = int(pos.remaining_raw * (sell_pct / 100))
        if sell_amount <= 0:
            return
        sol_received = self._sim_sell_sol(pos, sell_amount, price)
        pos.remaining_raw -= sell_amount
        pos.sol_received_total += sol_received
        pos.tp_hits.append(TpHit(
            level=level, trigger_pct=trigger_pct, sold_pct=sell_pct,
            sold_amount_raw=sell_amount, sol_received=sol_received,
            tx_sig="paper", ts=time.time(),
        ))
        if level == 1 and config.breakeven_after_tp1:
            pos.breakeven_armed = True
        self.store.save()
        log.info(
            "PAPER TP%d %s +%.0f%% sol_in=%.4f",
            level, pos.symbol, trigger_pct, sol_received,
        )

    def _close_all(self, pos: Position, price: float, reason: str) -> None:
        if pos.remaining_raw > 0:
            sol_received = self._sim_sell_sol(pos, pos.remaining_raw, price)
            pos.sol_received_total += sol_received
            pos.remaining_raw = 0
        pnl_pct = (
            (pos.sol_received_total - pos.sol_spent) / pos.sol_spent * 100
            if pos.sol_spent else 0
        )
        pos.pnl_pct = pnl_pct
        pos.status = "closed"
        pos.closed_at = time.time()
        pos.close_reason = reason
        self.store.save()
        log.info(
            "PAPER CLOSE %s pnl=%+.1f%% reason=%s",
            pos.symbol, pnl_pct, reason,
        )

    def _sim_pyramid(self, pos: Position, price: float) -> bool:
        """Paper karşılığı pyramid add. True dönerse tick'i kıs."""
        if not config.pyramid_enabled:
            return False
        hit = {h.level for h in pos.tp_hits}
        if 1 not in hit:
            return False
        if len(pos.pyramid_adds) >= config.pyramid_max_adds:
            return False
        orig = pos.original_entry_price_usd or pos.entry_price_usd
        if orig <= 0:
            return False
        pnl_orig = (price - orig) / orig * 100
        next_idx = len(pos.pyramid_adds)
        trigger_pct = config.tp1_trigger + (next_idx + 1) * config.pyramid_trigger_step_pct
        if pnl_orig < trigger_pct:
            return False

        add_sol = config.buy_amount_sol * config.pyramid_size_ratio
        # Paper'da slippage uygula
        slip_bps = config.buy_slippage_bps
        eff_price = price * (1 + slip_bps / 10000)
        tokens_added = int(add_sol * 1_000_000 / max(eff_price, 1e-12))
        if tokens_added <= 0:
            return False

        old_basis = pos.amount_raw * pos.entry_price_usd
        add_basis = tokens_added * eff_price
        new_amount = pos.amount_raw + tokens_added
        new_entry = (
            (old_basis + add_basis) / new_amount if new_amount > 0 else pos.entry_price_usd
        )

        pos.amount_raw = new_amount
        pos.remaining_raw += tokens_added
        pos.sol_spent += add_sol
        pos.entry_price_usd = new_entry
        pos.peak_price_usd = price
        pos.pyramid_adds.append(PyramidAdd(
            pct_at_add=pnl_orig,
            price_usd=eff_price,
            amount_raw=tokens_added,
            sol_spent=add_sol,
            tx_sig="paper",
            ts=time.time(),
        ))
        self.store.save()
        log.info(
            "PAPER PYRAMID#%d %s +%.0f%% add=%.4f SOL new_entry=$%.8f",
            len(pos.pyramid_adds), pos.symbol, pnl_orig, add_sol, new_entry,
        )
        return True

    async def _tick_one(self, pos: Position) -> None:
        if pos.remaining_raw <= 0:
            pos.status = "closed"
            self.store.save()
            return
        pair = await self.ds.pair("solana", pos.pair_address)
        if not pair:
            return
        try:
            price = float(pair.get("priceUsd") or 0)
        except (TypeError, ValueError):
            return
        if price <= 0:
            return

        if price > pos.peak_price_usd:
            pos.peak_price_usd = price
            self.store.save()

        pnl_pct = ((price - pos.entry_price_usd) / pos.entry_price_usd) * 100
        drawdown = ((pos.peak_price_usd - price) / pos.peak_price_usd) * 100
        hit = {h.level for h in pos.tp_hits}

        # Pyramid (TP1 sonrası, TP3 öncesi)
        if 1 in hit and 3 not in hit:
            if self._sim_pyramid(pos, price):
                return

        if 3 not in hit and pnl_pct >= config.tp3_trigger:
            self._partial_sell(pos, 3, config.tp3_trigger, config.tp3_sell, price)
            return
        if 2 not in hit and pnl_pct >= config.tp2_trigger:
            self._partial_sell(pos, 2, config.tp2_trigger, config.tp2_sell, price)
            return
        if 1 not in hit and pnl_pct >= config.tp1_trigger:
            self._partial_sell(pos, 1, config.tp1_trigger, config.tp1_sell, price)
            return
        if pos.tp_hits and drawdown >= config.trailing_stop:
            self._close_all(pos, price, f"trailing -{drawdown:.1f}% from peak")
            return
        if pos.breakeven_armed and pnl_pct <= 0:
            self._close_all(pos, price, "breakeven SL")
            return
        if pnl_pct <= -config.stop_loss:
            self._close_all(pos, price, f"SL {pnl_pct:.1f}%")
            return

    async def tick(self) -> None:
        for pos in list(self.store.open_positions()):
            try:
                await self._tick_one(pos)
            except Exception:
                log.exception("paper monitor tick error for %s", pos.symbol)

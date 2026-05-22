"""Sinyal performans logu.

Her alert'i kaydeder; periyodik olarak DexScreener'dan fiyat çekip
+1h ve +24h zirve performansını günceller. Threshold optimizasyonu için
ham veri sağlar.
"""
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from config import config

log = logging.getLogger(__name__)

DB_PATH = config.data_dir / "signals.json"


@dataclass
class LoggedSignal:
    ts: float
    token: str
    pair: str
    symbol: str
    profile: str
    entry_price_usd: float
    score: float
    safety_score: float
    score_breakdown: dict = field(default_factory=dict)
    peak_price_1h: float = 0.0
    peak_pct_1h: float = 0.0
    peak_price_24h: float = 0.0
    peak_pct_24h: float = 0.0
    last_check_ts: float = 0.0
    final_24h: bool = False
    # Sinyal anındaki makro snapshot (analog regime backtest için)
    macro: dict = field(default_factory=dict)


class SignalLog:
    def __init__(self) -> None:
        self.signals: list[LoggedSignal] = self._load()

    def _load(self) -> list[LoggedSignal]:
        if not DB_PATH.exists():
            return []
        try:
            data = json.loads(DB_PATH.read_text())
            return [LoggedSignal(**s) for s in data]
        except (json.JSONDecodeError, TypeError) as e:
            log.error("signal log load error: %s", e)
            return []

    def _save(self) -> None:
        DB_PATH.write_text(json.dumps([asdict(s) for s in self.signals], indent=2))

    def add(
        self,
        token: str,
        pair: str,
        symbol: str,
        profile: str,
        entry_price_usd: float,
        score: float,
        safety_score: float,
        score_breakdown: dict,
        macro: dict | None = None,
    ) -> None:
        self.signals.append(LoggedSignal(
            ts=time.time(),
            token=token,
            pair=pair,
            symbol=symbol,
            profile=profile,
            entry_price_usd=entry_price_usd,
            score=score,
            safety_score=safety_score,
            score_breakdown=dict(score_breakdown),
            macro=dict(macro) if macro else {},
        ))
        self._save()

    def pending(self) -> list[LoggedSignal]:
        return [s for s in self.signals if not s.final_24h]

    def update_with_price(self, sig: LoggedSignal, price_now: float) -> None:
        if sig.entry_price_usd <= 0 or price_now <= 0:
            return
        pct = (price_now - sig.entry_price_usd) / sig.entry_price_usd * 100
        age_h = (time.time() - sig.ts) / 3600

        if age_h <= 1 and pct > sig.peak_pct_1h:
            sig.peak_pct_1h = pct
            sig.peak_price_1h = price_now
        if age_h <= 24 and pct > sig.peak_pct_24h:
            sig.peak_pct_24h = pct
            sig.peak_price_24h = price_now
        if age_h > 24:
            sig.final_24h = True
        sig.last_check_ts = time.time()

    def save(self) -> None:
        self._save()

    def stats(self) -> dict:
        """Toplu performans özeti — finalize olmuş sinyaller üzerinden."""
        finalized = [s for s in self.signals if s.final_24h]
        if not finalized:
            return {"total": 0}

        def pct_above(threshold: float) -> float:
            return sum(1 for s in finalized if s.peak_pct_24h >= threshold) / len(finalized) * 100

        return {
            "total": len(finalized),
            "avg_peak_1h": round(sum(s.peak_pct_1h for s in finalized) / len(finalized), 1),
            "avg_peak_24h": round(sum(s.peak_pct_24h for s in finalized) / len(finalized), 1),
            "hit_rate_30pct_24h": round(pct_above(30), 1),
            "hit_rate_100pct_24h": round(pct_above(100), 1),
            "pending": len(self.pending()),
        }

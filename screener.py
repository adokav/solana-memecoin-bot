"""Screener — aday mint'leri toplar ve hard filter uygular.

Akış:
  1. Kaynaklardan mint listesi al.
  2. Her mint için DexScreener pair fetch yap.
  3. Candidate parse.
  4. filter.passes() hard gate.
  5. Pass eden adaylar safety + Telegram radar katmanına gider.
"""
from __future__ import annotations

import html
import logging
import time
from dataclasses import dataclass, field

from candidate import Candidate, parse as parse_candidate
from config import config
from dexscreener import DexScreener
from filter import passes as filter_passes
from pumpfun import PumpFun

log = logging.getLogger(__name__)


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _snapshot_passed_candidate(c: Candidate) -> PassedCandidateSnapshot:
    """Create a human-readable scan snapshot without doing network safety checks."""
    from opportunity import score as opportunity_score

    op = opportunity_score(c, "hard filter ok")
    liq = float(c.liquidity_usd or 0)
    tx = int(c.txns_h1 or 0)
    buy_ratio_pct = (c.buys_h1 / max(tx, 1)) * 100.0
    vol_liq = float(c.volume_h1 or 0) / max(liq, 1.0)
    return PassedCandidateSnapshot(
        symbol=c.base_symbol or "?",
        mint=c.base_token,
        pair_address=c.pair_address,
        url=c.url,
        mode=op.mode,
        opportunity_score=op.opportunity_score,
        risk_score=op.risk_score,
        exit_score=op.exit_score,
        liquidity_usd=liq,
        volume_h1=float(c.volume_h1 or 0),
        volume_liq_ratio=vol_liq,
        buy_ratio_pct=buy_ratio_pct,
        txns_h1=tx,
        sells_h1=int(c.sells_h1 or 0),
        h1=float(c.price_change_h1 or 0),
        h6=float(c.price_change_h6 or 0),
        age_min=float(c.pair_age_h or 0) * 60.0,
        reasons=op.reasons[:4],
        cautions=op.cautions[:3],
    )




@dataclass
class PassedCandidateSnapshot:
    """Lightweight candidate snapshot for /scan_stats."""
    symbol: str
    mint: str
    pair_address: str
    url: str
    mode: str
    opportunity_score: int
    risk_score: int
    exit_score: int
    liquidity_usd: float
    volume_h1: float
    volume_liq_ratio: float
    buy_ratio_pct: float
    txns_h1: int
    sells_h1: int
    h1: float
    h6: float
    age_min: float
    reasons: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)

@dataclass
class ScanResult:
    """Diagnostic — /scan_stats için."""
    ts: float = 0.0
    src_ds_profiles: int = 0
    src_ds_boosted: int = 0
    src_ds_top: int = 0
    src_pump: int = 0
    unique_mints: int = 0
    on_cooldown: int = 0
    fetched: int = 0
    no_pairs: int = 0
    parse_fail: int = 0
    filter_fail: int = 0
    passed: int = 0
    sample_filter_reasons: list[str] = field(default_factory=list)
    passed_candidates: list[PassedCandidateSnapshot] = field(default_factory=list)


class Screener:
    def __init__(self, ds: DexScreener, pf: PumpFun) -> None:
        self.ds = ds
        self.pf = pf
        self._cooldown: dict[str, tuple[float, bool]] = {}
        self._history: list[ScanResult] = []

    def _cooldown_hours(self, passed: bool) -> float:
        return config.cooldown_hours_pass if passed else config.cooldown_hours_reject

    def _on_cooldown(self, token: str) -> bool:
        e = self._cooldown.get(token)
        if not e:
            return False
        ts, passed = e
        return (time.time() - ts) < (self._cooldown_hours(passed) * 3600)

    def mark_seen(self, token: str, passed: bool) -> None:
        self._cooldown[token] = (time.time(), passed)

    async def scan(self) -> tuple[list[Candidate], ScanResult]:
        result = ScanResult(ts=time.time())

        src_profiles = await self.ds.latest_profiles()
        src_latest = await self.ds.latest_boosted()
        src_top = await self.ds.top_boosted()
        src_pump = await self.pf.recently_graduated()

        result.src_ds_profiles = len(src_profiles)
        result.src_ds_boosted = len(src_latest)
        result.src_ds_top = len(src_top)
        result.src_pump = len(src_pump)

        seen: set[str] = set()
        mints: list[str] = []

        for item in src_profiles + src_latest + src_top:
            if not isinstance(item, dict):
                continue
            if item.get("chainId") != "solana":
                continue
            addr = item.get("tokenAddress")
            if not addr or addr in seen:
                continue
            seen.add(addr)
            if self._on_cooldown(addr):
                result.on_cooldown += 1
            else:
                mints.append(addr)

        for addr in src_pump:
            if not addr or addr in seen:
                continue
            seen.add(addr)
            if self._on_cooldown(addr):
                result.on_cooldown += 1
            else:
                mints.append(addr)

        result.unique_mints = len(seen)
        result.fetched = min(len(mints), config.max_mints_per_scan)

        candidates: list[Candidate] = []
        for mint in mints[:config.max_mints_per_scan]:
            pairs = await self.ds.pairs_for_token("solana", mint)
            if not pairs:
                result.no_pairs += 1
                continue

            pairs.sort(
                key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0),
                reverse=True,
            )
            c = parse_candidate(pairs[0])
            if c is None:
                result.parse_fail += 1
                continue

            ok, reason = filter_passes(c)
            if not ok:
                result.filter_fail += 1
                if len(result.sample_filter_reasons) < 5:
                    result.sample_filter_reasons.append(f"{c.base_symbol}: {reason}")
                self.mark_seen(c.base_token, passed=False)
                continue

            candidates.append(c)
            result.passed_candidates.append(_snapshot_passed_candidate(c))

        result.passed = len(candidates)
        self._history.append(result)
        self._history = self._history[-10:]

        log.info(
            "scan: src=(prf=%d boost=%d top=%d pump=%d) unique=%d cd=%d "
            "fetched=%d cuts=(no_pairs=%d parse=%d filter=%d) -> pass=%d",
            result.src_ds_profiles, result.src_ds_boosted, result.src_ds_top,
            result.src_pump, result.unique_mints, result.on_cooldown,
            result.fetched, result.no_pairs, result.parse_fail,
            result.filter_fail, result.passed,
        )
        if result.sample_filter_reasons:
            log.info("filter samples: %s", " | ".join(result.sample_filter_reasons))

        return candidates, result

    def format_scan_stats(self) -> str:
        if not self._history:
            return (
                "🔍 <b>Tarama istatistikleri</b>\n"
                "Henüz tarama yok.\n\n"
                "Not: Bot yeni başladıysa ilk tarama için scan loop'un tamamlanması gerekir."
            )

        recent = list(reversed(self._history[-5:]))
        total_passed = sum(x.passed for x in self._history)
        total_filter_fail = sum(x.filter_fail for x in self._history)
        total_fetched = sum(x.fetched for x in self._history)
        last = recent[0]
        last_early = len([x for x in last.passed_candidates if x.mode == "EARLY WATCH"])
        last_confirmed = len([x for x in last.passed_candidates if x.mode == "CONFIRMED SIGNAL"])

        lines = [
            "🔍 <b>Radar sağlık paneli</b>",
            f"Son tarama: fetched=<code>{last.fetched}</code> | "
            f"filter=<code>{last.filter_fail}</code> | "
            f"aday=<code>{last.passed}</code>",
            f"Son tarama Early/Alınabilir: 🟡 <code>{last_early}</code> / 🟢 <code>{last_confirmed}</code>",
            f"Son {len(self._history)} tarama toplamı: fetched=<code>{total_fetched}</code> | "
            f"filter fail=<code>{total_filter_fail}</code> | aday=<code>{total_passed}</code>",
            "",
            "Not: Yalnızca skoru yeterli ALINABİLİR coinler ayrı Telegram bildirimi alır. Erken ama zayıf adaylar sessiz izlenebilir.",
        ]

        if last.sample_filter_reasons:
            sample = "; ".join(_esc(x) for x in last.sample_filter_reasons[:3])
            lines.append(f"\n<i>Örnek red sebepleri: {sample}</i>")

        return "\n".join(lines)


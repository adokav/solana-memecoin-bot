"""Screener — aday mint'leri toplar, 5 hard gate'ten geçirir.

Akış (linear, basit):
  1. Kaynaklardan mint listesi al (DS latest_profiles + boosted + top + pump.fun graduate)
  2. Her mint için DS pair fetch (en likit pair)
  3. Candidate parse
  4. filter.passes() — 5 hard gate
  5. Pass eden mint'ler döner — safety + buy main.py'da
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from candidate import Candidate, parse as parse_candidate
from config import config
from dexscreener import DexScreener
from filter import passes as filter_passes
from pumpfun import PumpFun

log = logging.getLogger(__name__)


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


class Screener:
    def __init__(self, ds: DexScreener, pf: PumpFun) -> None:
        self.ds = ds
        self.pf = pf
        # token → (last_seen_ts, passed_bool)
        self._cooldown: dict[str, tuple[float, bool]] = {}
        # Son 10 scan diagnostic
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
        """Bir tarama turu döndürür: (geçen aday listesi, diagnostic)."""
        result = ScanResult(ts=time.time())

        # 1. Kaynaklardan mint listesi
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
        result.fetched = min(len(mints), 80)

        # 2-4. Her mint için pair fetch + parse + filter
        candidates: list[Candidate] = []
        for mint in mints[:80]:
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
                # Filtreyi geçmeyen mint'e kısa cooldown
                self.mark_seen(c.base_token, passed=False)
                continue
            candidates.append(c)

        result.passed = len(candidates)
        self._history.append(result)
        if len(self._history) > 10:
            self._history = self._history[-10:]

        log.info(
            "scan: src=(prf=%d boost=%d top=%d pump=%d) unique=%d cd=%d "
            "fetched=%d cuts=(no_pairs=%d parse=%d filter=%d) → pass=%d",
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
            return "🔍 <b>Tarama istatistikleri</b>\nHenüz tarama yok."
        recent = list(reversed(self._history[-5:]))
        lines = [f"🔍 <b>Son {len(recent)} tarama</b>"]
        for s in recent:
            age_min = (time.time() - s.ts) / 60
            lines.append(
                f"\n<i>{age_min:.0f}dk önce</i>\n"
                f"  Kaynak: ds_profiles=<code>{s.src_ds_profiles}</code> "
                f"boost=<code>{s.src_ds_boosted}</code> "
                f"top=<code>{s.src_ds_top}</code> "
                f"pump=<code>{s.src_pump}</code>\n"
                f"  Unique: <code>{s.unique_mints}</code>  "
                f"cooldown: <code>{s.on_cooldown}</code>  "
                f"fetched: <code>{s.fetched}</code>\n"
                f"  Cuts: no_pairs=<code>{s.no_pairs}</code> "
                f"parse=<code>{s.parse_fail}</code> "
                f"filter=<code>{s.filter_fail}</code>\n"
                f"  → <b>passed: {s.passed}</b>"
            )
            if s.sample_filter_reasons:
                lines.append(
                    f"  <i>Filter reasons: {', '.join(s.sample_filter_reasons[:3])}</i>"
                )
        return "\n".join(lines)

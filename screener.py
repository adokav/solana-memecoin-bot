"""KATMAN 1: Fırsat avı.

İki paralel profil:
  - EARLY: 1-24h yaşında, momentum başlangıcı yakala
  - TREND: 24h-7g yaşında, yerleşmiş ama hâlâ koşan

Olmazsa olmaz filtreleri geçen aday → 0-100 skor → yüksek skorlar alert
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Literal

from config import config
from dexscreener import DexScreener

log = logging.getLogger(__name__)

Profile = Literal["early", "trend"]


@dataclass
class Candidate:
    chain: str
    pair_address: str
    base_token: str
    base_symbol: str
    quote_symbol: str
    dex: str
    price_usd: float
    liquidity_usd: float
    fdv: float
    volume_h24: float
    volume_h6: float
    volume_h1: float
    volume_m5: float
    price_change_h24: float
    price_change_h6: float
    price_change_h1: float
    price_change_m5: float
    txns_h1: int
    buys_h1: int
    sells_h1: int
    pair_age_h: float
    boosts_active: int
    has_twitter: bool
    has_telegram: bool
    has_website: bool
    url: str
    profile: Profile = "early"
    score: float = 0
    score_breakdown: dict = field(default_factory=dict)


# ---------- DexScreener pair -> Candidate ----------

def _parse_pair(p: dict) -> Candidate | None:
    try:
        liq = (p.get("liquidity") or {})
        vol = (p.get("volume") or {})
        chg = (p.get("priceChange") or {})
        txns = (p.get("txns") or {})
        h1_txns = (txns.get("h1") or {})
        buys_h1 = int(h1_txns.get("buys", 0))
        sells_h1 = int(h1_txns.get("sells", 0))

        created_ms = p.get("pairCreatedAt") or 0
        age_h = (time.time() * 1000 - created_ms) / 3_600_000 if created_ms else 9999

        info = p.get("info") or {}
        socials = {s.get("type", "").lower(): s.get("url") for s in (info.get("socials") or [])}
        websites = info.get("websites") or []
        boosts = (p.get("boosts") or {}).get("active", 0)

        return Candidate(
            chain=p.get("chainId", ""),
            pair_address=p.get("pairAddress", ""),
            base_token=(p.get("baseToken") or {}).get("address", ""),
            base_symbol=(p.get("baseToken") or {}).get("symbol", "?"),
            quote_symbol=(p.get("quoteToken") or {}).get("symbol", "?"),
            dex=p.get("dexId", ""),
            price_usd=float(p.get("priceUsd") or 0),
            liquidity_usd=float(liq.get("usd") or 0),
            fdv=float(p.get("fdv") or 0),
            volume_h24=float(vol.get("h24") or 0),
            volume_h6=float(vol.get("h6") or 0),
            volume_h1=float(vol.get("h1") or 0),
            volume_m5=float(vol.get("m5") or 0),
            price_change_h24=float(chg.get("h24") or 0),
            price_change_h6=float(chg.get("h6") or 0),
            price_change_h1=float(chg.get("h1") or 0),
            price_change_m5=float(chg.get("m5") or 0),
            txns_h1=buys_h1 + sells_h1,
            buys_h1=buys_h1,
            sells_h1=sells_h1,
            pair_age_h=age_h,
            boosts_active=int(boosts) if boosts else 0,
            has_twitter="twitter" in socials,
            has_telegram="telegram" in socials,
            has_website=bool(websites),
            url=p.get("url", ""),
        )
    except (TypeError, ValueError) as e:
        log.debug("pair parse error: %s", e)
        return None


# ---------- Hızlı eler (genel kalite kontrolü) ----------

def _basic_sanity(c: Candidate) -> tuple[bool, str]:
    if c.chain != "solana":
        return False, "not solana"
    if c.quote_symbol.upper() not in {"SOL", "WSOL", "USDC"}:
        return False, f"unsupported quote {c.quote_symbol}"
    if c.price_usd <= 0 or c.liquidity_usd <= 0:
        return False, "zero price/liq"
    # Honeypot klasiği: hiç satış olmamış
    if c.txns_h1 >= 20 and c.sells_h1 == 0:
        return False, "no sells (honeypot suspicion)"
    # Wash trading şüphesi (üst sınır)
    if c.txns_h1 >= 50:
        buy_ratio = c.buys_h1 / max(c.txns_h1, 1)
        if buy_ratio > config.early_max_buy_ratio:
            return False, f"wash trading suspicion ({buy_ratio:.0%} buys)"
    # Ortalama işlem boyutu: çok küçük = micro-spam, çok büyük = whale wash
    if c.txns_h1 >= config.avg_tx_min_txns and c.volume_h1 > 0:
        avg_tx = c.volume_h1 / c.txns_h1
        if avg_tx < config.min_avg_tx_size_usd:
            return False, f"avg tx too small: ${avg_tx:.1f} (micro-spam)"
        if avg_tx > config.max_avg_tx_size_usd:
            return False, f"avg tx too large: ${avg_tx:.0f} (whale/wash)"
    return True, "ok"


# ---------- Profil filtreleri ----------

def _check_early(c: Candidate) -> tuple[bool, str]:
    if not (config.early_min_age_h <= c.pair_age_h <= config.early_max_age_h):
        return False, f"early age out: {c.pair_age_h:.1f}h"
    if not (config.early_min_liq <= c.liquidity_usd <= config.early_max_liq):
        return False, f"early liq out: ${c.liquidity_usd:.0f}"
    vol_ratio = c.volume_h1 / max(c.liquidity_usd, 1)
    if vol_ratio < config.early_min_vol_h1_ratio:
        return False, f"early vol/liq low: {vol_ratio:.2f}"
    if c.price_change_h1 < config.early_min_price_h1:
        return False, f"early h1 weak: {c.price_change_h1:.1f}%"
    if c.price_change_m5 < config.early_min_price_m5:
        return False, f"early m5 weak: {c.price_change_m5:.1f}%"
    if c.txns_h1 < config.early_min_txns_h1:
        return False, f"early txns low: {c.txns_h1}"
    buy_ratio = c.buys_h1 / max(c.txns_h1, 1)
    if buy_ratio < config.early_min_buy_ratio:
        return False, f"early buys ratio low: {buy_ratio:.2f}"
    return True, "early ok"


def _check_trend(c: Candidate) -> tuple[bool, str]:
    if not (config.trend_min_age_h <= c.pair_age_h <= config.trend_max_age_h):
        return False, f"trend age out: {c.pair_age_h:.1f}h"
    if c.liquidity_usd < config.trend_min_liq:
        return False, f"trend liq low: ${c.liquidity_usd:.0f}"
    if c.volume_h6 < config.trend_min_vol_h6:
        return False, f"trend vol_h6 low: ${c.volume_h6:.0f}"
    if c.price_change_h6 < config.trend_min_price_h6:
        return False, f"trend h6 weak: {c.price_change_h6:.1f}%"
    if c.price_change_h24 < config.trend_min_price_h24:
        return False, f"trend h24 weak: {c.price_change_h24:.1f}%"
    if c.txns_h1 < config.trend_min_txns_h1:
        return False, f"trend txns low: {c.txns_h1}"
    return True, "trend ok"


# ---------- 0-100 skor sistemi ----------

def _score(c: Candidate) -> tuple[float, dict]:
    breakdown: dict = {}

    # Momentum (max 25)
    momentum_raw = (c.price_change_h1 * 1.5) + (c.price_change_h6 * 0.8)
    momentum = min(25.0, momentum_raw / 4.0)
    breakdown["momentum"] = round(momentum, 1)

    # Volume/Liquidity oranı (max 20)
    vol_liq = c.volume_h1 / max(c.liquidity_usd, 1)
    vol_score = min(20.0, vol_liq * 20)  # 1.0 oran = tam puan
    breakdown["vol_liq"] = round(vol_score, 1)

    # Net alıcı baskısı (max 15)
    if c.txns_h1 > 0:
        buy_ratio = c.buys_h1 / c.txns_h1
        # 0.5 nötr, 0.7+ tam puan
        buy_score = max(0, min(15.0, (buy_ratio - 0.5) * 75))
    else:
        buy_score = 0
    breakdown["buy_pressure"] = round(buy_score, 1)

    # Hacim ivmesi (max 10) - son 5dk × 12 vs son 1h
    accel = (c.volume_m5 * 12) / max(c.volume_h1, 1)
    accel_score = min(10.0, accel * 6.67)  # 1.5x = tam puan
    breakdown["acceleration"] = round(accel_score, 1)

    # Boost + sosyal (max 10)
    social = 0
    if c.boosts_active > 0:
        social += 4
    if c.has_twitter:
        social += 2
    if c.has_telegram:
        social += 2
    if c.has_website:
        social += 2
    breakdown["social"] = min(10, social)

    # Yaş sweet spot (max 5)
    # EARLY için 2-12h tam, TREND için 24-72h tam
    if c.profile == "early":
        if 2 <= c.pair_age_h <= 12:
            age_score = 5
        elif c.pair_age_h < 2:
            age_score = 2  # çok yeni risk
        else:
            age_score = max(0, 5 - (c.pair_age_h - 12) * 0.2)
    else:
        if 24 <= c.pair_age_h <= 72:
            age_score = 5
        else:
            age_score = max(0, 5 - abs(c.pair_age_h - 48) * 0.05)
    breakdown["age_fit"] = round(age_score, 1)

    # Likidite kalitesi - FDV oranı (max 5)
    if c.fdv > 0:
        liq_fdv = c.liquidity_usd / c.fdv
        if liq_fdv >= 0.1:
            liq_score = 5
        elif liq_fdv >= 0.05:
            liq_score = 3
        elif liq_fdv >= 0.02:
            liq_score = 1
        else:
            liq_score = 0
    else:
        liq_score = 2
    breakdown["liq_quality"] = round(liq_score, 1)

    # Holder sağlığı placeholder (max 10) - rugcheck.py'de doldurulacak
    breakdown["holder_health"] = 0

    total = sum(breakdown.values())
    return round(total, 1), breakdown


# ---------- Ana Screener ----------

class Screener:
    def __init__(self, ds: DexScreener) -> None:
        self.ds = ds
        # {base_token: (last_alerted_ts, score)}  score=0 → red (rug/honeypot), uzun cooldown
        self._cooldown: dict[str, tuple[float, float]] = {}

    def _cooldown_hours_for(self, score: float) -> float:
        if score >= config.high_confidence_score:
            return config.cooldown_hours_high
        if score >= config.min_score_to_alert:
            return config.cooldown_hours_mid
        return config.cooldown_hours_reject

    def _on_cooldown(self, token: str) -> bool:
        entry = self._cooldown.get(token)
        if not entry:
            return False
        last_ts, last_score = entry
        return (time.time() - last_ts) < (self._cooldown_hours_for(last_score) * 3600)

    def mark_alerted(self, token: str, score: float = 0.0) -> None:
        self._cooldown[token] = (time.time(), score)

    async def scan(self) -> list[Candidate]:
        """Yeni token havuzunu çek, ikili profilden geçenleri döner (skorla sıralı)."""
        seen_tokens: set[str] = set()
        sol_tokens: list[str] = []

        for source in (
            await self.ds.latest_profiles(),
            await self.ds.latest_boosted(),
            await self.ds.top_boosted(),
        ):
            for item in source:
                if item.get("chainId") != "solana":
                    continue
                addr = item.get("tokenAddress")
                if not addr or addr in seen_tokens:
                    continue
                seen_tokens.add(addr)
                if not self._on_cooldown(addr):
                    sol_tokens.append(addr)

        candidates: list[Candidate] = []
        for token in sol_tokens[:80]:  # işlem yükünü kapla
            pairs = await self.ds.pairs_for_token("solana", token)
            if not pairs:
                continue
            # En likit pair
            pairs.sort(
                key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0),
                reverse=True,
            )
            c = _parse_pair(pairs[0])
            if not c:
                continue
            ok, reason = _basic_sanity(c)
            if not ok:
                log.debug("sanity skip %s: %s", c.base_symbol, reason)
                continue

            # İkili profil değerlendirmesi
            passed_early, early_reason = _check_early(c)
            passed_trend, trend_reason = _check_trend(c)

            if passed_early:
                c.profile = "early"
            elif passed_trend:
                c.profile = "trend"
            else:
                log.debug("both profiles skip %s: %s | %s",
                          c.base_symbol, early_reason, trend_reason)
                continue

            score, breakdown = _score(c)
            c.score = score
            c.score_breakdown = breakdown

            if score < config.min_score_to_alert:
                log.debug("low score skip %s: %.1f", c.base_symbol, score)
                continue

            candidates.append(c)

        candidates.sort(key=lambda x: x.score, reverse=True)
        return candidates

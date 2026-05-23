"""Pump.fun pre-graduation alert sistemi + sosyal velocity sinyali.

Bot bonding curve üzerinde direkt trade edemez (Jupiter route yok).
Onun yerine: graduation'a yaklaşmış (≥X% progress) ve yüksek sosyal
engagement (reply velocity) gösteren coin'ler için alert atar.

Kullanım:
  - Kullanıcı manuel olarak pump.fun'da satın alabilir
  - VEYA graduation gerçekleştiğinde mevcut pipeline'ımız token'ı zaten
    yakalıyor (pumpfun.recently_graduated), bu sayede önceden tanımış
    oluyoruz → smart_signal varsa daha hızlı reaksiyon

Sosyal velocity:
  - Twitter mention velocity için ücretsiz güvenilir kaynak yok
    ($100/ay Twitter API)
  - Pump.fun'ın reply_count metriği memecoin community engagement için
    free ve gerçek zamanlı proxy
  - Reply velocity (saatte) = (son count - ilk count) / saat
  - Tüm aktif coin'leri pollarken her birinin reply_count'unu zaten
    çekiyoruz, ek API çağrısı yok
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from config import config
from pumpfun import PumpCoin, PumpFun

log = logging.getLogger(__name__)


@dataclass
class ReplyTracker:
    # mint -> [(ts, reply_count), ...] sliding window 2h
    history: dict[str, list[tuple[float, int]]] = field(default_factory=dict)
    # mint -> last_alerted_ts (24h cooldown)
    alerted: dict[str, float] = field(default_factory=dict)

    def record(self, mint: str, count: int) -> None:
        now = time.time()
        cutoff = now - 2 * 3600  # 2h window
        hist = [(ts, c) for ts, c in self.history.get(mint, []) if ts > cutoff]
        hist.append((now, count))
        self.history[mint] = hist

    def velocity_per_hour(self, mint: str) -> float:
        """Reply count saatlik artış hızı."""
        hist = self.history.get(mint) or []
        if len(hist) < 2:
            return 0.0
        ts_first, count_first = hist[0]
        ts_last, count_last = hist[-1]
        elapsed_h = (ts_last - ts_first) / 3600
        if elapsed_h < 0.05:  # minimum 3 dakika ölçüm
            return 0.0
        delta = count_last - count_first
        if delta <= 0:
            return 0.0
        return delta / elapsed_h

    def is_alerted_recently(self, mint: str) -> bool:
        last = self.alerted.get(mint)
        return last is not None and (time.time() - last) < 24 * 3600

    def mark_alerted(self, mint: str) -> None:
        self.alerted[mint] = time.time()

    def cleanup(self) -> None:
        """Eski cooldown kayıtlarını sil."""
        cutoff = time.time() - 48 * 3600
        for mint in list(self.alerted.keys()):
            if self.alerted[mint] < cutoff:
                del self.alerted[mint]


class PrePumpDetector:
    def __init__(self, pf: PumpFun) -> None:
        self.pf = pf
        self.tracker = ReplyTracker()

    async def scan(self) -> list[tuple[PumpCoin, float]]:
        """Alert verilmesi gereken (coin, velocity) listesini döner."""
        coins = await self.pf.active_coins()
        if not coins:
            return []
        self.tracker.cleanup()

        out: list[tuple[PumpCoin, float]] = []
        for coin in coins:
            # Reply history'i her tur kaydet
            self.tracker.record(coin.mint, coin.reply_count)

            # Filtre 1: graduation yakın mı
            if coin.progress_pct < config.prepump_min_progress_pct:
                continue
            # Filtre 2: minimum MC (terkedilmiş tokenları ele)
            if coin.usd_market_cap < config.prepump_min_mc_usd:
                continue
            # Filtre 3: bot/spam değil — gerçek community
            if coin.reply_count < config.prepump_min_replies:
                continue
            # Filtre 4: sosyal velocity yeterli (asıl sinyal)
            velocity = self.tracker.velocity_per_hour(coin.mint)
            if velocity < config.prepump_min_velocity_per_hour:
                continue
            # Cooldown
            if self.tracker.is_alerted_recently(coin.mint):
                continue

            out.append((coin, velocity))
            self.tracker.mark_alerted(coin.mint)

        return out


def format_prepump_alert(coin: PumpCoin, velocity: float) -> str:
    age_h = (
        (time.time() - coin.created_ts) / 3600 if coin.created_ts > 0 else 0
    )
    return (
        f"🐸 <b>PUMP.FUN PRE-GRAD</b>\n"
        f"<b>${coin.symbol}</b> — {coin.name}\n\n"
        f"💰 MC: <code>${coin.usd_market_cap:,.0f}</code>  "
        f"(<code>{coin.progress_pct:.0f}%</code> bonding curve)\n"
        f"💬 Reply velocity: <code>{velocity:.1f}/saat</code>\n"
        f"💬 Toplam reply: <code>{coin.reply_count}</code>\n"
        f"⏱ Yaş: <code>{age_h:.1f}h</code>\n\n"
        f"⚠️ <i>Bonding curve'de — Jupiter route yok. Manuel almak için "
        f"pump.fun'a git.</i>\n\n"
        f"<a href=\"{coin.pump_url}\">pump.fun</a> · "
        f"<a href=\"https://solscan.io/token/{coin.mint}\">Solscan</a>\n"
        f"<code>{coin.mint}</code>"
    )

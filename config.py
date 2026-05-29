"""Bot ayarları.

Bu sürümün ana felsefesi:
- Otomatik alım kapalıdır.
- Bot fırsat/scam filtresi yapar ve Telegram'a aday yollar.
- Kullanıcı alımı manuel yapar.
- İstenirse aynı cüzdandaki token hızlı kapatma butonuyla satılabilir.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _str(name: str, default: str = "", required: bool = False) -> str:
    val = os.getenv(name, default)
    if required and not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val or ""


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (ValueError, TypeError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (ValueError, TypeError):
        return default


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name, str(default)).strip().lower()
    return val in ("1", "true", "yes", "on")


@dataclass
class Config:
    # Telegram
    telegram_token: str = field(default_factory=lambda: _str("TOKEN", required=True))
    telegram_chat_id: int = field(default_factory=lambda: _int("CHAT_ID", 0))

    # Solana / execution. WALLET_PRIVATE_KEY opsiyonel:
    # boşsa bot sadece uyarı verir; doluysa "Pozisyonu Kapat" butonu satış deneyebilir.
    rpc_url: str = field(default_factory=lambda: _str("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"))
    wallet_private_key: str = field(default_factory=lambda: _str("WALLET_PRIVATE_KEY", ""))

    # Veri
    data_dir: Path = field(default_factory=lambda: Path(_str("DATA_DIR", "./data")))

    # Çalışma modu
    alert_only_mode: bool = field(default_factory=lambda: _bool("ALERT_ONLY_MODE", True))
    auto_buy_enabled: bool = field(default_factory=lambda: _bool("AUTO_BUY_ENABLED", False))

    # Fırsat filtresi: scam riskini azaltmak için eski MVP'ye göre daha sıkı defaultlar.
    min_liq_usd: float = field(default_factory=lambda: _float("MIN_LIQ_USD", 5_000))
    max_liq_usd: float = field(default_factory=lambda: _float("MAX_LIQ_USD", 500_000))
    min_age_h: float = field(default_factory=lambda: _float("MIN_AGE_H", 0.08))      # ~5 dk
    max_age_h: float = field(default_factory=lambda: _float("MAX_AGE_H", 72))        # 2 gün
    # Radar V2: early-watch alt eşikleri. Bu eşikler erken fırsatı kaçırmamak için kullanılır;
    # güçlü sinyal ayrımı opportunity.py içinde yapılır.
    early_min_liq_usd: float = field(default_factory=lambda: _float("EARLY_MIN_LIQ_USD", 1_500))
    early_min_age_h: float = field(default_factory=lambda: _float("EARLY_MIN_AGE_H", 0.03))  # ~2 dk
    early_min_txns_h1: int = field(default_factory=lambda: _int("EARLY_MIN_TXNS_H1", 10))
    early_min_volume_h1_usd: float = field(default_factory=lambda: _float("EARLY_MIN_VOLUME_H1_USD", 500))
    early_min_sells_h1: int = field(default_factory=lambda: _int("EARLY_MIN_SELLS_H1", 1))
    early_min_volume_liq_ratio: float = field(default_factory=lambda: _float("EARLY_MIN_VOLUME_LIQ_RATIO", 0.05))
    min_txns_h1: int = field(default_factory=lambda: _int("MIN_TXNS_H1", 35))
    min_volume_h1_usd: float = field(default_factory=lambda: _float("MIN_VOLUME_H1_USD", 4_000))
    min_volume_liq_ratio: float = field(default_factory=lambda: _float("MIN_VOLUME_LIQ_RATIO", 0.15))
    max_volume_liq_ratio: float = field(default_factory=lambda: _float("MAX_VOLUME_LIQ_RATIO", 25.0))
    min_buy_ratio: float = field(default_factory=lambda: _float("MIN_BUY_RATIO", 0.48))
    max_buy_ratio: float = field(default_factory=lambda: _float("MAX_BUY_RATIO", 0.78))
    min_sells_h1: int = field(default_factory=lambda: _int("MIN_SELLS_H1", 3))
    min_price_h1: float = field(default_factory=lambda: _float("MIN_PRICE_H1", -18))
    max_price_h1: float = field(default_factory=lambda: _float("MAX_PRICE_H1", 500))
    min_price_h6: float = field(default_factory=lambda: _float("MIN_PRICE_H6", -35))
    max_price_h6: float = field(default_factory=lambda: _float("MAX_PRICE_H6", 1600))

    # Safety / rug bariyeri
    require_mint_revoked: bool = field(default_factory=lambda: _bool("REQUIRE_MINT_REVOKED", True))
    require_freeze_revoked: bool = field(default_factory=lambda: _bool("REQUIRE_FREEZE_REVOKED", True))
    max_roundtrip_loss_pct: float = field(default_factory=lambda: _float("MAX_ROUNDTRIP_LOSS_PCT", 18))
    max_price_impact_pct: float = field(default_factory=lambda: _float("MAX_PRICE_IMPACT_PCT", 7))

    # Alert policy: hard filter sadece bariz çöp/scam gürültüsünü eler.
    # Gerçek "alınabilir" kararı skor eşikleriyle verilir.
    min_alert_opportunity_score: int = field(default_factory=lambda: _int("MIN_ALERT_OPPORTUNITY_SCORE", 70))
    max_alert_risk_score: int = field(default_factory=lambda: _int("MAX_ALERT_RISK_SCORE", 62))
    min_alert_exit_score: int = field(default_factory=lambda: _int("MIN_ALERT_EXIT_SCORE", 55))
    # V10 probability scoring gates. These are deliberately stricter than raw
    # opportunity: buyable alerts must be survivable, exit-able and statistically
    # credible, not merely "pumping".
    min_alert_edge_score: int = field(default_factory=lambda: _int("MIN_ALERT_EDGE_SCORE", 68))
    min_alert_confidence_score: int = field(default_factory=lambda: _int("MIN_ALERT_CONFIDENCE_SCORE", 55))
    min_alert_survival_score: int = field(default_factory=lambda: _int("MIN_ALERT_SURVIVAL_SCORE", 58))
    silent_watch_early: bool = field(default_factory=lambda: _bool("SILENT_WATCH_EARLY", True))
    safety_allow_rugcheck_unreachable: bool = field(default_factory=lambda: _bool("SAFETY_ALLOW_RUGCHECK_UNREACHABLE", True))
    quote_test_sol: float = field(default_factory=lambda: _float("QUOTE_TEST_SOL", 0.01))

    # Jupiter buy/sell settings
    buy_amount_sol: float = field(default_factory=lambda: _float("BUY_AMOUNT_SOL", 0.01))
    buy_slippage_bps: int = field(default_factory=lambda: _int("BUY_SLIPPAGE_BPS", 500))
    sell_slippage_bps: int = field(default_factory=lambda: _int("SELL_SLIPPAGE_BPS", 900))
    dynamic_slippage_enabled: bool = field(default_factory=lambda: _bool("DYNAMIC_SLIPPAGE", True))
    dynamic_slippage_max_bps: int = field(default_factory=lambda: _int("DYNAMIC_SLIPPAGE_MAX_BPS", 1500))
    priority_fee_level: str = field(default_factory=lambda: _str("PRIORITY_FEE_LEVEL", "veryHigh"))
    max_priority_fee_lamports: int = field(default_factory=lambda: _int("MAX_PRIORITY_FEE_LAMPORTS", 5_000_000))

    # İzleme / formasyon bozulma
    watch_after_alert: bool = field(default_factory=lambda: _bool("WATCH_AFTER_ALERT", True))
    watch_ttl_hours: float = field(default_factory=lambda: _float("WATCH_TTL_HOURS", 6))
    warn_drawdown_from_peak_pct: float = field(default_factory=lambda: _float("WARN_DRAWDOWN_FROM_PEAK_PCT", 28))
    warn_liq_drop_pct: float = field(default_factory=lambda: _float("WARN_LIQ_DROP_PCT", 30))
    warn_buy_ratio_below: float = field(default_factory=lambda: _float("WARN_BUY_RATIO_BELOW", 0.42))
    warn_price_h1_below: float = field(default_factory=lambda: _float("WARN_PRICE_H1_BELOW", -12))

    # Kaynaklar
    pumpfun_enabled: bool = field(default_factory=lambda: _bool("PUMPFUN_ENABLED", True))
    pumpfun_fetch_limit: int = field(default_factory=lambda: _int("PUMPFUN_FETCH_LIMIT", 30))
    max_mints_per_scan: int = field(default_factory=lambda: _int("MAX_MINTS_PER_SCAN", 80))

    # Loop
    scan_interval: int = field(default_factory=lambda: _int("SCAN_INTERVAL", 60))
    monitor_interval: int = field(default_factory=lambda: _int("MONITOR_INTERVAL", 20))
    cooldown_hours_pass: float = field(default_factory=lambda: _float("COOLDOWN_HOURS_PASS", 3))
    # Reject cooldown must be short for memecoin radar. A 12h reject cooldown can make
    # the bot blind after one scan because the same fresh tokens quickly reappear from
    # DexScreener sources. Default: 0.15h ≈ 9 minutes.
    cooldown_hours_reject: float = field(default_factory=lambda: _float("COOLDOWN_HOURS_REJECT", 0.15))

    # Sabitler
    sol_mint: str = "So11111111111111111111111111111111111111112"
    usdc_mint: str = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

    def __post_init__(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.telegram_chat_id:
            raise RuntimeError("CHAT_ID is required and must be an integer")


config = Config()

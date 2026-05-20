"""Ortam değişkenlerini yükler.

Render'daki mevcut isimler:
  TOKEN              -> Telegram bot token
  CHAT_ID            -> Telegram chat ID
Bunları kod içinde standart isimlerle eşliyoruz.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _str(name: str, default: str | None = None, required: bool = False) -> str:
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
    # --- Telegram (Render isimleri: TOKEN, CHAT_ID) ---
    telegram_token: str = field(default_factory=lambda: _str("TOKEN", required=True))
    telegram_chat_id: int = field(default_factory=lambda: _int("CHAT_ID", 0))

    # --- Solana ---
    rpc_url: str = field(default_factory=lambda: _str(
        "SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"
    ))
    wallet_private_key: str = field(default_factory=lambda: _str(
        "WALLET_PRIVATE_KEY", required=True
    ))
    helius_api_key: str = field(default_factory=lambda: _str("HELIUS_API_KEY", ""))

    # --- Veri kalıcılığı ---
    data_dir: Path = field(default_factory=lambda: Path(_str("DATA_DIR", "./data")))

    # --- İşlem ---
    buy_amount_sol: float = field(default_factory=lambda: _float("BUY_AMOUNT_SOL", 0.01))
    slippage_bps: int = field(default_factory=lambda: _int("SLIPPAGE_BPS", 300))

    # --- Kademeli çıkış ---
    tp1_trigger: float = field(default_factory=lambda: _float("TP1_TRIGGER_PCT", 30))
    tp1_sell: float = field(default_factory=lambda: _float("TP1_SELL_PCT", 30))
    tp2_trigger: float = field(default_factory=lambda: _float("TP2_TRIGGER_PCT", 80))
    tp2_sell: float = field(default_factory=lambda: _float("TP2_SELL_PCT", 40))
    tp3_trigger: float = field(default_factory=lambda: _float("TP3_TRIGGER_PCT", 200))
    tp3_sell: float = field(default_factory=lambda: _float("TP3_SELL_PCT", 50))
    stop_loss: float = field(default_factory=lambda: _float("STOP_LOSS_PCT", 35))
    trailing_stop: float = field(default_factory=lambda: _float("TRAILING_STOP_PCT", 25))
    breakeven_after_tp1: bool = field(default_factory=lambda: _bool("BREAKEVEN_AFTER_TP1", True))

    # --- KATMAN 1: Erken giriş ---
    early_min_liq: float = field(default_factory=lambda: _float("EARLY_MIN_LIQUIDITY", 15000))
    early_max_liq: float = field(default_factory=lambda: _float("EARLY_MAX_LIQUIDITY", 150000))
    early_min_age_h: float = field(default_factory=lambda: _float("EARLY_MIN_AGE_H", 1))
    early_max_age_h: float = field(default_factory=lambda: _float("EARLY_MAX_AGE_H", 24))
    early_min_vol_h1_ratio: float = field(default_factory=lambda: _float("EARLY_MIN_VOL_H1_RATIO", 0.5))
    early_min_price_h1: float = field(default_factory=lambda: _float("EARLY_MIN_PRICE_H1", 15))
    early_min_price_m5: float = field(default_factory=lambda: _float("EARLY_MIN_PRICE_M5", 3))
    early_min_txns_h1: int = field(default_factory=lambda: _int("EARLY_MIN_TXNS_H1", 80))
    early_min_buy_ratio: float = field(default_factory=lambda: _float("EARLY_MIN_BUY_RATIO", 0.57))

    # --- KATMAN 1: Trend takip ---
    trend_min_liq: float = field(default_factory=lambda: _float("TREND_MIN_LIQUIDITY", 50000))
    trend_min_age_h: float = field(default_factory=lambda: _float("TREND_MIN_AGE_H", 24))
    trend_max_age_h: float = field(default_factory=lambda: _float("TREND_MAX_AGE_H", 168))
    trend_min_vol_h6: float = field(default_factory=lambda: _float("TREND_MIN_VOL_H6", 100000))
    trend_min_price_h6: float = field(default_factory=lambda: _float("TREND_MIN_PRICE_H6", 25))
    trend_min_price_h24: float = field(default_factory=lambda: _float("TREND_MIN_PRICE_H24", 50))
    trend_min_txns_h1: int = field(default_factory=lambda: _int("TREND_MIN_TXNS_H1", 150))

    # --- KATMAN 2: Anti-rug ---
    require_mint_revoked: bool = field(default_factory=lambda: _bool("REQUIRE_MINT_REVOKED", True))
    require_freeze_revoked: bool = field(default_factory=lambda: _bool("REQUIRE_FREEZE_REVOKED", True))
    require_lp_locked: bool = field(default_factory=lambda: _bool("REQUIRE_LP_LOCKED", True))
    min_lp_locked_pct: float = field(default_factory=lambda: _float("MIN_LP_LOCKED_PCT", 95))
    max_top10_holder_pct: float = field(default_factory=lambda: _float("MAX_TOP10_HOLDER_PCT", 30))
    max_top1_holder_pct: float = field(default_factory=lambda: _float("MAX_TOP1_HOLDER_PCT", 10))
    min_holder_count: int = field(default_factory=lambda: _int("MIN_HOLDER_COUNT", 150))
    max_price_impact_pct: float = field(default_factory=lambda: _float("MAX_PRICE_IMPACT_PCT", 5))
    max_roundtrip_loss_pct: float = field(default_factory=lambda: _float("MAX_ROUNDTRIP_LOSS_PCT", 15))

    # --- Skor ---
    min_score_to_alert: float = field(default_factory=lambda: _float("MIN_SCORE_TO_ALERT", 50))
    high_confidence_score: float = field(default_factory=lambda: _float("HIGH_CONFIDENCE_SCORE", 70))

    # --- Loop ---
    scan_interval: int = field(default_factory=lambda: _int("SCAN_INTERVAL", 60))
    monitor_interval: int = field(default_factory=lambda: _int("MONITOR_INTERVAL", 20))
    heartbeat_interval: int = field(default_factory=lambda: _int("HEARTBEAT_INTERVAL", 300))

    # --- Anti-spam ---
    cooldown_hours: float = field(default_factory=lambda: _float("COOLDOWN_HOURS", 24))
    max_alerts_per_scan: int = field(default_factory=lambda: _int("MAX_ALERTS_PER_SCAN", 3))

    # --- Sabitler ---
    sol_mint: str = "So11111111111111111111111111111111111111112"
    usdc_mint: str = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

    def __post_init__(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.telegram_chat_id:
            raise RuntimeError("CHAT_ID is required and must be an integer")


config = Config()

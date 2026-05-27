"""Tüm parametreler tek dosyada. Sade.

Matematik temelli grup'lar:
  - Risk caps (asimetrik risk yönetimi)
  - 5 hard filter gate (EV>0 hipotezi)
  - Çıkış stratejisi (TP1 dinamik + trailing + SL + pyramid)
"""
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
    # === Telegram ===
    telegram_token: str = field(default_factory=lambda: _str("TOKEN", required=True))
    telegram_chat_id: int = field(default_factory=lambda: _int("CHAT_ID", 0))

    # === Solana RPC + cüzdan ===
    rpc_url: str = field(default_factory=lambda: _str(
        "SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"
    ))
    wallet_private_key: str = field(default_factory=lambda: _str(
        "WALLET_PRIVATE_KEY", required=True
    ))
    helius_api_key: str = field(default_factory=lambda: _str("HELIUS_API_KEY", ""))

    # === Veri kalıcılığı ===
    data_dir: Path = field(default_factory=lambda: Path(_str("DATA_DIR", "./data")))

    # === Position sizing (Kelly-conservative) ===
    # Kelly ~%1-2 sermaye. Küçük başla, EV doğrulandıkça büyüt.
    buy_amount_sol: float = field(default_factory=lambda: _float("BUY_AMOUNT_SOL", 0.01))
    max_open_positions: int = field(default_factory=lambda: _int("MAX_OPEN_POSITIONS", 3))
    max_total_exposure_sol: float = field(default_factory=lambda: _float("MAX_TOTAL_EXPOSURE_SOL", 0.05))

    # === Risk circuit breaker ===
    daily_loss_stop_sol: float = field(default_factory=lambda: _float("DAILY_LOSS_STOP_SOL", 0.05))
    max_consecutive_losses: int = field(default_factory=lambda: _int("MAX_CONSECUTIVE_LOSSES", 5))

    # === 5 Hard Filter Gate ===
    min_liq_usd: float = field(default_factory=lambda: _float("MIN_LIQ_USD", 1000))
    max_liq_usd: float = field(default_factory=lambda: _float("MAX_LIQ_USD", 500000))
    min_age_h: float = field(default_factory=lambda: _float("MIN_AGE_H", 0.25))    # 15dk
    max_age_h: float = field(default_factory=lambda: _float("MAX_AGE_H", 168))     # 7 gün
    min_txns_h1: int = field(default_factory=lambda: _int("MIN_TXNS_H1", 5))
    min_buy_ratio: float = field(default_factory=lambda: _float("MIN_BUY_RATIO", 0.40))
    min_price_h1: float = field(default_factory=lambda: _float("MIN_PRICE_H1", -30))

    # === KATMAN 2 Safety (rug barrier) ===
    require_mint_revoked: bool = field(default_factory=lambda: _bool("REQUIRE_MINT_REVOKED", True))
    require_freeze_revoked: bool = field(default_factory=lambda: _bool("REQUIRE_FREEZE_REVOKED", True))
    # Honeypot sim: SOL→token→SOL roundtrip max % kayıp
    max_roundtrip_loss_pct: float = field(default_factory=lambda: _float("MAX_ROUNDTRIP_LOSS_PCT", 15))
    max_price_impact_pct: float = field(default_factory=lambda: _float("MAX_PRICE_IMPACT_PCT", 8))

    # === Execution ===
    buy_slippage_bps: int = field(default_factory=lambda: _int("BUY_SLIPPAGE_BPS", 500))
    sell_slippage_bps: int = field(default_factory=lambda: _int("SELL_SLIPPAGE_BPS", 700))
    dynamic_slippage_enabled: bool = field(default_factory=lambda: _bool("DYNAMIC_SLIPPAGE", True))
    dynamic_slippage_max_bps: int = field(default_factory=lambda: _int("DYNAMIC_SLIPPAGE_MAX_BPS", 1500))
    priority_fee_level: str = field(default_factory=lambda: _str("PRIORITY_FEE_LEVEL", "veryHigh"))
    max_priority_fee_lamports: int = field(default_factory=lambda: _int("MAX_PRIORITY_FEE_LAMPORTS", 5_000_000))

    # === Çıkış stratejisi (matematik temelli) ===
    # TP1: dinamik anapara kurtarma — sell_pct = 1/(1+tp1_trigger/100) × 1.05
    tp1_trigger: float = field(default_factory=lambda: _float("TP1_TRIGGER_PCT", 50))
    # TP2/TP3: moon bag küçültme
    tp2_trigger: float = field(default_factory=lambda: _float("TP2_TRIGGER_PCT", 200))
    tp2_sell: float = field(default_factory=lambda: _float("TP2_SELL_PCT", 50))
    tp3_trigger: float = field(default_factory=lambda: _float("TP3_TRIGGER_PCT", 500))
    tp3_sell: float = field(default_factory=lambda: _float("TP3_SELL_PCT", 50))
    # SL + trailing
    stop_loss: float = field(default_factory=lambda: _float("STOP_LOSS_PCT", 35))
    trailing_stop: float = field(default_factory=lambda: _float("TRAILING_STOP_PCT", 25))
    # Hold-time: LP çekiliyor mu (gerçek rug indikatörü)
    hold_liq_drain_pct: float = field(default_factory=lambda: _float("HOLD_LIQ_DRAIN_PCT", 40))

    # === Pyramid (anti-martingale) ===
    pyramid_enabled: bool = field(default_factory=lambda: _bool("PYRAMID_ENABLED", True))
    pyramid_max_adds: int = field(default_factory=lambda: _int("PYRAMID_MAX_ADDS", 2))
    # TP1 sonrası her +N% adımında ekle (TP1=50, step=50 → 100, 150 noktalarında)
    pyramid_trigger_step_pct: float = field(default_factory=lambda: _float("PYRAMID_TRIGGER_STEP_PCT", 50))
    pyramid_size_ratio: float = field(default_factory=lambda: _float("PYRAMID_SIZE_RATIO", 0.5))

    # === Pump.fun graduate kaynağı ===
    pumpfun_enabled: bool = field(default_factory=lambda: _bool("PUMPFUN_ENABLED", True))
    pumpfun_fetch_limit: int = field(default_factory=lambda: _int("PUMPFUN_FETCH_LIMIT", 30))

    # === Loop interval'ları ===
    scan_interval: int = field(default_factory=lambda: _int("SCAN_INTERVAL", 60))
    monitor_interval: int = field(default_factory=lambda: _int("MONITOR_INTERVAL", 20))

    # === Anti-spam ===
    # Aynı tokeni cooldown saatleri (filtre eledikten sonra ne kadar tekrar bakmasın)
    cooldown_hours_pass: float = field(default_factory=lambda: _float("COOLDOWN_HOURS_PASS", 6))
    cooldown_hours_reject: float = field(default_factory=lambda: _float("COOLDOWN_HOURS_REJECT", 24))

    # === Sabitler ===
    sol_mint: str = "So11111111111111111111111111111111111111112"
    usdc_mint: str = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

    def __post_init__(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.telegram_chat_id:
            raise RuntimeError("CHAT_ID is required and must be an integer")


config = Config()

"""Machine learning scoring layer.

Bot'un kapanan trade verisinden (paper + real) feature-outcome ilişkisini
öğrenir. Logistic regression classifier:
  - Label: pnl_pct >= ML_WIN_THRESHOLD_PCT mı (binary)
  - Features: score_breakdown bileşenleri + profile + entry_liquidity +
    entry_top10_pct + saat (UTC) + smart_signal varlığı

Model `data/ml_model.pkl`'a kaydedilir. /train komutuyla manuel tetiklenir.
Model yoksa graceful no-op (skor componenti 0).

Tasarım kararı:
  - Logistic regression: az sample'la (~30+) çalışır, açıklanabilir
  - LightGBM/RF: ~200+ sample lazım, overkill
  - Online learning: state management complex, batch ile yetiniyoruz
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from config import config
from storage import Position

log = logging.getLogger(__name__)

MODEL_PATH = config.data_dir / "ml_model.pkl"

# Feature sırası — train ve inference arasında stabil olmalı
FEATURE_ORDER = [
    "momentum",
    "vol_liq",
    "buy_pressure",
    "acceleration",
    "social",
    "age_fit",
    "liq_quality",
    "holder_health",
    "smart_signal",
    "social_external",
    "is_early",          # profile == "early" → 1, "trend" → 0, "pump" → 2
    "is_pump",
    "entry_liquidity_log",
    "entry_top10_pct",
    "hour_utc",
]


@dataclass
class ModelBundle:
    model: LogisticRegression
    scaler: StandardScaler
    trained_at: float
    n_samples: int
    win_rate: float       # train set'teki kazanan oran
    test_accuracy: float


def _profile_encode(profile: str) -> tuple[int, int]:
    """is_early, is_pump döner."""
    if profile == "early":
        return 1, 0
    if profile == "pump":
        return 0, 1
    return 0, 0  # trend


def _safe_log(x: float) -> float:
    if x <= 0:
        return 0.0
    return float(np.log1p(x))


def extract_features_from_position(pos: Position) -> list[float]:
    """Closed Position'dan numerik feature vektörü çıkarır."""
    bd = pos.score_breakdown if hasattr(pos, "score_breakdown") else {}
    # score_breakdown Position dataclass'ında yok — closed pozisyonlar için
    # storage.Position score field'ı toplam ama breakdown saklı değil.
    # Fallback: dict erişimi mevcut field'a ya da 0
    is_early, is_pump = _profile_encode(getattr(pos, "profile", "trend"))
    hour = int(time.gmtime(pos.opened_at).tm_hour) if pos.opened_at else 0
    return [
        float(_get_bd(pos, "momentum")),
        float(_get_bd(pos, "vol_liq")),
        float(_get_bd(pos, "buy_pressure")),
        float(_get_bd(pos, "acceleration")),
        float(_get_bd(pos, "social")),
        float(_get_bd(pos, "age_fit")),
        float(_get_bd(pos, "liq_quality")),
        float(_get_bd(pos, "holder_health")),
        float(_get_bd(pos, "smart_signal")),
        float(_get_bd(pos, "social_external")),
        is_early,
        is_pump,
        _safe_log(pos.entry_liquidity_usd or 0),
        float(pos.entry_top10_pct or 0),
        hour,
    ]


def _get_bd(pos: Position, key: str) -> float:
    """Position.score_breakdown'a güvenli erişim — closed pozisyonlarda
    saklanmamış olabilir, varsayılan 0.
    """
    bd = getattr(pos, "score_breakdown", None)
    if isinstance(bd, dict):
        return float(bd.get(key) or 0)
    return 0.0


def extract_features_from_dict(score_breakdown: dict, profile: str,
                                entry_liquidity: float, entry_top10: float,
                                hour_utc: int) -> list[float]:
    """Henüz Position'a dönüşmemiş candidate için feature vektörü."""
    is_early, is_pump = _profile_encode(profile)
    return [
        float(score_breakdown.get("momentum") or 0),
        float(score_breakdown.get("vol_liq") or 0),
        float(score_breakdown.get("buy_pressure") or 0),
        float(score_breakdown.get("acceleration") or 0),
        float(score_breakdown.get("social") or 0),
        float(score_breakdown.get("age_fit") or 0),
        float(score_breakdown.get("liq_quality") or 0),
        float(score_breakdown.get("holder_health") or 0),
        float(score_breakdown.get("smart_signal") or 0),
        float(score_breakdown.get("social_external") or 0),
        is_early,
        is_pump,
        _safe_log(entry_liquidity),
        float(entry_top10),
        hour_utc,
    ]


def label_from_position(pos: Position) -> int | None:
    """Binary label: 1 = kazandı, 0 = kaybetti. None = label çıkarılamadı."""
    if pos.status != "closed":
        return None
    pnl = pos.pnl_pct
    if pnl is None:
        if pos.sol_spent > 0:
            pnl = ((pos.sol_received_total - pos.sol_spent) / pos.sol_spent) * 100
        else:
            return None
    return 1 if pnl >= config.ml_win_threshold_pct else 0


def train_from_positions(
    positions: list[Position],
) -> ModelBundle | None:
    """Closed pozisyonlardan model eğit. Yeterli sample yoksa None."""
    closed = [p for p in positions if p.status == "closed"]
    X_rows: list[list[float]] = []
    y_rows: list[int] = []
    for p in closed:
        label = label_from_position(p)
        if label is None:
            continue
        X_rows.append(extract_features_from_position(p))
        y_rows.append(label)

    n = len(X_rows)
    if n < config.ml_min_samples:
        log.info("ml train skip: %d samples < %d min", n, config.ml_min_samples)
        return None

    X = np.array(X_rows, dtype=float)
    y = np.array(y_rows, dtype=int)

    # Sınıf dengesi kontrolü
    pos_count = int(y.sum())
    neg_count = n - pos_count
    if pos_count < 3 or neg_count < 3:
        log.info("ml train skip: class imbalance pos=%d neg=%d", pos_count, neg_count)
        return None

    # Train/test split (80/20, deterministic)
    rng = np.random.default_rng(42)
    idx = rng.permutation(n)
    split = int(n * 0.8)
    train_idx, test_idx = idx[:split], idx[split:]
    X_train, y_train = X[train_idx], y[train_idx]
    X_test, y_test = X[test_idx], y[test_idx]

    scaler = StandardScaler()
    X_train_s = scaler.fit_transform(X_train)
    X_test_s = scaler.transform(X_test)

    model = LogisticRegression(
        max_iter=2000,
        class_weight="balanced",
        random_state=42,
    )
    model.fit(X_train_s, y_train)

    test_acc = float(model.score(X_test_s, y_test)) if len(X_test_s) > 0 else 0.0
    win_rate = float(y.mean())
    bundle = ModelBundle(
        model=model,
        scaler=scaler,
        trained_at=time.time(),
        n_samples=n,
        win_rate=win_rate,
        test_accuracy=test_acc,
    )
    return bundle


def save_model(bundle: ModelBundle) -> None:
    joblib.dump(
        {
            "model": bundle.model,
            "scaler": bundle.scaler,
            "trained_at": bundle.trained_at,
            "n_samples": bundle.n_samples,
            "win_rate": bundle.win_rate,
            "test_accuracy": bundle.test_accuracy,
        },
        MODEL_PATH,
    )
    log.info(
        "ml model saved: %s (n=%d, acc=%.2f)",
        MODEL_PATH, bundle.n_samples, bundle.test_accuracy,
    )


def load_model() -> ModelBundle | None:
    if not MODEL_PATH.exists():
        return None
    try:
        data = joblib.load(MODEL_PATH)
        return ModelBundle(
            model=data["model"],
            scaler=data["scaler"],
            trained_at=float(data["trained_at"]),
            n_samples=int(data["n_samples"]),
            win_rate=float(data["win_rate"]),
            test_accuracy=float(data["test_accuracy"]),
        )
    except Exception as e:
        log.error("ml model load failed: %s", e)
        return None


def predict_win_probability(bundle: ModelBundle, features: list[float]) -> float:
    """Verilen feature vektörü için kazanma olasılığı (0-1)."""
    X = np.array([features], dtype=float)
    X_s = bundle.scaler.transform(X)
    proba = bundle.model.predict_proba(X_s)[0]
    # proba[1] = class 1 (win) olasılığı
    if len(proba) >= 2:
        return float(proba[1])
    return 0.5


def format_ml_status(bundle: ModelBundle | None) -> str:
    if bundle is None:
        return (
            "🤖 <b>ML status</b>\n"
            f"Model yok. <code>/train</code> ile en az "
            f"<code>{config.ml_min_samples}</code> kapanan trade biriktiğinde eğitilebilir."
        )
    age_h = (time.time() - bundle.trained_at) / 3600
    return (
        f"🤖 <b>ML status</b>\n"
        f"Sample: <code>{bundle.n_samples}</code>  "
        f"Win rate (train): <code>{bundle.win_rate * 100:.0f}%</code>\n"
        f"Test accuracy: <code>{bundle.test_accuracy * 100:.0f}%</code>\n"
        f"Eğitildi: <code>{age_h:.1f}h</code> önce\n"
        f"Win threshold: <code>+{config.ml_win_threshold_pct:.0f}%</code> pnl"
    )

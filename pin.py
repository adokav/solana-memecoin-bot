"""Pin noktaları — parametre + performans snapshot'ları.

Manuel veya otomatik olarak mevcut tunable config + perf metriklerini
JSONL formatında kaydeder. Sonradan:
  - İki snapshot'ı karşılaştırarak (diff) hangi parametrenin değiştiğini gör
  - Performance trend takibi (her hafta pin alıp karşılaştır)
  - Parametre revizyonunu data-driven yapmak için referans

Data: data/pins.jsonl (append-only)
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from config import config
from pnl import summarize
from storage import Position

log = logging.getLogger(__name__)

DB_PATH = config.data_dir / "pins.jsonl"


# Pin'de saklanacak tunable parametre anahtarları
TUNABLE_KEYS = [
    # Score eşikleri
    "min_score_to_alert", "high_confidence_score",
    # Çıkış stratejisi
    "tp1_trigger", "tp1_sell",
    "tp2_trigger", "tp2_sell",
    "tp3_trigger", "tp3_sell",
    "stop_loss", "trailing_stop",
    # Risk yönetimi
    "buy_amount_sol", "slippage_bps",
    "buy_slippage_bps", "sell_slippage_bps",
    "max_open_positions", "max_total_exposure_sol",
    "daily_loss_stop_sol", "max_consecutive_losses",
    # Auto-trade
    "auto_trade_enabled", "auto_trade_min_score",
    "auto_trade_min_safety_score", "auto_trade_max_price_impact",
    # KATMAN 1 — early
    "early_min_liq", "early_max_liq",
    "early_min_price_h1", "early_min_txns_h1",
    "early_min_buy_ratio", "early_max_buy_ratio",
    # KATMAN 1 — trend
    "trend_min_liq", "trend_min_vol_h6",
    "trend_min_price_h6", "trend_min_price_h24",
    # Smart wallets
    "smart_min_buys_for_inject",
    "smart_exit_window_min", "smart_exit_min_sol",
    "wallet_auto_disable_quality", "wallet_auto_disable_min_samples",
    # Hold-time safety
    "hold_liq_drain_pct", "hold_top10_spike_pp",
    # Pyramid
    "pyramid_enabled", "pyramid_max_adds",
    "pyramid_trigger_step_pct", "pyramid_size_ratio",
    # Adaptive sizing
    "adaptive_sizing_enabled", "adaptive_sizing_min_samples",
    # ML
    "ml_enabled", "ml_win_threshold_pct", "ml_min_samples", "ml_max_score_points",
    # Jito / execution
    "jito_enabled", "jito_tip_lamports",
    "priority_fee_level", "max_priority_fee_lamports",
    # PumpPortal
    "pumpportal_enabled", "pumpportal_buy_amount_sol", "pumpportal_slippage_pct",
]


@dataclass
class Pin:
    name: str
    ts: float
    created_by: str  # "manual" or "auto"
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    perf_snapshot: dict[str, Any] = field(default_factory=dict)
    notes: str = ""


def _snapshot_config() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in TUNABLE_KEYS:
        if hasattr(config, k):
            val = getattr(config, k)
            # JSON serializable yap
            if isinstance(val, (int, float, bool, str)) or val is None:
                out[k] = val
            else:
                out[k] = str(val)
    return out


def _to_summary_dict(summary: dict) -> dict:
    """summarize() çıktısını JSON-serializable hale getir."""
    out: dict = {"total": summary.get("total", 0), "days": summary.get("days", 0)}
    overall = summary.get("overall")
    if overall is not None:
        out["win_rate"] = overall.win_rate
        out["total_pnl_sol"] = overall.total_pnl_sol
        out["avg_pnl_pct"] = overall.avg_pnl_pct
        out["best_pnl_pct"] = overall.best_pnl_pct
        out["worst_pnl_pct"] = overall.worst_pnl_pct
    return out


def _snapshot_perf(
    real_positions: list[Position],
    paper_positions: list[Position] | None,
) -> dict[str, Any]:
    out = {
        "real_all_time": _to_summary_dict(summarize(real_positions, days=0)),
        "real_7d": _to_summary_dict(summarize(real_positions, days=7)),
    }
    if paper_positions is not None:
        out["paper_all_time"] = _to_summary_dict(summarize(paper_positions, days=0))
        out["paper_7d"] = _to_summary_dict(summarize(paper_positions, days=7))
    return out


def create_pin(
    name: str,
    real_positions: list[Position],
    paper_positions: list[Position] | None,
    notes: str = "",
    by: str = "manual",
) -> Pin:
    pin = Pin(
        name=name,
        ts=time.time(),
        created_by=by,
        config_snapshot=_snapshot_config(),
        perf_snapshot=_snapshot_perf(real_positions, paper_positions),
        notes=notes,
    )
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DB_PATH.open("a") as f:
        f.write(json.dumps(asdict(pin)) + "\n")
    log.info("pin created: %s (n_real=%d)", name, len(real_positions))
    return pin


def list_pins() -> list[Pin]:
    if not DB_PATH.exists():
        return []
    pins: list[Pin] = []
    with DB_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                pins.append(Pin(**data))
            except (json.JSONDecodeError, TypeError, KeyError) as e:
                log.debug("pin parse error: %s", e)
                continue
    return pins


def find_pin(name: str) -> Pin | None:
    for p in list_pins():
        if p.name == name:
            return p
    return None


def diff_pins(a: Pin, b: Pin) -> dict[str, tuple[Any, Any]]:
    diffs: dict[str, tuple[Any, Any]] = {}
    keys = set(a.config_snapshot) | set(b.config_snapshot)
    for k in keys:
        av = a.config_snapshot.get(k)
        bv = b.config_snapshot.get(k)
        if av != bv:
            diffs[k] = (av, bv)
    return diffs


def format_pins_list(pins: list[Pin]) -> str:
    if not pins:
        return (
            "📭 Henüz pin yok. <code>/pin &lt;ad&gt;</code> ile snapshot al.\n"
            "Otomatik haftalık pin'ler de gelir."
        )
    recent = sorted(pins, key=lambda p: -p.ts)[:20]
    lines = [f"📌 <b>Son {len(recent)} pin</b> (toplam <code>{len(pins)}</code>)"]
    for p in recent:
        age = (time.time() - p.ts) / 3600
        age_str = f"{age:.0f}h" if age < 48 else f"{age / 24:.1f}d"
        real_n = p.perf_snapshot.get("real_all_time", {}).get("total", 0)
        paper_n = p.perf_snapshot.get("paper_all_time", {}).get("total", 0)
        tag = "🤖" if p.created_by == "auto" else "✋"
        lines.append(
            f"{tag} <b>{p.name}</b>  <i>{age_str}</i>  "
            f"real:<code>{real_n}</code> paper:<code>{paper_n}</code>"
        )
    lines.append("\n<i>Detay: <code>/pin show &lt;ad&gt;</code>  "
                 "Karşılaştır: <code>/pin diff &lt;a&gt; &lt;b&gt;</code></i>")
    return "\n".join(lines)


def format_pin_detail(p: Pin) -> str:
    lines = [
        f"📌 <b>{p.name}</b>",
        f"Tarih: <code>{time.strftime('%Y-%m-%d %H:%M', time.localtime(p.ts))}</code>",
        f"Tip: <code>{p.created_by}</code>",
    ]
    if p.notes:
        lines.append(f"Not: <i>{p.notes}</i>")
    lines.append("")
    lines.append("<b>Performance</b>")
    for key, label in [
        ("real_all_time", "real (tüm)"),
        ("real_7d", "real (7g)"),
        ("paper_all_time", "paper (tüm)"),
        ("paper_7d", "paper (7g)"),
    ]:
        s = p.perf_snapshot.get(key, {})
        total = s.get("total", 0)
        if total > 0:
            lines.append(
                f"• {label}: n=<code>{total}</code> "
                f"WR=<code>{s.get('win_rate', 0):.0f}%</code> "
                f"net=<code>{s.get('total_pnl_sol', 0):+.4f}SOL</code> "
                f"avg=<code>{s.get('avg_pnl_pct', 0):+.1f}%</code>"
            )
    lines.append("")
    lines.append("<b>Key params</b>")
    spotlight = [
        "min_score_to_alert", "tp1_trigger", "tp2_trigger", "tp3_trigger",
        "stop_loss", "trailing_stop", "buy_amount_sol",
        "max_total_exposure_sol", "daily_loss_stop_sol",
        "auto_trade_enabled", "auto_trade_min_score",
        "smart_min_buys_for_inject",
    ]
    for k in spotlight:
        if k in p.config_snapshot:
            lines.append(f"• {k}: <code>{p.config_snapshot[k]}</code>")
    return "\n".join(lines)


def format_diff(a: Pin, b: Pin) -> str:
    diffs = diff_pins(a, b)
    if not diffs:
        return (
            f"📌 <b>{a.name}</b> vs <b>{b.name}</b>\n"
            "Config aynı — değişiklik yok."
        )
    lines = [f"📌 <b>{a.name}</b> → <b>{b.name}</b>"]
    a_age = (time.time() - a.ts) / 3600
    b_age = (time.time() - b.ts) / 3600
    lines.append(
        f"<i>{a_age:.0f}h önce → {b_age:.0f}h önce</i>\n"
    )
    lines.append(f"<b>Config farkları</b> ({len(diffs)})")
    for k, (av, bv) in sorted(diffs.items()):
        lines.append(f"• <code>{k}</code>: <code>{av}</code> → <code>{bv}</code>")

    # Perf farkı da göster
    def _perf_net(p: Pin, key: str) -> float:
        return p.perf_snapshot.get(key, {}).get("total_pnl_sol", 0)

    def _perf_wr(p: Pin, key: str) -> float:
        return p.perf_snapshot.get(key, {}).get("win_rate", 0)

    lines.append("\n<b>Perf delta</b>")
    for key, label in [("real_7d", "real 7g"), ("paper_7d", "paper 7g")]:
        a_net = _perf_net(a, key)
        b_net = _perf_net(b, key)
        a_wr = _perf_wr(a, key)
        b_wr = _perf_wr(b, key)
        if (a_net or b_net) or (a_wr or b_wr):
            lines.append(
                f"• {label}: net <code>{a_net:+.4f}</code> → <code>{b_net:+.4f}</code> "
                f"({b_net - a_net:+.4f}) | "
                f"WR <code>{a_wr:.0f}%</code> → <code>{b_wr:.0f}%</code> "
                f"({b_wr - a_wr:+.0f}pp)"
            )
    return "\n".join(lines)

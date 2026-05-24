"""Bot dashboard — küçük HTTP server.

aiohttp ile tek bir HTML endpoint sunar. Token-auth: URL'de ?token=XXX
parametresi DASHBOARD_TOKEN env'iyle eşleşmelidir.

Render deploy senaryosu:
  - Bot şu an Background Worker olarak çalışıyor (no inbound HTTP)
  - Dashboard'a erişim için Render'da Web Service'e çevir VEYA ayrı bir
    Web Service oluşturup aynı disk'i (data/) mount et
  - PORT env Render tarafından set edilir; biz onu otomatik alıyoruz

Default kapalı (DASHBOARD_ENABLED=false).
"""
from __future__ import annotations

import html
import json
import logging
import time

from aiohttp import web

from config import config
from pin import list_pins
from pnl import format_report, summarize

log = logging.getLogger(__name__)


def _check_token(request: web.Request) -> bool:
    if not config.dashboard_token:
        return True  # token yoksa serbest (dev mode)
    return request.query.get("token") == config.dashboard_token


def _html_escape(text: str) -> str:
    return html.escape(text)


async def _index(request: web.Request) -> web.Response:
    if not _check_token(request):
        return web.Response(text="forbidden", status=403)

    bot = request.app["bot"]
    # Snapshot data
    open_positions = bot.store.open_positions()
    closed_count = sum(1 for p in bot.store.positions if p.status == "closed")
    paper_open = len(bot.paper_store.open_positions()) if bot.paper_store else 0
    real_summary = summarize(bot.store.positions, days=0)
    paper_summary = (
        summarize(bot.paper_store.positions, days=0)
        if bot.paper_store else {"total": 0}
    )
    pins = list_pins()
    smart_count = len(bot.smart_store.wallets) if bot.smart_store else 0
    smart_active = (
        sum(1 for w in bot.smart_store.wallets.values() if not w.disabled)
        if bot.smart_store else 0
    )
    ml_text = ""
    if bot.ml_bundle is not None:
        ml_text = (
            f"ML model: n={bot.ml_bundle.n_samples} "
            f"acc={bot.ml_bundle.test_accuracy * 100:.0f}%"
        )
    else:
        ml_text = "ML model: yok"

    overall = real_summary.get("overall")
    net_sol = overall.total_pnl_sol if overall else 0.0
    wr = overall.win_rate if overall else 0

    body = f"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="utf-8">
<title>Memecoin Sniper Dashboard</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, sans-serif;
    background: #0d1117; color: #c9d1d9;
    margin: 0; padding: 24px; max-width: 1100px; margin: auto;
  }}
  h1 {{ color: #58a6ff; }}
  h2 {{ color: #79c0ff; border-bottom: 1px solid #30363d; padding-bottom: 4px; }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 12px; margin: 16px 0;
  }}
  .card {{
    background: #161b22; border: 1px solid #30363d;
    padding: 14px; border-radius: 6px;
  }}
  .card .label {{ color: #8b949e; font-size: 12px; }}
  .card .value {{ font-size: 22px; font-weight: 600; }}
  .pos {{ color: #3fb950; }}
  .neg {{ color: #f85149; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ padding: 6px 10px; text-align: left; border-bottom: 1px solid #30363d; }}
  th {{ color: #8b949e; font-weight: 500; font-size: 12px; }}
  code {{ background: #21262d; padding: 2px 6px; border-radius: 3px; font-size: 12px; }}
  .small {{ color: #8b949e; font-size: 12px; }}
</style>
</head>
<body>
<h1>🤖 Memecoin Sniper Dashboard</h1>
<p class="small">Snapshot: {_html_escape(time.strftime('%Y-%m-%d %H:%M:%S'))} UTC</p>

<div class="grid">
  <div class="card">
    <div class="label">Açık pozisyon (real)</div>
    <div class="value">{len(open_positions)}</div>
  </div>
  <div class="card">
    <div class="label">Kapanmış (real)</div>
    <div class="value">{closed_count}</div>
  </div>
  <div class="card">
    <div class="label">Real net PnL</div>
    <div class="value {'pos' if net_sol >= 0 else 'neg'}">{net_sol:+.4f} SOL</div>
  </div>
  <div class="card">
    <div class="label">Real win rate</div>
    <div class="value">{wr:.0f}%</div>
  </div>
  <div class="card">
    <div class="label">Açık pozisyon (paper)</div>
    <div class="value">{paper_open}</div>
  </div>
  <div class="card">
    <div class="label">Paper kapanmış</div>
    <div class="value">{paper_summary.get('total', 0)}</div>
  </div>
  <div class="card">
    <div class="label">Smart wallets (aktif)</div>
    <div class="value">{smart_active} / {smart_count}</div>
  </div>
  <div class="card">
    <div class="label">Pin sayısı</div>
    <div class="value">{len(pins)}</div>
  </div>
</div>

<h2>Açık pozisyonlar</h2>
<table>
<thead><tr><th>Symbol</th><th>Skor</th><th>Profile</th><th>Harcanan SOL</th><th>TP hits</th></tr></thead>
<tbody>
"""
    for p in open_positions[:30]:
        tps = ",".join(str(h.level) for h in p.tp_hits) or "—"
        body += (
            f"<tr><td><b>${_html_escape(p.symbol)}</b></td>"
            f"<td>{p.score:.0f}</td>"
            f"<td>{_html_escape(p.profile)}</td>"
            f"<td>{p.sol_spent:.4f}</td>"
            f"<td>{_html_escape(tps)}</td></tr>"
        )
    body += "</tbody></table>"

    body += f"<h2>Son pinler</h2><table><thead><tr><th>Ad</th><th>Tarih</th><th>Tip</th><th>Real n</th><th>Paper n</th></tr></thead><tbody>"
    for p in sorted(pins, key=lambda x: -x.ts)[:10]:
        ts_str = time.strftime("%Y-%m-%d %H:%M", time.gmtime(p.ts))
        real_n = p.perf_snapshot.get("real_all_time", {}).get("total", 0)
        paper_n = p.perf_snapshot.get("paper_all_time", {}).get("total", 0)
        body += (
            f"<tr><td><b>{_html_escape(p.name)}</b></td>"
            f"<td>{ts_str}</td>"
            f"<td>{_html_escape(p.created_by)}</td>"
            f"<td>{real_n}</td>"
            f"<td>{paper_n}</td></tr>"
        )
    body += "</tbody></table>"

    body += f"<h2>ML</h2><p>{_html_escape(ml_text)}</p>"
    body += "<p class='small'>Auto-refresh: 30s</p>"
    body += "<script>setTimeout(function(){location.reload();},30000);</script>"
    body += "</body></html>"
    return web.Response(text=body, content_type="text/html")


async def _api_status(request: web.Request) -> web.Response:
    if not _check_token(request):
        return web.Response(text="forbidden", status=403)
    bot = request.app["bot"]
    summary = summarize(bot.store.positions, days=0)
    overall = summary.get("overall")
    data = {
        "ts": time.time(),
        "open_positions": len(bot.store.open_positions()),
        "closed_positions": sum(1 for p in bot.store.positions if p.status == "closed"),
        "net_sol": overall.total_pnl_sol if overall else 0.0,
        "win_rate": overall.win_rate if overall else 0.0,
        "smart_wallets": len(bot.smart_store.wallets) if bot.smart_store else 0,
        "breaker_halted": bot.breaker.state.halted,
        "ml_model_loaded": bot.ml_bundle is not None,
    }
    return web.json_response(data)


def make_app(bot) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/", _index)
    app.router.add_get("/api/status", _api_status)
    return app


async def start_dashboard(bot) -> "tuple[web.AppRunner, web.TCPSite] | None":
    if not config.dashboard_enabled:
        return None
    app = make_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(config.dashboard_render_port or config.dashboard_port)
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("dashboard started on 0.0.0.0:%d", port)
    return runner, site


async def stop_dashboard(handles) -> None:
    if not handles:
        return
    runner, site = handles
    try:
        await site.stop()
    except Exception:
        pass
    try:
        await runner.cleanup()
    except Exception:
        pass

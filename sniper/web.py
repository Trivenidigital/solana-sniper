"""PnL Dashboard — lightweight web UI served via aiohttp."""

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from sniper.config import Settings


def _get_db_path() -> str:
    settings = Settings()
    return str(settings.SNIPER_DB_PATH)


def _query(sql: str, params: tuple = ()) -> list[dict]:
    """Sync SQLite query for the web server (separate connection)."""
    db_path = _get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _scalar(sql: str, params: tuple = ()):
    db_path = _get_db_path()
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Solana Sniper — PnL Dashboard</title>
<meta http-equiv="refresh" content="30">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #0d1117; color: #c9d1d9; font-family: 'SF Mono', 'Fira Code', monospace; padding: 20px; }
  h1 { color: #58a6ff; margin-bottom: 20px; font-size: 1.4em; }
  h2 { color: #8b949e; margin: 20px 0 10px; font-size: 1.1em; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 24px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }
  .card .label { color: #8b949e; font-size: 0.75em; text-transform: uppercase; letter-spacing: 1px; }
  .card .value { font-size: 1.5em; font-weight: bold; margin-top: 4px; }
  .positive { color: #3fb950; }
  .negative { color: #f85149; }
  .neutral { color: #58a6ff; }
  table { width: 100%; border-collapse: collapse; margin-top: 8px; }
  th { text-align: left; padding: 8px 12px; background: #161b22; color: #8b949e; font-size: 0.75em;
       text-transform: uppercase; letter-spacing: 1px; border-bottom: 1px solid #30363d; }
  td { padding: 8px 12px; border-bottom: 1px solid #21262d; font-size: 0.85em; }
  tr:hover { background: #161b22; }
  .tag { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.7em; font-weight: bold; }
  .tag-open { background: #1f6feb33; color: #58a6ff; }
  .tag-closed { background: #21262d; color: #8b949e; }
  .tag-sl { background: #f8514933; color: #f85149; }
  .tag-tp { background: #3fb95033; color: #3fb950; }
  .tag-trail { background: #d2a82833; color: #d2a828; }
  .tag-paper { background: #8b949e33; color: #8b949e; }
  .tag-live { background: #3fb95033; color: #3fb950; }
  .footer { margin-top: 30px; color: #484f58; font-size: 0.75em; }
  a { color: #58a6ff; text-decoration: none; }
  a:hover { text-decoration: underline; }
</style>
</head>
<body>

<h1>Solana Sniper — PnL Dashboard</h1>

<div class="cards">
  <div class="card">
    <div class="label">Open Positions</div>
    <div class="value neutral">{{ open_count }}</div>
  </div>
  <div class="card">
    <div class="label">Total Exposure</div>
    <div class="value neutral">{{ "%.4f"|format(exposure) }} SOL</div>
  </div>
  <div class="card">
    <div class="label">Realized PnL</div>
    <div class="value {{ 'positive' if realized_pnl >= 0 else 'negative' }}">
      {{ "%+.4f"|format(realized_pnl) }} SOL
    </div>
  </div>
  <div class="card">
    <div class="label">Total Trades</div>
    <div class="value neutral">{{ total_trades }}</div>
  </div>
  <div class="card">
    <div class="label">Win Rate</div>
    <div class="value {{ 'positive' if win_rate >= 50 else 'negative' }}">
      {{ "%.0f"|format(win_rate) }}%
    </div>
  </div>
  <div class="card">
    <div class="label">Avg PnL</div>
    <div class="value {{ 'positive' if avg_pnl >= 0 else 'negative' }}">
      {{ "%+.1f"|format(avg_pnl) }}%
    </div>
  </div>
</div>

{% if open_positions %}
<h2>Open Positions</h2>
<table>
  <tr>
    <th>Token</th><th>Entry SOL</th><th>Tokens</th><th>Opened</th><th>Trailing</th><th>Partial</th><th>Mode</th>
  </tr>
  {% for p in open_positions %}
  <tr>
    <td><strong>{{ p.token_name }}</strong> ({{ p.ticker }})</td>
    <td>{{ "%.4f"|format(p.entry_sol) }}</td>
    <td>{{ "{:,.0f}".format(p.entry_token_amount) }}</td>
    <td>{{ p.opened_at[:16] }}</td>
    <td>{{ "YES" if p.trailing_active else "—" }}</td>
    <td>{{ "DONE" if p.partial_exit_done else "—" }}</td>
    <td><span class="tag {{ 'tag-paper' if p.paper else 'tag-live' }}">{{ "PAPER" if p.paper else "LIVE" }}</span></td>
  </tr>
  {% endfor %}
</table>
{% endif %}

<h2>Closed Positions</h2>
{% if closed_positions %}
<table>
  <tr>
    <th>Token</th><th>Entry</th><th>Exit</th><th>PnL SOL</th><th>PnL %</th><th>Reason</th><th>Duration</th><th>Mode</th>
  </tr>
  {% for p in closed_positions %}
  <tr>
    <td><strong>{{ p.token_name }}</strong> ({{ p.ticker }})</td>
    <td>{{ "%.4f"|format(p.entry_sol) }}</td>
    <td>{{ "%.4f"|format(p.exit_sol or 0) }}</td>
    <td class="{{ 'positive' if (p.pnl_sol or 0) >= 0 else 'negative' }}">
      {{ "%+.4f"|format(p.pnl_sol or 0) }}
    </td>
    <td class="{{ 'positive' if (p.pnl_pct or 0) >= 0 else 'negative' }}">
      {{ "%+.1f"|format(p.pnl_pct or 0) }}%
    </td>
    <td>
      <span class="tag
        {% if p.exit_reason == 'stop_loss' %}tag-sl
        {% elif p.exit_reason == 'take_profit' %}tag-tp
        {% elif p.exit_reason == 'trailing_stop' %}tag-trail
        {% else %}tag-closed{% endif %}">
        {{ (p.exit_reason or "manual") | upper }}
      </span>
    </td>
    <td>{{ p.duration or "—" }}</td>
    <td><span class="tag {{ 'tag-paper' if p.paper else 'tag-live' }}">{{ "PAPER" if p.paper else "LIVE" }}</span></td>
  </tr>
  {% endfor %}
</table>
{% else %}
<p style="color: #484f58;">No closed positions yet.</p>
{% endif %}

<h2>Recent Trades</h2>
{% if recent_trades %}
<table>
  <tr><th>Time</th><th>Side</th><th>SOL</th><th>Tokens</th><th>TX</th></tr>
  {% for t in recent_trades %}
  <tr>
    <td>{{ t.executed_at[:16] }}</td>
    <td class="{{ 'positive' if t.side == 'buy' else 'negative' }}">{{ t.side | upper }}</td>
    <td>{{ "%.4f"|format(t.sol_amount) }}</td>
    <td>{{ "{:,.0f}".format(t.token_amount) }}</td>
    <td>
      {% if t.tx_signature and not t.tx_signature.startswith('paper') %}
        <a href="https://solscan.io/tx/{{ t.tx_signature }}" target="_blank">{{ t.tx_signature[:12] }}...</a>
      {% else %}
        {{ (t.tx_signature or "—")[:20] }}
      {% endif %}
    </td>
  </tr>
  {% endfor %}
</table>
{% else %}
<p style="color: #484f58;">No trades yet.</p>
{% endif %}

<div class="footer">
  Auto-refreshes every 30s &bull; {{ now[:19] }} UTC
</div>

</body>
</html>"""


async def handle_dashboard(request: web.Request) -> web.Response:
    """Render the PnL dashboard."""
    from jinja2 import Template

    open_positions = _query("SELECT * FROM positions WHERE status='open' ORDER BY opened_at DESC")
    closed_positions = _query("SELECT * FROM positions WHERE status='closed' ORDER BY closed_at DESC LIMIT 50")
    recent_trades = _query("SELECT * FROM trades ORDER BY executed_at DESC LIMIT 20")

    open_count = len(open_positions)
    exposure = sum(p["entry_sol"] for p in open_positions)
    realized_pnl = sum(p.get("pnl_sol", 0) or 0 for p in closed_positions)

    winners = sum(1 for p in closed_positions if (p.get("pnl_sol", 0) or 0) > 0)
    total_closed = len(closed_positions)
    win_rate = (winners / total_closed * 100) if total_closed > 0 else 0
    avg_pnl = (sum(p.get("pnl_pct", 0) or 0 for p in closed_positions) / total_closed) if total_closed > 0 else 0
    total_trades = _scalar("SELECT COUNT(*) FROM trades")

    # Calculate durations for closed positions
    for p in closed_positions:
        if p.get("opened_at") and p.get("closed_at"):
            try:
                opened = datetime.fromisoformat(p["opened_at"])
                closed = datetime.fromisoformat(p["closed_at"])
                delta = closed - opened
                hours = delta.total_seconds() / 3600
                if hours < 1:
                    p["duration"] = f"{int(delta.total_seconds() / 60)}m"
                elif hours < 24:
                    p["duration"] = f"{hours:.1f}h"
                else:
                    p["duration"] = f"{hours / 24:.1f}d"
            except Exception:
                p["duration"] = "—"
        else:
            p["duration"] = "—"

    template = Template(DASHBOARD_HTML)
    html = template.render(
        open_count=open_count,
        exposure=exposure,
        realized_pnl=realized_pnl,
        total_trades=total_trades,
        win_rate=win_rate,
        avg_pnl=avg_pnl,
        open_positions=open_positions,
        closed_positions=closed_positions,
        recent_trades=recent_trades,
        now=datetime.now(timezone.utc).isoformat(),
    )
    return web.Response(text=html, content_type="text/html")


async def handle_api(request: web.Request) -> web.Response:
    """JSON API endpoint for programmatic access."""
    open_positions = _query("SELECT * FROM positions WHERE status='open'")
    closed_positions = _query("SELECT * FROM positions WHERE status='closed' ORDER BY closed_at DESC LIMIT 50")

    exposure = sum(p["entry_sol"] for p in open_positions)
    realized_pnl = sum(p.get("pnl_sol", 0) or 0 for p in closed_positions)
    winners = sum(1 for p in closed_positions if (p.get("pnl_sol", 0) or 0) > 0)
    total_closed = len(closed_positions)

    data = {
        "open_positions": len(open_positions),
        "exposure_sol": round(exposure, 4),
        "realized_pnl_sol": round(realized_pnl, 4),
        "win_rate": round(winners / total_closed * 100, 1) if total_closed > 0 else 0,
        "total_trades": _scalar("SELECT COUNT(*) FROM trades"),
        "positions": open_positions + closed_positions,
    }
    return web.json_response(data)


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_dashboard)
    app.router.add_get("/api", handle_api)
    return app


def main():
    """Run the dashboard web server."""
    import argparse

    parser = argparse.ArgumentParser(description="Solana Sniper PnL Dashboard")
    parser.add_argument("--port", type=int, default=8080, help="Port (default: 8080)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host (default: 0.0.0.0)")
    args = parser.parse_args()

    app = create_app()
    print(f"Dashboard running at http://localhost:{args.port}")
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()

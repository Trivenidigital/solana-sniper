"""PnL Dashboard — web UI with live prices, charts, scanner status, and trade actions."""

import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from aiohttp import web

from sniper.config import Settings

logger = logging.getLogger(__name__)

# Rate limiting for trade endpoint
_last_trade_time: float = 0.0
_TRADE_RATE_LIMIT_SECONDS = 10.0

JUPITER_QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"
SOL_MINT = "So11111111111111111111111111111111111111112"


def _get_settings() -> Settings:
    return Settings()


def _get_db_path() -> str:
    return str(_get_settings().SNIPER_DB_PATH)


def _query(sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(_get_db_path())
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _scalar(sql: str, params: tuple = ()):
    conn = sqlite3.connect(_get_db_path())
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def _format_tokens(raw_amount: float) -> str:
    if raw_amount >= 1e15:
        return f"{raw_amount / 1e9:,.0f}"
    elif raw_amount >= 1e9:
        return f"{raw_amount / 1e6:,.0f}"
    elif raw_amount >= 1e6:
        return f"{raw_amount / 1e6:,.2f}"
    else:
        return f"{raw_amount:,.0f}"


async def _get_sol_price() -> float:
    try:
        usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        url = f"{JUPITER_QUOTE_URL}?inputMint={SOL_MINT}&outputMint={usdc}&amount=1000000000&slippageBps=50"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return int(data.get("outAmount", 0)) / 1e6
    except Exception:
        pass
    return 0.0


async def _get_token_value_sol(contract_address: str, token_amount: int) -> float | None:
    if token_amount <= 0:
        return 0.0
    try:
        url = f"{JUPITER_QUOTE_URL}?inputMint={contract_address}&outputMint={SOL_MINT}&amount={token_amount}&slippageBps=300"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return int(data.get("outAmount", 0)) / 1e9
    except Exception:
        pass
    return None


async def _get_sol_balance() -> float:
    try:
        settings = _get_settings()
        async with aiohttp.ClientSession() as session:
            payload = {
                "jsonrpc": "2.0", "id": 1, "method": "getBalance",
                "params": [str(_get_wallet_pubkey())]
            }
            async with session.post(settings.SOLANA_RPC_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                return data.get("result", {}).get("value", 0) / 1e9
    except Exception:
        return 0.0


def _get_wallet_pubkey() -> str:
    import json as _json
    settings = _get_settings()
    try:
        from solders.keypair import Keypair
        secret = _json.loads(settings.KEYPAIR_PATH.read_text())
        kp = Keypair.from_bytes(bytes(secret))
        return str(kp.pubkey())
    except Exception:
        return "unknown"


def _get_scout_stats() -> dict:
    settings = _get_settings()
    scout_path = str(settings.SCOUT_DB_PATH)
    try:
        conn = sqlite3.connect(scout_path)
        conn.row_factory = sqlite3.Row
        total_candidates = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        solana_candidates = conn.execute("SELECT COUNT(*) FROM candidates WHERE chain='solana'").fetchone()[0]
        total_alerts = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        top_scored = [dict(r) for r in conn.execute(
            "SELECT token_name, ticker, quant_score, holder_count, liquidity_usd "
            "FROM candidates WHERE chain='solana' AND quant_score > 0 "
            "ORDER BY quant_score DESC LIMIT 5"
        ).fetchall()]
        conn.close()
        return {
            "total_candidates": total_candidates,
            "solana_candidates": solana_candidates,
            "total_alerts": total_alerts,
            "top_scored": top_scored,
        }
    except Exception:
        return {"total_candidates": 0, "solana_candidates": 0, "total_alerts": 0, "top_scored": []}


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Solana Sniper Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  :root {
    --bg: #0d1117;
    --bg2: #161b22;
    --bg-card: #1c2333;
    --border: #30363d;
    --text: #e6edf3;
    --text-muted: #8b949e;
    --cyan: #58a6ff;
    --green: #3fb950;
    --red: #f85149;
    --yellow: #d29922;
    --purple: #bc8cff;
    --orange: #f0883e;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace; }
  .container { max-width: 1280px; margin: 0 auto; padding: 16px; }

  /* Header */
  .header { display: flex; align-items: center; gap: 12px; margin-bottom: 20px; }
  .header h1 { color: var(--cyan); font-size: 1.3em; }
  .status-dot { width: 10px; height: 10px; border-radius: 50%; }
  .status-dot.connected { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .status-dot.disconnected { background: var(--red); box-shadow: 0 0 6px var(--red); }
  .wallet-badge { margin-left: auto; background: var(--bg2); border: 1px solid var(--border); padding: 6px 12px;
                  border-radius: 6px; font-size: 0.75em; color: var(--text-muted); }
  .wallet-badge strong { color: var(--green); }

  /* Metric Cards */
  .metrics { display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; margin-bottom: 24px; }
  .metric-card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  .metric-card .label { color: var(--text-muted); font-size: 0.65em; text-transform: uppercase; letter-spacing: 1.2px; font-weight: 600; }
  .metric-card .value { font-size: 1.8rem; font-weight: 700; margin-top: 6px; line-height: 1; }
  .metric-card .sub { font-size: 0.7em; color: var(--text-muted); margin-top: 4px; }
  .positive { color: var(--green); }
  .negative { color: var(--red); }
  .neutral { color: var(--cyan); }
  .cb-on { color: var(--red); font-weight: 700; }
  .cb-off { color: var(--green); font-weight: 700; }

  /* Performance Section */
  .performance { display: grid; grid-template-columns: 3fr 2fr; gap: 16px; margin-bottom: 24px; }
  .chart-panel { background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  .chart-panel h3 { color: var(--text-muted); font-size: 0.8em; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; }
  .chart-wrap { position: relative; height: 280px; }

  /* Sections */
  .section { margin-bottom: 24px; }
  .section h2 { color: var(--text-muted); font-size: 1em; margin-bottom: 10px; display: flex; align-items: center; gap: 8px; }
  .section h2 .badge { background: var(--cyan); color: #fff; padding: 2px 8px; border-radius: 10px; font-size: 0.7em; }

  /* Tables */
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; padding: 8px 10px; background: var(--bg2); color: var(--text-muted); font-size: 0.7em;
       text-transform: uppercase; letter-spacing: 1px; border-bottom: 1px solid var(--border); position: sticky; top: 0; }
  td { padding: 8px 10px; border-bottom: 1px solid var(--border); font-size: 0.82em; }
  tr:hover { background: var(--bg2); }
  .table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }

  /* Tags */
  .tag { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 0.65em; font-weight: 600; }
  .tag-live { background: #3fb95022; color: var(--green); }
  .tag-paper { background: #8b949e22; color: var(--text-muted); }
  .tag-sl { background: #f8514922; color: var(--red); }
  .tag-tp { background: #3fb95022; color: var(--green); }
  .tag-trail { background: #d2992222; color: var(--yellow); }
  .tag-closed { background: #8b949e22; color: var(--text-muted); }

  /* Scanner */
  .scanner-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; }
  .scanner-item { background: var(--bg2); border: 1px solid var(--border); border-radius: 6px; padding: 10px; }
  .scanner-item .name { font-weight: 600; font-size: 0.85em; }
  .scanner-item .details { font-size: 0.72em; color: var(--text-muted); margin-top: 4px; }
  .scanner-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 10px; }
  .scanner-cards .metric-card { padding: 12px; }
  .scanner-cards .metric-card .value { font-size: 1.4rem; }

  /* Trade form */
  .trade-form { background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  .form-row { display: flex; gap: 10px; flex-wrap: wrap; align-items: end; margin-bottom: 10px; }
  .form-group { flex: 1; min-width: 150px; }
  .form-group label { display: block; font-size: 0.7em; color: var(--text-muted); text-transform: uppercase; margin-bottom: 4px; }
  .form-group input, .form-group select { width: 100%; padding: 8px; background: var(--bg); border: 1px solid var(--border);
    color: var(--text); border-radius: 6px; font-size: 0.85em; font-family: inherit; }
  .btn { padding: 8px 20px; border: none; border-radius: 6px; font-weight: 600; cursor: pointer; font-size: 0.85em; }
  .btn-buy { background: var(--green); color: #fff; }
  .btn-sell { background: var(--red); color: #fff; }
  .btn:hover { opacity: 0.85; }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .trade-status { font-size: 0.8em; margin-top: 8px; padding: 8px; border-radius: 6px; display: none; }
  .trade-status.show { display: block; }
  .trade-status.success { background: #3fb95022; color: var(--green); }
  .trade-status.error { background: #f8514922; color: var(--red); }

  /* Footer */
  .footer { margin-top: 20px; padding-top: 12px; border-top: 1px solid var(--border);
            color: var(--text-muted); font-size: 0.7em; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px; }

  /* Mobile */
  @media (max-width: 768px) {
    .container { padding: 10px; }
    .metrics { grid-template-columns: repeat(3, 1fr); }
    .metric-card .value { font-size: 1.2rem; }
    .performance { grid-template-columns: 1fr; }
    .header { flex-wrap: wrap; }
    .wallet-badge { margin-left: 0; }
    .form-row { flex-direction: column; }
    td, th { padding: 6px 8px; font-size: 0.75em; }
  }
  @media (max-width: 480px) {
    .metrics { grid-template-columns: repeat(2, 1fr); }
  }
</style>
</head>
<body>
<div class="container">

<!-- Header -->
<div class="header">
  <h1>Solana Sniper</h1>
  <div class="status-dot {{ 'connected' if is_connected else 'disconnected' }}"></div>
  <div class="wallet-badge">
    {{ wallet_pubkey[:8] }}...{{ wallet_pubkey[-4:] }} &bull; <strong>{{ "%.4f"|format(sol_balance) }} SOL</strong>
    {% if sol_price > 0 %}<span style="color:var(--text-muted)"> (${{ "%.2f"|format(sol_balance * sol_price) }})</span>{% endif %}
  </div>
</div>

<!-- Top Metrics Row -->
<div class="metrics">
  <div class="metric-card">
    <div class="label">Net P&amp;L (48H)</div>
    <div class="value {{ 'positive' if net_pnl_usd >= 0 else 'negative' }}">${{ "%.2f"|format(net_pnl_usd) }}</div>
    <div class="sub">TODAY: ${{ "%.2f"|format(today_pnl_usd) }}</div>
  </div>
  <div class="metric-card">
    <div class="label">Total Trades</div>
    <div class="value neutral">{{ total_closed }}</div>
    <div class="sub">closed positions</div>
  </div>
  <div class="metric-card">
    <div class="label">Win Rate</div>
    <div class="value {{ 'positive' if win_rate >= 50 else 'negative' }}">{{ "%.0f"|format(win_rate) }}%</div>
    <div class="sub">{{ winners }}/{{ total_closed }}</div>
  </div>
  <div class="metric-card">
    <div class="label">Profit Factor</div>
    <div class="value {{ 'positive' if profit_factor >= 1 else 'negative' }}">{{ "%.2f"|format(profit_factor) }}</div>
    <div class="sub">{{ ">1 = profitable" if profit_factor >= 1 else "<1 = losing" }}</div>
  </div>
  <div class="metric-card">
    <div class="label">Expectancy</div>
    <div class="value {{ 'positive' if expectancy >= 0 else 'negative' }}">{{ "%.4f"|format(expectancy) }}</div>
    <div class="sub">SOL/trade (${{ "%.2f"|format(expectancy * sol_price) }})</div>
  </div>
  <div class="metric-card">
    <div class="label">Bankroll</div>
    <div class="value neutral">{{ "%.4f"|format(sol_balance) }}</div>
    <div class="sub">SOL (${{ "%.2f"|format(sol_balance * sol_price) }})</div>
  </div>
  <div class="metric-card">
    <div class="label">Exposure</div>
    <div class="value neutral">{{ "%.4f"|format(exposure) }}</div>
    <div class="sub">SOL in {{ open_count }} position{{ 's' if open_count != 1 else '' }}</div>
  </div>
  <div class="metric-card">
    <div class="label">Circuit Breaker</div>
    <div class="value {{ 'cb-on' if circuit_breaker else 'cb-off' }}">{{ "ON" if circuit_breaker else "OFF" }}</div>
    <div class="sub">{{ open_count }}/{{ max_open }} slots</div>
  </div>
</div>

<!-- Performance Section -->
<div class="performance">
  <div class="chart-panel">
    <h3>Equity Curve (48H)</h3>
    <div class="chart-wrap">
      <canvas id="equityChart"></canvas>
    </div>
  </div>
  <div class="chart-panel">
    <h3>Exit Reasons</h3>
    <div class="chart-wrap">
      <canvas id="exitDonut"></canvas>
    </div>
  </div>
</div>

<!-- Open Positions with Live PnL -->
{% if open_positions %}
<div class="section">
  <h2>Open Positions <span class="badge">{{ open_count }}</span></h2>
  <div class="table-wrap">
  <table>
    <tr>
      <th>Token</th><th>Entry</th><th>Current Value</th><th>Entry MC</th><th>Current MC</th><th>PnL</th><th>Opened</th><th>Trail</th><th>Mode</th><th>Action</th>
    </tr>
    {% for p in open_positions %}
    <tr>
      <td><a href="https://dexscreener.com/solana/{{ p.contract_address }}" target="_blank" style="color:inherit;text-decoration:none;"><strong>{{ p.token_name }}</strong> ({{ p.ticker }})</a></td>
      <td>{{ "%.4f"|format(p.entry_sol) }} SOL</td>
      <td>
        {% if p.current_value is not none %}
          {{ "%.4f"|format(p.current_value) }} SOL
        {% else %}
          <span style="color:var(--text-muted)">—</span>
        {% endif %}
      </td>
      <td>
        {% if p.entry_mcap_usd %}
          ${{ "{:,}".format(p.entry_mcap_usd | int) }}
        {% else %}
          <span style="color:var(--text-muted)">—</span>
        {% endif %}
      </td>
      <td>
        {% if p.current_mcap %}
          ${{ "{:,}".format(p.current_mcap | int) }}
        {% else %}
          <span style="color:var(--text-muted)">—</span>
        {% endif %}
      </td>
      <td>
        {% if p.current_value is not none %}
          {% set pnl = p.current_value - p.entry_sol %}
          {% set pnl_pct = (pnl / p.entry_sol * 100) if p.entry_sol > 0 else 0 %}
          {% set pnl_usd = pnl * sol_price %}
          <span class="{{ 'positive' if pnl >= 0 else 'negative' }}">
            {{ "%+.4f"|format(pnl) }} ({{ "%+.1f"|format(pnl_pct) }}%)
            <br><small>${{ "%+.2f"|format(pnl_usd) }}</small>
          </span>
        {% else %}
          <span style="color:var(--text-muted)">—</span>
        {% endif %}
      </td>
      <td>{{ p.opened_at[:16] }}</td>
      <td>{{ "Active" if p.trailing_active else "—" }}</td>
      <td><span class="tag {{ 'tag-paper' if p.paper else 'tag-live' }}">{{ "PAPER" if p.paper else "LIVE" }}</span></td>
      <td>
        <button
            onclick="closePosition('{{ p.contract_address }}', '{{ p.token_name }}', {{ p.entry_token_amount }})"
            class="close-btn"
            style="background:#ff4444;color:white;border:none;padding:4px 10px;border-radius:4px;cursor:pointer;font-size:12px;">
            Close
        </button>
      </td>
    </tr>
    {% endfor %}
  </table>
  </div>
</div>
{% endif %}

<!-- Manual Trade -->
<div class="section">
  <h2>Manual Trade</h2>
  <div class="trade-form">
    <div class="form-row">
      <div class="form-group">
        <label>Token Address</label>
        <input type="text" id="trade-token" placeholder="Contract address...">
      </div>
      <div class="form-group" style="max-width:120px">
        <label>SOL Amount</label>
        <input type="number" id="trade-amount" value="0.2" step="0.01" min="0.01">
      </div>
      <div>
        <button class="btn btn-buy" onclick="executeTrade('buy')">Buy</button>
        <button class="btn btn-sell" onclick="executeTrade('sell')">Sell</button>
      </div>
    </div>
    <div class="trade-status" id="trade-status"></div>
  </div>
</div>

<!-- Closed Positions -->
<div class="section">
  <h2>Closed Positions</h2>
  {% if closed_positions %}
  <div class="table-wrap">
  <table>
    <tr><th>Token</th><th>Entry</th><th>Exit</th><th>PnL SOL</th><th>PnL %</th><th>Reason</th><th>Duration</th></tr>
    {% for p in closed_positions %}
    <tr>
      <td><a href="https://dexscreener.com/solana/{{ p.contract_address }}" target="_blank" style="color:inherit;text-decoration:none;"><strong>{{ p.token_name }}</strong></a></td>
      <td>{{ "%.4f"|format(p.entry_sol) }}</td>
      <td>{{ "%.4f"|format(p.exit_sol or 0) }}</td>
      <td class="{{ 'positive' if (p.pnl_sol or 0) >= 0 else 'negative' }}">{{ "%+.4f"|format(p.pnl_sol or 0) }}<br><small>${{ "%+.2f"|format((p.pnl_sol or 0) * sol_price) }}</small></td>
      <td class="{{ 'positive' if (p.pnl_pct or 0) >= 0 else 'negative' }}">{{ "%+.1f"|format(p.pnl_pct or 0) }}%</td>
      <td><span class="tag {% if p.exit_reason == 'stop_loss' %}tag-sl{% elif p.exit_reason == 'take_profit' %}tag-tp{% elif p.exit_reason == 'trailing_stop' %}tag-trail{% else %}tag-closed{% endif %}">{{ (p.exit_reason or "manual") | upper }}</span></td>
      <td>{{ p.duration or "—" }}</td>
    </tr>
    {% endfor %}
  </table>
  </div>
  {% else %}
  <p style="color:var(--text-muted);font-size:0.85em">No closed positions yet.</p>
  {% endif %}
</div>

<!-- Scanner Status -->
<div class="section">
  <h2>Scanner Status</h2>
  <div class="scanner-cards">
    <div class="metric-card">
      <div class="label">Total Candidates</div>
      <div class="value neutral">{{ scanner.total_candidates }}</div>
    </div>
    <div class="metric-card">
      <div class="label">Solana Tokens</div>
      <div class="value neutral">{{ scanner.solana_candidates }}</div>
    </div>
    <div class="metric-card">
      <div class="label">Alerts Fired</div>
      <div class="value {{ 'positive' if scanner.total_alerts > 0 else 'neutral' }}">{{ scanner.total_alerts }}</div>
    </div>
  </div>
  {% if scanner.top_scored %}
  <div class="scanner-grid">
    {% for t in scanner.top_scored %}
    <div class="scanner-item">
      <div class="name">{{ t.token_name }} ({{ t.ticker }})</div>
      <div class="details">Score: {{ t.quant_score }} &bull; Holders: {{ t.holder_count }} &bull; Liq: ${{ "{:,.0f}".format(t.liquidity_usd or 0) }}</div>
    </div>
    {% endfor %}
  </div>
  {% endif %}
</div>

<!-- Recent Trades -->
<div class="section">
  <h2>Recent Trades</h2>
  {% if recent_trades %}
  <div class="table-wrap">
  <table>
    <tr><th>Time</th><th>Side</th><th>SOL</th><th>TX</th></tr>
    {% for t in recent_trades %}
    <tr>
      <td>{{ t.executed_at[:16] }}</td>
      <td class="{{ 'positive' if t.side == 'buy' else 'negative' }}">{{ t.side | upper }}</td>
      <td>{{ "%.4f"|format(t.sol_amount) }}</td>
      <td>
        {% if t.tx_signature and not t.tx_signature.startswith('paper') %}
          <a href="https://solscan.io/tx/{{ t.tx_signature }}" target="_blank" style="color:var(--cyan)">{{ t.tx_signature[:16] }}...</a>
        {% else %}{{ (t.tx_signature or "—")[:20] }}{% endif %}
      </td>
    </tr>
    {% endfor %}
  </table>
  </div>
  {% else %}
  <p style="color:var(--text-muted);font-size:0.85em">No trades yet.</p>
  {% endif %}
</div>

<div class="footer">
  <span>Auto-refreshes every 30s</span>
  <span>{{ now[:19] }} UTC</span>
</div>

</div>

<script>
// Equity Curve Chart
const eqCtx = document.getElementById('equityChart').getContext('2d');
const equityData = {{ equity_json | safe }};
if (equityData.length > 0) {
  new Chart(eqCtx, {
    data: {
      labels: equityData.map(d => d.hour),
      datasets: [
        {
          type: 'line', label: 'Equity', data: equityData.map(d => d.cumulative),
          borderColor: '#58a6ff', backgroundColor: 'rgba(88,166,255,0.1)',
          fill: true, tension: 0.3, pointRadius: 2, order: 1
        },
        {
          type: 'bar', label: 'Hourly P&L', data: equityData.map(d => d.hourly),
          backgroundColor: equityData.map(d => d.hourly >= 0 ? 'rgba(63,185,80,0.6)' : 'rgba(248,81,73,0.6)'),
          order: 2
        }
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: {
        x: { ticks: { color: '#8b949e', maxRotation: 45 }, grid: { color: '#30363d' } },
        y: { ticks: { color: '#8b949e' }, grid: { color: '#30363d' } }
      },
      plugins: { legend: { labels: { color: '#e6edf3' } } }
    }
  });
} else {
  eqCtx.font = '14px monospace';
  eqCtx.fillStyle = '#8b949e';
  eqCtx.textAlign = 'center';
  eqCtx.fillText('No data yet', eqCtx.canvas.width / 2, 140);
}

// Exit Reason Donut
const exitCtx = document.getElementById('exitDonut').getContext('2d');
const exitData = {{ exit_json | safe }};
const exitColors = {
  'trailing_stop': '#d29922', 'take_profit': '#3fb950', 'stop_loss': '#f85149',
  'sell_pressure': '#f0883e', 'momentum_lost': '#8b949e', 'rug_detected': '#f85149',
  'pump_window_expired': '#d29922', 'manual': '#8b949e', 'breakeven_stop': '#58a6ff',
  'max_hold_exceeded': '#bc8cff', 'unsellable': '#f85149',
  'conviction_liq_floor': '#f0883e'
};
if (exitData.length > 0) {
  new Chart(exitCtx, {
    type: 'doughnut',
    data: {
      labels: exitData.map(d => d.exit_reason),
      datasets: [{
        data: exitData.map(d => d.count),
        backgroundColor: exitData.map(d => exitColors[d.exit_reason] || '#8b949e')
      }]
    },
    options: {
      cutout: '60%', responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: 'bottom', labels: { color: '#e6edf3', padding: 12, usePointStyle: true } } }
    }
  });
} else {
  exitCtx.font = '14px monospace';
  exitCtx.fillStyle = '#8b949e';
  exitCtx.textAlign = 'center';
  exitCtx.fillText('No data yet', exitCtx.canvas.width / 2, 140);
}

// Trade execution
async function executeTrade(side) {
  const token = document.getElementById('trade-token').value.trim();
  const amount = parseFloat(document.getElementById('trade-amount').value);
  const status = document.getElementById('trade-status');

  if (!token) { showStatus('Enter a token address', 'error'); return; }
  if (!amount || amount <= 0) { showStatus('Enter a valid amount', 'error'); return; }

  showStatus('Executing ' + side + '...', 'success');

  let apiKey = sessionStorage.getItem('dashboard_api_key');
  if (!apiKey) {
    apiKey = prompt('Enter Dashboard API Key:');
    if (!apiKey) { showStatus('API key required', 'error'); return; }
    sessionStorage.setItem('dashboard_api_key', apiKey);
  }

  try {
    const resp = await fetch('/api/trade', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-API-Key': apiKey},
      body: JSON.stringify({side, token, amount})
    });
    const data = await resp.json();
    if (data.error) {
      if (resp.status === 401) { sessionStorage.removeItem('dashboard_api_key'); }
      showStatus('Error: ' + data.error, 'error');
    } else {
      showStatus('Success! TX: ' + (data.tx || 'done'), 'success');
      setTimeout(() => location.reload(), 2000);
    }
  } catch(e) {
    showStatus('Request failed: ' + e.message, 'error');
  }
}

async function closePosition(contractAddress, tokenName, tokenAmount) {
  if (!confirm('Close position for ' + tokenName + '?')) return;

  let apiKey = sessionStorage.getItem('dashboard_api_key');
  if (!apiKey) {
    apiKey = prompt('Enter Dashboard API Key:');
    if (!apiKey) { showStatus('API key required', 'error'); return; }
    sessionStorage.setItem('dashboard_api_key', apiKey);
  }

  showStatus('Closing ' + tokenName + '...', 'success');

  try {
    const resp = await fetch('/api/trade', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-API-Key': apiKey },
      body: JSON.stringify({ side: 'sell', token: contractAddress, amount: tokenAmount })
    });
    const data = await resp.json();
    if (data.error) {
      if (resp.status === 401) { sessionStorage.removeItem('dashboard_api_key'); }
      showStatus('Error: ' + data.error, 'error');
    } else {
      showStatus(tokenName + ' closed! TX: ' + (data.tx || 'done'), 'success');
      setTimeout(() => location.reload(), 2000);
    }
  } catch(e) {
    showStatus('Request failed: ' + e.message, 'error');
  }
}

function showStatus(msg, type) {
  const el = document.getElementById('trade-status');
  el.textContent = msg;
  el.className = 'trade-status show ' + type;
}

// Auto-refresh
setTimeout(() => location.reload(), 30000);
</script>
</body>
</html>"""


async def handle_dashboard(request: web.Request) -> web.Response:
    from jinja2 import Template

    open_positions = _query("SELECT * FROM positions WHERE status='open' ORDER BY opened_at DESC")
    closed_positions = _query("SELECT * FROM positions WHERE status='closed' ORDER BY closed_at DESC LIMIT 50")
    recent_trades = _query("SELECT * FROM trades ORDER BY executed_at DESC LIMIT 20")

    # Fetch live data in parallel
    sol_price_task = _get_sol_price()
    sol_balance_task = _get_sol_balance()
    sol_price, sol_balance = await asyncio.gather(sol_price_task, sol_balance_task)

    # Fetch current values + market cap for open positions
    unrealized_pnl = 0.0
    for p in open_positions:
        val = await _get_token_value_sol(p["contract_address"], int(p["entry_token_amount"]))
        p["current_value"] = val
        p["current_mcap"] = None
        if val is not None:
            unrealized_pnl += val - p["entry_sol"]
        # Fetch market cap from DexScreener
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://api.dexscreener.com/tokens/v1/solana/{p['contract_address']}",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        dex_data = await resp.json()
                        if isinstance(dex_data, list) and dex_data:
                            p["current_mcap"] = float(dex_data[0].get("marketCap") or 0)
        except Exception:
            pass

    open_count = len(open_positions)
    exposure = sum(p["entry_sol"] for p in open_positions)
    realized_pnl = sum(p.get("pnl_sol", 0) or 0 for p in closed_positions)

    winners = sum(1 for p in closed_positions if (p.get("pnl_sol", 0) or 0) > 0)
    total_closed = len(closed_positions)
    win_rate = (winners / total_closed * 100) if total_closed > 0 else 0

    # Profit factor = gross wins / gross losses (>1 = profitable)
    gross_wins = sum((p.get("pnl_sol", 0) or 0) for p in closed_positions if (p.get("pnl_sol", 0) or 0) > 0)
    gross_losses = abs(sum((p.get("pnl_sol", 0) or 0) for p in closed_positions if (p.get("pnl_sol", 0) or 0) < 0))
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else 0

    # Expectancy = avg SOL per trade (positive = making money per trade)
    expectancy = (realized_pnl / total_closed) if total_closed > 0 else 0
    total_trades = _scalar("SELECT COUNT(*) FROM trades")

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

    scanner = _get_scout_stats()
    wallet_pubkey = _get_wallet_pubkey()

    # 48h realized P&L
    realized_48h = _scalar(
        "SELECT COALESCE(SUM(pnl_sol), 0) FROM positions WHERE status='closed' AND closed_at >= datetime('now', '-48 hours')"
    )

    # Today's realized P&L
    today_pnl = _scalar(
        "SELECT COALESCE(SUM(pnl_sol), 0) FROM positions WHERE status='closed' AND closed_at >= date('now')"
    )

    # Equity curve data (hourly buckets, last 48h)
    equity_rows = _query(
        "SELECT strftime('%Y-%m-%d %H:00', closed_at) as hour_bucket, SUM(pnl_sol) as hourly_pnl "
        "FROM positions WHERE status='closed' AND closed_at >= datetime('now', '-48 hours') "
        "GROUP BY hour_bucket ORDER BY hour_bucket"
    )
    equity_data = []
    cumulative = 0
    for row in equity_rows:
        hourly_pnl = row["hourly_pnl"] or 0
        cumulative += hourly_pnl
        equity_data.append({"hour": row["hour_bucket"][-5:], "cumulative": round(cumulative, 4), "hourly": round(hourly_pnl, 4)})

    # Exit reason breakdown (all-time)
    exit_reasons = _query(
        "SELECT exit_reason, COUNT(*) as count FROM positions "
        "WHERE status='closed' AND exit_reason IS NOT NULL "
        "GROUP BY exit_reason ORDER BY count DESC"
    )

    # NET P&L (48h realized + unrealized)
    net_pnl_sol = realized_48h + unrealized_pnl
    net_pnl_usd = net_pnl_sol * sol_price
    today_pnl_usd = today_pnl * sol_price

    # Circuit breaker: max positions OR consecutive loss streak
    settings = _get_settings()
    max_open = settings.MAX_OPEN_POSITIONS
    loss_streak = _scalar(
        """SELECT COUNT(*) FROM (
            SELECT pnl_pct FROM positions
            WHERE status='closed' AND paper=0 AND closed_at >= datetime('now', '-1 hour')
            ORDER BY closed_at DESC
        ) sub WHERE pnl_pct <= 0"""
    ) or 0
    # Check if it's a real streak (no wins mixed in)
    recent_closed = _query(
        "SELECT pnl_pct FROM positions WHERE status='closed' AND paper=0 AND closed_at >= datetime('now', '-1 hour') ORDER BY closed_at DESC LIMIT 10"
    )
    streak = 0
    for r in recent_closed:
        if (r.get("pnl_pct") or 0) <= 0:
            streak += 1
        else:
            break
    circuit_breaker = open_count >= max_open or streak >= 3

    # Connection status (any activity in last 5 min)
    last_activity = _scalar(
        "SELECT MAX(COALESCE(closed_at, opened_at)) FROM positions WHERE opened_at >= datetime('now', '-5 minutes') OR closed_at >= datetime('now', '-5 minutes')"
    )
    is_connected = last_activity is not None and last_activity != 0

    template = Template(DASHBOARD_HTML)
    template.globals['format_tokens'] = _format_tokens
    html = template.render(
        open_count=open_count, exposure=exposure, realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl, total_trades=total_trades,
        win_rate=win_rate, winners=winners, total_closed=total_closed,
        profit_factor=profit_factor, expectancy=expectancy,
        sol_price=sol_price, sol_balance=sol_balance,
        wallet_pubkey=wallet_pubkey, scanner=scanner,
        open_positions=open_positions, closed_positions=closed_positions,
        recent_trades=recent_trades, now=datetime.now(timezone.utc).isoformat(),
        net_pnl_usd=net_pnl_usd, today_pnl_usd=today_pnl_usd,
        circuit_breaker=circuit_breaker, max_open=max_open,
        is_connected=is_connected,
        equity_json=json.dumps(equity_data),
        exit_json=json.dumps([dict(r) for r in exit_reasons]),
    )
    return web.Response(text=html, content_type="text/html")


async def handle_api(request: web.Request) -> web.Response:
    open_positions = _query("SELECT * FROM positions WHERE status='open'")
    closed_positions = _query("SELECT * FROM positions WHERE status='closed' ORDER BY closed_at DESC LIMIT 50")

    exposure = sum(p["entry_sol"] for p in open_positions)
    realized_pnl = sum(p.get("pnl_sol", 0) or 0 for p in closed_positions)
    winners = sum(1 for p in closed_positions if (p.get("pnl_sol", 0) or 0) > 0)
    total_closed = len(closed_positions)
    gross_wins = sum((p.get("pnl_sol", 0) or 0) for p in closed_positions if (p.get("pnl_sol", 0) or 0) > 0)
    gross_losses = abs(sum((p.get("pnl_sol", 0) or 0) for p in closed_positions if (p.get("pnl_sol", 0) or 0) < 0))
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else 0
    expectancy = (realized_pnl / total_closed) if total_closed > 0 else 0

    data = {
        "open_positions": len(open_positions),
        "exposure_sol": round(exposure, 4),
        "realized_pnl_sol": round(realized_pnl, 4),
        "win_rate": round(winners / total_closed * 100, 1) if total_closed > 0 else 0,
        "profit_factor": round(profit_factor, 2),
        "expectancy_sol": round(expectancy, 4),
        "total_trades": _scalar("SELECT COUNT(*) FROM trades"),
        "positions": open_positions + closed_positions,
    }
    return web.json_response(data)


async def handle_trade(request: web.Request) -> web.Response:
    """Handle manual buy/sell from the dashboard."""
    global _last_trade_time

    # I2: Only allow trade requests from localhost
    peername = request.transport.get_extra_info("peername") if request.transport else None
    remote_ip = peername[0] if peername else request.remote
    if remote_ip not in ("127.0.0.1", "::1", "localhost"):
        logger.warning("Trade attempt from non-local IP: %s", remote_ip)
        return web.json_response({"error": "Trades only allowed from localhost"}, status=403)

    # Require API key for trade execution
    settings = _get_settings()
    if not settings.DASHBOARD_API_KEY:
        return web.json_response({"error": "Trading disabled — set DASHBOARD_API_KEY in .env"}, status=403)
    import hmac
    api_key = request.headers.get("X-API-Key", "")
    if not hmac.compare_digest(api_key, settings.DASHBOARD_API_KEY):
        logger.warning("Trade attempt with invalid API key from %s", remote_ip)
        return web.json_response({"error": "Unauthorized"}, status=401)

    # I2: Rate limiting — max 1 trade per 10 seconds
    now = time.monotonic()
    if now - _last_trade_time < _TRADE_RATE_LIMIT_SECONDS:
        remaining = _TRADE_RATE_LIMIT_SECONDS - (now - _last_trade_time)
        return web.json_response({"error": f"Rate limited — wait {remaining:.0f}s"}, status=429)

    try:
        body = await request.json()
        side = body.get("side", "buy")
        token = body.get("token", "")
        amount = float(body.get("amount", 0))

        if not token or amount <= 0:
            return web.json_response({"error": "Invalid token or amount"}, status=400)

        # I2: Log all trade attempts
        logger.info("Trade attempt: side=%s token=%s amount=%s ip=%s", side, token, amount, remote_ip)
        _last_trade_time = time.monotonic()

        from sniper.wallet import load_keypair
        from sniper.config import Settings
        from sniper.executor import execute_buy, execute_sell
        from solana.rpc.async_api import AsyncClient

        settings = Settings()
        # DR-003: Respect env PAPER_MODE setting, don't force live
        kp = load_keypair(settings.KEYPAIR_PATH)
        client = AsyncClient(settings.SOLANA_RPC_URL)

        async with aiohttp.ClientSession() as session:
            if side == "buy":
                tx_sig, tokens, _decimals = await execute_buy(client, kp, session, token, amount, settings)

                from sniper.db import Database
                from sniper.models import Position
                db = Database(settings.SNIPER_DB_PATH)
                await db.initialize()
                pos = Position(
                    contract_address=token, token_name=token[:12], ticker=token[:5],
                    entry_sol=amount, entry_token_amount=tokens, entry_tx=tx_sig, paper=settings.PAPER_MODE,
                    manual=True,
                )
                pos_id = await db.open_position(pos)
                await db.log_trade(pos_id, "buy", amount, tokens, tx_sig, None)
                await db.close()
            else:
                # For sell, find the open position and sell all tokens
                from sniper.db import Database
                db = Database(settings.SNIPER_DB_PATH)
                await db.initialize()
                pos = await db.get_open_position_by_address(token)
                if not pos:
                    await db.close()
                    return web.json_response({"error": "No open position for this token"}, status=400)
                token_amount = int(pos.entry_token_amount)
                tx_sig, sol_received = await execute_sell(client, kp, session, token, token_amount, settings)
                pnl_sol = sol_received - pos.entry_sol
                pnl_pct = (pnl_sol / pos.entry_sol * 100) if pos.entry_sol > 0 else 0
                await db.close_position(pos.id, sol_received, 0, tx_sig, "manual", pnl_sol, pnl_pct)
                await db.log_trade(pos.id, "sell", sol_received, float(token_amount), tx_sig, None)
                await db.close()

        await client.close()
        return web.json_response({"tx": tx_sig, "side": side})

    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_dashboard)
    app.router.add_get("/api", handle_api)
    app.router.add_post("/api/trade", handle_trade)
    return app


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Solana Sniper PnL Dashboard")
    parser.add_argument("--port", type=int, default=8080, help="Port (default: 8080)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host (default: 127.0.0.1, use nginx proxy for external)")
    args = parser.parse_args()

    app = create_app()
    print(f"Dashboard running at http://localhost:{args.port}")
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()

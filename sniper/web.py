"""PnL Dashboard — web UI with live prices, charts, scanner status, and trade actions."""

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from aiohttp import web

from sniper.config import Settings

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
<style>
  :root {
    --bg: #0d1117; --bg2: #161b22; --border: #30363d; --text: #c9d1d9;
    --muted: #484f58; --label: #8b949e; --blue: #58a6ff; --green: #3fb950;
    --red: #f85149; --yellow: #d2a828; --purple: #a371f7;
  }
  [data-theme="light"] {
    --bg: #ffffff; --bg2: #f6f8fa; --border: #d0d7de; --text: #1f2328;
    --muted: #656d76; --label: #636c76; --blue: #0969da; --green: #1a7f37;
    --red: #cf222e; --yellow: #9a6700; --purple: #8250df;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace; }
  .container { max-width: 1200px; margin: 0 auto; padding: 16px; }

  /* Header */
  .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; flex-wrap: wrap; gap: 10px; }
  .header h1 { color: var(--blue); font-size: 1.3em; }
  .header-actions { display: flex; gap: 8px; align-items: center; }
  .theme-btn { background: var(--bg2); border: 1px solid var(--border); color: var(--text); padding: 6px 12px;
               border-radius: 6px; cursor: pointer; font-size: 0.8em; }
  .theme-btn:hover { border-color: var(--blue); }
  .wallet-badge { background: var(--bg2); border: 1px solid var(--border); padding: 6px 12px;
                  border-radius: 6px; font-size: 0.75em; color: var(--label); }
  .wallet-badge strong { color: var(--green); }

  /* Cards */
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 20px; }
  .card { background: var(--bg2); border: 1px solid var(--border); border-radius: 8px; padding: 14px; }
  .card .label { color: var(--label); font-size: 0.7em; text-transform: uppercase; letter-spacing: 1px; }
  .card .value { font-size: 1.4em; font-weight: bold; margin-top: 4px; }
  .card .sub { font-size: 0.7em; color: var(--muted); margin-top: 2px; }
  .positive { color: var(--green); }
  .negative { color: var(--red); }
  .neutral { color: var(--blue); }

  /* Sections */
  .section { margin-bottom: 24px; }
  .section h2 { color: var(--label); font-size: 1em; margin-bottom: 10px; display: flex; align-items: center; gap: 8px; }
  .section h2 .badge { background: var(--blue); color: #fff; padding: 2px 8px; border-radius: 10px; font-size: 0.7em; }

  /* Tables */
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; padding: 8px 10px; background: var(--bg2); color: var(--label); font-size: 0.7em;
       text-transform: uppercase; letter-spacing: 1px; border-bottom: 1px solid var(--border); position: sticky; top: 0; }
  td { padding: 8px 10px; border-bottom: 1px solid var(--border); font-size: 0.82em; }
  tr:hover { background: var(--bg2); }
  .table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }

  /* Tags */
  .tag { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 0.65em; font-weight: 600; }
  .tag-live { background: #3fb95022; color: var(--green); }
  .tag-paper { background: #8b949e22; color: var(--label); }
  .tag-sl { background: #f8514922; color: var(--red); }
  .tag-tp { background: #3fb95022; color: var(--green); }
  .tag-trail { background: #d2a82822; color: var(--yellow); }
  .tag-closed { background: #8b949e22; color: var(--label); }

  /* Chart */
  .chart-container { background: var(--bg2); border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin-bottom: 20px; }
  .chart-bar { display: flex; align-items: center; gap: 8px; margin: 4px 0; }
  .chart-label { width: 120px; font-size: 0.75em; color: var(--label); text-align: right; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .chart-fill { height: 20px; border-radius: 3px; min-width: 2px; transition: width 0.3s; }
  .chart-value { font-size: 0.7em; color: var(--muted); white-space: nowrap; }

  /* Scanner */
  .scanner-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; }
  .scanner-item { background: var(--bg2); border: 1px solid var(--border); border-radius: 6px; padding: 10px; }
  .scanner-item .name { font-weight: 600; font-size: 0.85em; }
  .scanner-item .details { font-size: 0.72em; color: var(--label); margin-top: 4px; }

  /* Trade form */
  .trade-form { background: var(--bg2); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  .form-row { display: flex; gap: 10px; flex-wrap: wrap; align-items: end; margin-bottom: 10px; }
  .form-group { flex: 1; min-width: 150px; }
  .form-group label { display: block; font-size: 0.7em; color: var(--label); text-transform: uppercase; margin-bottom: 4px; }
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
            color: var(--muted); font-size: 0.7em; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px; }

  /* Mobile */
  @media (max-width: 768px) {
    .container { padding: 10px; }
    .cards { grid-template-columns: repeat(3, 1fr); }
    .card .value { font-size: 1.1em; }
    .header { flex-direction: column; align-items: flex-start; }
    .form-row { flex-direction: column; }
    td, th { padding: 6px 8px; font-size: 0.75em; }
    .chart-label { width: 80px; }
  }
  @media (max-width: 480px) {
    .cards { grid-template-columns: repeat(2, 1fr); }
  }
</style>
</head>
<body data-theme="dark">
<div class="container">

<!-- Header -->
<div class="header">
  <h1>Solana Sniper</h1>
  <div class="header-actions">
    <div class="wallet-badge">
      {{ wallet_pubkey[:8] }}...{{ wallet_pubkey[-4:] }} &bull; <strong>{{ "%.4f"|format(sol_balance) }} SOL</strong>
      {% if sol_price > 0 %}<span style="color:var(--muted)"> (${{ "%.2f"|format(sol_balance * sol_price) }})</span>{% endif %}
    </div>
    <button class="theme-btn" onclick="toggleTheme()">Toggle Theme</button>
  </div>
</div>

<!-- Summary Cards -->
<div class="cards">
  <div class="card">
    <div class="label">Open Positions</div>
    <div class="value neutral">{{ open_count }}</div>
  </div>
  <div class="card">
    <div class="label">Exposure</div>
    <div class="value neutral">{{ "%.4f"|format(exposure) }}</div>
    <div class="sub">SOL</div>
  </div>
  <div class="card">
    <div class="label">Unrealized PnL</div>
    <div class="value {{ 'positive' if unrealized_pnl >= 0 else 'negative' }}">{{ "%+.4f"|format(unrealized_pnl) }}</div>
    <div class="sub">SOL</div>
  </div>
  <div class="card">
    <div class="label">Realized PnL</div>
    <div class="value {{ 'positive' if realized_pnl >= 0 else 'negative' }}">{{ "%+.4f"|format(realized_pnl) }}</div>
    <div class="sub">SOL</div>
  </div>
  <div class="card">
    <div class="label">Win Rate</div>
    <div class="value {{ 'positive' if win_rate >= 50 else 'negative' }}">{{ "%.0f"|format(win_rate) }}%</div>
    <div class="sub">{{ winners }}/{{ total_closed }} trades</div>
  </div>
  <div class="card">
    <div class="label">SOL Price</div>
    <div class="value neutral">${{ "%.2f"|format(sol_price) }}</div>
  </div>
</div>

<!-- PnL Chart -->
{% if closed_positions %}
<div class="section">
  <h2>Trade PnL History</h2>
  <div class="chart-container">
    {% for p in closed_positions[:15] %}
    {% set pnl = p.pnl_pct or 0 %}
    {% set width = [([pnl if pnl > 0 else -pnl, 100] | min), 3] | max %}
    <div class="chart-bar">
      <div class="chart-label">{{ p.token_name[:12] }}</div>
      <div class="chart-fill {{ 'positive' if pnl >= 0 else 'negative' }}"
           style="width: {{ width }}%; background: {{ 'var(--green)' if pnl >= 0 else 'var(--red)' }}; opacity: 0.7;"></div>
      <div class="chart-value">{{ "%+.1f"|format(pnl) }}% ({{ "%+.4f"|format(p.pnl_sol or 0) }} SOL)</div>
    </div>
    {% endfor %}
  </div>
</div>
{% endif %}

<!-- Open Positions with Live PnL -->
{% if open_positions %}
<div class="section">
  <h2>Open Positions <span class="badge">{{ open_count }}</span></h2>
  <div class="table-wrap">
  <table>
    <tr>
      <th>Token</th><th>Entry</th><th>Current Value</th><th>PnL</th><th>Opened</th><th>Trail</th><th>Mode</th>
    </tr>
    {% for p in open_positions %}
    <tr>
      <td><strong>{{ p.token_name }}</strong> ({{ p.ticker }})</td>
      <td>{{ "%.4f"|format(p.entry_sol) }} SOL</td>
      <td>
        {% if p.current_value is not none %}
          {{ "%.4f"|format(p.current_value) }} SOL
        {% else %}
          <span style="color:var(--muted)">—</span>
        {% endif %}
      </td>
      <td>
        {% if p.current_value is not none %}
          {% set pnl = p.current_value - p.entry_sol %}
          {% set pnl_pct = (pnl / p.entry_sol * 100) if p.entry_sol > 0 else 0 %}
          <span class="{{ 'positive' if pnl >= 0 else 'negative' }}">
            {{ "%+.4f"|format(pnl) }} ({{ "%+.1f"|format(pnl_pct) }}%)
          </span>
        {% else %}
          <span style="color:var(--muted)">—</span>
        {% endif %}
      </td>
      <td>{{ p.opened_at[:16] }}</td>
      <td>{{ "Active" if p.trailing_active else "—" }}</td>
      <td><span class="tag {{ 'tag-paper' if p.paper else 'tag-live' }}">{{ "PAPER" if p.paper else "LIVE" }}</span></td>
    </tr>
    {% endfor %}
  </table>
  </div>
</div>
{% endif %}

<!-- Scanner Status -->
<div class="section">
  <h2>Scanner Status</h2>
  <div class="cards" style="margin-bottom:0">
    <div class="card">
      <div class="label">Total Candidates</div>
      <div class="value neutral">{{ scanner.total_candidates }}</div>
    </div>
    <div class="card">
      <div class="label">Solana Tokens</div>
      <div class="value neutral">{{ scanner.solana_candidates }}</div>
    </div>
    <div class="card">
      <div class="label">Alerts Fired</div>
      <div class="value {{ 'positive' if scanner.total_alerts > 0 else 'neutral' }}">{{ scanner.total_alerts }}</div>
    </div>
  </div>
  {% if scanner.top_scored %}
  <div class="scanner-grid" style="margin-top:10px">
    {% for t in scanner.top_scored %}
    <div class="scanner-item">
      <div class="name">{{ t.token_name }} ({{ t.ticker }})</div>
      <div class="details">Score: {{ t.quant_score }} &bull; Holders: {{ t.holder_count }} &bull; Liq: ${{ "{:,.0f}".format(t.liquidity_usd or 0) }}</div>
    </div>
    {% endfor %}
  </div>
  {% endif %}
</div>

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
      <td><strong>{{ p.token_name }}</strong></td>
      <td>{{ "%.4f"|format(p.entry_sol) }}</td>
      <td>{{ "%.4f"|format(p.exit_sol or 0) }}</td>
      <td class="{{ 'positive' if (p.pnl_sol or 0) >= 0 else 'negative' }}">{{ "%+.4f"|format(p.pnl_sol or 0) }}</td>
      <td class="{{ 'positive' if (p.pnl_pct or 0) >= 0 else 'negative' }}">{{ "%+.1f"|format(p.pnl_pct or 0) }}%</td>
      <td><span class="tag {% if p.exit_reason == 'stop_loss' %}tag-sl{% elif p.exit_reason == 'take_profit' %}tag-tp{% elif p.exit_reason == 'trailing_stop' %}tag-trail{% else %}tag-closed{% endif %}">{{ (p.exit_reason or "manual") | upper }}</span></td>
      <td>{{ p.duration or "—" }}</td>
    </tr>
    {% endfor %}
  </table>
  </div>
  {% else %}
  <p style="color:var(--muted);font-size:0.85em">No closed positions yet.</p>
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
          <a href="https://solscan.io/tx/{{ t.tx_signature }}" target="_blank" style="color:var(--blue)">{{ t.tx_signature[:16] }}...</a>
        {% else %}{{ (t.tx_signature or "—")[:20] }}{% endif %}
      </td>
    </tr>
    {% endfor %}
  </table>
  </div>
  {% else %}
  <p style="color:var(--muted);font-size:0.85em">No trades yet.</p>
  {% endif %}
</div>

<div class="footer">
  <span>Auto-refreshes every 30s</span>
  <span>{{ now[:19] }} UTC</span>
</div>

</div>

<script>
function toggleTheme() {
  const body = document.body;
  const current = body.getAttribute('data-theme');
  const next = current === 'dark' ? 'light' : 'dark';
  body.setAttribute('data-theme', next);
  localStorage.setItem('theme', next);
}
// Restore theme
const saved = localStorage.getItem('theme');
if (saved) document.body.setAttribute('data-theme', saved);

async function executeTrade(side) {
  const token = document.getElementById('trade-token').value.trim();
  const amount = parseFloat(document.getElementById('trade-amount').value);
  const status = document.getElementById('trade-status');

  if (!token) { showStatus('Enter a token address', 'error'); return; }
  if (!amount || amount <= 0) { showStatus('Enter a valid amount', 'error'); return; }

  showStatus('Executing ' + side + '...', 'success');

  try {
    const resp = await fetch('/api/trade', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-API-Key': 'DASHBOARD_KEY_HERE'},
      body: JSON.stringify({side, token, amount})
    });
    const data = await resp.json();
    if (data.error) {
      showStatus('Error: ' + data.error, 'error');
    } else {
      showStatus('Success! TX: ' + (data.tx || 'done'), 'success');
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

    # Fetch current values for open positions
    unrealized_pnl = 0.0
    for p in open_positions:
        val = await _get_token_value_sol(p["contract_address"], int(p["entry_token_amount"]))
        p["current_value"] = val
        if val is not None:
            unrealized_pnl += val - p["entry_sol"]

    open_count = len(open_positions)
    exposure = sum(p["entry_sol"] for p in open_positions)
    realized_pnl = sum(p.get("pnl_sol", 0) or 0 for p in closed_positions)

    winners = sum(1 for p in closed_positions if (p.get("pnl_sol", 0) or 0) > 0)
    total_closed = len(closed_positions)
    win_rate = (winners / total_closed * 100) if total_closed > 0 else 0
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

    template = Template(DASHBOARD_HTML)
    template.globals['format_tokens'] = _format_tokens
    html = template.render(
        open_count=open_count, exposure=exposure, realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl, total_trades=total_trades,
        win_rate=win_rate, winners=winners, total_closed=total_closed,
        sol_price=sol_price, sol_balance=sol_balance,
        wallet_pubkey=wallet_pubkey, scanner=scanner,
        open_positions=open_positions, closed_positions=closed_positions,
        recent_trades=recent_trades, now=datetime.now(timezone.utc).isoformat(),
    )
    return web.Response(text=html, content_type="text/html")


async def handle_api(request: web.Request) -> web.Response:
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


async def handle_trade(request: web.Request) -> web.Response:
    """Handle manual buy/sell from the dashboard."""
    # Require API key for trade execution
    settings = _get_settings()
    if not settings.DASHBOARD_API_KEY:
        return web.json_response({"error": "Trading disabled — set DASHBOARD_API_KEY in .env"}, status=403)
    api_key = request.headers.get("X-API-Key", "")
    if api_key != settings.DASHBOARD_API_KEY:
        return web.json_response({"error": "Unauthorized"}, status=401)
    try:
        body = await request.json()
        side = body.get("side", "buy")
        token = body.get("token", "")
        amount = float(body.get("amount", 0))

        if not token or amount <= 0:
            return web.json_response({"error": "Invalid token or amount"}, status=400)

        from sniper.wallet import load_keypair
        from sniper.config import Settings
        from sniper.executor import execute_buy, execute_sell
        from solana.rpc.async_api import AsyncClient

        settings = Settings()
        settings.PAPER_MODE = False
        kp = load_keypair(settings.KEYPAIR_PATH)
        client = AsyncClient(settings.SOLANA_RPC_URL)

        async with aiohttp.ClientSession() as session:
            if side == "buy":
                tx_sig, tokens = await execute_buy(client, kp, session, token, amount, settings)

                from sniper.db import Database
                from sniper.models import Position
                db = Database(settings.SNIPER_DB_PATH)
                await db.initialize()
                pos = Position(
                    contract_address=token, token_name=token[:12], ticker=token[:5],
                    entry_sol=amount, entry_token_amount=tokens, entry_tx=tx_sig, paper=False,
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
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host (default: 0.0.0.0)")
    args = parser.parse_args()

    app = create_app()
    print(f"Dashboard running at http://localhost:{args.port}")
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()

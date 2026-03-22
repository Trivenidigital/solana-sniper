# Dashboard Redesign — Design Spec

**Date:** 2026-03-22
**Scope:** `solana-sniper` repo, `sniper/web.py`
**Goal:** Redesign the sniper dashboard to match Srini's Scalper style — dark theme, top metrics row, equity curve, exit reason donut.

---

## Overview

Visual overhaul of the existing aiohttp-based dashboard. Same architecture (embedded HTML in web.py, no build step), new layout matching the Scalper reference screenshot.

**Reference:** `~/Desktop/Screenshot 2026-03-21 at 9.31.45 PM.png`

---

## Layout

### Header Bar
- Title: "Solana Sniper"
- Connection status dot (green = bot running, based on last position update recency)
- Dark theme only (drop light/dark toggle)

### Top Metrics Row (6 cards, horizontal)

| Card | Value | Sub-text |
|------|-------|----------|
| NET P&L (48H) | Realized + unrealized P&L in USD | TODAY: today's P&L |
| TOTAL TRADES | Count of closed positions | — |
| WIN RATE | Percentage (winners/total) | e.g., "22/42" |
| BANKROLL | Current SOL balance + USD value | — |
| EXPOSURE | Total SOL in open positions | — |
| CIRCUIT BREAKER | ON/OFF based on max positions reached | — |

**P&L calculation:**
- Realized: sum of `pnl_sol` for closed positions in last 48h
- Unrealized: sum of (current_value_sol - entry_sol) for open positions
- NET P&L = realized + unrealized
- Convert to USD via SOL price from Jupiter
- TODAY = same calculation but filtered to today (UTC midnight)

### Performance Section (2 columns)

**Left column (60%): Equity Curve**
- Line chart using Chart.js (CDN, no build)
- X-axis: time (last 48h, hourly buckets)
- Two datasets:
  - **Equity line** (cyan): cumulative P&L over time
  - **Daily P&L bars** (green/red): P&L per hour bucket
- Data source: closed positions `closed_at` + `pnl_sol`, converted to USD

**Right column (40%): Exit Reason Donut**
- Donut chart (Chart.js)
- Segments: trailing_stop, take_profit, stop_loss, sell_pressure, momentum_lost, pump_window_expired, manual, rug_detected, breakeven_stop, other
- Colors: distinct per reason (green for profits, red for losses, yellow for neutral)
- Data source: count of closed positions grouped by `exit_reason`
- Shows all-time data (not just 48h)

### Open Positions Table
Same columns as current:
- Token (name + ticker)
- Entry SOL
- Current Value SOL
- PnL % (color-coded green/red)
- Age (minutes/hours)
- Trail status (active/inactive)
- Mode (paper/live)

### Closed Positions Table
Same as current with exit reason tags color-coded.

### Scanner Status
Keep existing section: total candidates, alerts fired, top 5 scored tokens.

### Manual Trade Form
Keep as-is: token address, SOL amount, buy/sell buttons, API key protection.

---

## Visual Style

**Color palette (matching Scalper):**
```css
--bg-primary: #0d1117;
--bg-secondary: #161b22;
--bg-card: #1c2333;
--border: #30363d;
--text-primary: #e6edf3;
--text-secondary: #8b949e;
--accent-cyan: #58a6ff;
--accent-green: #3fb950;
--accent-red: #f85149;
--accent-yellow: #d29922;
--accent-purple: #bc8cff;
```

**Typography:**
- Font: `-apple-system, BlinkMacSystemFont, 'Segoe UI', monospace`
- Metrics: large bold numbers (2rem)
- Labels: uppercase, small, muted color
- Sub-values: smaller, secondary color

**Cards:**
- Background: `--bg-card`
- Border: 1px solid `--border`
- Border-radius: 8px
- Padding: 16px

---

## Data Flow

```
Browser loads /
  -> handle_dashboard() queries:
     1. sniper.db: open positions, closed positions (48h + all-time), trades
     2. Jupiter API: SOL price, token values for open positions
     3. Solana RPC: wallet balance
     4. scout.db: scanner stats
  -> Compute:
     - realized_pnl_48h, unrealized_pnl, today_pnl
     - equity_curve_data (hourly buckets, last 48h)
     - exit_reason_counts (all-time)
     - win_rate, total_trades
     - circuit_breaker status (open_count >= MAX_OPEN_POSITIONS)
  -> Render template with all data
  -> Auto-refresh every 30s via JS
```

---

## Chart.js Integration

**CDN:** `https://cdn.jsdelivr.net/npm/chart.js@4`

**Equity curve config:**
```javascript
{
  type: 'line',
  data: {
    labels: hourly_labels,  // ["12:00", "13:00", ...]
    datasets: [
      { label: 'Equity', data: cumulative_pnl, borderColor: '#58a6ff', fill: false },
      { label: 'Daily P&L', data: hourly_pnl, type: 'bar', backgroundColor: pnl_colors }
    ]
  },
  options: {
    scales: { y: { grid: { color: '#30363d' } } },
    plugins: { legend: { labels: { color: '#e6edf3' } } }
  }
}
```

**Donut config:**
```javascript
{
  type: 'doughnut',
  data: {
    labels: exit_reasons,
    datasets: [{ data: counts, backgroundColor: reason_colors }]
  },
  options: { cutout: '60%', plugins: { legend: { position: 'bottom', labels: { color: '#e6edf3' } } } }
}
```

---

## New DB Queries Needed

### Equity curve data (hourly P&L buckets, last 48h)
```sql
SELECT
  strftime('%Y-%m-%d %H:00:00', closed_at) as hour_bucket,
  SUM(pnl_sol) as hourly_pnl
FROM positions
WHERE status = 'closed' AND closed_at >= datetime('now', '-48 hours')
GROUP BY hour_bucket
ORDER BY hour_bucket
```

### Exit reason breakdown (all-time)
```sql
SELECT exit_reason, COUNT(*) as count
FROM positions
WHERE status = 'closed' AND exit_reason IS NOT NULL
GROUP BY exit_reason
ORDER BY count DESC
```

### Today's P&L
```sql
SELECT COALESCE(SUM(pnl_sol), 0) as today_pnl
FROM positions
WHERE status = 'closed' AND closed_at >= date('now')
```

### 48h realized P&L
```sql
SELECT COALESCE(SUM(pnl_sol), 0) as pnl_48h
FROM positions
WHERE status = 'closed' AND closed_at >= datetime('now', '-48 hours')
```

---

## Files Changed

| File | Change |
|------|--------|
| `sniper/web.py` | Full template rewrite (DASHBOARD_HTML), new DB queries, new template context variables |

Single file change. No new files needed.

---

## What Stays the Same

- aiohttp framework
- Embedded HTML template approach
- `/api` and `/api/trade` endpoints
- 30-second auto-refresh
- Manual trade form with API key
- Scanner status section
- Mobile responsive breakpoints
- systemd service configuration

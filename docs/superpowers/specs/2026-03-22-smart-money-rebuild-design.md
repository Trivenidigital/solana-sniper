# Smart Money Copy Trading Rebuild — Design Spec

**Date:** 2026-03-22
**Scope:** Both `coinpump-scout` and `solana-sniper` repos
**Goal:** Never miss a smart wallet buy. Two-way detection with full pipeline coverage.

---

## Overview

Two-directional smart money integration:

1. **Scanner → Smart Money (Direction 1):** Scout discovers token → checks if tracked wallets bought it → score boost
2. **Smart Money → Scanner (Direction 2):** Tracked wallet buys token → inject into scout pipeline → full scoring + quality gate + safety → alert → sniper buys

Both directions run in parallel. Regular scanner flow is untouched.

---

## Step 0: Prerequisites

Before starting this work:

1. **Resolve merge conflicts on `feat/jito-mev-protection`** — `sniper/main.py` has conflict markers (`<<<<<<<`). Fix, test, merge to main.
2. **Re-add `smart_money_signals` dict** — Jito branch removed it. This rebuild re-introduces it with the new accumulating structure (Section 4d).
3. **Enable SQLite WAL mode** — Neither scout nor sniper currently sets WAL mode. Both need `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000` in their DB init, since the sniper will now WRITE to scout's DB.
4. **Startup validation** — When `COPY_TRADE_ENABLED=true`, fail fast with a clear error if `SMART_MONEY_WALLETS` is empty. Don't silently monitor zero wallets.

---

## Architecture

```
                    ┌──────────────────────────────────────┐
                    │         SHARED CONFIG                 │
                    │  SMART_MONEY_WALLETS env var          │
                    │  (read by both scout + sniper)        │
                    └──────────┬───────────┬───────────────┘
                               │           │
              ┌────────────────▼──┐   ┌────▼────────────────┐
              │   SCOUT           │   │   SNIPER             │
              │                   │   │                      │
              │  Direction 1:     │   │  Direction 2:        │
              │  Token found →    │   │  WebSocket monitors  │
              │  check_smart_     │   │  wallets → detect    │
              │  money() → +20    │   │  swap → write to     │
              │  score boost      │   │  smart_money_        │
              │                   │   │  injections table    │
              │  Direction 2:     │   │  in scout DB         │
              │  New ingestion    │   │                      │
              │  source reads     │   │  Also: boost +20     │
              │  injections table │   │  per wallet when     │
              │  → full pipeline  │   │  processing signals  │
              │  → alert          │   │                      │
              └───────────────────┘   └──────────────────────┘
```

---

## 1. Shared Wallet Configuration

**Single source of truth:** `SMART_MONEY_WALLETS` env var (comma-separated).

Both services read from this. No more empty hardcoded sets/lists.

```env
# .env (both services)
SMART_MONEY_WALLETS=54Pz1e35z9uoFdnxtzjp7xZQoFiofqhdayQWBMN7dsuy,7pwKymyhUwdSLVXVLbBaQKxkxL86naC7nLLaiy11p3eh,4uENWUN5ieDfq8r3qGPSbDHByMPe6fny2Wp5cMSSsESd,2tgUbS9UMoQD6GkDZBiqKYCURnGrSb6ocYwRABrSJUvY
```

### Changes:

**Scout `config.py`:**
- Add `SMART_MONEY_WALLETS: str = ""` setting
- `onchain_signals.py` reads from config instead of empty `set()`

**Sniper `config.py`:**
- Rename `COPY_TRADE_WALLETS` → `SMART_MONEY_WALLETS` (or alias both)
- `copy_trader.py` reads from config instead of empty `DEFAULT_TRACKED_WALLETS`

---

## 2. Direction 1: Scanner → Smart Money Check (Fix Existing)

**Current state:** `onchain_signals.py` has the logic but `SMART_MONEY_WALLETS` is empty.

### Changes to `scout/ingestion/onchain_signals.py`:

1. Load wallets from `settings.SMART_MONEY_WALLETS` instead of hardcoded empty set
2. Keep existing `check_smart_money()` logic — fetches last 50 SWAP txns via Helius, checks if buyers are in wallet set
3. Score boost unified to graduated +20/wallet (same as Direction 2 — see Section 3c)

**No architectural change needed — just wire up the config and update scorer.**

---

## 3. Direction 2: Smart Money → Scanner Injection (New)

### 3a. Sniper writes to `smart_money_injections` table

When copy_trader detects a tracked wallet swap:

1. Extract token mint address
2. Write to `smart_money_injections` table in **scout's SQLite DB**
3. Include: token_mint, wallet_address, detected_at, tx_signature

**New table in scout DB:**

```sql
CREATE TABLE IF NOT EXISTS smart_money_injections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_mint TEXT NOT NULL,
    wallet_address TEXT NOT NULL,
    tx_signature TEXT,
    source TEXT DEFAULT 'websocket',  -- 'websocket' or 'backfill'
    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    processed INTEGER DEFAULT 0,
    UNIQUE(token_mint, tx_signature)  -- prevent duplicate writes on backfill/reconnect
);
CREATE INDEX idx_smi_unprocessed ON smart_money_injections(processed, detected_at);
```

**Write strategy:** Sniper uses `INSERT OR IGNORE` to avoid duplicates on reconnect backfill.

**Cleanup:** Scout runs periodic cleanup: `DELETE FROM smart_money_injections WHERE processed = 1 AND detected_at < datetime('now', '-7 days')`. Runs once per hour (check at start of cycle, skip if ran within last hour). Prevents unbounded table growth.

### 3b. Scout reads injections as new ingestion source

**New file: `scout/ingestion/smart_money_feed.py`**

Each scout cycle:
1. In a single transaction: `SELECT id, ... WHERE processed = 0` → collect specific row IDs → `UPDATE ... SET processed = 1 WHERE id IN (collected_ids)` (must use explicit IDs, NOT `WHERE processed = 0` in the UPDATE, to avoid marking rows inserted between SELECT and UPDATE as processed without reading them)
2. Group injections by token_mint, count unique wallets per token
3. For each unique token:
   - Fetch token metadata from DexScreener — use batch endpoint `/tokens/v1/solana/{addr1,addr2,...}` (supports ~30 addresses per call) instead of sequential calls. One batch request instead of N individual requests.
   - Create `CandidateToken` with `smart_money_buys = <count of unique wallets>`
   - Skip redundant `check_smart_money()` Helius call for these tokens (we already know smart wallets bought)
4. Return list of `CandidateToken` objects

These candidates merge with regular scanner candidates in `aggregator.py` (dedup by contract address, prefer max values).

### 3c. Score boost for injected tokens

In `scorer.py`, smart_money_buys already awards +10 points. For injected tokens:

- **Graduated boost:** +20 per unique tracked wallet that bought (configurable via `COPY_TRADE_SCORE_BOOST`)
  - 1 wallet bought = +20
  - 2 wallets bought = +40
  - 3 wallets bought = +60
  - 4 wallets bought = +80
  - **Cap:** `SMART_MONEY_BOOST_CAP` env var, default 80 (matches current 4-wallet list; scales as wallet list grows)
- This replaces the flat +10 for `smart_money_buys > 0`

### 3d. Pipeline flow for injected tokens

Injected tokens go through the FULL pipeline — no shortcuts:

```
smart_money_feed.py → aggregator (dedup) → holder_enricher → onchain_signals
→ scorer (+20/wallet boost) → quality_gate → social/news → MiroFish narrative
→ conviction gate → safety check → alert → sniper buys
```

Quality gate and safety checks still apply. Smart money doesn't bypass rug detection.

---

## 4. Copy Trader Rebuild (Sniper)

### 4a. Full DEX coverage

Add detection patterns for all major Solana DEXs:

```python
SWAP_PATTERNS = [
    # Jupiter
    "Instruction: Route",
    "Instruction: Swap",
    "Program JUP",
    # Raydium AMM
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
    # Raydium CPMM
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",
    # Raydium V4
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    # Orca Whirlpool
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",
    # Meteora
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",
    # Meteora DLMM
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB",
    # pump.fun
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
]
```

### 4b. WebSocket reliability

**Heartbeat/keepalive:**
- Send ping every 30 seconds
- If no pong within 10 seconds, force reconnect

**Transaction backfill after reconnect:**
- On reconnect, call Helius `getSignaturesForAddress` for each tracked wallet
- Fetch transactions since last known signature (store last seen sig per wallet **in DB**, not in-memory — survives restarts)
- **Max backfill window: 30 minutes.** Older transactions are stale (token likely already pumped/dumped). Configurable via `BACKFILL_MAX_MINUTES` env var.
- Process any missed swaps within the window
- Dedup against `smart_money_injections` table via `INSERT OR IGNORE` (UNIQUE constraint on token_mint + tx_signature)
- Mark backfilled rows with `source = 'backfill'`
- Prevents permanent loss during reconnect window

**Subscription confirmation:**
- After `logsSubscribe`, wait for subscription ID response
- If no confirmation within 5 seconds, retry subscribe

### 4c. Better token extraction

- Validate that token transfer `toUserAccount` or `fromUserAccount` matches tracked wallet
- **Multi-hop swap handling:** Use Helius parsed transaction's `tokenTransfers` array. Algorithm: (1) filter transfers where `toUserAccount` matches the tracked wallet, (2) exclude known intermediary mints (SOL `So111...`, USDC `EPjFW...`, USDT `Es9vM...`), (3) take the last remaining transfer's mint (final destination token in the route). If no transfers match, fall back to first non-SOL mint as today.
- Verify SOL was spent (not just received tokens via airdrop) — check `nativeTransfers` for SOL leaving the wallet
- Add retry with backoff on Helius API calls (3 attempts, 1s/2s/4s)

### 4d. Multiple wallet tracking

Change `smart_money_signals` from overwriting dict to accumulating:

```python
# Before: {token: {"wallet": str, "detected_at": datetime}}
# After:  {token: {"wallets": set[str], "detected_at": datetime, "count": int}}
```

When multiple tracked wallets buy same token:
- Track all wallet addresses
- Count unique wallets
- Graduated conviction boost: +20 per wallet (capped at SMART_MONEY_BOOST_CAP, default 80)

### 4e. Write to scout DB instead of in-memory dict

Instead of storing signals only in module-level dict:
1. Write to `smart_money_injections` table in scout DB (for Direction 2) using `INSERT OR IGNORE`
2. Keep in-memory dict for sniper's own conviction boost (for tokens already in pipeline)
3. Signals persist across restarts
4. **In-memory dict lifecycle:** Prune entries older than 60 minutes (increased from 30 min to account for slow scout cycles). This dict is a cache for fast conviction lookup — the DB is the source of truth.

**Connection management:** Sniper needs a new read-write connection to scout DB (separate from the read-only `signal_reader.py` connection). Initialize in `main.py` as `scout_db_writer`, pass to `copy_trader.py`.

### 4f. Helius rate limit coordination

- Add retry with exponential backoff to `_extract_bought_token()` (3 attempts)
- Add 0.5s delay between Helius API calls
- Use separate Helius endpoint for WebSocket vs REST to avoid starvation

---

## 5. Remove Dead Hours from Sniper

Dead hours logic was already removed from scout. Remove from sniper too:

- Delete `TRADING_DEAD_HOURS` from `sniper/config.py`
- Delete dead hours filter block from `sniper/signal_reader.py` (lines 86-90)

---

## 6. Graduated Conviction Boost in Sniper

Update `main.py` conviction boost logic:

```python
# Current: flat +20 if token in smart_money_signals
# New: +20 per unique wallet, capped at +60
if sig_data.contract_address in smart_money_signals:
    sm = smart_money_signals[sig_data.contract_address]
    wallet_count = sm["count"]
    boost = min(wallet_count * settings.COPY_TRADE_SCORE_BOOST, settings.SMART_MONEY_BOOST_CAP)
    conviction += boost
```

---

## 7. Merge Jito Branch Fixes First

`feat/jito-mev-protection` has reviewed fixes that should be merged before this work:

- CT-001: Fix Helius API URL trailing slash
- CT-003: Safety check before copy trade buy
- CT-004: Raydium AMM/CPMM detection
- CT-005: asyncio.Lock for scanner/copy trader race condition

These fixes are prerequisites — we build on top of them.

---

## 8. Files Changed

### Scout (`coinpump-scout`):

| File | Change |
|------|--------|
| `scout/config.py` | Add `SMART_MONEY_WALLETS` setting |
| `scout/ingestion/onchain_signals.py` | Read wallets from config, not empty set; skip smart money check for injected tokens |
| `scout/ingestion/smart_money_feed.py` | **NEW** — ingestion source reading injections table |
| `scout/db.py` | Add `smart_money_injections` table + migration; enable WAL mode + busy_timeout |
| `scout/main.py` | Add smart_money_feed to Stage 1 ingestion sources |
| `scout/scorer.py` | Graduated boost: +20 per wallet instead of flat +10 |

### Sniper (`solana-sniper`):

| File | Change |
|------|--------|
| `sniper/config.py` | Add `SMART_MONEY_WALLETS`, remove `TRADING_DEAD_HOURS` |
| `sniper/copy_trader.py` | Full rebuild: DEX coverage, heartbeat, backfill, multi-wallet tracking, write to scout DB |
| `sniper/signal_reader.py` | Remove dead hours filter |
| `sniper/main.py` | Graduated conviction boost (+20/wallet, cap +60); init `scout_db_writer` connection for copy_trader |
| `sniper/db.py` | Enable WAL mode + busy_timeout |

---

## 9. Config Changes

### New/Modified env vars:

```env
# Shared (both services)
SMART_MONEY_WALLETS=54Pz...,7pwK...,4uEN...,2tgU...

# Sniper
COPY_TRADE_ENABLED=true          # (existing)
COPY_TRADE_SCORE_BOOST=20        # (existing, now per-wallet)
SMART_MONEY_BOOST_CAP=80         # NEW — max total boost from smart wallets
BACKFILL_MAX_MINUTES=30          # NEW — max age for reconnect backfill transactions
HELIUS_API_KEY=854efe68-...      # (existing)

# Removed
# TRADING_DEAD_HOURS — deleted
# COPY_TRADE_WALLETS — replaced by SMART_MONEY_WALLETS
```

---

## 10. Data Flow Summary

```
DIRECTION 1 (Scanner → Smart Money):
  DexScreener/GeckoTerminal/Birdeye/PumpFun discover token
  → onchain_signals checks if SMART_MONEY_WALLETS bought it
  → +20/wallet score boost in scorer
  → full pipeline → alert → sniper buys

DIRECTION 2 (Smart Money → Scanner):
  copy_trader WebSocket detects tracked wallet swap
  → writes to smart_money_injections table in scout DB
  → scout reads injection next cycle
  → fetches token metadata from DexScreener
  → creates CandidateToken with smart_money_buys count
  → full pipeline (enrichment → scoring → quality gate → narrative → conviction → safety)
  → alert → sniper buys

BOTH DIRECTIONS:
  → aggregator deduplicates (if both find same token, merge with max values)
  → sniper also applies conviction boost from in-memory smart_money_signals dict
  → graduated: +20 per wallet, capped at SMART_MONEY_BOOST_CAP (default 80)
```

---

## 11. Risk Mitigations

| Risk | Mitigation |
|------|------------|
| WebSocket disconnect → miss buys | Heartbeat + backfill on reconnect via Helius history API |
| Helius rate limits | Retry with backoff, 0.5s delay between calls, separate endpoints |
| Smart money buys rug token | Full quality gate + safety check still applies |
| Scout DB locked by both services | Enable SQLite WAL mode + busy_timeout=5000 (see Step 0) |
| Signal expires before processing | Persistent in DB (no TTL), processed flag instead |
| Multiple wallets = dict overwrite | Accumulate wallets in set, count unique |
| Service restart loses signals | DB persistence instead of in-memory only |

---

## 12. Boost Stacking Clarification

Smart money boosts apply at TWO levels — they DO stack intentionally:

1. **Scout scorer** (quant_score 0-100): +20 per wallet in `smart_money_buys` → affects conviction via `quant_score * 0.6`
2. **Sniper conviction** (at buy time): +20 per wallet from in-memory dict → direct conviction boost

Let's do the math for a 2-wallet token:
- Scout: +40 quant boost → contributes +24 to conviction (×0.6 weight)
- Sniper: +40 direct conviction boost
- **Total conviction impact: +64**

With a conviction gate of 70, a token with mediocre fundamentals (base conviction ~20-30) **can clear the gate purely on smart money**. **This is intentional and a conscious design choice** — if 2+ of our tracked profitable wallets buy the same token, we want to trade it even if other signals are weak. Smart money consensus IS the signal. The quality gate and safety checks still block actual rugs.

---

## 13. Monitoring

- **Injection pipeline heartbeat:** If `COPY_TRADE_ENABLED=true` but no injection written in 30 minutes, send Telegram alert: "Smart money WebSocket may be down"
- **Processing lag monitor:** Scout checks oldest unprocessed injection age each cycle. If older than 2 cycle intervals (2 × `SCAN_INTERVAL_SECONDS`), log warning: "Smart money injections backing up — oldest: Xm ago". Send Telegram if > 5 minutes.
- **Sniper write latency:** Log time taken for each `INSERT OR IGNORE` to scout DB. If > 1 second, log warning (indicates WAL contention).
- **Log smart money signals:** Both detection directions log to structured JSON (already standard in both services)

---

## 14. What Stays the Same

- Regular scanner flow (DexScreener, GeckoTerminal, Birdeye, PumpFun) — untouched
- Quality gate thresholds — unchanged
- Safety checks (GoPlus) — unchanged
- Conviction gate threshold (70) — unchanged
- MiroFish narrative scoring — unchanged
- Alert delivery (Telegram + Discord) — unchanged
- Position management, Kelly sizing, trailing stops — unchanged
- Multi-wallet buy execution — unchanged

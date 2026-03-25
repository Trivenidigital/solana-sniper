# Critical Code Review — 2026-03-25

## Executive Summary

Full code review of coinpump-scout and solana-sniper found **11 critical** and **17 important** issues.
4 critical fixes deployed same-day via PRs #13 (scout), #31, #32, #33 (sniper).

**Estimated annual loss from misconfigurations: ~$1,400 SOL-worth**

---

## Critical Issues (Fixed)

### T7: MAX_BUY_SOL not enforced — could bet 5x limit
- **File**: `sniper/main.py:57`
- **Bug**: `_conviction_bet_size()` returned raw Kelly bet without clamping to MAX_BUY_SOL
- **Impact**: .env said 0.2 SOL max but code could bet 1.0+ SOL
- **Fix**: PR #31 — added `return min(raw, settings.MAX_BUY_SOL)`

### T1/C1: Smart money pipeline broken — wrong INJECTIONS_DB_PATH
- **File**: `sniper/config.py:29`
- **Bug**: Sniper wrote to `/opt/scout/injections.db`, scout read from `./injections.db`
- **Impact**: Copy trade signals never reached the scout pipeline
- **Fix**: PR #31 — changed to `../coinpump-scout/injections.db`

### T2/T3: Hardcoded thresholds bypass config
- **File**: `scout/scorer.py:77,80`
- **Bug**: Top-3 concentration (0.40) and deployer supply (0.20) hardcoded, ignoring settings
- **Impact**: Changing config had zero effect on scoring disqualifiers
- **Fix**: PR #13 — now uses `settings.MAX_TOP3_CONCENTRATION / 100.0` and added `MAX_DEPLOYER_SUPPLY_PCT`

### Config defaults misaligned with settings PDF
- **Files**: `sniper/config.py` multiple defaults
- **Bug**: 8 defaults didn't match the sniper-bot-settings.pdf
- **Fix**: PR #32

| Setting | Was | Fixed To | Impact |
|---|---|---|---|
| MAX_PORTFOLIO_SOL | 1.0 | 6.0 | Could only hold 1 SOL total |
| MAX_HOLD_MIN | 60 | 180 | Force-closing at 1hr not 3hr |
| SLIPPAGE_BPS | 300 | 500 | Failed swaps on volatile memes |
| MAX_SIGNAL_AGE_SECONDS | 120 | 300 | Dropping valid signals |
| KELLY_MIN_BET | 0.25 | 0.50 | Min bet below PDF spec |
| JITO_TIP_LAMPORTS | 100,000 | 10,000 | 10x overpaying tips |
| STOP_LOSS_PCT (.env) | 25% | 35% | Exiting 10% too early |
| POSITION_CHECK | 10s | 15s | Minor timing |

### B2: DexScreener API hammered every cycle
- **File**: `sniper/position_manager.py:144-156`
- **Bug**: Liquidity rug check hit DexScreener every 15s per conviction hold position
- **Impact**: 5 positions = 20+ calls/min, risking rate limits
- **Fix**: PR #33 — 60s cache per token

### M2: Conviction hold skipped profit ladder
- **File**: `sniper/position_manager.py:261`
- **Bug**: `continue` skipped profit ladder entirely for conviction holds
- **Impact**: Tokens at +100% never partial-sold — just trailed
- **Fix**: PR #33 — conviction holds now use profit ladder, only skip phase-based exits

---

## Critical Issues (Helius/Anthropic Degraded Mode)

### Scout scoring dead without Helius
- **Files**: `scout/scorer.py`, `scout/quality_gate.py`, `scout/gate.py`, `scout/main.py`
- **Bug**: 0.8x co-occurrence penalty, holder_growth gate, and 0.6x conviction weight all assumed Helius was available
- **Impact**: Zero alerts for 3 days
- **Fix**: PR #13
  - Scorer: skip penalty when `helius_available=False`
  - Quality gate: skip holder_growth when holder_count <= 20 (Rugcheck cap)
  - Quality gate: skip unique_buyers when value is 0
  - Gate: use quant_score directly in quant-only mode (not * 0.6)

### Scout DB missing 17 columns
- Applied directly (SQLite ALTER TABLE), not in PR
- Columns: smart_money_buys, whale_buys, liquidity_locked, volume_spike, volume_spike_ratio, holder_gini_healthy, whale_txns_1h, social_score, has_twitter, has_telegram, has_github, on_coingecko, multi_dex, dex_count, news_mentions, news_sentiment, has_news

### Sniper ZACK MORRIS stuck position
- Applied directly — closed position with 0 on-chain balance
- Was retrying sells in infinite loop, blocking all other operations

---

## Important Issues (Documented, Some Open)

### Silent Failures
- **S1**: `copy_trader._write_injection` swallows DB write errors (`copy_trader.py:98-99`)
- **S4**: GoPlus safety in sniper catches all exceptions silently (`main.py:480-481`)
- **S5**: Social enrichment returns unchanged token on any error (`social.py:440-447`)
- **S6**: CoinGecko contract verification fails open — scam tokens get +8 pts (`cex_monitor.py:78-80`)

### Dead Code
- **D1**: `check_whale_activity` deprecated, never called (`onchain_signals.py:297-343`)
- **D3**: Sniper `token_age_days` check unreachable — SQL hardcodes `0` (`signal_reader.py:125`)
- **D5**: DCA code disabled per user rule (`position_manager.py:527`)
- **D6**: COOLDOWN_HOURS never enforced per user rule
- **D7**: Legacy trailing settings replaced by tiered trailing
- **D8**: MiroFish .env config not in config.py, silently ignored

### Config Inconsistencies
- **C2**: Scout SNIPER_DB_PATH `/opt/sniper/sniper.db` can't read local sniper DB
- **C4**: Scout and sniper GoPlus checks use completely different danger flags
- **C5**: Sniper .env missing HELIUS_API_KEY — bundle detection silently does nothing

### Rate Limit / API Waste
- **R1**: DexScreener called 3x per token (social + liquidity lock + main check)
- **R2**: Reddit 3s sleep per token burns 15s per cycle
- **R5**: check_smart_money and check_holder_distribution make separate Helius calls

---

## PRs Deployed

| PR | Repo | Description |
|---|---|---|
| #13 | coinpump-scout | Degraded mode + hardcoded thresholds + quality gates |
| #31 | solana-sniper | MAX_BUY_SOL clamp, injections path, conviction hold |
| #32 | solana-sniper | Config defaults aligned with PDF |
| #33 | solana-sniper | DexScreener cache, conviction hold + profit ladder |

---

## Recommended Next Actions (Priority Order)

1. **Add Alchemy RPC key to VPS .env** — currently on public RPC
2. **Fix C2**: Scout SNIPER_DB_PATH for missed trade recovery
3. **Fix R1**: Deduplicate DexScreener calls in scout
4. **Fix S1/S4**: Add proper error logging for silent failures
5. **Clean up dead code**: D1, D5, D6, D7, D8

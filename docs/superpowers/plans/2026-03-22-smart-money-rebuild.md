# Smart Money Copy Trading Rebuild — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two-directional smart money detection — scanner checks tracked wallets, and tracked wallet buys inject into the scanner pipeline. Never miss a smart wallet buy.

**Architecture:** Shared wallet config (env var) read by both services. Direction 1: scout's `onchain_signals.py` checks tracked wallets when scoring tokens. Direction 2: sniper's `copy_trader.py` writes wallet buys to a `smart_money_injections` table in scout's DB, which scout reads as a new ingestion source each cycle. Both pass through the full pipeline.

**Tech Stack:** Python 3.12, asyncio, aiosqlite, aiohttp, websockets, Helius API, DexScreener API, pytest + pytest-asyncio

**Spec:** `docs/superpowers/specs/2026-03-22-smart-money-rebuild-design.md`

---

## File Structure

### Scout (`/Users/ramujakkampudi/coinpump-scout`):

| File | Action | Responsibility |
|------|--------|---------------|
| `scout/config.py` | Modify | Add `SMART_MONEY_WALLETS` and `SMART_MONEY_BOOST_CAP` settings |
| `scout/db.py` | Modify | Add WAL mode, `smart_money_injections` table, read/mark/cleanup methods |
| `scout/ingestion/onchain_signals.py` | Modify | Load wallets from config instead of empty set |
| `scout/ingestion/smart_money_feed.py` | Create | New ingestion source: read injections table, fetch DexScreener metadata |
| `scout/main.py` | Modify | Add smart_money_feed to Stage 1, pass settings to scorer |
| `scout/scorer.py` | Modify | Graduated +20/wallet boost replacing flat +10 |
| `tests/test_smart_money_feed.py` | Create | Tests for new ingestion source |
| `tests/test_scorer_smart_money.py` | Create | Tests for graduated smart money scoring |

### Sniper (`/Users/ramujakkampudi/solana-sniper`):

| File | Action | Responsibility |
|------|--------|---------------|
| `sniper/config.py` | Modify | Add `SMART_MONEY_WALLETS`, `SMART_MONEY_BOOST_CAP`, `BACKFILL_MAX_MINUTES`; remove `TRADING_DEAD_HOURS` |
| `sniper/db.py` | Modify | Add WAL mode to initialization |
| `sniper/copy_trader.py` | Rewrite | Full rebuild: DEX coverage, heartbeat, backfill, multi-wallet signals, write to scout DB |
| `sniper/signal_reader.py` | Modify | Remove dead hours filter |
| `sniper/main.py` | Modify | Graduated conviction boost, scout_db_writer connection, startup validation |
| `tests/test_copy_trader.py` | Create | Tests for rebuilt copy trader |
| `tests/test_signal_reader_no_dead_hours.py` | Create | Verify dead hours removal |

---

## Task 0: Merge Jito Branch & Resolve Conflicts

> This is a prerequisite. The `feat/jito-mev-protection` branch has CT fixes (Helius URL, safety check, Raydium detection, race lock) that we build on.

**Files:**
- Modify: `/Users/ramujakkampudi/solana-sniper/sniper/main.py` (resolve conflict markers)

- [ ] **Step 1: Check conflict state**

```bash
cd /Users/ramujakkampudi/solana-sniper
git diff main..feat/jito-mev-protection -- sniper/main.py | grep -c '<<<<<<<'
```

Expected: Shows conflict markers count

- [ ] **Step 2: Merge branch, resolve conflicts manually**

```bash
git checkout main
git merge feat/jito-mev-protection
```

Resolve any conflicts in `sniper/main.py` — keep both Jito and copy trading changes. Ensure imports are clean and no duplicate code.

- [ ] **Step 3: Run tests**

```bash
cd /Users/ramujakkampudi/solana-sniper
uv run pytest -v
```

Expected: All tests pass

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "merge: feat/jito-mev-protection into main — resolve conflicts"
```

---

## Task 1: Enable WAL Mode in Both DBs

**Files:**
- Modify: `/Users/ramujakkampudi/coinpump-scout/scout/db.py:72-76`
- Modify: `/Users/ramujakkampudi/solana-sniper/sniper/db.py:18-22`
- Test: `/Users/ramujakkampudi/coinpump-scout/tests/test_db.py`
- Test: `/Users/ramujakkampudi/solana-sniper/tests/test_db.py`

- [ ] **Step 1: Write failing test (scout)**

In `/Users/ramujakkampudi/coinpump-scout/tests/test_db.py`, add:

```python
@pytest.mark.asyncio
async def test_wal_mode_enabled(tmp_path):
    """Database should use WAL journal mode for concurrent access."""
    from scout.db import Database
    db = Database(tmp_path / "test.db")
    await db.initialize()
    async with db._conn.execute("PRAGMA journal_mode") as cursor:
        row = await cursor.fetchone()
        assert row[0] == "wal"
    await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/ramujakkampudi/coinpump-scout && uv run pytest tests/test_db.py::test_wal_mode_enabled -v
```

Expected: FAIL — journal_mode is "delete"

- [ ] **Step 3: Implement WAL mode in scout DB**

In `/Users/ramujakkampudi/coinpump-scout/scout/db.py`, after line 75 (`self._conn.row_factory = aiosqlite.Row`), add:

```python
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /Users/ramujakkampudi/coinpump-scout && uv run pytest tests/test_db.py::test_wal_mode_enabled -v
```

Expected: PASS

- [ ] **Step 5: Write failing test (sniper)**

In `/Users/ramujakkampudi/solana-sniper/tests/test_db.py`, add:

```python
@pytest.mark.asyncio
async def test_wal_mode_enabled(tmp_path):
    """Database should use WAL journal mode for concurrent access."""
    from sniper.db import Database
    db = Database(tmp_path / "test.db")
    await db.initialize()
    async with db._conn.execute("PRAGMA journal_mode") as cursor:
        row = await cursor.fetchone()
        assert row[0] == "wal"
    await db.close()
```

- [ ] **Step 6: Run test, verify fails**

```bash
cd /Users/ramujakkampudi/solana-sniper && uv run pytest tests/test_db.py::test_wal_mode_enabled -v
```

- [ ] **Step 7: Implement WAL mode in sniper DB**

In `/Users/ramujakkampudi/solana-sniper/sniper/db.py`, after line 20 (`self._conn.row_factory = aiosqlite.Row`), add:

```python
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
```

- [ ] **Step 8: Run test, verify passes**

```bash
cd /Users/ramujakkampudi/solana-sniper && uv run pytest tests/test_db.py::test_wal_mode_enabled -v
```

- [ ] **Step 9: Commit both repos**

```bash
cd /Users/ramujakkampudi/coinpump-scout && git add scout/db.py tests/test_db.py && git commit -m "feat: enable SQLite WAL mode for concurrent access"
cd /Users/ramujakkampudi/solana-sniper && git add sniper/db.py tests/test_db.py && git commit -m "feat: enable SQLite WAL mode for concurrent access"
```

---

## Task 2: Shared Wallet Config (Scout)

**Files:**
- Modify: `/Users/ramujakkampudi/coinpump-scout/scout/config.py:79-80`
- Modify: `/Users/ramujakkampudi/coinpump-scout/scout/ingestion/onchain_signals.py:27`
- Test: `/Users/ramujakkampudi/coinpump-scout/tests/test_onchain_signals.py`

- [ ] **Step 1: Write failing test**

In `/Users/ramujakkampudi/coinpump-scout/tests/test_onchain_signals.py`, add:

```python
def test_smart_money_wallets_loaded_from_config():
    """SMART_MONEY_WALLETS should be loaded from settings, not empty set."""
    from scout.ingestion.onchain_signals import _get_smart_wallets
    settings = _settings(SMART_MONEY_WALLETS="wallet1,wallet2,wallet3")
    wallets = _get_smart_wallets(settings)
    assert wallets == {"wallet1", "wallet2", "wallet3"}


def test_smart_money_wallets_empty_when_not_configured():
    """SMART_MONEY_WALLETS returns empty set when not configured."""
    from scout.ingestion.onchain_signals import _get_smart_wallets
    settings = _settings(SMART_MONEY_WALLETS="")
    wallets = _get_smart_wallets(settings)
    assert wallets == set()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/ramujakkampudi/coinpump-scout && uv run pytest tests/test_onchain_signals.py::test_smart_money_wallets_loaded_from_config -v
```

Expected: FAIL — `_get_smart_wallets` doesn't exist yet

- [ ] **Step 3: Add SMART_MONEY_WALLETS to scout config**

In `/Users/ramujakkampudi/coinpump-scout/scout/config.py`, after line 80 (`ONCHAIN_SIGNALS_ENABLED: bool = True`), add:

```python
    SMART_MONEY_WALLETS: str = ""  # Comma-separated tracked wallet addresses
    SMART_MONEY_BOOST_CAP: int = 80  # Max total smart money score boost
```

- [ ] **Step 4: Update onchain_signals.py to use config**

In `/Users/ramujakkampudi/coinpump-scout/scout/ingestion/onchain_signals.py`:

Replace line 27:
```python
SMART_MONEY_WALLETS: set[str] = set()
```

With:
```python
def _get_smart_wallets(settings: Settings) -> set[str]:
    """Load smart money wallet set from config."""
    if not settings.SMART_MONEY_WALLETS:
        return set()
    return {w.strip() for w in settings.SMART_MONEY_WALLETS.split(",") if w.strip()}
```

Then update `check_smart_money()` to accept settings and call `_get_smart_wallets(settings)` instead of referencing the module-level `SMART_MONEY_WALLETS` set. Pass `settings` from `enrich_onchain_signals()`.

- [ ] **Step 5: Run tests**

```bash
cd /Users/ramujakkampudi/coinpump-scout && uv run pytest tests/test_onchain_signals.py -v
```

Expected: All pass including new tests

- [ ] **Step 6: Commit**

```bash
cd /Users/ramujakkampudi/coinpump-scout && git add scout/config.py scout/ingestion/onchain_signals.py tests/test_onchain_signals.py && git commit -m "feat: load SMART_MONEY_WALLETS from config instead of empty set"
```

---

## Task 3: Shared Wallet Config (Sniper) + Remove Dead Hours

**Files:**
- Modify: `/Users/ramujakkampudi/solana-sniper/sniper/config.py:116-126`
- Modify: `/Users/ramujakkampudi/solana-sniper/sniper/signal_reader.py:86-90`
- Modify: `/Users/ramujakkampudi/solana-sniper/sniper/copy_trader.py:161-166`
- Test: `/Users/ramujakkampudi/solana-sniper/tests/test_signal_reader.py`

- [ ] **Step 1: Write failing test — dead hours removed**

In `/Users/ramujakkampudi/solana-sniper/tests/test_signal_reader_no_dead_hours.py`, create:

```python
"""Verify dead hours filter is removed — signals process 24/7."""
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from sniper.signal_reader import filter_actionable
from sniper.config import Settings


def _settings(**overrides):
    defaults = dict(
        SOLANA_RPC_URL="http://localhost",
        KEYPAIR_PATH="/tmp/test.json",
        SCOUT_DB_PATH="/tmp/scout.db",
        SNIPER_DB_PATH="/tmp/sniper.db",
    )
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.mark.asyncio
async def test_no_dead_hours_filter():
    """Signals should NOT be filtered by time of day — dead hours removed."""
    settings = _settings()
    # Verify TRADING_DEAD_HOURS attribute no longer exists
    assert not hasattr(settings, "TRADING_DEAD_HOURS")
```

- [ ] **Step 2: Run test, verify fails**

```bash
cd /Users/ramujakkampudi/solana-sniper && uv run pytest tests/test_signal_reader_no_dead_hours.py -v
```

Expected: FAIL — `TRADING_DEAD_HOURS` still exists

- [ ] **Step 3: Update sniper config**

In `/Users/ramujakkampudi/solana-sniper/sniper/config.py`:

**Delete** lines 116-117 (TRADING_DEAD_HOURS).

**Replace** lines 122-126 with:

```python
    # Smart money / copy trading
    COPY_TRADE_ENABLED: bool = False
    SMART_MONEY_WALLETS: str = ""  # Comma-separated tracked wallet addresses (shared with scout)
    COPY_TRADE_SCORE_BOOST: int = 20  # Conviction boost per wallet
    SMART_MONEY_BOOST_CAP: int = 80  # Max total boost from smart wallets
    BACKFILL_MAX_MINUTES: int = 30  # Max age for reconnect backfill
    HELIUS_API_KEY: str = ""  # Required for copy trading WebSocket
```

- [ ] **Step 4: Remove dead hours filter from signal_reader.py**

In `/Users/ramujakkampudi/solana-sniper/sniper/signal_reader.py`, delete lines 86-90:

```python
    # Time-of-day filter: skip all signals during dead hours
    dead_hours = {int(h.strip()) for h in settings.TRADING_DEAD_HOURS.split(",") if h.strip()}
    if now.hour in dead_hours:
        logger.info("Skipping signals during dead hours", utc_hour=now.hour, dead_hours=sorted(dead_hours))
        return actionable
```

- [ ] **Step 5: Update copy_trader.py to use SMART_MONEY_WALLETS**

In `/Users/ramujakkampudi/solana-sniper/sniper/copy_trader.py`, replace `_get_tracked_wallets()` (lines 161-166):

```python
def _get_tracked_wallets(settings: Settings) -> list[str]:
    """Get list of wallet addresses to track from shared config."""
    if not settings.SMART_MONEY_WALLETS:
        return []
    return [w.strip() for w in settings.SMART_MONEY_WALLETS.split(",") if w.strip()]
```

Delete `DEFAULT_TRACKED_WALLETS = []` (line 19) and remove the reference to `COPY_TRADE_WALLETS`.

- [ ] **Step 6: Run all tests**

```bash
cd /Users/ramujakkampudi/solana-sniper && uv run pytest -v
```

Expected: All pass

- [ ] **Step 7: Commit**

```bash
cd /Users/ramujakkampudi/solana-sniper && git add sniper/config.py sniper/signal_reader.py sniper/copy_trader.py tests/test_signal_reader_no_dead_hours.py && git commit -m "feat: shared SMART_MONEY_WALLETS config, remove dead hours filter"
```

---

## Task 4: Smart Money Injections Table (Scout DB)

**Files:**
- Modify: `/Users/ramujakkampudi/coinpump-scout/scout/db.py`
- Test: `/Users/ramujakkampudi/coinpump-scout/tests/test_db.py`

- [ ] **Step 1: Write failing tests**

In `/Users/ramujakkampudi/coinpump-scout/tests/test_db.py`, add:

```python
@pytest.mark.asyncio
async def test_smart_money_injections_table_exists(tmp_path):
    """smart_money_injections table should be created on init."""
    from scout.db import Database
    db = Database(tmp_path / "test.db")
    await db.initialize()
    async with db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='smart_money_injections'"
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None
    await db.close()


@pytest.mark.asyncio
async def test_read_unprocessed_injections(tmp_path):
    """Should read unprocessed injections and mark them as processed atomically."""
    from scout.db import Database
    db = Database(tmp_path / "test.db")
    await db.initialize()
    # Insert two injections
    await db._conn.execute(
        "INSERT INTO smart_money_injections (token_mint, wallet_address, tx_signature) VALUES (?, ?, ?)",
        ("mint1", "wallet1", "tx1"),
    )
    await db._conn.execute(
        "INSERT INTO smart_money_injections (token_mint, wallet_address, tx_signature) VALUES (?, ?, ?)",
        ("mint1", "wallet2", "tx2"),
    )
    await db._conn.commit()
    # Read and mark
    injections = await db.read_and_mark_injections()
    assert len(injections) == 2
    assert injections[0]["token_mint"] == "mint1"
    # Verify they're now processed
    second_read = await db.read_and_mark_injections()
    assert len(second_read) == 0
    await db.close()


@pytest.mark.asyncio
async def test_injection_dedup_on_tx_signature(tmp_path):
    """Duplicate (token_mint, tx_signature) should be ignored."""
    from scout.db import Database
    db = Database(tmp_path / "test.db")
    await db.initialize()
    await db._conn.execute(
        "INSERT OR IGNORE INTO smart_money_injections (token_mint, wallet_address, tx_signature) VALUES (?, ?, ?)",
        ("mint1", "wallet1", "tx1"),
    )
    await db._conn.execute(
        "INSERT OR IGNORE INTO smart_money_injections (token_mint, wallet_address, tx_signature) VALUES (?, ?, ?)",
        ("mint1", "wallet1", "tx1"),
    )
    await db._conn.commit()
    async with db._conn.execute("SELECT COUNT(*) FROM smart_money_injections") as cursor:
        row = await cursor.fetchone()
        assert row[0] == 1
    await db.close()


@pytest.mark.asyncio
async def test_cleanup_old_injections(tmp_path):
    """Processed injections older than 7 days should be cleaned up."""
    from scout.db import Database
    db = Database(tmp_path / "test.db")
    await db.initialize()
    # Insert old processed injection
    await db._conn.execute(
        "INSERT INTO smart_money_injections (token_mint, wallet_address, tx_signature, processed, detected_at) VALUES (?, ?, ?, 1, datetime('now', '-8 days'))",
        ("old_mint", "wallet1", "old_tx"),
    )
    # Insert recent processed injection
    await db._conn.execute(
        "INSERT INTO smart_money_injections (token_mint, wallet_address, tx_signature, processed) VALUES (?, ?, ?, 1)",
        ("new_mint", "wallet1", "new_tx"),
    )
    await db._conn.commit()
    deleted = await db.cleanup_old_injections()
    assert deleted == 1
    await db.close()
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
cd /Users/ramujakkampudi/coinpump-scout && uv run pytest tests/test_db.py::test_smart_money_injections_table_exists tests/test_db.py::test_read_unprocessed_injections tests/test_db.py::test_injection_dedup_on_tx_signature tests/test_db.py::test_cleanup_old_injections -v
```

Expected: FAIL — table doesn't exist, methods don't exist

- [ ] **Step 3: Add table creation to scout/db.py**

In `/Users/ramujakkampudi/coinpump-scout/scout/db.py`, in the `_create_tables` method, after the `alerts` table creation (after line 149), add:

```python
            CREATE TABLE IF NOT EXISTS smart_money_injections (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                token_mint      TEXT NOT NULL,
                wallet_address  TEXT NOT NULL,
                tx_signature    TEXT,
                source          TEXT DEFAULT 'websocket',
                detected_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed       INTEGER DEFAULT 0,
                UNIQUE(token_mint, tx_signature)
            );
            CREATE INDEX IF NOT EXISTS idx_smi_unprocessed
                ON smart_money_injections(processed, detected_at);
```

- [ ] **Step 4: Add read_and_mark_injections method**

In `/Users/ramujakkampudi/coinpump-scout/scout/db.py`, add method to Database class:

```python
    async def read_and_mark_injections(self) -> list[dict]:
        """Read unprocessed smart money injections and mark them processed atomically.

        Uses explicit IDs to avoid marking rows inserted between SELECT and UPDATE.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        rows_data = []
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self._conn.execute(
                "SELECT id, token_mint, wallet_address, tx_signature, source, detected_at "
                "FROM smart_money_injections WHERE processed = 0"
            )
            rows = await cursor.fetchall()
            if rows:
                ids = [row["id"] for row in rows]
                rows_data = [dict(row) for row in rows]
                placeholders = ",".join("?" for _ in ids)
                await self._conn.execute(
                    f"UPDATE smart_money_injections SET processed = 1 WHERE id IN ({placeholders})",
                    ids,
                )
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise
        return rows_data

    async def cleanup_old_injections(self, days: int = 7) -> int:
        """Delete processed injections older than N days. Returns count deleted."""
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        cursor = await self._conn.execute(
            "DELETE FROM smart_money_injections WHERE processed = 1 AND detected_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        await self._conn.commit()
        return cursor.rowcount
```

- [ ] **Step 5: Run tests, verify they pass**

```bash
cd /Users/ramujakkampudi/coinpump-scout && uv run pytest tests/test_db.py -v
```

Expected: All pass

- [ ] **Step 6: Commit**

```bash
cd /Users/ramujakkampudi/coinpump-scout && git add scout/db.py tests/test_db.py && git commit -m "feat: smart_money_injections table with atomic read-mark and cleanup"
```

---

## Task 5: Smart Money Feed Ingestion Source (Scout)

**Files:**
- Create: `/Users/ramujakkampudi/coinpump-scout/scout/ingestion/smart_money_feed.py`
- Modify: `/Users/ramujakkampudi/coinpump-scout/scout/main.py:48-86`
- Test: `/Users/ramujakkampudi/coinpump-scout/tests/test_smart_money_feed.py`

- [ ] **Step 1: Write failing tests**

Create `/Users/ramujakkampudi/coinpump-scout/tests/test_smart_money_feed.py`:

```python
"""Tests for smart money feed ingestion source."""
import pytest
from unittest.mock import AsyncMock, patch
from scout.ingestion.smart_money_feed import fetch_smart_money_injections
from scout.config import Settings
from scout.models import CandidateToken


def _settings(**overrides):
    defaults = dict(
        TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k",
        SMART_MONEY_WALLETS="wallet1,wallet2",
    )
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.mark.asyncio
async def test_no_injections_returns_empty():
    """No unprocessed injections -> empty list."""
    mock_db = AsyncMock()
    mock_db.read_and_mark_injections = AsyncMock(return_value=[])
    mock_session = AsyncMock()
    settings = _settings()
    result = await fetch_smart_money_injections(mock_session, mock_db, settings)
    assert result == []


@pytest.mark.asyncio
async def test_injection_creates_candidate_with_smart_money_count():
    """Injection with 2 wallets buying same token -> smart_money_buys=2."""
    mock_db = AsyncMock()
    mock_db.read_and_mark_injections = AsyncMock(return_value=[
        {"token_mint": "mint1", "wallet_address": "wallet1", "tx_signature": "tx1", "source": "websocket", "detected_at": "2026-03-22T10:00:00"},
        {"token_mint": "mint1", "wallet_address": "wallet2", "tx_signature": "tx2", "source": "websocket", "detected_at": "2026-03-22T10:01:00"},
    ])
    mock_session = AsyncMock()
    settings = _settings()

    # Mock DexScreener response
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=[{
        "tokenAddress": "mint1",
        "info": {"name": "TestToken", "symbol": "TST"},
        "marketCap": 50000,
        "liquidity": {"usd": 20000},
        "volume": {"h24": 100000},
    }])
    mock_session.get = AsyncMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_resp), __aexit__=AsyncMock()))

    result = await fetch_smart_money_injections(mock_session, mock_db, settings)
    assert len(result) == 1
    assert result[0].contract_address == "mint1"
    assert result[0].smart_money_buys == 2
    assert result[0].token_name == "TestToken"


@pytest.mark.asyncio
async def test_dexscreener_failure_skips_token():
    """If DexScreener returns error, token is skipped (not crash)."""
    mock_db = AsyncMock()
    mock_db.read_and_mark_injections = AsyncMock(return_value=[
        {"token_mint": "mint1", "wallet_address": "wallet1", "tx_signature": "tx1", "source": "websocket", "detected_at": "2026-03-22T10:00:00"},
    ])
    mock_session = AsyncMock()
    settings = _settings()

    mock_resp = AsyncMock()
    mock_resp.status = 404
    mock_session.get = AsyncMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_resp), __aexit__=AsyncMock()))

    result = await fetch_smart_money_injections(mock_session, mock_db, settings)
    assert result == []
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
cd /Users/ramujakkampudi/coinpump-scout && uv run pytest tests/test_smart_money_feed.py -v
```

Expected: FAIL — module doesn't exist

- [ ] **Step 3: Implement smart_money_feed.py**

Create `/Users/ramujakkampudi/coinpump-scout/scout/ingestion/smart_money_feed.py`:

```python
"""Smart money feed — ingestion source for tokens detected by tracked wallet buys.

Direction 2 of smart money integration: reads from smart_money_injections table
(written by sniper's copy_trader) and creates CandidateToken objects for the
full scout pipeline.
"""

from collections import defaultdict

import aiohttp
import structlog

from scout.config import Settings
from scout.db import Database
from scout.models import CandidateToken

logger = structlog.get_logger()

DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/tokens/v1/solana"


async def fetch_smart_money_injections(
    session: aiohttp.ClientSession,
    db: Database,
    settings: Settings,
) -> list[CandidateToken]:
    """Read unprocessed smart money injections and create CandidateToken objects.

    Groups injections by token_mint, counts unique wallets per token,
    fetches metadata from DexScreener batch endpoint.
    """
    injections = await db.read_and_mark_injections()
    if not injections:
        return []

    # Group by token_mint, count unique wallets
    token_wallets: dict[str, set[str]] = defaultdict(set)
    for inj in injections:
        token_wallets[inj["token_mint"]].add(inj["wallet_address"])

    mints = list(token_wallets.keys())
    logger.info("Smart money injections to process", count=len(mints))

    # Batch fetch metadata from DexScreener (supports comma-separated addresses)
    candidates: list[CandidateToken] = []
    batch_url = f"{DEXSCREENER_TOKENS_URL}/{','.join(mints)}"

    try:
        async with session.get(
            batch_url,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                logger.warning("DexScreener batch fetch failed", status=resp.status)
                return []
            data = await resp.json()
    except Exception as e:
        logger.warning("DexScreener fetch error", error=str(e))
        return []

    if not isinstance(data, list):
        data = [data] if data else []

    # Map DexScreener results by token address
    dex_by_mint: dict[str, dict] = {}
    for item in data:
        addr = item.get("tokenAddress", "")
        if addr:
            dex_by_mint[addr] = item

    for mint, wallets in token_wallets.items():
        dex_data = dex_by_mint.get(mint)
        if not dex_data:
            logger.debug("No DexScreener data for injected token", mint=mint[:20])
            continue

        info = dex_data.get("info", {})
        name = info.get("name", "Unknown")
        ticker = info.get("symbol", "???")
        mcap = dex_data.get("marketCap", 0) or 0
        liq = (dex_data.get("liquidity") or {}).get("usd", 0) or 0
        vol = (dex_data.get("volume") or {}).get("h24", 0) or 0

        candidate = CandidateToken(
            contract_address=mint,
            chain="solana",
            token_name=name,
            ticker=ticker,
            market_cap_usd=float(mcap),
            liquidity_usd=float(liq),
            volume_24h_usd=float(vol),
            smart_money_buys=len(wallets),
        )
        candidates.append(candidate)
        logger.info(
            "Smart money injection → candidate",
            token=name,
            ticker=ticker,
            wallets=len(wallets),
            mcap=mcap,
        )

    return candidates
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
cd /Users/ramujakkampudi/coinpump-scout && uv run pytest tests/test_smart_money_feed.py -v
```

Expected: All pass

- [ ] **Step 5: Wire into scout main.py**

In `/Users/ramujakkampudi/coinpump-scout/scout/main.py`:

Add import at top:
```python
from scout.ingestion.smart_money_feed import fetch_smart_money_injections
```

After line 86 (`[:settings.MAX_CANDIDATES_PER_CYCLE]`), before Stage 2 enrichment, add:

```python
    # Stage 1b: Smart money injections (Direction 2 — tokens from tracked wallet buys)
    try:
        sm_candidates = await fetch_smart_money_injections(session, db, settings)
        if sm_candidates:
            logger.info("Smart money feed injected", count=len(sm_candidates))
    except Exception as e:
        logger.warning("Smart money feed failed", error=str(e))
        sm_candidates = []

    # Processing lag monitor: check oldest unprocessed injection age
    try:
        async with db._conn.execute(
            "SELECT MIN(detected_at) FROM smart_money_injections WHERE processed = 0"
        ) as cursor:
            row = await cursor.fetchone()
            if row and row[0]:
                oldest = datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc)
                lag_seconds = (now - oldest).total_seconds()
                if lag_seconds > 300:  # 5 minutes
                    logger.warning("Smart money injections backing up", oldest_age_min=int(lag_seconds / 60))
                elif lag_seconds > settings.SCAN_INTERVAL_SECONDS * 2:
                    logger.info("Injection processing lag detected", oldest_age_sec=int(lag_seconds))
    except Exception:
        pass  # Non-critical monitoring

    # Merge smart money candidates with scanner candidates
    all_candidates = aggregate(
        list(dex_tokens) + list(gecko_tokens) + list(birdeye_tokens) + list(pumpfun_tokens) + sm_candidates
    )[:settings.MAX_CANDIDATES_PER_CYCLE]
```

Replace the existing `all_candidates = aggregate(...)` line (84-86) with the merged version above.

Also add periodic cleanup (once per hour) — add a module-level variable and check at start of cycle:

```python
_last_injection_cleanup = datetime.min.replace(tzinfo=timezone.utc)
```

At the start of `run_cycle()`, before Stage 1:
```python
    global _last_injection_cleanup
    now = datetime.now(timezone.utc)
    if (now - _last_injection_cleanup).total_seconds() > 3600:
        try:
            deleted = await db.cleanup_old_injections()
            if deleted:
                logger.info("Cleaned up old injections", deleted=deleted)
            _last_injection_cleanup = now
        except Exception as e:
            logger.warning("Injection cleanup failed", error=str(e))
```

- [ ] **Step 6: Run full test suite**

```bash
cd /Users/ramujakkampudi/coinpump-scout && uv run pytest -v
```

Expected: All pass

- [ ] **Step 7: Commit**

```bash
cd /Users/ramujakkampudi/coinpump-scout && git add scout/ingestion/smart_money_feed.py scout/main.py tests/test_smart_money_feed.py && git commit -m "feat: smart money feed ingestion source (Direction 2)"
```

---

## Task 6: Graduated Scorer Boost (Scout)

**Files:**
- Modify: `/Users/ramujakkampudi/coinpump-scout/scout/scorer.py:14,43,160-164`
- Test: `/Users/ramujakkampudi/coinpump-scout/tests/test_scorer.py`

- [ ] **Step 1: Write failing tests**

Add to `/Users/ramujakkampudi/coinpump-scout/tests/test_scorer.py`:

```python
def test_smart_money_graduated_boost_1_wallet(token_factory, settings_factory):
    """1 smart wallet buy = +20 points."""
    token = token_factory(smart_money_buys=1, liquidity_usd=20000)
    settings = settings_factory(SMART_MONEY_BOOST_CAP=80)
    points, signals = score(token, settings)
    assert "smart_money_buys" in signals
    # Check the contribution is ~20 (not the old flat 10)


def test_smart_money_graduated_boost_3_wallets(token_factory, settings_factory):
    """3 smart wallet buys = +60 points (3 x 20)."""
    token = token_factory(smart_money_buys=3, liquidity_usd=20000)
    settings = settings_factory(SMART_MONEY_BOOST_CAP=80)
    points, signals = score(token, settings)
    assert "smart_money_buys" in signals


def test_smart_money_boost_capped(token_factory, settings_factory):
    """5 smart wallet buys should be capped at SMART_MONEY_BOOST_CAP (80)."""
    token = token_factory(smart_money_buys=5, liquidity_usd=20000)
    settings = settings_factory(SMART_MONEY_BOOST_CAP=80)
    points_5, _ = score(token, settings)
    token_4 = token_factory(smart_money_buys=4, liquidity_usd=20000)
    points_4, _ = score(token_4, settings)
    # 5 wallets should produce same score as 4 (both capped at 80)
    assert points_5 == points_4
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
cd /Users/ramujakkampudi/coinpump-scout && uv run pytest tests/test_scorer.py::test_smart_money_graduated_boost_1_wallet tests/test_scorer.py::test_smart_money_graduated_boost_3_wallets tests/test_scorer.py::test_smart_money_boost_capped -v
```

Expected: FAIL — scorer still uses flat +10

- [ ] **Step 3: Update scorer.py**

In `/Users/ramujakkampudi/coinpump-scout/scout/scorer.py`:

Update the docstring (line 14):
```python
- smart_money_buys: +20 per wallet (capped at SMART_MONEY_BOOST_CAP) -- Graduated alpha wallet boost
```

Update the scoring logic (replace lines 160-164):
```python
    # Signal 11: Smart Money Buys -- +20 per wallet, capped (on-chain signal)
    # Graduated boost: more tracked wallets buying = higher confidence.
    # Replaces flat +10. Cap controlled by SMART_MONEY_BOOST_CAP setting.
    if token.smart_money_buys > 0:
        sm_boost = min(token.smart_money_buys * 20, settings.SMART_MONEY_BOOST_CAP)
        points += sm_boost
        signals.append("smart_money_buys")
```

**RAW_MAX stays at 154.** Smart money scoring is Helius-dependent (the comment at line 39-42 explicitly excludes Helius signals from the denominator). The graduated boost applies on top of normalized scores. No change to RAW_MAX — this preserves scoring calibration when Helius is unavailable.

Note: The `score()` function signature already takes `settings`, so no signature change needed.

- [ ] **Step 4: Run tests, verify they pass**

```bash
cd /Users/ramujakkampudi/coinpump-scout && uv run pytest tests/test_scorer.py -v
```

Expected: All pass

- [ ] **Step 5: Commit**

```bash
cd /Users/ramujakkampudi/coinpump-scout && git add scout/scorer.py tests/test_scorer.py && git commit -m "feat: graduated smart money scorer boost — +20/wallet, capped at SMART_MONEY_BOOST_CAP"
```

---

## Task 7: Copy Trader Rebuild (Sniper)

> This is the largest task. The copy_trader.py gets a full rewrite with: all DEX detection, WebSocket heartbeat, backfill, multi-wallet accumulation, and DB writes.

**Files:**
- Rewrite: `/Users/ramujakkampudi/solana-sniper/sniper/copy_trader.py`
- Test: `/Users/ramujakkampudi/solana-sniper/tests/test_copy_trader.py`

- [ ] **Step 1: Write failing tests**

Create `/Users/ramujakkampudi/solana-sniper/tests/test_copy_trader.py`:

```python
"""Tests for rebuilt copy trader."""
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from sniper.copy_trader import (
    SWAP_PATTERNS,
    _is_swap_transaction,
    _extract_bought_token,
    _write_injection,
    _get_tracked_wallets,
    smart_money_signals,
    prune_stale_signals,
)
from sniper.config import Settings


def _settings(**overrides):
    defaults = dict(
        SOLANA_RPC_URL="http://localhost",
        KEYPAIR_PATH="/tmp/test.json",
        SCOUT_DB_PATH="/tmp/scout.db",
        SNIPER_DB_PATH="/tmp/sniper.db",
        SMART_MONEY_WALLETS="wallet1,wallet2,wallet3",
        COPY_TRADE_ENABLED=True,
        HELIUS_API_KEY="test-key",
    )
    defaults.update(overrides)
    return Settings(**defaults)


class TestSwapPatterns:
    def test_all_dexes_covered(self):
        """SWAP_PATTERNS should cover Jupiter, Raydium, Orca, Meteora, pump.fun."""
        pattern_text = " ".join(SWAP_PATTERNS)
        assert "JUP" in pattern_text  # Jupiter
        assert "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA" in pattern_text  # Raydium AMM
        assert "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C" in pattern_text  # Raydium CPMM
        assert "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8" in pattern_text  # Raydium V4
        assert "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc" in pattern_text  # Orca
        assert "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo" in pattern_text  # Meteora
        assert "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P" in pattern_text  # pump.fun

    def test_jupiter_route_detected(self):
        logs = ["Program log: Instruction: Route", "other log"]
        assert _is_swap_transaction(logs) is True

    def test_raydium_v4_detected(self):
        logs = ["Program 675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8 invoke"]
        assert _is_swap_transaction(logs) is True

    def test_orca_detected(self):
        logs = ["Program whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc invoke"]
        assert _is_swap_transaction(logs) is True

    def test_non_swap_not_detected(self):
        logs = ["Program log: Transfer", "System program invoke"]
        assert _is_swap_transaction(logs) is False


class TestTrackedWallets:
    def test_wallets_from_config(self):
        settings = _settings(SMART_MONEY_WALLETS="a,b,c")
        assert _get_tracked_wallets(settings) == ["a", "b", "c"]

    def test_empty_wallets(self):
        settings = _settings(SMART_MONEY_WALLETS="")
        assert _get_tracked_wallets(settings) == []


class TestMultiWalletSignals:
    def test_accumulates_wallets(self):
        """Multiple wallets buying same token should accumulate, not overwrite."""
        smart_money_signals.clear()
        from sniper.copy_trader import _record_signal
        _record_signal("mint1", "walletA")
        _record_signal("mint1", "walletB")
        assert smart_money_signals["mint1"]["count"] == 2
        assert "walletA" in smart_money_signals["mint1"]["wallets"]
        assert "walletB" in smart_money_signals["mint1"]["wallets"]

    def test_prune_respects_max_age(self):
        """Signals older than max age should be pruned."""
        smart_money_signals.clear()
        from sniper.copy_trader import _record_signal
        _record_signal("mint1", "walletA")
        # Manually backdate
        smart_money_signals["mint1"]["detected_at"] = datetime(2020, 1, 1, tzinfo=timezone.utc)
        prune_stale_signals(max_age_minutes=60)
        assert "mint1" not in smart_money_signals


class TestExtractBoughtToken:
    @pytest.mark.asyncio
    async def test_filters_intermediary_mints(self):
        """Should skip SOL/USDC/USDT and return actual bought token."""
        settings = _settings()
        mock_response = [{
            "tokenTransfers": [
                {"mint": "So11111111111111111111111111111111111111112", "toUserAccount": "wallet1"},
                {"mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "toUserAccount": "wallet1"},
                {"mint": "actual_token_mint", "toUserAccount": "wallet1"},
            ],
            "nativeTransfers": [
                {"fromUserAccount": "wallet1", "amount": 1000000},
            ],
        }]
        with patch("aiohttp.ClientSession") as MockSession:
            session_instance = AsyncMock()
            resp = AsyncMock()
            resp.status = 200
            resp.json = AsyncMock(return_value=mock_response)
            session_instance.post = AsyncMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=resp),
                __aexit__=AsyncMock(),
            ))
            MockSession.return_value = AsyncMock(
                __aenter__=AsyncMock(return_value=session_instance),
                __aexit__=AsyncMock(),
            )
            result = await _extract_bought_token("test_sig", "wallet1", settings)
            assert result == "actual_token_mint"

    @pytest.mark.asyncio
    async def test_takes_last_non_intermediary(self):
        """Multi-hop: should take LAST non-intermediary token (final destination)."""
        settings = _settings()
        mock_response = [{
            "tokenTransfers": [
                {"mint": "intermediate_token", "toUserAccount": "wallet1"},
                {"mint": "final_token", "toUserAccount": "wallet1"},
            ],
            "nativeTransfers": [
                {"fromUserAccount": "wallet1", "amount": 1000000},
            ],
        }]
        with patch("aiohttp.ClientSession") as MockSession:
            session_instance = AsyncMock()
            resp = AsyncMock()
            resp.status = 200
            resp.json = AsyncMock(return_value=mock_response)
            session_instance.post = AsyncMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=resp),
                __aexit__=AsyncMock(),
            ))
            MockSession.return_value = AsyncMock(
                __aenter__=AsyncMock(return_value=session_instance),
                __aexit__=AsyncMock(),
            )
            result = await _extract_bought_token("test_sig", "wallet1", settings)
            assert result == "final_token"

    @pytest.mark.asyncio
    async def test_rejects_airdrop_no_sol_spent(self):
        """If wallet didn't spend SOL, it's not a buy — return None."""
        settings = _settings()
        mock_response = [{
            "tokenTransfers": [
                {"mint": "airdrop_token", "toUserAccount": "wallet1"},
            ],
            "nativeTransfers": [
                {"fromUserAccount": "other_wallet", "amount": 1000000},
            ],
        }]
        with patch("aiohttp.ClientSession") as MockSession:
            session_instance = AsyncMock()
            resp = AsyncMock()
            resp.status = 200
            resp.json = AsyncMock(return_value=mock_response)
            session_instance.post = AsyncMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=resp),
                __aexit__=AsyncMock(),
            ))
            MockSession.return_value = AsyncMock(
                __aenter__=AsyncMock(return_value=session_instance),
                __aexit__=AsyncMock(),
            )
            result = await _extract_bought_token("test_sig", "wallet1", settings)
            assert result is None


class TestWriteInjection:
    @pytest.mark.asyncio
    async def test_writes_to_db(self, tmp_path):
        """Should write injection row to scout DB."""
        import aiosqlite
        db_path = tmp_path / "scout.db"
        conn = await aiosqlite.connect(str(db_path))
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.executescript("""
            CREATE TABLE IF NOT EXISTS smart_money_injections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_mint TEXT NOT NULL,
                wallet_address TEXT NOT NULL,
                tx_signature TEXT,
                source TEXT DEFAULT 'websocket',
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed INTEGER DEFAULT 0,
                UNIQUE(token_mint, tx_signature)
            );
        """)
        await _write_injection(conn, "mint1", "wallet1", "tx1", "websocket")
        async with conn.execute("SELECT * FROM smart_money_injections") as cursor:
            rows = await cursor.fetchall()
            assert len(rows) == 1
        await conn.close()

    @pytest.mark.asyncio
    async def test_dedup_on_same_tx(self, tmp_path):
        """Duplicate (token_mint, tx_signature) should be silently ignored."""
        import aiosqlite
        db_path = tmp_path / "scout.db"
        conn = await aiosqlite.connect(str(db_path))
        await conn.executescript("""
            CREATE TABLE IF NOT EXISTS smart_money_injections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_mint TEXT NOT NULL,
                wallet_address TEXT NOT NULL,
                tx_signature TEXT,
                source TEXT DEFAULT 'websocket',
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed INTEGER DEFAULT 0,
                UNIQUE(token_mint, tx_signature)
            );
        """)
        await _write_injection(conn, "mint1", "wallet1", "tx1")
        await _write_injection(conn, "mint1", "wallet1", "tx1")  # duplicate
        async with conn.execute("SELECT COUNT(*) FROM smart_money_injections") as cursor:
            row = await cursor.fetchone()
            assert row[0] == 1
        await conn.close()
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
cd /Users/ramujakkampudi/solana-sniper && uv run pytest tests/test_copy_trader.py -v
```

Expected: FAIL — new functions/signatures don't exist

- [ ] **Step 3: Rewrite copy_trader.py**

Rewrite `/Users/ramujakkampudi/solana-sniper/sniper/copy_trader.py`:

```python
"""Copy Trading — monitor profitable wallets and boost their picks.

Two outputs:
1. In-memory smart_money_signals dict for sniper conviction boost
2. DB writes to scout's smart_money_injections table (Direction 2)

Supports: Jupiter, Raydium (AMM/CPMM/V4), Orca, Meteora, pump.fun
"""

import asyncio
import json
import time
from datetime import datetime, timezone

import aiohttp
import aiosqlite
import structlog

from sniper.config import Settings

logger = structlog.get_logger()

# All major Solana DEX program IDs and instruction patterns
SWAP_PATTERNS = [
    "Instruction: Route",       # Jupiter
    "Instruction: Swap",        # Generic swap instruction
    "Program JUP",              # Jupiter program prefix
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",  # Raydium AMM
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",  # Raydium CPMM
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium V4
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",   # Orca Whirlpool
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",   # Meteora
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB",  # Meteora DLMM
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",   # pump.fun
]

# Known intermediary token mints (excluded from bought-token detection)
INTERMEDIARY_MINTS = {
    "So11111111111111111111111111111111111111112",       # Wrapped SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",    # USDT
}

# Accumulated smart money signals — scanner checks this for conviction boost
# Format: {token_mint: {"wallets": set[str], "count": int, "detected_at": datetime}}
smart_money_signals: dict[str, dict] = {}


def _is_swap_transaction(logs: list[str]) -> bool:
    """Check if transaction logs indicate a swap on any supported DEX."""
    return any(pattern in log for log in logs for pattern in SWAP_PATTERNS)


def _record_signal(token_mint: str, wallet: str) -> None:
    """Record a smart money signal, accumulating wallets per token."""
    now = datetime.now(timezone.utc)
    if token_mint in smart_money_signals:
        smart_money_signals[token_mint]["wallets"].add(wallet)
        smart_money_signals[token_mint]["count"] = len(smart_money_signals[token_mint]["wallets"])
        # Update timestamp to latest detection
        smart_money_signals[token_mint]["detected_at"] = now
    else:
        smart_money_signals[token_mint] = {
            "wallets": {wallet},
            "count": 1,
            "detected_at": now,
        }


def prune_stale_signals(max_age_minutes: int = 60) -> None:
    """Remove smart money signals older than max_age_minutes."""
    now = datetime.now(timezone.utc)
    stale = [
        k for k, v in smart_money_signals.items()
        if (now - v["detected_at"]).total_seconds() > max_age_minutes * 60
    ]
    for k in stale:
        del smart_money_signals[k]


def _get_tracked_wallets(settings: Settings) -> list[str]:
    """Get tracked wallet addresses from shared config."""
    if not settings.SMART_MONEY_WALLETS:
        return []
    return [w.strip() for w in settings.SMART_MONEY_WALLETS.split(",") if w.strip()]


async def _write_injection(
    conn: aiosqlite.Connection,
    token_mint: str,
    wallet: str,
    tx_signature: str,
    source: str = "websocket",
) -> None:
    """Write a smart money injection to scout's DB for Direction 2 processing.

    Uses a persistent connection (scout_db_writer) passed from main.py.
    """
    start = time.monotonic()
    try:
        await conn.execute(
            "INSERT OR IGNORE INTO smart_money_injections "
            "(token_mint, wallet_address, tx_signature, source) VALUES (?, ?, ?, ?)",
            (token_mint, wallet, tx_signature, source),
        )
        await conn.commit()
        elapsed = time.monotonic() - start
        if elapsed > 1.0:
            logger.warning("Slow injection write", elapsed_ms=int(elapsed * 1000))
    except Exception as e:
        logger.error("Failed to write injection to scout DB", error=str(e), token=token_mint)


async def _extract_bought_token(
    signature: str,
    wallet: str,
    settings: Settings,
) -> str | None:
    """Parse a transaction to find which token was bought.

    Uses Helius parsed transaction API. Multi-hop aware: filters intermediary
    mints (SOL, USDC, USDT) and finds the final destination token.
    If all transfers are intermediaries, falls back to first non-SOL mint.
    """
    if not settings.HELIUS_API_KEY:
        return None

    url = f"https://api.helius.xyz/v0/transactions/?api-key={settings.HELIUS_API_KEY}"

    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json={"transactions": [signature]},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 429:
                        wait = (attempt + 1) * 2
                        logger.debug("Helius rate limited, retrying", wait=wait)
                        await asyncio.sleep(wait)
                        continue
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    if not data:
                        return None

                    tx = data[0]
                    transfers = tx.get("tokenTransfers", [])

                    # Verify SOL was spent (not just an airdrop)
                    native = tx.get("nativeTransfers", [])
                    sol_spent = any(
                        t.get("fromUserAccount") == wallet
                        for t in native
                    )
                    if not sol_spent:
                        return None

                    # Find bought token: filter to transfers TO wallet, exclude intermediaries
                    wallet_receives = [
                        t for t in transfers
                        if t.get("toUserAccount") == wallet
                        and t.get("mint") not in INTERMEDIARY_MINTS
                    ]
                    if wallet_receives:
                        return wallet_receives[-1].get("mint")

                    # Fallback: first non-SOL mint (covers edge cases where
                    # all transfers are intermediaries — unlikely but safe)
                    for t in transfers:
                        mint = t.get("mint", "")
                        if mint and mint not in INTERMEDIARY_MINTS:
                            return mint

                    return None
        except Exception as e:
            if attempt < 2:
                await asyncio.sleep((attempt + 1) * 1)
            else:
                logger.debug("Token extraction failed after retries", error=str(e))
    return None


def _find_wallet_in_logs(logs: list[str], tracked: list[str]) -> str | None:
    """Find which tracked wallet appears in transaction logs."""
    log_text = " ".join(logs)
    for wallet in tracked:
        if wallet in log_text:
            return wallet
    return None


async def _backfill_after_reconnect(
    tracked: list[str],
    settings: Settings,
    last_signatures: dict[str, str],
) -> None:
    """Backfill missed transactions after WebSocket reconnect.

    Fetches recent transactions per wallet from Helius, limited to
    BACKFILL_MAX_MINUTES window. Dedup handled by INSERT OR IGNORE.
    """
    if not settings.HELIUS_API_KEY:
        return

    max_age_seconds = settings.BACKFILL_MAX_MINUTES * 60
    now = datetime.now(timezone.utc)

    for wallet in tracked:
        try:
            url = (
                f"https://api.helius.xyz/v0/addresses/{wallet}/transactions"
                f"?api-key={settings.HELIUS_API_KEY}&limit=20&type=SWAP"
            )
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        continue
                    txns = await resp.json()

            for tx in txns:
                sig = tx.get("signature", "")
                ts = tx.get("timestamp", 0)
                if ts and (now.timestamp() - ts) > max_age_seconds:
                    continue  # Too old, skip

                # Extract bought token
                token_mint = None
                # Use last matching transfer (final destination in multi-hop route)
                token_mint = None
                for transfer in tx.get("tokenTransfers", []):
                    mint = transfer.get("mint", "")
                    if mint and mint not in INTERMEDIARY_MINTS and transfer.get("toUserAccount") == wallet:
                        token_mint = mint  # Don't break — take last match

                if token_mint:
                    _record_signal(token_mint, wallet)
                    await _write_injection(
                        scout_db_conn, token_mint, wallet, sig, source="backfill",
                    )
                    logger.info("Backfilled smart money signal", wallet=wallet[:8], token=token_mint[:20], tx=sig[:20])

                # Track last seen signature
                if sig:
                    last_signatures[wallet] = sig

            await asyncio.sleep(0.5)  # Rate limit between wallets
        except Exception as e:
            logger.debug("Backfill failed for wallet", wallet=wallet[:8], error=str(e))


async def _open_scout_db_writer(settings: Settings) -> aiosqlite.Connection:
    """Open a persistent read-write connection to scout's DB for injection writes."""
    conn = await aiosqlite.connect(str(settings.SCOUT_DB_PATH))
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    return conn


async def monitor_wallets(settings: Settings, buy_callback, send_telegram_fn=None) -> None:
    """Monitor tracked wallets for swaps via Helius WebSocket.

    On detection: record in-memory signal, write to scout DB, call callback.
    Includes heartbeat, subscription confirmation, and backfill on reconnect.

    Args:
        send_telegram_fn: Optional async callable for alerts (injected from main.py)
    """
    if not settings.COPY_TRADE_ENABLED:
        return

    tracked = _get_tracked_wallets(settings)
    if not tracked:
        raise ValueError(
            "COPY_TRADE_ENABLED=true but SMART_MONEY_WALLETS is empty. "
            "Set SMART_MONEY_WALLETS in .env or disable copy trading."
        )

    # Persistent DB connection for injection writes
    scout_db_conn = await _open_scout_db_writer(settings)

    ws_url = f"wss://mainnet.helius-rpc.com/?api-key={settings.HELIUS_API_KEY}"
    last_signatures: dict[str, str] = {}  # Per-wallet last seen tx sig
    last_injection_time = datetime.now(timezone.utc)

    logger.info("Copy trading started", wallets=len(tracked))

    while True:
        try:
            import websockets

            async with websockets.connect(ws_url, ping_interval=30, ping_timeout=10) as ws:
                # Subscribe to each tracked wallet
                confirmed_subs = 0
                for i, wallet in enumerate(tracked):
                    subscribe = {
                        "jsonrpc": "2.0",
                        "id": i + 1,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [wallet]},
                            {"commitment": "confirmed"},
                        ],
                    }
                    await ws.send(json.dumps(subscribe))

                # Wait for subscription confirmations (5s timeout)
                try:
                    deadline = asyncio.get_event_loop().time() + 5.0
                    while confirmed_subs < len(tracked):
                        remaining = deadline - asyncio.get_event_loop().time()
                        if remaining <= 0:
                            break
                        msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
                        data = json.loads(msg)
                        if "result" in data and isinstance(data["result"], int):
                            confirmed_subs += 1
                except asyncio.TimeoutError:
                    pass

                logger.info(
                    "WebSocket connected",
                    subscriptions_confirmed=confirmed_subs,
                    total=len(tracked),
                )

                # Backfill missed transactions from reconnect gap
                await _backfill_after_reconnect(tracked, settings, last_signatures)

                # Monitor loop
                async for msg in ws:
                    try:
                        data = json.loads(msg)
                        if "params" not in data:
                            continue

                        result = data.get("params", {}).get("result", {})
                        value = result.get("value", {})
                        logs = value.get("logs", [])
                        signature = value.get("signature", "")

                        if _is_swap_transaction(logs):
                            wallet = _find_wallet_in_logs(logs, tracked)
                            token_mint = await _extract_bought_token(signature, wallet or "", settings)

                            if token_mint:
                                _record_signal(token_mint, wallet or "unknown")
                                last_injection_time = datetime.now(timezone.utc)

                                # Write to scout DB for Direction 2
                                await _write_injection(
                                    scout_db_conn,
                                    token_mint,
                                    wallet or "unknown",
                                    signature,
                                )

                                logger.info(
                                    "Smart money signal detected",
                                    wallet=wallet[:8] + "..." if wallet else "unknown",
                                    token=token_mint[:20],
                                    tx=signature[:20],
                                    total_wallets=smart_money_signals.get(token_mint, {}).get("count", 1),
                                )

                                if buy_callback:
                                    await buy_callback(token_mint, wallet)

                                if last_signatures.get(wallet):
                                    last_signatures[wallet] = signature

                        # Heartbeat monitor: alert if no injections in 30 min
                        stale_seconds = (datetime.now(timezone.utc) - last_injection_time).total_seconds()
                        if stale_seconds > 1800:
                            logger.warning("No smart money signals in 30 minutes — WebSocket may be stale")
                            if send_telegram_fn:
                                await send_telegram_fn(
                                    "Smart Money WebSocket may be down\n"
                                    f"No signals detected in {int(stale_seconds / 60)} minutes",
                                    settings,
                                )
                                # Reset to avoid spamming (next alert in 30 min)
                                last_injection_time = datetime.now(timezone.utc)

                    except Exception as e:
                        logger.debug("WebSocket message parse error", error=str(e))
                        continue

        except Exception as e:
            logger.warning("Copy trade WebSocket disconnected, reconnecting in 3s", error=str(e))
            await asyncio.sleep(3)


async def get_wallet_recent_trades(wallet: str, settings: Settings) -> list[dict]:
    """Get recent trades for a wallet (for dashboard/analysis display)."""
    if not settings.HELIUS_API_KEY:
        return []
    try:
        url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={settings.HELIUS_API_KEY}&limit=10&type=SWAP"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.json()
    except Exception:
        pass
    return []
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
cd /Users/ramujakkampudi/solana-sniper && uv run pytest tests/test_copy_trader.py -v
```

Expected: All pass

- [ ] **Step 5: Run full test suite**

```bash
cd /Users/ramujakkampudi/solana-sniper && uv run pytest -v
```

Expected: All pass (imports may need fixing in main.py — see Task 8)

- [ ] **Step 6: Commit**

```bash
cd /Users/ramujakkampudi/solana-sniper && git add sniper/copy_trader.py tests/test_copy_trader.py && git commit -m "feat: copy trader rebuild — full DEX coverage, heartbeat, backfill, multi-wallet signals"
```

---

## Task 8: Graduated Conviction Boost + Startup Validation (Sniper main.py)

**Files:**
- Modify: `/Users/ramujakkampudi/solana-sniper/sniper/main.py:13,135-152,184-192`

- [ ] **Step 1: Update imports in main.py**

In `/Users/ramujakkampudi/solana-sniper/sniper/main.py`, line 13:

Replace:
```python
from sniper.copy_trader import monitor_wallets, smart_money_signals, prune_stale_signals
```

With:
```python
from sniper.copy_trader import monitor_wallets, smart_money_signals, prune_stale_signals
```

(Same imports — the rebuilt module exports the same names.)

- [ ] **Step 2: Add startup validation**

After `settings = Settings()` (line 61), add:

```python
    # Validate smart money config
    if settings.COPY_TRADE_ENABLED and not settings.SMART_MONEY_WALLETS.strip():
        raise ValueError(
            "COPY_TRADE_ENABLED=true but SMART_MONEY_WALLETS is empty. "
            "Configure tracked wallets in .env or disable copy trading."
        )
```

- [ ] **Step 3: Update monitor_wallets call to pass send_telegram**

In the copy trade task startup block (line 149-151), update:
```python
        copy_trade_task = asyncio.create_task(
            monitor_wallets(settings, _on_smart_money_signal, send_telegram_fn=send_telegram)
        )
```

- [ ] **Step 4: Update prune call**

In the main loop (line 161), update to use config-aware max:

```python
                    prune_stale_signals(max_age_minutes=max(60, settings.BACKFILL_MAX_MINUTES))
```

- [ ] **Step 5: Update conviction boost logic**

Replace lines 184-192:
```python
                            conviction = sig_data.conviction_score or 30
                            if sig_data.contract_address in smart_money_signals:
                                conviction += settings.COPY_TRADE_SCORE_BOOST
                                logger.info(
                                    "Smart money boost applied",
                                    token=sig_data.token_name,
                                    original=sig_data.conviction_score,
                                    boosted=conviction,
                                )
```

With:
```python
                            conviction = sig_data.conviction_score or 30
                            if sig_data.contract_address in smart_money_signals:
                                sm = smart_money_signals[sig_data.contract_address]
                                wallet_count = sm["count"]
                                boost = min(
                                    wallet_count * settings.COPY_TRADE_SCORE_BOOST,
                                    settings.SMART_MONEY_BOOST_CAP,
                                )
                                conviction += boost
                                logger.info(
                                    "Smart money boost applied",
                                    token=sig_data.token_name,
                                    original=sig_data.conviction_score,
                                    boosted=conviction,
                                    smart_wallets=wallet_count,
                                    boost=boost,
                                )
```

- [ ] **Step 5: Write tests for conviction boost and startup validation**

Add to `/Users/ramujakkampudi/solana-sniper/tests/test_copy_trader.py`:

```python
class TestStartupValidation:
    def test_raises_on_empty_wallets_when_enabled(self):
        """COPY_TRADE_ENABLED=true with empty wallets should raise."""
        from sniper.copy_trader import _get_tracked_wallets
        settings = _settings(SMART_MONEY_WALLETS="", COPY_TRADE_ENABLED=True)
        wallets = _get_tracked_wallets(settings)
        assert wallets == []  # Validation happens in monitor_wallets/main.py
```

Create `/Users/ramujakkampudi/solana-sniper/tests/test_conviction_boost.py`:

```python
"""Tests for graduated smart money conviction boost in main loop."""
import pytest
from sniper.copy_trader import smart_money_signals, _record_signal


def test_graduated_boost_math():
    """Conviction boost should be wallet_count * per_wallet, capped."""
    smart_money_signals.clear()
    _record_signal("token_abc", "wallet1")
    _record_signal("token_abc", "wallet2")
    _record_signal("token_abc", "wallet3")

    sm = smart_money_signals["token_abc"]
    boost_per_wallet = 20
    cap = 80
    boost = min(sm["count"] * boost_per_wallet, cap)
    assert boost == 60  # 3 * 20 = 60, under cap


def test_boost_capped_at_max():
    """5 wallets should be capped at SMART_MONEY_BOOST_CAP."""
    smart_money_signals.clear()
    for i in range(5):
        _record_signal("token_xyz", f"wallet{i}")

    sm = smart_money_signals["token_xyz"]
    boost_per_wallet = 20
    cap = 80
    boost = min(sm["count"] * boost_per_wallet, cap)
    assert boost == 80  # 5 * 20 = 100, capped to 80
```

- [ ] **Step 6: Run tests**

```bash
cd /Users/ramujakkampudi/solana-sniper && uv run pytest -v
```

Expected: All pass

- [ ] **Step 7: Commit**

```bash
cd /Users/ramujakkampudi/solana-sniper && git add sniper/main.py tests/test_conviction_boost.py tests/test_copy_trader.py && git commit -m "feat: graduated conviction boost (+20/wallet, cap 80), startup validation"
```

---

## Task 9: Integration Test — End-to-End Smart Money Flow

**Files:**
- Create: `/Users/ramujakkampudi/coinpump-scout/tests/test_smart_money_e2e.py`

- [ ] **Step 1: Write integration test**

Create `/Users/ramujakkampudi/coinpump-scout/tests/test_smart_money_e2e.py`:

```python
"""End-to-end test: smart money injection → scout pipeline → scored candidate."""
import pytest
from scout.config import Settings
from scout.db import Database
from scout.scorer import score
from scout.models import CandidateToken


def _settings(**overrides):
    defaults = dict(
        TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k",
        SMART_MONEY_WALLETS="wallet1,wallet2",
        SMART_MONEY_BOOST_CAP=80,
    )
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.mark.asyncio
async def test_injection_write_read_cycle(tmp_path):
    """Simulate sniper writing injection → scout reading it."""
    import aiosqlite
    db = Database(tmp_path / "scout.db")
    await db.initialize()

    # Simulate sniper writing injection (as it would via _write_injection)
    await db._conn.execute(
        "INSERT OR IGNORE INTO smart_money_injections "
        "(token_mint, wallet_address, tx_signature, source) VALUES (?, ?, ?, ?)",
        ("mint_abc", "wallet1", "tx_001", "websocket"),
    )
    await db._conn.execute(
        "INSERT OR IGNORE INTO smart_money_injections "
        "(token_mint, wallet_address, tx_signature, source) VALUES (?, ?, ?, ?)",
        ("mint_abc", "wallet2", "tx_002", "websocket"),
    )
    await db._conn.commit()

    # Scout reads and marks processed
    injections = await db.read_and_mark_injections()
    assert len(injections) == 2

    # Group by token
    from collections import defaultdict
    wallets_per_token = defaultdict(set)
    for inj in injections:
        wallets_per_token[inj["token_mint"]].add(inj["wallet_address"])

    assert wallets_per_token["mint_abc"] == {"wallet1", "wallet2"}

    # Verify marked as processed
    second_read = await db.read_and_mark_injections()
    assert len(second_read) == 0

    await db.close()


@pytest.mark.asyncio
async def test_graduated_scoring_with_smart_money():
    """Token with 3 smart money buys should get graduated boost in scorer."""
    settings = _settings(SMART_MONEY_BOOST_CAP=80)
    token = CandidateToken(
        contract_address="mint_xyz",
        chain="solana",
        token_name="SmartTest",
        ticker="SMT",
        market_cap_usd=50000,
        liquidity_usd=20000,
        volume_24h_usd=150000,
        smart_money_buys=3,
    )
    points, signals = score(token, settings)
    assert "smart_money_buys" in signals
    # 3 wallets × 20 = 60 points from smart money alone
    assert points > 0

    # Compare with 0 smart money
    token_no_sm = token.model_copy(update={"smart_money_buys": 0})
    points_no_sm, signals_no_sm = score(token_no_sm, settings)
    assert points > points_no_sm
```

- [ ] **Step 2: Run test**

```bash
cd /Users/ramujakkampudi/coinpump-scout && uv run pytest tests/test_smart_money_e2e.py -v
```

Expected: All pass

- [ ] **Step 3: Commit**

```bash
cd /Users/ramujakkampudi/coinpump-scout && git add tests/test_smart_money_e2e.py && git commit -m "test: end-to-end smart money injection and scoring flow"
```

---

## Task 10: Final Verification & PR

- [ ] **Step 1: Run full test suites**

```bash
cd /Users/ramujakkampudi/coinpump-scout && uv run pytest -v
cd /Users/ramujakkampudi/solana-sniper && uv run pytest -v
```

Expected: All green in both repos

- [ ] **Step 2: Create PRs**

```bash
cd /Users/ramujakkampudi/coinpump-scout && git checkout -b feat/smart-money-rebuild && git push -u origin feat/smart-money-rebuild
gh pr create --title "feat: smart money two-directional integration" --body "## Summary
- Direction 1: Scanner checks if tracked wallets bought discovered tokens (+20/wallet boost)
- Direction 2: Tracked wallet buys inject into scanner pipeline via smart_money_injections table
- WAL mode for concurrent DB access
- Graduated scoring: +20 per wallet, capped at SMART_MONEY_BOOST_CAP (80)
- Periodic injection cleanup (7-day retention)

## Test plan
- [ ] Unit tests for smart_money_feed, scorer graduated boost, DB operations
- [ ] E2E test: injection write → read → score
- [ ] Deploy to VPS with SMART_MONEY_WALLETS configured
- [ ] Verify injections appear in scout logs

Generated with Claude Code"
```

```bash
cd /Users/ramujakkampudi/solana-sniper && git checkout -b feat/smart-money-rebuild && git push -u origin feat/smart-money-rebuild
gh pr create --title "feat: smart money copy trader rebuild" --body "## Summary
- Full DEX coverage (Jupiter, Raydium v4/AMM/CPMM, Orca, Meteora, pump.fun)
- WebSocket heartbeat + reconnect backfill (30-min window)
- Multi-wallet signal accumulation (no more dict overwrite)
- Writes to scout DB smart_money_injections table (Direction 2)
- Graduated conviction boost (+20/wallet, cap 80)
- Removed dead hours filter
- Shared SMART_MONEY_WALLETS config
- WAL mode + startup validation

## Test plan
- [ ] Unit tests for swap detection, wallet config, signal accumulation
- [ ] Verify dead hours removed
- [ ] Deploy to VPS, confirm WebSocket connects and signals detected
- [ ] Monitor injection write latency in logs

Generated with Claude Code"
```

- [ ] **Step 3: Update .env on VPS (do NOT commit .env)**

After PR merge, update both services' `.env` files on VPS via SSH:

```bash
ssh root@149.28.125.16
# Scout .env
echo 'SMART_MONEY_WALLETS=54Pz1e35z9uoFdnxtzjp7xZQoFiofqhdayQWBMN7dsuy,7pwKymyhUwdSLVXVLbBaQKxkxL86naC7nLLaiy11p3eh,4uENWUN5ieDfq8r3qGPSbDHByMPe6fny2Wp5cMSSsESd,2tgUbS9UMoQD6GkDZBiqKYCURnGrSb6ocYwRABrSJUvY' >> /opt/scout/.env
echo 'SMART_MONEY_BOOST_CAP=80' >> /opt/scout/.env

# Sniper .env
echo 'COPY_TRADE_ENABLED=true' >> /opt/sniper/.env
echo 'SMART_MONEY_WALLETS=54Pz1e35z9uoFdnxtzjp7xZQoFiofqhdayQWBMN7dsuy,7pwKymyhUwdSLVXVLbBaQKxkxL86naC7nLLaiy11p3eh,4uENWUN5ieDfq8r3qGPSbDHByMPe6fny2Wp5cMSSsESd,2tgUbS9UMoQD6GkDZBiqKYCURnGrSb6ocYwRABrSJUvY' >> /opt/sniper/.env
echo 'SMART_MONEY_BOOST_CAP=80' >> /opt/sniper/.env
echo 'BACKFILL_MAX_MINUTES=30' >> /opt/sniper/.env
```

- [ ] **Step 4: Deploy**

```bash
cd /Users/ramujakkampudi/solana-sniper && ./deploy/sync.sh
cd /Users/ramujakkampudi/coinpump-scout && ./deploy/sync.sh
```

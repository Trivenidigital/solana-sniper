"""Tests for signal reader."""

from datetime import datetime, timezone

import aiosqlite
import pytest

from sniper.config import Settings
from sniper.db import Database
from sniper.signal_reader import filter_actionable, read_new_signals


async def _create_scout_db(path):
    """Create a mock scout database with test data."""
    async with aiosqlite.connect(str(path)) as conn:
        await conn.executescript("""
            CREATE TABLE candidates (
                contract_address TEXT PRIMARY KEY,
                chain TEXT NOT NULL,
                token_name TEXT NOT NULL,
                ticker TEXT NOT NULL,
                market_cap_usd REAL DEFAULT 0,
                liquidity_usd REAL DEFAULT 0,
                volume_24h_usd REAL DEFAULT 0
            );
            CREATE TABLE alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_address TEXT NOT NULL,
                chain TEXT NOT NULL,
                conviction_score REAL NOT NULL,
                alerted_at TEXT NOT NULL,
                FOREIGN KEY (contract_address) REFERENCES candidates(contract_address)
            );
        """)
        await conn.execute(
            "INSERT INTO candidates VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("Sol111", "solana", "GoodToken", "GT", 100000, 50000, 20000),
        )
        await conn.execute(
            "INSERT INTO alerts VALUES (NULL, ?, ?, ?, ?)",
            ("Sol111", "solana", 85.0, "2025-01-01T12:00:00+00:00"),
        )
        await conn.execute(
            "INSERT INTO candidates VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("Sol222", "solana", "LowConviction", "LC", 50000, 30000, 10000),
        )
        await conn.execute(
            "INSERT INTO alerts VALUES (NULL, ?, ?, ?, ?)",
            ("Sol222", "solana", 40.0, "2025-01-01T12:00:00+00:00"),
        )
        await conn.commit()


async def test_read_new_signals(tmp_path):
    db_path = tmp_path / "scout.db"
    await _create_scout_db(db_path)

    since = datetime(2024, 1, 1, tzinfo=timezone.utc)
    signals = await read_new_signals(db_path, since, min_conviction=70.0)

    assert len(signals) == 1
    assert signals[0].token_name == "GoodToken"
    assert signals[0].conviction_score == 85.0


async def test_read_new_signals_filters_by_time(tmp_path):
    db_path = tmp_path / "scout.db"
    await _create_scout_db(db_path)

    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    signals = await read_new_signals(db_path, since, min_conviction=70.0)

    assert len(signals) == 0


async def test_read_new_signals_missing_db(tmp_path):
    signals = await read_new_signals(tmp_path / "nonexistent.db", datetime.min.replace(tzinfo=timezone.utc), 70.0)
    assert signals == []


async def test_filter_actionable_removes_low_liquidity(tmp_path):
    db_path = tmp_path / "scout.db"
    await _create_scout_db(db_path)

    since = datetime(2024, 1, 1, tzinfo=timezone.utc)
    signals = await read_new_signals(db_path, since, min_conviction=0)

    sniper_db = Database(tmp_path / "sniper.db")
    await sniper_db.initialize()
    settings = Settings(MIN_LIQUIDITY_USD=100000)

    actionable = await filter_actionable(signals, sniper_db, settings)
    assert len(actionable) == 0
    await sniper_db.close()

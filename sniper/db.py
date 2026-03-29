"""Async SQLite database layer for the sniper bot."""

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import structlog

from sniper.models import Position

logger = structlog.get_logger()


class Database:
    """Thin async wrapper around an aiosqlite connection."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._create_tables()
        await self._migrate_tables()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def _create_tables(self) -> None:
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS positions (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_address   TEXT NOT NULL,
                token_name         TEXT NOT NULL,
                ticker             TEXT NOT NULL,
                entry_sol          REAL NOT NULL,
                entry_token_amount REAL NOT NULL,
                entry_price_usd    REAL DEFAULT 0,
                entry_tx           TEXT,
                exit_sol           REAL,
                exit_price_usd     REAL,
                exit_tx            TEXT,
                exit_reason        TEXT,
                status             TEXT NOT NULL DEFAULT 'open',
                pnl_sol            REAL,
                pnl_pct            REAL,
                paper              INTEGER NOT NULL DEFAULT 1,
                opened_at          TEXT NOT NULL,
                closed_at          TEXT
            );

            CREATE TABLE IF NOT EXISTS trades (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id   INTEGER NOT NULL,
                side          TEXT NOT NULL,
                sol_amount    REAL NOT NULL,
                token_amount  REAL NOT NULL,
                tx_signature  TEXT,
                price_usd     REAL,
                executed_at   TEXT NOT NULL,
                FOREIGN KEY (position_id) REFERENCES positions(id)
            );

            CREATE TABLE IF NOT EXISTS cooldowns (
                contract_address TEXT PRIMARY KEY,
                cooldown_until   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS kv_store (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

    async def _migrate_tables(self) -> None:
        """Add columns that may not exist in older databases."""
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        # Attempt to add new columns; ignore errors if they already exist.
        for col, typedef in [
            ("peak_value_sol", "REAL"),
            ("trailing_active", "INTEGER DEFAULT 0"),
            ("partial_exit_done", "INTEGER DEFAULT 0"),
            ("partial_exit_tier", "INTEGER DEFAULT 0"),
            ("sell_fail_count", "INTEGER DEFAULT 0"),
            ("dca_completed", "INTEGER DEFAULT 0"),
            ("decimals", "INTEGER"),
            ("conviction_score", "REAL"),
            ("entry_liquidity_usd", "REAL DEFAULT 0"),
            ("entry_mcap_usd", "REAL DEFAULT 0"),
            ("manual", "INTEGER DEFAULT 0"),
        ]:
            try:
                await self._conn.execute(
                    f"ALTER TABLE positions ADD COLUMN {col} {typedef}"
                )
            except Exception as e:
                logger.debug("Migration skipped (column likely exists)", column=col, error=str(e))

        # Add indexes
        try:
            await self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_positions_address_status "
                "ON positions (contract_address, status)"
            )
        except Exception as e:
            logger.debug("Index creation skipped", error=str(e))

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    async def open_position(self, pos: Position) -> int:
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        cursor = await self._conn.execute(
            """INSERT INTO positions
               (contract_address, token_name, ticker, entry_sol, entry_token_amount,
                entry_price_usd, entry_tx, status, paper, opened_at, decimals,
                conviction_score, entry_liquidity_usd, entry_mcap_usd, manual)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?)""",
            (
                pos.contract_address, pos.token_name, pos.ticker,
                pos.entry_sol, pos.entry_token_amount, pos.entry_price_usd,
                pos.entry_tx, 1 if pos.paper else 0,
                pos.opened_at.isoformat(), pos.decimals,
                pos.conviction_score, pos.entry_liquidity_usd, pos.entry_mcap_usd,
                1 if pos.manual else 0,
            ),
        )
        await self._conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def close_position(
        self,
        position_id: int,
        exit_sol: float,
        exit_price_usd: float,
        exit_tx: str | None,
        exit_reason: str,
        pnl_sol: float,
        pnl_pct: float,
    ) -> None:
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """UPDATE positions SET
               exit_sol=?, exit_price_usd=?, exit_tx=?, exit_reason=?,
               status='closed', pnl_sol=?, pnl_pct=?, closed_at=?
               WHERE id=?""",
            (exit_sol, exit_price_usd, exit_tx, exit_reason, pnl_sol, pnl_pct, now, position_id),
        )
        await self._conn.commit()

    async def get_open_positions(self) -> list[Position]:
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        cursor = await self._conn.execute(
            "SELECT * FROM positions WHERE status='open'"
        )
        rows = await cursor.fetchall()
        return [self._row_to_position(row) for row in rows]

    async def get_open_position_by_address(self, contract_address: str) -> Position | None:
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        cursor = await self._conn.execute(
            "SELECT * FROM positions WHERE contract_address=? AND status='open' LIMIT 1",
            (contract_address,),
        )
        row = await cursor.fetchone()
        return self._row_to_position(row) if row else None

    async def has_open_position(self, contract_address: str) -> bool:
        """Check if we already hold an open position for this token."""
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        cursor = await self._conn.execute(
            "SELECT 1 FROM positions WHERE contract_address=? AND status='open' LIMIT 1",
            (contract_address,),
        )
        return await cursor.fetchone() is not None

    async def count_open_positions(self) -> int:
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM positions WHERE status='open'"
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_total_exposure_sol(self) -> float:
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        cursor = await self._conn.execute(
            "SELECT COALESCE(SUM(entry_sol), 0) FROM positions WHERE status='open'"
        )
        row = await cursor.fetchone()
        return float(row[0]) if row else 0.0

    async def get_realized_pnl(self) -> float:
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        cursor = await self._conn.execute(
            "SELECT COALESCE(SUM(pnl_sol), 0) FROM positions WHERE status='closed'"
        )
        row = await cursor.fetchone()
        return float(row[0]) if row else 0.0

    async def get_recent_closed(self, limit: int = 20) -> list[dict]:
        """Get the N most recent closed positions for Kelly calculation."""
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        cursor = await self._conn.execute(
            "SELECT pnl_sol, pnl_pct, exit_reason FROM positions "
            "WHERE status='closed' AND pnl_sol IS NOT NULL AND pnl_sol != 0 "
            "AND exit_reason NOT LIKE 'partial%' "
            "ORDER BY closed_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def recent_consecutive_losses(self, hours: int = 1) -> int:
        """Count consecutive losses from most recent closed positions within N hours.

        Returns the streak length (0 if last trade was a win).
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        cursor = await self._conn.execute(
            "SELECT pnl_pct FROM positions "
            "WHERE status='closed' AND paper=0 AND closed_at > ? "
            "ORDER BY closed_at DESC",
            (cutoff,),
        )
        rows = await cursor.fetchall()
        streak = 0
        for row in rows:
            if (row[0] or 0) <= 0:
                streak += 1
            else:
                break
        return streak

    # ------------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------------

    async def log_trade(
        self,
        position_id: int,
        side: str,
        sol_amount: float,
        token_amount: float,
        tx_signature: str | None,
        price_usd: float | None,
    ) -> None:
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """INSERT INTO trades
               (position_id, side, sol_amount, token_amount, tx_signature, price_usd, executed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (position_id, side, sol_amount, token_amount, tx_signature, price_usd, now),
        )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # Cooldowns
    # ------------------------------------------------------------------

    async def set_cooldown(self, contract_address: str, hours: int) -> None:
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        from datetime import timedelta
        until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
        await self._conn.execute(
            """INSERT OR REPLACE INTO cooldowns (contract_address, cooldown_until)
               VALUES (?, ?)""",
            (contract_address, until),
        )
        await self._conn.commit()

    async def is_on_cooldown(self, contract_address: str) -> bool:
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        cursor = await self._conn.execute(
            "SELECT cooldown_until FROM cooldowns WHERE contract_address=?",
            (contract_address,),
        )
        row = await cursor.fetchone()
        if not row:
            return False
        cooldown_until = datetime.fromisoformat(row[0])
        return datetime.now(timezone.utc) < cooldown_until

    # ------------------------------------------------------------------
    # Peak value / trailing / partial exit
    # ------------------------------------------------------------------

    async def update_peak_value(self, position_id: int, peak_sol: float) -> None:
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        await self._conn.execute(
            "UPDATE positions SET peak_value_sol=? WHERE id=?",
            (peak_sol, position_id),
        )
        await self._conn.commit()

    async def set_trailing_active(self, position_id: int) -> None:
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        await self._conn.execute(
            "UPDATE positions SET trailing_active=1 WHERE id=?",
            (position_id,),
        )
        await self._conn.commit()

    async def mark_partial_exit(self, position_id: int, remaining_tokens: float) -> None:
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        await self._conn.execute(
            "UPDATE positions SET partial_exit_done=1, entry_token_amount=? WHERE id=?",
            (remaining_tokens, position_id),
        )
        await self._conn.commit()

    async def update_partial_exit(
        self, position_id: int, new_entry_sol: float, new_token_amount: float, partial_tier: int,
    ) -> None:
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        await self._conn.execute(
            "UPDATE positions SET entry_sol=?, entry_token_amount=?, partial_exit_tier=?, partial_exit_done=1 WHERE id=?",
            (new_entry_sol, new_token_amount, partial_tier, position_id),
        )
        await self._conn.commit()

    async def increment_sell_fail(self, position_id: int) -> int:
        """Increment sell fail count, return new count."""
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        await self._conn.execute(
            "UPDATE positions SET sell_fail_count = sell_fail_count + 1 WHERE id = ?", (position_id,)
        )
        await self._conn.commit()
        cursor = await self._conn.execute("SELECT sell_fail_count FROM positions WHERE id = ?", (position_id,))
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def reset_sell_fail(self, position_id: int) -> None:
        """Reset sell fail count to 0 (for retry after position still has value)."""
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        await self._conn.execute(
            "UPDATE positions SET sell_fail_count = 0 WHERE id = ?", (position_id,)
        )
        await self._conn.commit()

    async def mark_dca_completed(self, position_id: int) -> None:
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        await self._conn.execute("UPDATE positions SET dca_completed = 1 WHERE id = ?", (position_id,))
        await self._conn.commit()

    async def update_dca_entry(self, position_id: int, new_entry_sol: float, new_token_amount: float) -> None:
        """Update position entry after DCA buy. Does NOT touch partial_exit_tier."""
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        await self._conn.execute(
            "UPDATE positions SET entry_sol=?, entry_token_amount=? WHERE id=?",
            (new_entry_sol, new_token_amount, position_id),
        )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # Key-Value store
    # ------------------------------------------------------------------

    async def kv_get(self, key: str) -> str | None:
        if self._conn is None:
            return None
        cursor = await self._conn.execute("SELECT value FROM kv_store WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else None

    async def kv_set(self, key: str, value: str) -> None:
        if self._conn is None:
            return
        await self._conn.execute(
            "INSERT OR REPLACE INTO kv_store (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (key, value),
        )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_position(row: aiosqlite.Row) -> Position:
        d = dict(row)
        d["paper"] = bool(d.get("paper", 1))
        d["trailing_active"] = bool(d.get("trailing_active", 0))
        d["partial_exit_done"] = bool(d.get("partial_exit_done", 0))
        d["partial_exit_tier"] = int(d.get("partial_exit_tier", 0))
        d["sell_fail_count"] = int(d.get("sell_fail_count", 0))
        d["dca_completed"] = int(d.get("dca_completed", 0))
        d["decimals"] = d.get("decimals")  # may be None for legacy rows
        d["conviction_score"] = d.get("conviction_score")  # may be None for legacy rows
        d["manual"] = bool(d.get("manual", 0))
        if d.get("opened_at"):
            d["opened_at"] = datetime.fromisoformat(d["opened_at"])
        if d.get("closed_at"):
            d["closed_at"] = datetime.fromisoformat(d["closed_at"])
        return Position(**d)

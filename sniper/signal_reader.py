"""Read high-conviction signals from coinpump-scout's database."""

from datetime import datetime
from pathlib import Path

import aiosqlite
import structlog

from sniper.config import Settings
from sniper.db import Database
from sniper.models import Signal

logger = structlog.get_logger()


async def read_new_signals(
    scout_db_path: Path,
    since: datetime,
    min_conviction: float,
) -> list[Signal]:
    """Read new Solana alerts from scout's database (read-only).

    Opens a short-lived read-only connection to avoid write contention
    with the running scout process.
    """
    db_uri = f"file:{scout_db_path}?mode=ro"
    signals: list[Signal] = []

    try:
        async with aiosqlite.connect(db_uri, uri=True) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                """
                SELECT c.contract_address, c.chain, c.token_name, c.ticker,
                       a.conviction_score, c.market_cap_usd, c.liquidity_usd,
                       c.volume_24h_usd, a.alerted_at
                FROM alerts a
                JOIN candidates c ON a.contract_address = c.contract_address
                WHERE a.chain = 'solana'
                  AND a.conviction_score >= ?
                  AND a.alerted_at > ?
                ORDER BY a.conviction_score DESC
                """,
                (min_conviction, since.isoformat()),
            )
            rows = await cursor.fetchall()
            for row in rows:
                d = dict(row)
                d["alerted_at"] = datetime.fromisoformat(d["alerted_at"])
                signals.append(Signal(**d))
    except Exception:
        logger.warning("Failed to read scout database", path=str(scout_db_path), exc_info=True)

    if signals:
        logger.info("New signals from scout", count=len(signals))
    return signals


async def filter_actionable(
    signals: list[Signal],
    db: Database,
    settings: Settings,
) -> list[Signal]:
    """Filter signals to only actionable ones.

    Removes signals where:
    - We already have an open position for this token
    - Liquidity is too low
    """
    actionable: list[Signal] = []
    for signal in signals:
        if signal.liquidity_usd < settings.MIN_LIQUIDITY_USD:
            continue
        existing = await db.get_open_position_by_address(signal.contract_address)
        if existing is not None:
            continue
        actionable.append(signal)
    return actionable

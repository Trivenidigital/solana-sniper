"""Read high-conviction signals from coinpump-scout's database."""

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import structlog

from sniper.config import Settings
from sniper.db import Database
from sniper.models import Signal

logger = structlog.get_logger()


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure datetime is timezone-aware (UTC)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


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
            # TODO: The scout should snapshot quality fields (market_cap_usd,
            # liquidity_usd, volume_24h_usd, token_age_days,
            # top3_wallet_concentration, holder_count) into the alerts table
            # at alert time. For now, read only from alerts and use defaults
            # for fields that may not yet be present.
            # Query only columns that exist in the alerts table.
            # The scout's alerts table has: contract_address, chain,
            # conviction_score, alerted_at, market_cap_usd.
            # Other fields default to 0/empty — the live liquidity check
            # in main.py validates before buying.
            cursor = await conn.execute(
                """
                SELECT a.contract_address, a.chain,
                       COALESCE(c.token_name, a.contract_address) AS token_name,
                       COALESCE(c.ticker, '') AS ticker,
                       a.conviction_score,
                       COALESCE(a.market_cap_usd, 0) AS market_cap_usd,
                       0 AS liquidity_usd,
                       0 AS volume_24h_usd,
                       a.alerted_at,
                       0 AS token_age_days,
                       0 AS top3_wallet_concentration,
                       0 AS holder_count
                FROM alerts a
                LEFT JOIN candidates c ON a.contract_address = c.contract_address
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
                alerted_at = datetime.fromisoformat(d["alerted_at"])
                d["alerted_at"] = _ensure_utc(alerted_at)
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
) -> tuple[list[Signal], list[str]]:
    """Filter signals to only actionable ones.

    Removes signals where:
    - We already have an open position for this token
    - Liquidity is too low
    - Token is older than MAX_TOKEN_AGE_DAYS

    Returns:
        (actionable_signals, skipped_descriptions)
    """
    actionable: list[Signal] = []
    skipped_signals: list[str] = []
    now = datetime.now(timezone.utc)

    for signal in signals:
        # Ensure timezone-aware datetime
        signal.alerted_at = _ensure_utc(signal.alerted_at)
        # Signal freshness gate
        signal_age_seconds = (now - signal.alerted_at).total_seconds()
        if signal_age_seconds > settings.MAX_SIGNAL_AGE_SECONDS:
            logger.info("Signal dropped — too old", token=signal.token_name, age_seconds=int(signal_age_seconds),
                        max_age=settings.MAX_SIGNAL_AGE_SECONDS)
            continue
        # Hard quality gates
        if signal.top3_wallet_concentration > settings.MAX_TOP3_CONCENTRATION:
            logger.debug(
                "Top3 wallet concentration too high",
                token=signal.token_name,
                concentration=signal.top3_wallet_concentration,
                max_allowed=settings.MAX_TOP3_CONCENTRATION,
            )
            continue
        # NOTE: holder_count and liquidity_usd are not available from alerts table
        # (they default to 0). Real-time liquidity is checked in main.py via
        # DexScreener before buying. Don't filter on stale/missing data here.
        if signal.token_age_days > settings.MAX_TOKEN_AGE_DAYS:
            logger.debug("Token too old", token=signal.token_name, age_days=signal.token_age_days)
            continue
        existing = await db.get_open_position_by_address(signal.contract_address)
        if existing is not None:
            skipped_signals.append(
                f"{signal.token_name} ({signal.ticker}) — already in open position"
            )
            continue
        actionable.append(signal)
    return actionable, skipped_signals

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
            cursor = await conn.execute(
                """
                SELECT c.contract_address, c.chain, c.token_name, c.ticker,
                       a.conviction_score, c.market_cap_usd, c.liquidity_usd,
                       c.volume_24h_usd, a.alerted_at,
                       COALESCE(c.token_age_days, 0) AS token_age_days,
                       COALESCE(c.top3_wallet_concentration, 0) AS top3_wallet_concentration,
                       COALESCE(c.holder_count, 0) AS holder_count
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
) -> list[Signal]:
    """Filter signals to only actionable ones.

    Removes signals where:
    - We already have an open position for this token
    - Liquidity is too low
    - Token is older than MAX_TOKEN_AGE_DAYS
    - Token is on cooldown after a stop-loss
    """
    actionable: list[Signal] = []
    now = datetime.now(timezone.utc)

    for signal in signals:
        # Ensure timezone-aware datetime
        signal.alerted_at = _ensure_utc(signal.alerted_at)
        # Signal freshness gate
        signal_age_seconds = (now - signal.alerted_at).total_seconds()
        if signal_age_seconds > settings.MAX_SIGNAL_AGE_SECONDS:
            logger.debug("Signal too old", token=signal.token_name, age_seconds=signal_age_seconds)
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
        if signal.holder_count < settings.MIN_HOLDER_COUNT:
            logger.debug(
                "Holder count too low",
                token=signal.token_name,
                holder_count=signal.holder_count,
                min_required=settings.MIN_HOLDER_COUNT,
            )
            continue
        if signal.liquidity_usd < settings.MIN_LIQUIDITY_USD:
            continue
        if signal.token_age_days > settings.MAX_TOKEN_AGE_DAYS:
            logger.debug("Token too old", token=signal.token_name, age_days=signal.token_age_days)
            continue
        if await db.is_on_cooldown(signal.contract_address):
            logger.debug("Token on cooldown", token=signal.token_name)
            continue
        existing = await db.get_open_position_by_address(signal.contract_address)
        if existing is not None:
            continue
        actionable.append(signal)
    return actionable

"""Kelly Criterion position sizing — dynamically size bets based on edge."""

import structlog

from sniper.config import Settings
from sniper.db import Database

logger = structlog.get_logger()


async def calculate_kelly_bet(
    db: Database,
    sol_balance: float,
    settings: Settings,
) -> float:
    """Calculate position size using Half Kelly criterion.

    Uses recent trade history to determine win rate and payoff ratio,
    then applies Half Kelly with min/max bounds.

    Returns the SOL amount to bet.
    """
    if not settings.KELLY_ENABLED:
        return settings.MAX_BUY_SOL

    # Get recent closed trades (last N trades)
    positions = await db.get_recent_closed(settings.KELLY_LOOKBACK)

    if len(positions) < settings.KELLY_MIN_TRADES:
        # Not enough data — use default bet
        logger.debug(
            "Kelly: insufficient trades, using default",
            trades=len(positions),
            min_required=settings.KELLY_MIN_TRADES,
            default=settings.MAX_BUY_SOL,
        )
        return settings.MAX_BUY_SOL

    # Calculate win rate and payoff ratio
    wins = [p for p in positions if (p.get("pnl_sol") or 0) > 0]
    losses = [p for p in positions if (p.get("pnl_sol") or 0) < 0]

    win_count = len(wins)
    loss_count = len(losses)
    total = win_count + loss_count

    if total == 0:
        return settings.MAX_BUY_SOL

    win_rate = win_count / total

    avg_win = sum(p["pnl_sol"] for p in wins) / win_count if win_count > 0 else 0
    avg_loss = abs(sum(p["pnl_sol"] for p in losses) / loss_count) if loss_count > 0 else 0.01

    payoff_ratio = avg_win / avg_loss if avg_loss > 0 else 1.0

    # Kelly formula: K = W - (1-W)/R
    kelly = win_rate - ((1 - win_rate) / payoff_ratio)

    # Half Kelly for safety
    half_kelly = kelly / 2

    # Calculate bet as percentage of bankroll
    if half_kelly <= 0:
        # Negative Kelly — minimum bet only
        bet = settings.KELLY_MIN_BET
        logger.info(
            "Kelly: negative edge, using minimum bet",
            win_rate=f"{win_rate:.1%}",
            payoff_ratio=f"{payoff_ratio:.2f}",
            kelly=f"{kelly:.1%}",
            bet=bet,
        )
    else:
        bet = sol_balance * half_kelly
        logger.info(
            "Kelly: positive edge",
            win_rate=f"{win_rate:.1%}",
            payoff_ratio=f"{payoff_ratio:.2f}",
            kelly=f"{kelly:.1%}",
            half_kelly=f"{half_kelly:.1%}",
            bankroll=f"{sol_balance:.4f}",
            raw_bet=f"{bet:.4f}",
        )

    # Apply bounds
    bet = max(settings.KELLY_MIN_BET, min(settings.KELLY_MAX_BET, bet))

    logger.info("Kelly bet size", bet=f"{bet:.4f} SOL")
    return round(bet, 4)

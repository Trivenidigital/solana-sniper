"""Position monitoring — stop-loss and take-profit checks."""

import aiohttp
import structlog
from solana.rpc.async_api import AsyncClient
from solders.keypair import Keypair

from sniper.config import Settings
from sniper.db import Database
from sniper.executor import execute_sell, get_current_value_sol

logger = structlog.get_logger()


async def check_positions(
    db: Database,
    client: AsyncClient,
    keypair: Keypair,
    session: aiohttp.ClientSession,
    settings: Settings,
) -> list[str]:
    """Check all open positions for stop-loss and take-profit triggers.

    Returns list of action descriptions for logging.
    """
    positions = await db.get_open_positions()
    actions: list[str] = []

    for pos in positions:
        current_value = await get_current_value_sol(
            session, pos.contract_address, int(pos.entry_token_amount), settings,
        )
        if current_value is None:
            continue

        pnl_pct = ((current_value - pos.entry_sol) / pos.entry_sol) * 100

        # Stop-loss check
        if pnl_pct <= -settings.STOP_LOSS_PCT:
            action = await _close_position(
                db, client, keypair, session, settings,
                pos.id, pos.contract_address, pos.token_name,
                int(pos.entry_token_amount), pos.entry_sol,
                current_value, pnl_pct, "stop_loss",
            )
            actions.append(action)
            continue

        # Take-profit check
        if pnl_pct >= settings.TAKE_PROFIT_PCT:
            action = await _close_position(
                db, client, keypair, session, settings,
                pos.id, pos.contract_address, pos.token_name,
                int(pos.entry_token_amount), pos.entry_sol,
                current_value, pnl_pct, "take_profit",
            )
            actions.append(action)
            continue

        logger.debug(
            "Position check",
            token=pos.token_name,
            pnl_pct=f"{pnl_pct:.1f}%",
            current_value_sol=f"{current_value:.4f}",
        )

    return actions


async def _close_position(
    db: Database,
    client: AsyncClient,
    keypair: Keypair,
    session: aiohttp.ClientSession,
    settings: Settings,
    position_id: int | None,
    contract_address: str,
    token_name: str,
    token_amount: int,
    entry_sol: float,
    current_value: float,
    pnl_pct: float,
    reason: str,
) -> str:
    """Execute sell and close position in DB."""
    try:
        tx_sig, sol_received = await execute_sell(
            client, keypair, session, contract_address, token_amount, settings,
        )
        pnl_sol = sol_received - entry_sol
        await db.close_position(
            position_id=position_id,  # type: ignore[arg-type]
            exit_sol=sol_received,
            exit_price_usd=0,
            exit_tx=tx_sig,
            exit_reason=reason,
            pnl_sol=pnl_sol,
            pnl_pct=pnl_pct,
        )
        await db.log_trade(
            position_id=position_id,  # type: ignore[arg-type]
            side="sell",
            sol_amount=sol_received,
            token_amount=float(token_amount),
            tx_signature=tx_sig,
            price_usd=None,
        )
        action = f"{reason.upper()}: {token_name} PnL={pnl_pct:.1f}% ({pnl_sol:+.4f} SOL)"
        logger.info(action, tx=tx_sig)
        return action
    except Exception as e:
        logger.error(f"Failed to close position ({reason})", token=token_name, error=str(e))
        return f"FAILED {reason}: {token_name} — {e}"


async def portfolio_summary(db: Database) -> dict:
    """Compute and log portfolio summary."""
    open_positions = await db.get_open_positions()
    exposure = await db.get_total_exposure_sol()
    realized_pnl = await db.get_realized_pnl()

    summary = {
        "open_positions": len(open_positions),
        "exposure_sol": round(exposure, 4),
        "realized_pnl_sol": round(realized_pnl, 4),
    }
    logger.info("Portfolio summary", **summary)
    return summary

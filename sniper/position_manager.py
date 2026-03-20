"""Position monitoring — stop-loss and take-profit checks."""

import asyncio

import aiohttp
import structlog
from solana.rpc.async_api import AsyncClient
from solders.keypair import Keypair

from sniper.config import Settings
from sniper.db import Database
from sniper.executor import execute_sell, get_current_value_sol
from sniper.telegram_notify import send_telegram

logger = structlog.get_logger()


async def check_positions(
    db: Database,
    client: AsyncClient,
    keypair: Keypair,
    session: aiohttp.ClientSession,
    settings: Settings,
) -> list[str]:
    """Check all open positions for stop-loss and take-profit triggers.

    Fetches all position prices in parallel using asyncio.gather for speed.

    Returns list of action descriptions for logging.
    """
    positions = await db.get_open_positions()
    if not positions:
        return []

    actions: list[str] = []

    # Batch price check: fetch all position values in parallel
    value_tasks = [
        get_current_value_sol(
            session, pos.contract_address, int(pos.entry_token_amount), settings,
        )
        for pos in positions
    ]
    current_values = await asyncio.gather(*value_tasks, return_exceptions=True)

    for pos, current_value in zip(positions, current_values):
        # Handle exceptions from gather
        if isinstance(current_value, Exception):
            logger.warning("Price check raised exception", token=pos.token_name, error=str(current_value))
            continue
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
            # Set cooldown after stop-loss
            await db.set_cooldown(pos.contract_address, settings.COOLDOWN_HOURS)
            actions.append(action)
            continue

        # --- Partial take-profit ---
        partial_tp_pct = settings.TAKE_PROFIT_PCT / 2
        if (
            pnl_pct >= partial_tp_pct
            and not pos.partial_exit_done
            and pos.id is not None
        ):
            try:
                sell_tokens = int(pos.entry_token_amount * settings.PARTIAL_SELL_FRACTION)
                remaining_tokens = pos.entry_token_amount - sell_tokens
                tx_sig, sol_received = await execute_sell(
                    client, keypair, session,
                    pos.contract_address, sell_tokens, settings,
                )
                await db.log_trade(
                    position_id=pos.id,
                    side="sell",
                    sol_amount=sol_received,
                    token_amount=float(sell_tokens),
                    tx_signature=tx_sig,
                    price_usd=None,
                )
                await db.mark_partial_exit(pos.id, remaining_tokens)
                # Update local state for subsequent checks in this iteration
                pos.entry_token_amount = remaining_tokens
                pos.partial_exit_done = True
                action = (
                    f"PARTIAL_TP: {pos.token_name} sold {settings.PARTIAL_SELL_FRACTION*100:.0f}% "
                    f"at {pnl_pct:.1f}% gain ({sol_received:.4f} SOL)"
                )
                logger.info(action, tx=tx_sig)
                actions.append(action)
            except Exception as e:
                logger.error("Partial exit failed", token=pos.token_name, error=str(e))

        # --- Trailing take-profit tracking ---
        # Update peak value
        if pos.id is not None:
            if pos.peak_value_sol is None or current_value > pos.peak_value_sol:
                pos.peak_value_sol = current_value
                await db.update_peak_value(pos.id, current_value)

            # Activate trailing if gain exceeds trigger
            if pnl_pct >= settings.TRAILING_TRIGGER_PCT and not pos.trailing_active:
                pos.trailing_active = True
                await db.set_trailing_active(pos.id)
                logger.info(
                    "Trailing stop activated",
                    token=pos.token_name,
                    pnl_pct=f"{pnl_pct:.1f}%",
                )

            # Check trailing stop
            if pos.trailing_active and pos.peak_value_sol is not None and pos.peak_value_sol > 0:
                drop_from_peak = ((pos.peak_value_sol - current_value) / pos.peak_value_sol) * 100
                if drop_from_peak >= settings.TRAILING_STOP_PCT:
                    action = await _close_position(
                        db, client, keypair, session, settings,
                        pos.id, pos.contract_address, pos.token_name,
                        int(pos.entry_token_amount), pos.entry_sol,
                        current_value, pnl_pct, "trailing_stop",
                    )
                    actions.append(action)
                    continue

        # Take-profit check (full exit)
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
            trailing_active=pos.trailing_active,
            peak_value_sol=pos.peak_value_sol,
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

        # Telegram notification for position close
        await send_telegram(
            f"Position Closed ({reason.replace('_', ' ').title()})\n"
            f"Token: {token_name}\n"
            f"PnL: {pnl_pct:.1f}% ({pnl_sol:+.4f} SOL)\n"
            f"TX: {tx_sig}",
            settings,
        )

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

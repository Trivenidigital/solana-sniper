"""Position monitoring — time-based phase exits with trailing tiers."""

import asyncio
from datetime import datetime, timezone

import aiohttp
import structlog
from solana.rpc.async_api import AsyncClient
from solders.keypair import Keypair

from sniper.config import Settings
from sniper.db import Database
from sniper.executor import execute_sell, get_current_value_sol
from sniper.telegram_notify import send_telegram

logger = structlog.get_logger()


async def _get_sell_pressure(session: aiohttp.ClientSession, contract_address: str) -> float | None:
    """Fetch the 5-minute sell ratio from DexScreener.

    Returns sell_ratio = sells / (buys + sells), or None on failure.
    """
    try:
        url = f"https://api.dexscreener.com/tokens/v1/solana/{contract_address}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            pairs = await resp.json()
            if not pairs or not isinstance(pairs, list) or len(pairs) == 0:
                return None
            pair = pairs[0]
            txns = pair.get("txns", {}).get("m5", {})
            buys = txns.get("buys", 0)
            sells = txns.get("sells", 0)
            total = buys + sells
            if total == 0:
                return None
            return sells / total
    except Exception:
        return None


async def check_positions(
    db: Database,
    client: AsyncClient,
    keypair: Keypair,
    session: aiohttp.ClientSession,
    settings: Settings,
) -> list[str]:
    """Check all open positions using time-based phase exit strategy.

    Phases:
      1. Protection (0-10 min): only exit on rug detection
      2. Momentum (10-30 min): exit on momentum loss, activate trailing
      3. Pump window (30-60 min): must show minimum gain or exit
      4. Cleanup (60+ min): force exit unless trailing with large gain

    Trailing tiers are checked BEFORE phases so active trails are managed
    regardless of which phase the position is in.

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
        age_minutes = (datetime.now(timezone.utc) - pos.opened_at).total_seconds() / 60

        # --- Trailing stop management (runs BEFORE phase checks) ---
        if pos.trailing_active and pos.id is not None:
            # Update peak value
            if current_value > (pos.peak_value_sol or 0):
                pos.peak_value_sol = current_value
                await db.update_peak_value(pos.id, current_value)

            if pos.peak_value_sol is not None and pos.peak_value_sol > 0:
                peak_pnl = ((pos.peak_value_sol - pos.entry_sol) / pos.entry_sol) * 100
                drop_from_peak = ((pos.peak_value_sol - current_value) / pos.peak_value_sol) * 100

                # Determine trail percentage based on peak gain tier
                if peak_pnl > 200:
                    trail_pct = settings.TRAILING_TIER3_PCT  # 10%
                    # Tier 2 partial sell: 50% of remaining (25% of original) if tier < 2
                    if pos.partial_exit_tier < 2:
                        await _partial_sell(
                            db, client, keypair, session, settings,
                            pos, 0.50, pnl_pct, actions, tier=2,
                        )
                elif peak_pnl > 100:
                    trail_pct = settings.TRAILING_TIER2_PCT  # 15%
                    # Tier 1 partial sell: 50% if tier < 1
                    if pos.partial_exit_tier < 1:
                        await _partial_sell(
                            db, client, keypair, session, settings,
                            pos, 0.50, pnl_pct, actions, tier=1,
                        )
                else:
                    trail_pct = settings.TRAILING_TIER1_PCT  # 20%

                logger.debug(
                    "Trailing check",
                    token=pos.token_name,
                    peak_pnl=f"{peak_pnl:.1f}%",
                    drop_from_peak=f"{drop_from_peak:.1f}%",
                    trail_pct=f"{trail_pct}%",
                )

                if drop_from_peak >= trail_pct:
                    action = await _close_position(
                        db, client, keypair, session, settings,
                        pos.id, pos.contract_address, pos.token_name,
                        int(pos.entry_token_amount), pos.entry_sol,
                        current_value, pnl_pct, "trailing_stop",
                    )
                    actions.append(action)
                    continue

        # --- Phase-based exit logic ---

        # Phase 1: Protection (0-10 min)
        if age_minutes <= settings.PROTECTION_WINDOW_MIN:
            if pnl_pct <= -settings.RUG_DETECT_PCT:
                logger.warning(
                    "Rug detected in protection phase",
                    token=pos.token_name,
                    pnl_pct=f"{pnl_pct:.1f}%",
                    age_minutes=f"{age_minutes:.1f}",
                )
                action = await _close_position(
                    db, client, keypair, session, settings,
                    pos.id, pos.contract_address, pos.token_name,
                    int(pos.entry_token_amount), pos.entry_sol,
                    current_value, pnl_pct, "rug_detected",
                )
                await db.set_cooldown(pos.contract_address, settings.COOLDOWN_HOURS)
                actions.append(action)
                continue
            else:
                logger.debug(
                    "Phase 1 (protection)",
                    token=pos.token_name,
                    pnl_pct=f"{pnl_pct:.1f}%",
                    age_minutes=f"{age_minutes:.1f}",
                )

        # Phase 2: Momentum (10-30 min)
        elif age_minutes <= settings.MOMENTUM_CHECK_MIN:
            if pnl_pct <= -settings.MOMENTUM_LOSS_PCT:
                logger.info(
                    "Momentum lost in phase 2",
                    token=pos.token_name,
                    pnl_pct=f"{pnl_pct:.1f}%",
                    age_minutes=f"{age_minutes:.1f}",
                )
                action = await _close_position(
                    db, client, keypair, session, settings,
                    pos.id, pos.contract_address, pos.token_name,
                    int(pos.entry_token_amount), pos.entry_sol,
                    current_value, pnl_pct, "momentum_lost",
                )
                await db.set_cooldown(pos.contract_address, settings.COOLDOWN_HOURS)
                actions.append(action)
                continue
            elif pnl_pct >= settings.TRAILING_ACTIVATE_PCT and not pos.trailing_active and pos.id is not None:
                pos.trailing_active = True
                await db.set_trailing_active(pos.id)
                # Initialize peak value
                if pos.peak_value_sol is None or current_value > pos.peak_value_sol:
                    pos.peak_value_sol = current_value
                    await db.update_peak_value(pos.id, current_value)
                logger.info(
                    "Trailing stop activated in phase 2",
                    token=pos.token_name,
                    pnl_pct=f"{pnl_pct:.1f}%",
                    age_minutes=f"{age_minutes:.1f}",
                )

        # Phase 3: Pump window (30-60 min)
        elif age_minutes <= settings.MAX_HOLD_MIN:
            if not pos.trailing_active and pnl_pct < settings.PUMP_WINDOW_MIN_GAIN_PCT:
                logger.info(
                    "Pump window expired in phase 3",
                    token=pos.token_name,
                    pnl_pct=f"{pnl_pct:.1f}%",
                    age_minutes=f"{age_minutes:.1f}",
                    required_gain=f"{settings.PUMP_WINDOW_MIN_GAIN_PCT}%",
                )
                action = await _close_position(
                    db, client, keypair, session, settings,
                    pos.id, pos.contract_address, pos.token_name,
                    int(pos.entry_token_amount), pos.entry_sol,
                    current_value, pnl_pct, "pump_window_expired",
                )
                await db.set_cooldown(pos.contract_address, settings.COOLDOWN_HOURS)
                actions.append(action)
                continue
            # Activate trailing if gain is sufficient and not already active
            elif pnl_pct >= settings.TRAILING_ACTIVATE_PCT and not pos.trailing_active and pos.id is not None:
                pos.trailing_active = True
                await db.set_trailing_active(pos.id)
                if pos.peak_value_sol is None or current_value > pos.peak_value_sol:
                    pos.peak_value_sol = current_value
                    await db.update_peak_value(pos.id, current_value)
                logger.info(
                    "Trailing stop activated in phase 3",
                    token=pos.token_name,
                    pnl_pct=f"{pnl_pct:.1f}%",
                    age_minutes=f"{age_minutes:.1f}",
                )

        # Phase 4: Cleanup (60+ min)
        else:
            if not (pos.trailing_active and pnl_pct > settings.PHASE4_TRAILING_MIN_PNL):
                logger.info(
                    "Max hold exceeded in phase 4",
                    token=pos.token_name,
                    pnl_pct=f"{pnl_pct:.1f}%",
                    age_minutes=f"{age_minutes:.1f}",
                    trailing_active=pos.trailing_active,
                )
                action = await _close_position(
                    db, client, keypair, session, settings,
                    pos.id, pos.contract_address, pos.token_name,
                    int(pos.entry_token_amount), pos.entry_sol,
                    current_value, pnl_pct, "max_hold_exceeded",
                )
                await db.set_cooldown(pos.contract_address, settings.COOLDOWN_HOURS)
                actions.append(action)
                continue

        # --- Sell pressure check (after phase logic) ---
        sell_ratio = await _get_sell_pressure(session, pos.contract_address)
        if sell_ratio is not None:
            logger.debug(
                "Sell pressure",
                token=pos.token_name,
                sell_ratio=f"{sell_ratio:.2f}",
            )
            if sell_ratio > settings.SELL_PRESSURE_THRESHOLD:
                logger.warning(
                    "High sell pressure detected — force closing",
                    token=pos.token_name,
                    sell_ratio=f"{sell_ratio:.2f}",
                    threshold=settings.SELL_PRESSURE_THRESHOLD,
                )
                action = await _close_position(
                    db, client, keypair, session, settings,
                    pos.id, pos.contract_address, pos.token_name,
                    int(pos.entry_token_amount), pos.entry_sol,
                    current_value, pnl_pct, "sell_pressure",
                )
                actions.append(action)
                continue

        logger.debug(
            "Position check",
            token=pos.token_name,
            pnl_pct=f"{pnl_pct:.1f}%",
            current_value_sol=f"{current_value:.4f}",
            age_minutes=f"{age_minutes:.1f}",
            trailing_active=pos.trailing_active,
            peak_value_sol=pos.peak_value_sol,
        )

    return actions


async def _partial_sell(
    db: Database,
    client: AsyncClient,
    keypair: Keypair,
    session: aiohttp.ClientSession,
    settings: Settings,
    pos,
    fraction: float,
    pnl_pct: float,
    actions: list[str],
    tier: int = 1,
) -> None:
    """Execute a partial sell of a position and mark it in the DB."""
    if pos.id is None:
        return
    try:
        sell_tokens = int(pos.entry_token_amount * fraction)
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
        # Adjust entry_sol proportionally to reflect the fraction sold
        fraction_sold = sell_tokens / pos.entry_token_amount if pos.entry_token_amount > 0 else fraction
        new_entry_sol = pos.entry_sol * (1 - fraction_sold)
        await db.update_partial_exit(pos.id, new_entry_sol, remaining_tokens, tier)
        pos.entry_sol = new_entry_sol
        pos.entry_token_amount = remaining_tokens
        pos.partial_exit_done = True
        pos.partial_exit_tier = tier
        action = (
            f"PARTIAL_SELL(T{tier}): {pos.token_name} sold {fraction*100:.0f}% "
            f"at {pnl_pct:.1f}% gain ({sol_received:.4f} SOL)"
        )
        logger.info(action, tx=tx_sig)
        actions.append(action)
    except Exception as e:
        logger.error("Partial sell failed", token=pos.token_name, fraction=fraction, error=str(e))


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
        pnl_pct = (pnl_sol / entry_sol * 100) if entry_sol > 0 else 0
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

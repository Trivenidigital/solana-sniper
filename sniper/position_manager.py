"""Position monitoring — time-based phase exits with trailing tiers."""

import asyncio
import time
from datetime import datetime, timezone

import aiohttp
import structlog
from solana.rpc.async_api import AsyncClient
from solders.keypair import Keypair

from sniper.config import Settings
from sniper.db import Database
from sniper.executor import execute_buy, execute_sell
from sniper.jupiter import SOL_MINT, LAMPORTS_PER_SOL, get_quote
from sniper.telegram_notify import send_telegram

# B2 fix: cache DexScreener liquidity per token to avoid hammering API
_liq_cache: dict[str, tuple[float, float]] = {}  # {contract: (liq_usd, timestamp)}
_LIQ_CACHE_TTL = 60  # seconds

logger = structlog.get_logger()


async def _jupiter_value_sol(
    session: aiohttp.ClientSession,
    contract_address: str,
    token_amount: int,
    settings: Settings,
) -> float | None:
    """Get SOL value of a token position using Jupiter ONLY (no DexScreener).

    Used specifically for rug-detection verification so we get an independent
    price source that doesn't share DexScreener's stale data.
    """
    if token_amount <= 0:
        return 0.0
    try:
        quote = await get_quote(
            session, contract_address, SOL_MINT, token_amount, settings,
        )
        return float(quote.out_amount) / LAMPORTS_PER_SOL
    except Exception:
        logger.warning("Jupiter-only price check failed", token=contract_address)
        return None


async def _fetch_position_data(
    session: aiohttp.ClientSession, contract_address: str, token_amount: int,
    decimals: int = 9,
) -> tuple[float | None, float | None, float]:
    """Fetch price, mcap, and sell ratio from DexScreener in one call.

    Returns (value_sol, sell_ratio, market_cap).
    """
    try:
        url = f"https://api.dexscreener.com/tokens/v1/solana/{contract_address}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return None, None, 0
            pairs = await resp.json()
            if not pairs or not isinstance(pairs, list):
                return None, None, 0
            pair = pairs[0]
            # Price
            price_native = pair.get("priceNative")
            value_sol = None
            if price_native:
                human_tokens = token_amount / (10 ** decimals)
                value_sol = human_tokens * float(price_native)
            # Sell ratio
            txns = pair.get("txns", {}).get("m5", {})
            buys = txns.get("buys", 0)
            sells = txns.get("sells", 0)
            total = buys + sells
            sell_ratio = sells / total if total > 0 else None
            return value_sol, sell_ratio, float(pair.get("marketCap") or 0)
    except Exception:
        return None, None, 0


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

    # Filter out paper and manual positions — user manages these via dashboard
    active_positions = [pos for pos in positions if not pos.paper and not pos.manual]
    skipped = len(positions) - len(active_positions)
    if skipped:
        logger.debug("Skipping paper/manual positions", count=skipped)

    # Batch price check: single DexScreener call per active position
    data_tasks = [
        _fetch_position_data(session, pos.contract_address, int(pos.entry_token_amount),
                             decimals=pos.decimals if pos.decimals is not None else 9)
        for pos in active_positions
    ]
    position_data = await asyncio.gather(*data_tasks, return_exceptions=True)

    for pos, pos_data in zip(active_positions, position_data):
        # Handle exceptions from gather
        if isinstance(pos_data, Exception):
            logger.warning("Price check raised exception", token=pos.token_name, error=str(pos_data))
            continue
        current_value, sell_ratio, _mcap = pos_data
        if current_value is None:
            continue

        pnl_pct = ((current_value - pos.entry_sol) / pos.entry_sol) * 100
        age_minutes = (datetime.now(timezone.utc) - pos.opened_at).total_seconds() / 60

        # --- Conviction hold: patient exit logic for high-score tokens ---
        conviction_score = pos.conviction_score or 0
        is_conviction_hold = (
            settings.CONVICTION_HOLD_ENABLED
            and conviction_score >= settings.CONVICTION_HOLD_MIN_SCORE
        )

        if is_conviction_hold:
            # Update peak value
            if current_value > (pos.peak_value_sol or 0) and pos.id:
                pos.peak_value_sol = current_value
                await db.update_peak_value(pos.id, current_value)

            # Check 1: Liquidity rug — fetch live from DexScreener (cached 60s)
            liq_usd = 0.0
            cached = _liq_cache.get(pos.contract_address)
            if cached and (time.monotonic() - cached[1]) < _LIQ_CACHE_TTL:
                liq_usd = cached[0]
            else:
                try:
                    async with session.get(
                        f"https://api.dexscreener.com/tokens/v1/solana/{pos.contract_address}",
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as resp:
                        if resp.status == 200:
                            pairs = await resp.json()
                            if pairs and isinstance(pairs, list):
                                liq_obj = pairs[0].get("liquidity")
                                if liq_obj and isinstance(liq_obj, dict):
                                    liq_usd = float(liq_obj.get("usd", 0) or 0)
                    _liq_cache[pos.contract_address] = (liq_usd, time.monotonic())
                except Exception as e:
                    logger.warning(
                        "Conviction hold: DexScreener liquidity fetch failed — rug check skipped",
                        token=pos.token_name,
                        error=str(e),
                    )

            # If liquidity dropped 70%+ from entry → real rug
            entry_liq = pos.entry_liquidity_usd or 0
            if liq_usd > 0 and entry_liq > 0:
                liq_drop_pct = ((entry_liq - liq_usd) / entry_liq) * 100
                if liq_drop_pct >= settings.CONVICTION_HOLD_RUG_LIQUIDITY_DROP_PCT:
                    logger.warning(
                        "Conviction hold: liquidity rug detected",
                        token=pos.token_name,
                        conviction=conviction_score,
                        liq_usd=f"${liq_usd:,.0f}",
                        entry_liq=f"${entry_liq:,.0f}",
                        liq_drop_pct=f"{liq_drop_pct:.1f}%",
                    )
                    action = await _close_position(
                        db, client, keypair, session, settings,
                        pos.id, pos.contract_address, pos.token_name,
                        int(pos.entry_token_amount), pos.entry_sol,
                        current_value, pnl_pct, "conviction_rug_detected",
                    )
                    actions.append(action)
                    continue

            # Check 2: Scaled hard stop — higher conviction = wider stop
            # 45 conviction → -35% (same as universal), 80+ → -70% (full diamond hands)
            scaled_stop = settings.STOP_LOSS_PCT + (
                (settings.CONVICTION_HOLD_HARD_STOP_PCT - settings.STOP_LOSS_PCT)
                * (conviction_score - settings.CONVICTION_HOLD_MIN_SCORE)
                / (80 - settings.CONVICTION_HOLD_MIN_SCORE)
            )
            scaled_stop = min(scaled_stop, settings.CONVICTION_HOLD_HARD_STOP_PCT)
            if pnl_pct <= -scaled_stop:
                logger.warning(
                    "Conviction hold: scaled hard stop triggered",
                    token=pos.token_name,
                    conviction=conviction_score,
                    pnl_pct=f"{pnl_pct:.1f}%",
                    scaled_stop=f"-{scaled_stop:.1f}%",
                )
                action = await _close_position(
                    db, client, keypair, session, settings,
                    pos.id, pos.contract_address, pos.token_name,
                    int(pos.entry_token_amount), pos.entry_sol,
                    current_value, pnl_pct, "conviction_hard_stop",
                )
                actions.append(action)
                continue

            # Check 3: Max hold 4 hours (if not trailing)
            if age_minutes > settings.CONVICTION_HOLD_MAX_HOLD_MIN and not pos.trailing_active:
                logger.info(
                    "Conviction hold: max hold exceeded",
                    token=pos.token_name,
                    conviction=conviction_score,
                    age_minutes=f"{age_minutes:.1f}",
                    max_hold_min=settings.CONVICTION_HOLD_MAX_HOLD_MIN,
                )
                action = await _close_position(
                    db, client, keypair, session, settings,
                    pos.id, pos.contract_address, pos.token_name,
                    int(pos.entry_token_amount), pos.entry_sol,
                    current_value, pnl_pct, "conviction_max_hold",
                )
                actions.append(action)
                continue

            # Conviction hold trailing REMOVED — profit ladder (PR #30) handles
            # trailing via step 2 (+50% → trail 25%) and step 3 (+100% → trail 15%).
            # The old 20% trail was too tight for meme coins (same issue PR #30 fixed).

            # Still holding — fall through to profit ladder but skip phase logic
            logger.info(
                "Conviction hold: holding",
                token=pos.token_name,
                conviction=conviction_score,
                pnl_pct=f"{pnl_pct:.1f}%",
                age_minutes=f"{age_minutes:.1f}",
            )
            # DON'T continue — let conviction holds use the profit ladder below

        # Use fixed phase timings from settings directly
        protection_end = settings.PROTECTION_WINDOW_MIN
        momentum_end = settings.MOMENTUM_CHECK_MIN
        max_hold = settings.MAX_HOLD_MIN

        # --- Track peak value (always, for all phases) ---
        if pos.id is not None and current_value > (pos.peak_value_sol or 0):
            pos.peak_value_sol = current_value
            await db.update_peak_value(pos.id, current_value)

        # --- Profit ladder + breakeven stop ---
        # High conviction (70+) gets wider ladder — let winners run longer
        high_conv = (pos.conviction_score or 0) >= 70
        step1_trigger = 50 if high_conv else 25
        step2_trigger = 100 if high_conv else 50
        step3_trigger = 200 if high_conv else 100

        # Step 1: Sell 25%, move stop to breakeven
        if (pnl_pct >= step1_trigger
                and pos.partial_exit_tier < 1
                and pos.id is not None):
            await _partial_sell(
                db, client, keypair, session, settings,
                pos, 0.25, pnl_pct, actions, tier=1,
            )
            logger.info(
                "Ladder step 1: sold 25%, stop moved to breakeven",
                token=pos.token_name,
                pnl_pct=f"{pnl_pct:.1f}%",
                trigger=f"+{step1_trigger}%",
                high_conviction=high_conv,
            )
            continue

        # Step 2: Sell 25% more, activate trailing at 25% from peak
        if (pnl_pct >= step2_trigger
                and pos.partial_exit_tier < 2
                and pos.id is not None):
            await _partial_sell(
                db, client, keypair, session, settings,
                pos, 0.33, pnl_pct, actions, tier=2,
            )
            if not pos.trailing_active:
                pos.trailing_active = True
                await db.set_trailing_active(pos.id)
            logger.info(
                "Ladder step 2: sold 25% more, trailing active at 25%",
                token=pos.token_name,
                pnl_pct=f"{pnl_pct:.1f}%",
                trigger=f"+{step2_trigger}%",
                high_conviction=high_conv,
            )
            continue

        # Step 3: Sell 25% more, tighten trail to 15%
        if (pnl_pct >= step3_trigger
                and pos.partial_exit_tier < 3
                and pos.id is not None):
            await _partial_sell(
                db, client, keypair, session, settings,
                pos, 0.50, pnl_pct, actions, tier=3,
            )
            logger.info(
                "Ladder step 3: sold 25% more, trail tightened to 15%",
                token=pos.token_name,
                pnl_pct=f"{pnl_pct:.1f}%",
                trigger=f"+{step3_trigger}%",
                high_conviction=high_conv,
            )
            continue

        # --- Breakeven stop: after first partial sell, never lose money ---
        # Use 1% instead of 0% to cover gas/slippage fees
        if pos.partial_exit_tier >= 1 and pnl_pct <= 1 and pos.id is not None:
            logger.info(
                "Breakeven stop — locked profit from ladder step 1",
                token=pos.token_name,
                pnl_pct=f"{pnl_pct:.1f}%",
                tier=pos.partial_exit_tier,
            )
            action = await _close_position(
                db, client, keypair, session, settings,
                pos.id, pos.contract_address, pos.token_name,
                int(pos.entry_token_amount), pos.entry_sol,
                current_value, pnl_pct, "breakeven_stop",
            )
            actions.append(action)
            continue

        # --- Trailing stop (active after ladder step 2+) ---
        if pos.trailing_active and pos.peak_value_sol and pos.peak_value_sol > 0 and pos.id is not None:
            drop_from_peak = ((pos.peak_value_sol - current_value) / pos.peak_value_sol) * 100
            # Tighter trail after step 3 (moon bag)
            trail_pct = 15.0 if pos.partial_exit_tier >= 3 else 25.0

            logger.debug(
                "Trailing check",
                token=pos.token_name,
                pnl_pct=f"{pnl_pct:.1f}%",
                drop_from_peak=f"{drop_from_peak:.1f}%",
                trail_pct=f"{trail_pct}%",
                tier=pos.partial_exit_tier,
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

        # --- Universal stop loss (runs every cycle, all phases) ---
        if pnl_pct <= -settings.STOP_LOSS_PCT:
            logger.warning(
                "Stop loss triggered",
                token=pos.token_name,
                pnl_pct=f"{pnl_pct:.1f}%",
                stop_loss=f"-{settings.STOP_LOSS_PCT}%",
                age_minutes=f"{age_minutes:.1f}",
            )
            action = await _close_position(
                db, client, keypair, session, settings,
                pos.id, pos.contract_address, pos.token_name,
                int(pos.entry_token_amount), pos.entry_sol,
                current_value, pnl_pct, "stop_loss",
            )
            actions.append(action)
            continue

        # --- Phase-based exit logic (skip for conviction holds) ---
        if is_conviction_hold:
            continue

        # Phase 1: Protection (0-{protection_end} min)
        if age_minutes <= protection_end:
            # Rug detection: use Rugcheck API + Jupiter price verification
            # NOT DexScreener price — it gives false -99% on some tokens
            is_rug = False
            rug_reason = ""

            # Check 1: Rugcheck risk score (creator history, LP issues)
            try:
                async with session.get(
                    f"https://api.rugcheck.xyz/v1/tokens/{pos.contract_address}/report/summary",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        rc_data = await resp.json()
                        risk_score = rc_data.get("score", 0)
                        risks = rc_data.get("risks", [])
                        risk_names = [r.get("name", "") if isinstance(r, dict) else str(r) for r in risks]

                        # High risk: only trust Rugcheck rug signal if Jupiter
                        # also confirms a significant price drop. Rugcheck returns
                        # false "LP Unlocked" for pumpswap tokens.
                        if risk_score >= 10000 or any("rug" in r.lower() for r in risk_names):
                            # Verify with Jupiter directly — NOT get_current_value_sol
                            # which tries DexScreener first (same stale data as rug trigger)
                            jupiter_value = await _jupiter_value_sol(
                                session, pos.contract_address,
                                int(pos.entry_token_amount), settings,
                            )
                            if jupiter_value is not None:
                                real_pnl = ((jupiter_value - pos.entry_sol) / pos.entry_sol) * 100
                                if real_pnl <= -30:
                                    # Jupiter confirms big drop + Rugcheck flags danger
                                    is_rug = True
                                    rug_reason = (f"Rugcheck risk={risk_score}: "
                                                  f"{', '.join(risk_names[:3])} "
                                                  f"(Jupiter confirms {real_pnl:.0f}%)")
                                else:
                                    logger.info(
                                        "Rugcheck flagged but Jupiter shows token healthy",
                                        token=pos.token_name,
                                        rugcheck_score=risk_score,
                                        jupiter_pnl=f"{real_pnl:.1f}%",
                                        risks=risk_names[:3],
                                    )
                            else:
                                # Can't verify with Jupiter — trust Rugcheck
                                is_rug = True
                                rug_reason = f"Rugcheck risk={risk_score}: {', '.join(risk_names[:3])}"
            except Exception:
                pass  # Rugcheck unavailable — don't false-positive

            # Check 2: Verify actual price via Jupiter (not DexScreener)
            if not is_rug and pnl_pct <= -settings.RUG_DETECT_PCT:
                # Verify with Jupiter directly — NOT get_current_value_sol
                # which tries DexScreener first (same stale data as rug trigger)
                jupiter_value = await _jupiter_value_sol(
                    session, pos.contract_address, int(pos.entry_token_amount), settings,
                )
                if jupiter_value is not None:
                    real_pnl = ((jupiter_value - pos.entry_sol) / pos.entry_sol) * 100
                    if real_pnl <= -settings.RUG_DETECT_PCT:
                        is_rug = True
                        rug_reason = f"Jupiter confirms -{abs(real_pnl):.0f}% drop"
                        current_value = jupiter_value
                        pnl_pct = real_pnl
                    else:
                        logger.info(
                            "DexScreener false alarm — Jupiter shows token is fine",
                            token=pos.token_name,
                            dexscreener_pnl=f"{pnl_pct:.1f}%",
                            jupiter_pnl=f"{real_pnl:.1f}%",
                        )
                        current_value = jupiter_value
                        pnl_pct = real_pnl
                elif pnl_pct <= -90:
                    # Can't get Jupiter quote at all — probably truly dead
                    is_rug = True
                    rug_reason = "No Jupiter quote available — token likely dead"

            if is_rug:
                logger.warning(
                    "Rug detected in protection phase",
                    token=pos.token_name,
                    reason=rug_reason,
                    pnl_pct=f"{pnl_pct:.1f}%",
                    age_minutes=f"{age_minutes:.1f}",
                )
                action = await _close_position(
                    db, client, keypair, session, settings,
                    pos.id, pos.contract_address, pos.token_name,
                    int(pos.entry_token_amount), pos.entry_sol,
                    current_value, pnl_pct, "rug_detected",
                )
                actions.append(action)
                continue
            elif pnl_pct <= -settings.STOP_LOSS_PCT:
                # Soft stop-loss in protection phase — not a rug but too much loss
                logger.warning(
                    "Soft stop-loss in protection phase",
                    token=pos.token_name,
                    pnl_pct=f"{pnl_pct:.1f}%",
                    age_minutes=f"{age_minutes:.1f}",
                )
                action = await _close_position(
                    db, client, keypair, session, settings,
                    pos.id, pos.contract_address, pos.token_name,
                    int(pos.entry_token_amount), pos.entry_sol,
                    current_value, pnl_pct, "stop_loss",
                )
                actions.append(action)
                continue
            else:
                logger.debug(
                    "Phase 1 (protection)",
                    token=pos.token_name,
                    pnl_pct=f"{pnl_pct:.1f}%",
                    age_minutes=f"{age_minutes:.1f}",
                )

        # Phase 2: Momentum ({protection_end}-{momentum_end} min)
        # No momentum loss exit here — let stop loss handle early dips.
        # Memecoins dip 15-30% regularly before pumping; exiting early
        # kills winners (Bobbie -14% → +290%, Sandwich -4% → +240%).
        elif age_minutes <= momentum_end:
            # Track peak
            if current_value > (pos.peak_value_sol or 0) and pos.id is not None:
                pos.peak_value_sol = current_value
                await db.update_peak_value(pos.id, current_value)

            # Activate trailing if token pumps enough
            if pnl_pct >= settings.TRAILING_ACTIVATE_PCT and not pos.trailing_active and pos.id is not None:
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

            # DCA: if high-conviction token dips 15-25% but sell pressure is normal, buy more
            if settings.DCA_ENABLED and pnl_pct < -5 and pnl_pct > -10:
                sell_ratio_dca = sell_ratio  # reuse sell_ratio from _fetch_position_data
                if sell_ratio_dca is not None and sell_ratio_dca < 0.5:
                    # Only DCA once per position
                    if pos.dca_completed == 0:
                        logger.info("DCA opportunity", token=pos.token_name, pnl=f"{pnl_pct:.1f}%")
                        try:
                            dca_amount = pos.entry_sol * 0.5  # Buy half the original size
                            tx_sig, tokens, _dca_decimals = await execute_buy(
                                client, keypair, session, pos.contract_address, dca_amount, settings,
                            )
                            # Update position with new tokens and adjusted entry
                            new_tokens = pos.entry_token_amount + tokens
                            new_entry = pos.entry_sol + dca_amount
                            await db.update_dca_entry(pos.id, new_entry, new_tokens)
                            pos.entry_sol = new_entry
                            pos.entry_token_amount = new_tokens
                            await db.mark_dca_completed(pos.id)
                            actions.append(f"DCA: {pos.token_name} added {dca_amount} SOL")
                        except Exception as e:
                            logger.warning("DCA buy failed", token=pos.token_name, error=str(e))

        # Phase 3: Hold window ({momentum_end}-{max_hold} min)
        # No pump_window_expired exit — meme coins need time to build momentum.
        # 大胖 (-23% at 60min → +298%), Nintendo (-0.6% → +396%), Hamburger (-1.7% → +185%)
        # all pumped AFTER the old pump window would have exited them.
        # Let stop loss, trailing stop, and max hold handle exits.
        elif age_minutes <= max_hold:
            # Track peak value
            if current_value > (pos.peak_value_sol or 0) and pos.id is not None:
                pos.peak_value_sol = current_value
                await db.update_peak_value(pos.id, current_value)

            # Activate trailing if gain is sufficient and not already active
            if pnl_pct >= settings.TRAILING_ACTIVATE_PCT and not pos.trailing_active and pos.id is not None:
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
                # Cooldown removed — trust conviction score for re-entry
                actions.append(action)
                continue

        # Sell pressure check removed — DexScreener's m5 sell ratio reflects overall
        # market activity, not activity since our entry. This caused false exits
        # (e.g., entering after a 70% correction still showed 70% sell ratio).
        # Price-based exits (stop loss, momentum loss, trailing stop) handle this better.

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
        total_tokens = int(pos.entry_token_amount)
        sell_tokens = round(total_tokens * fraction)
        remaining_tokens = total_tokens - sell_tokens
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
        # Log partial sell PnL for Kelly tracking
        partial_pnl = sol_received - (pos.entry_sol * fraction)
        logger.info("Partial sell PnL", token=pos.token_name, sol_received=sol_received,
                    fraction=fraction, partial_pnl=f"{partial_pnl:+.4f}")
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

        # Track sell failures — force close in DB after 5 failed attempts
        # to prevent dead positions from blocking new trades
        fail_count = await db.increment_sell_fail(position_id) if position_id else 1

        if fail_count >= 5:
            # Only force-close if position is actually worthless (< 5% of entry)
            # Don't kill profitable positions just because Jupiter routing fails
            if current_value is not None and current_value > entry_sol * 0.05:
                logger.warning(
                    "Sell failed 5x but position still has value — keeping open",
                    token=token_name, value_sol=current_value, entry_sol=entry_sol,
                )
                # Reset counter so we retry fresh next cycle
                if position_id:
                    await db.reset_sell_fail(position_id)
                return f"RETRY {reason}: {token_name} (still has value {current_value:.4f} SOL)"

            logger.warning(
                "Force-closing worthless position after 5 failed sell attempts",
                token=token_name, reason=reason,
            )
            try:
                await db.close_position(
                    position_id=position_id,
                    exit_sol=0,
                    exit_price_usd=0,
                    exit_tx=None,
                    exit_reason="unsellable",
                    pnl_sol=-entry_sol,
                    pnl_pct=-100,
                )
            except Exception as db_err:
                logger.error("Failed to force-close in DB", token=token_name, error=str(db_err))
                return f"FORCE_CLOSE_DB_FAILED: {token_name}"
            await send_telegram(
                f"Position Force-Closed (unsellable)\n"
                f"Token: {token_name}\n"
                f"Reason: 5 failed sell attempts, value < 5% of entry\n"
                f"Loss: -{entry_sol:.4f} SOL",
                settings,
            )
            return f"FORCE_CLOSED unsellable: {token_name} (-{entry_sol:.4f} SOL)"

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

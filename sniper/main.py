"""Solana Sniper — main entry point."""

import argparse
import asyncio
import signal
from datetime import datetime, timezone

import aiohttp
import structlog
from solana.rpc.async_api import AsyncClient

from sniper.config import Settings
from sniper.copy_trader import monitor_wallets, smart_money_signals, prune_stale_signals, signals_lock
from sniper.dashboard import print_dashboard
from sniper.db import Database
from sniper.executor import execute_buy, execute_buy_split
from sniper.kelly import calculate_kelly_bet
from sniper.models import Position
from sniper.multi_wallet import copy_buy, load_wallets, get_all_balances
from sniper.position_manager import check_positions, portfolio_summary
from sniper.godmode import check_godmode_bundles
from sniper.safety import check_token_safety  # GoPlus — fallback when Rugcheck is down
from sniper.signal_reader import filter_actionable, read_new_signals
from sniper.telegram_notify import send_telegram
from sniper.wallet import get_sol_balance, load_keypair

logger = structlog.get_logger()


def _conviction_bet_size(conviction: float, settings: Settings) -> float:
    """Return SOL bet size based on conviction score.

    Tiered sizing: higher conviction = larger bet.
    All values are proportional to KELLY_MAX_BET so the ceiling
    can be adjusted in .env without changing the tiers.
    Final result is clamped to MAX_BUY_SOL to prevent oversized bets.
    """
    max_bet = settings.KELLY_MAX_BET
    if conviction >= 80:
        raw = max_bet
    elif conviction >= 75:
        raw = round(max_bet * 0.90, 4)
    elif conviction >= 70:
        raw = round(max_bet * 0.75, 4)
    elif conviction >= 65:
        raw = round(max_bet * 0.60, 4)
    elif conviction >= 60:
        raw = round(max_bet * 0.50, 4)
    elif conviction >= 55:
        raw = 0.25  # Low conviction: fixed small bet, not scaled from max
    elif conviction >= 50:
        raw = 0.20
    elif conviction >= 45:
        raw = 0.15
    else:
        raw = 0.10
    return min(raw, settings.MAX_BUY_SOL)


async def _dashboard_task(db: Database, interval: int, shutdown: asyncio.Event) -> None:
    """Background task that prints dashboard periodically."""
    while not shutdown.is_set():
        try:
            await print_dashboard(db)
        except Exception as e:
            logger.error("Dashboard error", error=str(e))
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def main() -> None:
    parser = argparse.ArgumentParser(description="Solana Sniper trading bot")
    parser.add_argument("--live", action="store_true", help="Enable live trading (default: paper)")
    parser.add_argument("--cycles", type=int, default=0, help="Number of cycles (0=infinite)")
    parser.add_argument("--dashboard-interval", type=int, default=60,
                        help="Dashboard log interval in seconds (default: 60)")
    args = parser.parse_args()

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )

    settings = Settings()
    if args.live:
        settings.PAPER_MODE = False

    # Validate smart money config
    if settings.COPY_TRADE_ENABLED and not settings.SMART_MONEY_WALLETS.strip():
        raise ValueError(
            "COPY_TRADE_ENABLED=true but SMART_MONEY_WALLETS is empty. "
            "Configure tracked wallets in .env or disable copy trading."
        )

    # Load wallets
    keypair = load_keypair(settings.KEYPAIR_PATH)
    pubkey = keypair.pubkey()

    # Multi-wallet setup
    wallets = []
    if settings.MULTI_WALLET_ENABLED and settings.WALLET_PATHS:
        wallet_paths = [p.strip() for p in settings.WALLET_PATHS.split(",") if p.strip()]
        wallets = load_wallets(wallet_paths)
        logger.info(
            "Multi-wallet mode",
            wallets_loaded=len(wallets),
            pubkeys=[str(w.pubkey()) for w in wallets],
        )
    else:
        wallets = [keypair]

    logger.info(
        "Sniper starting",
        wallet=str(pubkey),
        multi_wallet=settings.MULTI_WALLET_ENABLED,
        wallet_count=len(wallets),
        paper_mode=settings.PAPER_MODE,
        max_buy_sol=settings.MAX_BUY_SOL,
        max_portfolio_sol=settings.MAX_PORTFOLIO_SOL,
        stop_loss=f"{settings.STOP_LOSS_PCT}%",
        take_profit=f"{settings.TAKE_PROFIT_PCT}%",
    )

    # Initialize DB
    db = Database(settings.SNIPER_DB_PATH)
    await db.initialize()

    # Solana RPC client
    rpc_client = AsyncClient(settings.SOLANA_RPC_URL)

    # Check SOL balance
    try:
        balance = await get_sol_balance(rpc_client, pubkey)
        logger.info("Wallet balance", sol=balance)
        if balance < 0.01 and not settings.PAPER_MODE:
            logger.warning(
                "Low SOL balance! Fund your wallet before live trading",
                wallet=str(pubkey),
                balance=balance,
            )
    except Exception:
        logger.warning("Could not fetch wallet balance (RPC may be down)")
        balance = 0.0

    shutdown_event = asyncio.Event()

    def _shutdown(sig, frame):
        logger.info("Shutdown signal received", signal=sig)
        shutdown_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    try:
        signal.signal(signal.SIGTERM, _shutdown)
    except (OSError, ValueError):
        pass

    last_signal_check = datetime.min.replace(tzinfo=timezone.utc)
    cycle_count = 0

    # Start background dashboard
    dash_task = asyncio.create_task(
        _dashboard_task(db, args.dashboard_interval, shutdown_event)
    )

    # Start Telegram command bot
    from sniper.telegram_bot import telegram_command_loop
    tg_task = asyncio.create_task(
        telegram_command_loop(settings, shutdown_event)
    )

    # Start copy trader background task (score boost mode — not blind buying)
    copy_trade_task = None
    if settings.COPY_TRADE_ENABLED:
        async def _on_smart_money_signal(token_mint: str, source_wallet: str | None) -> None:
            """Notification when a tracked wallet buys — sends Telegram alert."""
            await send_telegram(
                f"Smart Money Signal\n"
                f"Token: {token_mint[:20]}...\n"
                f"Wallet: {source_wallet[:8] + '...' if source_wallet else 'unknown'}\n"
                f"Score boost: +{settings.COPY_TRADE_SCORE_BOOST}/wallet conviction\n"
                f"Token will be prioritized in next scan cycle",
                settings,
            )

        copy_trade_task = asyncio.create_task(
            monitor_wallets(settings, _on_smart_money_signal, send_telegram_fn=send_telegram)
        )
        logger.info("Copy trader started (score boost mode)")

    try:
        async with aiohttp.ClientSession() as session:
            while not shutdown_event.is_set():
                try:
                    now = datetime.now(timezone.utc)

                    # --- Signal check phase ---
                    await prune_stale_signals(max_age_minutes=max(60, settings.BACKFILL_MAX_MINUTES))
                    elapsed = (now - last_signal_check).total_seconds()
                    if elapsed >= settings.POLL_INTERVAL_SECONDS:
                        signals = await read_new_signals(
                            settings.SCOUT_DB_PATH,
                            last_signal_check,
                            settings.MIN_CONVICTION_SCORE,
                        )
                        actionable, skipped = await filter_actionable(signals, db, settings)
                        last_signal_check = now

                        # Notify skipped signals (cooldown, existing position)
                        if skipped:
                            skip_msg = "Skipped signals:\n" + "\n".join(f"  {s}" for s in skipped)
                            logger.info("Signals skipped", count=len(skipped))
                            await send_telegram(skip_msg, settings)

                        # Trigger GODMODE scans for all actionable tokens proactively
                        # so data is ready by the time we reach the buy check
                        if settings.GODMODE_ENABLED and actionable:
                            from sniper.godmode import trigger_godmode_scan
                            await asyncio.gather(
                                *[trigger_godmode_scan(s.contract_address, settings) for s in actionable],
                                return_exceptions=True,
                            )

                        # Fetch SOL balance once before iterating signals
                        cycle_balance = await get_sol_balance(rpc_client, pubkey) if not settings.PAPER_MODE else 1.0

                        for sig_data in actionable:
                            safety_passed = False
                            # Pre-trade checks
                            open_count = await db.count_open_positions()
                            if open_count >= settings.MAX_OPEN_POSITIONS:
                                logger.info("Max positions reached", count=open_count)
                                break

                            # Conviction-based direct sizing
                            conviction = sig_data.conviction_score or 0

                            # Smart money boost (adds to conviction score, not to bet size)
                            async with signals_lock:
                                sm = smart_money_signals.get(sig_data.contract_address)
                            if sm is not None:
                                wallet_count = sm["count"]
                                boost = min(
                                    wallet_count * settings.COPY_TRADE_SCORE_BOOST,
                                    settings.SMART_MONEY_BOOST_CAP,
                                )
                                conviction += boost
                                logger.info(
                                    "Smart money boost applied",
                                    token=sig_data.token_name,
                                    original=sig_data.conviction_score,
                                    boosted=conviction,
                                    smart_wallets=wallet_count,
                                    boost=boost,
                                )

                            # Pre-buy safety: real-time liquidity check via DexScreener
                            # Fetched BEFORE sizing so live_liq feeds into scaling
                            live_liq = 0.0
                            live_mcap = 0.0
                            try:
                                async with session.get(
                                    f"https://api.dexscreener.com/tokens/v1/solana/{sig_data.contract_address}",
                                    timeout=aiohttp.ClientTimeout(total=5),
                                ) as dex_resp:
                                    if dex_resp.status == 200:
                                        dex_data = await dex_resp.json()
                                        if isinstance(dex_data, list) and dex_data:
                                            live_mcap = float(dex_data[0].get("marketCap") or 0)
                                            liq_obj = dex_data[0].get("liquidity")
                                            if liq_obj and isinstance(liq_obj, dict):
                                                live_liq = float(liq_obj.get("usd", 0) or 0)
                                                if live_liq > 0 and live_liq < settings.MIN_LIQUIDITY_USD:
                                                    logger.warning(
                                                        "Live liquidity below minimum — skipping buy",
                                                        token=sig_data.token_name,
                                                        live_liquidity_usd=f"${live_liq:,.0f}",
                                                        min_required=f"${settings.MIN_LIQUIDITY_USD:,.0f}",
                                                    )
                                                    continue
                                                logger.debug(
                                                    "Live liquidity check passed",
                                                    token=sig_data.token_name,
                                                    liquidity_usd=f"${live_liq:,.0f}",
                                                )
                                            else:
                                                logger.debug(
                                                    "DexScreener liquidity data unavailable — proceeding",
                                                    token=sig_data.token_name,
                                                )
                            except Exception as e:
                                logger.debug("DexScreener liquidity check failed, proceeding", error=str(e))

                            # Direct tiered sizing based on conviction score
                            buy_amount = _conviction_bet_size(conviction, settings)

                            # Apply hard ceiling
                            buy_amount = min(buy_amount, settings.KELLY_MAX_BET)

                            # Liquidity scaling — uses live DexScreener liquidity
                            # Only scales DOWN for thin liquidity tokens
                            if settings.LIQUIDITY_SIZING_ENABLED and live_liq > 0:
                                if live_liq < settings.MIN_LIQUIDITY_USD:
                                    size_ratio = live_liq / settings.MIN_LIQUIDITY_USD
                                    buy_amount = round(buy_amount * size_ratio, 4)
                                    logger.info(
                                        "Liquidity scaling applied",
                                        token=sig_data.token_name,
                                        liq=f"${live_liq:,.0f}",
                                        size_ratio=f"{size_ratio:.2f}",
                                        buy_amount=f"{buy_amount:.4f} SOL",
                                    )

                            # Apply floor — only if liquidity scaling didn't reduce the bet
                            if live_liq >= 20000 or live_liq == 0:
                                # No scaling happened — apply normal floor
                                buy_amount = max(settings.KELLY_MIN_BET, buy_amount)
                            elif live_liq >= 10000:
                                # Scaling applied but decent liquidity — lower floor
                                buy_amount = max(settings.KELLY_MIN_BET * 0.5, buy_amount)
                            else:
                                buy_amount = max(0.1, buy_amount)  # absolute minimum for gas

                            logger.info(
                                "Position sizing (conviction-tiered)",
                                token=sig_data.token_name,
                                conviction=f"{conviction:.1f}",
                                buy_amount=f"{buy_amount:.4f} SOL",
                                kelly_max=settings.KELLY_MAX_BET,
                                live_liq=f"${live_liq:,.0f}",
                            )

                            exposure = await db.get_total_exposure_sol()
                            if exposure + buy_amount > settings.MAX_PORTFOLIO_SOL:
                                logger.info("Max exposure reached", exposure=exposure)
                                break

                            if not settings.PAPER_MODE:
                                max_available = cycle_balance - 0.01  # Reserve for gas
                                if max_available < settings.KELLY_MIN_BET:
                                    logger.warning("Insufficient SOL", balance=cycle_balance)
                                    break
                                if buy_amount > max_available:
                                    logger.info("Capping buy to available balance",
                                        original=f"{buy_amount:.4f}", capped=f"{max_available:.4f}")
                                    buy_amount = max_available

                            # Pre-buy safety: Rugcheck score + Helius/GODMODE bundle detection
                            try:
                                async with session.get(
                                    f"https://api.rugcheck.xyz/v1/tokens/{sig_data.contract_address}/report",
                                    timeout=aiohttp.ClientTimeout(total=5),
                                ) as rc_full_resp:
                                    if rc_full_resp.status == 200:
                                        rc_full = await rc_full_resp.json()

                                        # --- Safety check (extracted from full report) ---
                                        rc_score = rc_full.get("score", 0)
                                        rc_risks = rc_full.get("risks", [])
                                        rc_names = [r.get("name", "") if isinstance(r, dict) else str(r) for r in rc_risks]
                                        danger_keywords = ["rug", "honeypot", "mintable", "freeze"]
                                        has_danger = any(
                                            any(kw in r.lower() for kw in danger_keywords)
                                            for r in rc_names
                                        )
                                        if rc_score >= 10000 or (rc_score >= 5000 and has_danger):
                                            logger.warning(
                                                "Rugcheck BLOCKED pre-buy",
                                                token=sig_data.token_name,
                                                risk_score=rc_score,
                                                risks=rc_names[:3],
                                            )
                                            await send_telegram(
                                                f"Blocked by Rugcheck\n"
                                                f"Token: {sig_data.token_name} ({sig_data.ticker})\n"
                                                f"Risk: {rc_score} — {', '.join(rc_names[:3])}",
                                                settings,
                                            )
                                            continue
                                        logger.info(
                                            "Rugcheck passed",
                                            token=sig_data.token_name,
                                            risk_score=rc_score,
                                        )
                                        safety_passed = True

                                        # Holder concentration checks removed — LP pool addresses
                                        # cause too many false positives. Real protection comes from
                                        # Rugcheck score, Helius bundle detection, and GODMODE.
                            except Exception as e:
                                logger.warning("Rugcheck failed, trying GoPlus fallback", error=str(e))

                            # GODMODE bundle detection — check DB for prior scan results
                            if settings.GODMODE_ENABLED:
                                try:
                                    gm = await check_godmode_bundles(
                                        sig_data.contract_address, settings,
                                    )
                                    if gm["error"]:
                                        logger.debug(
                                            "GODMODE check error (fail open)",
                                            token=sig_data.token_name,
                                            error=gm["error"],
                                        )
                                    elif not gm["clean"]:
                                        logger.warning(
                                            "GODMODE bundle detected — skipping buy",
                                            token=sig_data.token_name,
                                            bundle_pct=f"{gm['bundle_pct']:.1f}%",
                                            bundled_wallets=gm["bundled_wallets"],
                                        )
                                        await send_telegram(
                                            f"Blocked by GODMODE\n"
                                            f"Token: {sig_data.token_name} ({sig_data.ticker})\n"
                                            f"Bundle: {gm['bundle_pct']:.1f}% of holders bundled\n"
                                            f"Bundled wallets: {gm['bundled_wallets']}",
                                            settings,
                                        )
                                        continue
                                    elif gm["bundled_wallets"] > 0:
                                        logger.info(
                                            "GODMODE: some bundles but below threshold",
                                            token=sig_data.token_name,
                                            bundle_pct=f"{gm['bundle_pct']:.1f}%",
                                            bundled_wallets=gm["bundled_wallets"],
                                        )
                                except Exception as e:
                                    logger.debug("GODMODE check exception (fail open)", error=str(e))

                            # Helius-based bundle detection — only for fresh tokens (<30 min old)
                            # Older tokens: bundler already sold, organic holders took over
                            token_age_hours = sig_data.token_age_days * 24 if sig_data.token_age_days else 0
                            skip_bundle = token_age_hours > 0.5  # >30 min
                            if skip_bundle:
                                logger.debug("Skipping bundle check — token older than 30 min", token=sig_data.token_name)
                            try:
                                from sniper.bundle_check import check_bundle
                                bundle = await check_bundle(sig_data.contract_address, session, settings) if not skip_bundle else {"is_bundled": False, "early_buyers": 0}
                                if bundle["is_bundled"]:
                                    logger.warning(
                                        "Helius bundle detected — early buyers share funding source",
                                        token=sig_data.token_name,
                                        bundle_pct=f"{bundle['bundle_pct']:.0f}%",
                                        early_buyers=bundle["early_buyers"],
                                        same_block=bundle["same_block_buyers"],
                                        funder=bundle["top_funder"][:15],
                                    )
                                    await send_telegram(
                                        f"Blocked — bundled launch detected\n"
                                        f"Token: {sig_data.token_name} ({sig_data.ticker})\n"
                                        f"Bundle: {bundle['bundle_pct']:.0f}% of early buyers from same funder\n"
                                        f"Early buyers: {bundle['early_buyers']} | Same block: {bundle['same_block_buyers']}",
                                        settings,
                                    )
                                    continue
                                elif bundle["early_buyers"] > 0:
                                    logger.info(
                                        "Bundle check passed",
                                        token=sig_data.token_name,
                                        bundle_pct=f"{bundle['bundle_pct']:.0f}%",
                                        early_buyers=bundle["early_buyers"],
                                    )
                            except Exception as e:
                                logger.debug("Helius bundle check failed", error=str(e))

                            # Fallback: GoPlus (works for non-pump tokens)
                            if not safety_passed:
                                try:
                                    is_safe = await check_token_safety(session, sig_data.contract_address)
                                    if not is_safe:
                                        logger.warning("GoPlus BLOCKED pre-buy", token=sig_data.token_name)
                                        continue
                                    safety_passed = True
                                    logger.info("GoPlus fallback passed", token=sig_data.token_name)
                                except Exception as e:
                                    logger.warning(
                                        "GoPlus safety check failed",
                                        token=sig_data.token_name,
                                        error=str(e),
                                    )

                            # If both checks failed (APIs down), don't buy blind
                            if not safety_passed:
                                logger.warning(
                                    "Both safety checks failed — skipping buy",
                                    token=sig_data.token_name,
                                )
                                await send_telegram(
                                    f"Safety check unavailable — skipped\n"
                                    f"Token: {sig_data.token_name} ({sig_data.ticker})\n"
                                    f"Both Rugcheck and GoPlus APIs down",
                                    settings,
                                )
                                continue

                            # Execute buy (single or multi-wallet)
                            try:
                                if settings.MULTI_WALLET_ENABLED and len(wallets) > 1:
                                    # Copy trade across all wallets (with timeout)
                                    try:
                                        results = await asyncio.wait_for(
                                            copy_buy(
                                                rpc_client, wallets, session,
                                                sig_data.contract_address,
                                                buy_amount,
                                                settings,
                                            ),
                                            timeout=settings.BUY_TIMEOUT_SECONDS,
                                        )
                                    except asyncio.TimeoutError:
                                        logger.warning(
                                            "Buy timed out, skipping",
                                            token=sig_data.token_name,
                                            timeout=settings.BUY_TIMEOUT_SECONDS,
                                            mode="multi_wallet",
                                        )
                                        continue
                                    # Log a position for each successful wallet
                                    succeeded = [r for r in results if r["success"]]
                                    if not succeeded:
                                        logger.error("All wallet buys failed", token=sig_data.token_name)
                                        continue

                                    for r in succeeded:
                                        pos = Position(
                                            contract_address=sig_data.contract_address,
                                            token_name=sig_data.token_name,
                                            ticker=sig_data.ticker,
                                            entry_sol=buy_amount,
                                            entry_token_amount=r["tokens"],
                                            entry_price_usd=sig_data.market_cap_usd or 0,
                                            entry_tx=r["tx"],
                                            paper=settings.PAPER_MODE,
                                            decimals=r.get("decimals"),
                                            conviction_score=sig_data.conviction_score,
                                            entry_liquidity_usd=live_liq,
                                            entry_mcap_usd=live_mcap,
                                        )
                                        pos_id = await db.open_position(pos)
                                        await db.log_trade(
                                            pos_id, "buy", buy_amount, r["tokens"], r["tx"], None,
                                        )

                                    tx_sig = succeeded[0]["tx"]
                                    total_tokens = sum(r["tokens"] for r in succeeded)
                                    total_sol = buy_amount * len(succeeded)

                                    logger.info(
                                        "Copy trade opened",
                                        token=sig_data.token_name,
                                        wallets_succeeded=len(succeeded),
                                        wallets_total=len(wallets),
                                        total_sol=total_sol,
                                        total_tokens=total_tokens,
                                    )

                                    await send_telegram(
                                        f"Copy Trade Opened\n"
                                        f"Token: {sig_data.token_name} ({sig_data.ticker})\n"
                                        f"Conviction: {sig_data.conviction_score or 0:.1f}\n"
                                        f"Wallets: {len(succeeded)}/{len(wallets)}\n"
                                        f"Total SOL: {total_sol}\n"
                                        f"Total Tokens: {total_tokens:.2f}",
                                        settings,
                                    )
                                    cycle_balance -= buy_amount

                                else:
                                    # Single wallet buy (with timeout)
                                    try:
                                        if settings.SPLIT_ORDERS:
                                            tx_sigs, tokens, decimals = await asyncio.wait_for(
                                                execute_buy_split(
                                                    rpc_client, keypair, session,
                                                    sig_data.contract_address,
                                                    buy_amount,
                                                    settings,
                                                    num_splits=settings.SPLIT_COUNT,
                                                    delay_seconds=settings.SPLIT_DELAY_SECONDS,
                                                ),
                                                timeout=settings.BUY_TIMEOUT_SECONDS,
                                            )
                                            tx_sig = tx_sigs[0]
                                        else:
                                            tx_sig, tokens, decimals = await asyncio.wait_for(
                                                execute_buy(
                                                    rpc_client, keypair, session,
                                                    sig_data.contract_address,
                                                    buy_amount,
                                                    settings,
                                                ),
                                                timeout=settings.BUY_TIMEOUT_SECONDS,
                                            )
                                    except asyncio.TimeoutError:
                                        logger.warning(
                                            "Buy timed out, skipping",
                                            token=sig_data.token_name,
                                            timeout=settings.BUY_TIMEOUT_SECONDS,
                                            mode="single_wallet",
                                        )
                                        continue
                                    pos = Position(
                                        contract_address=sig_data.contract_address,
                                        token_name=sig_data.token_name,
                                        ticker=sig_data.ticker,
                                        entry_sol=buy_amount,
                                        entry_token_amount=tokens,
                                        entry_price_usd=sig_data.market_cap_usd or 0,
                                        entry_tx=tx_sig,
                                        paper=settings.PAPER_MODE,
                                        decimals=decimals,
                                        conviction_score=sig_data.conviction_score,
                                        entry_liquidity_usd=live_liq,
                                        entry_mcap_usd=live_mcap,
                                    )
                                    pos_id = await db.open_position(pos)
                                    await db.log_trade(
                                        pos_id, "buy", buy_amount, tokens, tx_sig, None,
                                    )
                                    logger.info(
                                        "Position opened",
                                        token=sig_data.token_name,
                                        conviction=sig_data.conviction_score,
                                        sol=buy_amount,
                                        tokens=tokens,
                                    )
                                    await send_telegram(
                                        f"Position Opened\n"
                                        f"Token: {sig_data.token_name} ({sig_data.ticker})\n"
                                        f"Conviction: {sig_data.conviction_score or 0:.1f}\n"
                                        f"SOL: {buy_amount}\n"
                                        f"Tokens: {tokens:.2f}\n"
                                        f"TX: {tx_sig}",
                                        settings,
                                    )
                                    cycle_balance -= buy_amount

                            except Exception as e:
                                logger.error(
                                    "Buy failed",
                                    token=sig_data.token_name,
                                    error=str(e),
                                )

                    # --- Position monitoring phase ---
                    actions = await check_positions(
                        db, rpc_client, keypair, session, settings,
                    )

                    # --- Periodic portfolio summary ---
                    cycle_count += 1
                    if cycle_count % 10 == 0:
                        await portfolio_summary(db)

                except Exception as e:
                    logger.error("Loop iteration failed", error=str(e))

                if args.cycles > 0 and cycle_count >= args.cycles:
                    break

                # Wait for next iteration
                try:
                    await asyncio.wait_for(
                        shutdown_event.wait(),
                        timeout=settings.POSITION_CHECK_INTERVAL_SECONDS,
                    )
                except asyncio.TimeoutError:
                    pass

    finally:
        shutdown_event.set()
        dash_task.cancel()
        try:
            await dash_task
        except asyncio.CancelledError:
            pass
        if copy_trade_task is not None:
            copy_trade_task.cancel()
            try:
                await copy_trade_task
            except asyncio.CancelledError:
                pass
        await db.close()
        await rpc_client.close()
        logger.info("Sniper stopped", cycles_completed=cycle_count)


def _cli() -> None:
    """CLI entry point for `solana-sniper` command."""
    asyncio.run(main())


if __name__ == "__main__":
    _cli()

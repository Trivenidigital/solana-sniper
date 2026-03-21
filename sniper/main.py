"""Solana Sniper — main entry point."""

import argparse
import asyncio
import signal
from datetime import datetime, timezone

import aiohttp
import structlog
from solana.rpc.async_api import AsyncClient

from sniper.config import Settings
from sniper.dashboard import print_dashboard
from sniper.db import Database
from sniper.executor import execute_buy, execute_buy_split
from sniper.kelly import calculate_kelly_bet
from sniper.models import Position
from sniper.multi_wallet import copy_buy, load_wallets, get_all_balances
from sniper.position_manager import check_positions, portfolio_summary
from sniper.safety import check_token_safety
from sniper.signal_reader import filter_actionable, read_new_signals
from sniper.telegram_notify import send_telegram
from sniper.wallet import get_sol_balance, load_keypair

logger = structlog.get_logger()


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

    try:
        async with aiohttp.ClientSession() as session:
            while not shutdown_event.is_set():
                try:
                    now = datetime.now(timezone.utc)

                    # --- Signal check phase ---
                    elapsed = (now - last_signal_check).total_seconds()
                    if elapsed >= settings.POLL_INTERVAL_SECONDS:
                        signals = await read_new_signals(
                            settings.SCOUT_DB_PATH,
                            last_signal_check,
                            settings.MIN_CONVICTION_SCORE,
                        )
                        actionable = await filter_actionable(signals, db, settings)
                        last_signal_check = now

                        for sig_data in actionable:
                            # Pre-trade checks
                            open_count = await db.count_open_positions()
                            if open_count >= settings.MAX_OPEN_POSITIONS:
                                logger.info("Max positions reached", count=open_count)
                                break

                            # Position sizing: Kelly → liquidity adjustment
                            bal = await get_sol_balance(rpc_client, pubkey) if not settings.PAPER_MODE else 1.0
                            kelly_bet = await calculate_kelly_bet(db, bal, settings)

                            # Conviction-weighted sizing
                            conviction = sig_data.conviction_score or 30
                            conviction_factor = 0.5 + 0.5 * (conviction / 100)
                            kelly_bet_adj = kelly_bet * conviction_factor

                            if settings.LIQUIDITY_SIZING_ENABLED:
                                liq = sig_data.liquidity_usd or 5000
                                size_ratio = min(1.0, liq / 20000)
                                buy_amount = max(settings.KELLY_MIN_BET, kelly_bet_adj * size_ratio)
                            else:
                                buy_amount = kelly_bet_adj

                            buy_amount = min(buy_amount, settings.KELLY_MAX_BET)
                            logger.info(
                                "Position sizing",
                                token=sig_data.token_name,
                                kelly_bet=f"{kelly_bet:.4f}",
                                conviction=f"{conviction:.0f}",
                                conviction_factor=f"{conviction_factor:.2f}",
                                buy_amount=f"{buy_amount:.4f}",
                            )

                            exposure = await db.get_total_exposure_sol()
                            if exposure + buy_amount > settings.MAX_PORTFOLIO_SOL:
                                logger.info("Max exposure reached", exposure=exposure)
                                break

                            if not settings.PAPER_MODE:
                                bal = await get_sol_balance(rpc_client, pubkey)
                                if bal < buy_amount + 0.01:
                                    logger.warning("Insufficient SOL", balance=bal)
                                    break

                            # Anti-rug safety check
                            is_safe = await check_token_safety(
                                session, sig_data.contract_address,
                            )
                            if not is_safe:
                                logger.warning(
                                    "Token failed safety check — skipping",
                                    token=sig_data.token_name,
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
                                            entry_price_usd=0,  # Will be updated with actual price from DexScreener
                                            entry_tx=r["tx"],
                                            paper=settings.PAPER_MODE,
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
                                        f"Conviction: {sig_data.conviction_score:.1f}\n"
                                        f"Wallets: {len(succeeded)}/{len(wallets)}\n"
                                        f"Total SOL: {total_sol}\n"
                                        f"Total Tokens: {total_tokens:.2f}",
                                        settings,
                                    )

                                else:
                                    # Single wallet buy (with timeout)
                                    try:
                                        if settings.SPLIT_ORDERS:
                                            tx_sigs, tokens = await asyncio.wait_for(
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
                                            tx_sig, tokens = await asyncio.wait_for(
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
                                        entry_price_usd=0,  # Will be updated with actual price from DexScreener
                                        entry_tx=tx_sig,
                                        paper=settings.PAPER_MODE,
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
                                        f"Conviction: {sig_data.conviction_score:.1f}\n"
                                        f"SOL: {buy_amount}\n"
                                        f"Tokens: {tokens:.2f}\n"
                                        f"TX: {tx_sig}",
                                        settings,
                                    )

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
        await db.close()
        await rpc_client.close()
        logger.info("Sniper stopped", cycles_completed=cycle_count)


def _cli() -> None:
    """CLI entry point for `solana-sniper` command."""
    asyncio.run(main())


if __name__ == "__main__":
    _cli()

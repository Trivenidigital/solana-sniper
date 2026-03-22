"""Copy Trading — monitor profitable wallets and boost their picks.

Instead of blindly copying trades, detected tokens get a conviction
score boost (+20) in the scanner. The token still goes through our
full pipeline (quality gate, scoring, safety checks) before buying.
"""

import asyncio
import json
import structlog
import aiohttp
from datetime import datetime, timezone

from sniper.config import Settings

logger = structlog.get_logger()

# Tokens detected by copy trader — scanner checks this and boosts score
# Format: {contract_address: {"wallet": str, "detected_at": datetime}}
smart_money_signals: dict[str, dict] = {}


def prune_stale_signals(max_age_minutes: int = 30) -> None:
    """Remove smart money signals older than max_age_minutes."""
    now = datetime.now(timezone.utc)
    stale = [k for k, v in smart_money_signals.items()
             if (now - v["detected_at"]).total_seconds() > max_age_minutes * 60]
    for k in stale:
        del smart_money_signals[k]


async def monitor_wallets(settings: Settings, buy_callback) -> None:
    """Monitor tracked wallets for new token buys via WebSocket.

    When a tracked wallet buys a new token, calls buy_callback(token_mint, wallet_address).

    Uses Helius WebSocket for real-time transaction monitoring.
    """
    if not settings.COPY_TRADE_ENABLED:
        return

    tracked = _get_tracked_wallets(settings)
    if not tracked:
        logger.warning("Copy trading enabled but no wallets to track")
        return

    ws_url = settings.SOLANA_WS_URL
    # If using Helius, append API key
    if settings.HELIUS_API_KEY and "helius" not in ws_url:
        ws_url = f"wss://mainnet.helius-rpc.com/?api-key={settings.HELIUS_API_KEY}"

    logger.info("Copy trading started", wallets=len(tracked))

    while True:
        try:
            import websockets
            async with websockets.connect(ws_url) as ws:
                # Subscribe to each tracked wallet's transactions
                for i, wallet in enumerate(tracked):
                    subscribe = {
                        "jsonrpc": "2.0",
                        "id": i + 1,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [wallet]},
                            {"commitment": "confirmed"}
                        ]
                    }
                    await ws.send(json.dumps(subscribe))

                logger.info("WebSocket connected, monitoring wallets", count=len(tracked))

                async for msg in ws:
                    try:
                        data = json.loads(msg)
                        if "params" not in data:
                            continue

                        result = data.get("params", {}).get("result", {})
                        value = result.get("value", {})
                        logs = value.get("logs", [])
                        signature = value.get("signature", "")

                        # Detect swap transactions (Jupiter, Raydium, etc.)
                        is_swap = any(
                            "Instruction: Route" in log or
                            "Instruction: Swap" in log or
                            "Program JUP" in log or
                            "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA" in log
                            for log in logs
                        )

                        if is_swap:
                            # Parse which token was bought
                            token_mint = await _extract_bought_token(
                                signature, settings,
                            )
                            if token_mint:
                                # Find which tracked wallet made the trade
                                wallet = _find_wallet_in_logs(logs, tracked)
                                # Store as signal for scanner to boost
                                smart_money_signals[token_mint] = {
                                    "wallet": wallet,
                                    "detected_at": datetime.now(timezone.utc),
                                }
                                logger.info(
                                    "Smart money signal detected — boosting score",
                                    wallet=wallet[:8] + "..." if wallet else "unknown",
                                    token=token_mint,
                                    tx=signature[:20],
                                )
                                if buy_callback:
                                    await buy_callback(token_mint, wallet)

                    except Exception as e:
                        logger.debug("WebSocket message parse error", error=str(e))
                        continue

        except Exception as e:
            logger.warning("Copy trade WebSocket disconnected, reconnecting in 5s", error=str(e))
            await asyncio.sleep(5)


async def _extract_bought_token(signature: str, settings: Settings) -> str | None:
    """Parse a transaction to find which token was bought.

    Uses Helius parsed transaction API if available, otherwise RPC.
    """
    if settings.HELIUS_API_KEY:
        try:
            url = f"https://api.helius.xyz/v0/transactions/?api-key={settings.HELIUS_API_KEY}"
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json={"transactions": [signature]},
                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data and len(data) > 0:
                            tx = data[0]
                            # Look for token transfers where SOL was spent
                            for transfer in tx.get("tokenTransfers", []):
                                mint = transfer.get("mint", "")
                                if mint and mint != "So11111111111111111111111111111111111111112":
                                    return mint
        except Exception:
            pass
    return None


def _find_wallet_in_logs(logs: list, tracked: list) -> str | None:
    """Find which tracked wallet appears in transaction logs."""
    log_text = " ".join(logs)
    for wallet in tracked:
        if wallet in log_text:
            return wallet
    return None


def _get_tracked_wallets(settings: Settings) -> list[str]:
    """Get list of wallet addresses to track from shared config."""
    if not settings.SMART_MONEY_WALLETS:
        return []
    return [w.strip() for w in settings.SMART_MONEY_WALLETS.split(",") if w.strip()]


async def get_wallet_recent_trades(wallet: str, settings: Settings) -> list[dict]:
    """Get recent trades for a wallet (for analysis/display)."""
    if not settings.HELIUS_API_KEY:
        return []
    try:
        url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?api-key={settings.HELIUS_API_KEY}&limit=10&type=SWAP"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.json()
    except Exception:
        pass
    return []

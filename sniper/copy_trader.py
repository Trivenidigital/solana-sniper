"""Copy Trading — monitor profitable wallets and boost their picks.

Two outputs:
1. In-memory smart_money_signals dict for sniper conviction boost
2. DB writes to scout's smart_money_injections table (Direction 2)

Supports: Jupiter, Raydium (AMM/CPMM/V4), Orca, Meteora, pump.fun
"""

import asyncio
import json
import time
from datetime import datetime, timezone

import aiohttp
import aiosqlite
import structlog

from sniper.config import Settings

logger = structlog.get_logger()

SWAP_PATTERNS = [
    "Instruction: Route",
    "Instruction: Swap",
    "Program JUP",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",  # Raydium AMM
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",  # Raydium CPMM
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium V4
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",   # Orca Whirlpool
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",   # Meteora
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB",  # Meteora DLMM
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",   # pump.fun
]

INTERMEDIARY_MINTS = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}

# {token_mint: {"wallets": set[str], "count": int, "detected_at": datetime}}
smart_money_signals: dict[str, dict] = {}


def _is_swap_transaction(logs: list[str]) -> bool:
    return any(pattern in log for log in logs for pattern in SWAP_PATTERNS)


def _record_signal(token_mint: str, wallet: str) -> None:
    now = datetime.now(timezone.utc)
    if token_mint in smart_money_signals:
        smart_money_signals[token_mint]["wallets"].add(wallet)
        smart_money_signals[token_mint]["count"] = len(smart_money_signals[token_mint]["wallets"])
        smart_money_signals[token_mint]["detected_at"] = now
    else:
        smart_money_signals[token_mint] = {
            "wallets": {wallet}, "count": 1, "detected_at": now,
        }


def prune_stale_signals(max_age_minutes: int = 60) -> None:
    now = datetime.now(timezone.utc)
    stale = [k for k, v in smart_money_signals.items()
             if (now - v["detected_at"]).total_seconds() > max_age_minutes * 60]
    for k in stale:
        del smart_money_signals[k]


def _get_tracked_wallets(settings: Settings) -> list[str]:
    if not settings.SMART_MONEY_WALLETS:
        return []
    return [w.strip() for w in settings.SMART_MONEY_WALLETS.split(",") if w.strip()]


async def _write_injection(
    conn: aiosqlite.Connection,
    token_mint: str,
    wallet: str,
    tx_signature: str,
    source: str = "websocket",
) -> None:
    start = time.monotonic()
    try:
        await conn.execute(
            "INSERT OR IGNORE INTO smart_money_injections "
            "(token_mint, wallet_address, tx_signature, source) VALUES (?, ?, ?, ?)",
            (token_mint, wallet, tx_signature, source),
        )
        await conn.commit()
        elapsed = time.monotonic() - start
        if elapsed > 1.0:
            logger.warning("Slow injection write", elapsed_ms=int(elapsed * 1000))
    except Exception as e:
        logger.error("Failed to write injection", error=str(e), token=token_mint)


async def _extract_bought_token(
    signature: str, wallet: str, settings: Settings,
) -> str | None:
    if not settings.HELIUS_API_KEY:
        return None
    url = f"https://api.helius.xyz/v0/transactions/?api-key={settings.HELIUS_API_KEY}"
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json={"transactions": [signature]},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 429:
                        await asyncio.sleep((attempt + 1) * 2)
                        continue
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    if not data:
                        return None
                    tx = data[0]
                    native = tx.get("nativeTransfers", [])
                    if not any(t.get("fromUserAccount") == wallet for t in native):
                        return None
                    transfers = tx.get("tokenTransfers", [])
                    wallet_receives = [
                        t for t in transfers
                        if t.get("toUserAccount") == wallet
                        and t.get("mint") not in INTERMEDIARY_MINTS
                    ]
                    if wallet_receives:
                        return wallet_receives[-1].get("mint")
                    for t in transfers:
                        mint = t.get("mint", "")
                        if mint and mint not in INTERMEDIARY_MINTS:
                            return mint
                    return None
        except Exception as e:
            if attempt < 2:
                await asyncio.sleep((attempt + 1))
            else:
                logger.debug("Token extraction failed", error=str(e))
    return None


def _find_wallet_in_logs(logs: list[str], tracked: list[str]) -> str | None:
    """Note: substring match on full 44-char base58 addresses — negligible collision risk."""
    log_text = " ".join(logs)
    for wallet in tracked:
        if wallet in log_text:
            return wallet
    return None


async def _open_scout_db_writer(settings: Settings) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(str(settings.SCOUT_DB_PATH))
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    return conn


async def _backfill_after_reconnect(
    tracked: list[str], settings: Settings,
    last_signatures: dict[str, str], conn: aiosqlite.Connection,
) -> None:
    if not settings.HELIUS_API_KEY:
        return
    max_age_seconds = settings.BACKFILL_MAX_MINUTES * 60
    now = datetime.now(timezone.utc)
    async with aiohttp.ClientSession() as session:
        for wallet in tracked:
            try:
                url = (
                    f"https://api.helius.xyz/v0/addresses/{wallet}/transactions"
                    f"?api-key={settings.HELIUS_API_KEY}&limit=20&type=SWAP"
                )
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        continue
                    txns = await resp.json()
                for tx in txns:
                    sig = tx.get("signature", "")
                    ts = tx.get("timestamp", 0)
                    if ts and (now.timestamp() - ts) > max_age_seconds:
                        continue
                    token_mint = None
                    for transfer in tx.get("tokenTransfers", []):
                        mint = transfer.get("mint", "")
                        if mint and mint not in INTERMEDIARY_MINTS and transfer.get("toUserAccount") == wallet:
                            token_mint = mint
                    if token_mint:
                        _record_signal(token_mint, wallet)
                        await _write_injection(conn, token_mint, wallet, sig, source="backfill")
                        logger.info("Backfilled signal", wallet=wallet[:8], token=token_mint[:20])
                    if sig:
                        last_signatures[wallet] = sig
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.debug("Backfill failed", wallet=wallet[:8], error=str(e))


async def monitor_wallets(settings: Settings, buy_callback, send_telegram_fn=None) -> None:
    if not settings.COPY_TRADE_ENABLED:
        return
    tracked = _get_tracked_wallets(settings)
    if not tracked:
        raise ValueError("COPY_TRADE_ENABLED=true but SMART_MONEY_WALLETS is empty.")
    scout_db_conn = await _open_scout_db_writer(settings)
    ws_url = f"wss://mainnet.helius-rpc.com/?api-key={settings.HELIUS_API_KEY}"
    last_signatures: dict[str, str] = {}
    last_injection_time = datetime.now(timezone.utc)
    logger.info("Copy trading started", wallets=len(tracked))
    while True:
        try:
            import websockets
            async with websockets.connect(ws_url, ping_interval=30, ping_timeout=10) as ws:
                confirmed = 0
                for i, wallet in enumerate(tracked):
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": i + 1, "method": "logsSubscribe",
                        "params": [{"mentions": [wallet]}, {"commitment": "confirmed"}],
                    }))
                try:
                    deadline = asyncio.get_event_loop().time() + 5.0
                    while confirmed < len(tracked):
                        remaining = deadline - asyncio.get_event_loop().time()
                        if remaining <= 0:
                            break
                        msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
                        data = json.loads(msg)
                        if "result" in data and isinstance(data["result"], int):
                            confirmed += 1
                except asyncio.TimeoutError:
                    pass
                logger.info("WebSocket connected", subs=confirmed, total=len(tracked))
                await _backfill_after_reconnect(tracked, settings, last_signatures, scout_db_conn)
                async for msg in ws:
                    try:
                        data = json.loads(msg)
                        if "params" not in data:
                            continue
                        value = data["params"].get("result", {}).get("value", {})
                        logs = value.get("logs", [])
                        signature = value.get("signature", "")
                        if _is_swap_transaction(logs):
                            wallet = _find_wallet_in_logs(logs, tracked)
                            token_mint = await _extract_bought_token(signature, wallet or "", settings)
                            if token_mint:
                                _record_signal(token_mint, wallet or "unknown")
                                last_injection_time = datetime.now(timezone.utc)
                                await _write_injection(scout_db_conn, token_mint, wallet or "unknown", signature)
                                logger.info("Smart money signal",
                                    wallet=wallet[:8] + "..." if wallet else "unknown",
                                    token=token_mint[:20],
                                    wallets=smart_money_signals.get(token_mint, {}).get("count", 1))
                                if buy_callback:
                                    await buy_callback(token_mint, wallet)
                        stale = (datetime.now(timezone.utc) - last_injection_time).total_seconds()
                        if stale > 1800:
                            logger.warning("No signals in 30min")
                            if send_telegram_fn:
                                await send_telegram_fn(
                                    f"Smart Money WebSocket may be down\nNo signals in {int(stale/60)} min", settings)
                                last_injection_time = datetime.now(timezone.utc)
                    except Exception as e:
                        logger.debug("WS parse error", error=str(e))
        except Exception as e:
            logger.warning("WS disconnected, reconnecting", error=str(e))
            await asyncio.sleep(3)


async def get_wallet_recent_trades(wallet: str, settings: Settings) -> list[dict]:
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

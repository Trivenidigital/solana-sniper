"""Trade execution — buy and sell tokens via Jupiter swaps."""

import asyncio
import uuid
from collections import OrderedDict

import aiohttp
import structlog
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solders.keypair import Keypair
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from sniper.config import Settings
from sniper.exceptions import ExecutionError, TransactionFailedError
from sniper.jupiter import (
    LAMPORTS_PER_SOL,
    SOL_MINT,
    get_quote,
    get_swap_transaction,
)

logger = structlog.get_logger()

# --- RPC Failover ---
_rpc_clients: list[AsyncClient] = []


async def _get_healthy_client(settings: Settings) -> AsyncClient:
    """Get a healthy RPC client, falling back to alternatives.

    NOTE: This is infrastructure for future use. Not wired into execution path yet
    to avoid changing the hot path. Call manually if primary RPC is unresponsive.
    """
    if not _rpc_clients:
        urls = [settings.SOLANA_RPC_URL]
        if settings.SOLANA_RPC_URLS:
            urls.extend(u.strip() for u in settings.SOLANA_RPC_URLS.split(",") if u.strip())
        for url in urls:
            _rpc_clients.append(AsyncClient(url))

    for rpc_client in _rpc_clients:
        try:
            await asyncio.wait_for(rpc_client.get_health(), timeout=3)
            return rpc_client
        except Exception:
            continue
    return _rpc_clients[0]  # Fallback to primary


async def _get_actual_token_balance(client: AsyncClient, owner_pubkey, mint_address: str) -> int:
    """Get actual on-chain token balance for a mint."""
    from solana.rpc.types import TokenAccountOpts
    from solders.pubkey import Pubkey
    resp = await client.get_token_accounts_by_owner_json_parsed(
        owner_pubkey, TokenAccountOpts(mint=Pubkey.from_string(mint_address)),
    )
    if resp.value:
        return int(resp.value[0].account.data.parsed["info"]["tokenAmount"]["amount"])
    return 0


# Module-level LRU cache for token decimals (contract_address -> decimals)
_decimals_cache: OrderedDict[str, int] = OrderedDict()
_DECIMALS_CACHE_MAX = 500


async def _get_token_decimals(contract_address: str, session: aiohttp.ClientSession, settings: Settings) -> int:
    """Fetch token decimals from Solana RPC, with caching and fallback."""
    if contract_address in _decimals_cache:
        _decimals_cache.move_to_end(contract_address)
        return _decimals_cache[contract_address]
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [contract_address, {"encoding": "jsonParsed"}],
        }
        async with session.post(
            settings.SOLANA_RPC_URL, json=payload, timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            data = await resp.json()
            parsed = data["result"]["value"]["data"]["parsed"]["info"]
            decimals = int(parsed["decimals"])
            _decimals_cache[contract_address] = decimals
            if len(_decimals_cache) > _DECIMALS_CACHE_MAX:
                _decimals_cache.popitem(last=False)  # Remove oldest
            return decimals
    except Exception:
        # Fallback: pump.fun tokens are 6, most others are 9
        return 6 if contract_address.endswith("pump") else 9


_BUY_SLIPPAGE_TIERS = [500, 1000, 1500]  # 5%, 10%, 15%


async def execute_buy(
    client: AsyncClient,
    keypair: Keypair,
    session: aiohttp.ClientSession,
    contract_address: str,
    sol_amount: float,
    settings: Settings,
) -> tuple[str, float]:
    """Buy a token with SOL via Jupiter with auto-retry on increasing slippage.

    Tries at 5% → 10% → 15% slippage before giving up.

    Returns:
        (tx_signature, tokens_received)
    """
    amount_lamports = int(sol_amount * LAMPORTS_PER_SOL)

    for attempt, slippage_bps in enumerate(_BUY_SLIPPAGE_TIERS):
        try:
            buy_settings = settings.model_copy(update={"SLIPPAGE_BPS": slippage_bps})

            quote = await get_quote(
                session, SOL_MINT, contract_address, amount_lamports, buy_settings,
            )
            tokens_received = float(quote.out_amount)

            logger.info(
                "Buy quote received",
                token=contract_address,
                sol_in=sol_amount,
                tokens_out=tokens_received,
                price_impact=f"{quote.price_impact_pct:.2f}%",
                slippage_bps=slippage_bps,
                attempt=attempt + 1,
                paper=settings.PAPER_MODE,
            )

            if settings.PAPER_MODE:
                tx_sig = f"paper-buy-{uuid.uuid4().hex[:12]}"
                logger.info("PAPER BUY executed", tx=tx_sig, token=contract_address, sol=sol_amount)
                return (tx_sig, tokens_received)

            # Live execution
            tx_bytes = await get_swap_transaction(
                session, quote, str(keypair.pubkey()), buy_settings,
            )
            tx_sig = await _sign_and_send(client, keypair, tx_bytes, settings)
            logger.info("BUY executed", tx=tx_sig, token=contract_address, sol=sol_amount,
                        slippage_bps=slippage_bps, attempt=attempt + 1)

            # Verify transaction succeeded on-chain
            await asyncio.sleep(2)
            for verify_attempt in range(3):
                try:
                    tx_resp = await client.get_transaction(
                        Signature.from_string(tx_sig),
                        max_supported_transaction_version=0,
                    )
                    if tx_resp.value is None:
                        if verify_attempt < 2:
                            await asyncio.sleep(2)
                            continue
                        raise TransactionFailedError(
                            f"Transaction not found on-chain after 3 attempts: {tx_sig}"
                        )
                    if tx_resp.value.transaction.meta.err:
                        raise TransactionFailedError(
                            f"Transaction failed on-chain: {tx_resp.value.transaction.meta.err} TX: {tx_sig}"
                        )
                    break  # TX confirmed and no error
                except TransactionFailedError:
                    raise
                except Exception as verify_err:
                    if verify_attempt < 2:
                        await asyncio.sleep(2)
                        continue
                    raise TransactionFailedError(
                        f"Could not verify transaction: {verify_err} TX: {tx_sig}"
                    ) from verify_err

            return (tx_sig, tokens_received)

        except Exception as e:
            error_str = str(e)
            is_slippage = "0x1771" in error_str or "0x1789" in error_str or "SlippageToleranceExceeded" in error_str
            if is_slippage and attempt < len(_BUY_SLIPPAGE_TIERS) - 1:
                logger.warning(
                    "Buy failed on slippage, retrying with higher tolerance",
                    token=contract_address,
                    slippage_bps=slippage_bps,
                    next_slippage_bps=_BUY_SLIPPAGE_TIERS[attempt + 1],
                    attempt=attempt + 1,
                )
                await asyncio.sleep(1)
                continue
            else:
                raise


_SELL_SLIPPAGE_TIERS = [500, 1000, 1500, 2500]  # 5%, 10%, 15%, 25%


async def execute_sell(
    client: AsyncClient,
    keypair: Keypair,
    session: aiohttp.ClientSession,
    contract_address: str,
    token_amount: int,
    settings: Settings,
) -> tuple[str, float]:
    """Sell a token for SOL via Jupiter with auto-retry on increasing slippage.

    Tries at 5% → 10% → 15% → 25% slippage before giving up.

    Returns:
        (tx_signature, sol_received)
    """
    # On-chain balance check before sell (skip in paper mode)
    if not settings.PAPER_MODE:
        actual_balance = await _get_actual_token_balance(client, keypair.pubkey(), contract_address)
        if actual_balance <= 0:
            raise ExecutionError(f"No tokens to sell — on-chain balance is 0")
        if actual_balance < token_amount:
            logger.warning("Adjusting sell amount to actual balance", requested=token_amount, actual=actual_balance)
            token_amount = actual_balance

    for attempt, slippage_bps in enumerate(_SELL_SLIPPAGE_TIERS):
        try:
            # Override slippage for this attempt
            sell_settings = settings.model_copy(update={"SLIPPAGE_BPS": slippage_bps})

            quote = await get_quote(
                session, contract_address, SOL_MINT, token_amount, sell_settings,
            )
            sol_received = float(quote.out_amount) / LAMPORTS_PER_SOL

            logger.info(
                "Sell quote received",
                token=contract_address,
                tokens_in=token_amount,
                sol_out=sol_received,
                price_impact=f"{quote.price_impact_pct:.2f}%",
                slippage_bps=slippage_bps,
                attempt=attempt + 1,
                paper=settings.PAPER_MODE,
            )

            if settings.PAPER_MODE:
                # NOTE: Paper mode does not track on-chain balance, so partial sells
                # may over-sell. This is acceptable since we run live. To fix properly,
                # track cumulative sold amounts in a module-level dict keyed by contract_address.
                tx_sig = f"paper-sell-{uuid.uuid4().hex[:12]}"
                logger.info("PAPER SELL executed", tx=tx_sig, token=contract_address, sol=sol_received)
                return (tx_sig, sol_received)

            tx_bytes = await get_swap_transaction(
                session, quote, str(keypair.pubkey()), sell_settings,
            )
            tx_sig = await _sign_and_send(client, keypair, tx_bytes, settings)
            logger.info("SELL executed", tx=tx_sig, token=contract_address, sol=sol_received,
                        slippage_bps=slippage_bps, attempt=attempt + 1)
            return (tx_sig, sol_received)

        except Exception as e:
            error_str = str(e)
            is_slippage = "0x1788" in error_str or "0x1789" in error_str or "SlippageToleranceExceeded" in error_str
            if is_slippage and attempt < len(_SELL_SLIPPAGE_TIERS) - 1:
                logger.warning(
                    "Sell failed on slippage, retrying with higher tolerance",
                    token=contract_address,
                    slippage_bps=slippage_bps,
                    next_slippage_bps=_SELL_SLIPPAGE_TIERS[attempt + 1],
                    attempt=attempt + 1,
                )
                await asyncio.sleep(1)
                continue
            else:
                raise  # Non-slippage error or last attempt — propagate


async def get_current_value_sol(
    session: aiohttp.ClientSession,
    contract_address: str,
    token_amount: int,
    settings: Settings,
) -> float | None:
    """Get current SOL value of a token position.

    Uses DexScreener (free, no rate limit) as primary price source.
    Falls back to Jupiter quote if DexScreener fails.
    Returns None if both fail.
    """
    if token_amount <= 0:
        return 0.0

    # Primary: DexScreener (no rate limit)
    try:
        url = f"https://api.dexscreener.com/tokens/v1/solana/{contract_address}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                pairs = await resp.json()
                if pairs and isinstance(pairs, list) and len(pairs) > 0:
                    pair = pairs[0]
                    price_native = pair.get("priceNative")
                    if price_native:
                        price_sol = float(price_native)
                        decimals = await _get_token_decimals(contract_address, session, settings)
                        human_tokens = token_amount / (10 ** decimals)
                        return human_tokens * price_sol
    except Exception:
        pass

    # Fallback: Jupiter quote
    try:
        quote = await get_quote(
            session, contract_address, SOL_MINT, token_amount, settings,
        )
        return float(quote.out_amount) / LAMPORTS_PER_SOL
    except Exception:
        logger.warning("Price check failed", token=contract_address)
        return None


async def execute_buy_split(
    client: AsyncClient,
    keypair: Keypair,
    session: aiohttp.ClientSession,
    contract_address: str,
    total_sol: float,
    settings: Settings,
    num_splits: int = 3,
    delay_seconds: int = 10,
) -> tuple[list[str], float]:
    """Buy a token with SOL via Jupiter, splitting into multiple smaller orders.

    This reduces price impact by spreading the buy over time.

    Returns:
        (list_of_tx_signatures, total_tokens_received)
    """
    sol_per_split = total_sol / num_splits
    tx_sigs: list[str] = []
    total_tokens: float = 0.0

    for i in range(num_splits):
        logger.info(
            "Split order",
            split=f"{i + 1}/{num_splits}",
            sol=sol_per_split,
            token=contract_address,
        )
        try:
            tx_sig, tokens = await execute_buy(
                client, keypair, session, contract_address, sol_per_split, settings,
            )
            tx_sigs.append(tx_sig)
            total_tokens += tokens
        except Exception as e:
            logger.error(
                "Split order failed",
                split=f"{i + 1}/{num_splits}",
                token=contract_address,
                error=str(e),
            )
            # Continue with remaining splits even if one fails
            if not tx_sigs:
                raise  # Re-raise if the very first split fails

        # Delay between splits (skip after last split)
        if i < num_splits - 1:
            await asyncio.sleep(delay_seconds)

    if not tx_sigs:
        raise ExecutionError("All split orders failed")

    logger.info(
        "Split buy complete",
        token=contract_address,
        total_sol=total_sol,
        total_tokens=total_tokens,
        successful_splits=len(tx_sigs),
        total_splits=num_splits,
    )
    return (tx_sigs, total_tokens)


async def _sign_and_send(
    client: AsyncClient, keypair: Keypair, tx_bytes: bytes,
    settings: Settings | None = None,
) -> str:
    """Deserialize, sign, send (via Jito if enabled), and confirm."""
    if settings and settings.JITO_ENABLED:
        from sniper.jito import send_transaction_with_jito
        return await send_transaction_with_jito(client, keypair, tx_bytes, settings)

    # Standard path
    try:
        txn = VersionedTransaction.from_bytes(tx_bytes)
        txn = VersionedTransaction(txn.message, [keypair])

        resp = await client.send_transaction(txn)
        sig = resp.value
        sig_str = str(sig)

        await client.confirm_transaction(Signature.from_string(sig_str), commitment=Confirmed)
        return sig_str
    except Exception as e:
        raise TransactionFailedError(f"Transaction failed: {e}") from e

"""Trade execution — buy and sell tokens via Jupiter swaps."""

import uuid

import aiohttp
import structlog
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solders.keypair import Keypair
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


async def execute_buy(
    client: AsyncClient,
    keypair: Keypair,
    session: aiohttp.ClientSession,
    contract_address: str,
    sol_amount: float,
    settings: Settings,
) -> tuple[str, float]:
    """Buy a token with SOL via Jupiter.

    Returns:
        (tx_signature, tokens_received)
    """
    amount_lamports = int(sol_amount * LAMPORTS_PER_SOL)

    # Get quote: SOL -> token
    quote = await get_quote(
        session, SOL_MINT, contract_address, amount_lamports, settings,
    )
    tokens_received = float(quote.out_amount)

    logger.info(
        "Buy quote received",
        token=contract_address,
        sol_in=sol_amount,
        tokens_out=tokens_received,
        price_impact=f"{quote.price_impact_pct:.2f}%",
        paper=settings.PAPER_MODE,
    )

    if settings.PAPER_MODE:
        tx_sig = f"paper-buy-{uuid.uuid4().hex[:12]}"
        logger.info("PAPER BUY executed", tx=tx_sig, token=contract_address, sol=sol_amount)
        return (tx_sig, tokens_received)

    # Live execution
    tx_bytes = await get_swap_transaction(
        session, quote, str(keypair.pubkey()), settings,
    )
    tx_sig = await _sign_and_send(client, keypair, tx_bytes)
    logger.info("BUY executed", tx=tx_sig, token=contract_address, sol=sol_amount)
    return (tx_sig, tokens_received)


async def execute_sell(
    client: AsyncClient,
    keypair: Keypair,
    session: aiohttp.ClientSession,
    contract_address: str,
    token_amount: int,
    settings: Settings,
) -> tuple[str, float]:
    """Sell a token for SOL via Jupiter.

    Returns:
        (tx_signature, sol_received)
    """
    # Get quote: token -> SOL
    quote = await get_quote(
        session, contract_address, SOL_MINT, token_amount, settings,
    )
    sol_received = float(quote.out_amount) / LAMPORTS_PER_SOL

    logger.info(
        "Sell quote received",
        token=contract_address,
        tokens_in=token_amount,
        sol_out=sol_received,
        price_impact=f"{quote.price_impact_pct:.2f}%",
        paper=settings.PAPER_MODE,
    )

    if settings.PAPER_MODE:
        tx_sig = f"paper-sell-{uuid.uuid4().hex[:12]}"
        logger.info("PAPER SELL executed", tx=tx_sig, token=contract_address, sol=sol_received)
        return (tx_sig, sol_received)

    tx_bytes = await get_swap_transaction(
        session, quote, str(keypair.pubkey()), settings,
    )
    tx_sig = await _sign_and_send(client, keypair, tx_bytes)
    logger.info("SELL executed", tx=tx_sig, token=contract_address, sol=sol_received)
    return (tx_sig, sol_received)


async def get_current_value_sol(
    session: aiohttp.ClientSession,
    contract_address: str,
    token_amount: int,
    settings: Settings,
) -> float | None:
    """Get current SOL value of a token position via Jupiter quote.

    Returns None if quote fails (token may be delisted/illiquid).
    """
    if token_amount <= 0:
        return 0.0
    try:
        quote = await get_quote(
            session, contract_address, SOL_MINT, token_amount, settings,
        )
        return float(quote.out_amount) / LAMPORTS_PER_SOL
    except Exception:
        logger.warning("Price check failed", token=contract_address)
        return None


async def _sign_and_send(
    client: AsyncClient, keypair: Keypair, tx_bytes: bytes,
) -> str:
    """Deserialize, sign, send, and confirm a versioned transaction."""
    try:
        txn = VersionedTransaction.from_bytes(tx_bytes)
        txn = VersionedTransaction(txn.message, [keypair])

        resp = await client.send_transaction(txn)
        sig = str(resp.value)

        await client.confirm_transaction(sig, commitment=Confirmed)
        return sig
    except Exception as e:
        raise TransactionFailedError(f"Transaction failed: {e}") from e

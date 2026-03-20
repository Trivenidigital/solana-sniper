"""Jupiter V6 aggregator API client for Solana swaps."""

import base64

import aiohttp
import structlog

from sniper.config import Settings
from sniper.exceptions import JupiterQuoteError, JupiterSwapError
from sniper.models import JupiterQuote

logger = structlog.get_logger()

SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000
MAX_PRICE_IMPACT_PCT = 5.0


async def get_quote(
    session: aiohttp.ClientSession,
    input_mint: str,
    output_mint: str,
    amount: int,
    settings: Settings,
) -> JupiterQuote:
    """Get a swap quote from Jupiter V6.

    Args:
        amount: Amount in smallest unit (lamports for SOL).
    """
    url = f"{settings.JUPITER_API_URL}/quote"
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount),
        "slippageBps": str(settings.SLIPPAGE_BPS),
    }

    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=settings.JUPITER_TIMEOUT_SEC)) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise JupiterQuoteError(f"Jupiter quote failed ({resp.status}): {body}")
            data = await resp.json()
    except JupiterQuoteError:
        raise
    except Exception as e:
        raise JupiterQuoteError(f"Jupiter quote request failed: {e}") from e

    price_impact = float(data.get("priceImpactPct", 0))
    if price_impact > MAX_PRICE_IMPACT_PCT:
        raise JupiterQuoteError(
            f"Price impact too high: {price_impact:.2f}% (max {MAX_PRICE_IMPACT_PCT}%)"
        )

    return JupiterQuote(
        input_mint=data["inputMint"],
        output_mint=data["outputMint"],
        in_amount=int(data["inAmount"]),
        out_amount=int(data["outAmount"]),
        price_impact_pct=price_impact,
        raw_response=data,
    )


async def get_swap_transaction(
    session: aiohttp.ClientSession,
    quote: JupiterQuote,
    user_pubkey: str,
    settings: Settings,
) -> bytes:
    """Get a serialized swap transaction from Jupiter V6.

    Returns base64-decoded transaction bytes ready for signing.
    """
    url = f"{settings.JUPITER_API_URL}/swap"
    payload = {
        "quoteResponse": quote.raw_response,
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": True,
        "prioritizationFeeLamports": settings.PRIORITY_FEE_LAMPORTS,
    }

    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=settings.JUPITER_TIMEOUT_SEC)) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise JupiterSwapError(f"Jupiter swap failed ({resp.status}): {body}")
            data = await resp.json()
    except JupiterSwapError:
        raise
    except Exception as e:
        raise JupiterSwapError(f"Jupiter swap request failed: {e}") from e

    swap_tx_b64 = data.get("swapTransaction")
    if not swap_tx_b64:
        raise JupiterSwapError("No swapTransaction in Jupiter response")

    return base64.b64decode(swap_tx_b64)

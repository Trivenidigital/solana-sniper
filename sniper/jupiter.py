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


async def _fetch_quote(
    session: aiohttp.ClientSession,
    base_url: str,
    params: dict,
    timeout_sec: int,
) -> dict:
    """Fetch a raw quote response from a Jupiter endpoint."""
    url = f"{base_url}/quote"
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=timeout_sec)) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise JupiterQuoteError(f"Jupiter quote failed ({resp.status}): {body}")
        return await resp.json()


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

    Falls back to JUPITER_FALLBACK_URL if the primary endpoint fails.
    """
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount),
        "slippageBps": str(settings.SLIPPAGE_BPS),
    }

    try:
        data = await _fetch_quote(session, settings.JUPITER_API_URL, params, settings.JUPITER_TIMEOUT_SEC)
    except Exception as primary_err:
        fallback_url = settings.JUPITER_FALLBACK_URL
        if fallback_url and fallback_url != settings.JUPITER_API_URL:
            logger.warning(
                "Primary Jupiter endpoint failed, trying fallback",
                error=str(primary_err),
                fallback_url=fallback_url,
            )
            try:
                data = await _fetch_quote(session, fallback_url, params, settings.JUPITER_TIMEOUT_SEC)
            except Exception as fallback_err:
                raise JupiterQuoteError(
                    f"Jupiter quote failed on both endpoints: primary={primary_err}, fallback={fallback_err}"
                ) from fallback_err
        else:
            if isinstance(primary_err, JupiterQuoteError):
                raise
            raise JupiterQuoteError(f"Jupiter quote request failed: {primary_err}") from primary_err

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
    payload: dict = {
        "quoteResponse": quote.raw_response,
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": True,
    }

    # Dynamic priority fees: let Jupiter auto-detect optimal fee
    if settings.PRIORITY_FEE_AUTO:
        payload["prioritizationFeeLamports"] = "auto"
        payload["dynamicComputeUnitLimit"] = True
    else:
        payload["prioritizationFeeLamports"] = settings.PRIORITY_FEE_LAMPORTS

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

"""Bundle detection — check if early buyers of a token are funded by the same source.

Bundled launches: dev creates multiple wallets, funds them from one source,
buys the token across all wallets in the same block to fake organic demand.

Detection: pull first N swap transactions, extract buyer wallets, check if
multiple buyers were funded by the same parent wallet.
"""

import asyncio
from collections import Counter
from datetime import date

import aiohttp
import structlog

from sniper.config import Settings

logger = structlog.get_logger()

# Daily Helius call limit for bundle detection
_bundle_calls_today = 0
_bundle_reset_date: date | None = None
_BUNDLE_DAILY_LIMIT = 10_000


def _check_bundle_limit() -> bool:
    """Return True if under daily limit, False if exhausted."""
    global _bundle_calls_today, _bundle_reset_date
    today = date.today()
    if _bundle_reset_date != today:
        _bundle_calls_today = 0
        _bundle_reset_date = today
    if _bundle_calls_today >= _BUNDLE_DAILY_LIMIT:
        logger.warning("Sniper Helius bundle daily limit reached", calls=_bundle_calls_today)
        return False
    _bundle_calls_today += 1
    return True


# If X% or more of early buyers share a funding source, it's bundled
BUNDLE_THRESHOLD_PCT = 30  # 30% of early buyers from same source = bundled
MIN_EARLY_BUYERS = 5  # Need at least 5 buyers to check (raised from 3 to reduce false positives)

SOL_MINT = "So11111111111111111111111111111111111111112"

# Known exchange hot wallets — don't count these as "same funder"
KNOWN_EXCHANGES = {
    "9WzDXwBbmPJiHaZgB6Lgr9xnp2YhhDRtAcCRv7A1dBn7",  # Binance
    "2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S",  # Coinbase
    "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS",  # FTX (legacy)
}


async def check_bundle(
    contract_address: str,
    session: aiohttp.ClientSession,
    settings: Settings,
) -> dict:
    """Check if a token's early buyers are bundled (funded by same source).

    Returns:
        {
            "is_bundled": bool,
            "bundle_pct": float,  # % of early buyers from same funder
            "top_funder": str,    # address of the most common funder
            "early_buyers": int,  # number of unique early buyers checked
            "same_block_buyers": int,  # buyers in the same block as first tx
        }
    """
    if not _check_bundle_limit():
        return {
            "is_bundled": False, "bundle_pct": 0.0, "top_funder": "",
            "early_buyers": 0, "same_block_buyers": 0,
        }

    result = {
        "is_bundled": False,
        "bundle_pct": 0.0,
        "top_funder": "",
        "early_buyers": 0,
        "same_block_buyers": 0,
    }

    if not settings.HELIUS_API_KEY:
        return result

    try:
        # Step 1: Get early swap transactions for this token
        url = f"https://api.helius.xyz/v0/addresses/{contract_address}/transactions"
        params = {"api-key": settings.HELIUS_API_KEY, "limit": 20, "type": "SWAP"}

        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return result
            txns = await resp.json()

        if not txns:
            return result

        # Step 2: Extract buyer wallets (fee payers who received the token)
        # Transactions are newest-first, so reverse to get chronological order
        txns.reverse()

        first_timestamp = txns[0].get("timestamp", 0)
        buyer_wallets = []

        for tx in txns:
            ts = tx.get("timestamp", 0)
            fee_payer = tx.get("feePayer", "")

            # Only check buyers in the first 60 seconds (early buyers)
            if ts - first_timestamp > 60:
                break

            # Check if this wallet received the token (= buyer)
            transfers = tx.get("tokenTransfers", [])
            is_buyer = any(
                t.get("toUserAccount") == fee_payer
                and t.get("mint") != SOL_MINT
                for t in transfers
            )
            if is_buyer and fee_payer:
                buyer_wallets.append(fee_payer)

        # Count same-block buyers
        same_block = sum(1 for tx in txns if tx.get("timestamp") == first_timestamp)
        result["same_block_buyers"] = same_block

        unique_buyers = list(set(buyer_wallets))
        result["early_buyers"] = len(unique_buyers)

        if len(unique_buyers) < MIN_EARLY_BUYERS:
            return result

        # Step 3: Check funding sources concurrently (semaphore limits to 3 parallel)
        sem = asyncio.Semaphore(3)

        async def _get_funder(buyer: str) -> str | None:
            async with sem:
                try:
                    funder_url = f"https://api.helius.xyz/v0/addresses/{buyer}/transactions"
                    funder_params = {"api-key": settings.HELIUS_API_KEY, "limit": 5, "type": "TRANSFER"}

                    async with session.get(funder_url, params=funder_params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status != 200:
                            return None
                        funder_txns = await resp.json()

                    # Find who sent SOL to this buyer
                    for ftx in funder_txns:
                        for t in ftx.get("tokenTransfers", []) + ftx.get("nativeTransfers", []):
                            to_addr = t.get("toUserAccount", "")
                            from_addr = t.get("fromUserAccount", "")
                            if to_addr == buyer and from_addr and from_addr != buyer:
                                return from_addr
                    return None
                except Exception:
                    return None

        # Run funder lookups concurrently (cap at 10 buyers)
        funder_tasks = [_get_funder(buyer) for buyer in unique_buyers[:10]]
        funder_results = await asyncio.gather(*funder_tasks)
        funders = [f for f in funder_results if f and f not in KNOWN_EXCHANGES]

        if not funders:
            return result

        # Step 4: Check if multiple buyers share the same funder
        funder_counts = Counter(funders)
        top_funder, top_count = funder_counts.most_common(1)[0]
        bundle_pct = (top_count / len(unique_buyers)) * 100

        result["top_funder"] = top_funder
        result["bundle_pct"] = round(bundle_pct, 1)
        result["is_bundled"] = bundle_pct >= BUNDLE_THRESHOLD_PCT

        return result

    except Exception as e:
        logger.debug("Bundle check failed", error=str(e), token=contract_address)
        return result

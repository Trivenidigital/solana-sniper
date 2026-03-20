"""Anti-rug safety checks using GoPlus API."""

import aiohttp
import structlog

logger = structlog.get_logger()

GOPLUS_URL = "https://api.gopluslabs.io/api/v1/token_security/solana"

# Flags that indicate a risky token
_DANGER_FLAGS = ("is_mintable", "is_honeypot", "can_take_back_ownership", "transfer_pausable")


async def check_token_safety(
    session: aiohttp.ClientSession,
    contract_address: str,
) -> bool:
    """Check token safety via GoPlus API.

    Returns True if the token appears safe, False if risky.
    On API failure, returns True (fail-open) so we don't block trades
    due to third-party downtime.
    """
    try:
        url = f"{GOPLUS_URL}?contract_addresses={contract_address}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                logger.warning(
                    "GoPlus API non-200 response",
                    status=resp.status,
                    token=contract_address,
                )
                return True  # fail-open

            data = await resp.json()
            result = data.get("result", {}).get(contract_address.lower(), {})
            if not result:
                # Try original case as well
                result = data.get("result", {}).get(contract_address, {})
            if not result:
                logger.debug("GoPlus returned no data for token", token=contract_address)
                return True  # no data, fail-open

            for flag in _DANGER_FLAGS:
                value = result.get(flag)
                # GoPlus returns "1" for true, "0" for false
                if value in ("1", 1, True):
                    logger.warning(
                        "Token failed safety check",
                        token=contract_address,
                        flag=flag,
                    )
                    return False

            logger.debug("Token passed safety check", token=contract_address)
            return True

    except Exception:
        logger.warning("GoPlus API call failed", token=contract_address, exc_info=True)
        return True  # fail-open

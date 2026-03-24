"""GODMODE integration — check token bundle stats via GODMODE API.

GODMODE scans top holders of a token, traces their funding sources,
and detects clusters of wallets funded from the same source (bundles).

API: GET /api/db/token/{mint} returns { token, scans[] }
Each scan has: bundle_pct, bundle_wallets, suspicious_clusters, holders_scanned.

Requires auth token from POST /api/db/auth (24h session tokens).
"""

import time

import aiohttp
import structlog

from sniper.config import Settings

logger = structlog.get_logger()

# Module-level token cache — reuse until near expiry
_godmode_session: dict = {"token": None, "expires_at": 0.0}


async def _get_godmode_token(
    session: aiohttp.ClientSession, settings: Settings,
) -> str | None:
    """Get a GODMODE session token, reusing cached if still valid."""
    now = time.monotonic()
    if _godmode_session["token"] and now < _godmode_session["expires_at"]:
        return _godmode_session["token"]

    try:
        async with session.post(
            f"{settings.GODMODE_URL}/api/db/auth",
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                token = data.get("token")
                if token:
                    # Cache for 23 hours (tokens last 24h)
                    _godmode_session["token"] = token
                    _godmode_session["expires_at"] = now + (23 * 3600)
                    return token
    except Exception as e:
        logger.debug("GODMODE auth failed", error=str(e))
    return None


async def check_godmode_bundles(
    token_mint: str,
    settings: Settings,
) -> dict:
    """Check GODMODE DB for existing bundle scan results on a token.

    Returns:
        {
            "clean": bool,           # True if bundle_pct < threshold (safe to buy)
            "bundle_pct": float,     # % of holders in bundle clusters
            "bundled_wallets": int,  # Number of wallets flagged as bundled
            "error": str | None,     # Error message if request failed
        }
    """
    result = {
        "clean": True,
        "bundle_pct": 0.0,
        "bundled_wallets": 0,
        "error": None,
    }

    if not settings.GODMODE_ENABLED:
        return result

    try:
        url = f"{settings.GODMODE_URL}/api/db/token/{token_mint}"

        async with aiohttp.ClientSession() as session:
            token = await _get_godmode_token(session, settings)
            headers = {"x-auth-token": token} if token else {}

            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    result["error"] = f"GODMODE returned {resp.status}"
                    return result

                data = await resp.json()

        # Response: { token: {...} | null, scans: [...] }
        scans = data.get("scans", [])
        if not scans:
            return result

        latest = scans[0]
        bundle_pct = float(latest.get("bundle_pct", 0) or 0)
        bundle_wallets = int(latest.get("bundle_wallets", 0) or 0)

        result["bundle_pct"] = bundle_pct
        result["bundled_wallets"] = bundle_wallets
        result["clean"] = bundle_pct < settings.GODMODE_BUNDLE_THRESHOLD

        return result

    except Exception as e:
        logger.debug("GODMODE check failed", error=str(e), token=token_mint)
        result["error"] = str(e)
        return result


async def trigger_godmode_scan(token_mint: str, settings: Settings) -> None:
    """Trigger a fresh GODMODE scan for a token asynchronously.

    Fire-and-forget — call at signal receipt time so by the time the bot
    reaches the buy check, GODMODE has had a few seconds to scan.
    """
    if not settings.GODMODE_ENABLED:
        return
    try:
        url = f"{settings.GODMODE_URL}/api/db/scan-token"
        async with aiohttp.ClientSession() as session:
            token = await _get_godmode_token(session, settings)
            headers = {"x-auth-token": token} if token else {}

            async with session.post(
                url,
                json={"mint": token_mint},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    logger.debug("GODMODE scan triggered", token=token_mint)
                else:
                    logger.debug("GODMODE scan trigger failed", status=resp.status, token=token_mint)
    except Exception:
        pass  # fire and forget

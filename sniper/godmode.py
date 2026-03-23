"""GODMODE integration — check token bundle stats via GODMODE API.

GODMODE scans top holders of a token, traces their funding sources,
and detects clusters of wallets funded from the same source (bundles).

API: GET /api/db/token/{mint} returns { token, scans[] }
Each scan has: bundle_pct, bundle_wallets, suspicious_clusters, holders_scanned.
"""

import aiohttp
import structlog

from sniper.config import Settings

logger = structlog.get_logger()


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
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    result["error"] = f"GODMODE returned {resp.status}"
                    return result

                data = await resp.json()

        # Response: { token: {...} | null, scans: [...] }
        scans = data.get("scans", [])
        if not scans:
            # No scan data for this token — treat as clean (no info)
            return result

        # Use the most recent scan (first in list, ordered by scanned_at DESC)
        latest = scans[0]
        bundle_pct = float(latest.get("bundle_pct", 0) or 0)
        bundle_wallets = int(latest.get("bundle_wallets", 0) or 0)
        suspicious_clusters = int(latest.get("suspicious_clusters", 0) or 0)

        result["bundle_pct"] = bundle_pct
        result["bundled_wallets"] = bundle_wallets
        result["clean"] = bundle_pct < settings.GODMODE_BUNDLE_THRESHOLD

        return result

    except Exception as e:
        # Fail open — don't block buys if GODMODE is unreachable
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
            async with session.post(
                url,
                json={"mint": token_mint},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    logger.debug("GODMODE scan triggered", token=token_mint)
                else:
                    logger.debug("GODMODE scan trigger failed", status=resp.status, token=token_mint)
    except Exception:
        pass  # fire and forget

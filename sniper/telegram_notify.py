"""Telegram notification support for the sniper bot."""

import aiohttp
import structlog

from sniper.config import Settings

logger = structlog.get_logger()


async def send_telegram(message: str, settings: Settings) -> None:
    """Send a notification message via Telegram bot API.

    Silently skips if TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not configured.
    """
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": settings.TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Telegram send failed", status=resp.status, body=body)
                else:
                    logger.debug("Telegram notification sent")
    except Exception as e:
        # Never let Telegram failures break the bot
        logger.warning("Telegram notification error", error=str(e))

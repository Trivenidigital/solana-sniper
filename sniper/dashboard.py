"""Monitoring dashboard — periodic portfolio health logging."""

import asyncio
from datetime import datetime, timezone

import structlog

from sniper.db import Database

logger = structlog.get_logger()


async def print_dashboard(db: Database) -> dict:
    """Print a formatted dashboard of current portfolio state.

    Returns the dashboard data dict for programmatic use.
    """
    open_positions = await db.get_open_positions()
    exposure = await db.get_total_exposure_sol()
    realized_pnl = await db.get_realized_pnl()
    now = datetime.now(timezone.utc)

    dashboard = {
        "timestamp": now.isoformat(),
        "open_positions": len(open_positions),
        "total_exposure_sol": round(exposure, 4),
        "realized_pnl_sol": round(realized_pnl, 4),
        "positions": [],
    }

    for pos in open_positions:
        age_seconds = (now - pos.opened_at).total_seconds()
        age_minutes = round(age_seconds / 60, 1)
        dashboard["positions"].append({
            "token": pos.token_name,
            "ticker": pos.ticker,
            "entry_sol": pos.entry_sol,
            "age_minutes": age_minutes,
            "paper": pos.paper,
        })

    logger.info(
        "=== DASHBOARD ===",
        open=dashboard["open_positions"],
        exposure_sol=dashboard["total_exposure_sol"],
        realized_pnl_sol=dashboard["realized_pnl_sol"],
    )

    for p in dashboard["positions"]:
        logger.info(
            "  Position",
            token=p["token"],
            ticker=p["ticker"],
            entry_sol=p["entry_sol"],
            age_min=p["age_minutes"],
            paper=p["paper"],
        )

    return dashboard


async def run_dashboard_loop(db: Database, interval_seconds: int = 60) -> None:
    """Run the dashboard on a loop. Intended to be used as a background task."""
    while True:
        try:
            await print_dashboard(db)
        except Exception as e:
            logger.error("Dashboard error", error=str(e))
        await asyncio.sleep(interval_seconds)

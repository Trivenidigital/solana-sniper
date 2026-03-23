"""Data models for the sniper bot."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


class Signal(BaseModel):
    """A buy signal read from coinpump-scout's database."""

    contract_address: str
    chain: str
    token_name: str
    ticker: str
    conviction_score: float
    market_cap_usd: float = 0
    liquidity_usd: float = 0
    volume_24h_usd: float = 0
    alerted_at: datetime
    token_age_days: float = 0
    quant_score: float | None = 0
    top3_wallet_concentration: float = 0
    holder_count: int = 0


class Position(BaseModel):
    """An open or closed trading position."""

    id: int | None = None
    contract_address: str
    token_name: str
    ticker: str
    entry_sol: float
    entry_token_amount: float
    entry_price_usd: float = 0
    entry_tx: str | None = None
    exit_sol: float | None = None
    exit_price_usd: float | None = None
    exit_tx: str | None = None
    exit_reason: Literal[
        "stop_loss", "take_profit", "trailing_stop", "manual",
        "rug_detected", "momentum_lost", "pump_window_expired", "max_hold_exceeded",
        "sell_pressure", "unsellable", "breakeven_stop",
    ] | None = None
    status: Literal["open", "closed"] = "open"
    pnl_sol: float | None = None
    pnl_pct: float | None = None
    opened_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: datetime | None = None
    paper: bool = True
    peak_value_sol: float | None = None
    trailing_active: bool = False
    partial_exit_done: bool = False
    partial_exit_tier: int = 0
    sell_fail_count: int = 0
    dca_completed: int = 0
    decimals: int | None = None


class JupiterQuote(BaseModel):
    """Response from Jupiter V6 quote API."""

    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    price_impact_pct: float = 0
    raw_response: dict

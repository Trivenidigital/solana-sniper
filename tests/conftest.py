"""Shared test fixtures."""

import pytest

from sniper.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        SCOUT_DB_PATH=":memory:",
        SNIPER_DB_PATH=":memory:",
        PAPER_MODE=True,
        MAX_BUY_SOL=0.1,
        MAX_PORTFOLIO_SOL=1.0,
        MAX_OPEN_POSITIONS=5,
    )

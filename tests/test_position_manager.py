"""Tests for position manager TP/SL logic."""

from unittest.mock import AsyncMock, patch

import pytest

from sniper.config import Settings
from sniper.db import Database
from sniper.models import Position
from sniper.position_manager import check_positions


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


@pytest.fixture
def settings():
    return Settings(STOP_LOSS_PCT=25.0, TAKE_PROFIT_PCT=100.0, PAPER_MODE=True)


async def test_stop_loss_triggers(db, settings):
    pos = Position(
        contract_address="Token1", token_name="Loser", ticker="L",
        entry_sol=0.1, entry_token_amount=1000, paper=True,
    )
    await db.open_position(pos)

    with patch("sniper.position_manager.get_current_value_sol", new_callable=AsyncMock, return_value=0.05), \
         patch("sniper.position_manager.execute_sell", new_callable=AsyncMock, return_value=("paper-sell-x", 0.05)):
        actions = await check_positions(db, AsyncMock(), AsyncMock(), AsyncMock(), settings)

    assert len(actions) == 1
    assert "STOP_LOSS" in actions[0]
    assert await db.count_open_positions() == 0


async def test_take_profit_triggers(db, settings):
    pos = Position(
        contract_address="Token2", token_name="Winner", ticker="W",
        entry_sol=0.1, entry_token_amount=1000, paper=True,
    )
    await db.open_position(pos)

    with patch("sniper.position_manager.get_current_value_sol", new_callable=AsyncMock, return_value=0.25), \
         patch("sniper.position_manager.execute_sell", new_callable=AsyncMock, return_value=("paper-sell-y", 0.25)):
        actions = await check_positions(db, AsyncMock(), AsyncMock(), AsyncMock(), settings)

    assert len(actions) == 1
    assert "TAKE_PROFIT" in actions[0]
    assert await db.count_open_positions() == 0


async def test_no_action_within_bounds(db, settings):
    pos = Position(
        contract_address="Token3", token_name="Stable", ticker="S",
        entry_sol=0.1, entry_token_amount=1000, paper=True,
    )
    await db.open_position(pos)

    with patch("sniper.position_manager.get_current_value_sol", new_callable=AsyncMock, return_value=0.12):
        actions = await check_positions(db, AsyncMock(), AsyncMock(), AsyncMock(), settings)

    assert len(actions) == 0
    assert await db.count_open_positions() == 1

"""Tests for the monitoring dashboard."""

import pytest

from sniper.dashboard import print_dashboard
from sniper.db import Database
from sniper.models import Position


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


async def test_dashboard_empty(db):
    result = await print_dashboard(db)
    assert result["open_positions"] == 0
    assert result["total_exposure_sol"] == 0.0
    assert result["realized_pnl_sol"] == 0.0
    assert result["positions"] == []


async def test_dashboard_with_positions(db):
    pos = Position(
        contract_address="Token1", token_name="TestCoin", ticker="TST",
        entry_sol=0.1, entry_token_amount=1000, paper=True,
    )
    await db.open_position(pos)

    result = await print_dashboard(db)
    assert result["open_positions"] == 1
    assert result["total_exposure_sol"] == 0.1
    assert len(result["positions"]) == 1
    assert result["positions"][0]["token"] == "TestCoin"
    assert result["positions"][0]["ticker"] == "TST"

"""Tests for sniper database."""

from datetime import datetime, timezone

import pytest

from sniper.db import Database
from sniper.models import Position


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


def _make_position(**overrides) -> Position:
    defaults = dict(
        contract_address="TokenMint123",
        token_name="TestCoin",
        ticker="TST",
        entry_sol=0.1,
        entry_token_amount=1000.0,
        entry_price_usd=50000,
        entry_tx="paper-buy-abc123",
        paper=True,
    )
    defaults.update(overrides)
    return Position(**defaults)


async def test_open_and_get_position(db):
    pos = _make_position()
    pos_id = await db.open_position(pos)
    assert pos_id is not None

    positions = await db.get_open_positions()
    assert len(positions) == 1
    assert positions[0].contract_address == "TokenMint123"
    assert positions[0].status == "open"


async def test_close_position(db):
    pos = _make_position()
    pos_id = await db.open_position(pos)

    await db.close_position(
        pos_id, exit_sol=0.15, exit_price_usd=0,
        exit_tx="paper-sell-xyz", exit_reason="take_profit",
        pnl_sol=0.05, pnl_pct=50.0,
    )

    open_positions = await db.get_open_positions()
    assert len(open_positions) == 0


async def test_count_and_exposure(db):
    await db.open_position(_make_position(entry_sol=0.1))
    await db.open_position(_make_position(contract_address="Token2", entry_sol=0.2))

    assert await db.count_open_positions() == 2
    assert await db.get_total_exposure_sol() == pytest.approx(0.3)


async def test_get_position_by_address(db):
    await db.open_position(_make_position())
    pos = await db.get_open_position_by_address("TokenMint123")
    assert pos is not None
    assert pos.token_name == "TestCoin"

    missing = await db.get_open_position_by_address("NonExistent")
    assert missing is None


async def test_realized_pnl(db):
    pos_id = await db.open_position(_make_position(entry_sol=0.1))
    await db.close_position(
        pos_id, exit_sol=0.15, exit_price_usd=0,
        exit_tx="tx", exit_reason="take_profit",
        pnl_sol=0.05, pnl_pct=50.0,
    )
    assert await db.get_realized_pnl() == pytest.approx(0.05)


async def test_log_trade(db):
    pos_id = await db.open_position(_make_position())
    await db.log_trade(pos_id, "buy", 0.1, 1000.0, "paper-tx", None)
    # No assertion needed — just verify it doesn't crash

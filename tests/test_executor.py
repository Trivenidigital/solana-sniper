"""Tests for trade execution (paper mode)."""

import re
from unittest.mock import AsyncMock

import pytest
import aiohttp
from aioresponses import aioresponses

from sniper.config import Settings
from sniper.executor import execute_buy, execute_sell, get_current_value_sol
from sniper.jupiter import SOL_MINT


@pytest.fixture
def settings():
    return Settings(PAPER_MODE=True)


@pytest.fixture
def mock_aiohttp():
    with aioresponses() as m:
        yield m


QUOTE_URL = re.compile(r"https://quote-api\.jup\.ag/v6/quote.*")


def _mock_quote(mock, out_amount="500000000", price_impact="0.5"):
    mock.get(QUOTE_URL, payload={
        "inputMint": SOL_MINT,
        "outputMint": "TokenMint123",
        "inAmount": "100000000",
        "outAmount": out_amount,
        "priceImpactPct": price_impact,
        "routePlan": [],
    })


async def test_paper_buy(mock_aiohttp, settings):
    _mock_quote(mock_aiohttp)

    async with aiohttp.ClientSession() as session:
        tx_sig, tokens = await execute_buy(
            AsyncMock(), AsyncMock(), session,
            "TokenMint123", 0.1, settings,
        )

    assert tx_sig.startswith("paper-buy-")
    assert tokens == 500000000.0


async def test_paper_sell(mock_aiohttp, settings):
    mock_aiohttp.get(QUOTE_URL, payload={
        "inputMint": "TokenMint123",
        "outputMint": SOL_MINT,
        "inAmount": "500000000",
        "outAmount": "100000000",
        "priceImpactPct": "0.3",
        "routePlan": [],
    })

    async with aiohttp.ClientSession() as session:
        tx_sig, sol_received = await execute_sell(
            AsyncMock(), AsyncMock(), session,
            "TokenMint123", 500000000, settings,
        )

    assert tx_sig.startswith("paper-sell-")
    assert sol_received == pytest.approx(0.1)


async def test_get_current_value_sol(mock_aiohttp, settings):
    mock_aiohttp.get(QUOTE_URL, payload={
        "inputMint": "TokenMint123",
        "outputMint": SOL_MINT,
        "inAmount": "1000",
        "outAmount": "200000000",
        "priceImpactPct": "0.1",
        "routePlan": [],
    })

    async with aiohttp.ClientSession() as session:
        value = await get_current_value_sol(session, "TokenMint123", 1000, settings)

    assert value == pytest.approx(0.2)


async def test_get_current_value_sol_zero_amount(settings):
    async with aiohttp.ClientSession() as session:
        value = await get_current_value_sol(session, "TokenMint123", 0, settings)
    assert value == 0.0


async def test_get_current_value_sol_returns_none_on_failure(mock_aiohttp, settings):
    mock_aiohttp.get(QUOTE_URL, status=500, body="error")

    async with aiohttp.ClientSession() as session:
        value = await get_current_value_sol(session, "TokenMint123", 1000, settings)

    assert value is None

"""Tests for Jupiter V6 client."""

import re

import pytest
import aiohttp
from aioresponses import aioresponses

from sniper.config import Settings
from sniper.jupiter import get_quote, SOL_MINT
from sniper.exceptions import JupiterQuoteError


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def mock_aiohttp():
    with aioresponses() as m:
        yield m


QUOTE_URL_PATTERN = re.compile(r"https://quote-api\.jup\.ag/v6/quote.*")


async def test_get_quote_success(mock_aiohttp, settings):
    mock_aiohttp.get(
        QUOTE_URL_PATTERN,
        payload={
            "inputMint": SOL_MINT,
            "outputMint": "TokenMint123",
            "inAmount": "100000000",
            "outAmount": "500000000",
            "priceImpactPct": "0.5",
            "routePlan": [],
        },
    )

    async with aiohttp.ClientSession() as session:
        quote = await get_quote(
            session, SOL_MINT, "TokenMint123", 100000000, settings,
        )

    assert quote.in_amount == 100000000
    assert quote.out_amount == 500000000
    assert quote.price_impact_pct == 0.5


async def test_get_quote_high_price_impact_rejected(mock_aiohttp, settings):
    mock_aiohttp.get(
        QUOTE_URL_PATTERN,
        payload={
            "inputMint": SOL_MINT,
            "outputMint": "TokenMint123",
            "inAmount": "100000000",
            "outAmount": "500000000",
            "priceImpactPct": "8.5",
            "routePlan": [],
        },
    )

    async with aiohttp.ClientSession() as session:
        with pytest.raises(JupiterQuoteError, match="Price impact too high"):
            await get_quote(session, SOL_MINT, "TokenMint123", 100000000, settings)


async def test_get_quote_api_error(mock_aiohttp, settings):
    mock_aiohttp.get(QUOTE_URL_PATTERN, status=500, body="Server Error")

    async with aiohttp.ClientSession() as session:
        with pytest.raises(JupiterQuoteError):
            await get_quote(session, SOL_MINT, "TokenMint123", 100000000, settings)

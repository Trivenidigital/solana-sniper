"""Tests for bundle detection."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from sniper.bundle_check import check_bundle, BUNDLE_THRESHOLD_PCT, MIN_EARLY_BUYERS
from sniper.config import Settings


def _settings(**overrides):
    defaults = dict(
        SOLANA_RPC_URL="http://localhost",
        KEYPAIR_PATH="/tmp/test.json",
        SCOUT_DB_PATH="/tmp/scout.db",
        SNIPER_DB_PATH="/tmp/sniper.db",
        HELIUS_API_KEY="test-key",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _make_swap_tx(timestamp, fee_payer, token_mint="token1"):
    return {
        "timestamp": timestamp,
        "feePayer": fee_payer,
        "tokenTransfers": [
            {"toUserAccount": fee_payer, "mint": token_mint}
        ],
        "nativeTransfers": [],
    }


def _make_transfer_tx(from_addr, to_addr):
    return {
        "tokenTransfers": [],
        "nativeTransfers": [
            {"fromUserAccount": from_addr, "toUserAccount": to_addr}
        ],
    }


@pytest.mark.asyncio
async def test_no_helius_key_returns_not_bundled():
    settings = _settings(HELIUS_API_KEY="")
    session = AsyncMock()
    result = await check_bundle("mint1", session, settings)
    assert result["is_bundled"] is False


@pytest.mark.asyncio
async def test_no_transactions_returns_not_bundled():
    settings = _settings()
    resp = MagicMock()
    resp.status = 200
    resp.json = AsyncMock(return_value=[])
    session = MagicMock()
    session.get = MagicMock(return_value=MagicMock(
        __aenter__=AsyncMock(return_value=resp),
        __aexit__=AsyncMock(return_value=False),
    ))
    result = await check_bundle("mint1", session, settings)
    assert result["is_bundled"] is False
    assert result["early_buyers"] == 0


@pytest.mark.asyncio
async def test_too_few_buyers_returns_not_bundled():
    settings = _settings()
    txns = [_make_swap_tx(1000, f"buyer{i}") for i in range(MIN_EARLY_BUYERS - 1)]
    resp = MagicMock()
    resp.status = 200
    resp.json = AsyncMock(return_value=txns)
    session = MagicMock()
    session.get = MagicMock(return_value=MagicMock(
        __aenter__=AsyncMock(return_value=resp),
        __aexit__=AsyncMock(return_value=False),
    ))
    result = await check_bundle("token1", session, settings)
    assert result["is_bundled"] is False


@pytest.mark.asyncio
async def test_bundled_same_funder():
    """All early buyers funded by same wallet → bundled."""
    settings = _settings()

    swap_txns = [_make_swap_tx(1000, f"buyer{i}") for i in range(5)]

    def mock_get(url, **kwargs):
        resp = MagicMock()
        resp.status = 200
        params = kwargs.get("params", {})
        if params.get("type") == "SWAP":
            resp.json = AsyncMock(return_value=swap_txns)
        else:
            # All buyers funded by same "master_funder"
            # Extract buyer from URL
            buyer = url.split("/addresses/")[1].split("/")[0] if "/addresses/" in url else ""
            resp.json = AsyncMock(return_value=[
                _make_transfer_tx("master_funder", buyer)
            ])
        return MagicMock(
            __aenter__=AsyncMock(return_value=resp),
            __aexit__=AsyncMock(return_value=False),
        )

    session = MagicMock()
    session.get = mock_get
    result = await check_bundle("token1", session, settings)
    assert result["is_bundled"] is True
    assert result["bundle_pct"] >= BUNDLE_THRESHOLD_PCT
    assert result["top_funder"] == "master_funder"


@pytest.mark.asyncio
async def test_not_bundled_different_funders():
    """Each buyer funded by different wallet → not bundled."""
    settings = _settings()

    swap_txns = [_make_swap_tx(1000, f"buyer{i}") for i in range(5)]

    def mock_get(url, **kwargs):
        resp = MagicMock()
        resp.status = 200
        params = kwargs.get("params", {})
        if params.get("type") == "SWAP":
            resp.json = AsyncMock(return_value=swap_txns)
        else:
            # Each buyer has a unique funder
            buyer = url.split("/addresses/")[1].split("/")[0] if "/addresses/" in url else ""
            resp.json = AsyncMock(return_value=[
                _make_transfer_tx(f"unique_funder_for_{buyer}", buyer)
            ])
        return MagicMock(
            __aenter__=AsyncMock(return_value=resp),
            __aexit__=AsyncMock(return_value=False),
        )

    session = MagicMock()
    session.get = mock_get
    result = await check_bundle("token1", session, settings)
    assert result["is_bundled"] is False

"""Tests for configuration."""

import os
from pathlib import Path
from unittest.mock import patch

from sniper.config import Settings


def test_settings_defaults():
    # Override env vars so .env file doesn't interfere
    with patch.dict(os.environ, {"MAX_BUY_SOL": "0.1"}, clear=False):
        s = Settings()
    assert s.PAPER_MODE is True
    assert s.MAX_BUY_SOL == 0.1
    assert s.MAX_PORTFOLIO_SOL == 1.0
    assert s.MAX_OPEN_POSITIONS == 5
    assert s.STOP_LOSS_PCT == 25.0
    assert s.TAKE_PROFIT_PCT == 100.0
    assert s.SLIPPAGE_BPS == 300
    assert s.SOLANA_RPC_URL == "https://api.mainnet-beta.solana.com"
    assert s.KEYPAIR_PATH == Path("wallet.json")


def test_settings_custom():
    s = Settings(MAX_BUY_SOL=0.5, STOP_LOSS_PCT=10.0, PAPER_MODE=False)
    assert s.MAX_BUY_SOL == 0.5
    assert s.STOP_LOSS_PCT == 10.0
    assert s.PAPER_MODE is False

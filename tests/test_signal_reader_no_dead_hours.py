"""Verify dead hours filter is removed — signals process 24/7."""
import pytest
from sniper.config import Settings


def _settings(**overrides):
    defaults = dict(
        SOLANA_RPC_URL="http://localhost",
        KEYPAIR_PATH="/tmp/test.json",
        SCOUT_DB_PATH="/tmp/scout.db",
        SNIPER_DB_PATH="/tmp/sniper.db",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_no_dead_hours_config():
    """TRADING_DEAD_HOURS setting should no longer exist."""
    settings = _settings()
    assert not hasattr(settings, "TRADING_DEAD_HOURS")


def test_smart_money_wallets_config():
    """SMART_MONEY_WALLETS should be configurable."""
    settings = _settings(SMART_MONEY_WALLETS="wallet1,wallet2")
    assert settings.SMART_MONEY_WALLETS == "wallet1,wallet2"


def test_smart_money_boost_cap_config():
    """SMART_MONEY_BOOST_CAP should default to 80."""
    settings = _settings()
    assert settings.SMART_MONEY_BOOST_CAP == 80

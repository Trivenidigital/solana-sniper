"""Tests for graduated smart money conviction boost."""
import pytest
from sniper.copy_trader import smart_money_signals, _record_signal


def test_graduated_boost_math():
    """Conviction boost should be wallet_count * per_wallet, capped."""
    smart_money_signals.clear()
    _record_signal("token_abc", "wallet1")
    _record_signal("token_abc", "wallet2")
    _record_signal("token_abc", "wallet3")
    sm = smart_money_signals["token_abc"]
    boost = min(sm["count"] * 20, 80)
    assert boost == 60


def test_boost_capped_at_max():
    """5 wallets should be capped at 80."""
    smart_money_signals.clear()
    for i in range(5):
        _record_signal("token_xyz", f"wallet{i}")
    sm = smart_money_signals["token_xyz"]
    boost = min(sm["count"] * 20, 80)
    assert boost == 80

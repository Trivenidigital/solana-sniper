"""Tests for position manager time-based phase exit logic."""

from datetime import datetime, timedelta, timezone
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
    return Settings(
        PAPER_MODE=True,
        # Phase thresholds (defaults, explicit for clarity)
        PROTECTION_WINDOW_MIN=10,
        MOMENTUM_CHECK_MIN=30,
        MAX_HOLD_MIN=60,
        RUG_DETECT_PCT=50.0,
        MOMENTUM_LOSS_PCT=10.0,
        PUMP_WINDOW_MIN_GAIN_PCT=20.0,
        TRAILING_ACTIVATE_PCT=30.0,
        PHASE4_TRAILING_MIN_PNL=50.0,
        TRAILING_TIER1_PCT=20.0,
        TRAILING_TIER2_PCT=15.0,
        TRAILING_TIER3_PCT=10.0,
        COOLDOWN_HOURS=6,
        MAX_BUY_SOL=0.1,
    )


def _make_position(
    age_minutes: float = 5,
    entry_sol: float = 0.1,
    entry_token_amount: float = 1000,
    trailing_active: bool = False,
    peak_value_sol: float | None = None,
    partial_exit_tier: int = 0,
) -> Position:
    """Create a Position with a given age in minutes."""
    return Position(
        contract_address="TestToken111",
        token_name="TestToken",
        ticker="TT",
        entry_sol=entry_sol,
        entry_token_amount=entry_token_amount,
        paper=True,
        opened_at=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
        trailing_active=trailing_active,
        peak_value_sol=peak_value_sol,
        partial_exit_tier=partial_exit_tier,
    )


async def _run_check(db, settings, current_value, sell_return=None):
    """Run check_positions with mocked price and sell."""
    if sell_return is None:
        sell_return = ("paper-sell-x", current_value)
    with patch("sniper.position_manager.get_current_value_sol", new_callable=AsyncMock, return_value=current_value), \
         patch("sniper.position_manager.execute_sell", new_callable=AsyncMock, return_value=sell_return), \
         patch("sniper.position_manager.send_telegram", new_callable=AsyncMock):
        actions = await check_positions(db, AsyncMock(), AsyncMock(), AsyncMock(), settings)
    return actions


# ======================================================================
# Phase 1: Protection (0-10 min)
# ======================================================================

class TestPhase1Protection:
    async def test_phase1_no_exit_at_minus_15pct(self, db, settings):
        """Phase 1: -15% loss should hold (below rug threshold of -50%)."""
        pos = _make_position(age_minutes=5, entry_sol=0.1)
        await db.open_position(pos)

        # 0.085 SOL = -15% loss
        actions = await _run_check(db, settings, 0.085)

        assert len(actions) == 0
        assert await db.count_open_positions() == 1

    async def test_phase1_rug_detected_at_minus_50pct(self, db, settings):
        """Phase 1: -50% or worse should exit with rug_detected."""
        pos = _make_position(age_minutes=5, entry_sol=0.1)
        await db.open_position(pos)

        # 0.04 SOL = -60% loss (below -50% rug threshold)
        actions = await _run_check(db, settings, 0.04)

        assert len(actions) == 1
        assert "RUG_DETECTED" in actions[0]
        assert await db.count_open_positions() == 0

    async def test_phase1_no_action_when_profitable(self, db, settings):
        """Phase 1: profitable position should hold."""
        pos = _make_position(age_minutes=5, entry_sol=0.1)
        await db.open_position(pos)

        # 0.15 SOL = +50% gain
        actions = await _run_check(db, settings, 0.15)

        assert len(actions) == 0
        assert await db.count_open_positions() == 1


# ======================================================================
# Phase 2: Momentum (10-30 min)
# ======================================================================

class TestPhase2Momentum:
    async def test_phase2_momentum_lost_at_minus_10pct(self, db, settings):
        """Phase 2: -10% or worse should exit with momentum_lost."""
        pos = _make_position(age_minutes=15, entry_sol=0.1)
        await db.open_position(pos)

        # 0.08 SOL = -20% loss (below -10% momentum threshold)
        actions = await _run_check(db, settings, 0.08)

        assert len(actions) == 1
        assert "MOMENTUM_LOST" in actions[0]
        assert await db.count_open_positions() == 0

    async def test_phase2_trailing_activated_at_plus_30pct(self, db, settings):
        """Phase 2: +30% gain should activate trailing stop."""
        pos = _make_position(age_minutes=15, entry_sol=0.1)
        pos_id = await db.open_position(pos)

        # 0.13 SOL = +30% gain (at TRAILING_ACTIVATE_PCT)
        actions = await _run_check(db, settings, 0.13)

        # No close action, just trailing activated
        assert len(actions) == 0
        assert await db.count_open_positions() == 1

        # Verify trailing was activated in DB
        updated = await db.get_open_position_by_address("TestToken111")
        assert updated is not None
        assert updated.trailing_active is True

    async def test_phase2_holds_when_flat(self, db, settings):
        """Phase 2: flat/slightly positive should hold."""
        pos = _make_position(age_minutes=15, entry_sol=0.1)
        await db.open_position(pos)

        # 0.10 SOL = 0% (flat)
        actions = await _run_check(db, settings, 0.10)

        assert len(actions) == 0
        assert await db.count_open_positions() == 1


# ======================================================================
# Phase 3: Pump window (30-60 min)
# ======================================================================

class TestPhase3PumpWindow:
    async def test_phase3_closes_if_not_up_20pct(self, db, settings):
        """Phase 3: below +20% gain without trailing should exit."""
        pos = _make_position(age_minutes=45, entry_sol=0.1)
        await db.open_position(pos)

        # 0.115 SOL = +15% (below 20% pump window threshold)
        actions = await _run_check(db, settings, 0.115)

        assert len(actions) == 1
        assert "PUMP_WINDOW_EXPIRED" in actions[0]
        assert await db.count_open_positions() == 0

    async def test_phase3_holds_if_up_25pct(self, db, settings):
        """Phase 3: +25% gain should hold (above pump window threshold)."""
        pos = _make_position(age_minutes=45, entry_sol=0.1)
        await db.open_position(pos)

        # 0.125 SOL = +25% gain (above 20% threshold)
        actions = await _run_check(db, settings, 0.125)

        assert len(actions) == 0
        assert await db.count_open_positions() == 1

    async def test_phase3_trailing_active_continues(self, db, settings):
        """Phase 3: with trailing active, should hold even if below 20%."""
        pos = _make_position(
            age_minutes=45, entry_sol=0.1,
            trailing_active=True, peak_value_sol=0.15,
        )
        pos_id = await db.open_position(pos)
        # Manually set trailing in DB since open_position doesn't persist trailing_active
        await db.set_trailing_active(pos_id)
        await db.update_peak_value(pos_id, 0.15)

        # 0.14 SOL = +40% gain, peak was 0.15 so drop_from_peak = 6.7% (below 20% trail)
        actions = await _run_check(db, settings, 0.14)

        assert len(actions) == 0
        assert await db.count_open_positions() == 1


# ======================================================================
# Phase 4: Cleanup (60+ min)
# ======================================================================

class TestPhase4Cleanup:
    async def test_phase4_force_closes(self, db, settings):
        """Phase 4: should force close without trailing."""
        pos = _make_position(age_minutes=65, entry_sol=0.1)
        await db.open_position(pos)

        # 0.12 SOL = +20% (not enough for phase 4 trailing exemption)
        actions = await _run_check(db, settings, 0.12)

        assert len(actions) == 1
        assert "MAX_HOLD_EXCEEDED" in actions[0]
        assert await db.count_open_positions() == 0

    async def test_phase4_keeps_trailing_above_50pct(self, db, settings):
        """Phase 4: trailing active with >50% PnL should hold."""
        pos = _make_position(
            age_minutes=65, entry_sol=0.1,
            trailing_active=True, peak_value_sol=0.18,
        )
        pos_id = await db.open_position(pos)
        await db.set_trailing_active(pos_id)
        await db.update_peak_value(pos_id, 0.18)

        # 0.16 SOL = +60% PnL (above PHASE4_TRAILING_MIN_PNL=50%)
        # drop_from_peak = (0.18-0.16)/0.18 = 11.1% (below tier1 trail 20%)
        actions = await _run_check(db, settings, 0.16)

        assert len(actions) == 0
        assert await db.count_open_positions() == 1

    async def test_phase4_closes_trailing_below_50pct(self, db, settings):
        """Phase 4: trailing active but PnL below 50% should force close."""
        pos = _make_position(
            age_minutes=65, entry_sol=0.1,
            trailing_active=True, peak_value_sol=0.15,
        )
        pos_id = await db.open_position(pos)
        await db.set_trailing_active(pos_id)
        await db.update_peak_value(pos_id, 0.15)

        # 0.13 SOL = +30% PnL (below PHASE4_TRAILING_MIN_PNL=50%)
        # Even with trailing active, phase 4 forces close
        actions = await _run_check(db, settings, 0.13)

        assert len(actions) == 1
        assert "MAX_HOLD_EXCEEDED" in actions[0]
        assert await db.count_open_positions() == 0


# ======================================================================
# Trailing tiers
# ======================================================================

class TestTrailingTiers:
    async def test_trailing_tier1_20pct_trail(self, db, settings):
        """Tier 1 (peak <100%): should close when drop_from_peak >= 20%."""
        # Peak gain ~50% (peak_value_sol=0.15 on 0.1 entry)
        pos = _make_position(
            age_minutes=20, entry_sol=0.1,
            trailing_active=True, peak_value_sol=0.15,
        )
        pos_id = await db.open_position(pos)
        await db.set_trailing_active(pos_id)
        await db.update_peak_value(pos_id, 0.15)

        # drop from peak: (0.15 - 0.11) / 0.15 = 26.7% >= 20%
        actions = await _run_check(db, settings, 0.11)

        assert len(actions) == 1
        assert "TRAILING_STOP" in actions[0]
        assert await db.count_open_positions() == 0

    async def test_trailing_tier2_partial_sell_50pct(self, db, settings):
        """Tier 2 (peak >100%): should trigger partial sell of 50%."""
        # Peak gain = 120% (peak_value_sol=0.22 on 0.1 entry)
        pos = _make_position(
            age_minutes=20, entry_sol=0.1, entry_token_amount=1000,
            trailing_active=True, peak_value_sol=0.22,
            partial_exit_tier=0,
        )
        pos_id = await db.open_position(pos)
        await db.set_trailing_active(pos_id)
        await db.update_peak_value(pos_id, 0.22)

        # Current value still near peak (small drop, below trail %)
        # 0.21 SOL = drop_from_peak = (0.22-0.21)/0.22 = 4.5% (below 15% tier2 trail)
        # But peak_pnl > 100% so partial sell tier 1 should fire
        with patch("sniper.position_manager.get_current_value_sol", new_callable=AsyncMock, return_value=0.21), \
             patch("sniper.position_manager.execute_sell", new_callable=AsyncMock, return_value=("paper-sell-partial", 0.05)), \
             patch("sniper.position_manager.send_telegram", new_callable=AsyncMock):
            actions = await check_positions(db, AsyncMock(), AsyncMock(), AsyncMock(), settings)

        # Should have a partial sell action (tier 1 at 100%+ peak)
        assert any("PARTIAL_SELL" in a for a in actions)
        # Position should still be open
        assert await db.count_open_positions() == 1

    async def test_trailing_tier3_partial_sell_75pct(self, db, settings):
        """Tier 3 (peak >200%): should trigger tier 2 partial sell."""
        # Peak gain = 220% (peak_value_sol=0.32 on 0.1 entry)
        pos = _make_position(
            age_minutes=20, entry_sol=0.1, entry_token_amount=1000,
            trailing_active=True, peak_value_sol=0.32,
            partial_exit_tier=1,  # Already did tier 1
        )
        pos_id = await db.open_position(pos)
        await db.set_trailing_active(pos_id)
        await db.update_peak_value(pos_id, 0.32)
        # Mark partial_exit_tier=1 in DB
        await db.update_partial_exit(pos_id, 0.05, 500, 1)

        # 0.30 SOL, drop_from_peak = (0.32-0.30)/0.32 = 6.25% (below 10% tier3 trail)
        with patch("sniper.position_manager.get_current_value_sol", new_callable=AsyncMock, return_value=0.30), \
             patch("sniper.position_manager.execute_sell", new_callable=AsyncMock, return_value=("paper-sell-partial2", 0.03)), \
             patch("sniper.position_manager.send_telegram", new_callable=AsyncMock):
            actions = await check_positions(db, AsyncMock(), AsyncMock(), AsyncMock(), settings)

        # Should have tier 2 partial sell action
        assert any("PARTIAL_SELL(T2)" in a for a in actions)
        assert await db.count_open_positions() == 1


# ======================================================================
# Partial sell PnL
# ======================================================================

class TestPartialSellPnl:
    async def test_partial_sell_adjusts_entry_sol(self, db, settings):
        """After partial sell, entry_sol should decrease proportionally."""
        pos = _make_position(
            age_minutes=20, entry_sol=0.1, entry_token_amount=1000,
            trailing_active=True, peak_value_sol=0.22,
            partial_exit_tier=0,
        )
        pos_id = await db.open_position(pos)
        await db.set_trailing_active(pos_id)
        await db.update_peak_value(pos_id, 0.22)

        # Trigger partial sell (tier 1 at peak_pnl > 100%)
        with patch("sniper.position_manager.get_current_value_sol", new_callable=AsyncMock, return_value=0.21), \
             patch("sniper.position_manager.execute_sell", new_callable=AsyncMock, return_value=("paper-sell-p", 0.10)), \
             patch("sniper.position_manager.send_telegram", new_callable=AsyncMock):
            await check_positions(db, AsyncMock(), AsyncMock(), AsyncMock(), settings)

        updated = await db.get_open_position_by_address("TestToken111")
        assert updated is not None
        # 50% of 1000 tokens sold -> 500 remaining
        assert updated.entry_token_amount == 500
        # entry_sol should be 50% of original (0.05)
        assert abs(updated.entry_sol - 0.05) < 0.001

    async def test_pnl_correct_after_partial_sell(self, db, settings):
        """After partial sell, PnL should be calculated on adjusted entry_sol."""
        pos = _make_position(
            age_minutes=20, entry_sol=0.1, entry_token_amount=1000,
            trailing_active=True, peak_value_sol=0.22,
            partial_exit_tier=0,
        )
        pos_id = await db.open_position(pos)
        await db.set_trailing_active(pos_id)
        await db.update_peak_value(pos_id, 0.22)

        # First call: partial sell fires at tier 1
        with patch("sniper.position_manager.get_current_value_sol", new_callable=AsyncMock, return_value=0.21), \
             patch("sniper.position_manager.execute_sell", new_callable=AsyncMock, return_value=("paper-sell-p1", 0.10)), \
             patch("sniper.position_manager.send_telegram", new_callable=AsyncMock):
            await check_positions(db, AsyncMock(), AsyncMock(), AsyncMock(), settings)

        # Verify adjusted state
        updated = await db.get_open_position_by_address("TestToken111")
        assert updated is not None
        assert updated.entry_sol == pytest.approx(0.05, abs=0.001)
        assert updated.entry_token_amount == 500

        # Second call: price drops to trigger trailing stop
        # With entry_sol=0.05 and current_value=0.05, PnL = 0% (not -50%)
        # This ensures partial sell doesn't cause misleading PnL
        with patch("sniper.position_manager.get_current_value_sol", new_callable=AsyncMock, return_value=0.05), \
             patch("sniper.position_manager.execute_sell", new_callable=AsyncMock, return_value=("paper-sell-close", 0.05)), \
             patch("sniper.position_manager.send_telegram", new_callable=AsyncMock):
            actions = await check_positions(db, AsyncMock(), AsyncMock(), AsyncMock(), settings)

        # Position should close via trailing stop (big drop from peak)
        assert len(actions) >= 1
        # The PnL in the close action should be based on adjusted entry_sol
        # With 0.05 received on 0.05 entry = 0% PnL, not -50%
        close_action = [a for a in actions if "TRAILING_STOP" in a or "PnL" in a]
        assert len(close_action) >= 1
        # Should NOT show -50% PnL
        assert "-50.0%" not in close_action[0]

"""Tests for rebuilt copy trader."""
import pytest
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from sniper.copy_trader import (
    SWAP_PATTERNS,
    _is_swap_transaction,
    _extract_bought_token,
    _write_injection,
    _get_tracked_wallets,
    _record_signal,
    smart_money_signals,
    prune_stale_signals,
)
from sniper.config import Settings


def _settings(**overrides):
    defaults = dict(
        SOLANA_RPC_URL="http://localhost",
        KEYPAIR_PATH="/tmp/test.json",
        SCOUT_DB_PATH="/tmp/scout.db",
        SNIPER_DB_PATH="/tmp/sniper.db",
        SMART_MONEY_WALLETS="wallet1,wallet2,wallet3",
        COPY_TRADE_ENABLED=True,
        HELIUS_API_KEY="test-key",
    )
    defaults.update(overrides)
    return Settings(**defaults)


class TestSwapPatterns:
    def test_all_dexes_covered(self):
        pattern_text = " ".join(SWAP_PATTERNS)
        assert "JUP" in pattern_text
        assert "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA" in pattern_text
        assert "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C" in pattern_text
        assert "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8" in pattern_text
        assert "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc" in pattern_text
        assert "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo" in pattern_text
        assert "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P" in pattern_text

    def test_jupiter_route_detected(self):
        assert _is_swap_transaction(["Program log: Instruction: Route"]) is True

    def test_raydium_v4_detected(self):
        assert _is_swap_transaction(["Program 675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8 invoke"]) is True

    def test_orca_detected(self):
        assert _is_swap_transaction(["Program whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc invoke"]) is True

    def test_non_swap_not_detected(self):
        assert _is_swap_transaction(["Program log: Transfer", "System program invoke"]) is False


class TestTrackedWallets:
    def test_wallets_from_config(self):
        settings = _settings(SMART_MONEY_WALLETS="a,b,c")
        assert _get_tracked_wallets(settings) == ["a", "b", "c"]

    def test_empty_wallets(self):
        settings = _settings(SMART_MONEY_WALLETS="")
        assert _get_tracked_wallets(settings) == []


class TestMultiWalletSignals:
    @pytest.mark.asyncio
    async def test_accumulates_wallets(self):
        smart_money_signals.clear()
        await _record_signal("mint1", "walletA")
        await _record_signal("mint1", "walletB")
        assert smart_money_signals["mint1"]["count"] == 2
        assert "walletA" in smart_money_signals["mint1"]["wallets"]
        assert "walletB" in smart_money_signals["mint1"]["wallets"]

    @pytest.mark.asyncio
    async def test_prune_respects_max_age(self):
        smart_money_signals.clear()
        await _record_signal("mint1", "walletA")
        smart_money_signals["mint1"]["detected_at"] = datetime(2020, 1, 1, tzinfo=timezone.utc)
        await prune_stale_signals(max_age_minutes=60)
        assert "mint1" not in smart_money_signals


class TestExtractBoughtToken:
    def _mock_helius_response(self, mock_response, status=200):
        """Build a properly nested async-context-manager mock for aiohttp session.post."""
        mock_resp = AsyncMock()
        mock_resp.status = status
        mock_resp.json = AsyncMock(return_value=mock_response)

        post_cm = AsyncMock()
        post_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        post_cm.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=post_cm)
        return mock_session

    @pytest.mark.asyncio
    async def test_filters_intermediary_mints(self):
        settings = _settings()
        mock_response = [{
            "tokenTransfers": [
                {"mint": "So11111111111111111111111111111111111111112", "toUserAccount": "wallet1"},
                {"mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "toUserAccount": "wallet1"},
                {"mint": "actual_token_mint", "toUserAccount": "wallet1"},
            ],
            "nativeTransfers": [{"fromUserAccount": "wallet1", "amount": 1000000}],
        }]
        mock_session = self._mock_helius_response(mock_response)
        result = await _extract_bought_token("sig", "wallet1", settings, mock_session)
        assert result == "actual_token_mint"

    @pytest.mark.asyncio
    async def test_rejects_airdrop_no_sol_spent(self):
        settings = _settings()
        mock_response = [{
            "tokenTransfers": [{"mint": "airdrop_token", "toUserAccount": "wallet1"}],
            "nativeTransfers": [{"fromUserAccount": "other_wallet", "amount": 1000000}],
        }]
        mock_session = self._mock_helius_response(mock_response)
        result = await _extract_bought_token("sig", "wallet1", settings, mock_session)
        assert result is None

    @pytest.mark.asyncio
    async def test_accepts_session_parameter(self):
        """Verify _extract_bought_token accepts an explicit session parameter."""
        settings = _settings(HELIUS_API_KEY="")
        mock_session = AsyncMock()
        result = await _extract_bought_token("sig", "wallet1", settings, mock_session)
        assert result is None  # Returns None because no API key


class TestWriteInjection:
    @pytest.mark.asyncio
    async def test_writes_to_db(self, tmp_path):
        import aiosqlite
        db_path = tmp_path / "scout.db"
        conn = await aiosqlite.connect(str(db_path))
        await conn.executescript("""
            CREATE TABLE IF NOT EXISTS smart_money_injections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_mint TEXT NOT NULL, wallet_address TEXT NOT NULL,
                tx_signature TEXT, source TEXT DEFAULT 'websocket',
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed INTEGER DEFAULT 0, UNIQUE(token_mint, tx_signature)
            );
        """)
        await _write_injection(conn, "mint1", "wallet1", "tx1", "websocket")
        cursor = await conn.execute("SELECT * FROM smart_money_injections")
        rows = await cursor.fetchall()
        assert len(rows) == 1
        await conn.close()

    @pytest.mark.asyncio
    async def test_dedup_on_same_tx(self, tmp_path):
        import aiosqlite
        db_path = tmp_path / "scout.db"
        conn = await aiosqlite.connect(str(db_path))
        await conn.executescript("""
            CREATE TABLE IF NOT EXISTS smart_money_injections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_mint TEXT NOT NULL, wallet_address TEXT NOT NULL,
                tx_signature TEXT, source TEXT DEFAULT 'websocket',
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed INTEGER DEFAULT 0, UNIQUE(token_mint, tx_signature)
            );
        """)
        await _write_injection(conn, "mint1", "wallet1", "tx1")
        await _write_injection(conn, "mint1", "wallet1", "tx1")
        cursor = await conn.execute("SELECT COUNT(*) FROM smart_money_injections")
        row = await cursor.fetchone()
        assert row[0] == 1
        await conn.close()

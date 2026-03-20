"""Application configuration via Pydantic BaseSettings."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Solana RPC
    SOLANA_RPC_URL: str = "https://api.mainnet-beta.solana.com"
    SOLANA_WS_URL: str = "wss://api.mainnet-beta.solana.com"

    # Wallet
    KEYPAIR_PATH: Path = Path("wallet.json")

    # Scout DB (read-only)
    SCOUT_DB_PATH: Path = Path("../coinpump-scout/scout.db")

    # Sniper DB (read-write)
    SNIPER_DB_PATH: Path = Path("sniper.db")

    # Risk controls
    MAX_BUY_SOL: float = 0.1
    MAX_PORTFOLIO_SOL: float = 1.0
    MAX_OPEN_POSITIONS: int = 5
    STOP_LOSS_PCT: float = 25.0
    TAKE_PROFIT_PCT: float = 100.0
    MIN_CONVICTION_SCORE: float = 70.0
    MIN_LIQUIDITY_USD: float = 15000.0
    MAX_TOKEN_AGE_DAYS: int = 3

    # Cooldown
    COOLDOWN_HOURS: int = 6

    # Trailing take-profit
    TRAILING_TRIGGER_PCT: float = 50.0
    TRAILING_STOP_PCT: float = 20.0

    # Partial exits
    PARTIAL_SELL_FRACTION: float = 0.5

    # Jupiter
    JUPITER_API_URL: str = "https://lite-api.jup.ag/swap/v1"
    JUPITER_FALLBACK_URL: str = "https://api.jup.ag/swap/v1"
    SLIPPAGE_BPS: int = 300
    JUPITER_TIMEOUT_SEC: int = 30

    # Execution
    PAPER_MODE: bool = True
    POLL_INTERVAL_SECONDS: int = 30
    POSITION_CHECK_INTERVAL_SECONDS: int = 10
    PRIORITY_FEE_LAMPORTS: int = 100000
    PRIORITY_FEE_AUTO: bool = True

    # Split orders
    SPLIT_ORDERS: bool = False
    SPLIT_COUNT: int = 3
    SPLIT_DELAY_SECONDS: int = 10

    # Telegram notifications
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

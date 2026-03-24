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
    SOLANA_RPC_URLS: str = ""  # Comma-separated fallback RPCs
    SOLANA_WS_URL: str = "wss://api.mainnet-beta.solana.com"

    # Wallet (single or multi)
    KEYPAIR_PATH: Path = Path("wallet.json")
    MULTI_WALLET_ENABLED: bool = False
    WALLET_PATHS: str = ""  # Comma-separated: "wallet1.json,wallet2.json,wallet3.json"

    # Scout DB (read-only)
    SCOUT_DB_PATH: Path = Path("../coinpump-scout/scout.db")

    # Injections DB (shared with scout — sniper inserts, scout marks processed)
    INJECTIONS_DB_PATH: str = "/opt/scout/injections.db"

    # Sniper DB (read-write)
    SNIPER_DB_PATH: Path = Path("sniper.db")

    # Risk controls
    MAX_BUY_SOL: float = 1.0  # Match Kelly max
    MAX_PORTFOLIO_SOL: float = 1.0
    MAX_OPEN_POSITIONS: int = 5
    STOP_LOSS_PCT: float = 35.0
    TAKE_PROFIT_PCT: float = 100.0
    MIN_CONVICTION_SCORE: float = 70.0
    MIN_LIQUIDITY_USD: float = 15000.0
    MAX_TOKEN_AGE_DAYS: int = 3

    # Cooldown
    COOLDOWN_HOURS: int = 6

    # Trailing take-profit (legacy — kept for backwards compat)
    TRAILING_TRIGGER_PCT: float = 50.0
    TRAILING_STOP_PCT: float = 20.0

    # Partial exits
    PARTIAL_SELL_FRACTION: float = 0.5

    # Time-based exit phases (minutes)
    PROTECTION_WINDOW_MIN: int = 10
    MOMENTUM_CHECK_MIN: int = 30
    MAX_HOLD_MIN: int = 60

    # Phase thresholds
    RUG_DETECT_PCT: float = 50.0        # Phase 1: exit if down this much
    MOMENTUM_LOSS_PCT: float = 15.0     # Phase 2: exit if down this much (loosened from 10%)
    PUMP_WINDOW_MIN_GAIN_PCT: float = 15.0  # Phase 3: must be up this much (loosened from 20%)
    TRAILING_ACTIVATE_PCT: float = 30.0  # Activate trailing at this gain

    # Phase 4 cleanup threshold
    PHASE4_TRAILING_MIN_PNL: float = 25.0  # Match trailing trigger — don't force-close climbing tokens

    # Trailing tiers
    TRAILING_TIER1_PCT: float = 20.0    # 30-100% gain: trail at 20%
    TRAILING_TIER2_PCT: float = 15.0    # 100-200% gain: trail at 15%
    TRAILING_TIER3_PCT: float = 10.0    # 200%+ gain: trail at 10%

    # Buy speed enforcement
    BUY_TIMEOUT_SECONDS: int = 30

    # Hard quality gates
    MAX_TOP3_CONCENTRATION: float = 0.40
    MIN_HOLDER_COUNT: int = 5

    # Signal freshness
    MAX_SIGNAL_AGE_SECONDS: int = 120

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

    # Profit-taking ladder
    PROFIT_LADDER_ENABLED: bool = True
    PROFIT_LADDER_PCT: float = 50.0

    # Sell pressure monitoring
    SELL_PRESSURE_THRESHOLD: float = 0.80  # 80% sells — only exit if also in loss and >5min held

    # Liquidity-adjusted position sizing
    LIQUIDITY_SIZING_ENABLED: bool = True

    # Kelly Criterion position sizing
    KELLY_ENABLED: bool = True
    KELLY_LOOKBACK: int = 20       # Number of recent trades to analyze
    KELLY_MIN_TRADES: int = 5      # Minimum trades before Kelly activates
    KELLY_MIN_BET: float = 0.25    # Floor (SOL)
    KELLY_MAX_BET: float = 1.0     # Ceiling (SOL)
    KELLY_WIN_RATE_OVERRIDE: float = 0  # Manual override from GMGN (0 = use DB)
    KELLY_PAYOFF_OVERRIDE: float = 0   # Manual override for avg_win/avg_loss ratio (0 = use DB)

    # Jito MEV Protection
    JITO_ENABLED: bool = False  # Enable for MEV-protected transactions
    JITO_TIP_LAMPORTS: int = 100000  # Tip amount (0.0001 SOL default)

    # Conviction hold — patient exit logic for high-score tokens
    CONVICTION_HOLD_ENABLED: bool = True
    CONVICTION_HOLD_MIN_SCORE: float = 70.0
    CONVICTION_HOLD_MAX_HOLD_MIN: float = 240  # 4 hours
    CONVICTION_HOLD_TRAILING_ACTIVATE_PCT: float = 50.0
    CONVICTION_HOLD_TRAILING_PCT: float = 20.0
    CONVICTION_HOLD_RUG_LIQUIDITY_DROP_PCT: float = 70.0
    CONVICTION_HOLD_HARD_STOP_PCT: float = 70.0

    # DCA on dip (risky — disabled by default)
    DCA_ENABLED: bool = False

    # Smart money / copy trading
    COPY_TRADE_ENABLED: bool = False
    SMART_MONEY_WALLETS: str = ""  # Comma-separated tracked wallet addresses (shared with scout)
    COPY_TRADE_SCORE_BOOST: int = 20  # Conviction boost per wallet
    SMART_MONEY_BOOST_CAP: int = 80  # Max total boost from smart wallets
    BACKFILL_MAX_MINUTES: int = 30  # Max age for reconnect backfill
    HELIUS_API_KEY: str = ""  # Required for copy trading WebSocket

    # GODMODE bundle detection
    GODMODE_URL: str = "http://localhost:8085"
    GODMODE_ENABLED: bool = True
    GODMODE_BUNDLE_THRESHOLD: int = 30

    # Dashboard API key (required for /api/trade endpoint)
    DASHBOARD_API_KEY: str = ""

    # Telegram notifications
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

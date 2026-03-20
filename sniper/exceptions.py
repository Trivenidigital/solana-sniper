"""Domain exceptions for the sniper bot."""


class SniperError(Exception):
    """Base exception for all sniper errors."""


class WalletError(SniperError):
    """Wallet-related errors."""


class InsufficientBalanceError(WalletError):
    """Not enough SOL to execute trade."""


class JupiterError(SniperError):
    """Jupiter API errors."""


class JupiterQuoteError(JupiterError):
    """Failed to get a quote from Jupiter."""


class JupiterSwapError(JupiterError):
    """Failed to build swap transaction."""


class ExecutionError(SniperError):
    """Transaction execution errors."""


class TransactionFailedError(ExecutionError):
    """Transaction was sent but failed to confirm."""


class SignalReaderError(SniperError):
    """Error reading signals from scout database."""

"""Solana wallet management — generate, load, and check balances."""

import json
from pathlib import Path

import structlog
from solana.rpc.async_api import AsyncClient
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from sniper.exceptions import WalletError

logger = structlog.get_logger()

LAMPORTS_PER_SOL = 1_000_000_000


def generate_keypair(path: Path) -> Keypair:
    """Generate a new Solana keypair and save to file."""
    kp = Keypair()
    secret_bytes = list(bytes(kp))
    path.write_text(json.dumps(secret_bytes))
    logger.info(
        "New wallet generated",
        pubkey=str(kp.pubkey()),
        path=str(path),
    )
    return kp


def load_keypair(path: Path) -> Keypair:
    """Load keypair from JSON file, or generate if it doesn't exist."""
    if not path.exists():
        logger.warning("Wallet file not found, generating new wallet", path=str(path))
        return generate_keypair(path)

    secret_bytes = json.loads(path.read_text())
    kp = Keypair.from_bytes(bytes(secret_bytes))
    logger.info("Wallet loaded", pubkey=str(kp.pubkey()))
    return kp


async def get_sol_balance(client: AsyncClient, pubkey: Pubkey) -> float:
    """Get SOL balance for a wallet address."""
    try:
        resp = await client.get_balance(pubkey)
        lamports = resp.value
        return lamports / LAMPORTS_PER_SOL
    except Exception as e:
        raise WalletError(f"Failed to get SOL balance: {e}") from e


async def get_token_balance(client: AsyncClient, owner: Pubkey, mint: Pubkey) -> int:
    """Get SPL token balance for a specific mint. Returns raw amount (smallest unit)."""
    try:
        from solders.rpc.config import RpcAccountInfoConfig
        from solana.rpc.types import TokenAccountOpts

        resp = await client.get_token_accounts_by_owner_json_parsed(
            owner, TokenAccountOpts(mint=mint),
        )
        accounts = resp.value
        if not accounts:
            return 0
        parsed = accounts[0].account.data.parsed
        return int(parsed["info"]["tokenAmount"]["amount"])
    except Exception as e:
        raise WalletError(f"Failed to get token balance: {e}") from e

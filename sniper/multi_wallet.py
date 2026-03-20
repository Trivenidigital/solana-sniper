"""Multi-wallet copy trading — execute trades across multiple wallets simultaneously."""

import asyncio
import json
from pathlib import Path

import aiohttp
import structlog
from solana.rpc.async_api import AsyncClient
from solders.keypair import Keypair

from sniper.config import Settings
from sniper.executor import execute_buy, execute_sell
from sniper.wallet import load_keypair, get_sol_balance

logger = structlog.get_logger()


def load_wallets(wallet_paths: list[str]) -> list[Keypair]:
    """Load multiple keypairs from a list of JSON file paths."""
    wallets = []
    for path_str in wallet_paths:
        path = Path(path_str.strip())
        if not path.exists():
            logger.warning("Wallet file not found, skipping", path=str(path))
            continue
        try:
            kp = load_keypair(path)
            wallets.append(kp)
            logger.info("Wallet loaded", pubkey=str(kp.pubkey()), path=str(path))
        except Exception as e:
            logger.error("Failed to load wallet", path=str(path), error=str(e))
    return wallets


async def copy_buy(
    client: AsyncClient,
    wallets: list[Keypair],
    session: aiohttp.ClientSession,
    contract_address: str,
    sol_amount: float,
    settings: Settings,
) -> list[dict]:
    """Execute buy across all wallets simultaneously.

    Returns list of results: [{"wallet": pubkey, "tx": sig, "tokens": amount, "success": bool}, ...]
    """
    async def _buy_one(kp: Keypair) -> dict:
        pubkey = str(kp.pubkey())
        try:
            # Check balance first
            bal = await get_sol_balance(client, kp.pubkey())
            if bal < sol_amount + 0.01:
                return {"wallet": pubkey, "tx": None, "tokens": 0, "success": False, "error": f"Insufficient SOL: {bal:.4f}"}

            tx_sig, tokens = await execute_buy(
                client, kp, session, contract_address, sol_amount, settings,
            )
            logger.info("Copy buy success", wallet=pubkey, tx=tx_sig, tokens=tokens)
            return {"wallet": pubkey, "tx": tx_sig, "tokens": tokens, "success": True, "error": None}
        except Exception as e:
            logger.error("Copy buy failed", wallet=pubkey, error=str(e))
            return {"wallet": pubkey, "tx": None, "tokens": 0, "success": False, "error": str(e)}

    # Execute all buys in parallel
    results = await asyncio.gather(*[_buy_one(kp) for kp in wallets])

    succeeded = sum(1 for r in results if r["success"])
    total_tokens = sum(r["tokens"] for r in results)
    total_sol = sol_amount * succeeded

    logger.info(
        "Copy buy complete",
        token=contract_address,
        wallets_total=len(wallets),
        wallets_succeeded=succeeded,
        total_sol=total_sol,
        total_tokens=total_tokens,
    )

    return list(results)


async def copy_sell(
    client: AsyncClient,
    wallets: list[Keypair],
    session: aiohttp.ClientSession,
    contract_address: str,
    token_amounts: dict[str, int],
    settings: Settings,
) -> list[dict]:
    """Execute sell across all wallets simultaneously.

    Args:
        token_amounts: dict mapping wallet pubkey -> token amount to sell
    """
    async def _sell_one(kp: Keypair) -> dict:
        pubkey = str(kp.pubkey())
        amount = token_amounts.get(pubkey, 0)
        if amount <= 0:
            return {"wallet": pubkey, "tx": None, "sol_received": 0, "success": False, "error": "No tokens"}
        try:
            tx_sig, sol_received = await execute_sell(
                client, kp, session, contract_address, amount, settings,
            )
            logger.info("Copy sell success", wallet=pubkey, tx=tx_sig, sol=sol_received)
            return {"wallet": pubkey, "tx": tx_sig, "sol_received": sol_received, "success": True, "error": None}
        except Exception as e:
            logger.error("Copy sell failed", wallet=pubkey, error=str(e))
            return {"wallet": pubkey, "tx": None, "sol_received": 0, "success": False, "error": str(e)}

    results = await asyncio.gather(*[_sell_one(kp) for kp in wallets])

    succeeded = sum(1 for r in results if r["success"])
    total_sol = sum(r["sol_received"] for r in results)

    logger.info(
        "Copy sell complete",
        token=contract_address,
        wallets_total=len(wallets),
        wallets_succeeded=succeeded,
        total_sol_received=total_sol,
    )

    return list(results)


async def get_all_balances(
    client: AsyncClient,
    wallets: list[Keypair],
) -> list[dict]:
    """Get SOL balance for all wallets."""
    async def _get_bal(kp: Keypair) -> dict:
        pubkey = str(kp.pubkey())
        try:
            bal = await get_sol_balance(client, kp.pubkey())
            return {"wallet": pubkey, "balance": bal}
        except Exception:
            return {"wallet": pubkey, "balance": 0.0}

    return list(await asyncio.gather(*[_get_bal(kp) for kp in wallets]))

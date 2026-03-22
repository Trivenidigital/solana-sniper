"""Jito bundle submission for MEV-protected transactions."""

import asyncio
import base64

import aiohttp
import structlog
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from sniper.exceptions import TransactionFailedError

logger = structlog.get_logger()

# Jito block engine endpoints (multiple for failover)
JITO_ENDPOINTS = [
    "https://mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://amsterdam.mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://ny.mainnet.block-engine.jito.wtf/api/v1/bundles",
]


async def send_jito_bundle(
    signed_tx_bytes: bytes,
    tip_lamports: int = 10000,
) -> str:
    """Send a transaction as a Jito bundle for MEV protection.

    Args:
        signed_tx_bytes: The signed, serialized transaction bytes
        tip_lamports: Tip amount for Jito validators (default 10000 = 0.00001 SOL)

    Returns:
        Bundle ID string
    """
    # Encode transaction as base64
    tx_b64 = base64.b64encode(signed_tx_bytes).decode("utf-8")

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendBundle",
        "params": [[tx_b64]],
    }

    async with aiohttp.ClientSession() as session:
        for endpoint in JITO_ENDPOINTS:
            try:
                async with session.post(
                    endpoint,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        bundle_id = data.get("result")
                        if bundle_id:
                            logger.info("Jito bundle sent", bundle_id=bundle_id, endpoint=endpoint)
                            return bundle_id
                    else:
                        body = await resp.text()
                        logger.debug("Jito endpoint failed", endpoint=endpoint, status=resp.status, body=body[:200])
            except Exception as e:
                logger.debug("Jito endpoint error", endpoint=endpoint, error=str(e))
                continue

    raise Exception("All Jito endpoints failed")


async def send_transaction_with_jito(
    client,
    keypair,
    tx_bytes: bytes,
    settings,
) -> str:
    """Sign and send transaction via Jito if enabled, otherwise standard RPC.

    Returns transaction signature string.
    """
    txn = VersionedTransaction.from_bytes(tx_bytes)
    txn = VersionedTransaction(txn.message, [keypair])
    signed_bytes = bytes(txn)

    if settings.JITO_ENABLED:
        try:
            bundle_id = await send_jito_bundle(signed_bytes, settings.JITO_TIP_LAMPORTS)
            # Get the transaction signature from the signed transaction
            sig = str(txn.signatures[0])

            # Wait for confirmation via standard RPC
            for _ in range(30):  # Wait up to 30 seconds
                await asyncio.sleep(1)
                try:
                    resp = await client.get_signature_statuses([Signature.from_string(sig)])
                    if resp.value and resp.value[0]:
                        if resp.value[0].err:
                            raise Exception(f"Jito bundle tx failed: {resp.value[0].err}")
                        logger.info("Jito bundle confirmed", sig=sig, bundle_id=bundle_id)
                        return sig
                except Exception:
                    continue

            # If we get here, tx wasn't confirmed in 30s — do NOT return
            # an unconfirmed signature as that creates phantom positions.
            raise TransactionFailedError(f"Jito bundle not confirmed after 30s: {sig}")

        except Exception:
            # Do NOT fall back to standard RPC — the transaction was signed
            # with a specific blockhash that may have expired.  Let the caller
            # retry with a fresh transaction instead.
            raise

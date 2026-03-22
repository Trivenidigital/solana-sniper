"""Check smart money wallet performance before adding to tracker.

Usage:
    uv run python -m sniper.wallet_stats <wallet_address> [--limit 50]
    uv run python -m sniper.wallet_stats 2tgUbS9UMoQD6GkDZBiqKYCURnGrSb6ocYwRABrSJUvY
"""

import argparse
import asyncio
import sys
from datetime import datetime, timezone

import aiohttp

from sniper.config import Settings

INTERMEDIARY_MINTS = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}

JUPITER_QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"
SOL_MINT = "So11111111111111111111111111111111111111112"


async def _get_sol_price(session: aiohttp.ClientSession) -> float:
    try:
        usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        url = f"{JUPITER_QUOTE_URL}?inputMint={SOL_MINT}&outputMint={usdc}&amount=1000000000&slippageBps=50"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return int(data.get("outAmount", 0)) / 1e6
    except Exception:
        pass
    return 0.0



def _extract_trade(tx: dict, wallet: str) -> dict | None:
    """Extract buy/sell info from a parsed Helius transaction.

    Helius Enhanced Transactions return SOL as wrapped SOL in tokenTransfers
    (mint = So111...), not in nativeTransfers. We use tokenTransfers for both
    SOL amounts and token identification.
    """
    transfers = tx.get("tokenTransfers", [])
    timestamp = tx.get("timestamp", 0)
    sig = tx.get("signature", "")

    if not transfers:
        return None

    SOL_MINT = "So11111111111111111111111111111111111111112"

    # SOL spent by wallet (wrapped SOL leaving wallet)
    sol_out = sum(
        t.get("tokenAmount", 0)
        for t in transfers
        if t.get("fromUserAccount") == wallet and t.get("mint") == SOL_MINT
    )

    # SOL received by wallet (wrapped SOL arriving)
    sol_in = sum(
        t.get("tokenAmount", 0)
        for t in transfers
        if t.get("toUserAccount") == wallet and t.get("mint") == SOL_MINT
    )

    # Find the non-intermediary token involved with this wallet
    token_mint = None
    for t in transfers:
        mint = t.get("mint", "")
        if mint and mint not in INTERMEDIARY_MINTS:
            if t.get("toUserAccount") == wallet or t.get("fromUserAccount") == wallet:
                token_mint = mint

    if not token_mint:
        return None

    # Determine side: wallet received non-SOL token = buy
    received_token = any(
        t.get("toUserAccount") == wallet and t.get("mint") == token_mint
        for t in transfers
    )

    return {
        "signature": sig,
        "timestamp": timestamp,
        "token_mint": token_mint,
        "side": "buy" if received_token else "sell",
        "sol_amount": sol_out if received_token else sol_in,
        "time": datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if timestamp else "?",
    }


async def _fetch_all_swaps(
    session: aiohttp.ClientSession, wallet: str, api_key: str, max_txns: int = 500,
) -> list[dict]:
    """Paginate through Helius transaction history to get full swap history."""
    all_txns: list[dict] = []
    before_sig: str | None = None
    page = 0

    while len(all_txns) < max_txns:
        url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions"
        params: dict = {"api-key": api_key, "limit": 100, "type": "SWAP"}
        if before_sig:
            params["before"] = before_sig

        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 429:
                    print(f"  Rate limited, waiting 3s...")
                    await asyncio.sleep(3)
                    continue
                if resp.status != 200:
                    print(f"  Helius API error: {resp.status}")
                    break
                batch = await resp.json()
        except Exception as e:
            print(f"  Fetch error: {e}")
            break

        if not batch:
            break

        all_txns.extend(batch)
        before_sig = batch[-1].get("signature")
        page += 1
        print(f"  Fetched page {page}: {len(batch)} txns (total: {len(all_txns)})")
        await asyncio.sleep(0.5)  # Rate limit

    return all_txns[:max_txns]


async def analyze_wallet(wallet: str, limit: int = 500) -> None:
    settings = Settings()
    if not settings.HELIUS_API_KEY:
        print("Error: HELIUS_API_KEY not set in .env")
        sys.exit(1)

    print(f"\nAnalyzing wallet: {wallet}")
    print(f"Fetching up to {limit} swap transactions (paginated)...\n")

    async with aiohttp.ClientSession() as session:
        txns = await _fetch_all_swaps(session, wallet, settings.HELIUS_API_KEY, max_txns=limit)

        if not txns:
            print("No swap transactions found.")
            return

        sol_price = await _get_sol_price(session)

        # Parse trades
        trades: list[dict] = []
        for tx in txns:
            trade = _extract_trade(tx, wallet)
            if trade:
                trades.append(trade)

        if not trades:
            print("No parseable trades found.")
            return

        # Match buys to sells per token
        # Group by token_mint
        token_trades: dict[str, list[dict]] = {}
        for t in trades:
            token_trades.setdefault(t["token_mint"], []).append(t)

        # Calculate P&L per token (simple: total SOL in vs total SOL out)
        results = []
        for mint, token_txns in token_trades.items():
            buys = [t for t in token_txns if t["side"] == "buy"]
            sells = [t for t in token_txns if t["side"] == "sell"]

            total_bought_sol = sum(t["sol_amount"] for t in buys)
            total_sold_sol = sum(t["sol_amount"] for t in sells)

            if not buys:
                continue

            pnl_sol = total_sold_sol - total_bought_sol
            pnl_pct = (pnl_sol / total_bought_sol * 100) if total_bought_sol > 0 else 0

            # Check if still holding (bought but not fully sold)
            still_holding = len(sells) == 0

            results.append({
                "mint": mint,
                "buys": len(buys),
                "sells": len(sells),
                "bought_sol": total_bought_sol,
                "sold_sol": total_sold_sol,
                "pnl_sol": pnl_sol,
                "pnl_pct": pnl_pct,
                "holding": still_holding,
                "first_buy": buys[-1]["time"] if buys else "?",  # oldest buy (list is newest first)
            })

        # Sort by time (most recent first)
        results.sort(key=lambda r: r["first_buy"], reverse=True)

        # Stats
        closed = [r for r in results if not r["holding"]]
        winners = [r for r in closed if r["pnl_sol"] > 0]
        losers = [r for r in closed if r["pnl_sol"] <= 0]
        total_pnl = sum(r["pnl_sol"] for r in closed)
        avg_win = sum(r["pnl_pct"] for r in winners) / len(winners) if winners else 0
        avg_loss = sum(r["pnl_pct"] for r in losers) / len(losers) if losers else 0
        win_rate = len(winners) / len(closed) * 100 if closed else 0

        # Print summary
        print("=" * 70)
        print(f"  WALLET STATS: {wallet[:8]}...{wallet[-4:]}")
        print("=" * 70)
        print(f"  Trades analyzed:  {len(results)} tokens ({len(trades)} total txns)")
        print(f"  Closed trades:    {len(closed)}")
        print(f"  Still holding:    {len([r for r in results if r['holding']])}")
        print(f"  Win Rate:         {win_rate:.1f}% ({len(winners)}W / {len(losers)}L)")
        print(f"  Total P&L:        {total_pnl:+.4f} SOL (${total_pnl * sol_price:+.2f})")
        print(f"  Avg Win:          {avg_win:+.1f}%")
        print(f"  Avg Loss:         {avg_loss:+.1f}%")
        if sol_price > 0:
            print(f"  SOL Price:        ${sol_price:.2f}")
        print("=" * 70)

        # Print individual trades
        print(f"\n{'Date':<18} {'Token':<12} {'Side':<8} {'SOL In':<10} {'SOL Out':<10} {'P&L %':<10} {'Status'}")
        print("-" * 85)
        for r in results:
            status = "HOLDING" if r["holding"] else ("WIN" if r["pnl_sol"] > 0 else "LOSS")
            status_color = "\033[92m" if status == "WIN" else ("\033[91m" if status == "LOSS" else "\033[93m")
            reset = "\033[0m"
            print(
                f"{r['first_buy']:<18} "
                f"{r['mint'][:10]+'...':<12} "
                f"{r['buys']}B/{r['sells']}S    "
                f"{r['bought_sol']:<10.4f} "
                f"{r['sold_sol']:<10.4f} "
                f"{r['pnl_pct']:>+8.1f}%  "
                f"{status_color}{status}{reset}"
            )

        # Verdict
        print(f"\n{'=' * 70}")
        if win_rate >= 50 and total_pnl > 0:
            print(f"  VERDICT: \033[92mGOOD — {win_rate:.0f}% WR, profitable. Add to tracker.\033[0m")
        elif win_rate >= 40:
            print(f"  VERDICT: \033[93mMEDIUM — {win_rate:.0f}% WR. Monitor before adding.\033[0m")
        else:
            print(f"  VERDICT: \033[91mPOOR — {win_rate:.0f}% WR. Do NOT add.\033[0m")
        print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="Check smart money wallet performance")
    parser.add_argument("wallet", help="Solana wallet address to analyze")
    parser.add_argument("--limit", type=int, default=500, help="Max swaps to fetch (default: 500, paginated)")
    args = parser.parse_args()

    asyncio.run(analyze_wallet(args.wallet, args.limit))


if __name__ == "__main__":
    main()

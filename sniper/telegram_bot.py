"""Telegram command bot — manage sniper from mobile.

Commands:
    /status   — open positions, PnL, balance
    /close X  — close position by token name or ticker
    /positions — list all open positions
    /balance  — wallet SOL balance
    /help     — show commands

Runs as a background asyncio task polling for Telegram updates.
"""

import asyncio
import json
from datetime import datetime, timezone

import aiohttp
import structlog

from sniper.config import Settings

logger = structlog.get_logger()

_last_update_id = 0
_PAUSE_FILE = "/tmp/sniper_paused"


async def _send(session: aiohttp.ClientSession, settings: Settings, text: str) -> None:
    """Send a message to the configured Telegram chat."""
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with session.post(url, json={
            "chat_id": settings.TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        }, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                logger.debug("Telegram send failed", status=resp.status)
    except Exception as e:
        logger.debug("Telegram send error", error=str(e))


async def _get_updates(session: aiohttp.ClientSession, settings: Settings) -> list:
    """Poll for new Telegram messages."""
    global _last_update_id
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"offset": _last_update_id + 1, "timeout": 5, "allowed_updates": '["message"]'}
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("result", [])
    except Exception:
        pass
    return []


async def _handle_status(session: aiohttp.ClientSession, settings: Settings) -> str:
    """Handle /status command."""
    import aiosqlite
    lines = ["<b>Sniper Status</b>\n"]

    # Open positions
    try:
        async with aiosqlite.connect(f"file:{settings.SNIPER_DB_PATH}?mode=ro", uri=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM positions WHERE status='open' ORDER BY opened_at DESC")
            positions = [dict(r) for r in await cursor.fetchall()]

            if positions:
                total_entry = sum(p["entry_sol"] for p in positions)
                lines.append(f"<b>{len(positions)} open</b> | {total_entry:.2f} SOL exposed\n")
                for p in positions:
                    name = p["token_name"]
                    entry = p["entry_sol"]
                    manual = "M" if p.get("manual") else "B"
                    tier = p.get("partial_exit_tier", 0)
                    lines.append(f"  [{manual}] <b>{name}</b> {entry:.3f} SOL (T{tier})")
            else:
                lines.append("No open positions")

            # Realized PnL today
            cursor = await db.execute(
                "SELECT COALESCE(SUM(pnl_sol), 0) FROM positions WHERE status='closed' AND closed_at >= date('now')"
            )
            row = await cursor.fetchone()
            today_pnl = float(row[0]) if row else 0
            lines.append(f"\nToday PnL: <b>{today_pnl:+.4f} SOL</b>")
    except Exception as e:
        lines.append(f"DB error: {e}")

    # Balance
    try:
        from solders.keypair import Keypair
        from solana.rpc.async_api import AsyncClient
        kp = Keypair.from_json(open(settings.KEYPAIR_PATH).read())
        client = AsyncClient(settings.SOLANA_RPC_URL)
        resp = await client.get_balance(kp.pubkey())
        bal = resp.value / 1e9
        await client.close()
        lines.append(f"Balance: <b>{bal:.3f} SOL</b>")
    except Exception:
        pass

    return "\n".join(lines)


async def _handle_positions(settings: Settings) -> str:
    """Handle /positions command."""
    import aiosqlite
    lines = ["<b>Open Positions</b>\n"]
    try:
        async with aiosqlite.connect(f"file:{settings.SNIPER_DB_PATH}?mode=ro", uri=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM positions WHERE status='open' ORDER BY opened_at DESC")
            positions = [dict(r) for r in await cursor.fetchall()]

            if not positions:
                return "No open positions"

            for p in positions:
                name = p["token_name"]
                ticker = p.get("ticker", "")
                entry = p["entry_sol"]
                opened = p["opened_at"][:16] if p.get("opened_at") else "?"
                manual = "MANUAL" if p.get("manual") else "BOT"
                tier = p.get("partial_exit_tier", 0)
                mcap = p.get("entry_mcap_usd", 0)
                mcap_str = f"${mcap:,.0f}" if mcap else "—"
                lines.append(
                    f"<b>{name}</b> ({ticker})\n"
                    f"  {entry:.3f} SOL | {manual} | Tier {tier}\n"
                    f"  Entry MC: {mcap_str} | {opened}"
                )
    except Exception as e:
        lines.append(f"Error: {e}")

    return "\n\n".join(lines)


async def _handle_close(token_query: str, session: aiohttp.ClientSession, settings: Settings) -> str:
    """Handle /close <token> command."""
    if not token_query:
        return "Usage: /close <token name or ticker>"

    import aiosqlite
    from sniper.executor import execute_sell
    from solders.keypair import Keypair
    from solana.rpc.async_api import AsyncClient

    try:
        async with aiosqlite.connect(str(settings.SNIPER_DB_PATH)) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM positions WHERE status='open' AND "
                "(LOWER(token_name) LIKE ? OR LOWER(ticker) LIKE ?) LIMIT 1",
                (f"%{token_query.lower()}%", f"%{token_query.lower()}%"),
            )
            row = await cursor.fetchone()
            if not row:
                return f"No open position matching '{token_query}'"

            pos = dict(row)
            name = pos["token_name"]
            token_amount = int(pos["entry_token_amount"])
            contract = pos["contract_address"]
            entry_sol = pos["entry_sol"]
            pos_id = pos["id"]

            kp = Keypair.from_json(open(settings.KEYPAIR_PATH).read())
            client = AsyncClient(settings.SOLANA_RPC_URL)

            tx_sig, sol_received = await execute_sell(
                client, kp, session, contract, token_amount, settings,
            )
            pnl_sol = sol_received - entry_sol
            pnl_pct = (pnl_sol / entry_sol * 100) if entry_sol > 0 else 0

            now = datetime.now(timezone.utc).isoformat()
            await db.execute(
                """UPDATE positions SET exit_sol=?, exit_reason='manual_telegram',
                   status='closed', pnl_sol=?, pnl_pct=?, closed_at=? WHERE id=?""",
                (sol_received, pnl_sol, pnl_pct, now, pos_id),
            )
            await db.execute(
                "INSERT INTO trades (position_id, side, sol_amount, token_amount, tx_signature, executed_at) "
                "VALUES (?, 'sell', ?, ?, ?, ?)",
                (pos_id, sol_received, float(token_amount), tx_sig, now),
            )
            await db.commit()
            await client.close()

            return (
                f"Closed <b>{name}</b>\n"
                f"Received: {sol_received:.4f} SOL\n"
                f"PnL: <b>{pnl_sol:+.4f} SOL ({pnl_pct:+.1f}%)</b>\n"
                f"TX: {tx_sig[:20]}..."
            )
    except Exception as e:
        return f"Close failed: {e}"


async def _handle_balance(settings: Settings) -> str:
    """Handle /balance command."""
    try:
        from solders.keypair import Keypair
        from solana.rpc.async_api import AsyncClient
        kp = Keypair.from_json(open(settings.KEYPAIR_PATH).read())
        client = AsyncClient(settings.SOLANA_RPC_URL)
        resp = await client.get_balance(kp.pubkey())
        bal = resp.value / 1e9
        await client.close()
        return f"Wallet: <code>{str(kp.pubkey())[:12]}...</code>\nBalance: <b>{bal:.4f} SOL</b>"
    except Exception as e:
        return f"Error: {e}"


async def _handle_pause(session: aiohttp.ClientSession, settings: Settings) -> str:
    """Handle /pause command — stop buying new tokens."""
    import os
    with open(_PAUSE_FILE, "w") as f:
        f.write(datetime.now(timezone.utc).isoformat())
    return "⏸ <b>PAUSED</b> — bot will NOT buy new tokens.\nExisting positions still managed.\nSend /resume to restart buying."


async def _handle_resume(session: aiohttp.ClientSession, settings: Settings) -> str:
    """Handle /resume command — resume buying."""
    import os
    try:
        os.remove(_PAUSE_FILE)
    except FileNotFoundError:
        pass
    return "▶️ <b>RESUMED</b> — bot is buying again."


async def _handle_closeall(session: aiohttp.ClientSession, settings: Settings) -> str:
    """Handle /closeall command — emergency liquidate all positions."""
    import aiosqlite
    from sniper.executor import execute_sell
    from solders.keypair import Keypair
    from solana.rpc.async_api import AsyncClient

    try:
        async with aiosqlite.connect(str(settings.SNIPER_DB_PATH)) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM positions WHERE status='open'")
            positions = [dict(r) for r in await cursor.fetchall()]

            if not positions:
                return "No open positions to close."

            kp = Keypair.from_json(open(settings.KEYPAIR_PATH).read())
            client = AsyncClient(settings.SOLANA_RPC_URL)
            results = []

            for pos in positions:
                try:
                    tx_sig, sol_received = await execute_sell(
                        client, kp, session, pos["contract_address"],
                        int(pos["entry_token_amount"]), settings,
                    )
                    pnl_sol = sol_received - pos["entry_sol"]
                    pnl_pct = (pnl_sol / pos["entry_sol"] * 100) if pos["entry_sol"] > 0 else 0
                    now = datetime.now(timezone.utc).isoformat()
                    await db.execute(
                        """UPDATE positions SET exit_sol=?, exit_reason='emergency_closeall',
                           status='closed', pnl_sol=?, pnl_pct=?, closed_at=? WHERE id=?""",
                        (sol_received, pnl_sol, pnl_pct, now, pos["id"]),
                    )
                    results.append(f"✅ {pos['token_name']}: {pnl_sol:+.4f} SOL ({pnl_pct:+.1f}%)")
                except Exception as e:
                    results.append(f"❌ {pos['token_name']}: {e}")

            await db.commit()
            await client.close()

            return f"<b>EMERGENCY CLOSE ALL</b>\n\n" + "\n".join(results)
    except Exception as e:
        return f"Close all failed: {e}"


def is_paused() -> bool:
    """Check if trading is paused. Called from main.py buy loop."""
    import os
    return os.path.exists(_PAUSE_FILE)


HELP_TEXT = """<b>Sniper Bot Commands</b>

/status — open positions, PnL, balance
/positions — detailed open positions
/close &lt;name&gt; — close position (e.g. /close wojak)
/closeall — emergency liquidate ALL positions
/pause — stop buying (positions still managed)
/resume — resume buying
/balance — wallet balance
/help — this message"""


async def telegram_command_loop(settings: Settings, shutdown: asyncio.Event) -> None:
    """Main loop — poll Telegram for commands and respond."""
    global _last_update_id

    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        logger.info("Telegram bot disabled — no token/chat configured")
        return

    logger.info("Telegram command bot started")

    async with aiohttp.ClientSession() as session:
        while not shutdown.is_set():
            try:
                updates = await _get_updates(session, settings)
                for update in updates:
                    _last_update_id = update["update_id"]
                    msg = update.get("message", {})
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    text = (msg.get("text") or "").strip()

                    # Only respond to our chat
                    if chat_id != settings.TELEGRAM_CHAT_ID:
                        continue

                    if not text.startswith("/"):
                        continue

                    parts = text.split(maxsplit=1)
                    cmd = parts[0].lower().split("@")[0]  # handle /cmd@botname
                    arg = parts[1] if len(parts) > 1 else ""

                    if cmd == "/status":
                        reply = await _handle_status(session, settings)
                    elif cmd == "/positions":
                        reply = await _handle_positions(settings)
                    elif cmd == "/close":
                        reply = await _handle_close(arg, session, settings)
                    elif cmd == "/closeall":
                        reply = await _handle_closeall(session, settings)
                    elif cmd == "/pause":
                        reply = await _handle_pause(session, settings)
                    elif cmd == "/resume":
                        reply = await _handle_resume(session, settings)
                    elif cmd == "/balance":
                        reply = await _handle_balance(settings)
                    elif cmd == "/help" or cmd == "/start":
                        reply = HELP_TEXT
                    else:
                        reply = f"Unknown command: {cmd}\nTry /help"

                    await _send(session, settings, reply)

            except Exception as e:
                logger.debug("Telegram bot error", error=str(e))

            await asyncio.sleep(2)

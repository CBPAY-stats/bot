import asyncio
import logging
import os
from typing import List, Tuple

import aiosqlite
from stellar_sdk import Server
from aiohttp import web

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ================== CONFIG ==================

HORIZON_URL = "https://horizon.livenet.xdbchain.com"
NETWORK_PASSPHRASE = "LiveNet Global XDBChain Network ; November 2023"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_PATH = "xdb_alert_bot.db"

server = Server(HORIZON_URL)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ================== DB HELPERS ==================

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS watches (
    chat_id     INTEGER NOT NULL,
    account_id  TEXT NOT NULL,
    min_amount  REAL NOT NULL,
    cursor      TEXT,
    PRIMARY KEY (chat_id, account_id)
);
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_TABLE_SQL)
        await db.commit()


async def add_watch(chat_id: int, account_id: str, min_amount: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO watches (chat_id, account_id, min_amount, cursor)
            VALUES (?, ?, ?, COALESCE(
                (SELECT cursor FROM watches WHERE chat_id = ? AND account_id = ?),
                'now'
            ))
            """,
            (chat_id, account_id, min_amount, chat_id, account_id),
        )
        await db.commit()


async def remove_watch(chat_id: int, account_id: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM watches WHERE chat_id = ? AND account_id = ?",
            (chat_id, account_id),
        )
        await db.commit()
        return cur.rowcount


async def list_watches(chat_id: int) -> List[Tuple[str, float, str]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT account_id, min_amount, COALESCE(cursor, '') FROM watches WHERE chat_id = ?",
            (chat_id,),
        )
        rows = await cur.fetchall()
        return rows


async def get_all_watches() -> List[Tuple[int, str, float, str]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT chat_id, account_id, min_amount, COALESCE(cursor, 'now') FROM watches"
        )
        rows = await cur.fetchall()
        return rows


async def update_cursor(chat_id: int, account_id: str, cursor: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE watches SET cursor = ? WHERE chat_id = ? AND account_id = ?",
            (cursor, chat_id, account_id),
        )
        await db.commit()


# ================== TELEGRAM HANDLERS ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 *Welcome to the XDB Alert Bot!*\n\n"
        "I monitor XDB blockchain accounts and alert you when payments above your threshold occur.\n\n"
        "*Available commands:*\n"
        "`/watch <ACCOUNT_ID> <MIN_AMOUNT>` – Start tracking an account (XDB native payments)\n"
        "`/unwatch <ACCOUNT_ID>` – Stop tracking an account\n"
        "`/list` – Show your tracked accounts\n"
        "\nExample:\n`/watch GDZ... 500000`\n"
    )
    await update.message.reply_markdown(msg)


async def watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /watch <ACCOUNT_ID> <MIN_AMOUNT>\nExample: /watch GDZ... 1000000"
        )
        return

    account_id = context.args[0].strip()
    try:
        min_amount = float(context.args[1])
    except ValueError:
        await update.message.reply_text("MIN_AMOUNT must be a number (example: 1000000).")
        return

    if not (account_id.startswith("G") and len(account_id) >= 20):
        await update.message.reply_text("Invalid XDB account address.")
        return

    await add_watch(chat_id, account_id, min_amount)
    await update.message.reply_text(
        f"🔔 Now watching:\n`{account_id}`\n"
        f"I will notify you for payments ≥ *{min_amount} XDB*.",
        parse_mode="Markdown"
    )


async def unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if len(context.args) < 1:
        await update.message.reply_text("Usage: /unwatch <ACCOUNT_ID>")
        return

    account_id = context.args[0].strip()
    removed = await remove_watch(chat_id, account_id)

    if removed:
        await update.message.reply_text(f"❎ Stopped watching:\n`{account_id}`", parse_mode="Markdown")
    else:
        await update.message.reply_text("This account was not being watched.")


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = await list_watches(chat_id)

    if not rows:
        await update.message.reply_text("You are not watching any accounts yet.")
        return

    lines = ["📋 *Tracked accounts:*"]
    for account_id, min_amt, cursor in rows:
        lines.append(f"- `{account_id}` (min: {min_amt} XDB)")
    await update.message.reply_markdown("\n".join(lines))


# ================== WATCHER (BACKGROUND ALERT LOOP) ==================

async def watcher_loop(application):
    await init_db()
    await asyncio.sleep(3)
    logger.info("Watcher started.")

    while True:
        try:
            watches = await get_all_watches()
            if not watches:
                await asyncio.sleep(5)
                continue

            for chat_id, account_id, min_amount, cursor in watches:
                payments = (
                    server.payments()
                    .for_account(account_id)
                    .order("asc")
                    .cursor(cursor)
                    .limit(50)
                    .call()
                )
                records = payments.get("_embedded", {}).get("records", [])
                if not records:
                    continue

                last_token = None

                for p in records:
                    last_token = p.get("paging_token")

                    if p["type"] != "payment":
                        continue
                    if p.get("asset_type") != "native":
                        continue

                    amount = float(p.get("amount", "0"))
                    if amount < min_amount:
                        continue

                    from_addr = p.get("from")
                    to_addr = p.get("to")
                    tx_hash = p.get("transaction_hash")
                    created_at = p.get("created_at")

                    text = (
                        "💸 *XDB Payment Alert!*\n\n"
                        f"*Account:* `{account_id}`\n"
                        f"*Amount:* *{amount} XDB*\n"
                        f"*From:* `{from_addr}`\n"
                        f"*To:* `{to_addr}`\n"
                        f"*Date:* `{created_at}`\n"
                        f"*Tx:* `{tx_hash}`"
                    )

                    await application.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode="Markdown"
                    )

                if last_token:
                    await update_cursor(chat_id, account_id, last_token)

            await asyncio.sleep(3)

        except Exception as e:
            logger.exception(f"watcher_loop error: {e}")
            await asyncio.sleep(5)


# ================== MINI HTTP SERVER FOR RENDER ==================

async def health_handler(request):
    return web.Response(text="OK")


async def start_http_server():
    app = web.Application()
    app.add_routes([web.get("/", health_handler), web.get("/healthz", health_handler)])

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    logger.info(f"Health HTTP server listening on port {port}")

    # keep running
    while True:
        await asyncio.sleep(3600)


# ================== MAIN ==================

async def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Please set TELEGRAM_BOT_TOKEN in your environment.")

    await init_db()

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("watch", watch))
    app.add_handler(CommandHandler("unwatch", unwatch))
    app.add_handler(CommandHandler("list", list_cmd))

    # Run bot + HTTP health server in parallel
    bot_task = asyncio.create_task(app.run_polling(close_loop=False))
    http_task = asyncio.create_task(start_http_server())

    await asyncio.gather(bot_task, http_task)


if __name__ == "__main__":
    asyncio.run(main())

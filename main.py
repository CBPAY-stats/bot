import logging
import os
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import List, Tuple

from stellar_sdk import Server as XDBServer
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

xdb_server = XDBServer(HORIZON_URL)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ================== DB HELPERS (sqlite3 síncrono) ==================

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS watches (
    chat_id     INTEGER NOT NULL,
    account_id  TEXT NOT NULL,
    min_amount  REAL NOT NULL,
    cursor      TEXT,
    PRIMARY KEY (chat_id, account_id)
);
"""


def init_db():
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(CREATE_TABLE_SQL)
        conn.commit()
    finally:
        conn.close()


def add_watch(chat_id: int, account_id: str, min_amount: float):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO watches (chat_id, account_id, min_amount, cursor)
            VALUES (?, ?, ?, 'init')
            """,
            (chat_id, account_id, min_amount),
        )
        conn.commit()
    finally:
        conn.close()


def remove_watch(chat_id: int, account_id: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(
            "DELETE FROM watches WHERE chat_id = ? AND account_id = ?",
            (chat_id, account_id),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def list_watches_db(chat_id: int) -> List[Tuple[str, float, str]]:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(
            "SELECT account_id, min_amount, COALESCE(cursor, '') FROM watches WHERE chat_id = ?",
            (chat_id,),
        )
        return cur.fetchall()
    finally:
        conn.close()


def get_all_watches() -> List[Tuple[int, str, float, str]]:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(
            "SELECT chat_id, account_id, min_amount, COALESCE(cursor, 'init') FROM watches"
        )
        return cur.fetchall()
    finally:
        conn.close()


def update_cursor(chat_id: int, account_id: str, cursor: str):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "UPDATE watches SET cursor = ? WHERE chat_id = ? AND account_id = ?",
            (cursor, chat_id, account_id),
        )
        conn.commit()
    finally:
        conn.close()


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

    add_watch(chat_id, account_id, min_amount)
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
    removed = remove_watch(chat_id, account_id)

    if removed:
        await update.message.reply_text(f"❎ Stopped watching:\n`{account_id}`", parse_mode="Markdown")
    else:
        await update.message.reply_text("This account was not being watched.")


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = list_watches_db(chat_id)

    if not rows:
        await update.message.reply_text("You are not watching any accounts yet.")
        return

    lines = ["📋 *Tracked accounts:*"]
    for account_id, min_amt, cursor in rows:
        lines.append(f"- `{account_id}` (min: {min_amt} XDB)")
    await update.message.reply_markdown("\n".join(lines))


# ================== WATCHER JOB (usa JobQueue) ==================

async def watcher_job(context: ContextTypes.DEFAULT_TYPE):
    """Runs every few seconds via JobQueue, checks Horizon and sends alerts."""
    try:
        watches = get_all_watches()
        if not watches:
            return

        for chat_id, account_id, min_amount, cursor in watches:
            # Se cursor ainda está em estado inicial, faz só sync e não alerta
            if cursor in (None, "", "init", "now"):
                payments = (
                    xdb_server.payments()
                    .for_account(account_id)
                    .order("asc")
                    .limit(50)
                    .call()
                )
                records = payments.get("_embedded", {}).get("records", [])
                if not records:
                    continue

                last_token = records[-1].get("paging_token")
                if last_token:
                    update_cursor(chat_id, account_id, last_token)
                # Não enviar alertas nesta primeira sync
                continue

            # A partir daqui, já temos cursor real → só novos movimentos
            payments = (
                xdb_server.payments()
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

                if p.get("type") != "payment":
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

                await context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="Markdown"
                )

            if last_token:
                update_cursor(chat_id, account_id, last_token)

    except Exception as e:
        logger.exception(f"Error in watcher_job: {e}")


# ================== MINI HTTP SERVER (HEALTHCHECK) ==================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")


def start_health_server():
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"Health HTTP server listening on port {port}")
    server.serve_forever()


# ================== MAIN ==================

def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Please set TELEGRAM_BOT_TOKEN in your environment.")

    # DB
    init_db()

    # Healthcheck HTTP server numa thread separada
    t = threading.Thread(target=start_health_server, daemon=True)
    t.start()

    # Telegram bot
    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("watch", watch))
    application.add_handler(CommandHandler("unwatch", unwatch))
    application.add_handler(CommandHandler("list", list_cmd))

    # Job que corre o watcher a cada 3 segundos
    application.job_queue.run_repeating(watcher_job, interval=3, first=5)

    # Este método gere o event loop internamente – nada de asyncio.run aqui
    application.run_polling()


if __name__ == "__main__":
    main()


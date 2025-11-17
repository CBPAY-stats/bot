import os
import json
import asyncio
import logging
import sqlite3
import threading
from datetime import datetime

from flask import Flask
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes
)
from stellar_sdk import Server


# ------------------------------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------------------------
# DATABASE
# ------------------------------------------------------------------------------------
DB_FILE = "xdb_alerts.db"


def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            chat_id INTEGER,
            account TEXT,
            asset_code TEXT,
            asset_issuer TEXT,
            min_amount REAL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS seen_txs (
            tx_id TEXT PRIMARY KEY
        )
    """)

    conn.commit()
    conn.close()


# ------------------------------------------------------------------------------------
# ASSET LIST – XDB CHAIN (CURATED)
# ------------------------------------------------------------------------------------
XDB_ASSETS = {
    "XDB": {"code": "XDB", "issuer": None},
    "CBPAY": {"code": "CBPAY", "issuer": "GD7PT6VAXH227WBYR5KN3OYKGSNXVETMYZUP3R62DFX3BBC7GGOBDFJ2"},
    "BEEFI": {"code": "BEEFI", "issuer": "GA6E22J3MFL5WZELZ64XQFI42CPJ6V7NMSGXAQKUJC4274GTSRRGUUXZ"},
    "HONEY": {"code": "HONEY", "issuer": "GAZ5BEQZI67UEJAUEFT7IQHU7A4FXWJDULHLMICLHQ5RF3KLWIRMFZQP"},
}

# ------------------------------------------------------------------------------------
# HEALTH SERVER
# ------------------------------------------------------------------------------------
app = Flask(__name__)


@app.get("/")
def root():
    return "Bot is running!", 200


def start_health_server():
    app.run(host="0.0.0.0", port=10000)


# ------------------------------------------------------------------------------------
# TELEGRAM COMMANDS
# ------------------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome! I monitor XDB Chain payments.\n\n"
        "Use /watch to track payments:\n\n"
        "Format:\n"
        "`/watch ACCOUNT MIN_AMOUNT`\n\n"
        "Example:\n"
        "`/watch GDZYYA...WFV6 2000000`\n\n"
        "Then choose the asset you want to monitor.",
        parse_mode="Markdown"
    )


async def watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Usage:\n`/watch ACCOUNT MIN_AMOUNT`",
            parse_mode="Markdown"
        )
        return

    account = context.args[0]
    min_amount = float(context.args[1])

    context.user_data["watch_account"] = account
    context.user_data["watch_amount"] = min_amount

    # ASSET SELECTION MENU
    keyboard = [
        [InlineKeyboardButton(asset, callback_data=f"asset:{asset}")]
        for asset in XDB_ASSETS.keys()
    ]

    await update.message.reply_text(
        "Select the asset you want to monitor:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def asset_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    selected = query.data.split(":")[1]

    account = context.user_data["watch_account"]
    min_amount = context.user_data["watch_amount"]

    asset_info = XDB_ASSETS[selected]

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("""
        INSERT INTO alerts (chat_id, account, asset_code, asset_issuer, min_amount)
        VALUES (?, ?, ?, ?, ?)
    """, (query.message.chat.id, account, asset_info["code"], asset_info["issuer"], min_amount))

    conn.commit()
    conn.close()

    await query.edit_message_text(
        f"✅ Alert created!\n\n"
        f"Account: `{account}`\n"
        f"Asset: **{selected}**\n"
        f"Minimum Amount: `{min_amount}` XDB",
        parse_mode="Markdown"
    )


# ------------------------------------------------------------------------------------
# BLOCKCHAIN MONITOR
# ------------------------------------------------------------------------------------
server = Server("https://api.mainnet-v2.xdbchain.com")


async def watcher_job():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    alerts = c.execute("SELECT chat_id, account, asset_code, asset_issuer, min_amount FROM alerts").fetchall()

    for chat_id, account, asset_code, asset_issuer, min_amount in alerts:
        txs = server.payments().for_account(account).limit(20).order(desc=True).call()

        for tx in txs["_embedded"]["records"]:
            txid = tx["id"]

            # skip if already sent
            if c.execute("SELECT 1 FROM seen_txs WHERE tx_id = ?", (txid,)).fetchone():
                continue

            # check asset match
            tx_asset = tx.get("asset_code", "XDB")
            tx_issuer = tx.get("asset_issuer")

            if tx_asset != asset_code:
                continue

            if asset_issuer and tx_issuer != asset_issuer:
                continue

            amount = float(tx["amount"])
            if amount < min_amount:
                continue

            # notify user
            message = (
                "💸 *XDB Chain Payment Alert!*\n\n"
                f"*Account:* `{account}`\n"
                f"*Asset:* `{asset_code}`\n"
                f"*Amount:* `{amount}`\n"
                f"*From:* `{tx['from']}`\n"
                f"*To:* `{tx['to']}`\n"
                f"*Date:* `{tx['created_at']}`\n"
                f"*Tx:* `{txid}`"
            )

            c.execute("INSERT INTO seen_txs (tx_id) VALUES (?)", (txid,))
            await context_app.bot.send_message(chat_id, message, parse_mode="Markdown")

    conn.commit()
    conn.close()


# ------------------------------------------------------------------------------------
# BOT RUNNER (CORRECT)
# ------------------------------------------------------------------------------------
async def run_bot():
    global context_app

    app = ApplicationBuilder().token(os.getenv("BOT_TOKEN")).build()
    context_app = app

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("watch", watch))
    app.add_handler(CallbackQueryHandler(asset_selected))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(watcher_job, "interval", seconds=4)
    scheduler.start()

    await app.run_polling(drop_pending_updates=True)


def main():
    init_db()

    # start health server
    threading.Thread(target=start_health_server, daemon=True).start()

    asyncio.run(run_bot())


if __name__ == "__main__":
    main()

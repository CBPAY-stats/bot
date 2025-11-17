import asyncio
import logging
import sqlite3
import http.server
import socketserver
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes
)
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------------------------
# DATABASE
# ------------------------------------------------------------------------------------
DB = "xdb_alerts.db"

def init_db():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS watches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            account TEXT,
            asset_code TEXT,
            asset_issuer TEXT,
            min_amount REAL,
            last_tx TEXT
        )
    """)
    con.commit()
    con.close()

def db_execute(query, params=(), fetch=False):
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute(query, params)
    con.commit()
    rows = cur.fetchall() if fetch else None
    con.close()
    return rows

# ------------------------------------------------------------------------------------
# SUPPORTED ASSETS
# ------------------------------------------------------------------------------------
ASSETS = {
    "XDB": {
        "code": "XDB",
        "issuer": None,  # native
    },
    "CBPAY": {
        "code": "CBPAY",
        "issuer": "GD7PT6VAXH227WBYR5KN3OYKGSNXVETMYZUP3R62DFX3BBC7GGOBDFJ2",
    },
    "BEEFI": {
        "code": "BEEFI",
        "issuer": "GA6E22J3MFL5WZELZ64XQFI42CPJ6V7NMSGXAQKUJC4274GTSRRGUUXZ",
    },
    "HONEY": {
        "code": "HONEY",
        "issuer": "GAZ5BEQZI67UEJAUEFT7IQHU7A4FXWJDULHLMICLHQ5RF3KLWIRMFZQP",
    }
}

# ------------------------------------------------------------------------------------
# HORIZON API
# ------------------------------------------------------------------------------------
HORIZON = "https://api.xdbchain.com"

async def fetch_payments(account, asset_code, asset_issuer):
    """Fetch payments filtered by asset."""
    url = f"{HORIZON}/accounts/{account}/payments?order=desc&limit=20"

    async with httpx.AsyncClient() as client:
        r = await client.get(url, timeout=10)
        if r.status_code != 200:
            return []

        data = r.json().get("_embedded", {}).get("records", [])

    results = []
    for p in data:
        if p.get("type") != "payment":
            continue

        # native XDB
        if asset_code == "XDB":
            if p.get("asset_type") != "native":
                continue
        else:
            # alphanum asset
            if p.get("asset_code") != asset_code:
                continue
            if p.get("asset_issuer") != asset_issuer:
                continue

        results.append(p)

    return results

# ------------------------------------------------------------------------------------
# TELEGRAM BOT
# ------------------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to the XDB Chain Alerts Bot!\n\n"
        "Use /watch to monitor an account.\n"
        "Example:\n"
        "`/watch GDZ...ABC 2000000`\n\n"
        "I will then ask which asset you want to monitor.",
        parse_mode="Markdown"
    )

async def watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: user sends /watch ACCOUNT MIN_AMOUNT"""
    if len(context.args) != 2:
        await update.message.reply_text("Usage:\n/watch ACCOUNT MIN_AMOUNT")
        return

    account = context.args[0]
    min_amount = float(context.args[1])

    # store temporary values in user_data
    context.user_data["pending_watch"] = {
        "account": account,
        "min_amount": min_amount
    }

    # send asset selection buttons
    keyboard = [
        [
            InlineKeyboardButton("XDB", callback_data="asset_XDB"),
            InlineKeyboardButton("CBPAY", callback_data="asset_CBPAY"),
        ],
        [
            InlineKeyboardButton("BEEFI", callback_data="asset_BEEFI"),
            InlineKeyboardButton("HONEY", callback_data="asset_HONEY"),
        ]
    ]
    await update.message.reply_text(
        "Choose the asset you want to monitor:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def asset_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2: user clicks an asset button."""
    query = update.callback_query
    await query.answer()

    asset_key = query.data.split("_")[1]  # e.g., asset_XDB → XDB
    info = context.user_data.get("pending_watch")

    if not info:
        await query.edit_message_text("Session expired. Please use /watch again.")
        return

    account = info["account"]
    min_amount = info["min_amount"]
    asset_code = ASSETS[asset_key]["code"]
    issuer = ASSETS[asset_key]["issuer"]

    # Insert into DB
    db_execute("""
        INSERT INTO watches (chat_id, account, asset_code, asset_issuer, min_amount, last_tx)
        VALUES (?, ?, ?, ?, ?, NULL)
    """, (query.message.chat_id, account, asset_code, issuer, min_amount))

    await query.edit_message_text(
        f"🔔 Monitoring enabled!\n\n"
        f"**Account:** `{account}`\n"
        f"**Asset:** `{asset_code}`\n"
        f"**Minimum Amount:** `{min_amount}`\n\n"
        f"I will notify you when a new payment meets the criteria.",
        parse_mode="Markdown"
    )

    del context.user_data["pending_watch"]

# ------------------------------------------------------------------------------------
# MONITORING JOB
# ------------------------------------------------------------------------------------
async def watcher_job():
    rows = db_execute("SELECT id, chat_id, account, asset_code, asset_issuer, min_amount, last_tx FROM watches", fetch=True)

    for wid, chat_id, account, asset_code, issuer, min_amount, last_tx in rows:
        try:
            payments = await fetch_payments(account, asset_code, issuer)
        except:
            continue

        if not payments:
            continue

        latest = payments[0]
        txid = latest.get("transaction_hash")

        # skip already processed or old
        if last_tx == txid:
            continue

        # check amount
        amount = float(latest.get("amount", 0))
        if amount < min_amount:
            continue

        # Save new last_tx
        db_execute("UPDATE watches SET last_tx=? WHERE id=?", (txid, wid))

        # send alert
        text = (
            f"💸 *XDB Asset Payment Alert!*\n\n"
            f"*Asset:* `{asset_code}`\n"
            f"*Account:* `{account}`\n"
            f"*Amount:* `{amount}`\n"
            f"*From:* `{latest.get('from')}`\n"
            f"*To:* `{latest.get('to')}`\n"
            f"*Date:* `{latest.get('created_at')}`\n"
            f"*Transaction:* `{txid}`"
        )

        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    data={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
                )
        except:
            pass

# ------------------------------------------------------------------------------------
# HEALTH SERVER
# ------------------------------------------------------------------------------------
class HealthHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def start_health_server():
    PORT = 10000
    server = socketserver.TCPServer(("", PORT), HealthHandler)
    logger.info(f"Health server on port {PORT}")
    server.serve_forever()

# ------------------------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------------------------
BOT_TOKEN = "8240598081:AAH7RGt1c2KkTUQ4F5dsEs0OEgtgbQhgIbQ"

async def run_bot():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("watch", watch))
    app.add_handler(CallbackQueryHandler(asset_selected))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(watcher_job, "interval", seconds=4)
    scheduler.start()

    await app.run_polling()

def main():
    init_db()
    asyncio.get_event_loop().create_task(run_bot())

    # start health server (separate thread)
    import threading
    threading.Thread(target=start_health_server, daemon=True).start()

    asyncio.get_event_loop().run_forever()

if __name__ == "__main__":
    main()


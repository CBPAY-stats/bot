import os
import threading
import asyncio
from flask import Flask
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.ext import ApplicationBuilder, CommandHandler
from stellar_sdk import Server

# ---------------------------------------------------------
# CONFIG
# ---------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable missing!")

server = Server("https://api.xdbchain.com")

WATCHLIST = {}  # addr → (threshold, asset_code, issuer)

# ---------------------------------------------------------
# FLASK HEALTH CHECK
# ---------------------------------------------------------
app_flask = Flask(__name__)

@app_flask.route("/")
def health():
    return "Bot is running!", 200

def start_health_server():
    app_flask.run(host="0.0.0.0", port=10000, debug=False)

# ---------------------------------------------------------
# TELEGRAM COMMANDS
# ---------------------------------------------------------
async def start(update, context):
    await update.message.reply_text(
        "✅ XDB Alerts Bot is running.\nUse:\n"
        "/watch ADDRESS AMOUNT ASSET\n"
        "/unwatch ADDRESS"
    )

async def watch(update, context):
    if len(context.args) < 3:
        return await update.message.reply_text("Usage: /watch ADDRESS AMOUNT ASSET")

    addr = context.args[0].upper()
    amount = float(context.args[1])
    asset = context.args[2].upper()

    WATCHLIST[addr] = (amount, asset)
    await update.message.reply_text(f"✓ Watching {addr} for {amount}+ {asset}")

async def unwatch(update, context):
    if not context.args:
        return await update.message.reply_text("Usage: /unwatch ADDRESS")

    addr = context.args[0].upper()
    WATCHLIST.pop(addr, None)

    await update.message.reply_text(f"✓ Removed {addr} from watchlist")

# ---------------------------------------------------------
# WATCHER JOB
# ---------------------------------------------------------
async def check_payments():
    for addr, (threshold, asset) in WATCHLIST.items():

        payments = (
            server.payments()
            .for_account(addr)
            .order(desc=True)
            .limit(5)
            .call()
            ["records"]
        )

        for p in payments:
            # XDB native
            if asset == "XDB" and p["type"] == "payment" and p["asset_type"] == "native":
                amount = float(p["amount"])
                if amount >= threshold:
                    print("ALERT XDB:", amount)

            # Tokens (assets)
            if (
                asset != "XDB"
                and p["type"] == "payment"
                and p["asset_code"].upper() == asset
            ):
                amount = float(p["amount"])
                if amount >= threshold:
                    print("ALERT TOKEN:", amount)

# ---------------------------------------------------------
# MAIN APP
# ---------------------------------------------------------
async def run_bot():
    tg = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    tg.add_handler(CommandHandler("start", start))
    tg.add_handler(CommandHandler("watch", watch))
    tg.add_handler(CommandHandler("unwatch", unwatch))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_payments, "interval", seconds=4)
    scheduler.start()

    print("🔵 Telegram Bot starting polling...")
    await tg.run_polling(close_loop=False)

def main():
    # Start health server in thread
    threading.Thread(target=start_health_server, daemon=True).start()

    # Run bot in existing event loop
    loop = asyncio.get_event_loop()
    loop.create_task(run_bot())
    loop.run_forever()

if __name__ == "__main__":
    main()

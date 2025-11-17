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

# WATCHLIST structure:
# address → { "threshold": float, "asset": "XDB" or CODE }
WATCHLIST = {}

# ---------------------------------------------------------
# FLASK HEALTH CHECK
# ---------------------------------------------------------

flask_app = Flask(__name__)

@flask_app.route("/")
def health():
    return "Bot is running OK!", 200

def start_flask():
    flask_app.run(host="0.0.0.0", port=10000, debug=False)

# ---------------------------------------------------------
# TELEGRAM COMMANDS
# ---------------------------------------------------------

async def start(update, context):
    await update.message.reply_text(
        "🤖 XDB Alert Bot is ONLINE!\n\n"
        "Commands:\n"
        "/watch ADDRESS AMOUNT ASSET\n"
        "Example:\n"
        "/watch GDZ... 2000000 XDB\n\n"
        "/unwatch ADDRESS\n"
        "/list"
    )

async def list_cmd(update, context):
    if not WATCHLIST:
        return await update.message.reply_text("No addresses being monitored.")

    msg = "📡 Currently watching:\n\n"
    for addr, data in WATCHLIST.items():
        msg += f"{addr}\n • Threshold: {data['threshold']}\n • Asset: {data['asset']}\n\n"

    await update.message.reply_text(msg)

async def watch(update, context):
    if len(context.args) < 3:
        return await update.message.reply_text("Usage:\n/watch ADDRESS AMOUNT ASSET")

    address = context.args[0].upper()
    amount = float(context.args[1])
    asset = context.args[2].upper()

    WATCHLIST[address] = {
        "threshold": amount,
        "asset": asset
    }

    await update.message.reply_text(
        f"✅ Now watching:\n{address}\nThreshold: {amount} {asset}"
    )

async def unwatch(update, context):
    if not context.args:
        return await update.message.reply_text("Usage:\n/unwatch ADDRESS")

    addr = context.args[0].upper()
    WATCHLIST.pop(addr, None)

    await update.message.reply_text(f"❎ Removed {addr} from watchlist.")

# ---------------------------------------------------------
# PAYMENT CHECKER
# ---------------------------------------------------------

async def check_payments():
    for address, data in WATCHLIST.items():
        threshold = data["threshold"]
        asset = data["asset"]

        try:
            payments = (
                server.payments()
                .for_account(address)
                .order(desc=True)
                .limit(5)
                .call()["records"]
            )
        except Exception as e:
            print("ERROR calling Horizon:", e)
            continue

        for p in payments:

            # Native XDB
            if asset == "XDB" and p["type"] == "payment" and p.get("asset_type") == "native":
                amt = float(p["amount"])
                if amt >= threshold:
                    print("ALERT XDB:", amt)

            # Tokens
            if (
                asset != "XDB"
                and p["type"] == "payment"
                and p.get("asset_code", "").upper() == asset
            ):
                amt = float(p["amount"])
                if amt >= threshold:
                    print("ALERT TOKEN:", amt)

# ---------------------------------------------------------
# TELEGRAM BOT RUNNER
# ---------------------------------------------------------

async def run_bot():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("watch", watch))
    app.add_handler(CommandHandler("unwatch", unwatch))

    # JOB SCHEDULER
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_payments, "interval", seconds=4)
    scheduler.start()

    print("🤖 Telegram polling started...")
    await app.run_polling()

# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------

def main():
    # Start FLASK in separate thread
    threading.Thread(target=start_flask, daemon=True).start()

    # Start telegram bot
    asyncio.run(run_bot())

if __name__ == "__main__":
    main()

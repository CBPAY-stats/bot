import os
import threading
import asyncio
from typing import Dict, Any

from flask import Flask
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)
from stellar_sdk import Server


# ---------------------------------------------------------
# CONFIG
# ---------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable missing!")

STELLAR_HORIZON_URL = "https://horizon.stellar.org"

WATCHLIST: Dict[str, Dict[str, Any]] = {}

server = Server(STELLAR_HORIZON_URL)

telegram_app = None


# ---------------------------------------------------------
# FLASK
# ---------------------------------------------------------

flask_app = Flask(__name__)


@flask_app.get("/")
def index():
    return "✅ XDB Bot is running!", 200


@flask_app.get("/health")
def health():
    return "OK", 200


def start_flask():
    port = int(os.environ.get("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port, debug=False)


# ---------------------------------------------------------
# TELEGRAM HANDLERS
# ---------------------------------------------------------

HELP_TEXT = (
    "👋 Olá! Eu sou o XDB Bot.\n\n"
    "Comandos disponíveis:\n"
    "/start – mensagem de boas-vindas\n"
    "/help – mostra esta ajuda\n"
    "/watch ADDRESS AMOUNT ASSET – vigiar saldo\n"
    "/unwatch ADDRESS – parar vigilância\n"
    "/list – listar vigilâncias\n"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 Bem-vindo ao XDB Bot!\n"
        "Usa /help para veres os comandos."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text(
            "❗ Uso:\n/watch ADDRESS AMOUNT ASSET\n\nExemplo:\n/watch GABC... 100 XLM"
        )
        return

    address = context.args[0].upper()

    try:
        amount = float(context.args[1])
    except ValueError:
        await update.message.reply_text("❗ AMOUNT tem de ser número.")
        return

    asset = context.args[2].upper()
    chat_id = update.effective_chat.id

    WATCHLIST[address] = {
        "threshold": amount,
        "asset": asset,
        "chat_id": chat_id,
    }

    await update.message.reply_text(
        f"📡 A vigiar:\n"
        f"• Address: {address}\n"
        f"• Threshold: {amount}\n"
        f"• Asset: {asset}"
    )


async def unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❗ Uso: /unwatch ADDRESS")
        return

    address = context.args[0].upper()

    if address in WATCHLIST:
        del WATCHLIST[address]
        await update.message.reply_text(f"🛑 Removido:\n{address}")
    else:
        await update.message.reply_text("❗ Esse address não está a ser vigiado.")


async def list_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not WATCHLIST:
        await update.message.reply_text("📭 Nenhum endereço em vigilância.")
        return

    msg = "📡 Endereços em vigilância:\n\n"
    for addr, d in WATCHLIST.items():
        msg += f"{addr}\n • Asset: {d['asset']}\n • Threshold: {d['threshold']}\n\n"

    await update.message.reply_text(msg)


# ---------------------------------------------------------
# STELLAR CHECK
# ---------------------------------------------------------

async def fetch_balance(address: str, asset_code: str) -> float:
    def _call():
        return server.accounts().account_id(address).call()

    account = await asyncio.to_thread(_call)
    balances = account.get("balances", [])

    asset_code = asset_code.upper()

    for b in balances:
        if asset_code == "XLM" and b.get("asset_type") == "native":
            return float(b["balance"])

        if b.get("asset_code", "").upper() == asset_code:
            return float(b["balance"])

    return 0.0


async def check_watchlist(context: ContextTypes.DEFAULT_TYPE):
    if not WATCHLIST:
        return

    items = list(WATCHLIST.items())

    for address, data in items:
        threshold = data["threshold"]
        asset = data["asset"]
        chat_id = data["chat_id"]

        try:
            balance = await fetch_balance(address, asset)
        except Exception as exc:
            await context.bot.send_message(
                chat_id,
                f"⚠️ Erro ao verificar {address}: {exc}"
            )
            continue

        if balance >= threshold:
            await context.bot.send_message(
                chat_id,
                "🎉 *ALERTA XDB*\n\n"
                f"`{address}` atingiu o threshold!\n"
                f"*Asset:* {asset}\n"
                f"*Atual:* {balance}\n"
                f"*Threshold:* {threshold}",
                parse_mode="Markdown",
            )

            WATCHLIST.pop(address, None)


# ---------------------------------------------------------
# BOOTSTRAP PTB
# ---------------------------------------------------------

def run_bot():
    global telegram_app

    application = ApplicationBuilder().token(BOT_TOKEN).build()
    telegram_app = application

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("watch", watch))
    application.add_handler(CommandHandler("unwatch", unwatch))
    application.add_handler(CommandHandler("list", list_watch))

    application.job_queue.run_repeating(check_watchlist, interval=60, first=10)

    application.run_polling(allowed_updates=Update.ALL_TYPES)


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------

def main():
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()

    run_bot()


if __name__ == "__main__":
    main()

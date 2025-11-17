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

# Horizon public network server
STELLAR_HORIZON_URL = "https://horizon.stellar.org"

# In-memory watchlist:
# {
#   address: {
#       "threshold": float,
#       "asset": str,      # e.g. "XLM" or custom asset code
#       "chat_id": int,
#   }
# }
WATCHLIST: Dict[str, Dict[str, Any]] = {}

server = Server(STELLAR_HORIZON_URL)

# Vamos guardar a app do Telegram aqui se for preciso noutros sítios
telegram_app = None


# ---------------------------------------------------------
# FLASK (para Render keep-alive / health checks)
# ---------------------------------------------------------

flask_app = Flask(__name__)


@flask_app.get("/")
def index():
    return "✅ XDB Bot is running!", 200


@flask_app.get("/health")
def health():
    return "OK", 200


def start_flask() -> None:
    """Arranca o servidor Flask (Render vai bater aqui)."""
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
    "   • ADDRESS: endereço Stellar\n"
    "   • AMOUNT: valor mínimo (ex: 100.5)\n"
    "   • ASSET: código do asset (ex: XLM)\n"
    "/unwatch ADDRESS – parar de vigiar um endereço\n"
    "/list – mostrar endereços em vigilância\n"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🚀 Bem-vindo ao XDB Bot!\n\n"
        "Vou ajudar a vigiar saldos na rede Stellar.\n\n"
        "Usa /help para veres os comandos disponíveis."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


async def watch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 3:
        await update.message.reply_text(
            "❗ Uso correto:\n"
            "/watch ADDRESS AMOUNT ASSET\n\n"
            "Exemplo:\n"
            "/watch GB.... 100 XLM"
        )
        return

    address = context.args[0].upper()
    try:
        amount = float(context.args[1])
    except ValueError:
        await update.message.reply_text("❗ AMOUNT tem de ser um número. Ex: 100 ou 250.5")
        return

    asset = context.args[2].upper()
    chat_id = update.effective_chat.id

    WATCHLIST[address] = {
        "threshold": amount,
        "asset": asset,
        "chat_id": chat_id,
    }

    await update.message.reply_text(
        "✅ Endereço adicionado à watchlist:\n"
        f"• Address: {address}\n"
        f"• Threshold: {amount}\n"
        f"• Asset: {asset}"
    )


async def unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Uso: /unwatch ADDRESS")
        return

    address = context.args[0].upper()

    if address in WATCHLIST:
        del WATCHLIST[address]
        await update.message.reply_text(f"✅ Deixei de vigiar o endereço:\n{address}")
    else:
        await update.message.reply_text("❗ Esse endereço não está a ser vigiado.")


async def list_watch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not WATCHLIST:
        await update.message.reply_text("📭 Não há nenhum endereço em vigilância.")
        return

    msg_lines = ["📡 Endereços atualmente em vigilância:\n"]
    for addr, data in WATCHLIST.items():
        msg_lines.append(
            f"{addr}\n"
            f" • Threshold: {data['threshold']}\n"
            f" • Asset: {data['asset']}\n"
        )

    await update.message.reply_text("\n".join(msg_lines))


# ---------------------------------------------------------
# STELLAR BALANCE CHECK
# ---------------------------------------------------------

async def fetch_balance(address: str, asset_code: str) -> float:
    """
    Devolve o saldo do asset_code para o address.
    Para XLM (native), usa 'native'. Para outros assets, compara pelo 'asset_code'.
    """
    # A chamada à Horizon é bloqueante, por isso vamos para uma thread separada.
    def _get_account():
        return server.accounts().account_id(address).call()

    account = await asyncio.to_thread(_get_account)
    balances = account.get("balances", [])

    asset_code = asset_code.upper()

    for b in balances:
        if asset_code == "XLM":
            if b.get("asset_type") == "native":
                return float(b["balance"])
        else:
            if b.get("asset_code", "").upper() == asset_code:
                return float(b["balance"])

    return 0.0


async def check_watchlist(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Job que corre periodicamente e verifica se algum saldo passou o threshold.
    """
    if not WATCHLIST:
        return

    # Copiamos items para evitar problemas se o dicionário mudar durante o loop
    items = list(WATCHLIST.items())

    for address, data in items:
        threshold = data["threshold"]
        asset = data["asset"]
        chat_id = data["chat_id"]

        try:
            balance = await fetch_balance(address, asset)
        except Exception as exc:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⚠️ Erro ao verificar saldo de {address} ({asset}):\n"
                    f"{exc}"
                ),
            )
            continue

        if balance >= threshold:
            # Envia alerta e remove da watchlist
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "🎉 *ALERTA XDB*\n\n"
                    f"O endereço:\n`{address}`\n\n"
                    f"atingiu o threshold definido:\n"
                    f"*Asset:* {asset}\n"
                    f"*Saldo atual:* {balance}\n"
                    f"*Threshold:* {threshold}"
                ),
                parse_mode="Markdown",
            )
            WATCHLIST.pop(address, None)


# ---------------------------------------------------------
# TELEGRAM BOOTSTRAP
# ---------------------------------------------------------

async def run_bot() -> None:
    global telegram_app

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    telegram_app = application

    # Comandos
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("watch", watch))
    application.add_handler(CommandHandler("unwatch", unwatch))
    application.add_handler(CommandHandler("list", list_watch))

    # Job para verificar a watchlist a cada 60 segundos
    application.job_queue.run_repeating(
        check_watchlist,
        interval=60,   # segundos
        first=10,      # atraso inicial
    )

    # Inicia o polling
    await application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        close_loop=False,
    )


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------

def main() -> None:
    # Start Flask num thread separado
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()

    # Start Telegram bot (async)
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()

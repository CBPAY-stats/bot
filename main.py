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

# Endpoint oficial da XDB Chain Mainnet (Horizon)
STELLAR_HORIZON_URL = "https://horizon.livenet.xdbchain.com"

# Estrutura em memória:
# {
#   address: {
#       "threshold": float,
#       "asset": str,      # ex: "XDB"
#       "chat_id": int,
#   }
# }
WATCHLIST: Dict[str, Dict[str, Any]] = {}

server = Server(STELLAR_HORIZON_URL)

telegram_app = None


# ---------------------------------------------------------
# FLASK - Healthcheck / Render
# ---------------------------------------------------------

flask_app = Flask(__name__)


@flask_app.get("/")
def index():
    return "✅ XDB Bot is running on XDB Chain!", 200


@flask_app.get("/health")
def health():
    return "OK", 200


def start_flask():
    """
    Arranca o servidor Flask. No Render, a PORT vem em variável de ambiente.
    """
    port = int(os.environ.get("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port, debug=False)


# ---------------------------------------------------------
# TELEGRAM HANDLERS
# ---------------------------------------------------------

HELP_TEXT = (
    "👋 Olá! Eu sou o XDB Bot (XDB Chain).\n\n"
    "Comandos disponíveis:\n"
    "/start – mensagem de boas-vindas\n"
    "/help – mostra esta ajuda\n"
    "/watch ADDRESS AMOUNT ASSET – vigiar saldo\n"
    "   • ADDRESS: endereço XDB Chain\n"
    "   • AMOUNT: valor mínimo (ex: 100.5)\n"
    "   • ASSET: código do asset (ex: XDB)\n"
    "/unwatch ADDRESS – parar vigilância\n"
    "/list – listar vigilâncias\n"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 Bem-vindo ao XDB Bot na XDB Chain!\n\n"
        "Vou ajudar a vigiar saldos de endereços na rede XDB.\n"
        "Usa /help para veres os comandos disponíveis."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /watch ADDRESS AMOUNT ASSET
    Ex: /watch GDZYYA... 100 XDB
    """
    if len(context.args) < 3:
        await update.message.reply_text(
            "❗ Uso correto:\n"
            "/watch ADDRESS AMOUNT ASSET\n\n"
            "Exemplo:\n"
            "/watch GDZYYA... 100 XDB"
        )
        return

    address = context.args[0].upper().strip()

    try:
        amount = float(context.args[1])
    except ValueError:
        await update.message.reply_text("❗ AMOUNT tem de ser um número. Ex: 100 ou 250.5")
        return

    asset = context.args[2].upper().strip()
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


async def unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /unwatch ADDRESS")
        return

    address = context.args[0].upper().strip()

    if address in WATCHLIST:
        del WATCHLIST[address]
        await update.message.reply_text(f"✅ Deixei de vigiar o endereço:\n{address}")
    else:
        await update.message.reply_text("❗ Esse endereço não está a ser vigiado.")


async def list_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not WATCHLIST:
        await update.message.reply_text("📭 Não há nenhum endereço em vigilância.")
        return

    msg_lines = ["📡 Endereços atualmente em vigilância (XDB Chain):\n"]
    for addr, data in WATCHLIST.items():
        msg_lines.append(
            f"{addr}\n"
            f" • Threshold: {data['threshold']}\n"
            f" • Asset: {data['asset']}\n"
        )

    await update.message.reply_text("\n".join(msg_lines))


# ---------------------------------------------------------
# BALANCE CHECK NA XDB CHAIN (via Horizon)
# ---------------------------------------------------------

async def fetch_balance(address: str, asset_code: str) -> float:
    """
    Devolve o saldo do asset_code para o address na XDB Chain.
    Para asset "XDB", normalmente é o asset nativo desta rede (dependendo da config).
    Caso a conta não exista, devolve 0.0.
    """

    def _get_account():
        return server.accounts().account_id(address).call()

    # Chamada bloqueante para thread separada
    try:
        account = await asyncio.to_thread(_get_account)
    except Exception:
        # Conta não encontrada ou endpoint sem dados -> tratamos como saldo 0.0
        return 0.0

    balances = account.get("balances", [])
    asset_code = asset_code.upper()

    for b in balances:
        # Muitos forks de Stellar usam "native" para o asset base (ex: XDB)
        if asset_code in ("XDB", "NATIVE"):
            if b.get("asset_type") == "native":
                return float(b["balance"])

        # Outros assets com asset_code específico
        if b.get("asset_code", "").upper() == asset_code:
            return float(b["balance"])

    return 0.0


async def check_watchlist(context: ContextTypes.DEFAULT_TYPE):
    """
    Job que corre periodicamente e verifica se algum saldo passou o threshold.
    """
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
            # Erro inesperado (network, formato, etc.)
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⚠️ Erro ao verificar saldo na XDB Chain para {address} ({asset}):\n"
                    f"{exc}"
                ),
            )
            continue

        if balance >= threshold:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "🎉 *ALERTA XDB CHAIN*\n\n"
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
# TELEGRAM BOOTSTRAP (SEM asyncio.run)
# ---------------------------------------------------------

def run_bot():
    """
    Cria a aplicação do Telegram e inicia o polling.
    """
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

    # run_polling gere o event loop por nós (não usar asyncio.run)
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
    )


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------

def main():
    # Start Flask num thread separado (para Render / healthchecks)
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()

    # Start Telegram bot
    run_bot()


if __name__ == "__main__":
    main()


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
from stellar_sdk.exceptions import NotFoundError


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
#       "threshold": float,       # valor mínimo de NOVO montante
#       "asset": str,            # ex: "XDB"
#       "chat_id": int,
#       "base_balance": float,   # saldo no momento em que /watch foi chamado
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
    "/watch ADDRESS AMOUNT ASSET – vigiar NOVO montante\n"
    "   • ADDRESS: endereço XDB Chain\n"
    "   • AMOUNT: valor mínimo de novo montante (ex: 30)\n"
    "   • ASSET: código do asset (ex: XDB)\n"
    "/unwatch ADDRESS – parar vigilância\n"
    "/list – listar vigilâncias\n"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 Bem-vindo ao XDB Bot na XDB Chain!\n\n"
        "Vou avisar quando um endereço receber NOVO montante acima de um threshold.\n"
        "Usa /help para veres os comandos disponíveis."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


# -------------------- WATCH / UNWATCH / LIST --------------------

async def watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /watch ADDRESS AMOUNT ASSET
    Ex: /watch GDZYYA... 30 XDB
    Significa: alerta quando esse endereço receber >= 30 XDB NOVOS (desde agora).
    """
    if len(context.args) < 3:
        await update.message.reply_text(
            "❗ Uso correto:\n"
            "/watch ADDRESS AMOUNT ASSET\n\n"
            "Exemplo:\n"
            "/watch GDZYYA... 30 XDB\n\n"
            "Vou comparar o saldo daqui para a frente e avisar quando o NOVO montante atingir o threshold."
        )
        return

    address = context.args[0].upper().strip()

    try:
        amount = float(context.args[1])
    except ValueError:
        await update.message.reply_text("❗ AMOUNT tem de ser um número. Ex: 30 ou 250.5")
        return

    asset = context.args[2].upper().strip()
    chat_id = update.effective_chat.id

    # Lê o saldo atual para definir o "ponto de partida"
    current_balance = await fetch_balance(address, asset)

    WATCHLIST[address] = {
        "threshold": amount,
        "asset": asset,
        "chat_id": chat_id,
        "base_balance": current_balance,
    }

    await update.message.reply_text(
        "✅ Endereço adicionado à watchlist (XDB Chain):\n"
        f"• Address: {address}\n"
        f"• Threshold de NOVO montante: {amount} {asset}\n"
        f"• Saldo atual (ponto de partida): {current_balance} {asset}"
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
            f" • Threshold NOVO montante: {data['threshold']} {data['asset']}\n"
            f" • Saldo base: {data.get('base_balance', 0.0)} {data['asset']}\n"
        )

    await update.message.reply_text("\n".join(msg_lines))


# ---------------------------------------------------------
# BALANCE CHECK NA XDB CHAIN (via Horizon)
# ---------------------------------------------------------

async def fetch_balance(address: str, asset_code: str) -> float:
    """
    Devolve o saldo do asset_code para o address na XDB Chain.
    Se a conta não existir (404 / NotFound), devolve 0.0 sem mandar erro para o utilizador.
    """
    def _get_account():
        return server.accounts().account_id(address).call()

    try:
        account = await asyncio.to_thread(_get_account)
    except NotFoundError:
        # Conta ainda não existe / não foi funded -> tratamos como saldo 0
        return 0.0
    except Exception:
        # Qualquer outro erro inesperado -> também tratamos como 0 para não spammar
        return 0.0

    balances = account.get("balances", [])
    asset_code = asset_code.upper()

    for b in balances:
        # Em forks de Stellar normalmente o asset base (XDB) continua a ser "native"
        if asset_code in ("XDB", "NATIVE"):
            if b.get("asset_type") == "native":
                return float(b["balance"])

        if b.get("asset_code", "").upper() == asset_code:
            return float(b["balance"])

    return 0.0


async def check_watchlist(context: ContextTypes.DEFAULT_TYPE):
    """
    Job que corre periodicamente e verifica se entrou NOVO montante
    suficiente (>= threshold) desde o saldo base guardado em /watch.
    """
    if not WATCHLIST:
        return

    items = list(WATCHLIST.items())

    for address, data in items:
        threshold = data["threshold"]
        asset = data["asset"]
        chat_id = data["chat_id"]
        base_balance = float(data.get("base_balance", 0.0))

        # Saldo atual
        balance = await fetch_balance(address, asset)

        # Novo montante recebido desde que começámos a vigiar
        delta = max(0.0, balance - base_balance)

        if delta >= threshold:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "🎉 *ALERTA XDB CHAIN*\n\n"
                    f"O endereço:\n`{address}`\n\n"
                    f"recebeu um NOVO montante de pelo menos {threshold} {asset}.\n\n"
                    f"*Saldo base:* {base_balance} {asset}\n"
                    f"*Saldo atual:* {balance} {asset}\n"
                    f"*Novo montante total:* {delta} {asset}"
                ),
                parse_mode="Markdown",
            )

            # Remove da watchlist após disparar o alerta
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

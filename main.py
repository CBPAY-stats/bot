import os
import threading
import asyncio
from typing import Dict, Any, Optional

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

# Endpoint oficial da XDB Chain Mainnet (Horizon compatível)
STELLAR_HORIZON_URL = "https://horizon.livenet.xdbchain.com"

# Estrutura em memória:
# {
#   address: {
#       "threshold": float,          # valor mínimo de UMA NOVA transação
#       "asset": str,               # ex: "XDB"
#       "chat_id": int,
#       "last_paging_token": str | None,  # última payment vista
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
    return "✅ XDB Bot (nova transação ≥ X) a correr na XDB Chain!", 200


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
# HELP / START
# ---------------------------------------------------------

HELP_TEXT = (
    "👋 Olá! Eu sou o XDB Bot (XDB Chain).\n\n"
    "Vou avisar quando um endereço receber *uma nova transação* com valor ≥ threshold.\n\n"
    "Comandos disponíveis:\n"
    "/start – mensagem de boas-vindas\n"
    "/help – mostra esta ajuda\n"
    "/watch ADDRESS AMOUNT ASSET – vigiar NOVAS transações\n"
    "   • ADDRESS: endereço XDB Chain\n"
    "   • AMOUNT: valor mínimo de uma nova transação (ex: 30)\n"
    "   • ASSET: código do asset (ex: XDB)\n"
    "/unwatch ADDRESS – parar vigilância\n"
    "/list – listar vigilâncias\n"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 Bem-vindo ao XDB Bot na XDB Chain!\n\n"
        "Eu aviso-te quando um endereço receber UMA NOVA transação "
        "com valor igual ou superior ao threshold que definires.\n\n"
        "Usa /help para veres os comandos disponíveis."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


# ---------------------------------------------------------
# FUNÇÕES AUXILIARES HORIZON (PAYMENTS)
# ---------------------------------------------------------

async def get_latest_paging_token(address: str) -> Optional[str]:
    """
    Obtém o paging_token da payment mais recente deste address.
    Se não houver nenhuma payment ou a conta ainda não existir, devolve None.
    """
    def _call():
        return (
            server.payments()
            .for_account(address)
            .order("desc")
            .limit(1)
            .call()
        )

    try:
        resp = await asyncio.to_thread(_call)
    except NotFoundError:
        # Conta não existe / sem dados -> sem payments
        return None
    except Exception:
        # Outro erro qualquer -> tratamos como sem payments
        return None

    records = resp.get("_embedded", {}).get("records", [])
    if not records:
        return None

    return records[0].get("paging_token")


async def get_new_payments(address: str, cursor: Optional[str]) -> list[dict]:
    """
    Devolve a lista de *novas* payments para o address, a partir de um cursor (paging_token).
    Se cursor for None, começa a partir do estado atual (vai apanhar tudo no histórico),
    mas nós só vamos usar cursor=None quando ainda não havia nada.
    """
    def _call():
        req = (
            server.payments()
            .for_account(address)
            .order("asc")
            .limit(50)
        )
        if cursor:
            req = req.cursor(cursor)
        return req.call()

    try:
        resp = await asyncio.to_thread(_call)
    except NotFoundError:
        # Conta sem payments / não existe -> nada de novo
        return []
    except Exception:
        # Outro erro -> por segurança devolve vazio
        return []

    return resp.get("_embedded", {}).get("records", [])


def payment_matches_asset(rec: dict, asset_code: str, address: str) -> bool:
    """
    Verifica se uma payment:
    - é *para* este address
    - é do asset certo (XDB/nativo ou outro)
    """
    # Apenas payments onde o address é o destinatário
    if rec.get("to") != address:
        return False

    asset_code = asset_code.upper()

    # Asset nativo (XDB)
    if asset_code in ("XDB", "NATIVE"):
        return rec.get("asset_type") == "native"

    # Outros assets emitidos
    return rec.get("asset_code", "").upper() == asset_code


# ---------------------------------------------------------
# WATCH / UNWATCH / LIST
# ---------------------------------------------------------

async def watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /watch ADDRESS AMOUNT ASSET
    Ex: /watch GDZYYA... 30 XDB
    Significa: alerta quando esse address receber *uma nova transação* ≥ 30 XDB.
    """
    if len(context.args) < 3:
        await update.message.reply_text(
            "❗ Uso correto:\n"
            "/watch ADDRESS AMOUNT ASSET\n\n"
            "Exemplo:\n"
            "/watch GDZYYA... 30 XDB\n\n"
            "Vou escutar novas transações e avisar quando UMA delas tiver "
            "valor ≥ AMOUNT para esse endereço."
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

    # Guardamos o paging_token mais recente agora -> tudo o que vier DEPOIS disto é "novo"
    last_token = await get_latest_paging_token(address)

    WATCHLIST[address] = {
        "threshold": amount,
        "asset": asset,
        "chat_id": chat_id,
        "last_paging_token": last_token,
    }

    await update.message.reply_text(
        "✅ Endereço adicionado à watchlist (XDB Chain):\n"
        f"• Address: {address}\n"
        f"• Threshold (nova transação): {amount} {asset}\n"
        f"• Cursor inicial: {last_token if last_token else 'nenhuma payment anterior encontrada'}\n\n"
        "Só vou considerar *novas* transações a partir deste momento."
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
            f" • Threshold (nova tx): {data['threshold']} {data['asset']}\n"
            f" • Último paging_token: {data.get('last_paging_token', 'None')}\n"
        )

    await update.message.reply_text("\n".join(msg_lines))


# ---------------------------------------------------------
# CHECK DAS NOVAS TRANSAÇÕES
# ---------------------------------------------------------

async def check_watchlist(context: ContextTypes.DEFAULT_TYPE):
    """
    Job que corre periodicamente e verifica se apareceram NOVAS payments
    com valor ≥ threshold para cada address vigiado.
    """
    if not WATCHLIST:
        return

    items = list(WATCHLIST.items())

    for address, data in items:
        threshold = data["threshold"]
        asset = data["asset"]
        chat_id = data["chat_id"]
        last_token = data.get("last_paging_token")

        # Vai buscar novas payments depois do last_token
        records = await get_new_payments(address, last_token)

        if not records:
            continue

        triggered = False
        new_last_token = last_token

        for rec in records:
            # Atualizamos sempre o último paging_token percorrido
            new_last_token = rec.get("paging_token", new_last_token)

            if not payment_matches_asset(rec, asset, address):
                continue

            try:
                amt = float(rec.get("amount", "0"))
            except ValueError:
                continue

            if amt >= threshold:
                # Encontrámos pelo menos uma nova payment ≥ threshold
                triggered = True

        # Atualizamos o cursor para não repetir as mesmas payments
        data["last_paging_token"] = new_last_token

        if triggered:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "🎉 *ALERTA XDB CHAIN*\n\n"
                    f"O endereço:\n`{address}`\n\n"
                    f"recebeu *uma nova transação* com valor ≥ {threshold} {asset}.\n\n"
                    f"(Estou a vigiar apenas transações novas a partir do momento em que fizeste /watch.)"
                ),
                parse_mode="Markdown",
            )
            # Depois do alerta, removemos este address da watchlist
            WATCHLIST.pop(address, None)


# ---------------------------------------------------------
# TELEGRAM BOOTSTRAP
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

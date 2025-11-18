import os
import threading
import asyncio
from typing import Dict, Any, Optional, List, Tuple

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
#       "threshold": float,   # valor mínimo de UMA transação
#       "asset": str,        # ex: "XDB"
#       "chat_id": int,
#   }
# }
WATCHLIST: Dict[str, Dict[str, Any]] = {}

# Cursor global para o stream de /operations
GLOBAL_CURSOR: Optional[str] = "now"

server = Server(STELLAR_HORIZON_URL)
telegram_app: Optional[Any] = None


# ---------------------------------------------------------
# FLASK - Healthcheck / Render
# ---------------------------------------------------------

flask_app = Flask(__name__)


@flask_app.get("/")
def index():
    return "✅ XDB Bot (pagamentos de/para conta) a correr na XDB Chain!", 200


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
    "Vou avisar quando um endereço tiver *uma nova transação* (de OU para a conta) "
    "com valor ≥ threshold.\n\n"
    "Comandos disponíveis:\n"
    "/start – mensagem de boas-vindas\n"
    "/help – mostra esta ajuda\n"
    "/watch ADDRESS AMOUNT ASSET – vigiar transações novas\n"
    "   • ADDRESS: endereço XDB Chain\n"
    "   • AMOUNT: valor mínimo de UMA transação (ex: 30)\n"
    "   • ASSET: código do asset (ex: XDB)\n"
    "/unwatch ADDRESS – parar vigilância\n"
    "/list – listar vigilâncias\n"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 Bem-vindo ao XDB Bot na XDB Chain!\n\n"
        "Eu aviso-te quando um endereço tiver UMA NOVA transação "
        "(de ou para a conta) com valor igual ou superior ao threshold que definires.\n\n"
        "Usa /help para veres os comandos disponíveis."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


# ---------------------------------------------------------
# FUNÇÕES AUXILIARES HORIZON (OPERATIONS GLOBAIS)
# ---------------------------------------------------------

async def prime_global_cursor_if_needed() -> None:
    """
    Se o cursor global estiver em 'now', faz uma chamada ao /operations global
    para obter a operação mais recente e define o cursor para o respetivo paging_token.
    Assim só consideramos operações novas a partir do arranque do bot.
    """
    global GLOBAL_CURSOR
    if GLOBAL_CURSOR != "now":
        return

    def _call():
        return (
            server.operations()
            .order("desc")
            .limit(1)
            .call()
        )

    try:
        resp = await asyncio.to_thread(_call)
        records = resp.get("_embedded", {}).get("records", [])
        if records:
            GLOBAL_CURSOR = records[0].get("paging_token") or GLOBAL_CURSOR
        else:
            # Se não houver operações, deixamos como "now"
            GLOBAL_CURSOR = "now"
    except Exception:
        # Em caso de erro, mantemos "now" e tentamos de novo no próximo ciclo
        GLOBAL_CURSOR = "now"


async def get_new_global_operations() -> List[dict]:
    """
    Lê operações novas do endpoint global /operations, a partir do GLOBAL_CURSOR,
    em ordem ascendente.
    """
    global GLOBAL_CURSOR

    await prime_global_cursor_if_needed()

    def _call(cursor: Optional[str]):
        req = server.operations().order("asc").limit(200)
        if cursor and cursor != "now":
            req = req.cursor(cursor)
        return req.call()

    try:
        resp = await asyncio.to_thread(_call, GLOBAL_CURSOR)
    except NotFoundError:
        # Muito improvável num stream global, mas tratamos como sem operações
        return []
    except Exception:
        # Em caso de erro de rede, etc., devolvemos vazio
        return []

    records = resp.get("_embedded", {}).get("records", [])

    # Atualiza o cursor global para a última operação vista
    if records:
        last_token = records[-1].get("paging_token")
        if last_token:
            GLOBAL_CURSOR = last_token

    return records


def parse_operation_for_address(rec: dict, address: str, asset_code: str) -> Optional[Tuple[str, float]]:
    """
    Verifica se a operação envolve o 'address' e o 'asset_code' indicado.

    Consideramos:
    - type == "payment"
        • campos: 'from', 'to', 'amount', 'asset_type', 'asset_code'
    - type == "create_account"
        • campos: 'funder', 'account', 'starting_balance'
        • tratado como funding inicial em asset nativo

    Se for relevante, devolve uma tupla:
      (direction, amount_float)
    onde direction é "in" (recebeu) ou "out" (enviou).
    Senão, devolve None.
    """
    op_type = rec.get("type")
    asset_code = asset_code.upper()

    # Caso 1: payment normal
    if op_type == "payment":
        from_addr = rec.get("from")
        to_addr = rec.get("to")

        # Verificamos se o address está envolvido como remetente ou destinatário
        involved = (from_addr == address) or (to_addr == address)
        if not involved:
            return None

        # Verifica asset
        rec_asset_type = rec.get("asset_type")
        rec_asset_code = rec.get("asset_code")

        if asset_code in ("XDB", "NATIVE"):
            # Asset nativo da XDBChain
            if rec_asset_type != "native":
                return None
        else:
            if (rec_asset_code or "").upper() != asset_code:
                return None

        try:
            amt = float(rec.get("amount", "0"))
        except ValueError:
            return None

        # Direção
        if to_addr == address:
            direction = "in"   # recebeu
        else:
            direction = "out"  # enviou

        return direction, amt

    # Caso 2: create_account -> funding inicial (native)
    if op_type == "create_account":
        funder = rec.get("funder")
        account = rec.get("account")

        if address not in (funder, account):
            return None

        # Só faz sentido para asset nativo
        if asset_code not in ("XDB", "NATIVE"):
            return None

        try:
            amt = float(rec.get("starting_balance", "0"))
        except ValueError:
            return None

        direction = "in" if account == address else "out"
        return direction, amt

    # Outros tipos de operação não contam como pagamento
    return None


# ---------------------------------------------------------
# WATCH / UNWATCH / LIST
# ---------------------------------------------------------

async def watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /watch ADDRESS AMOUNT ASSET
    Ex: /watch GDZYYA... 30 XDB
    Significa: alerta quando esse address tiver UMA NOVA transação ≥ 30 XDB,
    seja a receber (in) ou a enviar (out).
    """
    if len(context.args) < 3:
        await update.message.reply_text(
            "❗ Uso correto:\n"
            "/watch ADDRESS AMOUNT ASSET\n\n"
            "Exemplo:\n"
            "/watch GDZYYA... 30 XDB\n\n"
            "Vou escutar operações novas na rede e avisar quando UMA delas "
            "envolver esse address (de ou para) com valor ≥ AMOUNT."
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

    WATCHLIST[address] = {
        "threshold": amount,
        "asset": asset,
        "chat_id": chat_id,
    }

    await update.message.reply_text(
        "✅ Endereço adicionado à watchlist (XDB Chain):\n"
        f"• Address: {address}\n"
        f"• Threshold (nova transação): {amount} {asset}\n\n"
        "Vou considerar *apenas operações novas* a partir do estado atual do stream."
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
        )

    await update.message.reply_text("\n".join(msg_lines))


# ---------------------------------------------------------
# CHECK DAS NOVAS TRANSAÇÕES (GLOBAL /OPERATIONS)
# ---------------------------------------------------------

async def check_watchlist(context: ContextTypes.DEFAULT_TYPE):
    """
    Job que corre periodicamente e verifica se apareceram NOVAS operações na rede
    (via /operations global) que envolvam algum dos endereços da WATCHLIST,
    com valor ≥ threshold, seja a receber (in) ou a enviar (out).
    """
    if not WATCHLIST:
        return

    records = await get_new_global_operations()
    if not records:
        return

    # Processamos cada operação e vemos se toca em algum address vigiado
    for rec in records:
        op_type = rec.get("type")
        # Só vale a pena olhar para tipos potencialmente relevantes
        if op_type not in ("payment", "create_account"):
            continue

        for address, data in list(WATCHLIST.items()):
            threshold = data["threshold"]
            asset = data["asset"]
            chat_id = data["chat_id"]

            parsed = parse_operation_for_address(rec, address, asset)
            if parsed is None:
                continue

            direction, amt = parsed
            if amt < threshold:
                continue

            # Se chegou aqui, esta operação é um match
            tx_hash = rec.get("transaction_hash")
            created_at = rec.get("created_at")

            if direction == "in":
                direction_text = "recebeu"
            else:
                direction_text = "enviou"

            explorer_url = (
                f"https://explorer.xdbchain.com/en-US/mainnet/transaction/{tx_hash}"
                if tx_hash
                else None
            )

            text_lines = [
                "🎉 *ALERTA XDB CHAIN*",
                "",
                f"O endereço:",
                f"`{address}`",
                "",
                f"{direction_text} uma nova transação de pelo menos {threshold} {asset}.",
                "",
                f"*Montante da operação:* {amt} {asset}",
            ]
            if created_at:
                text_lines.append(f"*Data/hora:* {created_at}")
            if explorer_url:
                text_lines.append(f"[🔗 Ver no explorer]({explorer_url})")

            await context.bot.send_message(
                chat_id=chat_id,
                text="\n".join(text_lines),
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )

            # Remove o endereço depois de disparar um alerta
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

    # Job para verificar a watchlist a cada 10 segundos
    application.job_queue.run_repeating(
        check_watchlist,
        interval=10,   # segundos
        first=5,       # atraso inicial
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

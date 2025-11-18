# ---------------------------------------------------------
# TELEGRAM BOOTSTRAP (SEM asyncio.run)
# ---------------------------------------------------------

def run_bot() -> None:
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
        interval=60,
        first=10,
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
    )


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------

def main() -> None:
    # Start Flask num thread separado
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()

    # Start Telegram bot (sem asyncio.run)
    run_bot()


if __name__ == "__main__":
    main()

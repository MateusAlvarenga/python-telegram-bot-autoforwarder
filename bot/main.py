from __future__ import annotations

import logging

from telegram import Bot, BotCommand, BotCommandScopeChat, Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler
from telegram.request import HTTPXRequest
from telethon import TelegramClient
from telethon.sessions import StringSession

from bot.config import BotConfig, load_config
from bot.db import Database
from bot.handlers import (
    create_routine,
    delete_routine,
    edit_routine,
    enable_routine,
    execute_routine,
    help_command,
    list_routines,
    pause_routine,
    show_history,
    show_routine,
    start,
    stop_routine,
)
from bot.routines import RoutineScheduler

logger = logging.getLogger(__name__)


def _http_request() -> HTTPXRequest:
    return HTTPXRequest(
        connection_pool_size=10,
        connect_timeout=15,
        read_timeout=60,
        write_timeout=60,
        pool_timeout=15,
    )


async def _post_init(application: Application) -> None:
    config: BotConfig = application.bot_data["config"]
    database = Database(config.database_url)
    source_client = TelegramClient(
        StringSession(config.telethon_session),
        config.api_id,
        config.api_hash,
    )
    copier_bot: Bot = application.bot
    separate_copier = config.copier_bot_token is not None
    if separate_copier:
        copier_bot = Bot(token=config.copier_bot_token, request=_http_request())
    copier_initialized = False

    try:
        await database.connect()
        await source_client.connect()
        if not await source_client.is_user_authorized():
            raise RuntimeError(
                "TELETHON_SESSION is not authorized. Generate a new session with `python -m bot.auth`."
            )
        if separate_copier:
            await copier_bot.initialize()
            copier_initialized = True
        await application.bot.set_my_commands(
            [
                BotCommand("start", "Iniciar"),
                BotCommand("ajuda", "Ver comandos"),
                BotCommand("rotinas", "Listar rotinas"),
                BotCommand("rotina", "Ver uma rotina"),
                BotCommand("rotina_criar", "Criar rotina"),
                BotCommand("rotina_editar", "Editar rotina"),
                BotCommand("rotina_ativar", "Ativar rotina"),
                BotCommand("rotina_pausar", "Pausar rotina"),
                BotCommand("rotina_executar", "Executar agora"),
                BotCommand("rotina_parar", "Interromper execução"),
                BotCommand("rotina_excluir", "Excluir rotina"),
                BotCommand("historico", "Ver histórico"),
            ],
            scope=BotCommandScopeChat(config.owner_id),
        )
        scheduler = RoutineScheduler(database, source_client, copier_bot)
        application.bot_data.update(
            database=database,
            source_client=source_client,
            copier_bot=copier_bot,
            scheduler=scheduler,
        )
        await scheduler.start()
        logger.info("Bot initialized; routine scheduler is running.")
    except Exception:
        await source_client.disconnect()
        if separate_copier and copier_initialized:
            await copier_bot.shutdown()
        await database.close()
        raise


async def _post_shutdown(application: Application) -> None:
    scheduler: RoutineScheduler | None = application.bot_data.get("scheduler")
    source_client: TelegramClient | None = application.bot_data.get("source_client")
    database: Database | None = application.bot_data.get("database")
    copier_bot: Bot | None = application.bot_data.get("copier_bot")
    if scheduler is not None:
        await scheduler.stop()
    if source_client is not None:
        await source_client.disconnect()
    if copier_bot is not None and copier_bot is not application.bot:
        await copier_bot.shutdown()
    if database is not None:
        await database.close()
    logger.info("Bot services stopped.")


def build_application(config: BotConfig) -> Application:
    application = (
        ApplicationBuilder()
        .token(config.bot_token)
        .request(_http_request())
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    application.bot_data["config"] = config
    command_handlers = (
        ("start", start),
        ("ajuda", help_command),
        ("rotinas", list_routines),
        ("rotina", show_routine),
        ("rotina_criar", create_routine),
        ("rotina_editar", edit_routine),
        ("rotina_ativar", enable_routine),
        ("rotina_pausar", pause_routine),
        ("rotina_executar", execute_routine),
        ("rotina_parar", stop_routine),
        ("rotina_excluir", delete_routine),
        ("historico", show_history),
    )
    for command, handler in command_handlers:
        application.add_handler(CommandHandler(command, handler))
    return application


def main() -> None:
    config = load_config()
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    application = build_application(config)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

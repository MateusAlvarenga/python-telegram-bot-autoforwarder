from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from bot.config import BotConfig
from bot.db import Database
from bot.routines import RoutineScheduler

_SPEC_SEPARATOR = "|"


def parse_routine_spec(
    value: str,
) -> tuple[str, str, int | None, str, int | None, int, int | None]:
    fields = [field.strip() for field in value.split(_SPEC_SEPARATOR)]
    if len(fields) not in (6, 7):
        raise ValueError(
            "Use: nome|chat_origem|topico_origem_ou_-|chat_destino|topico_destino_ou_-|intervalo_em_segundos[|limite_de_posts_ou_-]"
        )
    name, source_chat, source_topic, destination_chat, destination_topic, interval = fields[:6]
    if not name or len(name) > 64:
        raise ValueError("O nome da rotina deve ter entre 1 e 64 caracteres.")
    if not source_chat or not destination_chat:
        raise ValueError("Informe os chats de origem e destino.")

    def parse_topic(raw: str) -> int | None:
        if raw == "-":
            return None
        try:
            topic_id = int(raw)
        except ValueError as exc:
            raise ValueError("IDs de tópico devem ser inteiros positivos ou '-'.") from exc
        if topic_id <= 0:
            raise ValueError("IDs de tópico devem ser inteiros positivos ou '-'.")
        return topic_id

    try:
        interval_seconds = int(interval)
    except ValueError as exc:
        raise ValueError("O intervalo deve ser um número positivo de segundos.") from exc
    if interval_seconds <= 0:
        raise ValueError("O intervalo deve ser um número positivo de segundos.")

    max_posts_per_run = None
    if len(fields) == 7 and fields[6] != "-":
        try:
            max_posts_per_run = int(fields[6])
        except ValueError as exc:
            raise ValueError("O limite de posts deve ser um número positivo ou '-'.") from exc
        if not 0 < max_posts_per_run <= 9_223_372_036_854_775_807:
            raise ValueError("O limite de posts deve ser um número positivo ou '-'.")

    return (
        name,
        source_chat,
        parse_topic(source_topic),
        destination_chat,
        parse_topic(destination_topic),
        interval_seconds,
        max_posts_per_run,
    )


def parse_cursor_spec(value: str) -> tuple[int, int]:
    fields = [field.strip() for field in value.split(_SPEC_SEPARATOR)]
    if len(fields) != 2:
        raise ValueError("Use: id_da_rotina|ultimo_id_de_mensagem")
    try:
        routine_id, cursor = (int(field) for field in fields)
    except ValueError as exc:
        raise ValueError("O ID da rotina deve ser positivo e o cursor não negativo.") from exc
    if routine_id <= 0 or not 0 <= cursor <= 9_223_372_036_854_775_807:
        raise ValueError("O ID da rotina deve ser positivo e o cursor não negativo.")
    return routine_id, cursor


def _services(context: ContextTypes.DEFAULT_TYPE) -> tuple[BotConfig, Database, RoutineScheduler]:
    return (
        context.application.bot_data["config"],
        context.application.bot_data["database"],
        context.application.bot_data["scheduler"],
    )


async def _owner_only(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    config: BotConfig = context.application.bot_data["config"]
    user = update.effective_user
    if user is None or user.id != config.owner_id:
        if update.effective_message is not None:
            await update.effective_message.reply_text("Este bot é privado.")
        return False
    if update.effective_chat is None or update.effective_chat.type != "private":
        if update.effective_message is not None:
            await update.effective_message.reply_text("Use os comandos no chat privado do bot.")
        return False
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _owner_only(update, context):
        return
    await update.effective_message.reply_text(
        "Bot de cópia contínua. Use /ajuda para ver os comandos."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _owner_only(update, context):
        return
    
    date_and_time = datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%d %H:%M:%S")
    await update.effective_message.reply_text(
        "Comandos:\n"
        "/rotina_criar nome|chat_origem|topico_origem_ou_-|chat_destino|topico_destino_ou_-|intervalo_segundos[|limite_de_posts_ou_-]\n"
        "/rotina_editar id|nome|chat_origem|topico_origem_ou_-|chat_destino|topico_destino_ou_-|intervalo_segundos[|limite_de_posts_ou_-]\n"
        "/rotinas - listar rotinas\n"
        "/rotina id - ver configuração e último checkpoint\n"
        "/rotina_cursor id|ultimo_id_de_mensagem - definir manualmente o cursor\n"
        "/rotina_ativar id - ativar execução periódica (a primeira é imediata)\n"
        "/rotina_pausar id - pausar futuras execuções\n"
        "/rotina_executar id - disparar uma execução manual\n"
        "/rotina_parar id - pausar e interromper após o lote atual\n"
        "/rotina_excluir id - excluir uma rotina parada\n"
        "/historico id - últimas execuções\n\n"
        "Chats podem ser IDs numéricos ou @username. Use '-' para não selecionar tópico."
        f"\n\nData e hora: {date_and_time}" 
    )


async def create_routine(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _owner_only(update, context):
        return
    try:
        spec = parse_routine_spec(" ".join(context.args))
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    _, database, scheduler = _services(context)
    last_message_id = await scheduler.latest_source_message_id(spec[1], spec[2])
    routine = await database.create_routine(
        *spec[:6],
        last_message_id=last_message_id,
        max_posts_per_run=spec[6],
    )
    max_posts_per_run = routine.get("max_posts_per_run")
    limit_description = (
        "sem limite"
        if max_posts_per_run is None
        else f"limite de {max_posts_per_run} posts por execução"
    )
    await update.effective_message.reply_text(
        f"Rotina {routine['id']} criada e pausada, com cursor na mensagem "
        f"{last_message_id} e {limit_description}. "
        f"Use /rotina_ativar {routine['id']} para iniciar."
    )


async def edit_routine(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _owner_only(update, context):
        return
    try:
        parts = " ".join(context.args).split(_SPEC_SEPARATOR, maxsplit=1)
        routine_id = int(parts[0])
        if routine_id <= 0:
            raise ValueError
        if len(parts) != 2:
            raise ValueError
        spec = parse_routine_spec(parts[1])
    except (IndexError, ValueError):
        await update.effective_message.reply_text(
            "Use: /rotina_editar id|nome|chat_origem|topico_origem_ou_-|chat_destino|topico_destino_ou_-|intervalo_segundos[|limite_de_posts_ou_-]"
        )
        return
    _, database, _ = _services(context)
    routine = await database.update_routine(routine_id, *spec)
    if routine is None:
        await update.effective_message.reply_text(
            "Rotina não encontrada ou em execução; tente novamente após o término."
        )
        return
    await update.effective_message.reply_text(
        f"Rotina {routine_id} atualizada. Alterar a origem reinicia o checkpoint para 0."
    )


async def list_routines(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _owner_only(update, context):
        return
    _, database, scheduler = _services(context)
    routines = await database.list_routines()
    if not routines:
        await update.effective_message.reply_text("Nenhuma rotina cadastrada.")
        return
    lines = [
        f"{routine['id']} · {routine['name']} · "
        f"{'executando' if scheduler.is_running(routine['id']) else ('ativa' if routine['enabled'] else 'pausada')} · "
        f"próxima: {_format_time(routine['next_run_at'])}"
        for routine in routines
    ]
    await update.effective_message.reply_text("\n".join(lines))


async def show_routine(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _owner_only(update, context):
        return
    routine_id = await _parse_routine_id(update, context)
    if routine_id is None:
        return
    _, database, scheduler = _services(context)
    routine = await database.get_routine(routine_id)
    if routine is None:
        await update.effective_message.reply_text("Rotina não encontrada.")
        return
    await update.effective_message.reply_text(
        f"Rotina {routine['id']}: {routine['name']}\n"
        f"Agendamento: {'ativo' if routine['enabled'] else 'pausado'} · "
        f"Execução: {'em andamento' if scheduler.is_running(routine_id) else 'inativa'}\n"
        f"Origem: {routine['source_chat_id']} · tópico {routine['source_topic_id'] or '-'}\n"
        f"Destino: {routine['destination_chat_id']} · tópico {routine['destination_topic_id'] or '-'}\n"
        f"Intervalo: {routine['interval_seconds']}s\n"
        f"Limite por execução: {routine.get('max_posts_per_run') or 'sem limite'}\n"
        f"Última mensagem verificada: {routine['last_message_id']}\n"
        f"Última execução: {_format_time(routine['last_run_at'])}\n"
        f"Próxima execução: {_format_time(routine['next_run_at'])}"
        f"Data e hora atual: {datetime.now(timezone(timedelta(hours=-3))).strftime('%Y-%m-%d %H:%M:%S')}"
    )


async def set_routine_cursor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _owner_only(update, context):
        return
    try:
        routine_id, cursor = parse_cursor_spec(" ".join(context.args))
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    _, database, _ = _services(context)
    routine = await database.set_routine_cursor(routine_id, cursor)
    if routine is None:
        await update.effective_message.reply_text(
            "Rotina não encontrada ou em execução; o cursor não foi alterado."
        )
        return
    await update.effective_message.reply_text(
        f"Cursor da rotina {routine_id} definido para {cursor}. "
        f"A próxima execução buscará mensagens com ID maior que {cursor}."
    )


async def enable_routine(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_routine_enabled(update, context, True)


async def pause_routine(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_routine_enabled(update, context, False)


async def _set_routine_enabled(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    enabled: bool,
) -> None:
    if not await _owner_only(update, context):
        return
    routine_id = await _parse_routine_id(update, context)
    if routine_id is None:
        return
    _, database, _ = _services(context)
    routine = await database.set_routine_enabled(routine_id, enabled)
    if routine is None:
        await update.effective_message.reply_text("Rotina não encontrada.")
        return
    action = "ativada; a primeira execução começará em instantes" if enabled else "pausada"
    await update.effective_message.reply_text(f"Rotina {routine_id} {action}.")


async def execute_routine(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _owner_only(update, context):
        return
    routine_id = await _parse_routine_id(update, context)
    if routine_id is None:
        return
    _, _, scheduler = _services(context)
    if not await scheduler.request_run(routine_id, manual=True):
        await update.effective_message.reply_text(
            "Não foi possível iniciar: rotina inexistente, já em execução ou indisponível."
        )
        return
    await update.effective_message.reply_text(f"Execução manual da rotina {routine_id} iniciada.")


async def stop_routine(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _owner_only(update, context):
        return
    routine_id = await _parse_routine_id(update, context)
    if routine_id is None:
        return
    _, database, scheduler = _services(context)
    routine = await database.set_routine_enabled(routine_id, False)
    if routine is None:
        await update.effective_message.reply_text("Rotina não encontrada.")
        return
    stopped = scheduler.request_stop(routine_id)
    message = (
        "Execução interrompida após o lote atual."
        if stopped
        else "Rotina pausada; não havia execução ativa neste serviço."
    )
    await update.effective_message.reply_text(f"Rotina {routine_id}: {message}")


async def delete_routine(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _owner_only(update, context):
        return
    routine_id = await _parse_routine_id(update, context)
    if routine_id is None:
        return
    _, database, _ = _services(context)
    if not await database.delete_routine(routine_id):
        await update.effective_message.reply_text(
            "Rotina não encontrada ou em execução; pause-a e tente novamente."
        )
        return
    await update.effective_message.reply_text(
        f"Rotina {routine_id} excluída. O histórico de execuções foi mantido."
    )


async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _owner_only(update, context):
        return
    routine_id = await _parse_routine_id(update, context)
    if routine_id is None:
        return
    _, database, _ = _services(context)
    runs = await database.list_runs(routine_id)
    if not runs:
        await update.effective_message.reply_text("Ainda não há execuções registradas.")
        return
    lines = [
        f"#{run['id']} · {run['status']} · "
        f"{_format_time(run['started_at'])} · {run['messages_copied']} mensagens"
        + (f" · {' '.join(run['error'].split())[:180]}" if run["error"] else "")
        for run in runs
    ]
    await update.effective_message.reply_text("\n".join(lines))


async def _parse_routine_id(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int | None:
    try:
        routine_id = int(context.args[0])
        if routine_id <= 0:
            raise ValueError
        return routine_id
    except (IndexError, ValueError):
        if update.effective_message is not None:
            await update.effective_message.reply_text("Informe um ID de rotina positivo.")
        return None


def _format_time(value: datetime | None) -> str:
    if value is None:
        return "—"
    
    value = value.astimezone(timezone(timedelta(hours=-3)))
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")

# return a datetime object in brazilia timezone
 
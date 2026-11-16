import logging
import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class BotConfig:
    bot_token: str
    owner_id: int
    database_url: str
    api_id: int
    api_hash: str
    telethon_session: str
    copier_bot_token: str | None
    log_level: int


def load_config() -> BotConfig:
    load_dotenv()

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    database_url = os.getenv("DATABASE_URL", "").strip()
    owner_id_value = os.getenv("OWNER_ID", "").strip()
    if not bot_token:
        raise ValueError("BOT_TOKEN must be set.")
    if not database_url:
        raise ValueError("DATABASE_URL must be set.")
    if not owner_id_value:
        raise ValueError("OWNER_ID must be set.")
    try:
        owner_id = int(owner_id_value)
    except ValueError as exc:
        raise ValueError("OWNER_ID must be a Telegram user ID.") from exc
    if owner_id <= 0:
        raise ValueError("OWNER_ID must be a positive Telegram user ID.")

    api_id_value = os.getenv("API_ID", "").strip()
    try:
        api_id = int(api_id_value)
    except ValueError as exc:
        raise ValueError("API_ID must be a positive Telegram API ID.") from exc
    if api_id <= 0:
        raise ValueError("API_ID must be a positive Telegram API ID.")

    api_hash = os.getenv("API_HASH", "").strip()
    if not api_hash:
        raise ValueError("API_HASH must be set.")
    telethon_session = os.getenv("TELETHON_SESSION", "").strip()
    if not telethon_session:
        raise ValueError("TELETHON_SESSION must be set.")

    log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, None)
    if not isinstance(log_level, int):
        raise ValueError(f"LOG_LEVEL is not a valid logging level: {log_level_name}")

    return BotConfig(
        bot_token=bot_token,
        owner_id=owner_id,
        database_url=database_url,
        api_id=api_id,
        api_hash=api_hash,
        telethon_session=telethon_session,
        copier_bot_token=os.getenv("COPIER_BOT_TOKEN", "").strip() or None,
        log_level=log_level,
    )
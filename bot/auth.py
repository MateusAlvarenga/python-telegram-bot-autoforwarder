import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession


async def generate_session() -> None:
    load_dotenv()
    api_id_value = os.getenv("API_ID", "").strip()
    api_hash = os.getenv("API_HASH", "").strip()
    try:
        api_id = int(api_id_value)
    except ValueError as exc:
        raise ValueError("Set a valid API_ID before generating the session.") from exc
    if api_id <= 0 or not api_hash:
        raise ValueError("Set a valid API_ID and API_HASH before generating the session.")

    phone = input("Telefone do Telegram (formato internacional): ").strip()
    if not phone:
        raise ValueError("O telefone é obrigatório.")
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        await client.start(phone=phone)
        print("\nCopie este valor para a variável Railway TELETHON_SESSION. Mantenha-o secreto:\n")
        print(client.session.save())


if __name__ == "__main__":
    asyncio.run(generate_session())

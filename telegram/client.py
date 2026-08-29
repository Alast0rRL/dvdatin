# Фабрика Telethon-клиента: создание, авторизация, управление сессиями.
# Не выполняет никаких действий после входа в аккаунт.

from __future__ import annotations

from pathlib import Path

from loguru import logger
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from app.config import TelegramConfig

SESSION_DIR = Path("data/sessions")
SESSION_NAME = "dvai"


def create_client(config: TelegramConfig) -> TelegramClient:
    """Создаёт TelegramClient с session-файлом в data/sessions/.

    Args:
        config: Конфигурация Telegram API.

    Returns:
        Экземпляр TelegramClient.
    """
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    session_path = SESSION_DIR / SESSION_NAME

    proxy = None
    if config.proxy.enabled and config.proxy.host:
        proxy = {
            "proxy_type": config.proxy.type,
            "addr": config.proxy.host,
            "port": config.proxy.port,
            "rdns": True,
        }
        if config.proxy.username:
            proxy["username"] = config.proxy.username
            proxy["password"] = config.proxy.password
        logger.info(f"Прокси: {config.proxy.type}://{config.proxy.host}:{config.proxy.port}")

    client = TelegramClient(
        str(session_path),
        config.api_id,
        config.api_hash,
        proxy=proxy,
    )
    logger.info("Telegram-клиент создан")
    return client


async def authorize(client: TelegramClient, phone: str) -> str | None:
    """Авторизует клиент по номеру телефона.

    Args:
        client: Экземпляр TelegramClient.
        phone: Номер телефона.

    Returns:
        Имя аккаунта или None если не удалось.
    """
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        name = me.first_name or me.username or "Unknown"
        logger.info(f"Авторизован как: {name}")
        return name

    await client.send_code_request(phone)
    code = input("\nEnter Telegram code: ")

    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        password = input("Enter 2FA password: ")
        await client.sign_in(password=password)

    if await client.is_user_authorized():
        me = await client.get_me()
        name = me.first_name or me.username or "Unknown"
        logger.info(f"Авторизован как: {name}")
        return name

    logger.warning("Authorization failed")
    return None

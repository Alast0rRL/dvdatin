# Photo download and cache — serves Telegram photos for Web UI.
#
# Фото хранятся только на серверах Telegram (не на диске).
# При первом запросе скачивает через Telethon, кэширует в media/<profile_id>/.
# Повторные запросы отдают из кэша.
#
# Поддержка нескольких аккаунтов: message_id в личном диалоге с Leo
# уникален для каждого аккаунта (разные number ranges: 697xxx vs 223xx).
# download_photo пробует клиенты по очереди, пока один не вернёт фото.

from __future__ import annotations

import asyncio
from pathlib import Path

from loguru import logger

#: Корневая директория кэша фото.
MEDIA_ROOT = Path(__file__).resolve().parent.parent / "media"

# Глобальные ссылки на TelegramClient(s) (устанавливается из main.py).
_telegram_clients: list = []


def set_telegram_clients(clients: list) -> None:
    """Устанавливает список TelegramClient-ов для скачивания фото."""
    global _telegram_clients
    _telegram_clients = list(clients)


def set_telegram_client(client) -> None:
    """Обратная совместимость: один клиент."""
    global _telegram_clients
    _telegram_clients = [client]


def get_telegram_clients() -> list:
    """Возвращает текущий список TelegramClient-ов."""
    return _telegram_clients


def get_telegram_client():
    """Обратная совместимость: первый клиент."""
    return _telegram_clients[0] if _telegram_clients else None


def get_photo_path(profile_id: int, message_id: int) -> Path | None:
    """Возвращает путь к кэшированному фото или None."""
    photo_dir = MEDIA_ROOT / str(profile_id)
    photo_file = photo_dir / f"{message_id}.jpg"
    if photo_file.exists():
        return photo_file
    return None


def get_all_photo_paths(profile_id: int) -> list[Path]:
    """Возвращает список кэшированных фото профиля."""
    photo_dir = MEDIA_ROOT / str(profile_id)
    if not photo_dir.exists():
        return []
    return sorted(photo_dir.glob("*.jpg"))


def _ensure_media_dir(profile_id: int) -> Path:
    """Создаёт директорию для фото профиля."""
    photo_dir = MEDIA_ROOT / str(profile_id)
    photo_dir.mkdir(parents=True, exist_ok=True)
    return photo_dir


async def _try_download_one(
    client, chat_id: int, message_id: int, profile_id: int,
) -> Path | None:
    """Пытается скачать фото одним клиентом."""
    try:
        messages = await client.get_messages(chat_id, ids=[message_id])
        if not messages or not messages[0]:
            return None
        msg = messages[0]
        if not msg.photo:
            return None

        photo_dir = _ensure_media_dir(profile_id)
        file_path = photo_dir / f"{message_id}.jpg"
        await client.download_media(msg, file=str(file_path))
        logger.info(f"Photo cached: profile={profile_id} msg={message_id}")
        return file_path
    except Exception as e:
        logger.debug(
            f"[photo] client download failed profile={profile_id} "
            f"msg={message_id}: {type(e).__name__}: {e!r}"
        )
        return None


async def download_photo(
    chat_id: int,
    message_id: int,
    profile_id: int,
) -> Path | None:
    """Скачивает фото из Telegram и кэширует на диск.

    Пробует все авторизованные клиенты по очереди (разные аккаунты —
    разные number spaces message_id в диалоге с Leo).
    """
    logger.debug(f"[photo] start profile={profile_id} msg={message_id}")

    # Проверяем кэш
    cached = get_photo_path(profile_id, message_id)
    if cached:
        return cached

    clients = get_telegram_clients()
    if not clients:
        logger.warning("TelegramClient недоступен для скачивания фото")
        return None

    for client in clients:
        if client is None:
            continue
        loop = getattr(client, "_loop", None)
        if loop is None or loop.is_closed():
            continue
        result = await _try_download_one(client, chat_id, message_id, profile_id)
        if result:
            return result

    logger.warning(
        f"Photo not found on any client: profile={profile_id} msg={message_id}"
    )
    return None


def download_photo_sync(
    chat_id: int,
    message_id: int,
    profile_id: int,
) -> Path | None:
    """Синхронная обёртка для скачивания фото (из Flask thread)."""
    clients = get_telegram_clients()
    if not clients:
        return get_photo_path(profile_id, message_id)

    # Ищем клиент с рабочим loop
    loop = None
    for cl in clients:
        if cl is None:
            continue
        l = getattr(cl, "_loop", None)
        if l is not None and not l.is_closed():
            loop = l
            break

    if loop is None:
        logger.warning(
            f"Photo sync: ни один loop недоступен "
            f"(clients={len(clients)})"
        )
        return get_photo_path(profile_id, message_id)

    try:
        future = asyncio.run_coroutine_threadsafe(
            download_photo(chat_id, message_id, profile_id),
            loop,
        )
        return future.result(timeout=30)
    except Exception as e:
        logger.error(
            f"Photo download bridge failed: {type(e).__name__}: {e!r} "
            f"(loop.running={loop.is_running()})"
        )
        return get_photo_path(profile_id, message_id)

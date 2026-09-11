# Photo download and cache — serves Telegram photos for Web UI.
#
# Фото хранятся только на серверах Telegram (не на диске).
# При первом запросе скачивает через Telethon, кэширует в media/<profile_id>/.
# Повторные запросы отдают из кэша.

from __future__ import annotations

import asyncio
from pathlib import Path

from loguru import logger

#: Корневая директория кэша фото.
MEDIA_ROOT = Path(__file__).resolve().parent.parent / "media"

# Глобальная ссылка на TelegramClient (устанавливается из main.py).
_telegram_client = None


def set_telegram_client(client) -> None:
    """Устанавливает TelegramClient для скачивания фото."""
    global _telegram_client
    _telegram_client = client


def get_telegram_client():
    """Возвращает текущий TelegramClient (или None)."""
    return _telegram_client


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


async def download_photo(
    chat_id: int,
    message_id: int,
    profile_id: int,
) -> Path | None:
    """Скачивает фото из Telegram и кэширует на диск.

    Возвращает путь к файлу или None при ошибке.
    """
    # Проверяем кэш
    cached = get_photo_path(profile_id, message_id)
    if cached:
        return cached

    client = get_telegram_client()
    if client is None:
        logger.warning("TelegramClient недоступен для скачивания фото")
        return None

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
        logger.error(f"Photo download failed: profile={profile_id} msg={message_id}: {e}")
        return None


def download_photo_sync(
    chat_id: int,
    message_id: int,
    profile_id: int,
) -> Path | None:
    """Синхронная обёртка для скачивания фото (из Flask thread)."""
    client = get_telegram_client()
    if client is None:
        return get_photo_path(profile_id, message_id)

    loop = client.loop if hasattr(client, "loop") else None
    if loop is None or loop.is_closed():
        logger.warning(
            f"Photo sync: loop недоступен "
            f"(loop={loop}, closed={loop.is_closed() if loop else None})"
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

# Photo download and cache — serves Telegram photos for Web UI.
#
# Фото хранятся только на серверах Telegram (не на диске).
# При первом запросе скачивает через Telethon, кэширует в media/<profile_id>/.
# Повторные запросы отдают из кэша.
#
# Поддержка нескольких аккаунтов: message_id в личном диалоге с Leo
# уникален для каждого аккаунта (разные number ranges: 697xxx vs 223xx).
# Поэтому фото скачивается ТЕМ аккаунтом, который реально ПОЛУЧИЛ сообщение
# (account_session из profile_messages); остальные используются только как
# fallback для старых записей без сессии. Иначе один и тот же message_id
# через чужой аккаунт может вернуть ФОТО ДРУГОЙ АНКЕТЫ.

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from loguru import logger

#: Корневая директория кэша фото.
MEDIA_ROOT = Path(__file__).resolve().parent.parent / "media"

# Глобальные ссылки на TelegramClient(s) (устанавливается из main.py).
_telegram_clients: list = []
_telegram_sessions: list[str] = []


def set_telegram_clients(clients: list) -> None:
    """Обратная совместимость: задать список клиентов без имён сессий."""
    global _telegram_clients, _telegram_sessions
    _telegram_clients = list(clients)
    _telegram_sessions = [""] * len(_telegram_clients)


def set_telegram_clients_with_sessions(
    clients: list, sessions: list[str] | None,
) -> None:
    """Задаёт список клиентов и имена их сессий (параллельные списки).

    Сессии нужны, чтобы качать фото тем аккаунтом, что реально получил
    сообщение анкеты (см. docstring модуля).
    """
    global _telegram_clients, _telegram_sessions
    _telegram_clients = list(clients)
    if sessions is None:
        _telegram_sessions = [""] * len(_telegram_clients)
    else:
        _telegram_sessions = list(sessions)
        if len(_telegram_sessions) < len(_telegram_clients):
            _telegram_sessions += [""] * (
                len(_telegram_clients) - len(_telegram_sessions)
            )


def set_telegram_client(client) -> None:
    """Обратная совместимость: один клиент."""
    global _telegram_clients, _telegram_sessions
    _telegram_clients = [client]
    _telegram_sessions = [""]


def get_telegram_clients() -> list:
    """Возвращает текущий список TelegramClient-ов."""
    return _telegram_clients


def get_telegram_client():
    """Обратная совместимость: первый клиент."""
    return _telegram_clients[0] if _telegram_clients else None


def _ordered_clients(account_session: str) -> list:
    """Клиенты в порядке предпочтения: сначала — аккаунт-получатель.

    Для записей без account_session (старые БД) — исходный порядок.
    """
    if not account_session:
        return list(_telegram_clients)
    preferred: list = []
    rest: list = []
    for client, sess in zip(_telegram_clients, _telegram_sessions):
        (preferred if sess == account_session else rest).append(client)
    return preferred + rest


def _safe_token(value: str) -> str:
    """Санитизирует имя сессии для имени файла кэша."""
    token = re.sub(r"[^A-Za-z0-9._-]", "_", value or "")
    return token


def _cache_filename(profile_id: int, message_id: int, account_session: str = '') -> Path:
    """Имя файла кэша фото (с учётом аккаунта, чтобы не подсунуть чужое фото)."""
    token = _safe_token(account_session)
    photo_dir = MEDIA_ROOT / str(profile_id)
    if token:
        return photo_dir / f"{message_id}.{token}.jpg"
    return photo_dir / f"{message_id}.jpg"


def get_photo_path(
    profile_id: int, message_id: int, account_session: str = '',
) -> Path | None:
    """Возвращает путь к кэшированному фото или None."""
    photo_file = _cache_filename(profile_id, message_id, account_session)
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
    account_session: str = '',
) -> Path | None:
    """Скачивает фото из Telegram и кэширует на диск.

    В первую очередь использует аккаунт, который реально получил сообщение
    (account_session из profile_messages), — иначе message_id чужого аккаунта
    может вернуть фото ДРУГОЙ анкеты. Остальные клиенты — fallback для старых
    записей без сессии.
    """
    logger.debug(
        f"[photo] start profile={profile_id} msg={message_id} "
        f"session={account_session!r}"
    )

    # Проверяем кэш (ключ включает аккаунт — чужие ранее скачанные фото не
    # переиспользуются)
    cached = get_photo_path(profile_id, message_id, account_session)
    if cached:
        return cached

    clients = get_telegram_clients()
    if not clients:
        logger.warning("TelegramClient недоступен для скачивания фото")
        return None

    for client in _ordered_clients(account_session):
        if client is None:
            continue
        loop = getattr(client, "_loop", None)
        if loop is None or loop.is_closed():
            continue
        result = await _try_download_one(client, chat_id, message_id, profile_id)
        if result:
            # Переименовываем в аккаунт-специфичный кэш (если сессия известна),
            # чтобы «украденное» через другой аккаунт фото не отдавалось впредь.
            if account_session:
                account_path = get_photo_path(
                    profile_id, message_id, account_session,
                )
                if account_path is None:
                    account_path = _cache_filename(
                        profile_id, message_id, account_session,
                    )
                    try:
                        result.replace(account_path)
                        result = account_path
                    except OSError as e:
                        logger.debug(f"[photo] cache rename failed: {e!r}")
            logger.info(
                f"Photo cached: profile={profile_id} msg={message_id} "
                f"session={account_session!r}"
            )
            return result

    logger.warning(
        f"Photo not found on any client: profile={profile_id} msg={message_id}"
    )
    return None


def download_photo_sync(
    chat_id: int,
    message_id: int,
    profile_id: int,
    account_session: str = '',
) -> Path | None:
    """Синхронная обёртка для скачивания фото (из Flask thread)."""
    cached = get_photo_path(profile_id, message_id, account_session)
    if cached:
        return cached

    clients = get_telegram_clients()
    if not clients:
        return cached

    # Ищем рабочий loop среди аккаунт-приоритетных клиентов
    preferred = _ordered_clients(account_session)
    loop = None
    for cl in preferred:
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
        return cached

    try:
        future = asyncio.run_coroutine_threadsafe(
            download_photo(
                chat_id, message_id, profile_id, account_session=account_session,
            ),
            loop,
        )
        return future.result(timeout=30)
    except Exception as e:
        logger.error(
            f"Photo download bridge failed: {type(e).__name__}: {e!r} "
            f"(loop.running={loop.is_running()})"
        )
        return get_photo_path(profile_id, message_id, account_session)

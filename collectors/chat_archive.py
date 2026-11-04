"""Архив Telegram для переписок, которые ведёт бот (владелец, 2026-11-04).

Пока бот сам ведёт девушку (привет → её ответ → анкета → сценарий) или она
замолчала насовсем, её чат лежит в архиве аккаунта — внимание владельца не
нужно. Переписка перешла к владельцу (``handoff``, ``owner``, ошибка, анкету
не нашли, нет доступа) или «Авто» выключено — чат достаётся из архива.

Раз в минуту (``run_history_sync``) проход по ``match_dialogs`` аккаунта: для
каждой строки — цель (``archive_target``) и метка состояния; метка совпадает
с сохранённой (``archive_mark``) — Telegram не трогаем. Её новое сообщение
Telegram сам вынимает из архива (чат не заглушён); бот ответил — метка
сменилась (``script_at`` / ``status``), чат снова уходит в архив.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

from collectors.match_greeter import flood_pause_sec
from collectors.match_intro import (
    ERROR,
    INTRO_SENT,
    MAIN_CHAT,
    NO_ACCESS,
    NO_PROFILE,
    NO_REPLY,
    OTHER_ACCOUNT,
    OWNER_CHAT,
    SCHEDULED,
    WAITING_REPLY,
)
from services.match_script_rules import ACTIVE_STEPS, DELETED, STOPPED

if TYPE_CHECKING:
    from database.database import Database

#: Папки Telegram: 1 — архив, 0 — основной список.
ARCHIVE_FOLDER: int = 1
MAIN_FOLDER: int = 0
#: Цели.
ARCHIVE: str = "archive"
UNARCHIVE: str = "unarchive"
#: Сколько чатов двигать за проход (первый запуск — много старых строк).
MAX_MOVES_PER_PASS: int = 15


def archive_target(row: dict, bot_on: bool) -> str | None:
    """Куда положить её чат: ``ARCHIVE`` / ``UNARCHIVE`` / None (не трогать).

    ``bot_on`` — автопереписка аккаунта включена («Авто»).
    """
    status = str(row.get("status") or "")
    step = str(row.get("script_step") or "")
    # Бот её ведёт: ждёт ответа на привет / анкета в очереди / идёт сценарий.
    if status in (WAITING_REPLY, SCHEDULED) or (
        status == INTRO_SENT and (step in ACTIVE_STEPS or not step)
    ):
        return ARCHIVE if bot_on else UNARCHIVE
    # Замолчала насовсем / пишет на другой аккаунт — внимания не нужно.
    if status in (NO_REPLY, MAIN_CHAT) or (
        status == INTRO_SENT and step == STOPPED
    ):
        return ARCHIVE
    # Переписка у владельца.
    if status in (OWNER_CHAT, NO_PROFILE, OTHER_ACCOUNT, NO_ACCESS, ERROR):
        return UNARCHIVE
    if status == INTRO_SENT:
        # handoff / owner / error; deleted — чата нет, не трогаем.
        return None if step == DELETED else UNARCHIVE
    return None


def archive_mark(row: dict, target: str) -> str:
    """Метка «цель + последнее действие бота»: сменилась — двигаем снова."""
    return "|".join(str(row.get(k) or "") for k in (
        "status", "script_step", "script_at", "wake_at", "sent_at",
    )) + f"|{target}"


class ChatArchiver:
    """Держит чаты, которые ведёт бот, в архиве аккаунта."""

    def __init__(self, db: Database | None, *, enabled: bool = True) -> None:
        self._db = db
        self.enabled = enabled

    async def sync(self, client: Any, session: str, bot_on: bool) -> int:
        """Один проход по аккаунту. Возвращает число перемещённых чатов."""
        if not self.enabled or self._db is None or client is None:
            return 0
        try:
            rows = await self._db.list_all_match_dialogs(session)
        except Exception as e:
            logger.error(f"Архив чатов: не прочитал переписки {session}: {e}")
            return 0
        moved = 0
        for row in rows:
            target = archive_target(row, bot_on)
            if target is None:
                continue
            mark = archive_mark(row, target)
            if mark == (row.get("archive_mark") or ""):
                continue
            if moved >= MAX_MOVES_PER_PASS:
                break
            user_id = int(row["user_id"])
            folder = ARCHIVE_FOLDER if target == ARCHIVE else MAIN_FOLDER
            try:
                peer = await client.get_input_entity(row.get("username") or user_id)
                await client.edit_folder(peer, folder)
            except Exception as e:
                if flood_pause_sec(e) is not None:
                    logger.warning(f"Архив чатов: {session} FloodWait — позже")
                    break
                # Не нашли / нет доступа — метку ставим, чтобы не долбить.
                logger.warning(
                    f"Архив чатов: {row.get('name') or user_id} на {session}: {e}"
                )
            else:
                moved += 1
                logger.info(
                    f"Архив чатов: {row.get('name') or user_id} на {session} — "
                    f"{'в архив' if target == ARCHIVE else 'из архива'}"
                )
            try:
                await self._db.update_match_dialog(
                    session, user_id, archive_mark=mark,
                )
            except Exception as e:
                logger.error(f"Архив чатов: не записал метку: {e}")
        return moved

"""Архив и папки Telegram для чатов девушек (владелец, 2026-11-04).

Все чаты девушек лежат в архиве аккаунта (``archive_bot_chats``): бот кладёт
каждый чат туда **один раз** (``dialog_contacts.archived_at``); вынул владелец —
обратно не кладём. Общение — через четыре папки (``match_intro.folders``):
New / Active / 50/50 / Auto, какая — решает ``services/chat_folders.py``.

Раз в минуту (``run_history_sync``) проход по аккаунту: папки Telegram читаются
одним запросом, ищутся по названию (нет — создаются при первом чате). Папка,
куда бот положил чат, пишется в ``dialog_contacts.chat_folder``; чат оказался
не там, где его оставил бот, — владелец перенёс сам → ``folder_manual = 1``,
бот этот чат больше не двигает. Изменения одной папки — одним запросом.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from loguru import logger
from telethon.tl.functions.messages import (
    GetDialogFiltersRequest,
    UpdateDialogFilterRequest,
)
from telethon.tl.types import DialogFilter, TextWithEntities

from collectors.match_greeter import flood_pause_sec
from services.chat_folders import FOLDERS, folder_target
from services.match_script_rules import DELETED

if TYPE_CHECKING:
    from app.config import ChatFoldersConfig
    from database.database import Database

#: Папка Telegram «Архив».
ARCHIVE_FOLDER: int = 1
#: Сколько чатов класть в архив / в папки за проход.
MAX_MOVES_PER_PASS: int = 15
#: Номера пользовательских папок Telegram.
_FILTER_IDS: range = range(2, 256)
#: Флаги папки «по типу чатов»: хоть один — папка не пустая.
_TYPE_FLAGS: tuple[str, ...] = (
    "contacts", "non_contacts", "groups", "broadcasts", "bots",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _title(f: Any) -> str:
    """Название папки (TextWithEntities или строка в старых слоях)."""
    t = getattr(f, "title", "")
    return str(getattr(t, "text", t) or "").strip()


def _peer_user(peer: Any) -> int | None:
    uid = getattr(peer, "user_id", None)
    return int(uid) if uid is not None else None


def _drop_user(f: Any, user_id: int) -> None:
    f.include_peers = [p for p in f.include_peers if _peer_user(p) != user_id]
    f.pinned_peers = [p for p in f.pinned_peers if _peer_user(p) != user_id]


def _is_empty(f: Any) -> bool:
    return not f.include_peers and not f.pinned_peers and not any(
        getattr(f, flag, False) for flag in _TYPE_FLAGS
    )


class ChatFolders:
    """Держит чаты девушек в архиве и раскладывает их по папкам."""

    def __init__(
        self,
        db: Database | None,
        config: ChatFoldersConfig,
        *,
        archive: bool = True,
        delete_silent: bool = False,
    ) -> None:
        self._db = db
        self._config = config
        self.archive = archive
        #: Молчит и после «АЛООО» (``NO_REPLY``, ни одного её сообщения) —
        #: чат удаляется у владельца, и старые тоже (владелец, 2026-11-04).
        self.delete_silent = delete_silent
        #: Чаты, у которых прошлое перемещение не дошло (сбой запроса):
        #: их состояние не сверяем с «владелец перенёс сам», просто чиним.
        self._uncertain: dict[str, set[int]] = {}
        #: Папка переполнена — предупреждение один раз за запуск.
        self._full_warned: set[tuple[str, str]] = set()

    @property
    def enabled(self) -> bool:
        return self.archive or bool(self._config.enabled)

    def _titles(self) -> dict[str, str]:
        cfg = self._config
        return {
            "new": cfg.new, "active": cfg.active,
            "fifty": cfg.fifty, "auto": cfg.auto,
        }

    async def sync(self, client: Any, session: str, bot_on: bool) -> int:
        """Один проход по аккаунту. Возвращает число перемещённых чатов."""
        if not self.enabled or self._db is None or client is None:
            return 0
        try:
            rows = await self._db.list_folder_dialogs(
                session, self._config.utc_offset_hours,
            )
        except Exception as e:
            logger.error(f"Папки чатов: не прочитал переписки {session}: {e}")
            return 0
        moved = 0
        if self.delete_silent:
            await self._delete_silent(client, session, rows)
        if self._config.enabled:
            moved += await self._sync_folders(client, session, rows, bot_on)
        if self.archive:
            moved += await self._sync_archive(client, session, rows)
        return moved

    async def _delete_silent(
        self, client: Any, session: str, rows: list[dict],
    ) -> None:
        """Молчит и после «АЛООО» — чат удаляется только у владельца, в т.ч.
        замолчавшие до 2026-11-04 (прод: Guangxi лежала в 50/50)."""
        deleted = 0
        for row in rows:
            if (row.get("intro_status") or "") != "NO_REPLY":
                continue
            if int(row.get("her_count") or 0) or row.get("chat_deleted_at"):
                continue
            if int(row.get("total") or 0) == 0 or int(row.get("folder_manual") or 0):
                continue
            if deleted >= MAX_MOVES_PER_PASS:
                break
            user_id = int(row["user_id"])
            name = row.get("name") or user_id
            try:
                peer = await client.get_input_entity(row.get("username") or user_id)
                await client.delete_dialog(peer, revoke=False)
            except Exception as e:
                if flood_pause_sec(e) is not None:
                    logger.warning(f"Папки чатов: {session} FloodWait — позже")
                    break
                logger.warning(f"Папки чатов: не удалил чат {name} на {session}: {e}")
                continue
            deleted += 1
            await self._db.set_dialog_chat_deleted(session, user_id, True)
            row["chat_deleted_at"] = _now()  # в этом же проходе — вон из папок
            logger.info(
                f"Папки чатов: {name} на {session} молчит и после «АЛООО» — "
                "чат удалён у тебя"
            )

    async def hide_now(
        self, client: Any, session: str, *, user_id: int = 0, username: str = "",
    ) -> None:
        """Сразу после стикера-привета её чат — в архив (владелец,
        2026-11-04: не ждать её ответа). Звук бот не трогает — уведомления
        владелец включает сам. Ошибка — WARNING, минутный проход доделает."""
        if not self.archive or self._db is None or client is None:
            return
        who: object = username or user_id
        if not who:
            return
        try:
            peer = await client.get_input_entity(who)
        except Exception as e:
            logger.warning(f"Папки чатов: не нашёл {who} на {session}: {e}")
            return
        uid = int(getattr(peer, "user_id", 0) or user_id or 0)
        fields: dict[str, object] = {}
        if self.archive:
            try:
                await client.edit_folder(peer, ARCHIVE_FOLDER)
                fields["archived_at"] = _now()
            except Exception as e:
                logger.warning(f"Архив чатов: {who} на {session}: {e}")
        if fields and uid:
            await self._db.update_dialog_contact(session, uid, **fields)
        logger.info(f"Папки чатов: {who} на {session} — в архив сразу")

    # ── Архив ────────────────────────────────────────────────────────

    async def _sync_archive(
        self, client: Any, session: str, rows: list[dict],
    ) -> int:
        """Каждый её чат — в архив один раз."""
        moved = 0
        for row in rows:
            if row.get("archived_at") or row.get("chat_deleted_at"):
                continue
            if int(row.get("total") or 0) == 0:
                continue  # пустой чат — в Telegram его нет
            if (row.get("script_step") or "") == DELETED:
                continue
            if moved >= MAX_MOVES_PER_PASS:
                break
            user_id = int(row["user_id"])
            try:
                peer = await client.get_input_entity(row.get("username") or user_id)
                await client.edit_folder(peer, ARCHIVE_FOLDER)
            except Exception as e:
                if flood_pause_sec(e) is not None:
                    logger.warning(f"Архив чатов: {session} FloodWait — позже")
                    break
                # Не нашли / нет доступа — отметку ставим, чтобы не долбить.
                logger.warning(
                    f"Архив чатов: {row.get('name') or user_id} на {session}: {e}"
                )
            else:
                moved += 1
                logger.info(
                    f"Архив чатов: {row.get('name') or user_id} на {session} — в архив"
                )
            await self._db.update_dialog_contact(session, user_id, archived_at=_now())
        return moved

    # ── Папки ────────────────────────────────────────────────────────

    async def _load_filters(self, client: Any, session: str) -> list[Any] | None:
        try:
            result = await client(GetDialogFiltersRequest())
        except Exception as e:
            logger.warning(f"Папки чатов: не прочитал папки {session}: {e}")
            return None
        filters = getattr(result, "filters", result)
        return list(filters or [])

    async def _sync_folders(
        self, client: Any, session: str, rows: list[dict], bot_on: bool,
    ) -> int:
        filters = await self._load_filters(client, session)
        if filters is None:
            return 0
        titles = self._titles()
        by_title = {t.casefold(): key for key, t in titles.items()}
        ours: dict[str, Any] = {}
        used_ids: set[int] = set()
        for f in filters:
            fid = getattr(f, "id", None)
            if isinstance(fid, int):
                used_ids.add(fid)
            if not isinstance(f, DialogFilter):
                continue  # «Все чаты», общие папки-ссылки — не наши
            key = by_title.get(_title(f).casefold())
            if key and key not in ours:
                ours[key] = f
        members: dict[int, set[str]] = {}
        for key, f in ours.items():
            for p in [*f.include_peers, *f.pinned_peers]:
                uid = _peer_user(p)
                if uid is not None:
                    members.setdefault(uid, set()).add(key)

        uncertain = self._uncertain.setdefault(session, set())
        cfg = self._config
        now = datetime.now(timezone.utc)
        plan: list[tuple[dict, int, set[str], str | None]] = []
        for row in rows:
            user_id = int(row["user_id"])
            actual = members.get(user_id, set())
            if int(row.get("folder_manual") or 0):
                continue
            stored = str(row.get("chat_folder") or "")
            expected = {stored} if stored else set()
            target = folder_target(
                row, bot_on=bot_on,
                active_days=cfg.active_days,
                silence_hours=cfg.silence_hours,
                unanswered_hours=cfg.unanswered_hours,
                bot_wait_hours=cfg.bot_wait_hours,
                now=now,
            )
            # Пропал из папок, а ему и не место в них (удалённый / пустой чат
            # Telegram сам выкидывает из папок) — это не перенос владельца
            # (прод 2026-11-04: Альбина, Алина).
            pruned = not actual and target is None
            if user_id not in uncertain and actual != expected and not pruned:
                # Чат не там, где его оставил бот, — перенёс владелец.
                where = min(actual) if actual else ""
                logger.info(
                    f"Папки чатов: {row.get('name') or user_id} на {session} — "
                    "владелец перенёс сам, бот больше не трогает"
                )
                await self._db.update_dialog_contact(
                    session, user_id, folder_manual=1, chat_folder=where,
                )
                continue
            want = {target} if target else set()
            if actual == want:
                if user_id in uncertain or stored != (target or ""):
                    await self._db.update_dialog_contact(
                        session, user_id, chat_folder=target or "",
                    )
                    uncertain.discard(user_id)
                continue
            plan.append((row, user_id, actual, target))

        if not plan:
            return 0
        changed: set[str] = set()
        done: list[tuple[dict, int, set[str], str | None]] = []
        adds = 0
        for row, user_id, actual, target in plan:
            name = row.get("name") or user_id
            if target is not None:
                if adds >= MAX_MOVES_PER_PASS:
                    continue
                f = ours.get(target)
                if f is None:
                    free = next((i for i in _FILTER_IDS if i not in used_ids), None)
                    if free is None:
                        continue
                    used_ids.add(free)
                    f = DialogFilter(
                        id=free,
                        title=TextWithEntities(text=titles[target], entities=[]),
                        pinned_peers=[], include_peers=[], exclude_peers=[],
                    )
                    ours[target] = f
                    logger.info(f"Папки чатов: {session} — создаю папку «{titles[target]}»")
                if len(f.include_peers) + len(f.pinned_peers) >= cfg.max_chats_per_folder:
                    if (session, target) not in self._full_warned:
                        self._full_warned.add((session, target))
                        logger.warning(
                            f"Папки чатов: папка «{titles[target]}» на {session} "
                            f"заполнена ({cfg.max_chats_per_folder}) — новые чаты не кладу"
                        )
                    continue
                try:
                    peer = await client.get_input_entity(row.get("username") or user_id)
                except Exception as e:
                    if flood_pause_sec(e) is not None:
                        logger.warning(f"Папки чатов: {session} FloodWait — позже")
                        break
                    logger.warning(f"Папки чатов: {name} на {session}: {e}")
                    continue
                f.include_peers.append(peer)
                changed.add(target)
                adds += 1
            for old in actual - {target}:
                _drop_user(ours[old], user_id)
                changed.add(old)
            done.append((row, user_id, actual, target))

        failed: set[str] = set()
        for key in sorted(changed):
            f = ours[key]
            try:
                if _is_empty(f):
                    # Пустую папку Telegram не держит — убираем, появится снова.
                    await client(UpdateDialogFilterRequest(id=f.id))
                    ours.pop(key)
                else:
                    await client(UpdateDialogFilterRequest(id=f.id, filter=f))
            except Exception as e:
                failed.add(key)
                logger.warning(f"Папки чатов: папка «{titles[key]}» на {session}: {e}")

        moved = 0
        for row, user_id, actual, target in done:
            keys = actual | ({target} if target else set())
            if keys & failed:
                uncertain.add(user_id)
                continue
            uncertain.discard(user_id)
            await self._db.update_dialog_contact(
                session, user_id, chat_folder=target or "",
            )
            moved += 1
            logger.info(
                f"Папки чатов: {row.get('name') or user_id} на {session} — "
                f"{titles[target] if target else 'убран из папок'}"
            )
        return moved


__all__ = ["ARCHIVE_FOLDER", "FOLDERS", "MAX_MOVES_PER_PASS", "ChatFolders"]

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

import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from loguru import logger
from telethon.tl.functions.account import (
    GetNotifySettingsRequest,
    UpdateNotifySettingsRequest,
)
from telethon.tl.functions.messages import (
    GetDialogFiltersRequest,
    UpdateDialogFilterRequest,
)
from telethon.tl.functions.users import GetUsersRequest
from telethon.tl.types import (
    DialogFilter,
    InputNotifyPeer,
    InputPeerNotifySettings,
    TextWithEntities,
    UserStatusEmpty,
)

from collectors.match_greeter import flood_pause_sec
from services.chat_folders import AUTO, FOLDERS, folder_target
from services.dialog_outcome import parse_time
from services.match_script_rules import DELETED

if TYPE_CHECKING:
    from app.config import ChatFoldersConfig
    from database.database import Database

#: Папка Telegram «Архив».
ARCHIVE_FOLDER: int = 1
#: Сколько чатов класть в архив / в папки за проход.
MAX_MOVES_PER_PASS: int = 15
#: «Заглушить навсегда» для Telegram (mute_until).
MUTE_FOREVER: int = 2**31 - 1
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
        #: Чаты, чей собеседник не находится («No user has…», «Could not find
        #: input entity»): предупреждаем один раз за запуск и больше не ищем.
        self._dead: set[tuple[str, int]] = set()
        #: Сколько peer-запросов для звука ещё можно за проход.
        self._mute_budget: int = MAX_MOVES_PER_PASS

    @property
    def enabled(self) -> bool:
        return self.archive or bool(self._config.enabled)

    def _titles(self) -> dict[str, str]:
        cfg = self._config
        return {
            "new": cfg.new, "active": cfg.active,
            "fifty": cfg.fifty, "auto": cfg.auto, "blocked": cfg.blocked,
        }

    async def _peer(
        self, client: Any, session: str, user_id: int, username: str = "",
        name: object = "",
    ) -> Any | None:
        """InputPeer её чата. Не нашёлся — WARNING один раз за запуск, дальше
        молча None. FloodWait пробрасывается вызывающему."""
        key = (session, int(user_id))
        if key in self._dead:
            return None
        try:
            return await client.get_input_entity(username or user_id)
        except Exception as e:
            if flood_pause_sec(e) is not None:
                raise
            if isinstance(e, ValueError):
                self._dead.add(key)
            logger.warning(f"Папки чатов: не нашёл {name or user_id} на {session}: {e}")
            return None

    async def _set_mute(
        self, client: Any, session: str, user_id: int, peer: Any, mute: bool,
    ) -> bool:
        """Выключить / вернуть звук её чата. True — запрос прошёл."""
        try:
            await client(UpdateNotifySettingsRequest(
                peer=InputNotifyPeer(peer=peer),
                settings=InputPeerNotifySettings(
                    mute_until=MUTE_FOREVER if mute else 0,
                ),
            ))
        except Exception as e:
            if flood_pause_sec(e) is not None:
                logger.warning(f"Папки чатов: {session} FloodWait — звук позже")
            else:
                logger.warning(f"Папки чатов: звук {user_id} на {session}: {e}")
            return False
        await self._db.update_dialog_contact(
            session, user_id, notify_mark="bot_muted" if mute else "on",
        )
        return True

    @staticmethod
    def _muted_now(settings: Any) -> bool:
        """Чат уже заглушён (mute_until в будущем): int или datetime."""
        until = getattr(settings, "mute_until", None)
        if isinstance(until, datetime):
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            return until > datetime.now(timezone.utc)
        if isinstance(until, (int, float)) and not isinstance(until, bool):
            return until > time.time()
        return False

    async def _update_mute(
        self, client: Any, session: str, row: dict, user_id: int,
        target: str | None, peers: dict[int, Any],
    ) -> None:
        """Звук следует за Auto: положили в Auto — глушим (папка глушит только
        текущих участников), вышел из Auto — возвращаем, но только если глушил
        бот. Чаты вне Auto и не заглушённые ботом не трогаем (2026-11-07).
        Уже заглушён владельцем → ``owner_muted``, никогда не возвращаем."""
        mark = str(row.get("notify_mark") or "")
        if target == AUTO:
            if not self._config.mute_auto or mark in ("bot_muted", "owner_muted"):
                return
            peer = peers.get(user_id)
            if peer is None:
                if self._mute_budget <= 0:
                    return
                self._mute_budget -= 1
                try:
                    peer = await self._peer(
                        client, session, user_id, row.get("username") or "",
                        row.get("name"),
                    )
                except Exception:
                    self._mute_budget = 0  # FloodWait — до следующего прохода
                    return
            if peer is None:
                return
            try:
                current = await client(GetNotifySettingsRequest(
                    InputNotifyPeer(peer=peer),
                ))
            except Exception as e:
                if flood_pause_sec(e) is not None:
                    self._mute_budget = 0
                logger.warning(f"Папки чатов: настройки звука {user_id} на {session}: {e}")
                return
            if self._muted_now(current):
                await self._db.update_dialog_contact(
                    session, user_id, notify_mark="owner_muted",
                )
                row["notify_mark"] = "owner_muted"
                return
            if await self._set_mute(client, session, user_id, peer, True):
                row["notify_mark"] = "bot_muted"
        elif mark == "bot_muted" and target is not None:
            try:
                peer = peers.get(user_id) or await self._peer(
                    client, session, user_id, row.get("username") or "",
                    row.get("name"),
                )
            except Exception:
                return
            if peer is not None and await self._set_mute(
                client, session, user_id, peer, False,
            ):
                row["notify_mark"] = "on"

    async def _mute_pending(
        self, client: Any, session: str, auto_rows: list[dict],
        peers: dict[int, Any],
    ) -> None:
        """Чаты, уже лежащие в Auto (до деплоя / сбой звука), — заглушить."""
        for row in auto_rows:
            if self._mute_budget <= 0:
                break
            await self._update_mute(
                client, session, row, int(row["user_id"]), AUTO, peers,
            )

    async def _check_blocked(
        self, client: Any, session: str, rows: list[dict],
    ) -> None:
        """Эвристика «она нас заблокировала» (владелец, 2026-11-07): Telegram
        блок не сообщает, но у заблокировавшей users.GetUsers отдаёт пустой
        статус и обычно нет фото. Раз в ``block_check_hours`` на чат, пачкой."""
        cfg = self._config
        if not cfg.check_blocked:
            return
        now = datetime.now(timezone.utc)
        limit = now - timedelta(hours=cfg.block_check_hours)
        batch: list[tuple[dict, Any]] = []
        for row in rows:
            if len(batch) >= cfg.block_check_batch:
                break
            if row.get("chat_deleted_at") or int(row.get("total") or 0) == 0:
                continue
            if (row.get("script_step") or "") == DELETED:
                continue
            checked = parse_time(row.get("block_checked_at"))
            if checked is not None and checked > limit:
                continue
            try:
                peer = await self._peer(
                    client, session, int(row["user_id"]),
                    row.get("username") or "", row.get("name"),
                )
            except Exception:
                break  # FloodWait
            if peer is not None:
                batch.append((row, peer))
        if not batch:
            return
        try:
            users = await client(GetUsersRequest([p for _, p in batch]))
        except Exception as e:
            if flood_pause_sec(e) is not None:
                logger.warning(f"Папки чатов: {session} FloodWait — ЧС позже")
            else:
                logger.warning(f"Папки чатов: не проверил ЧС на {session}: {e}")
            return
        by_id = {int(u.id): u for u in users if getattr(u, "id", None) is not None}
        stamp = now.isoformat()
        for row, _peer in batch:
            user = by_id.get(int(row["user_id"]))
            uid = int(row["user_id"])
            fields: dict[str, object] = {"block_checked_at": stamp}
            if (
                user is None or type(user).__name__ == "UserEmpty"
                or getattr(user, "deleted", False) is True
            ):
                # Нет данных / удалённый аккаунт — не «заблокировала».
                await self._db.update_dialog_contact(session, uid, **fields)
                row["block_checked_at"] = stamp
                continue
            status = getattr(user, "status", None)
            photo = getattr(user, "photo", None)
            suspect = (status is None or isinstance(status, UserStatusEmpty)) and (
                photo is None or type(photo).__name__ == "UserProfilePhotoEmpty"
            )
            was = bool(row.get("blocked_at"))
            hits = int(row.get("block_hits") or 0)
            if suspect:
                hits += 1
                fields["block_hits"] = hits
                # ЧС — только после двух проверок подряд.
                if hits >= 2 and not was:
                    fields["blocked_at"] = stamp
                    logger.info(
                        f"Папки чатов: {row.get('name') or uid} на {session} — "
                        "похоже, заблокировала"
                    )
            else:
                fields["block_hits"] = 0
                if was:
                    fields["blocked_at"] = None
            await self._db.update_dialog_contact(session, uid, **fields)
            row["block_checked_at"] = stamp
            row["block_hits"] = fields.get("block_hits", hits)
            if "blocked_at" in fields:
                row["blocked_at"] = fields["blocked_at"]

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
        await self._check_blocked(client, session, rows)
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
                peer = await self._peer(
                    client, session, user_id, row.get("username") or "", name,
                )
                if peer is None:
                    continue
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
        if user_id and (session, int(user_id)) in self._dead:
            return
        try:
            peer = await client.get_input_entity(who)
        except Exception as e:
            if isinstance(e, ValueError) and user_id:
                self._dead.add((session, int(user_id)))
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
                peer = await self._peer(
                    client, session, user_id, row.get("username") or "",
                    row.get("name"),
                )
                if peer is None:
                    # Мёртвый чат: отметку ставим, чтобы не долбить.
                    await self._db.update_dialog_contact(
                        session, user_id, archived_at=_now(),
                    )
                    continue
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
        self._mute_budget = MAX_MOVES_PER_PASS
        auto_rows: list[dict] = []
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
                if target == AUTO:
                    auto_rows.append(row)
                continue
            plan.append((row, user_id, actual, target))

        if not plan:
            await self._mute_pending(client, session, auto_rows, {})
            return 0
        changed: set[str] = set()
        done: list[tuple[dict, int, set[str], str | None]] = []
        adds = 0
        peers: dict[int, Any] = {}
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
                    peer = await self._peer(
                        client, session, user_id, row.get("username") or "", name,
                    )
                except Exception:
                    logger.warning(f"Папки чатов: {session} FloodWait — позже")
                    break
                if peer is None:
                    continue
                peers[user_id] = peer
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
            await self._update_mute(client, session, row, user_id, target, peers)
        await self._mute_pending(client, session, auto_rows, peers)
        return moved


__all__ = ["ARCHIVE_FOLDER", "FOLDERS", "MAX_MOVES_PER_PASS", "ChatFolders"]

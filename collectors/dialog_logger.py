"""Логирование переписок с девушками после взаимного лайка.

Цель — данные для анализа и будущего алгоритма общения: каждое сообщение в
личке с девушкой-матчем (её, владельца и бота) пишется в ``dialog_messages``.

Источники:
- ``track`` — каждый матч (MATCH в чате Leo) заводит девушку в
  ``dialog_contacts`` (независимо от привета и автопереписки);
- ``log_event`` — живые события Telethon: её входящие и исходящие владельца
  (с телефона/другого клиента). Сообщения не из реестра не пишутся (переписка
  владельца с другими людьми сюда не попадает);
- ``log_sent`` — отправки самого бота (стикер-привет, её анкета): Telethon не
  присылает событий на собственные отправки, поэтому пишем их сразу, ``bot``;
- ``sync`` — сверка с историей Telegram (пропуски, пока бот лежал, и старые
  переписки): раз в ``SYNC_INTERVAL_SEC`` по ``SYNC_BATCH`` чатов, каждый не
  чаще ``SYNC_CONTACT_EVERY_SEC``. Заодно дозаводит старые матчи из
  ``match_responses`` (по @username, не больше ``RESOLVE_BATCH`` за проход).

Текст переписки в лог (loguru / debug-консоль сайта) НЕ пишется — только в БД.
Ошибки не ломают pipeline (всё в try/except).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from loguru import logger

from collectors.match_greeter import flood_pause_sec

if TYPE_CHECKING:
    from database.database import Database
    from models.raw import ParsedMatch

#: Кто написал сообщение (dialog_messages.sent_by).
HER: str = "her"
OWNER: str = "owner"
BOT: str = "bot"

#: Проход сверки истории — не чаще, чем раз в столько секунд (на аккаунт).
SYNC_INTERVAL_SEC: float = 600.0
#: Сколько чатов сверять за проход (getHistory на каждый).
SYNC_BATCH: int = 15
#: Один и тот же чат сверяем не чаще, чем раз в столько секунд.
SYNC_CONTACT_EVERY_SEC: float = 3600.0
#: Сколько сообщений качать: первая сверка чата / последующие.
SYNC_FIRST_LIMIT: int = 1000
SYNC_NEXT_LIMIT: int = 200
#: Сколько старых матчей (по @username) резолвить за проход.
RESOLVE_BATCH: int = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def media_kind(msg: Any) -> str:
    """Вид вложения сообщения Telethon ('' — просто текст)."""
    if getattr(msg, "sticker", None):
        return "sticker"
    if getattr(msg, "video_note", None):
        return "video_note"
    if getattr(msg, "voice", None):
        return "voice"
    if getattr(msg, "gif", None):
        return "gif"
    if getattr(msg, "photo", None):
        return "photo"
    if getattr(msg, "video", None):
        return "video"
    if getattr(msg, "audio", None):
        return "audio"
    if getattr(msg, "document", None):
        return "document"
    if getattr(msg, "media", None) is not None:
        return "other"
    return ""


def message_text(msg: Any, kind: str) -> str:
    """Текст сообщения; у стикера без текста — его эмодзи."""
    text = getattr(msg, "message", None)
    if not isinstance(text, str):
        text = getattr(msg, "text", None)
    text = text if isinstance(text, str) else ""
    if not text and kind == "sticker":
        emoji = getattr(getattr(msg, "file", None), "emoji", None)
        if isinstance(emoji, str):
            text = emoji
    return text


def _iso(date: object) -> str:
    if isinstance(date, datetime):
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return date.isoformat()
    return _now().isoformat()


def _reply_to(msg: Any) -> int | None:
    value = getattr(msg, "reply_to_msg_id", None)
    return int(value) if isinstance(value, int) else None


class DialogLogger:
    """Журнал переписок с девушками-матчами (dialog_contacts/messages)."""

    def __init__(self, db: Database | None, *, enabled: bool = True) -> None:
        self._db = db
        self._enabled = enabled and db is not None
        # Кэш user_id девушек на аккаунт (чтобы не ходить в БД на каждое
        # личное сообщение владельца).
        self._contacts: dict[str, set[int]] = {}
        self._last_sync: dict[str, float] = {}
        # Старые матчи, чей @username не удалось резолвить (не повторяем).
        self._unresolvable: set[tuple[str, str]] = set()

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def _contact_ids(self, account_session: str) -> set[int]:
        ids = self._contacts.get(account_session)
        if ids is None:
            ids = await self._db.get_dialog_contact_ids(account_session)
            self._contacts[account_session] = ids
        return ids

    async def is_tracked(self, account_session: str, user_id: int) -> bool:
        if not self._enabled or not user_id:
            return False
        try:
            return int(user_id) in await self._contact_ids(account_session)
        except Exception as e:
            logger.error(f"DialogLog: не прочитал реестр: {e}")
            return False

    async def track(
        self,
        client: Any,
        account_session: str,
        match: ParsedMatch,
        profile_id: int | None = None,
    ) -> int:
        """Матч → девушка в реестре. Возвращает её user_id (0 — не узнали)."""
        if not self._enabled:
            return 0
        user_id = int(match.telegram_user_id or 0)
        if not user_id and match.telegram_username and client is not None:
            try:
                peer = await client.get_input_entity(match.telegram_username)
                user_id = int(getattr(peer, "user_id", 0) or 0)
            except Exception as e:
                logger.error(
                    f"DialogLog: не нашёл user_id {match.telegram_username}: {e}"
                )
                return 0
        if not user_id:
            return 0
        await self._add_contact(
            account_session, user_id,
            username=match.telegram_username or "",
            name=match.name or "",
            profile_id=profile_id,
        )
        return user_id

    async def _add_contact(
        self, account_session: str, user_id: int, **fields: Any,
    ) -> None:
        try:
            added = await self._db.upsert_dialog_contact(
                account_session, user_id, **fields,
            )
        except Exception as e:
            logger.error(f"DialogLog: не завёл девушку {user_id}: {e}")
            return
        self._contacts.setdefault(account_session, set()).add(int(user_id))
        if added:
            logger.info(
                f"DialogLog: слежу за перепиской с {fields.get('name') or user_id} "
                f"(id {user_id}) на {account_session or '?'}"
            )

    async def log_event(
        self, account_session: str, msg: Any, *, outgoing: bool,
        user_id: int | None = None,
    ) -> bool:
        """Живое сообщение лички → журнал, если собеседница в реестре.

        Входящее — её; исходящее, пришедшее событием, — владельца (свои
        отправки бот пишет сам через ``log_sent``). ``user_id`` — id чата
        (в личке = id собеседницы); без него берётся ``msg.chat_id``.
        """
        if not self._enabled or msg is None:
            return False
        raw_id = user_id if user_id is not None else getattr(msg, "chat_id", 0)
        if not isinstance(raw_id, int) or not raw_id:
            return False
        user_id = raw_id
        if not await self.is_tracked(account_session, user_id):
            return False
        return await self._save(
            account_session, user_id, msg, sent_by=OWNER if outgoing else HER,
        )

    async def log_sent(
        self, account_session: str, user_id: int, sent: Any,
    ) -> int:
        """Отправки бота (стикер-привет, пересланная анкета) → ``bot``."""
        if not self._enabled or not user_id or sent is None:
            return 0
        items = sent if isinstance(sent, list) else [sent]
        saved = 0
        for msg in items:
            if msg is None or not isinstance(getattr(msg, "id", None), int):
                continue
            if await self._save(account_session, int(user_id), msg, sent_by=BOT):
                saved += 1
        return saved

    async def _save(
        self, account_session: str, user_id: int, msg: Any, *, sent_by: str,
    ) -> bool:
        kind = media_kind(msg)
        try:
            return await self._db.save_dialog_message(
                account_session, user_id, int(msg.id),
                is_outgoing=sent_by != HER,
                sent_by=sent_by,
                message_date=_iso(getattr(msg, "date", None)),
                text=message_text(msg, kind),
                media_type=kind,
                is_forward=getattr(msg, "fwd_from", None) is not None,
                reply_to_message_id=_reply_to(msg),
            )
        except Exception as e:
            logger.error(f"DialogLog: не сохранил сообщение {user_id}: {e}")
            return False

    # ── Сверка с историей Telegram ───────────────────────────────────

    async def sync(self, client: Any, account_session: str) -> int:
        """Один проход сверки аккаунта. Возвращает число досохранённых."""
        if not self._enabled or client is None:
            return 0
        now = _now()
        last = self._last_sync.get(account_session)
        if last is not None and now.timestamp() - last < SYNC_INTERVAL_SEC:
            return 0
        self._last_sync[account_session] = now.timestamp()
        if not await self._resolve_old_matches(client, account_session):
            return 0
        older = (now - timedelta(seconds=SYNC_CONTACT_EVERY_SEC)).isoformat()
        try:
            rows = await self._db.get_dialog_sync_queue(
                account_session, older_than=older, limit=SYNC_BATCH,
            )
        except Exception as e:
            logger.error(f"DialogLog: не прочитал очередь сверки: {e}")
            return 0
        added = 0
        for row in rows:
            got = await self._sync_chat(client, account_session, row)
            if got is None:  # FloodWait — остальное в следующий проход
                break
            added += got
        if added:
            logger.info(
                f"DialogLog: сверка {account_session or '?'} — "
                f"досохранено {added} сообщений"
            )
        return added

    async def _sync_chat(
        self, client: Any, account_session: str, row: dict,
    ) -> int | None:
        """Сверка одного чата. None — FloodWait (прервать проход)."""
        user_id = int(row["user_id"])
        last_tm = int(row.get("last_tm_id") or 0)
        limit = SYNC_NEXT_LIMIT if last_tm else SYNC_FIRST_LIMIT
        messages: list[Any] = []
        try:
            peer = await client.get_input_entity(row.get("username") or user_id)
            async for msg in client.iter_messages(
                peer, limit=limit, min_id=last_tm,
            ):
                messages.append(msg)
        except Exception as e:
            logger.error(f"DialogLog: сверка чата {user_id}: {e}")
            if flood_pause_sec(e) is not None:
                return None
            await self._db.mark_dialog_synced(account_session, user_id)
            return 0
        added = 0
        for msg in reversed(messages):  # от старых к новым
            if not isinstance(getattr(msg, "id", None), int):
                continue
            if await self._save(
                account_session, user_id, msg,
                sent_by=self._history_sender(msg),
            ):
                added += 1
        await self._db.mark_dialog_synced(account_session, user_id)
        return added

    @staticmethod
    def _history_sender(msg: Any) -> str:
        """Автор сообщения из истории: её / бот (стикер-привет, пересланная
        анкета) / владелец. Свежие отправки бота уже записаны как ``bot``."""
        if not getattr(msg, "out", False):
            return HER
        if getattr(msg, "fwd_from", None) is not None:
            return BOT
        if media_kind(msg) == "sticker":
            return BOT
        return OWNER

    async def _resolve_old_matches(
        self, client: Any, account_session: str,
    ) -> bool:
        """Дозаводит старые матчи по @username. False — FloodWait."""
        try:
            rows = await self._db.list_untracked_matches(account_session, limit=50)
        except Exception as e:
            logger.error(f"DialogLog: не прочитал старые матчи: {e}")
            return True
        todo = [
            r for r in rows
            if (account_session, str(r["telegram_username"]).lower())
            not in self._unresolvable
        ][:RESOLVE_BATCH]
        for row in todo:
            username = str(row["telegram_username"])
            try:
                peer = await client.get_input_entity(username)
                user_id = int(getattr(peer, "user_id", 0) or 0)
            except Exception as e:
                if flood_pause_sec(e) is not None:
                    logger.warning(f"DialogLog: FloodWait при резолве @{username}")
                    return False
                logger.warning(f"DialogLog: @{username} не резолвится: {e}")
                user_id = 0
            if not user_id:
                self._unresolvable.add((account_session, username.lower()))
                continue
            await self._add_contact(
                account_session, user_id,
                username=username,
                name=row.get("name") or "",
                profile_id=row.get("profile_id"),
                matched_at=row.get("matched_at"),
            )
        return True

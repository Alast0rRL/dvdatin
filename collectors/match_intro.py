"""Автопереписка после взаимного лайка: начало знакомства (режим AUTO).

Бот уже отправил девушке стикер-привет (``MatchGreeter``). Дальше:

1. Привет ушёл → ``enroll``: строка ``match_dialogs`` (WAITING_REPLY) с её
   user_id и анкетой (profile_id).
2. Она ответила (событие личного сообщения → ``note_reply``; пропущенное при
   рестарте ловит сверка истории ``_check_replies``) → SCHEDULED, срок
   ``due_at`` = её ответ + ``delay_min`` (30 мин).
3. В срок (``process``, из фонового цикла коллектора раз в минуту) — пересылка
   её же анкеты из чата Leo (фото + подпись, только id этого аккаунта) и
   закреп у обоих (``pm_oneside=False``) → INTRO_SENT.
   Анкету видел только ДРУГОЙ аккаунт (перенаправление лички) — аккаунты
   друг другу ничего не пишут (владелец, 2026-11-01), но аккаунт карточки
   скачивает её фото и текст, а аккаунт лички шлёт ей КОПИЮ новым сообщением
   (владелец, 2026-11-02: @PzKpfwLin14 осталась без анкеты). Аккаунт карточки
   не вошёл — статус OTHER_ACCOUNT, анкету шлёт владелец.
   Пересылается та карточка, что шла перед уведомлением о матче
   (``card_session`` + ``card_message_id``), а не «последний показ профиля»:
   под одним profile_id бывают разные девушки с тем же «имя, возраст».

Правила:
- только в режимах ``match_intro.modes`` (по умолчанию AUTO); вне их ответы не
  планируются, а запланированное ждёт;
- один раз на девушку и аккаунт;
- спам-блок/FloodWait — не ошибка: срок сдвигается на паузу, повтор позже;
- владелец сам написал ей после привета (наше исходящее новее привета) —
  анкету не шлём, статус OWNER_CHAT (прод 2026-10-29: анкета ушла посреди
  его переписки);
- ошибки не ломают pipeline (всё в try/except, лог ERROR).
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from loguru import logger

from collectors.match_greeter import flood_pause_sec
from models.raw import ParsedMatch

if TYPE_CHECKING:
    from app.config import MatchIntroConfig
    from database.database import Database

#: Статусы переписки (match_dialogs.status).
WAITING_REPLY: str = "WAITING_REPLY"
SCHEDULED: str = "SCHEDULED"
INTRO_SENT: str = "INTRO_SENT"
NO_PROFILE: str = "NO_PROFILE"
#: Владелец сам написал ей после привета — анкету не шлём (переписку ведёт он).
OWNER_CHAT: str = "OWNER_CHAT"
#: Её карточка — в ленте другого аккаунта (перенаправление лички), а клиента
#: этого аккаунта нет — копию не сделать, анкету шлёт владелец.
OTHER_ACCOUNT: str = "OTHER_ACCOUNT"
ERROR: str = "ERROR"

#: Сверка ответов по истории — не чаще, чем раз в столько секунд.
REPLY_CHECK_INTERVAL_SEC: float = 600.0
#: Сколько ожидающих переписок проверять за раз (getHistory на каждую).
REPLY_CHECK_BATCH: int = 20
#: Сколько последних сообщений её чата смотреть при сверке.
REPLY_CHECK_HISTORY: int = 10
#: Сколько последних сообщений её чата смотреть перед отправкой анкеты:
#: есть ли там сообщения владельца после привета.
OWNER_CHECK_HISTORY: int = 50




def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class MatchIntro:
    """Её ответ на привет → через N минут её анкета + закреп у обоих."""

    def __init__(
        self, config: MatchIntroConfig, db: Database | None, leo_chat_id: int,
    ) -> None:
        self._config = config
        self._db = db
        self._leo_chat_id = leo_chat_id
        # Когда последний раз сверяли ответы по истории (на аккаунт).
        self._last_reply_check: dict[str, float] = {}
        # Колбэк «бот отправил девушке» (session, user_id, сообщения) —
        # журнал переписок (DialogLogger.log_sent). Ошибки не важны.
        self.on_sent: Callable[[str, int, Any], Awaitable[Any]] | None = None
        # Клиент аккаунта по имени сессии — скачать карточку из ЕГО чата Leo,
        # когда личка перенаправлена на другой аккаунт (копия анкеты).
        self.client_for: Callable[[str], Any] | None = None

    def allowed_in(self, mode: str) -> bool:
        return bool(self._config.enabled) and mode in self._config.modes

    async def enroll(
        self,
        client: Any,
        account_session: str,
        match: ParsedMatch,
        profile_id: int | None,
        *,
        card_session: str = "",
        card_message_id: int = 0,
    ) -> bool:
        """Привет ушёл → ждём её ответа. True — переписка заведена.

        ``card_session`` / ``card_message_id`` — её карточка в чате Leo перед
        уведомлением о матче (её и пересылаем, см. ``_find_card``).
        """
        if self._db is None or not self._config.enabled:
            return False
        user_id = match.telegram_user_id or 0
        if not user_id and match.telegram_username:
            try:
                peer = await client.get_input_entity(match.telegram_username)
                user_id = int(getattr(peer, "user_id", 0) or 0)
            except Exception as e:
                logger.error(
                    f"MatchIntro: не нашёл user_id {match.telegram_username}: {e}"
                )
                return False
        if not user_id:
            return False
        try:
            added = await self._db.add_match_dialog(
                account_session, user_id,
                username=match.telegram_username or "",
                name=match.name or "",
                profile_id=profile_id,
                card_session=card_session,
                card_message_id=card_message_id,
            )
        except Exception as e:
            logger.error(f"MatchIntro: не завёл переписку: {e}")
            return False
        if added:
            logger.info(
                f"MatchIntro: жду ответа {match.name} (id {user_id}, "
                f"анкета {profile_id}) на {account_session}"
            )
        return added

    async def note_reply(
        self,
        account_session: str,
        user_id: int,
        mode: str,
        at: datetime | None = None,
    ) -> bool:
        """Её входящее в личке → планируем анкету. True — запланировано."""
        if self._db is None or not self.allowed_in(mode) or not user_id:
            return False
        try:
            row = await self._db.get_match_dialog(account_session, user_id)
        except Exception as e:
            logger.error(f"MatchIntro: не прочитал переписку: {e}")
            return False
        if not row or row.get("status") != WAITING_REPLY:
            return False
        replied = at or _now()
        due = replied + timedelta(minutes=float(self._config.delay_min))
        try:
            await self._db.update_match_dialog(
                account_session, user_id,
                status=SCHEDULED,
                replied_at=replied.isoformat(),
                due_at=due.isoformat(),
            )
        except Exception as e:
            logger.error(f"MatchIntro: не запланировал анкету: {e}")
            return False
        logger.info(
            f"MatchIntro: {row.get('name') or user_id} ответила — анкета "
            f"уйдёт в {due.astimezone().strftime('%H:%M')}"
        )
        return True

    async def process(
        self, client: Any, account_session: str, mode: str,
    ) -> list[tuple[dict, str]]:
        """Один проход: сверка ответов + отправка созревших. [(строка, статус)]."""
        if self._db is None or not self.allowed_in(mode):
            return []
        await self._check_replies(client, account_session, mode)
        try:
            rows = await self._db.list_match_dialogs(account_session, SCHEDULED)
        except Exception as e:
            logger.error(f"MatchIntro: не прочитал очередь: {e}")
            return []
        now = _now()
        results: list[tuple[dict, str]] = []
        for row in rows:
            due = _parse_dt(row.get("due_at"))
            if due is None or due > now:
                continue
            status = await self.send_intro(client, account_session, row)
            results.append((row, status))
        return results

    async def send_intro(
        self, client: Any, account_session: str, row: dict,
    ) -> str:
        """Пересылает ей её анкету и закрепляет. Статус: INTRO_SENT /
        NO_PROFILE / OTHER_ACCOUNT / ERROR / FLOOD (повтор позже)."""
        user_id = int(row["user_id"])
        name = row.get("name") or str(user_id)
        try:
            peer = await client.get_input_entity(row.get("username") or user_id)
            owner_wrote = await self._owner_wrote(client, peer, row)
        except Exception as e:
            pause = flood_pause_sec(e)
            if pause is not None:
                due = _now() + timedelta(seconds=pause)
                await self._update(
                    account_session, user_id, due_at=due.isoformat(),
                    error=str(e)[:300],
                )
                return "FLOOD"
            logger.error(f"MatchIntro: не прочитал чат {name}: {e}")
            await self._update(
                account_session, user_id, status=ERROR, error=str(e)[:300],
            )
            return ERROR
        if owner_wrote:
            logger.info(
                f"MatchIntro: с {name} уже переписывается владелец — анкету не шлю"
            )
            await self._update(account_session, user_id, status=OWNER_CHAT)
            return OWNER_CHAT
        source, ids = await self._find_card(row, account_session)
        if not ids:
            logger.warning(
                f"MatchIntro: анкета {name} не найдена — не шлю"
            )
            await self._update(account_session, user_id, status=NO_PROFILE)
            return NO_PROFILE
        src_client = None
        if source != account_session:
            # Анкета в ленте другого аккаунта: аккаунты друг другу не пишут,
            # поэтому не пересылка, а копия (фото + текст) с аккаунта лички.
            src_client = self.client_for(source) if self.client_for else None
            if src_client is None:
                logger.info(
                    f"MatchIntro: анкета {name} — на {source}, его клиента "
                    "нет — не шлю"
                )
                await self._update(account_session, user_id, status=OTHER_ACCOUNT)
                return OTHER_ACCOUNT
        try:
            if src_client is not None:
                sent = await self._send_copy(client, peer, src_client, ids)
            else:
                from_peer: Any = self._leo_chat_id
                sent = await client.forward_messages(peer, ids, from_peer=from_peer)
        except Exception as e:
            pause = flood_pause_sec(e)
            if pause is not None:
                due = _now() + timedelta(seconds=pause)
                logger.warning(
                    f"MatchIntro: {account_session} в спам-блоке ({e}) — "
                    f"анкету {name} пришлю после {pause // 60} мин"
                )
                await self._update(
                    account_session, user_id, due_at=due.isoformat(),
                    error=str(e)[:300],
                )
                return "FLOOD"
            logger.error(f"MatchIntro: не переслал анкету {name}: {e}")
            await self._update(
                account_session, user_id, status=ERROR, error=str(e)[:300],
            )
            return ERROR
        if self.on_sent is not None:
            try:
                await self.on_sent(account_session, user_id, sent)
            except Exception as e:
                logger.error(f"MatchIntro: журнал переписки: {e}")
        pinned = 0
        error = ""
        if self._config.pin:
            target = self._pin_target(sent)
            if target is not None:
                try:
                    await client.pin_message(
                        peer, target, notify=False, pm_oneside=False,
                    )
                    pinned = 1
                except Exception as e:
                    # Анкета уже у неё — закреп не критичен, не повторяем.
                    error = f"pin: {e}"[:300]
                    logger.error(f"MatchIntro: не закрепил анкету {name}: {e}")
        row["pinned"] = pinned
        # Последнее сообщение бота в её чате — граница для сценария знакомства.
        ids_sent = [
            getattr(m, "id", None)
            for m in (sent if isinstance(sent, list) else [sent])
        ]
        row["sent_msg_id"] = max(
            (i for i in ids_sent if isinstance(i, int)), default=0,
        )
        await self._update(
            account_session, user_id,
            status=INTRO_SENT, sent_at=_now().isoformat(),
            pinned=pinned, error=error,
        )
        logger.info(
            f"MatchIntro: отправил {name} её анкету"
            f"{' и закрепил 📌' if pinned else ''}"
        )
        return INTRO_SENT

    async def _send_copy(
        self, client: Any, peer: Any, src_client: Any, ids: list[int],
    ) -> Any:
        """Копия карточки из чата Leo аккаунта ``src_client``: фото/видео
        скачиваются в память, ``client`` шлёт их ей одним альбомом с текстом
        анкеты (как у Leo — подпись на первом файле)."""
        msgs = await src_client.get_messages(self._leo_chat_id, ids=ids)
        if not isinstance(msgs, list):
            msgs = [msgs]
        caption = ""
        entities: list | None = None
        files: list[io.BytesIO] = []
        for m in sorted(
            (m for m in msgs if m is not None), key=lambda m: m.id,
        ):
            text = getattr(m, "message", "") or ""
            if text.strip() and not caption:
                caption = text
                entities = list(getattr(m, "entities", None) or [])
            if getattr(m, "media", None) is None:
                continue
            data = await src_client.download_media(m, file=bytes)
            if not data:
                continue
            buf = io.BytesIO(data)
            buf.name = "video.mp4" if getattr(m, "video", None) else "photo.jpg"
            files.append(buf)
        if not files and not caption:
            raise ValueError("карточка пуста — нечего копировать")
        if not files:
            return await client.send_message(
                peer, caption, formatting_entities=entities or None,
                parse_mode=None, link_preview=False,
            )
        if len(files) == 1:
            return await client.send_file(
                peer, files[0], caption=caption,
                formatting_entities=entities or None, parse_mode=None,
            )
        captions = [caption] + [""] * (len(files) - 1)
        kwargs: dict[str, Any] = {"caption": captions, "parse_mode": None}
        if entities:
            kwargs["formatting_entities"] = [entities] + [[]] * (len(files) - 1)
        return await client.send_file(peer, files, **kwargs)

    @staticmethod
    async def _owner_wrote(client: Any, peer: Any, row: dict) -> bool:
        """Есть ли в её чате наше исходящее новее привета (пишет владелец).

        Стикер-привет отправлен ДО ``greeted_at``, а анкету бот ещё не слал —
        значит любое исходящее позже ``greeted_at`` написал владелец.
        """
        greeted = _parse_dt(row.get("greeted_at"))
        if greeted is None:
            return False
        async for msg in client.iter_messages(peer, limit=OWNER_CHECK_HISTORY):
            date = getattr(msg, "date", None)
            if not isinstance(date, datetime):
                continue
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            if date <= greeted:
                break
            if getattr(msg, "out", False):
                return True
        return False

    async def _find_card(
        self, row: dict, account_session: str,
    ) -> tuple[str, list[int]]:
        """(аккаунт, id сообщений анкеты в его чате Leo).

        Известна карточка матча (``card_session`` + ``card_message_id``) —
        только она: одна запись profiles может склеить разных девушек с тем
        же «имя, возраст», и «последний показ профиля» — чужая анкета (прод
        2026-10-30: @Miamikii получила анкету другой «Мяу, 18»). Карточка
        не нашлась — не шлём ничего.

        Карточка неизвестна (старые строки, дозапись после спам-блока, имя
        не совпало с карточкой перед матчем) — тоже ничего: поиск «по имени /
        последнему показу» слал чужих (прод 2026-10-30, @nrvmux).
        """
        profile_id = row.get("profile_id")
        card_session = row.get("card_session") or ""
        card_id = int(row.get("card_message_id") or 0)
        if profile_id is None or self._db is None or not (card_id and card_session):
            return account_session, []
        try:
            ids = await self._db.get_profile_card_message_ids(
                int(profile_id), card_session, anchor=card_id,
            )
        except Exception as e:
            logger.error(f"MatchIntro: не получил сообщения анкеты: {e}")
            ids = []
        return card_session, ids

    @staticmethod
    def _pin_target(sent: Any) -> Any:
        """Что закреплять: сообщение с подписью анкеты, иначе первое."""
        items = sent if isinstance(sent, list) else [sent]
        items = [m for m in items if m is not None]
        if not items:
            return None
        for m in items:
            if (getattr(m, "text", "") or getattr(m, "message", "") or "").strip():
                return m
        return items[0]

    async def _check_replies(
        self, client: Any, account_session: str, mode: str,
    ) -> None:
        """Ответ мог прийти, пока бот лежал: сверка по истории её чата.

        Не чаще ``REPLY_CHECK_INTERVAL_SEC`` и не больше ``REPLY_CHECK_BATCH``
        чатов за раз; только приветствия не старше ``reply_check_hours``.
        """
        now_ts = _now().timestamp()
        last = self._last_reply_check.get(account_session)
        if last is not None and now_ts - last < REPLY_CHECK_INTERVAL_SEC:
            return
        self._last_reply_check[account_session] = now_ts
        try:
            rows = await self._db.list_match_dialogs(
                account_session, WAITING_REPLY, limit=200,
            )
        except Exception as e:
            logger.error(f"MatchIntro: не прочитал ожидающих: {e}")
            return
        horizon = _now() - timedelta(hours=float(self._config.reply_check_hours))
        fresh = [
            r for r in rows
            if (_parse_dt(r.get("greeted_at")) or horizon) > horizon
        ]
        for row in fresh[-REPLY_CHECK_BATCH:]:
            user_id = int(row["user_id"])
            greeted = _parse_dt(row.get("greeted_at"))
            try:
                peer = await client.get_input_entity(row.get("username") or user_id)
                first: datetime | None = None
                async for msg in client.iter_messages(
                    peer, limit=REPLY_CHECK_HISTORY,
                ):
                    if getattr(msg, "out", False):
                        continue
                    date = getattr(msg, "date", None)
                    if not isinstance(date, datetime):
                        continue
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)
                    if greeted is not None and date < greeted:
                        continue
                    first = date if first is None else min(first, date)
            except Exception as e:
                pause = flood_pause_sec(e)
                logger.error(f"MatchIntro: сверка ответа {user_id}: {e}")
                if pause is not None:
                    return
                continue
            if first is not None:
                await self.note_reply(account_session, user_id, mode, at=first)

    async def _update(
        self, account_session: str, user_id: int, **fields: object,
    ) -> None:
        try:
            await self._db.update_match_dialog(account_session, user_id, **fields)
        except Exception as e:
            logger.error(f"MatchIntro: не записал переписку: {e}")

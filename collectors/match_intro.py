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

Она молчит ``match_script.give_up_hours`` (24 ч) после стикера → в ближайшие
``wake_up_hour`` (22:00) «АЛООО ПРОСЫПАЕМСЯ» (``wake_at`` / ``wake_msg_id``,
статус остаётся WAITING_REPLY — её ответ планирует анкету как обычно); молчит
ещё 24 ч → NO_REPLY, бот больше не пишет (схема владельца 2026-11-02).

Она написала первой (до стикера) — это тоже ответ: сверка истории берёт её
сообщения не старше ``EARLY_REPLY_SEC`` до привета, срок анкеты считается от
привета.

Правила:
- только в режимах ``match_intro.modes`` (по умолчанию AUTO); вне их ответы не
  планируются, а запланированное ждёт;
- один раз на девушку и аккаунт;
- спам-блок/FloodWait — не ошибка: срок сдвигается на паузу, повтор позже;
- владелец сам написал ей после привета (наше исходящее новее привета) —
  анкету не шлём, статус OWNER_CHAT (прод 2026-10-29: анкета ушла посреди
  его переписки); её чат пуст (владелец удалил чат) — тоже OWNER_CHAT;
- ошибки не ломают pipeline (всё в try/except, лог ERROR).
"""

from __future__ import annotations

import copy
import io
import random
import re
from datetime import datetime, timedelta, timezone
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from loguru import logger

from collectors.match_greeter import flood_pause_sec
from models.raw import ParsedMatch
from services.match_script_rules import decide_before_card, in_hour_window

if TYPE_CHECKING:
    from app.config import MatchIntroConfig, MatchScriptConfig
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
#: Матч без @username, а личка перенаправлена на другой аккаунт: оттуда ей не
#: написать — бот молчит, пишет владелец с аккаунта матча.
NO_ACCESS: str = "NO_ACCESS"
#: Она написала на аккаунт матча, а личка перенаправлена сюда — бот молчит.
MAIN_CHAT: str = "MAIN_CHAT"
#: Молчит сутки и после «АЛООО ПРОСЫПАЕМСЯ» — бот больше не пишет.
NO_REPLY: str = "NO_REPLY"
#: Результат прохода: бот отправил «АЛООО ПРОСЫПАЕМСЯ» (статус не меняется).
WAKE_UP_SENT: str = "WAKE_UP_SENT"
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
#: Стикер-привет бота: его дата в Telegram может быть чуть позже
#: ``greeted_at`` (часы сервера). Исходящий стикер в этом окне — бот, не владелец.
GREETING_STICKER_SKEW_SEC: float = 60.0
#: Она написала первой: её сообщение не старше стольких секунд до привета —
#: тоже ответ (матч мог висеть, пока владелец не нажал «показать»).
EARLY_REPLY_SEC: float = 24 * 3600.0
#: «АЛООО» — в первое вечернее окно после суток тишины; позже стольких часов
#: после срока не шлём (иначе на запуске его получили бы все старые матчи).
#: Это только добор, если бот лежал в своё окно: ещё одно окно, не больше.
WAKE_UP_LATE_HOURS: float = 24.0
#: Сколько ожидающих ответа читаем из БД: выборка идёт «старые первыми», малый
#: лимит забивался бы давними строками и свежие не доходили до «АЛООО».
WAKE_SCAN_LIMIT: int = 5000

#: Шапка карточки той, кто лайкнула первой: «Кому-то понравилась твоя
#: анкета (и ещё 31):» — бывает без пробела перед скобкой и без счётчика.
LIKED_HEAD_RE = re.compile(
    r"^(?P<head>[^\n]*понравилась твоя анкета)"
    r"(?:(?P<pre>\s*\(и\s+[её]щ[её]\s+)(?P<num>\d+)\))?"
    r"(?P<tail>[^\n]*)",
    re.IGNORECASE,
)


def boost_liked_count(
    text: str, entities: list | None, lo: int, hi: int,
) -> tuple[str, list | None] | None:
    """Шапка «понравилась твоя анкета (и ещё N)» → N случайное из [lo, hi].

    Без счётчика («…анкета:») — дописывает « (и ещё N)». Нет шапки — None.
    Сущности форматирования после правки сдвигаются (шапка — символы BMP,
    так что сдвиг в символах = сдвиг в UTF-16).
    """
    m = LIKED_HEAD_RE.match(text or "")
    if m is None:
        return None
    count = str(random.randint(lo, hi))
    if m.group("num") is not None:
        start, end = m.span("num")
        new = count
    else:
        tail = m.group("tail").strip()
        if tail and not tail.startswith(":"):
            return None  # уведомление «…анкета! посмотрим кто», не карточка
        start = end = m.end("head")
        new = f" (и ещё {count})"
    delta = len(new) - (end - start)
    out = text[:start] + new + text[end:]
    if not entities or not delta:
        return out, entities
    shifted = []
    for e in entities:
        e = copy.copy(e)
        if e.offset >= end:
            e.offset += delta
        elif e.offset + e.length > start:
            e.length += delta
        shifted.append(e)
    return out, shifted


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
        script_config: MatchScriptConfig | None = None,
    ) -> None:
        self._config = config
        # Сроки и текст «АЛООО ПРОСЫПАЕМСЯ» — из сценария; None — без него.
        self._script = script_config
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
        # Наше «Лайкнули друг друга…» до анкеты: id (не считать владельцем при
        # повторе после FloodWait) и «уже слали» — по (аккаунт, user_id).
        self._know_ids: dict[tuple[str, int], int] = {}

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
        results: list[tuple[dict, str]] = await self._wake_silent(
            client, account_session, mode,
        )
        try:
            rows = await self._db.list_match_dialogs(account_session, SCHEDULED)
        except Exception as e:
            logger.error(f"MatchIntro: не прочитал очередь: {e}")
            return results
        now = _now()
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
            know_id = self._know_ids.get((account_session, user_id), 0)
            owner_wrote = await self._owner_wrote(
                client, peer, row, know_id, self._know_texts(),
            )
            her_texts = [] if owner_wrote else await self._her_texts(client, peer, row)
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
        plan = decide_before_card(her_texts)
        if plan == "handoff":
            # Её вопрос не про знакомство — анкету не шлём, отвечает владелец.
            logger.info(
                f"MatchIntro: {name} спросила своё до анкеты — бот молчит, "
                "владельцу"
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
        if plan in ("know", "taking") and not await self._send_know_before(
            client, peer, account_session, user_id, name, kind=plan,
        ):
            return "FLOOD"
        if plan == "taking":
            # Ответ про «берём» дан — после анкеты второй раз бот не отвечает.
            variants = str(row.get("script_variant") or "")
            row["script_variant"] = f"{variants},taking:0" if variants else "taking:0"
            await self._update(
                account_session, user_id, script_variant=row["script_variant"],
            )
        try:
            if src_client is not None:
                sent = await self._send_copy(client, peer, src_client, ids)
            else:
                own = await self._liked_card_messages(client, ids)
                if own is not None:
                    # Шапка «понравилась твоя анкета (и ещё N)»: пересылку не
                    # изменить — копия со своим N (владелец, 2026-11-04).
                    sent = await self._send_copy(client, peer, client, ids, msgs=own)
                else:
                    from_peer: Any = self._leo_chat_id
                    sent = await client.forward_messages(
                        peer, ids, from_peer=from_peer,
                    )
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

    async def _send_know_before(
        self, client: Any, peer: Any, account_session: str, user_id: int,
        name: str, kind: str = "know",
    ) -> bool:
        """«Лайкнули друг друга в Дайвинчике)» (``kind='know'``) или «Анкета
        понравилась, вот и беру)» (``'taking'``) перед анкетой (один раз).
        False — FloodWait, повтор позже. Прочие ошибки — анкета уходит так."""
        key = (account_session, user_id)
        cfg = self._script
        pool = (
            cfg.texts.taking if cfg and kind == "taking"
            else (cfg.texts.know_before if cfg else None)
        )
        texts = [t for t in (pool or []) if t]
        if key in self._know_ids or not texts:
            return True
        try:
            sent = await client.send_message(peer, random.choice(texts))
        except Exception as e:
            pause = flood_pause_sec(e)
            if pause is not None:
                await self._update(
                    account_session, user_id,
                    due_at=(_now() + timedelta(seconds=pause)).isoformat(),
                    error=str(e)[:300],
                )
                return False
            logger.warning(f"MatchIntro: не отправил «знакомы» {name}: {e}")
            return True
        sent_id = getattr(sent, "id", None)
        self._know_ids[key] = sent_id if isinstance(sent_id, int) else -1
        if self.on_sent is not None:
            try:
                await self.on_sent(account_session, user_id, sent)
            except Exception as e:
                logger.error(f"MatchIntro: журнал переписки: {e}")
        return True

    @staticmethod
    async def _her_texts(client: Any, peer: Any, row: dict) -> list[str]:
        """Её тексты до анкеты (после привета, включая ранние). Только в
        память для разбора — в лог не пишутся."""
        greeted = _parse_dt(row.get("greeted_at"))
        early = greeted - timedelta(seconds=EARLY_REPLY_SEC) if greeted else None
        texts: list[str] = []
        async for msg in client.iter_messages(peer, limit=OWNER_CHECK_HISTORY):
            if getattr(msg, "out", False):
                continue
            date = getattr(msg, "date", None)
            if isinstance(date, datetime):
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                if early is not None and date < early:
                    break
            text = getattr(msg, "message", None) or getattr(msg, "text", None) or ""
            texts.append(text if isinstance(text, str) else "")
        return texts

    def _liked_range(self) -> tuple[int, int] | None:
        rng = list(getattr(self._config, "liked_count", None) or [])
        if len(rng) != 2:
            return None
        lo, hi = sorted(int(x) for x in rng)
        return lo, hi

    async def _liked_card_messages(
        self, client: Any, ids: list[int],
    ) -> list | None:
        """Сообщения карточки, если в подписи шапка «понравилась твоя анкета»
        (её надо править — значит копия), иначе None (обычная пересылка).
        Сбой чтения — None: анкета уйдёт пересылкой, как раньше."""
        if self._liked_range() is None:
            return None
        try:
            msgs = await client.get_messages(self._leo_chat_id, ids=ids)
        except Exception as e:
            logger.warning(f"MatchIntro: не прочитал карточку для шапки: {e}")
            return None
        if not isinstance(msgs, list):
            msgs = [msgs]
        for m in msgs:
            text = getattr(m, "message", None)
            if isinstance(text, str) and (
                boost_liked_count(text, None, 1, 1) is not None
            ):
                return msgs
        return None

    async def _send_copy(
        self, client: Any, peer: Any, src_client: Any, ids: list[int],
        msgs: list | None = None,
    ) -> Any:
        """Копия карточки из чата Leo аккаунта ``src_client``: фото/видео
        скачиваются в память, ``client`` шлёт их ей одним альбомом с текстом
        анкеты (как у Leo — подпись на первом файле). Шапка «понравилась
        твоя анкета (и ещё N)» — со случайным N (``liked_count``)."""
        if msgs is None:
            msgs = await src_client.get_messages(self._leo_chat_id, ids=ids)
        if not isinstance(msgs, list):
            msgs = [msgs]
        caption = ""
        entities: list | None = None
        files: list[io.BytesIO] = []
        liked = self._liked_range()
        for m in sorted(
            (m for m in msgs if m is not None), key=lambda m: m.id,
        ):
            text = getattr(m, "message", "") or ""
            if text.strip() and not caption:
                caption = text
                entities = list(getattr(m, "entities", None) or [])
                if liked is not None:
                    boosted = boost_liked_count(caption, entities, *liked)
                    if boosted is not None:
                        caption, entities = boosted
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

    def _know_texts(self) -> set[str]:
        cfg = self._script
        pool = (
            list(cfg.texts.know_before) + list(cfg.texts.taking) if cfg else []
        )
        return {t.strip() for t in pool if t and t.strip()}

    @staticmethod
    async def _owner_wrote(
        client: Any, peer: Any, row: dict, skip_id: int = 0,
        know_texts: set[str] | frozenset[str] = frozenset(),
    ) -> bool:
        """Есть ли в её чате наше исходящее новее привета (пишет владелец).

        Анкету бот ещё не слал, так что исходящее позже ``greeted_at`` —
        владелец. Кроме стикер-привета самого бота: ``greeted_at`` — часы
        сервера сразу после отправки, а дату стикеру ставит Telegram (целые
        секунды по своим часам) и она бывает на секунду позже (прод
        2026-11-02, Кулебяка/ари/Вика: бот принял свой стикер за владельца
        и не отправил анкету). Стикер в первые ``GREETING_STICKER_SKEW_SEC``
        после привета — это он.
        """
        greeted = _parse_dt(row.get("greeted_at"))
        if greeted is None:
            return False
        skew = timedelta(seconds=GREETING_STICKER_SKEW_SEC)
        # «АЛООО ПРОСЫПАЕМСЯ» бота — тоже не владелец.
        wake_id = int(row.get("wake_msg_id") or 0)
        seen = False
        async for msg in client.iter_messages(peer, limit=OWNER_CHECK_HISTORY):
            seen = True
            msg_id = getattr(msg, "id", None)
            if msg_id is not None and msg_id in (wake_id, skip_id) and msg_id > 0:
                continue
            date = getattr(msg, "date", None)
            if not isinstance(date, datetime):
                continue
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            if date <= greeted:
                break
            if not getattr(msg, "out", False):
                continue
            is_sticker = getattr(msg, "sticker", None) is not None
            if is_sticker and date <= greeted + skew:
                continue
            # Наше «Лайкнули друг друга…» (id потерян после рестарта / сбоя
            # карточки) — тоже не владелец.
            body = getattr(msg, "message", None) or getattr(msg, "text", None)
            if isinstance(body, str) and body.strip() in know_texts:
                continue
            # Без текста (приватность): только что за сообщение и когда.
            logger.info(
                f"MatchIntro: исходящее владельца id {getattr(msg, 'id', '?')} "
                f"({'стикер' if is_sticker else 'сообщение'}) в "
                f"{date.isoformat()}, привет в {greeted.isoformat()}"
            )
            return True
        if not seen:
            # Чат пуст, хотя стикер-привет ушёл — владелец удалил чат: бот
            # молчит, иначе его сообщение вернуло бы чат (владелец, 2026-11-03).
            logger.info("MatchIntro: её чат пуст (удалён) — бот молчит")
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
                account_session, WAITING_REPLY, limit=WAKE_SCAN_LIMIT,
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
            try:
                peer = await client.get_input_entity(row.get("username") or user_id)
                first = await self._first_reply(client, peer, row)
            except Exception as e:
                pause = flood_pause_sec(e)
                logger.error(f"MatchIntro: сверка ответа {user_id}: {e}")
                if pause is not None:
                    return
                continue
            if first is not None:
                await self.note_reply(account_session, user_id, mode, at=first)

    @staticmethod
    async def _first_reply(
        client: Any, peer: Any, row: dict,
    ) -> datetime | None:
        """Когда она впервые написала (её входящее в чате) или None.

        Написала первой, до стикера (не раньше ``EARLY_REPLY_SEC``), — тоже
        ответ, срок считается от привета.
        """
        greeted = _parse_dt(row.get("greeted_at"))
        early = (
            greeted - timedelta(seconds=EARLY_REPLY_SEC)
            if greeted is not None else None
        )
        first: datetime | None = None
        async for msg in client.iter_messages(peer, limit=REPLY_CHECK_HISTORY):
            if getattr(msg, "out", False):
                continue
            date = getattr(msg, "date", None)
            if not isinstance(date, datetime):
                continue
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            if early is not None and date < early:
                continue
            first = date if first is None else min(first, date)
        if first is not None and greeted is not None and first < greeted:
            first = greeted
        return first

    async def _delete_silent_chat(
        self, client: Any, peer: Any, account_session: str, user_id: int,
        name: str,
    ) -> None:
        """Молчит и после «АЛООО» — чат удаляется только у владельца
        (владелец, 2026-11-04); на сайте — «Неудачные · чат удалён»."""
        try:
            await client.delete_dialog(peer, revoke=False)
        except Exception as e:
            logger.warning(f"MatchIntro: не удалил чат {name}: {e}")
            return
        logger.info(f"MatchIntro: {name} молчит и после «АЛООО» — чат удалён у тебя")
        try:
            await self._db.set_dialog_chat_deleted(account_session, user_id, True)
        except Exception as e:
            logger.warning(f"MatchIntro: не отметил удалённый чат {name}: {e}")

    async def _wake_silent(
        self, client: Any, account_session: str, mode: str,
    ) -> list[tuple[dict, str]]:
        """Молчит после стикера: сутки → «АЛООО ПРОСЫПАЕМСЯ» вечером, ещё
        сутки → NO_REPLY. [(строка, WAKE_UP_SENT | NO_REPLY | OWNER_CHAT)].

        Перед действием — её чат: ответила (событие пропущено) → анкета по
        плану; пишет владелец → OWNER_CHAT, бот молчит.
        """
        cfg = self._script
        if cfg is None or not cfg.enabled or mode not in cfg.modes:
            return []
        texts = [t for t in (cfg.texts.wake_up or []) if t]
        if not texts:
            return []
        try:
            rows = await self._db.list_match_dialogs(
                account_session, WAITING_REPLY, limit=WAKE_SCAN_LIMIT,
            )
        except Exception as e:
            logger.error(f"MatchIntro: не прочитал ожидающих: {e}")
            return []
        now = _now()
        limit = timedelta(hours=float(cfg.give_up_hours))
        evening = in_hour_window(
            now, cfg.wake_up_hour, cfg.wake_up_window_hours,
            cfg.utc_offset_hours,
        )
        results: list[tuple[dict, str]] = []
        for row in rows:
            woke = _parse_dt(row.get("wake_at"))
            greeted = _parse_dt(row.get("greeted_at"))
            uid = row.get("user_id")
            if woke is not None:
                if now - woke < limit:
                    logger.debug(f"MatchIntro: wake {uid}: после «АЛООО» ещё не сутки")
                    continue
            elif greeted is None:
                logger.debug(f"MatchIntro: wake {uid}: нет greeted_at")
                continue
            elif now - greeted < limit:
                logger.debug(f"MatchIntro: wake {uid}: ещё нет суток после привета")
                continue
            elif now - greeted >= limit + timedelta(hours=WAKE_UP_LATE_HOURS):
                # Её вечер для «АЛООО» прошёл (старые приветы до 2026-11-02,
                # бот лежал) — не пишем, ждём её ответа как раньше.
                logger.debug(f"MatchIntro: wake {uid}: окно для «АЛООО» прошло")
                continue
            elif not evening:
                logger.debug(f"MatchIntro: wake {uid}: не вечернее окно")
                continue
            user_id = int(row["user_id"])
            name = row.get("name") or str(user_id)
            try:
                peer = await client.get_input_entity(row.get("username") or user_id)
                first = await self._first_reply(client, peer, row)
                if first is not None:
                    await self.note_reply(account_session, user_id, mode, at=first)
                    continue
                if await self._owner_wrote(
                    client, peer, row, 0, self._know_texts(),
                ):
                    logger.info(
                        f"MatchIntro: с {name} переписывается владелец — "
                        "бот молчит"
                    )
                    await self._update(account_session, user_id, status=OWNER_CHAT)
                    results.append((row, OWNER_CHAT))
                    continue
                if woke is not None:
                    logger.info(
                        f"MatchIntro: {name} молчит и после «АЛООО» — бот "
                        "больше не пишет"
                    )
                    await self._update(account_session, user_id, status=NO_REPLY)
                    results.append((row, NO_REPLY))
                    if cfg.delete_silent_chat:
                        await self._delete_silent_chat(
                            client, peer, account_session, user_id, name,
                        )
                    continue
                sent = await client.send_message(peer, texts[0])
            except Exception as e:
                pause = flood_pause_sec(e)
                logger.error(f"MatchIntro: «АЛООО» {name}: {e}")
                if pause is not None:
                    return results
                continue
            if self.on_sent is not None:
                try:
                    await self.on_sent(account_session, user_id, sent)
                except Exception as e:
                    logger.error(f"MatchIntro: журнал переписки: {e}")
            sent_id = getattr(sent, "id", None)
            await self._update(
                account_session, user_id, wake_at=_now().isoformat(),
                wake_msg_id=sent_id if isinstance(sent_id, int) else 0,
            )
            logger.info(f"MatchIntro: {name} молчит сутки — написал «АЛООО»")
            results.append((row, WAKE_UP_SENT))
        return results

    async def _update(
        self, account_session: str, user_id: int, **fields: object,
    ) -> None:
        try:
            await self._db.update_match_dialog(account_session, user_id, **fields)
        except Exception as e:
            logger.error(f"MatchIntro: не записал переписку: {e}")

# Коллектор сообщений Telegram: перехват, сохранение RAW, классификация.
# Сохраняет RAW ПЕРВЫМ — парсер не может помешать сохранению.

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from telethon import events
from telethon.tl.types import (
    MessageMediaDocument,
    MessageMediaGeo,
    MessageMediaPhoto,
    MessageMediaPoll,
    MessageMediaWebPage,
)

from collectors.auto_action import (
    AutoActionEngine,
    DISLIKE_TEXT,
    LIKE_TEXT,
)
from collectors.dedup import Dedup
from collectors.dvinchik_parser import DvinchikParser
from collectors.match_greeter import MatchGreeter
from collectors.raw_worker import DvinchikRawWorker, RawTask
from core.types import Mode
from models.decision import AIDecision
from models.raw import MessageType, ParsedMatch

if TYPE_CHECKING:
    from telethon import TelegramClient

    from app.config import AppConfig
    from database.database import Database
    from services.filter_service import FilterService
    from services.profile_service import ProfileService

console = Console(force_terminal=True)

_MEDIA_TYPE_MAP: dict[type, str] = {
    MessageMediaPhoto: "photo",
    MessageMediaDocument: "document",
    MessageMediaGeo: "geo",
    MessageMediaWebPage: "webpage",
    MessageMediaPoll: "poll",
}

#: Кнопка Дайвинчика, которой продолжают показ анкет после исчерпания ленты:
#: Leo присылает промо «🚀 Смотреть анкеты», нажатие = отправка текста кнопки.
#: Парсим по частичному вхождению, т.к. эмодзи могут различаться.
VIEW_BUTTON_FRAGMENT: str = "Смотреть анкеты"

#: Кнопка «Смотреть анкеты» в ГЛАВНОМ МЕНЮ Leo. Когда лента исчерпана (или
#: после переключения аккаунта) бот шлёт текстовое меню «1. Смотреть анкеты.
#: 2. Моя анкета. …» с reply-клавиатурой [«1 🚀», «2», «3», «4»] — вот тут
#: VIEW_BUTTON_FRAGMENT не встречается (текст кнопки «1 🚀»), и лента ВСТАЁТ
#: до ручного нажатия. Жмём кнопку с этим фрагментом; остальные («2»/«3»/«4» —
#: «Моя анкета», «Я больше не хочу никого искать», Premium) НИКОГДА не жмём.
MENU_VIEW_FRAGMENT: str = "1 🚀"

#: Ключ app_state: последний сбой автопотока — бот не смог продолжить ленту
#: анкет (ни активной анкеты, ни кнопки продолжения, ни капчи). Хранит JSON
#: для баннера на сайте; владельцу шлётся уведомление в Telegram.
STREAM_ERROR_STATE_KEY: str = "stream_error"
#: Как часто повторно уведомлять владельцу о сбое (сек) — чтобы не спамить,
#: если лента стоит долго.
STREAM_ERROR_NOTIFY_COOLDOWN_SEC: float = 600.0
#: Сколько последних сообщений чата Leo попадает в диагностический снимок.
STREAM_ERROR_SNAPSHOT_LIMIT: int = 12
#: Ключ app_state: карточка «Ты понравился…», на которой остановлен поиск
#: (``<account_session>:<message_id>``) — чтобы уведомлять владельца один раз.
MATCH_STOP_STATE_KEY: str = "match_stop_card"
#: Режим до остановки на карточке взаимного лайка — его возвращает кнопка
#: «Продолжить» на сайте (``resume_after_match_stop``).
MATCH_STOP_PREV_MODE_KEY: str = "match_stop_prev_mode"

#: Штатные сообщения Leo, которые парсер не относит ни к одному типу, но
#: которые приходят на КАЖДОЙ анкете: в лог — DEBUG, а не WARNING.
ROUTINE_LEO_TEXTS: frozenset[str] = frozenset({"✨\U0001F50D"})  # ✨🔍

#: Сверка ленты с Telegram (``sync_chat_history``): сколько последних сообщений
#: чата Leo читать у каждого аккаунта, как часто и какой «возраст» сообщения
#: оставлять живому хендлеру (моложе — не трогаем, иначе отнимем его у pipeline).
HISTORY_SYNC_LIMIT: int = 100
HISTORY_SYNC_INTERVAL_SEC: float = 60.0
HISTORY_SYNC_MIN_AGE_SEC: float = 120.0

#: Кнопка-отказ от Premium Leo («Пока без Premium»): после ❤️/👎 бот иногда
#: присылает премиум-реклму с кнопками [⭐️Активировать, Пока без Premium].
#: Без нажатия отказной кнопки лента встаёт (новые анкеты/капчи не идут).
#: Нажимаем ТОЛЬКО кнопку с этим фрагментом («Активировать» — никогда).
PREMIUM_DECLINE_FRAGMENT: str = "без premium"

#: Проверка Leo «<имя>, ты живешь в Питере?» с кнопками [Да, Нет] (прод
#: 2026-10-27/29: лента вставала до ручного ответа). По просьбе владельца
#: всегда отвечаем «Нет». Узнаём по фрагменту текста + кнопке «Нет».
CITY_CHECK_FRAGMENTS: tuple[str, ...] = ("живешь в", "живёшь в")
CITY_CHECK_ANSWER: str = "нет"

#: Карточка взаимного лайка Leo: «Ты понравился 3 девушке, показать её?» с
#: кнопками [«1. Показать.», «2. Не хочу больше никого смотреть.»]. Бот НЕ
#: жмёт ни одну кнопку: такая карточка ОСТАНАВЛИВАЕТ поиск (режим → OBSERVE,
#: см. ``_stop_on_match_card_if_needed``) — владелец разбирает лайки сам.
MATCH_CARD_FRAGMENT: str = "понравил"
MATCH_SHOW_FRAGMENT: str = "показать"
MATCH_SHOW_SKIP_FRAGMENT: str = "не хочу"
#: На реальных карточках Leo клавиатура нумерованная: [«1 👍», «2 💤»],
#: т.е. слова «показать» в тексте кнопки НЕТ (он только в тексте карточки).
#: Поэтому «показать» ловим ещё и по 👍; отказной «2 💤» отсекается по MATCH_SHOW_SKIP_FRAGMENT.
MATCH_SHOW_ALT_FRAGMENT: str = "👍"
MATCH_SHOW_SKIP_ALT_FRAGMENT: str = "💤"

#: Stage 8.5: тексты, которые НЕ считаются ручным «сообщением при лайке»
#: (кнопки Leo/эмодзи-реакции/навигация). Они не попадают в sent_messages,
#: чтобы навигация и реакции не засоряли аналитику текстов.
MANUAL_MESSAGE_EXCLUDE: frozenset[str] = frozenset(
    {
        "❤️",
        "👎",
        "💤",
        "💌",
        "💌 📹 🎤",
        "🌸",
        "🚀 Смотреть анкеты",
        "Смотреть анкеты",
        "Возможно позже",
        "Продолжить смотреть анкеты",
        "Продолжить",
        "Начать",
        "← Назад",
        "Вернуться назад",
        "👌 Готово",
        "Готово",
        "Активировать Premium",
        "Пока без Premium",
        "Показать девушку",
        "Да",
        "Нет",
    }
)

#: Stage 8.5: фрагменты навигационных кнопок Leo. Точного совпадения не
#: хватает: кнопки главного меню приходят с номерами («1 🚀»,
#: «1 👍», «Главное меню»). Ловим по подстроке + шаблону «номер+не-лента».
MANUAL_MESSAGE_EXCLUDE_FRAGMENTS: tuple[str, ...] = (
    "смотреть анкеты",
    "главное меню",
    "возможно позже",
    "продолжить",
    "вернуться назад",
    "активировать premium",
    "пока без premium",
    "без premium",
    "показать девушку",
)

#: Кнопка меню с префиксом-номером: «1 🚀», «2 👍» и т.п.
#: (после цифры — НЕ буква и НЕ пробел; «2 часа назад…» не отсекается).
MANUAL_MESSAGE_NUMBERED_RE: re.Pattern[str] = re.compile(r"^\s*\d+\s*[^\wа-яё\s-]", re.IGNORECASE)


def is_manual_message_excluded(text: str) -> bool:
    """Кнопка/навигация Leo (не ручное сообщение при лайке).

    Точное совпадение не срабатывает на нумерованные кнопки меню
    («1 🚀», «Главное меню», «1 👍») — используем подстроки и шаблон
    «цифра + не-буква» (Анкета с цифры не начинается).
    """
    if text in MANUAL_MESSAGE_EXCLUDE:
        return True
    low = text.lower()
    if any(f in low for f in MANUAL_MESSAGE_EXCLUDE_FRAGMENTS):
        return True
    if MANUAL_MESSAGE_NUMBERED_RE.match(text):
        return True
    return False

#: На любые "проверки"/капчи Leo (сделка, подписка, подтверждение и т.п.)
#: авто-аккаунт всегда нажимает ПОСЛЕДНЮЮ reply-кнопку — это сбрасывает
#: диалог и продолжает ленту (в конкретном случае «Возможно позже»).
#: Срабатывает ТОЛЬКО на явные капчи/сделки: текст сообщения должен содержать
#: один из маркеров CAPTCHA_MARKERS (иначе легко зациклиться в меню/Premium).
#: Кнопок при этом должно быть >= CAPTCHA_MIN_BUTTONS.
CAPTCHA_MIN_BUTTONS: int = 2

#: Маркеры текста, по которым сообщение считается капчей/сделкой/проверкой
#: (а не меню/Premium-промо). Любой из них (без учёта регистра) включает
#: авто-ответ кнопкой. «расположени/геолокаци/координат» ловят гео-капчу
#: Leo («Пришли свое расположение…»), где обычной кнопкой отказываются от
#: передачи координат и продолжают ленту.
CAPTCHA_MARKERS: tuple[str, ...] = (
    "сделк",
    "подписываешься",
    "подпишись",
    "подтверд",
    "верификац",
    "капч",
    "проверк",
    "ты подписываешься",
    "@leoday",
    "расположени",
    "геолокаци",
    "координат",
)


def captcha_signature(text: str) -> str:
    """Нормализует текст капчи в сигнатуру для сопоставления (Stage 7.6).

    Нижний регистр + схлопывание всех пробельных символов: одинаковое
    сообщение с разными переносами строк/отступами даёт одну сигнатуру.
    Используется как ключ в captcha_memory (выученные ответы владельца).
    """
    return " ".join((text or "").lower().split())


def _detect_media_type(msg: object) -> str:
    """Определяет тип media через MessageMedia объект."""
    if not hasattr(msg, "media") or msg.media is None:
        return ""

    media = msg.media
    media_type = type(media)

    if media_type in _MEDIA_TYPE_MAP:
        return _MEDIA_TYPE_MAP[media_type]

    if media_type == MessageMediaDocument and hasattr(media, "document"):
        doc = media.document
        if hasattr(doc, "mime_type"):
            mime = doc.mime_type
            if "video" in mime:
                return "video"
            if "gif" in mime:
                return "gif"
            if "sticker" in getattr(doc, "attributes", [None])[0] if doc.attributes else False:
                return "sticker"
            if "voice" in mime or "audio" in mime:
                return "voice"
        return "document"

    return "unknown"


class DvinchikCollector:
    """Перехватывает входящие сообщения и сохраняет в БД."""

    def __init__(
        self,
        client: TelegramClient | list[TelegramClient],
        db: Database,
        config: AppConfig,
        profile_service: ProfileService | None = None,
        filter_service: FilterService | None = None,
        decision_service: object | None = None,
        stats: object | None = None,
        worker: DvinchikRawWorker | None = None,
        config_path: Path = Path("config/config.yaml"),
        manual_review: object | None = None,
        client_sessions: list[str] | None = None,
        photo_vice: object | None = None,
        photo_shadow: object | None = None,
    ) -> None:
        # Несколько аккаунтов: один pipeline (dedup/worker/БД) общий, хендлеры
        # регистрируются на каждом клиенте. Один и тот же message из одного
        # чата, увиденный разными аккаунтами, дедуплицируется (in-memory
        # Dedup + UNIQUE(chat_id, telegram_message_id) в БД) — обрабатывается
        # ровно один раз.
        self._clients: list[TelegramClient] = (
            [client] if not isinstance(client, list) else list(client)
        )
        self._client = self._clients[0]
        # Имя сессии каждого клиента (параллельно self._clients). main.py
        # передаёт его явно: в clients попадают ТОЛЬКО авторизованные аккаунты,
        # и если, например, первый не вошёл, позиция клиента перестаёт
        # совпадать с позицией в telegram.accounts — раньше второй аккаунт
        # получал имя первого (сообщения и действия уходили не тому аккаунту).
        self._config = config
        self._client_sessions: list[str] = self._resolve_client_sessions(
            client_sessions
        )
        self._db = db
        self._dedup: Dedup = Dedup()
        self._parser = DvinchikParser(config.filters)
        # Стикер-приветствие девушке после взаимного лайка.
        self._match_greeter = MatchGreeter(
            config.auto_actions.match_greeting, db
        )
        self._dvinchik_chat_id = config.dvinchik.chat_id
        self._allowed_chat_ids = set(
            config.sources.allowed_chat_ids
            or ([config.dvinchik.chat_id] if config.dvinchik.chat_id else [])
        )
        # Парсинг строго на аккаунт: у аккаунтов свой чат с Leo в ОДНОМ chat_id
        # (свои диапазоны message id), поэтому контекст анкеты/дедуп/лок чата
        # ключуются парой (account_session, chat_id) — иначе MEDIA_ONLY одного
        # аккаунта привязывался к карточке другого.
        self._pending_profiles: dict[tuple[str, int], int] = {}
        # Режим до остановки на карточке взаимного лайка («Продолжить»).
        self._mode_before_match_stop: Mode | None = None
        self._stats = stats
        # C: per-chat блокировка для гарантии порядка PROFILE → MEDIA_ONLY
        # при конкурентных handlers (без глобальной блокировки).
        self._chat_locks: dict[tuple[str, int], asyncio.Lock] = {}
        # D: raw_id, уже поставленные в очередь в этой сессии (live handler).
        # Исключает двойной enqueue между live worker и startup recovery (W3).
        # Set очищается после завершения recovery (steady-state live-сообщения
        # имеют id > cutoff и не могут быть продублированы recovery), поэтому
        # он не растёт бесконечно (MEDIUM-1). Флаг ``_recovery_armed`` показывает,
        # что startup-recovery ещё активна (или будет запущена) — только тогда
        # live handler добавляет raw_id в set.
        self._enqueued_raw_ids: set[int] = set()
        self._enqueued_raw_ids_cap: int = 5000
        self._recovery_armed: bool = False
        self._profile_service = profile_service
        self._filter_service = filter_service
        self._decision_service = decision_service
        # Stage 8: запись ручных решений владельца по REVIEW-анкетам.
        self._manual_review = manual_review
        # Проверка фото на сигарету/алкоголь (services/photo_vice.py) —
        # только для анкет, которые бот собирается лайкнуть.
        self._photo_vice = photo_vice
        # Теневая модель вкуса по фото (services/photo_taste.py): вердикт
        # каждой карточки пишется в photo_scores в фоне, действия не меняет.
        self._photo_shadow = photo_shadow
        # Фоновые расчёты по карточкам: (account, chat, msg_id) → Task с
        # результатом. Проверка сигарет/алкоголя для будущего лайка берёт
        # готовый результат отсюда, а не гоняет CLIP второй раз.
        self._photo_shadow_pending: dict[tuple[str, int, int], asyncio.Task] = {}
        # Готовые результаты последних карточек: расчёт может закончиться
        # РАНЬШЕ, чем коллектор дойдёт до решения (задача уже снята из pending).
        self._photo_shadow_results: dict[tuple[str, int, int], dict] = {}
        # Stage 7: авто-действия (SEMI_AUTO). Клиент ищется по сессии
        # (config.auto_actions.account_session) среди accounts/clients (они
        # параллельны: main.py строит clients в том же порядке, что accounts).
        self._auto_engine = self._build_auto_engine()
        self._config_path = config_path
        # Worker потребляет RawQueue и выполняет pipeline вне Telegram-хендлера.
        # Если worker не задан (тесты/отладка) — обработка идёт синхронно в
        # хендлере (fallback). В проде всегда привязан worker.
        self._worker = worker

    @property
    def _mode_label(self) -> str:
        """Дружелюбная строка текущего режима для вывода решений."""
        eng = self._auto_engine
        mode = eng.mode.value if eng is not None else self._config.project.mode.value
        if eng is None or not eng.enabled:
            return f"{mode} (действий нет)"
        return f"{mode} (авто-действия ❤️/👎 активны)"

    @property
    def mode(self) -> Mode:
        """Текущий режим работы (источник истины — AutoActionEngine)."""
        if self._auto_engine is not None:
            return self._auto_engine.mode
        return self._config.project.mode

    def set_mode(self, mode: Mode) -> None:
        """Меняет режим на лету (ControlBot) и сохраняет в config.yaml.

        Обновляет движок авто-действий и live-конфиг, чтобы переключение
        пережило restart приложения.
        """
        if self._auto_engine is not None:
            self._auto_engine.mode = mode
        self._config.project.mode = mode
        try:
            self._config.persist_mode(self._config_path, mode)
            logger.info(f"Режим изменён на {mode.value} (сохранён в config.yaml)")
        except Exception as e:
            logger.error(f"Не удалось сохранить режим в config.yaml: {e}")

    def auto_engine(self) -> AutoActionEngine | None:
        """Доступ к движку авто-действий для ControlBot."""
        return self._auto_engine

    def _build_auto_engine(self) -> AutoActionEngine:
        """Строит AutoActionEngine для сессии из config.auto_actions.

        Клиент — по имени сессии ``auto_actions.account_session``; для
        авто-уведомлений — «другой» аккаунт (на него пересылается карточка
        анкеты с причиной, Бармалей↔Меланхолик). См. ``_clients_for_session``.
        """
        mode = self._config.project.mode
        auto_cfg = self._config.auto_actions
        target_session = auto_cfg.account_session

        client, notify_client = (
            self._clients_for_session(target_session)
            if target_session else (None, None)
        )

        if auto_cfg.enabled and client is None:
            logger.warning(
                f"AutoAction: account_session={target_session!r} не найден среди "
                f"accounts; авто-действия отключены"
            )

        return AutoActionEngine(
            client=client,
            config=auto_cfg,
            mode=mode,
            chat_id=self._dvinchik_chat_id,
            notify_client=notify_client,
            db=self._db,
            account_session=target_session,
        )

    def switch_auto_account(self, session: str) -> bool:
        """Переключает аккаунт-исполнитель авто-действий на лету (Веб-панель).

        Ищет клиент по имени сессии (``_clients_for_session``), переуказывает
        живому движку новый client и сохраняет выбор в config.yaml (переживает
        restart).
        """
        client, notify_client = self._clients_for_session(session)

        if client is None:
            logger.warning(
                f"AutoAction: account_session={session!r} не найден среди accounts"
            )
            return False

        if self._auto_engine is None:
            self._auto_engine = self._build_auto_engine()
            if self._auto_engine is not None:
                self._auto_engine.swap_client(
                    client, notify_client, account_session=session,
                )
        else:
            self._auto_engine.swap_client(
                client, notify_client, account_session=session,
            )

        self._config.auto_actions.account_session = session
        try:
            self._config.persist_account_session(self._config_path, session)
            logger.info(f"Авто-аккаунт сменён на {session!r} (сохранён в config.yaml)")
        except Exception as e:
            logger.error(f"Не удалось сохранить авто-аккаунт в config.yaml: {e}")
        return True

    def _resolve_client_sessions(
        self, client_sessions: list[str] | None,
    ) -> list[str]:
        """Имена сессий, параллельные ``self._clients``.

        Явный список из main.py (имена, с которыми реально создавались
        клиенты) — главный источник. Без него (тесты/отладка) — прежняя
        эвристика по позиции в ``telegram.accounts``: ``acc.session`` или
        дефолт ``dvai``/``dvai_{idx}``, как в main.py.
        """
        if client_sessions is not None and len(client_sessions) == len(
            self._clients
        ):
            return list(client_sessions)
        accounts = self._config.telegram.accounts or []
        sessions: list[str] = []
        for i in range(len(self._clients)):
            if i < len(accounts) and accounts[i].session:
                sessions.append(accounts[i].session)
            else:
                sessions.append("dvai" if len(accounts) == 1 else f"dvai_{i}")
        return sessions

    def _clients_for_session(
        self, session: str,
    ) -> tuple[TelegramClient | None, TelegramClient | None]:
        """(клиент сессии ``session``, первый ДРУГОЙ клиент — для уведомлений)."""
        client = None
        notify_client = None
        for c, name in zip(self._clients, self._client_sessions):
            if name == session and client is None:
                client = c
            elif notify_client is None:
                notify_client = c
        return client, notify_client

    def _session_for_client(self, client: object) -> str:
        """Возвращает session-имя аккаунта, которому принадлежит client.

        По нему ключуются ``raw_messages.account_session``, дедуп и контекст
        анкеты, а веб выбирает клиент для скачивания фото. '' — если client
        не найден (например, backlog-рекавери без живого msg).
        """
        if client is None:
            return ''
        for c, name in zip(self._clients, self._client_sessions):
            if c is client:
                return name
        return ''

    def attach_worker(self, worker: DvinchikRawWorker) -> None:
        """Привязывает worker; хендлер начинает только ставить в очередь."""
        self._worker = worker

    def start(self) -> None:
        """Запускает фоновый worker (если привязан)."""
        if self._worker is not None:
            self._worker.start()
            # Recovery (W3) запускается сразу после start в main.py; считаем
            # защиту от double-enqueue вооружённой, пока recovery не завершится.
            self._recovery_armed = True

    async def stop(self) -> None:
        """Плавно останавливает worker (если привязан)."""
        if self._worker is not None:
            await self._worker.stop()

    async def start_auto_stream(self) -> bool:
        """Обрабатывает активную анкету или продолжает ленту Leo.

        ✨🔍 НЕ отправляется: команда не приводит новые анкеты, а показанная
        (обычный текст "Имя, возраст, город") уже ждёт только лайк/дизлайк.
        1) Есть активная анкета → обрабатываем (лайк/дизлайк).
        2) Нет активной анкеты, но Leo показал «Смотреть анкеты» → нажимаем
           кнопку, чтобы получить следующую партию.
        3) Ничего из этого → False.
        """
        if not self._auto_engine.enabled:
            return False
        try:
            await self._auto_engine.ensure_peer()
            # Одна выборка истории на все проверки кнопок (см. _recent_history).
            history = await self._recent_history()
            # Карточка «Ты понравился…» — первым делом: поиск останавливается,
            # пока владелец не разберёт лайки сам (никаких кнопок не жмём).
            if await self._stop_on_match_card_if_needed(history):
                return False
            if await self._process_active_profile_if_any():
                await self._clear_stream_error()
                return True
            if await self._continue_stream_if_needed(history):
                await self._clear_stream_error()
                return True
            if await self._handle_captcha():
                await self._clear_stream_error()
                return True
            # Ни активной анкеты, ни кнопки продолжения, ни капчи. Последняя
            # надежда — «зависший» композер Leo: после рестарта цепочка
            # «Берем)» теряется, а Leo всё ещё ждёт текст, и лента стоит.
            last_text = await self._last_incoming_text()
            if await self._auto_engine.recover_open_composer(last_text):
                await self._clear_stream_error()
                return True
            if self._auto_engine.is_composer_prompt(last_text):
                # Leo ждёт текст, а закрыть композер не можем (like_message
                # выключен) — это реальный сбой, владельца надо предупредить.
                await self._report_stream_stuck(
                    f"Leo ждёт текст в композере: «{last_text[:80]}»"
                )
                return False
            # Ничего продолжать не нужно (нормальное состояние, не сбой).
            return False
        except Exception as e:
            logger.error(f"AutoAction: ошибка обработки активной анкеты: {e}")
            await self._report_stream_stuck(f"ошибка автопотока: {e}")
            return False

    async def sync_chat_history(
        self,
        limit: int = HISTORY_SYNC_LIMIT,
        min_age_sec: float = HISTORY_SYNC_MIN_AGE_SEC,
    ) -> int:
        """Досохраняет в RAW сообщения чата Leo, которые прошли мимо событий.

        Лента на сайте — зеркало Telegram-чата, но строится только из того, что
        Telethon доставил событием. При обрывах прокси/рестартах события теряются
        (входящие карточки, наши ❤️/👎, ручные ответы с телефона) — в ленте
        появлялись дыры и «анархия». Здесь для КАЖДОГО аккаунта читаем последние
        ``limit`` сообщений чата и INSERT OR IGNORE'им недостающие (ключ
        ``(account_session, chat_id, tm_id, is_outgoing)`` — уже сохранённые
        не трогаются).

        Досохранённое помечается обработанным: это прошлое, реагировать на него
        поздно (карточка уже ушла). Сообщения моложе ``min_age_sec`` не трогаем:
        их вот-вот сохранит живой хендлер, а наш INSERT отнял бы у него
        сообщение (дубликат → pipeline пропущен). Возвращает число новых строк.
        """
        added = 0
        now = datetime.now(timezone.utc)
        for client in self._clients:
            account_session = self._session_for_client(client)
            # Карточка «Ты понравился…», пропущенная живым хендлером, тоже
            # должна остановить поиск — но только самая свежая и только если
            # после неё мы ещё ничего не отправляли (иначе уже разобрана).
            watch_match = client is self._auto_engine.client
            try:
                async for msg in client.iter_messages(
                    self._dvinchik_chat_id, limit=limit
                ):
                    if getattr(msg, "out", False):
                        watch_match = False
                    msg_dt = getattr(msg, "date", None)
                    if (
                        isinstance(msg_dt, datetime)
                        and (now - msg_dt).total_seconds() < min_age_sec
                    ):
                        continue
                    text = getattr(msg, "text", None) or ""
                    media_type = _detect_media_type(msg)
                    if not text.strip() and not media_type:
                        continue
                    outgoing = bool(getattr(msg, "out", False))
                    reply_to = getattr(msg, "reply_to_msg_id", None)
                    raw_id = await self._db.save_raw_message(
                        telegram_message_id=msg.id,
                        chat_id=self._dvinchik_chat_id,
                        sender_id=getattr(msg, "sender_id", 0) or 0,
                        sender_username="",
                        sender_name="",
                        message_date=(
                            msg_dt.isoformat()
                            if isinstance(msg_dt, datetime)
                            else now.isoformat()
                        ),
                        text=text,
                        raw_entities=(
                            "[]" if outgoing else self._serialize_entities(msg)
                        ),
                        reply_markup=(
                            "[]" if outgoing
                            else self._serialize_reply_markup(msg)
                        ),
                        media_type=media_type,
                        reply_to_message_id=(
                            reply_to if isinstance(reply_to, int) else None
                        ),
                        received_at=now.isoformat(),
                        is_outgoing=outgoing,
                        account_session=account_session,
                    )
                    is_card = watch_match and self._is_match_card(msg)
                    if is_card:
                        watch_match = False
                    if raw_id is None:
                        continue
                    await self._db.mark_raw_processed(raw_id)
                    added += 1
                    if is_card:
                        await self._stop_on_match_card(msg)
            except Exception as e:
                logger.error(
                    f"Сверка истории чата ({account_session or '?'}) не удалась: {e}"
                )
        if added:
            logger.info(f"Сверка истории чата: досохранено {added} сообщений")
        return added

    async def run_history_sync(
        self, interval_sec: float = HISTORY_SYNC_INTERVAL_SEC,
    ) -> None:
        """Фоновая сверка ленты с Telegram (раз в ``interval_sec``)."""
        while True:
            await asyncio.sleep(interval_sec)
            try:
                await self.sync_chat_history()
            except Exception as e:
                logger.error(f"Сверка истории чата: {e}")
            await self.retry_match_greetings()

    async def _stream_snapshot(
        self, limit: int = STREAM_ERROR_SNAPSHOT_LIMIT
    ) -> list[str]:
        """Снимок последних сообщений чата Leo — для диагностики сбоя потока.

        Идём от свежих к старым, затем разворачиваем: в конце списка — самое
        свежее сообщение (его и показываем как «последнее в чате»).
        """
        client = self._auto_engine.client
        if client is None:
            return []
        lines: list[str] = []
        try:
            async for msg in client.iter_messages(
                self._dvinchik_chat_id, limit=limit
            ):
                text = (msg.text or "").strip()
                if not text:
                    continue
                who = "мы" if getattr(msg, "out", False) else "Leo"
                lines.append(f"{who}: {text[:120]}")
        except Exception as e:
            logger.error(f"AutoAction: не удалось снять снимок чата: {e}")
            return []
        lines.reverse()
        return lines

    async def _last_incoming_text(self) -> str:
        """Текст последнего сообщения в чате Leo ('' — нет/ошибка).

        Используется, чтобы понять, не ждёт ли Leo текст в «зависшем»
        композере (после рестарта цепочка «Берем)» теряется).
        """
        client = self._auto_engine.client
        if client is None:
            return ""
        try:
            async for msg in client.iter_messages(self._dvinchik_chat_id, limit=1):
                return (msg.text or "").strip()
        except Exception as e:
            logger.error(
                f"AutoAction: не удалось прочитать последнее сообщение: {e}"
            )
        return ""

    async def _report_stream_stuck(self, reason: str = "") -> None:
        """Сообщает владельцу и на сайт, что бот не может продолжить ленту.

        Пишет диагностический снимок в ``app_state`` (его показывает баннер на
        сайте) и один раз в ``STREAM_ERROR_NOTIFY_COOLDOWN_SEC`` шлёт
        уведомление владельцу в Telegram. Если в чате нет ни одного внятного
        сообщения — ничего не сообщаем (ложное срабатывание на пустом старте).
        """
        if self._db is None:
            return
        snapshot = await self._stream_snapshot()
        if not snapshot and not reason:
            return
        if not reason and snapshot:
            reason = snapshot[-1]
        now = datetime.now(timezone.utc)
        payload = {
            "message": "Бот не может продолжить ленту анкет Дайвинчика",
            "reason": reason,
            "snapshot": snapshot,
            "at": now.isoformat(),
        }
        notify_needed = True
        try:
            prev_raw = await self._db.get_state(STREAM_ERROR_STATE_KEY)
            if prev_raw:
                try:
                    prev_at = datetime.fromisoformat(
                        str(json.loads(prev_raw).get("at") or "")
                    )
                    age = (now - prev_at).total_seconds()
                    if 0 <= age < STREAM_ERROR_NOTIFY_COOLDOWN_SEC:
                        notify_needed = False
                except Exception:
                    pass
            await self._db.set_state(
                STREAM_ERROR_STATE_KEY,
                json.dumps(payload, ensure_ascii=False),
            )
        except Exception as e:
            logger.error(f"AutoAction: не удалось сохранить сбой потока: {e}")

        logger.warning(
            f"AutoAction: лента встала — {reason or 'причина неизвестна'}"
        )
        if notify_needed and self._auto_engine is not None:
            await self._auto_engine.notify_owner(
                self._format_stream_stuck_text(reason, snapshot)
            )

    async def _clear_stream_error(self) -> None:
        """Сбрасывает отметку о сбое потока, когда лента снова пошла."""
        if self._db is None:
            return
        try:
            if await self._db.get_state(STREAM_ERROR_STATE_KEY):
                await self._db.delete_state(STREAM_ERROR_STATE_KEY)
        except Exception as e:
            logger.error(f"AutoAction: не удалось сбросить сбой потока: {e}")

    @staticmethod
    def _format_stream_stuck_text(reason: str, snapshot: list[str]) -> str:
        """Текст уведомления владельцу о сбое потока."""
        lines = ["⚠️ Бот не может продолжить ленту анкет (Дайвинчик)."]
        if reason:
            lines.append(f"Последнее в чате — {reason}")
        if snapshot:
            lines.append("")
            lines.append("Последние сообщения:")
            lines.extend(snapshot[-5:])
        lines.append("")
        lines.append(
            "Проверь чат с Leo — возможно, нужно нажать кнопку или ответить вручную."
        )
        return "\n".join(lines)

    async def _process_active_profile_if_any(self) -> bool:
        """Обрабатывает уже показанную активную анкету, если она есть.

        Механика Дайвинчика: анкета приходит обычным текстом
        ("Имя, возраст, город – …") и ждёт реакции простым текстом ❤️/👎
        (кнопок на анкете может не быть). Сканируем последние сообщения чата
        на авто-аккаунте и берём самую свежую анкету (PROFILE), на которую
        ещё не отправлена реакция (нет исходящего ❤️/👎 после неё). Прогоняем
        её через штатный pipeline (_process_message: parse → filter → AI →
        авто-действие). Если активной анкеты нет — возвращаем False.

        Важно: анкета считается активной, только если после неё нет СВЕЖЕГО
        содержательного входящего сообщения Leo (меню, Premium-промо, MATCH,
        «Лайк отправлен…», капча, «Нет такого варианта ответа») — такое
        сообщение означает, что фаза реакции на анкету завершена/лента ушла
        вперёд (после переключения аккаунта в истории может остаться старая
        анкета, на которую больше нельзя отвечать — отправка ❤️/👎 даст
        «Нет такого варианта ответа»). Медиа/фото без текста игнорируются
        (они относятся к текущей карточке анкеты).
        """
        client = self._auto_engine.client
        if client is None:
            return False
        try:
            latest_action_id: int | None = None
            active = None
            async for msg in client.iter_messages(self._dvinchik_chat_id, limit=15):
                text = (msg.text or "").strip()
                if getattr(msg, "out", False):
                    if text in (LIKE_TEXT, DISLIKE_TEXT):
                        if latest_action_id is None:
                            latest_action_id = msg.id
                    continue
                if not text:
                    # Медиа/стикер без текста — часть карточки анкеты,
                    # состояние чата не меняет.
                    continue
                if self._is_profile_text(msg):
                    if (
                        latest_action_id is not None
                        and msg.id < latest_action_id
                    ):
                        # на эту анкету уже отправлена реакция — не активна
                        continue
                    active = msg
                    break
                # Самое свежее входящее — НЕ анкета (меню/промо/MATCH/лайк-эк/
                # капча/отказ Leo): активной анкеты нет.
                logger.info(
                    f"AutoAction: нет активной анкеты — самое свежее сообщение "
                    f"Leo (msg={msg.id}) не анкета: {text[:60]!r}"
                )
                active = None
                break

            if active is None:
                logger.info("AutoAction: активная анкета в чате не найдена")
                return False

            text = (active.text or "").strip()
            if not text:
                return False

            logger.info(
                f"AutoAction: найдена активная анкета (msg={active.id}) — "
                "обрабатываю без повторного ✨🔍"
            )
            task = RawTask(
                chat_id=self._dvinchik_chat_id,
                message_id=active.id,
                sender_id=active.sender_id or 0,
                sender_username="",
                sender_name="",
                text=text,
                media_type="",
                entities_json="[]",
                reply_to=None,
                received_at=datetime.now(timezone.utc).isoformat(),
                msg_date=active.date.isoformat() if active.date else "",
                msg=active,
                raw_id=0,
                account_session=self._auto_engine.account_session,
            )
            await self._process_message(task)
            return True
        except Exception as e:
            logger.error(f"AutoAction: ошибка обработки активной анкеты: {e}")
            return False

    async def _recent_history(self, limit: int = 15) -> list:
        """Последние ``limit`` сообщений чата Leo на авто-аккаунте (новые первыми).

        Один запрос истории на всю проверку состояния чата. Раньше каждый
        обработчик кнопок (меню, Premium, «Смотреть анкеты», карточка лайка)
        сам дважды скачивал историю — на каждое служебное сообщение Leo
        выходило до ~10 запросов getHistory, то есть несколько в секунду
        при живой ленте: прямой путь к FloodWait.
        """
        client = self._auto_engine.client
        if client is None:
            return []
        try:
            return [
                msg async for msg in client.iter_messages(
                    self._dvinchik_chat_id, limit=limit
                )
            ]
        except Exception as e:
            # Как и раньше (каждый обработчик глотал свою ошибку): сбой
            # чтения истории не роняет автопоток — кнопки просто не жмём.
            logger.error(f"AutoAction: не удалось прочитать историю чата Leo: {e}")
            return []

    async def _continue_stream_if_needed(self, history: list) -> bool:
        """Продолжает ленту Leo одной кнопкой: «Смотреть анкеты» → отказ от
        Premium → «Нет» на «ты живешь в …?» → «1 🚀» в меню. Останавливается
        на первой нажатой — после
        нажатия ``history`` устарела, следующий шаг сделает уже новое
        сообщение Leo. Все проверки — по одной выборке истории.
        """
        for press in (
            self._press_view_button_if_needed,
            self._press_premium_decline_if_needed,
            self._answer_city_check_if_needed,
            self._press_menu_view_if_needed,
        ):
            if await press(history):
                return True
        return False

    async def _press_view_button_if_needed(
        self, history: list | None = None,
    ) -> bool:
        """Нажимает кнопку «Смотреть анкеты», если Leo её показал.

        При исчерпании ленты Leo присылает промо-сообщение с reply-кнопкой
        «🚀 Смотреть анкеты» — нажатие (отправка текста кнопки) продолжает
        поток. Работает только если активной анкеты нет (иначе кнопка не
        относится к продолжению). Сканируем последние сообщения чата на
        авто-аккаунте; если найдено сообщение с такой кнопкой и после него
        ещё не отправлен текст кнопки (идемпотентность) — отправляем.
        """
        client = self._auto_engine.client
        if client is None or not self._auto_engine.enabled:
            return False
        try:
            if history is None:
                history = await self._recent_history()
            card_msg = None
            button_text = ""
            for msg in history:
                texts = self._extract_button_texts(msg)
                hit = next(
                    (t for t in texts if VIEW_BUTTON_FRAGMENT in t), None
                )
                if hit:
                    card_msg = msg
                    button_text = hit
                    break

            if card_msg is None or not button_text:
                return False

            # Идемпотентность: если после карточки уже отправлен текст кнопки,
            # не нажимаем повторно (тот же поток уже продолжен).
            sent_at = card_msg.id
            already_sent = False
            for msg in history:
                if msg.id <= sent_at:
                    break
                if (
                    getattr(msg, "out", False)
                    and (msg.text or "").strip() == button_text
                ):
                    already_sent = True
                    break

            if already_sent:
                logger.info("AutoAction: кнопка «Смотреть анкеты» уже нажата")
                return False

            logger.info(
                f"AutoAction: найдена кнопка «Смотреть анкеты» (msg={card_msg.id}) — "
                "продолжаю ленту"
            )
            await self._auto_engine.send_text(button_text)
            return True
        except Exception as e:
            logger.error(f"AutoAction: ошибка нажатия кнопки «Смотреть анкеты»: {e}")
            return False

    async def _press_menu_view_if_needed(
        self, history: list | None = None,
    ) -> bool:
        """Нажимает «1 🚀» в ГЛАВНОМ МЕНЮ Leo, если чат стоит на нём.

        Когда лента исчерпана, Leo шлёт текстовое меню «1. Смотреть анкеты. …
        4. Активируй Premium» с reply-клавиатурой [«1 🚀», «2», «3», «4»].
        Кнопки-промо «🚀 Смотреть анкеты» здесь нет, поэтому
        ``_press_view_button_if_needed`` молча выходит, и без ручного нажатия
        лента стоит навсегда (типично после переключения авто-аккаунта, у
        которого чат уже был в этом состоянии).

        Жмём «1 🚀» ТОЛЬКО если самая свежая карточка с кнопками — это меню:
        тогда карточка взаимного лайка / Premium-промо / капча (у них свои
        обработчики, они проверяются раньше) не будет перебита нажатием.
        Идемпотентность — как у view-кнопки: текст кнопки не отправлялся после
        этой карточки. Сравнение id корректно, потому что оба прохода идут
        через клиент авто-аккаунта (у каждого аккаунта своё пространство id).
        """
        client = self._auto_engine.client
        if client is None or not self._auto_engine.enabled:
            return False
        try:
            if history is None:
                history = await self._recent_history()
            card_msg = None
            button_text = ""
            for msg in history:
                if getattr(msg, "out", False) or not (msg.text or "").strip():
                    continue
                texts = self._extract_button_texts(msg)
                if not texts:
                    # фото/текст без кнопок (карточка анкеты, сервис) —
                    # состояние чата не меняет, смотрим дальше.
                    continue
                # Самая свежая карточка с кнопками определяет состояние чата.
                card_msg = msg
                hit = next((t for t in texts if MENU_VIEW_FRAGMENT in t), None)
                button_text = hit or ""
                break

            if card_msg is None or not button_text:
                return False

            sent_at = card_msg.id
            already_sent = False
            for msg in history:
                if msg.id <= sent_at:
                    break
                if (
                    getattr(msg, "out", False)
                    and (msg.text or "").strip() == button_text
                ):
                    already_sent = True
                    break

            if already_sent:
                logger.info("AutoAction: кнопка меню «1 🚀» уже нажата")
                return False

            logger.info(
                f"AutoAction: чат стоит на главном меню Leo (msg={card_msg.id}) — "
                "жму «1 🚀», продолжаю ленту"
            )
            await self._auto_engine.send_text(button_text)
            return True
        except Exception as e:
            logger.error(f"AutoAction: ошибка нажатия кнопки меню «1 🚀»: {e}")
            return False

    async def _press_premium_decline_if_needed(
        self, history: list | None = None,
    ) -> bool:
        """Нажимает кнопку-отказ у Premium-промо Leo («Пока без Premium»).

        После ❤️/👎 Leo иногда присылает премиум-реклму («Борюсь с
        эгоизмом…», кнопки [⭐️Активировать, Пока без Premium]) — без
        нажатия отказной кнопки лента встаёт. Ищем самую свежую карточку
        с кнопкой, содержащей PREMIUM_DECLINE_FRAGMENT, и нажимаем её
        (идемпотентно: только если текст кнопки ещё не отправлялся после
        карточки). Кнопку «Активировать» никогда не нажимаем.
        """
        client = self._auto_engine.client
        if client is None or not self._auto_engine.enabled:
            return False
        try:
            if history is None:
                history = await self._recent_history()
            card_msg = None
            button_text = ""
            for msg in history:
                texts = self._extract_button_texts(msg)
                hit = next(
                    (t for t in texts if PREMIUM_DECLINE_FRAGMENT in t.lower()), None
                )
                if hit:
                    card_msg = msg
                    button_text = hit
                    break

            if card_msg is None or not button_text:
                return False

            # Идемпотентность: если после карточки уже отправлен текст кнопки,
            # не нажимаем повторно (тот же поток уже продолжен).
            sent_at = card_msg.id
            already_sent = False
            for msg in history:
                if msg.id <= sent_at:
                    break
                if (
                    getattr(msg, "out", False)
                    and (msg.text or "").strip() == button_text
                ):
                    already_sent = True
                    break

            if already_sent:
                logger.info("AutoAction: кнопка «Пока без Premium» уже нажата")
                return False

            logger.info(
                f"AutoAction: Premium-промо (msg={card_msg.id}) — нажимаю "
                f"«{button_text}», продолжаю ленту"
            )
            await self._auto_engine.send_text(button_text)
            return True
        except Exception as e:
            logger.error(f"AutoAction: ошибка нажатия кнопки Premium-промо: {e}")
            return False

    async def _answer_city_check_if_needed(
        self, history: list | None = None,
    ) -> bool:
        """Отвечает «Нет» на проверку Leo «<имя>, ты живешь в Питере?».

        Только если это САМАЯ СВЕЖАЯ входящая карточка с кнопками (активная
        клавиатура) и после неё нет нашего исходящего (уже ответили — сами
        или владелец вручную).
        """
        client = self._auto_engine.client
        if client is None or not self._auto_engine.enabled:
            return False
        try:
            if history is None:
                history = await self._recent_history()
            for msg in history:  # новые → старые
                if getattr(msg, "out", False):
                    return False  # после последней клавиатуры уже ответили
                texts = self._extract_button_texts(msg)
                if not texts:
                    continue
                text = (getattr(msg, "text", "") or "").lower()
                if not any(f in text for f in CITY_CHECK_FRAGMENTS):
                    return False  # активная клавиатура — не эта проверка
                answer = next(
                    (t for t in texts if t.strip().lower() == CITY_CHECK_ANSWER),
                    None,
                )
                if answer is None:
                    return False
                logger.info(
                    f"AutoAction: проверка «ты живешь в …?» (msg={msg.id}) — "
                    f"отвечаю «{answer}», продолжаю ленту"
                )
                await self._auto_engine.send_text(answer)
                return True
            return False
        except Exception as e:
            logger.error(f"AutoAction: ошибка ответа на проверку города: {e}")
            return False

    @staticmethod
    def _is_routine_leo_text(text: str) -> bool:
        """Штатное служебное сообщение Leo, не требующее внимания в логе."""
        stripped = (text or "").strip()
        return (
            stripped in ROUTINE_LEO_TEXTS
            or AutoActionEngine.is_composer_prompt(stripped)
        )

    def _is_match_card(self, msg: object) -> bool:
        """Карточка «Ты понравился N девушке, показать её?» (N — любое).

        Текст содержит MATCH_CARD_FRAGMENT и либо «показать» в самом тексте,
        либо кнопку показа (обе клавиатуры Leo: «1. Показать.» / «1 👍»).
        """
        low = (getattr(msg, "text", "") or "").lower()
        if MATCH_CARD_FRAGMENT not in low:
            return False
        if MATCH_SHOW_FRAGMENT in low:
            return True
        return any(
            self._is_match_show_button(t) for t in self._extract_button_texts(msg)
        )

    async def _stop_on_match_card_if_needed(
        self, history: list | None = None,
    ) -> bool:
        """Останавливает поиск на карточке «Ты понравился N девушке…».

        Владелец хочет сам решать, смотреть ли тех, кто его лайкнул, поэтому
        бот НЕ жмёт ни «Показать», ни «Не хочу…»: переводит режим в OBSERVE
        (сохраняется в config.yaml — переживает restart), снимает незакрытые
        цепочки «Берем)», ставит баннер на сайте и один раз на карточку
        уведомляет владельца. Продолжить — ``/mode on`` или режим на сайте.

        Карточка «актуальна», только если после неё мы ещё ничего не
        отправляли (иначе владелец уже ответил Leo). True — поиск остановлен.
        """
        client = self._auto_engine.client
        if client is None or not self._auto_engine.enabled:
            return False
        try:
            if history is None:
                history = await self._recent_history()
            card_msg = None
            for msg in history:
                if getattr(msg, "out", False):
                    # Наш ответ свежее любой карточки ниже — она уже разобрана.
                    return False
                if self._is_match_card(msg):
                    card_msg = msg
                    break
            if card_msg is None:
                return False

            return await self._stop_on_match_card(card_msg)
        except Exception as e:
            logger.error(f"AutoAction: ошибка остановки на карточке лайка: {e}")
            return False

    async def _greet_match(
        self, task: RawTask, msg: object | None, match: ParsedMatch,
    ) -> None:
        """Взаимный лайк → стикер девушке (вместо текста Leo).

        Всем матчам: и после лайка бота, и после ручного лайка владельца
        (в т.ч. в OBSERVE). Пишем с того аккаунта, на котором случился матч
        (у неё матч именно с ним). Режимы — ``match_greeting.modes``.
        """
        client = getattr(msg, "client", None) if msg is not None else None
        if client is None or task.chat_id != self._dvinchik_chat_id:
            return
        session = task.account_session or self._session_for_client(client)
        try:
            status = await self._match_greeter.greet(
                client, session, match, self.mode.value,
            )
        except Exception as e:
            logger.error(f"MatchGreeting error: {e}")
            return
        await self._notify_greeting(session, match, status)

    async def _notify_greeting(
        self, session: str, match: ParsedMatch, status: str, *,
        delayed: bool = False,
    ) -> None:
        """Уведомление владельцу: стикер ушёл / аккаунт в спам-блоке."""
        who = (
            f"@{match.telegram_username}" if match.telegram_username
            else f"id {match.telegram_user_id}"
        )
        if status == "SENT":
            tail = " (из очереди после спам-блока)" if delayed else ""
            await self._auto_engine.notify_owner(
                f"🔥 Взаимный лайк: {match.name} ({who}) — отправил ей "
                f"стикер-привет с {session or 'аккаунта'}{tail}"
            )
        elif status == "SPAM_BLOCK":
            until = await self._match_greeter.paused_until(session)
            when = (
                until.astimezone().strftime("%d.%m %H:%M") if until else "—"
            )
            await self._auto_engine.notify_owner(
                f"⛔ {session or 'Аккаунт'} в спам-блоке Telegram (нельзя "
                f"писать незнакомым): приветствия на паузе до {when}. "
                f"{match.name} ({who}) — в очереди, допишу после паузы. "
                "Проверь @SpamBot."
            )

    async def retry_match_greetings(self) -> None:
        """Дописывает девушкам, отложенным из-за спам-блока (после паузы)."""
        for client, session in zip(self._clients, self._client_sessions):
            try:
                results = await self._match_greeter.retry_queued(
                    client, session, self.mode.value,
                )
            except Exception as e:
                logger.error(f"MatchGreeting: дозапись очереди {session}: {e}")
                continue
            for match, status in results:
                await self._notify_greeting(
                    session, match, status, delayed=True,
                )

    def _halt_on_match_card(self, card_msg: object) -> bool:
        """Синхронная часть остановки: режим OBSERVE + снятие цепочек.

        Без сети и БД (кроме записи режима в config.yaml) — поэтому её можно
        звать прямо из Telegram-хендлера под локом чата, ДО постановки
        сообщения в очередь pipeline. Карточка помечается активной
        (``note_card``): реакции на анкеты, ещё стоящие в очереди, становятся
        «устаревшими» и не уходят — иначе queued 👎 уходил ПОСЛЕ карточки,
        и проверка «после карточки уже есть наш ответ» считала её разобранной
        (поиск не останавливался). True — поиск остановлен сейчас.
        """
        if not self._auto_engine.enabled:
            return False
        card_text = (getattr(card_msg, "text", "") or "").strip()
        logger.warning(
            f"AutoAction: карточка взаимного лайка (msg={card_msg.id}) — "
            f"«{card_text[:80]}»: останавливаю поиск (режим OBSERVE)"
        )
        self._auto_engine.note_card(self._dvinchik_chat_id, card_msg.id)
        self._auto_engine.abort_like_chains(self._dvinchik_chat_id)
        self._mode_before_match_stop = self.mode
        self.set_mode(Mode.OBSERVE)
        return True

    async def _stop_on_match_card(self, card_msg: object) -> bool:
        """Полная остановка на конкретной карточке «Ты понравился…»."""
        if not self._halt_on_match_card(card_msg):
            return False
        await self._mark_match_stop(
            card_msg.id, (getattr(card_msg, "text", "") or "").strip()
        )
        return True

    async def _mark_match_stop(self, card_id: int, card_text: str) -> None:
        """Баннер на сайте + уведомление владельцу (одно на карточку)."""
        if self._db is None:
            return
        card_key = f"{self._auto_engine.account_session}:{card_id}"
        notify = True
        try:
            if await self._db.get_state(MATCH_STOP_STATE_KEY) == card_key:
                notify = False
            await self._db.set_state(MATCH_STOP_STATE_KEY, card_key)
            prev_mode = self._mode_before_match_stop
            if prev_mode is not None and prev_mode != Mode.OBSERVE:
                await self._db.set_state(MATCH_STOP_PREV_MODE_KEY, prev_mode.value)
            await self._db.set_state(
                STREAM_ERROR_STATE_KEY,
                json.dumps(
                    {
                        "kind": "match_stop",
                        "message": "Поиск остановлен: тебя лайкнули",
                        "reason": card_text,
                        "snapshot": [f"Leo: {card_text[:120]}"],
                        "at": datetime.now(timezone.utc).isoformat(),
                    },
                    ensure_ascii=False,
                ),
            )
        except Exception as e:
            logger.error(f"AutoAction: не удалось сохранить остановку поиска: {e}")
        if notify:
            await self._auto_engine.notify_owner(
                f"💘 {card_text}\n\nПоиск остановлен (режим OBSERVE) — посмотри "
                "сам в Дайвинчике. Продолжить: кнопка «Продолжить» на сайте "
                "или /mode on."
            )

    async def _match_stop_prev_mode(self) -> Mode:
        """Режим, который был до остановки на карточке лайка (иначе SEMI_AUTO)."""
        prev = self._mode_before_match_stop
        if prev is None and self._db is not None:
            try:
                raw = await self._db.get_state(MATCH_STOP_PREV_MODE_KEY)
                if raw:
                    prev = Mode(raw)
            except Exception as e:
                logger.error(f"AutoAction: не удалось прочитать режим до стопа: {e}")
        if prev is None or prev == Mode.OBSERVE:
            prev = Mode.SEMI_AUTO
        return prev

    async def resume_after_match_stop(self) -> bool:
        """Кнопка «Продолжить» после остановки на карточке «Ты понравился…».

        Возвращает режим, который был до остановки (по умолчанию SEMI_AUTO),
        снимает баннер и продолжает ленту. Если карточка всё ещё активна
        (после неё нет нашего ответа), жмёт «Показать» — иначе
        ``start_auto_stream`` снова увидел бы её и остановил поиск. Анкету
        лайкнувшей Leo пришлёт обычной карточкой, её оценит pipeline.
        True — лента продолжена.
        """
        mode = await self._match_stop_prev_mode()
        self._mode_before_match_stop = None
        self.set_mode(mode)
        if self._db is not None:
            try:
                await self._db.delete_state(MATCH_STOP_PREV_MODE_KEY)
            except Exception as e:
                logger.error(f"AutoAction: не удалось сбросить режим до стопа: {e}")
        await self._clear_stream_error()
        if self._auto_engine is None or not self._auto_engine.enabled:
            return False
        logger.info(f"AutoAction: «Продолжить» — режим {mode.value}, продолжаю поиск")

        for msg in await self._recent_history():  # новые → старые
            if getattr(msg, "out", False):
                break  # после последней карточки уже ответили
            if not self._is_match_card(msg):
                continue
            button = next(
                (t for t in self._extract_button_texts(msg)
                 if self._is_match_show_button(t)),
                None,
            )
            if button is None:
                break
            logger.info(
                f"AutoAction: карточка лайка (msg={msg.id}) — жму «{button}»"
            )
            return await self._auto_engine.send_text(button)
        return await self.start_auto_stream()

    async def _handle_captcha(self, msg: object | None = None) -> bool:
        """Разруливает капчу/проверку Leo (Stage 7.6 «память капч»).

        Капча = текст с одним из CAPTCHA_MARKERS и >= CAPTCHA_MIN_BUTTONS
        reply-кнопок (иначе легко зациклиться в меню/Premium).

        Поведение:
        1. Если для этой капчи (сигнатура = нормализованный текст) уже
           выучен ответ владельца — отправляем его (идемпотентно: только
           если текст ещё не отправлялся после карточки капчи).
        2. Если ответа нет — капча записывается в captcha_memory (pending,
           answer IS NULL) и выводится на главном экране (блок «Капчи ждут
           ответа»). Кнопку наугад
           НЕ нажимаем: ждём ответа владельца. Исключение — включённый
           fallback_press_last: тогда пробиваем капчу последней ОБЫЧНОЙ
           кнопкой (старое поведение, чтобы не замирала лента).
        3. Если капча-память выключена (captcha.enabled=false) — полностью
           старое поведение: нажимаем последнюю обычную кнопку.

        ``msg`` — конкретное сообщение капчи из live-обработки (уже есть);
        если None — сканируем последние сообщения чата (start_auto_stream).
        Возвращает True, если на капчу отправлен текст.
        """
        client = self._auto_engine.client
        if client is None or not self._auto_engine.enabled:
            return False
        try:
            if msg is not None:
                card = msg if self._is_captcha_message(msg) else None
            else:
                card = await self._find_captcha_message()
            if card is None:
                return False

            # Сигнатура капчи: одинаковый текст — одинаковая капча, один ответ.
            text_full = (getattr(card, "text", None) or "").strip()
            signature = captcha_signature(text_full)
            buttons = self._extract_button_texts(card)
            cc = self._config.auto_actions.captcha

            if cc.enabled:
                # 1) Выученный ответ — отправляем авто-ответ.
                if cc.auto_answer and self._db is not None:
                    try:
                        answer = await self._db.get_captcha_answer(signature)
                    except Exception as e:
                        logger.error(f"AutoAction: ошибка чтения ответа капчи: {e}")
                        answer = None
                    if answer:
                        return await self._send_captcha_text(
                            card, answer, used_signature=signature
                        )

                # 2) Неизвестная капча — выводим на сайт, наугад не нажимаем.
                if self._db is not None:
                    try:
                        await self._db.record_pending_captcha(
                            signature,
                            text_full,
                            json.dumps(buttons, ensure_ascii=False),
                        )
                    except Exception as e:
                        logger.error(f"AutoAction: ошибка записи pending-капчи: {e}")
                logger.warning(
                    f"AutoAction: неизвестная капча (msg={card.id}) — жду ответа "
                    f"владельца в блоке «Капчи ждут ответа» на главном экране"
                )
                if not cc.fallback_press_last:
                    return False
                # fallback_press_last: проваливаемся в старое нажатие кнопки.

            # 3) Обычная кнопка: последняя ОБЫЧНАЯ reply-кнопка (не спец-запрос).
            # Спец-кнопки (гео/телефон) текстом не нажимаются — нужен реальный
            # маркап, поэтому в гео-капче берём «Продолжить смотреть анкеты».
            plain = self._extract_plain_button_texts(card)
            if not plain:
                return False
            return await self._send_captcha_text(card, plain[-1])
        except Exception as e:
            logger.error(f"AutoAction: ошибка обработки капчи: {e}")
            return False

    async def _send_captcha_text(
        self, card: object, text: str, used_signature: str | None = None
    ) -> bool:
        """Отправляет текст (выученный ответ или кнопку) на капчу. Идемпотентно."""
        try:
            if await self._captcha_text_sent_after(card.id, text):
                logger.info(
                    f"AutoAction: ответ на капчу «{text}» уже отправлен "
                    f"(msg={card.id})"
                )
                return False
            logger.info(
                f"AutoAction: капча/проверка (msg={card.id}) — отвечаю «{text}»"
            )
            await self._auto_engine.send_text(text)
            if used_signature and self._db is not None:
                try:
                    await self._db.mark_captcha_used(used_signature)
                except Exception as e:
                    logger.error(f"AutoAction: mark_captcha_used error: {e}")
            return True
        except Exception as e:
            logger.error(f"AutoAction: ошибка отправки текста на капчу: {e}")
            return False

    async def _captcha_text_sent_after(self, msg_id: int, text: str) -> bool:
        """Отправлялся ли уже ``text`` после сообщения капчи (идемпотентность)."""
        client = self._auto_engine.client
        if client is None:
            return False
        try:
            async for msg in client.iter_messages(self._dvinchik_chat_id, limit=15):
                if msg.id <= msg_id:
                    break
                if (
                    getattr(msg, "out", False)
                    and (msg.text or "").strip() == text
                ):
                    return True
        except Exception as e:
            logger.error(f"AutoAction: ошибка проверки уже отправленного ответа: {e}")
        return False

    def _is_captcha_message(self, msg: object) -> bool:
        """Является ли сообщение капчей/сделкой/проверкой Leo.

        Текст содержит один из CAPTCHA_MARKERS, а reply-кнопок — не меньше
        CAPTCHA_MIN_BUTTONS. Меню/Premium-промо без маркеров НЕ капча.
        """
        text = (getattr(msg, "text", None) or "").lower()
        if not any(m in text for m in CAPTCHA_MARKERS):
            return False
        return len(self._extract_button_texts(msg)) >= CAPTCHA_MIN_BUTTONS

    @staticmethod
    def _is_match_show_button(button_text: str) -> bool:
        """Кнопка «Показать» на карточке взаимного лайка?

        Leo шлёт разные клавиатуры: [«1. Показать.», «2. Не хочу…»] (старый
        вид) и [«1 👍», «2 💤»] (нумерованная, прод-2026). Отказную («не хочу»
        / «💤») не считаем — её жать нельзя.
        """
        t = button_text or ""
        low = t.lower()
        if MATCH_SHOW_SKIP_FRAGMENT in low or MATCH_SHOW_SKIP_ALT_FRAGMENT in t:
            return False
        return MATCH_SHOW_FRAGMENT in low or MATCH_SHOW_ALT_FRAGMENT in t

    async def _find_captcha_message(self) -> object | None:
        """Ищет самую свежую капчу среди последних сообщений чата."""
        client = self._auto_engine.client
        if client is None:
            return None
        try:
            async for msg in client.iter_messages(self._dvinchik_chat_id, limit=15):
                if self._is_captcha_message(msg):
                    return msg
        except Exception as e:
            logger.error(f"AutoAction: ошибка поиска капчи: {e}")
        return None

    def _extract_button_texts(self, msg: object) -> list[str]:
        """Извлекает тексты reply-кнопок сообщения (пусто, если их нет).

        Возвращает ВСЕ кнопки (включая специальные запросы геолокации/телефона) —
        используются только для подсчёта количества кнопок капчи.
        """
        rm = getattr(msg, "reply_markup", None)
        rows = getattr(rm, "rows", None)
        if not rows:
            return []
        out: list[str] = []
        for row in rows:
            for b in getattr(row, "buttons", []):
                t = getattr(b, "text", "")
                if t:
                    out.append(t)
        return out

    def _extract_plain_button_texts(self, msg: object) -> list[str]:
        """Как _extract_button_texts, но только ОБЫЧНЫЕ KeyboardButton.

        Специальные кнопки (запрос геолокации/телефона и т.п.) нельзя «нажать»
        простой отправкой их текста — Leo ответит «Нет такого варианта ответа».
        Для нажатия выбираем только plain-кнопки (все, кроме special-исключений;
        класс обычной кнопки = KeyboardButton).
        """
        special: tuple[str, ...] = (
            "KeyboardButtonRequestGeoLocation",
            "KeyboardButtonRequestPhone",
            "KeyboardButtonRequestPeer",
            "KeyboardButtonUrl",
            "KeyboardButtonCallback",
            "KeyboardButtonInline",
            "KeyboardButtonSwitchInline",
            "KeyboardButtonGame",
            "KeyboardButtonPay",
        )
        rm = getattr(msg, "reply_markup", None)
        rows = getattr(rm, "rows", None)
        if not rows:
            return []
        out: list[str] = []
        for row in rows:
            for b in getattr(row, "buttons", []):
                t = getattr(b, "text", "")
                if t and type(b).__name__ not in special:
                    out.append(t)
        return out

    def _is_profile_text(self, msg: object) -> bool:
        """Является ли текст сообщения анкетой (PROFILE)."""
        text = (getattr(msg, "text", None) or "").strip()
        if not text:
            return False
        return self._parser.classify(text) == MessageType.PROFILE

    async def recover_backlog(self, batch_size: int = 200) -> int:
        """Восстанавливает необработанные RAW из БД в очередь worker'а (W3).

        Обрабатывает батчами по ``batch_size``, чтобы не держать весь backlog
        в памяти. Очередь ограничена (maxsize), поэтому ``enqueue`` блокируется
        при заполнении — worker успевает обрабатывать (естественная backpressure,
        память ограничена). Захватывает cutoff (MAX id) на момент старта, чтобы
        не дублировать живой трафик, который хендлер сам ставит в очередь.
        Возвращает число поставленных в очередь заданий.
        """
        if self._worker is None:
            return 0
        try:
            cutoff_id = await self._db.get_max_raw_id()
        except Exception as e:
            logger.error(f"Backlog recovery: ошибка БД: {e}")
            return 0

        # Recovery активна: live handler помечает свои raw_id в set (см.
        # _handle_new_message). После завершения recovery флаг снимается и
        # set очищается — steady-state live-сообщения (id > cutoff) не могут
        # быть продублированы recovery, поэтому отслеживать их не нужно.
        self._recovery_armed = True

        total = 0
        after_id = 0
        while True:
            try:
                rows = await self._db.get_unprocessed_raw_messages_before(
                    cutoff_id, batch_size, after_id
                )
            except Exception as e:
                logger.error(f"Backlog recovery: ошибка чтения БД: {e}")
                break
            if not rows:
                break
            for row in rows:
                raw_id = row["id"]
                # D: не дублируем raw_id, уже поставленный live handler'ом
                # в эту сессию (иначе один RAW попадёт и в live worker, и в
                # recover_backlog). Recovery сама не добавляет в set — её курсор
                # (after_id) гарантирует, что каждая строка читается ровно один
                # раз, поэтому set нужен только для пропуска live-энqueued.
                if raw_id in self._enqueued_raw_ids:
                    continue
                task = RawTask(
                    chat_id=row["chat_id"],
                    message_id=row["telegram_message_id"],
                    sender_id=row["sender_id"],
                    sender_username=row.get("sender_username") or "",
                    sender_name=row.get("sender_name") or "",
                    text=row.get("text") or "",
                    media_type=row.get("media_type") or "",
                    entities_json=row.get("raw_entities") or "[]",
                    reply_markup_json=row.get("reply_markup") or "[]",
                    reply_to=row.get("reply_to_message_id"),
                    received_at=row["received_at"],
                    msg_date=row["message_date"],
                    msg=None,
                    raw_id=raw_id,
                    account_session=row.get("account_session") or "",
                )
                try:
                    await self._worker.enqueue(task)
                    total += 1
                except Exception as e:
                    logger.error(f"Backlog recovery: ошибка enqueue: {e}")
                    break
            # Продвигаем курсор, чтобы не переобрабатывать те же строки
            # (worker может ещё не пометить их processed_at).
            after_id = max(r["id"] for r in rows)
        # После recovery live-сообщения имеют id > cutoff и с recovery не
        # пересекаются, поэтому set больше не нужен: снимаем защиту и очищаем.
        # В steady-state (recovery не активна) live handler НЕ добавляет в set,
        # поэтому память не растёт бесконечно (MEDIUM-1).
        self._recovery_armed = False
        self._enqueued_raw_ids.clear()
        if total:
            logger.info(f"Backlog recovery: поставлено заданий: {total}")
        return total

    def register(self) -> None:
        """Регистрирует обработчики на всех аккаунтах.

        - incoming=True  — входящие сообщения (парсинг, фильтрация, AI).
        - outgoing=True   — исходящие сообщения (actions пользователя) только
          в чате бота: сохраняются в raw_messages, pipeline пропускается.
        - CallbackQuery   — inline-кнопки (callback_data): логируются для
          разведки механики LIKE.
        """
        for client in self._clients:
            client.add_event_handler(
                self._handle_new_message,
                events.NewMessage(incoming=True),
            )
            client.add_event_handler(
                self._handle_outgoing_message,
                events.NewMessage(outgoing=True),
            )
            client.add_event_handler(
                self._handle_callback_query,
                events.CallbackQuery(),
            )
        logger.info(
            f"Collector registered ({len(self._clients)} account(s)): "
            f"listening for new messages + outgoing + callbacks"
        )

    async def _handle_new_message(self, event: events.NewMessage.Event) -> None:
        """Обработчик: сохраняет RAW потом парсит."""
        # === SOURCE FILTER (allowlist) ===
        # Источник отбрасывается ДО любых операций: до classify, логирования,
        # создания/сохранения RAW, фильтра, AI и скачивания media.
        chat_id = event.chat_id
        if chat_id not in self._allowed_chat_ids:
            logger.debug(
                f"Ignored Telegram message from unauthorized chat_id={chat_id}"
            )
            return

        # Аккаунт-получатель: у каждого аккаунта свой чат с Leo в ОДНОМ chat_id
        # (свои диапазоны message id). Все ключи (дедуп, контекст, лок) — на
        # пару (account_session, chat_id).
        account_session = self._session_for_client(
            getattr(event, "client", None)
        )

        # === DEDUP (in-memory, атомарно в asyncio) ===
        # Быстрый фильтр повторной доставки в рамках сессии. Проверка БЕЗ
        # добавления: сам факт обработки фиксируется только ПОСЛЕ успешного
        # RAW-save (см. ниже ``mark``), чтобы при сбое сохранения сообщение не
        # терялось до restart (MEDIUM-2). Авторитетная защита — UNIQUE
        # (account_session, chat_id, telegram_message_id) в БД: save_raw_message
        # делает INSERT OR IGNORE и возвращает None при дубликате (переживает
        # restart/reconnect, когда in-memory Dedup пуст).
        if self._dedup.is_known((account_session, chat_id, event.message.id)):
            logger.debug(
                f"Повторная доставка отброшена (in-memory dedup): "
                f"account={account_session}, chat_id={chat_id}, "
                f"message_id={event.message.id}"
            )
            return

        msg = event.message

        # Синхронно доступный sender_id — БЕЗ network await (RAW-first):
        # сохраняем RAW до любого обращения к Telegram API.
        sender_id = msg.sender_id if getattr(msg, "sender_id", None) else 0

        text = msg.text or ""
        media_type = _detect_media_type(msg)
        entities_json = self._serialize_entities(msg)
        reply_markup_json = self._serialize_reply_markup(msg)
        reply_to = msg.reply_to_msg_id if msg.reply_to else None
        now = datetime.now(timezone.utc).isoformat()
        msg_date = msg.date.isoformat() if msg.date else now

        # === Сериализация save+enqueue на уровне chat_id (C) ===
        # Гарантирует порядок PROFILE → MEDIA_ONLY для одного чата даже при
        # конкурентных Telegram handlers. Без глобальной блокировки и без
        # второй очереди: первый обработчик чата захватывает per-chat lock,
        # выполняет save+enqueue; второй (напр. MEDIA_ONLY) дожидается и
        # становится в очередь строго ПОСЛЕ PROFILE. Блокировка только внутри
        # одного chat_id, другие чаты не затрагиваются.
        lock_key = (account_session, chat_id)
        lock = self._chat_locks.get(lock_key)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[lock_key] = lock
        async with lock:
            # === RAW сохраняется ПЕРВЫМ — ДО любого network await ===
            # save_raw_message сам делает ограниченный retry при транзитных
            # сбоях БД. Если после retry сохранить не удалось — сообщение НЕ
            # идёт дальше в pipeline (W4). C1/C2: дубликат → None (UNIQUE).
            try:
                raw_id = await self._db.save_raw_message(
                    telegram_message_id=msg.id,
                    chat_id=chat_id,
                    sender_id=sender_id,
                    sender_username="",
                    sender_name="",
                    message_date=msg_date,
                    text=text,
                    raw_entities=entities_json,
                    reply_markup=reply_markup_json,
                    media_type=media_type,
                    reply_to_message_id=reply_to,
                    received_at=now,
                    account_session=account_session,
                )
            except Exception as e:
                logger.error(
                    f"RAW save не удался (после retry) — сообщение НЕ попадает "
                    f"в pipeline: chat_id={chat_id}, message_id={msg.id}: {e}"
                )
                return
            if raw_id is None:
                logger.debug(
                    f"Повторная доставка отброшена (БД UNIQUE): "
                    f"chat_id={chat_id}, message_id={msg.id}"
                )
                return

            # C2/MEDIUM-2: помечаем сообщение в dedup ТОЛЬКО после успешного
            # RAW-save. Если save упал (W4 исчерпал retry), dedup НЕ
            # помечается — при повторной доставке сообщение можно переобработать,
            # а не терять до restart.
            self._dedup.mark((account_session, chat_id, event.message.id))

            # Активная карточка для гейта «реакция в свою карточку»
            # (AutoActionEngine.is_stale_card). Помечаем ЗДЕСЬ, в хендлере, а
            # не в worker'е: метка должна отражать реальный порядок показа
            # карточек, иначе отставание pipeline не видно и ❤️/👎 уезжают
            # на следующую анкету (прод: 54 промаха из 60). Классификация
            # чистая — regex, без сети и БД; RAW уже сохранён выше.
            if (
                self._auto_engine.enabled
                and chat_id == self._dvinchik_chat_id
                and event.client is self._auto_engine.client
            ):
                try:
                    if (
                        self._parser.classify(
                            text, has_media=media_type != ""
                        )
                        is MessageType.PROFILE
                    ):
                        self._auto_engine.note_card(chat_id, msg.id)
                    elif not getattr(msg, "out", False) and self._is_match_card(msg):
                        # «Ты понравился N девушке…» — стоп СРАЗУ, до очереди:
                        # pipeline может отставать, и queued 👎 на прошлую
                        # анкету ушёл бы после карточки (лента листалась
                        # дальше). После стопа режим OBSERVE — задержка
                        # следующих сообщений чата на уведомление не важна.
                        await self._stop_on_match_card(msg)
                except Exception as e:
                    logger.error(f"AutoAction: отметка карточки не удалась: {e}")

            # D: raw_id поставлен в очередь в этой сессии (live handler).
            # Только пока активна startup-recovery (recover_backlog) — иначе в
            # steady-state set рос бы бесконечно (MEDIUM-1). При переполнении
            # (на случай, если recovery не запускалась) — bounded eviction.
            if self._recovery_armed:
                s = self._enqueued_raw_ids
                s.add(raw_id)
                if len(s) > self._enqueued_raw_ids_cap:
                    for _ in range(len(s) - self._enqueued_raw_ids_cap // 2):
                        s.pop()

            # === Только ПОСЛЕ успешного RAW-save: best-effort обогащение sender ===
            # Сетевой вызов; ошибка/None sender не мешает сохранению и pipeline.
            sender_username = ""
            sender_name = ""
            try:
                sender = await msg.get_sender()
                if sender is not None:
                    sender_username = getattr(sender, "username", "") or ""
                    if getattr(sender, "first_name", None):
                        sender_name = (
                            (sender.first_name or "") + " " + (sender.last_name or "")
                        ).strip()
            except Exception as e:
                logger.debug(
                    f"get_sender недоступен (RAW уже сохранён): {e}"
                )

            # === Формируем задание для pipeline ===
            task = RawTask(
                chat_id=chat_id,
                message_id=msg.id,
                sender_id=sender_id,
                sender_username=sender_username,
                sender_name=sender_name,
                text=text,
                media_type=media_type,
                entities_json=entities_json,
                reply_markup_json=reply_markup_json,
                reply_to=reply_to,
                received_at=now,
                msg_date=msg_date,
                msg=msg,
                raw_id=raw_id,
                account_session=account_session,
            )

            if self._worker is not None:
                # Worker привязан: хендлер только ставит в очередь, а дорогой
                # pipeline (parse → filter → AI) выполняется фоновой задачей
                # вне Telegram event handler'а.
                await self._worker.enqueue(task)
                return

            # Fallback (без worker — тесты/отладка): обрабатываем синхронно.
            await self._process_message(task)

    # ------------------------------------------------------------------
    # Outgoing messages (действия пользователя: лайки/дизлайки эмодзи)
    # ------------------------------------------------------------------

    async def _handle_outgoing_message(self, event: events.NewMessage.Event) -> None:
        """Перехват исходящих сообщений (actions пользователя) в чате бота.

        Сохраняет RAW в raw_messages с ``is_outgoing=True`` и помечает
        processed_at — pipeline (парсинг/фильтр/AI) НЕ запускается. Единственная
        цель — ground truth для реверса механики LIKE: что именно отправляет
        пользователь.

        Исходящие — полноценные события ленты: веб показывает их отдельной
        строкой (``is_outgoing``), а не как сообщение от Leo. ``sent_messages`` /
        ``auto_actions_log`` в ленту НЕ подмешиваются — это журналы действий.
        """
        chat_id = event.chat_id
        if chat_id != self._dvinchik_chat_id:
            return

        msg = event.message
        text = msg.text or ""
        if not text.strip():
            return

        sender_id = msg.sender_id if getattr(msg, "sender_id", None) else 0
        msg_date = msg.date.isoformat() if msg.date else datetime.now(timezone.utc).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        account_session = self._session_for_client(
            getattr(event, "client", None)
        )

        # === RAW сохраняется ПЕРВЫМ ===
        try:
            raw_id = await self._db.save_raw_message(
                telegram_message_id=msg.id,
                chat_id=chat_id,
                sender_id=sender_id,
                sender_username="",
                sender_name="",
                message_date=msg_date,
                text=text,
                raw_entities="[]",
                reply_markup="[]",
                media_type="",
                reply_to_message_id=None,
                received_at=now,
                is_outgoing=True,
                account_session=account_session,
            )
        except Exception as e:
            logger.error(f"Outgoing RAW save failed: {e}")
            return
        if raw_id is None:
            return

        # Помечаем обработанным — pipeline пропускается.
        try:
            await self._db.mark_raw_processed(raw_id)
        except Exception as e:
            logger.error(f"Outgoing mark processed failed: {e}")

        self._print_outgoing_message(chat_id, msg.id, text, msg_date)

        await self._maybe_record_manual_review(
            chat_id, msg.id, text, account_session,
        )
        await self._maybe_record_sent_message(
            chat_id, text, msg, account_session,
        )

    async def _maybe_record_sent_message(
        self, chat_id: int, text: str, msg: object,
        account_session: str = "",
    ) -> None:
        """Stage 8.5: фиксирует РУЧНОЕ действие владельца при лайке.

        Авто-сообщения «Берем)» не попадают сюда: отправки с авто-аккаунта
        уже отслеживаются в auto_actions_log(message_text). Здесь — действия
        владельца (клиент НЕ авто-аккаунт):
          - «❤️»  → ручной LIKE (сам лайкнул),
          - «👎»  → ручной DISLIKE,
          - текст → ручное сообщение (MESSAGE), кроме кнопок/реакций из
            MANUAL_MESSAGE_EXCLUDE и коротких (len < 2).
        Профиль для привязки ответа берём из «текущей» анкеты чата
        (chat_context / pending). Ошибки БД не ломают перехват исходящих
        (RAW уже сохранён).
        """
        t = (text or "").strip()
        if not t:
            return
        # Исходящие с авто-аккаунта пропускаем ТОЛЬКО когда авто-действия
        # реально включены (SEMI_AUTO/AUTO): тогда их шлёт AutoActionEngine,
        # и они уже записаны в auto_actions_log. В OBSERVE авто ничего не
        # шлёт — любой исходящий (в т.ч. с авто-аккаунта) = ручное действие
        # владельца, его записываем в sent_messages.
        if (
            self._auto_engine.enabled
            and self._auto_engine.client is not None
            and getattr(msg, "client", None) is self._auto_engine.client
        ):
            return
        # Реакции владельца = ручной лайк/дизлайк (сам лайкнул и т.п.).
        if t == "❤️":
            action = "LIKE"
        elif t == "👎":
            action = "DISLIKE"
        else:
            if len(t) < 2 or is_manual_message_excluded(t):
                return
            action = "MESSAGE"
        profile_id: int | None = None
        try:
            context = await self._db.get_chat_profile_context(
                chat_id, account_session,
            )
            if context is None:
                context = self._pending_profiles.get((account_session, chat_id))
            if context is not None:
                profile_id = await self._db.resolve_profile_by_message(
                    context, account_session,
                )
        except Exception as e:
            logger.warning(f"sent_message: не смогли привязать профиль: {e}")
        try:
            await self._db.record_sent_message(
                t, chat_id, getattr(msg, "id", 0), source="manual",
                action=action, profile_id=profile_id,
            )
        except Exception as e:
            logger.warning(f"record_sent_message error: {e}")

    async def _maybe_record_manual_review(
        self, chat_id: int, outgoing_tm_id: int, text: str,
        account_session: str = "",
    ) -> None:
        """Фиксирует ручное решение владельца по активной REVIEW-анкете.

        Исходящее действие (❤️/👎/сообщение) в чате Дайвинчика привязывается к
        «текущей» анкете чата (chat_context / _pending_profiles). Если последнее
        AI-решение этой анкеты — REVIEW, действие записывается в журнал ручных
        решений. Ошибки не ломают перехват исходящих (RAW уже сохранён).
        """
        if not self._manual_review or not getattr(
            self._manual_review, "enabled", False
        ):
            return
        if chat_id != self._dvinchik_chat_id:
            return
        context = await self._db.get_chat_profile_context(
            chat_id, account_session,
        )
        if context is None:
            context = self._pending_profiles.get((account_session, chat_id))
        if context is None:
            return
        try:
            await self._manual_review.handle_outgoing(
                chat_id, context, outgoing_tm_id, text,
            )
        except Exception as e:
            logger.error(f"ManualReview: ошибка обработки исходящего: {e}")

    def _print_outgoing_message(
        self, chat_id: int, message_id: int, text: str, msg_date: str,
    ) -> None:
        """Красивый вывод исходящего сообщения (action пользователя)."""
        table = Table(show_header=False, border_style="dim", padding=(0, 1))
        table.add_column("Key", style="bold magenta", width=14)
        table.add_column("Value")
        table.add_row("Source", "[bold magenta]USER ACTION[/bold magenta]")
        table.add_row("Chat ID", str(chat_id))
        table.add_row("Message ID", str(message_id))
        table.add_row("Date", msg_date[:19])
        preview = text[:200] + ("..." if len(text) > 200 else "")
        table.add_row("Text", preview)
        console.print(Panel(
            table,
            title="[bold magenta]OUTGOING[/bold magenta]",
            border_style="magenta",
        ))

    # ------------------------------------------------------------------
    # Callback queries (inline-кнопки — разведка механики LIKE)
    # ------------------------------------------------------------------

    async def _handle_callback_query(self, event: events.CallbackQuery.Event) -> None:
        """Логирование callback queries для разведки кнопок LIKE.

        Read-only: данные neither сохраняются в БД, ни вызывают действия.
        Если лайк ставится через inline-кнопку — callback_data будет виден
        в логе.
        """
        data = event.data
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")

        sender_id = event.sender_id if getattr(event, "sender_id", None) else 0

        logger.info(
            f"CALLBACK QUERY: chat_id={event.chat_id}, "
            f"sender_id={sender_id}, data={data}"
        )

        table = Table(show_header=False, border_style="dim", padding=(0, 1))
        table.add_column("Key", style="bold cyan", width=14)
        table.add_column("Value")
        table.add_row("Source", "[bold cyan]CALLBACK QUERY[/bold cyan]")
        table.add_row("Chat ID", str(event.chat_id))
        table.add_row("Sender ID", str(sender_id))
        table.add_row("Data", data)
        console.print(Panel(
            table,
            title="[bold cyan]CALLBACK QUERY[/bold cyan]",
            border_style="cyan",
        ))

    # ── Фото: сигарета/алкоголь → 👎 (services/photo_vice.py) ──────────

    @staticmethod
    def _is_card_video(msg: object) -> bool:
        """Видео анкеты (в Telegram — документ video/*): берём его превью-кадр."""
        if getattr(msg, "video", None):
            return True
        doc = getattr(msg, "document", None)
        return bool(doc) and str(getattr(doc, "mime_type", "") or "").startswith("video/")

    @classmethod
    def _has_card_media(cls, msg: object) -> bool:
        return bool(getattr(msg, "photo", None)) or cls._is_card_video(msg)

    async def _card_photos(self, msg: object, limit: int) -> list[bytes]:
        """Байты фото карточки: само сообщение + остальной альбом (grouped_id).

        Альбом Leo приходит пачкой сообщений с соседними id, поэтому соседи
        берутся одним запросом. Видео-анкета (прод 2026-10-23: ~1 из 9
        карточек) — берётся превью-кадр видео (``thumb=-1``), иначе модель
        вкуса молчала и решало старое правило. Ошибки → то, что удалось скачать.
        """
        msgs = [msg]
        grouped_id = getattr(msg, "grouped_id", None)
        client = getattr(msg, "client", None)
        if grouped_id and client is not None:
            try:
                chat = await msg.get_input_chat()
                around = await client.get_messages(
                    chat, ids=list(range(msg.id - 9, msg.id + 10)),
                )
                album = [
                    m for m in around
                    if m is not None and getattr(m, "grouped_id", None) == grouped_id
                ]
                if album:
                    msgs = sorted(album, key=lambda m: m.id)
            except Exception as e:
                logger.debug(f"PhotoVice: альбом не прочитан: {e!r}")
        media = [m for m in msgs if self._has_card_media(m)][:limit]
        results = await asyncio.gather(
            *(
                m.download_media(file=bytes, thumb=-1)
                if self._is_card_video(m) and not getattr(m, "photo", None)
                else m.download_media(file=bytes)
                for m in media
            ),
            return_exceptions=True,
        )
        return [r for r in results if isinstance(r, (bytes, bytearray)) and r]

    # ── Фоновый досчёт старых анкет (вердикт + отпечаток фото) ──────────

    #: Пауза, когда досчитывать нечего (новые анкеты считает живая тень).
    PHOTO_BACKFILL_IDLE_SEC = 600.0

    async def _backfill_photo_bytes(self, photos: list[dict]) -> list[bytes]:
        """Фото из кэша media/; нет в кэше — скачиваем аккаунтом-получателем
        и кладём в кэш (тогда и на /rate они откроются сразу)."""
        from web.photos import _cache_filename  # noqa: PLC0415 — общая схема имён кэша

        out: list[bytes] = []
        missing: list[dict] = []
        for ph in photos:
            path = _cache_filename(ph["profile_id"], ph["message_id"], ph["account_session"])
            if path.exists():
                out.append(path.read_bytes())
            else:
                missing.append(ph)
        if not missing:
            return out
        client, _ = self._clients_for_session(missing[0]["account_session"])
        if client is None:
            return out
        try:
            msgs = await client.get_messages(
                missing[0]["chat_id"], ids=[ph["message_id"] for ph in missing],
            )
        except Exception as e:
            logger.debug(f"PhotoBackfill: get_messages: {e!r}")
            return out
        for ph, m in zip(missing, msgs or []):
            if m is None or not getattr(m, "photo", None):
                continue
            try:
                data = await m.download_media(file=bytes)
            except Exception as e:
                logger.debug(f"PhotoBackfill: download {ph['message_id']}: {e!r}")
                continue
            if not data:
                continue
            path = _cache_filename(ph["profile_id"], ph["message_id"], ph["account_session"])
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            except OSError:
                pass
            out.append(data)
        return out

    async def run_photo_backfill(self) -> None:
        """Фоновый досчёт: анкеты без отпечатка фото → вердикт + отпечаток.

        Сначала НЕоценённые на /rate (для очереди «модель сомневается»), потом
        оценённые (для переобучения). Медленно и по одной, чтобы не мешать
        ленте: CLIP на CPU ~1.5 с на фото.
        """
        shadow = self._photo_shadow
        cfg = self._config.photo_taste
        if shadow is None or not cfg.enabled or not cfg.backfill:
            return
        await asyncio.sleep(60)  # даём боту подняться и загрузить CLIP
        done = 0
        while True:
            try:
                if not shadow.enabled:
                    await asyncio.sleep(self.PHOTO_BACKFILL_IDLE_SEC)
                    continue
                batch = await self._db.get_photo_backfill_batch(
                    limit=10, max_photos=max(1, int(cfg.max_photos)),
                )
                if not batch:
                    if done:
                        logger.info(f"PhotoBackfill: досчитано {done} анкет, жду новых")
                        done = 0
                    await asyncio.sleep(self.PHOTO_BACKFILL_IDLE_SEC)
                    continue
                for item in batch:
                    pid = item["profile_id"]
                    photos = [dict(ph, profile_id=pid) for ph in item["photos"]]
                    images = await self._backfill_photo_bytes(photos)
                    result = (
                        await asyncio.to_thread(shadow.score_images, images)
                        if images else None
                    )
                    if not result:
                        # Отметка «не получилось», чтобы не крутиться на ней.
                        await self._db.save_photo_embedding(pid, None, 0, "")
                    else:
                        await self._db.save_photo_embedding(
                            pid, result["embedding"], result["n_photos"],
                            result["model_version"],
                        )
                        first = photos[0]
                        await self._db.save_photo_score(
                            profile_id=pid,
                            account_session=first["account_session"],
                            chat_id=first["chat_id"],
                            telegram_message_id=first["message_id"],
                            score=result["score"],
                            verdict=result["verdict"],
                            smoke_prob=result.get("smoke"),
                            alcohol_prob=result.get("alcohol"),
                            n_photos=result["n_photos"],
                            model_version=result["model_version"],
                        )
                        done += 1
                        if done % 50 == 0:
                            logger.info(f"PhotoBackfill: досчитано {done} анкет")
                    await asyncio.sleep(max(0.0, float(cfg.backfill_pause_sec)))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"PhotoBackfill: {e!r}")
                await asyncio.sleep(60)

    #: Сколько готовых результатов карточек держать в памяти.
    PHOTO_RESULTS_KEEP = 64

    def _remember_photo_result(self, key: tuple[str, int, int], result: dict) -> None:
        cache = getattr(self, "_photo_shadow_results", None)
        if cache is None:
            cache = self._photo_shadow_results = {}
        cache[key] = result
        while len(cache) > self.PHOTO_RESULTS_KEEP:
            cache.pop(next(iter(cache)))

    async def _photo_result_for(self, key: tuple[str, int, int] | None) -> dict | None:
        """Результат теневого расчёта карточки: готовый или дождаться идущего."""
        if key is None:
            return None
        ready = getattr(self, "_photo_shadow_results", {}).get(key)
        if ready is not None:
            return ready
        task = getattr(self, "_photo_shadow_pending", {}).get(key)
        if task is None:
            return None
        try:
            return await asyncio.wait_for(
                asyncio.shield(task), timeout=self.PHOTO_SHADOW_WAIT_SEC,
            )
        except Exception as e:
            logger.debug(f"PhotoShadow: результат карточки не дождались: {e!r}")
            return None

    async def _apply_photo_taste(
        self, decision: object, card_key: tuple[str, int, int],
    ) -> object:
        """Безопасная обёртка: любая ошибка → решение без изменений (лента не
        встаёт из-за фото-модели — прод-баг 2026-10-23)."""
        try:
            return await self._apply_photo_taste_impl(decision, card_key)
        except Exception as e:
            logger.error(f"PhotoTaste: сбой решения по фото, оставляю прежнее: {e!r}")
            return decision

    async def _apply_photo_taste_impl(
        self, decision: object, card_key: tuple[str, int, int],
    ) -> object:
        """Режим decide: LIKE/DISLIKE по модели вкуса (результат теневого расчёта
        этой карточки). Текстовые запреты и сильный плюс главнее; нет фото,
        CLIP недоступен или не дождались — решение не меняется (старое правило).
        """
        cfg = self._config.photo_taste
        shadow = self._photo_shadow
        if (
            not getattr(cfg, "decides", False)
            or shadow is None
            or self._decision_service is None
            or not self._decision_service.photo_taste_applies(decision)
        ):
            return decision
        result = await self._photo_result_for(card_key)
        if not result:
            # Нет вердикта по этой карточке (видео без кадра, не успели) — у
            # повторно показанной девушки есть отпечаток фото прошлого показа.
            try:
                blob = await self._db.get_photo_embedding(decision.profile_id)
                result = shadow.score_embedding(blob) if blob else None
            except Exception as e:
                logger.debug(f"PhotoTaste: отпечаток прошлого показа не прочитан: {e!r}")
                result = None
            if result:
                logger.info(
                    f"PhotoTaste: profile #{decision.profile_id} — по фото прошлого показа"
                )
        if not result:
            logger.info(
                f"PhotoTaste: profile #{decision.profile_id} — вердикта модели нет "
                "(нет фото / не успели), решает старое правило"
            )
            return decision
        try:
            threshold = shadow.like_threshold(float(cfg.like_share))
        except Exception as e:
            logger.warning(f"PhotoTaste: порог модели не получен: {e!r}")
            return decision
        if threshold is None:
            return decision
        try:
            return await self._decision_service.apply_photo_taste(
                decision, float(result["score"]), float(result["score"]) >= threshold,
                str(result.get("model_version") or ""),
            )
        except Exception as e:
            logger.error(f"PhotoTaste: не удалось записать решение: {e!r}")
            return decision

    #: Сколько ждать фонового расчёта этой карточки, прежде чем считать самим.
    PHOTO_SHADOW_WAIT_SEC = 15.0

    async def _apply_photo_vice(
        self,
        decision: object,
        msg: object,
        card_key: tuple[str, int, int] | None = None,
    ) -> object:
        """Безопасная обёртка: любая ошибка → решение без изменений."""
        try:
            return await self._apply_photo_vice_impl(decision, msg, card_key)
        except Exception as e:
            logger.error(f"PhotoVice: сбой проверки фото, оставляю прежнее: {e!r}")
            return decision

    async def _apply_photo_vice_impl(
        self,
        decision: object,
        msg: object,
        card_key: tuple[str, int, int] | None = None,
    ) -> object:
        """LIKE → DISLIKE, если на фото карточки уверенно найдены сигарета/алкоголь.

        Если по карточке уже идёт теневой расчёт (он считает и вероятности
        пороков), берём его результат — CLIP на CPU ~1.5 с на фото, двойной
        прогон задерживал бы реакцию. Иначе считаем сами.
        Любая ошибка (нет torch, сеть, битое фото) — решение не меняется:
        проверка — дополнительный фильтр, бот без неё работает как раньше.
        """
        detector = self._photo_vice
        if (
            detector is None
            or not getattr(detector, "enabled", False)
            or msg is None
            or self._decision_service is None
        ):
            return decision
        try:
            hit = None
            shadow_result = await self._photo_result_for(card_key)
            if shadow_result and shadow_result.get("smoke") is not None:
                hit = detector.best_hit([{
                    "smoke": shadow_result.get("smoke") or 0.0,
                    "alcohol": shadow_result.get("alcohol") or 0.0,
                }])
            else:
                images = await self._card_photos(msg, detector.max_photos)
                hit = await detector.check_async(images)
        except Exception as e:
            logger.error(f"PhotoVice: проверка фото не удалась: {e!r}")
            return decision
        if hit is None:
            logger.debug(f"PhotoVice: profile #{decision.profile_id} — чисто")
            return decision
        try:
            return await self._decision_service.apply_photo_vice(decision, hit.reason())
        except Exception as e:
            logger.error(f"PhotoVice: не удалось записать решение: {e!r}")
            return decision

    def _schedule_photo_shadow(
        self,
        msg: object,
        profile_id: int,
        account_session: str,
        chat_id: int,
        message_id: int,
    ) -> None:
        """Ставит фоновый расчёт теневого вердикта по фото карточки.

        Не ждём: CLIP на CPU занимает секунды, а реакция Leo должна уйти
        быстро. Очередь ограничена photo_taste.max_pending — при быстром
        листании лишние карточки пропускаются (в лог DEBUG).
        """
        shadow = self._photo_shadow
        if shadow is None or msg is None or not self._has_card_media(msg):
            return
        try:
            if not shadow.enabled:
                return
        except Exception:
            return
        cap = max(1, int(self._config.photo_taste.max_pending))
        if len(self._photo_shadow_pending) >= cap:
            logger.debug(f"PhotoShadow: очередь полна, карточка {message_id} пропущена")
            return
        key = (account_session or "", chat_id, message_id)
        if key in self._photo_shadow_pending:
            return
        task = asyncio.create_task(
            self._photo_shadow_run(msg, profile_id, account_session, chat_id, message_id)
        )
        self._photo_shadow_pending[key] = task
        task.add_done_callback(lambda _t, k=key: self._photo_shadow_pending.pop(k, None))

    async def _photo_shadow_run(
        self,
        msg: object,
        profile_id: int,
        account_session: str,
        chat_id: int,
        message_id: int,
    ) -> dict | None:
        """Фото карточки → CLIP → вердикт вкуса → photo_scores. Ошибки — в лог.

        Возвращает результат (score/verdict/smoke/alcohol) — его переиспользует
        проверка сигарет/алкоголя для этой же карточки.
        """
        try:
            limit = max(1, int(self._config.photo_taste.max_photos))
            images = await self._card_photos(msg, limit)
            if not images:
                return None
            result = await asyncio.to_thread(self._photo_shadow.score_images, images)
            if not result:
                return None
            if result.get("embedding"):
                await self._db.save_photo_embedding(
                    profile_id, result["embedding"], result["n_photos"],
                    result["model_version"],
                )
            await self._db.save_photo_score(
                profile_id=profile_id,
                account_session=account_session or "",
                chat_id=chat_id,
                telegram_message_id=message_id,
                score=result["score"],
                verdict=result["verdict"],
                smoke_prob=result.get("smoke"),
                alcohol_prob=result.get("alcohol"),
                n_photos=result["n_photos"],
                model_version=result["model_version"],
            )
            logger.info(
                f"PhotoShadow: profile #{profile_id} → {result['verdict']} "
                f"(score {result['score']:.2f}, фото {result['n_photos']})"
            )
            self._remember_photo_result(
                (account_session or "", chat_id, message_id), result,
            )
            return result
        except Exception as e:
            logger.warning(f"PhotoShadow: profile #{profile_id}: {e!r}")
            return None

    async def _process_message(self, task: RawTask) -> None:
        """Выполняет pipeline: parse → filter → AI → decision.

        Вызывается worker'ом (в проде) или напрямую из хендлера (fallback).
        RAW к этому моменту уже сохранён — ошибки pipeline не теряют сырьё.
        """
        chat_id = task.chat_id
        msg = task.msg
        text = task.text
        media_type = task.media_type
        sender_id = task.sender_id
        sender_username = task.sender_username
        sender_name = task.sender_name
        msg_date = task.msg_date

        logger.info(
            f"New Telegram message: chat_id={chat_id}, "
            f"sender_id={sender_id}, message_id={task.message_id}, "
            f"media={media_type}"
        )

        # === Парсер работает после сохранения ===
        success = False
        try:
            has_media = media_type != ""
            msg_type = self._parser.classify(text, has_media=has_media)

            # Stage 8.5.1: Leo отклонил лайк из-за дневного лимита
            # («Слишком много ❤️ за сегодня. Перейди на Premium…»). Такой лайк
            # не дошёл — помечаем последний лайк чата rejected, чтобы он не
            # портил конверсию (вечный responded=0). Само сообщение трактуем
            # как SERVICE, чтобы не давать ложный «Unknown message format».
            if (
                msg_type not in (MessageType.PROFILE, MessageType.MEDIA_ONLY)
                and chat_id == self._dvinchik_chat_id
            ):
                try:
                    if self._parser.is_like_rejection(text):
                        await self._db.mark_like_rejected(chat_id)
                        msg_type = MessageType.SERVICE
                        logger.info(
                            f"Leo: лайк отклонён (лимит). message_id={task.message_id}"
                        )
                        # Лайки исчерпаны до ближайших 07:00: движок перестаёт
                        # слать ❤️ (👎 продолжает), снимает цепочки «Берем)» и
                        # один раз уведомляет владельца. Состояние — в БД.
                        # Лимит — свойство аккаунта: отказ, пришедший на
                        # ДРУГОЙ аккаунт (ручные лайки владельца), лимит
                        # исполнителя не трогает.
                        engine_session = (
                            self._auto_engine.account_session
                            if self._auto_engine is not None else ""
                        )
                        if (
                            self._auto_engine is not None
                            and task.account_session
                            and engine_session
                            and task.account_session != engine_session
                        ):
                            logger.info(
                                f"Leo: лимит ❤️ на {task.account_session} — "
                                f"исполнитель {engine_session} не затронут"
                            )
                        elif self._auto_engine is not None:
                            try:
                                await self._auto_engine.handle_like_rejection(chat_id)
                            except Exception as e:
                                logger.error(
                                    f"AutoAction: обработка лимита лайков: {e}"
                                )
                except Exception as e:
                    logger.warning(f"like_rejection hook error: {e}")

            logger.info(f"Message classified: type={msg_type.value}")

            self._print_message(
                chat_id=chat_id,
                message_id=task.message_id,
                sender_name=sender_name,
                sender_username=sender_username,
                text=text,
                msg_date=msg_date,
                media_type=media_type,
                msg_type=msg_type,
                buttons=task.reply_markup_json,
            )

            # Stage 8 (§30): продвижение цепочки «Берем)» (LIKE_AND_MESSAGE)
            # по ответам Leo. Только для входящих НЕ-профильных сообщений чата
            # Дайвинчика на авто-аккаунте. step_pending сам проверяет, есть ли
            # активная цепочка, и шлёт 💌/«Берем)» по маркерам ответов Leo.
            if (
                msg_type != MessageType.PROFILE
                and self._auto_engine.enabled
                and chat_id == self._dvinchik_chat_id
                and msg is not None
                and getattr(msg, "client", None) is self._auto_engine.client
            ):
                try:
                    await self._auto_engine.step_pending(text, chat_id)
                except Exception as e:
                    logger.error(f"AutoAction: ошибка продвижения «Берем)»: {e}")

            if msg_type == MessageType.PROFILE:
                parsed = self._parser.parse_profile(
                    text,
                    source_message_id=task.message_id,
                    source_chat_id=chat_id,
                )

                if self._profile_service:
                    try:
                        account_session = task.account_session or (
                            self._session_for_client(
                                getattr(msg, "client", None)
                                if msg is not None else None
                            )
                        )
                        profile = await self._profile_service.upsert_profile(
                            parsed, account_session=account_session,
                        )

                        # PROFILE-сообщение становится "контекстом" для
                        # последующих MEDIA_ONLY ТОГО ЖЕ аккаунта. Фиксируем сразу
                        # после сохранения — независимо от вывода в консоль.
                        self._pending_profiles[(account_session, chat_id)] = (
                            task.message_id
                        )
                        # Персистентный контекст: переживает restart/reconnect,
                        # когда in-memory кэш пуст (W2).
                        try:
                            await self._db.set_chat_profile_context(
                                chat_id, task.message_id, account_session
                            )
                        except Exception as e:
                            logger.error(f"Ошибка сохранения контекста чата: {e}")

                        is_new = profile.status == "NEW"
                        self._print_profile_stored(profile, is_new=is_new)
                        # Теневой вердикт модели вкуса по фото (фон, не ждём).
                        self._schedule_photo_shadow(
                            msg, profile.id, account_session, chat_id, task.message_id,
                        )

                        if self._filter_service:
                            try:
                                filter_result = await self._filter_service.evaluate(profile)
                                self._print_filter_result(profile, filter_result)

                                # Deterministic scoring (Stage 8): всегда через DecisionService.
                                # Нет изображений, нет LLM — только текстовые правила.
                                if self._decision_service:
                                    try:
                                        decision = (
                                            await self._decision_service.evaluate(
                                                profile.id,
                                                filter_result=filter_result,
                                            )
                                        )
                                        self._print_ai_decision(profile, decision)
                                        # Stage 7: авто-действие (SEMI_AUTO).
                                        # Только если анкета пришла на авто-аккаунт
                                        # (msg.client — аккаунт-получатель).
                                        if (
                                            self._auto_engine.enabled
                                            and decision is not None
                                            and msg is not None
                                            and getattr(msg, "client", None)
                                            is self._auto_engine.client
                                        ):
                                            try:
                                                # Правило владельца: сигарета/алкоголь
                                                # на фото → 👎. Проверяем ТОЛЬКО будущие
                                                # лайки (остальные и так 👎) — лента не
                                                # замедляется.
                                                # Режим decide: лайк решает модель вкуса
                                                # по фото (текстовые запреты главнее).
                                                decision = await self._apply_photo_taste(
                                                    decision,
                                                    (
                                                        account_session or "",
                                                        chat_id,
                                                        task.message_id,
                                                    ),
                                                )
                                                if decision.decision == AIDecision.LIKE:
                                                    decision = await self._apply_photo_vice(
                                                        decision, msg,
                                                        card_key=(
                                                            account_session or "",
                                                            chat_id,
                                                            task.message_id,
                                                        ),
                                                    )
                                                if await self._db.has_auto_action_for_message(
                                                    chat_id, task.message_id
                                                ):
                                                    logger.info(
                                                        f"AutoAction: карточка {task.message_id} уже в журнале"
                                                    )
                                                else:
                                                    action = await self._auto_engine.maybe_act(
                                                        decision.decision, profile.id,
                                                        message_id=task.message_id,
                                                        reasons=decision.reasons,
                                                        card_text=text,
                                                        informative=decision.informative,
                                                    )
                                                    if action in ("LIKE", "DISLIKE"):
                                                        try:
                                                            await self._db.record_auto_action(
                                                                profile.id, action,
                                                                decision.decision.value,
                                                                chat_id,
                                                                task.message_id,
                                                            )
                                                        except Exception as e:
                                                            logger.error(
                                                                f"AutoAction: действие отправлено, но не записано: {e}"
                                                            )
                                            except Exception as e:
                                                logger.error(
                                                    f"AutoAction error: {e}"
                                                )
                                    except Exception as e:
                                        logger.error(f"Decision service error: {e}")
                                elif (
                                    str(filter_result.decision) != "PASS"
                                    and self._auto_engine.enabled
                                    and chat_id == self._dvinchik_chat_id
                                    and msg is not None
                                    and getattr(msg, "client", None)
                                    is self._auto_engine.client
                                ):
                                    # Фильтровые REJECT/REVIEW: AI не считается,
                                    # но Leo всё равно ждёт реакцию на показанную
                                    # анкету. Чтобы лента не замирала — 👎.
                                    try:
                                        if await self._db.has_auto_action_for_message(
                                            chat_id, task.message_id
                                        ):
                                            logger.info(
                                                f"AutoAction: карточка {task.message_id} "
                                                "уже в журнале"
                                            )
                                        else:
                                            action = (
                                                await self._auto_engine.maybe_act(
                                                    AIDecision.DISLIKE, profile.id,
                                                    message_id=task.message_id,
                                                    reasons=[r.value for r in filter_result.reasons],
                                                    card_text=text,
                                                )
                                            )
                                            if action in ("LIKE", "DISLIKE"):
                                                try:
                                                    await self._db.record_auto_action(
                                                        profile.id,
                                                        action,
                                                        filter_result.decision.value,
                                                        chat_id,
                                                        task.message_id,
                                                    )
                                                except Exception as e:
                                                    logger.error(
                                                        "AutoAction: действие "
                                                        f"отправлено, но не записано: {e}"
                                                    )
                                    except Exception as e:
                                        logger.error(f"AutoAction error: {e}")
                            except Exception as e:
                                logger.error(f"FilterService error: {e}")
                    except Exception as e:
                        logger.error(f"ProfileService error: {e}")
                        self._print_profile(parsed)
                else:
                    self._print_profile(parsed)

                if self._stats:
                    self._stats.record_profile(
                        filter_match=parsed.filter_result.value == "FILTER_MATCH"
                    )

            elif msg_type == MessageType.MATCH:
                match = self._parser.parse_match(text)
                if match:
                    self._print_match(match)
                    # Stage 8.5: взаимный лайк = «девушка ответила».
                    # Привязка к профилю по имени; дубликаты на карточку
                    # отбрасываются (INSERT OR IGNORE по chat_id+tm_id).
                    try:
                        await self._db.record_match_response(
                            match.name,
                            chat_id,
                            task.message_id,
                            telegram_username=match.telegram_username or "",
                        )
                    except Exception as e:
                        logger.error(f"record_match_response error: {e}")
                    await self._greet_match(task, msg, match)
                if self._stats:
                    self._stats.record_match()

            elif msg_type == MessageType.MEDIA_ONLY:
                await self._handle_media_only(
                    chat_id, task.message_id,
                    account_session=task.account_session or (
                        self._session_for_client(
                            getattr(msg, "client", None)
                            if msg is not None else None
                        )
                    ),
                )
                if self._stats:
                    self._stats.record_media_only()

            elif msg_type == MessageType.UNKNOWN:
                # Штатные служебные сообщения Leo (✨🔍 перед каждой анкетой,
                # «Отправь текст…» композера) — не проблема: в WARNING они
                # приходили на КАЖДОЙ анкете и топили настоящие ошибки в логе
                # и debug-консоли. Обработка (кнопки, цепочка «Берем)») та же.
                log_unknown = (
                    logger.debug if self._is_routine_leo_text(text)
                    else logger.warning
                )
                log_unknown(
                    f"Unknown message format: message_id={task.message_id}, "
                    f"preview={text[:100]}"
                )
                if self._stats:
                    self._stats.record_unknown()
                # Stage 7: Leo при исчерпании ленты шлёт промо с кнопкой
                # «Смотреть анкеты». Если это сообщение в чате Дайвинчика
                # на авто-аккаунте — нажимаем кнопку (продолжаем ленту).
                if (
                    self._auto_engine.enabled
                    and chat_id == self._dvinchik_chat_id
                    and msg is not None
                    and getattr(msg, "client", None) is self._auto_engine.client
                ):
                    try:
                        texts = self._extract_button_texts(msg)
                        if self._is_match_card(msg):
                            # «Ты понравился N девушке, показать её?» —
                            # останавливаем поиск, владелец решает сам.
                            await self._stop_on_match_card_if_needed()
                        elif any(VIEW_BUTTON_FRAGMENT in t for t in texts):
                            await self._press_view_button_if_needed()
                        elif (
                            len(texts) >= CAPTCHA_MIN_BUTTONS
                            and any(m in (text or "").lower() for m in CAPTCHA_MARKERS)
                        ):
                            # Капча/сделка/проверка — выученный ответ или вывод на сайт.
                            await self._handle_captcha(msg)
                        else:
                            # Рекламное/промо-сообщение без кнопки на самом себе:
                            # «🚀 Смотреть анкеты» может прийти на ОТДЕЛЬНОМ сообщении
                            # после рекламы. Сканируем последние сообщения чата —
                            # _press_view_button_if_needed уже идемпотентен (жмёт
                            # только если кнопка реально есть и ещё не нажата), а
                            # меню/Premium-промо такой кнопки не содержат → не
                            # зацикливаемся. Плюс Premium-промо с кнопкой «Пока
                            # без Premium» (после ❤️/👎) — нажимаем отказ, чтобы
                            # лента не встала. И главное меню ([«1 🚀», …]) —
                            # нажатие «1 🚀» продолжает ленту (см.
                            # _press_menu_view_if_needed).
                            history = await self._recent_history()
                            if not await self._stop_on_match_card_if_needed(history):
                                await self._continue_stream_if_needed(history)
                    except Exception as e:
                        logger.error(f"AutoAction: ошибка нажатия кнопки ленты: {e}")

            elif msg_type == MessageType.SERVICE:
                if self._stats:
                    self._stats.record_service()
                # Сетка: промо/меню/капчи Leo иногда классифицируются как
                # SERVICE (например «лайкают» в промо-тексте), но могут нести
                # кнопку «Смотреть анкеты». Если это чат Дайвинчика на авто-
                # аккаунте — пробуем продолжить ленту (идемпотентно).
                if (
                    self._auto_engine.enabled
                    and chat_id == self._dvinchik_chat_id
                    and msg is not None
                    and getattr(msg, "client", None) is self._auto_engine.client
                ):
                    try:
                        # Одна выборка истории на все проверки (FloodWait).
                        history = await self._recent_history()
                        if not await self._stop_on_match_card_if_needed(history):
                            await self._continue_stream_if_needed(history)
                    except Exception as e:
                        logger.error(
                            f"AutoAction: ошибка нажатия кнопки ленты "
                            f"(SERVICE): {e}"
                        )

            success = True
        except Exception as e:
            logger.error(f"Parser error (RAW already saved): {e}")
        finally:
            if success:
                # Помечаем RAW обработанным, чтобы startup-backlog recovery (W3)
                # не переобрабатывал завершённые сообщения.
                try:
                    await self._db.mark_raw_processed(task.raw_id)
                except Exception as e:
                    logger.error(f"Ошибка пометки RAW обработанным: {e}")
            else:
                # Ошибка pipeline: RAW остаётся processed_at=NULL, чтобы W3
                # повторил обработку после restart (at-least-once). Ошибка
                # одного RAW не роняет worker — исключение уже перехвачено.
                logger.warning(
                    f"Pipeline НЕ завершён для RAW id={task.raw_id}; "
                    f"processed_at не помечен — W3 повторит после restart."
                )

    async def _handle_media_only(
        self, chat_id: int, message_id: int, account_session: str = '',
    ) -> None:
        """Обработка photo-only сообщений: привязка к ПРЕДЫДУЩЕЙ анкете.

        Контекст (последнее PROFILE-сообщение ТОГО ЖЕ аккаунта) восстанавливается
        из БД (переживает restart/reconnect), затем — из in-memory кэша. Найденный
        профиль связывается с медиа-сообщением через profile_messages
        (UNIQUE(profile_id, telegram_message_id) исключает дубли media).
        """
        # Сначала пытаемся восстановить профиль из БД, затем in-memory.
        # Контекст СТРОГО на аккаунт: у аккаунтов свой чат с Leo в одном chat_id.
        prev = await self._db.get_chat_profile_context(chat_id, account_session)
        if prev is None:
            prev = self._pending_profiles.get((account_session, chat_id))

        if prev is None:
            logger.info(
                f"Media-only without profile context: msg={message_id}"
            )
            return

        if self._profile_service is not None:
            try:
                profile = await self._profile_service.find_profile_by_message(
                    chat_id, prev, account_session,
                )
                if profile is not None:
                    await self._profile_service.link_message_to_profile(
                        profile.id, message_id, chat_id,
                        account_session=account_session,
                    )
                    logger.info(
                        f"Media-only linked to profile: "
                        f"media_msg={message_id}, profile_id={profile.id}, "
                        f"profile_msg={prev}"
                    )
                else:
                    logger.warning(
                        f"Media-only: контекст указывает на несуществующий "
                        f"профиль (profile_msg={prev}); media не привязано"
                    )
            except Exception as e:
                logger.error(f"Media-only profile link error: {e}")
        else:
            logger.info(
                f"Media-only context (no service): "
                f"media_msg={message_id}, profile_msg={prev}"
            )

    def _serialize_entities(self, msg: object) -> str:
        """Сериализует entities сообщения в JSON."""
        if not msg.entities:
            return "[]"
        entities = []
        for e in msg.entities:
            entities.append({
                "type": type(e).__name__,
                "offset": e.offset,
                "length": e.length,
            })
        return json.dumps(entities, ensure_ascii=False)

    def _serialize_reply_markup(self, msg: object) -> str:
        """Сериализует кнопки сообщения (reply_markup) в JSON.

        Read-only разведка слоя действий: наличие inline-кнопок с callback_data
        на анкете говорит о том, как ставится LIKE (кнопка на сообщении).
        Никаких действий не вызывается. Ошибки/отсутствие разметки → "[]",
        чтобы RAW-save никогда не падал из-за незнакомой разметки.
        """
        markup = getattr(msg, "reply_markup", None)
        rows = getattr(markup, "rows", None) if markup is not None else None
        if not rows:
            return "[]"
        result: list[list[dict[str, str]]] = []
        for row in rows:
            buttons = getattr(row, "buttons", None) or []
            row_items = []
            for btn in buttons:
                item: dict[str, str] = {
                    "text": str(getattr(btn, "text", "") or ""),
                    "type": type(btn).__name__,
                }
                cb = getattr(btn, "callback_data", None)
                if cb:
                    if isinstance(cb, bytes):
                        item["callback_data"] = cb.decode("utf-8", errors="replace")
                    else:
                        item["callback_data"] = str(cb)
                url = getattr(btn, "url", None)
                if url:
                    item["url"] = str(url)
                row_items.append(item)
            if row_items:
                result.append(row_items)
        return json.dumps(result, ensure_ascii=False)

    def _print_message(
        self,
        chat_id: int,
        message_id: int,
        sender_name: str,
        sender_username: str,
        text: str,
        msg_date: str,
        media_type: str,
        msg_type: MessageType,
        buttons: str = "[]",
    ) -> None:
        """Красивый вывод нового сообщения в консоль."""
        is_dvinchik = (
            self._dvinchik_chat_id != 0
            and chat_id == self._dvinchik_chat_id
        )
        source = "[green]Дайвинчик[/green]" if is_dvinchik else f"chat={chat_id}"

        type_color = {
            MessageType.PROFILE: "yellow",
            MessageType.MATCH: "green",
            MessageType.MEDIA_ONLY: "cyan",
            MessageType.SERVICE: "dim",
            MessageType.UNKNOWN: "red",
        }.get(msg_type, "white")

        table = Table(show_header=False, border_style="dim", padding=(0, 1))
        table.add_column("Key", style="bold cyan", width=14)
        table.add_column("Value")
        table.add_row("Source", source)
        table.add_row("Message ID", str(message_id))
        table.add_row("Date", msg_date[:19])
        table.add_row("Type", f"[{type_color}]{msg_type.value}[/{type_color}]")
        table.add_row(
            "Sender",
            f"{sender_name} (@{sender_username})" if sender_username else sender_name,
        )
        if media_type:
            table.add_row("Media", media_type)
        if text:
            preview = text[:200] + ("..." if len(text) > 200 else "")
            table.add_row("Text", preview)
        if buttons and buttons != "[]":
            table.add_row("Buttons", self._render_buttons(buttons))

        console.print(Panel(table, title="[bold]NEW MESSAGE[/bold]", border_style="blue"))

    def _render_buttons(self, buttons_json: str) -> str:
        """Читаемое представление кнопок: 'текст(callback) | текст(url)'.

        Demo-формат для разведки: callback_data может быть любым байтовым
        payload, поэтому показываем как есть (полезно для реверса кнопки LIKE).
        """
        try:
            rows = json.loads(buttons_json)
        except (ValueError, TypeError):
            return buttons_json
        lines = []
        for row in rows or []:
            cells = []
            for btn in row or []:
                text = btn.get("text", "")
                cb = btn.get("callback_data")
                url = btn.get("url")
                if cb:
                    cells.append(f"{text} ({cb})")
                elif url:
                    cells.append(f"{text} [{url}]")
                else:
                    cells.append(text)
            lines.append(" | ".join(cells))
        return " / ".join(lines)

    def _print_profile_stored(self, profile: object, is_new: bool = True) -> None:
        """Красивый вывод сохранённого профиля (Stage 2 format)."""
        status_color = "green" if is_new else "cyan"
        status_label = "NEW" if is_new else "SEEN"
        title = f"[bold {status_color}]PROFILE[/]"

        table = Table(show_header=False, border_style="dim", padding=(0, 1))
        table.add_column("Key", style="bold yellow", width=18)
        table.add_column("Value")

        if hasattr(profile, "id") and profile.id:
            table.add_row("Profile ID", str(profile.id))
        if hasattr(profile, "name") and profile.name:
            table.add_row("Name", profile.name)
        if hasattr(profile, "age") and profile.age:
            table.add_row("Age", str(profile.age))
        if hasattr(profile, "normalized_city") and profile.normalized_city:
            table.add_row("City", profile.normalized_city)
        if hasattr(profile, "status"):
            table.add_row("Status", f"[{status_color}]{profile.status}[/{status_color}]")
        if hasattr(profile, "first_seen_at") and profile.first_seen_at:
            table.add_row("First seen", profile.first_seen_at[:19])
        if hasattr(profile, "last_seen_at") and profile.last_seen_at:
            table.add_row("Last seen", profile.last_seen_at[:19])
        if hasattr(profile, "source_message_id") and profile.source_message_id:
            table.add_row("Source message", str(profile.source_message_id))
        if hasattr(profile, "message_count") and profile.message_count:
            table.add_row("Messages", str(profile.message_count))

        console.print(Panel(table, title=title, border_style="yellow"))

    def _print_profile(self, profile: object) -> None:
        """Красивый вывод распарсенной анкеты (без сохранения)."""
        table = Table(show_header=False, border_style="dim", padding=(0, 1))
        table.add_column("Key", style="bold yellow", width=18)
        table.add_column("Value")

        if hasattr(profile, "name") and profile.name:
            table.add_row("Name", profile.name)
        if hasattr(profile, "age") and profile.age:
            table.add_row("Age", str(profile.age))
        if hasattr(profile, "raw_city") and profile.raw_city:
            table.add_row("Raw City", profile.raw_city)
        if hasattr(profile, "normalized_city") and profile.normalized_city:
            table.add_row("Normalized City", profile.normalized_city)
        if hasattr(profile, "filter_result"):
            color = "green" if profile.filter_result == "FILTER_MATCH" else "red"
            table.add_row("Filter", f"[{color}]{profile.filter_result}[/{color}]")
        if hasattr(profile, "description") and profile.description:
            desc = profile.description[:300]
            table.add_row("Description", desc)

        console.print(Panel(table, title="[bold yellow]PROFILE[/bold yellow]", border_style="yellow"))

    def _print_match(self, match: object) -> None:
        """Красивый вывод сообщения о матче."""
        table = Table(show_header=False, border_style="dim", padding=(0, 1))
        table.add_column("Key", style="bold green", width=18)
        table.add_column("Value")

        if hasattr(match, "name"):
            table.add_row("Name", match.name)
        if hasattr(match, "telegram_username"):
            table.add_row("Telegram", f"@{match.telegram_username}")
        if hasattr(match, "telegram_url"):
            table.add_row("URL", match.telegram_url)
        table.add_row("Action", "[yellow]NONE — OBSERVE MODE[/yellow]")

        console.print(Panel(table, title="[bold green]MATCH DETECTED[/bold green]", border_style="green"))

    def _print_filter_result(self, profile: object, result: object) -> None:
        """Красивый вывод результата фильтрации."""
        if result is None:
            return

        decision = result.decision if hasattr(result, "decision") else "UNKNOWN"
        reasons = result.reasons if hasattr(result, "reasons") else []

        color_map = {
            "PASS": "green",
            "REJECT": "red",
            "REVIEW": "yellow",
        }
        color = color_map.get(str(decision), "white")

        reason_icons = {
            "AGE_OK": "[green]✓[/green]",
            "CITY_OK": "[green]✓[/green]",
            "AGE_OUT_OF_RANGE": "[red]✗[/red]",
            "CITY_OUT_OF_RANGE": "[red]✗[/red]",
            "AGE_UNKNOWN": "[yellow]?[/yellow]",
            "CITY_UNKNOWN": "[yellow]?[/yellow]",
            "INSUFFICIENT_DATA": "[yellow]?[/yellow]",
        }

        table = Table(show_header=False, border_style="dim", padding=(0, 1))
        table.add_column("Key", style="bold cyan", width=18)
        table.add_column("Value")

        if hasattr(profile, "id"):
            table.add_row("Profile", f"#{profile.id}")
        if hasattr(profile, "name"):
            table.add_row("Name", profile.name)
        if hasattr(profile, "age") and profile.age:
            table.add_row("Age", str(profile.age))
        if hasattr(profile, "normalized_city") and profile.normalized_city:
            table.add_row("City", profile.normalized_city)

        table.add_row("Decision", f"[{color}]{decision}[/{color}]")

        reasons_text = []
        for r in reasons:
            icon = reason_icons.get(str(r), "?")
            reasons_text.append(f"  {icon} {r}")
        if reasons_text:
            table.add_row("Reasons", "\n".join(reasons_text))

        console.print(Panel(
            table,
            title=f"[bold {color}]FILTER RESULT[/]",
            border_style=color,
        ))

    def _print_ai_decision(self, profile: object, decision: object) -> None:
        """Красивый вывод решения Decision Engine (детерминированный scoring)."""
        color_map = {
            "LIKE": "green",
            "REVIEW": "yellow",
            "DISLIKE": "red",
        }
        color = color_map.get(decision.decision.value, "white")

        table = Table(show_header=False, border_style="dim", padding=(0, 1))
        table.add_column("Key", style="bold magenta", width=18)
        table.add_column("Value")

        if hasattr(profile, "id"):
            table.add_row("Profile", f"#{profile.id}")
        if hasattr(profile, "name"):
            table.add_row("Name", profile.name)

        table.add_row("Filter", "PASS")
        table.add_row("Score", f"{decision.combined_score:.2f}")
        table.add_row(
            "Decision",
            f"[bold {color}]{decision.decision.value}[/bold {color}]",
        )
        table.add_row(
            "Mode",
            self._mode_label,
        )

        if decision.reasons:
            reasons_text = "\n".join(f"  • {r}" for r in decision.reasons)
            table.add_row("Reasons", reasons_text)
        if decision.scoring_version:
            table.add_row("Version", decision.scoring_version)

        console.print(Panel(
            table,
            title=f"[bold {color}]DECISION[/]",
            border_style=color,
        ))

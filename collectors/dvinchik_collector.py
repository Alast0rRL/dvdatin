# Коллектор сообщений Telegram: перехват, сохранение RAW, классификация.
# Сохраняет RAW ПЕРВЫМ — парсер не может помешать сохранению.

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from rich.panel import Panel
from rich.table import Table
from telethon import events

from collectors.account_engines import AccountEnginesMixin
from collectors.auto_action import AutoActionEngine
from collectors.console_output import ConsoleOutputMixin
from collectors.dedup import Dedup
from collectors.dialog_logger import DialogLogger
from collectors.dvinchik_parser import DvinchikParser
from collectors.history_sync import HistorySyncMixin
from collectors.leo_navigation import LeoNavigationMixin
from collectors.match_dialogs import MatchDialogsMixin
from collectors.match_greeter import MatchGreeter
from collectors.chat_folders import ChatFolders
from collectors.match_intro import MatchIntro
from collectors.match_script import MatchScript
from collectors.outgoing import OutgoingMixin
from collectors.photo_steps import PhotoStepsMixin
from collectors.raw_worker import DvinchikRawWorker, RawTask
from collectors.stream_control import StreamControlMixin
from core.types import Mode
from models.decision import AIDecision
from models.raw import MessageType

# Маркеры Leo живут в collectors/leo_markers.py; имена реэкспортируются
# отсюда для прежних импортов (тесты, внешний код).
from collectors.leo_markers import (
    console,
    _MEDIA_TYPE_MAP,
    VIEW_BUTTON_FRAGMENT,
    MENU_VIEW_FRAGMENT,
    STREAM_ERROR_STATE_KEY,
    STREAM_ERROR_NOTIFY_COOLDOWN_SEC,
    STREAM_ERROR_SNAPSHOT_LIMIT,
    MATCH_STOP_STATE_KEY,
    MATCH_STOP_PREV_MODE_KEY,
    ROUTINE_LEO_TEXTS,
    HISTORY_SYNC_LIMIT,
    HISTORY_SYNC_INTERVAL_SEC,
    HISTORY_SYNC_MIN_AGE_SEC,
    PREMIUM_DECLINE_FRAGMENT,
    CITY_CHECK_FRAGMENTS,
    CITY_CHECK_ANSWER,
    MATCH_CARD_FRAGMENT,
    MATCH_SHOW_FRAGMENT,
    MATCH_NOTICE_FRAGMENTS,
    MATCH_SHOW_SKIP_FRAGMENT,
    MATCH_SHOW_ALT_FRAGMENT,
    MATCH_SHOW_SKIP_ALT_FRAGMENT,
    MANUAL_MESSAGE_EXCLUDE,
    MANUAL_MESSAGE_EXCLUDE_FRAGMENTS,
    MANUAL_MESSAGE_NUMBERED_RE,
    is_manual_message_excluded,
    CAPTCHA_MIN_BUTTONS,
    CAPTCHA_MARKERS,
    captcha_signature,
    _detect_media_type,
    _CURRENT_SESSION,
)

from app.config import FriendsConfig

if TYPE_CHECKING:
    from telethon import TelegramClient

    from app.config import AppConfig
    from database.database import Database
    from services.filter_service import FilterService
    from services.profile_service import ProfileService


__all__ = [
    "DvinchikCollector",
    "console",
    "_MEDIA_TYPE_MAP",
    "VIEW_BUTTON_FRAGMENT",
    "MENU_VIEW_FRAGMENT",
    "STREAM_ERROR_STATE_KEY",
    "STREAM_ERROR_NOTIFY_COOLDOWN_SEC",
    "STREAM_ERROR_SNAPSHOT_LIMIT",
    "MATCH_STOP_STATE_KEY",
    "MATCH_STOP_PREV_MODE_KEY",
    "ROUTINE_LEO_TEXTS",
    "HISTORY_SYNC_LIMIT",
    "HISTORY_SYNC_INTERVAL_SEC",
    "HISTORY_SYNC_MIN_AGE_SEC",
    "PREMIUM_DECLINE_FRAGMENT",
    "CITY_CHECK_FRAGMENTS",
    "CITY_CHECK_ANSWER",
    "MATCH_CARD_FRAGMENT",
    "MATCH_SHOW_FRAGMENT",
    "MATCH_NOTICE_FRAGMENTS",
    "MATCH_SHOW_SKIP_FRAGMENT",
    "MATCH_SHOW_ALT_FRAGMENT",
    "MATCH_SHOW_SKIP_ALT_FRAGMENT",
    "MANUAL_MESSAGE_EXCLUDE",
    "MANUAL_MESSAGE_EXCLUDE_FRAGMENTS",
    "MANUAL_MESSAGE_NUMBERED_RE",
    "is_manual_message_excluded",
    "CAPTCHA_MIN_BUTTONS",
    "CAPTCHA_MARKERS",
    "captcha_signature",
    "_detect_media_type",
    "_CURRENT_SESSION",
]


class DvinchikCollector(
    AccountEnginesMixin,
    StreamControlMixin,
    LeoNavigationMixin,
    HistorySyncMixin,
    MatchDialogsMixin,
    PhotoStepsMixin,
    OutgoingMixin,
    ConsoleOutputMixin,
):
    """Перехватывает входящие сообщения и сохраняет в БД.

    Здесь — конструктор, регистрация хендлеров, входящие (RAW-first) и
    pipeline ``_process_message``. Остальное — в миксинах (у всех общий
    ``self``, методы вызываются и патчатся как раньше):
    ``AccountEnginesMixin`` (движки и переключатели аккаунтов),
    ``StreamControlMixin`` (старт ленты, стоп на лайке, сбой автопотока),
    ``LeoNavigationMixin`` (кнопки Leo, капчи), ``HistorySyncMixin``
    (сверка истории, backlog), ``MatchDialogsMixin`` (привет, автопереписка,
    журнал лички), ``PhotoStepsMixin`` (фото-модели), ``OutgoingMixin``
    (наши исходящие), ``ConsoleOutputMixin`` (вывод в консоль).
    """

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
        friends_filter_service: FilterService | None = None,
        friends_decision_service: object | None = None,
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
        # Карточки, на которые реакция уже взята в работу (за аккаунт):
        # два прохода по одной карточке (живой хендлер + «активная анкета»
        # после капчи) не должны слать два 👎 — второй уходит на следующую.
        self._reacted_cards: Dedup = Dedup()
        self._parser = DvinchikParser(config.filters)
        # Стикер-приветствие девушке после взаимного лайка.
        self._match_greeter = MatchGreeter(
            config.auto_actions.match_greeting, db
        )
        self._dvinchik_chat_id = config.dvinchik.chat_id
        # Автопереписка (AUTO): её ответ на привет → её анкета + закреп.
        self._match_intro = MatchIntro(
            config.auto_actions.match_intro, db, config.dvinchik.chat_id,
            # Сроки и текст «АЛООО ПРОСЫПАЕМСЯ» после стикера — из сценария.
            script_config=config.auto_actions.match_script,
        )
        # Личка перенаправлена — копию анкеты качаем клиентом аккаунта карточки.
        self._match_intro.client_for = (
            lambda session: self._clients_for_session(session)[0]
        )
        # Чаты девушек — в архиве Telegram, общение — через папки New /
        # Active / 50/50 / Auto (владелец, 2026-11-04).
        self._chat_folders = ChatFolders(
            db, config.auto_actions.match_intro.folders,
            archive=bool(config.auto_actions.match_intro.archive_bot_chats),
            delete_silent=bool(config.auto_actions.match_script.delete_silent_chat),
        )
        # После анкеты — сценарий знакомства по схеме владельца (AUTO).
        self._match_script = MatchScript(config.auto_actions.match_script, db)
        # Журнал переписок с девушками-матчами (данные для анализа общения).
        dialog_cfg = config.auto_actions.dialog_log
        self._dialog_log = DialogLogger(db, enabled=dialog_cfg.enabled)
        self._dialog_sync = bool(dialog_cfg.sync)
        if self._dialog_log.enabled:
            self._match_greeter.on_sent = self._dialog_log.log_sent
            self._match_intro.on_sent = self._dialog_log.log_sent
            self._match_script.on_sent = self._dialog_log.log_sent
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
        self._mode_before_match_stop: dict[str, Mode] = {}
        # Карточки/уведомления о лайке, по которым владелец нажал «Продолжить»
        # ((session, msg_id)): у уведомления без кнопок нечего нажать, и без
        # этой отметки поиск тут же остановился бы на нём снова.
        self._match_ack: set[tuple[str, int]] = set()
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
        # Режим «кенты» (config.friends): у аккаунта-кентов свои фильтр и
        # правила (парни-друзья), без фото-модели и «девичьих» признаков.
        self._friends_filter_service = friends_filter_service
        self._friends_decision_service = friends_decision_service
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
        # Stage 7: авто-действия. Свой AutoActionEngine на КАЖДЫЙ вошедший
        # аккаунт (свой режим, rate-limit, гейт карточки, лимит ❤️): оба
        # аккаунта могут листать ленту одновременно. Основной аккаунт
        # (auto_actions.account_session) — лента сайта, ручные кнопки, ControlBot.
        self._config_path = config_path
        self._primary_session: str = config.auto_actions.account_session
        self._engines: dict[str, AutoActionEngine] = {}
        for session_name in self._client_sessions:
            if session_name not in self._engines:
                self._engines[session_name] = self._build_auto_engine(session_name)
        if self._primary_session not in self._engines:
            # Основной аккаунт не вошёл/не задан — движок без клиента (выключен).
            self._engines[self._primary_session] = self._build_auto_engine(
                self._primary_session
            )
        # Переключатель «Авто» (автопереписка в личке) каждого аккаунта.
        # «Скроллинг» живёт в режиме движка ленты (OBSERVE = выключен).
        self._auto_flags: dict[str, bool] = {
            name: config.controls_for(name).auto for name in self._engines
        }
        # Worker потребляет RawQueue и выполняет pipeline вне Telegram-хендлера.
        # Если worker не задан (тесты/отладка) — обработка идёт синхронно в
        # хендлере (fallback). В проде всегда привязан worker.
        self._worker = worker

    def _is_friends_session(self, session: str | None) -> bool:
        """Аккаунт в режиме «кенты» (``config.friends``)."""
        friends_cfg = getattr(self._config, "friends", None)
        # isinstance: в тестах config бывает MagicMock — он не «кенты».
        return isinstance(friends_cfg, FriendsConfig) and friends_cfg.is_friends(
            session
        )

    def _services_for(self, friends: bool) -> tuple[object | None, object | None]:
        """Фильтр и сервис решений: свои у «кентов», общие у девушек."""
        if friends and self._friends_filter_service is not None:
            return self._friends_filter_service, self._friends_decision_service
        return self._filter_service, self._decision_service

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
            # Личные сообщения девушек после взаимного лайка (автопереписка).
            client.add_event_handler(
                self._handle_private_message,
                events.NewMessage(incoming=True, func=lambda e: e.is_private),
            )
        logger.info(
            f"Collector registered ({len(self._clients)} account(s)): "
            f"listening for new messages + outgoing + callbacks"
        )

    async def _handle_new_message(self, event: events.NewMessage.Event) -> None:
        """Обработчик: сохраняет RAW потом парсит."""
        self._bind_client(getattr(event, "client", None))
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
                        if (
                            self._auto_show_likes()
                            and self._match_show_button(msg) is not None
                        ):
                            # «Показать» нажмёт pipeline (_on_unknown). Карточка
                            # — новая «активная»: queued 👎 на прошлую анкету
                            # ушёл бы ответом на неё.
                            self._auto_engine.note_card(chat_id, msg.id)
                        elif self._is_liked_notice(msg):
                            # «…Заканчивай с вопросом выше» — анкета выше ещё
                            # ждёт реакции, Leo покажет лайкнувшую после неё.
                            pass
                        else:
                            # Стоп СРАЗУ, до очереди: pipeline может отставать,
                            # и queued 👎 на прошлую анкету ушёл бы после
                            # карточки (лента листалась дальше).
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

    # ------------------------------------------------------------------
    # Callback queries (inline-кнопки — разведка механики LIKE)
    # ------------------------------------------------------------------

    async def _handle_callback_query(self, event: events.CallbackQuery.Event) -> None:
        """Логирование callback queries для разведки кнопок LIKE.

        Read-only: данные neither сохраняются в БД, ни вызывают действия.
        Если лайк ставится через inline-кнопку — callback_data будет виден
        в логе.
        """
        self._bind_client(getattr(event, "client", None))
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

    # ── Фоновый досчёт старых анкет (вердикт + отпечаток фото) ──────────

    #: Пауза, когда досчитывать нечего (новые анкеты считает живая тень).
    PHOTO_BACKFILL_IDLE_SEC = 600.0

    #: Сколько готовых результатов карточек держать в памяти.
    PHOTO_RESULTS_KEEP = 64

    #: Сколько ждать фонового расчёта этой карточки, прежде чем считать самим.
    PHOTO_SHADOW_WAIT_SEC = 15.0

    async def _process_message(self, task: RawTask) -> None:
        """Выполняет pipeline: parse → filter → решение → действие.

        Вызывается worker'ом (в проде) или напрямую из хендлера (fallback).
        RAW к этому моменту уже сохранён — ошибки pipeline не теряют сырьё.
        Каждый тип сообщения разбирает свой шаг ``_on_*``.
        """
        # Обработка — от имени аккаунта-получателя (его движок и режим).
        _CURRENT_SESSION.set(self._task_session(task) or None)
        logger.info(
            f"New Telegram message: chat_id={task.chat_id}, "
            f"sender_id={task.sender_id}, message_id={task.message_id}, "
            f"media={task.media_type}"
        )

        success = False
        try:
            msg_type = self._parser.classify(
                task.text, has_media=task.media_type != "",
            )
            msg_type = await self._check_like_rejection(task, msg_type)
            logger.info(f"Message classified: type={msg_type.value}")
            self._print_message(
                chat_id=task.chat_id,
                message_id=task.message_id,
                sender_name=task.sender_name,
                sender_username=task.sender_username,
                text=task.text,
                msg_date=task.msg_date,
                media_type=task.media_type,
                msg_type=msg_type,
                buttons=task.reply_markup_json,
            )

            # Stage 8 (§30): продвижение цепочки «Берем)» (LIKE_AND_MESSAGE)
            # по ответам Leo — только НЕ-профильные сообщения авто-аккаунта.
            # step_pending сам проверяет, есть ли активная цепочка.
            if msg_type != MessageType.PROFILE and self._is_auto_receiver(
                task.msg, task.chat_id,
            ):
                try:
                    await self._auto_engine.step_pending(task.text, task.chat_id)
                except Exception as e:
                    logger.error(f"AutoAction: ошибка продвижения «Берем)»: {e}")

            if msg_type == MessageType.PROFILE:
                await self._on_profile(task)
            elif msg_type == MessageType.MATCH:
                await self._on_match(task)
            elif msg_type == MessageType.MEDIA_ONLY:
                await self._handle_media_only(
                    task.chat_id, task.message_id,
                    account_session=self._task_session(task),
                )
                if self._stats:
                    self._stats.record_media_only()
            elif msg_type == MessageType.UNKNOWN:
                await self._on_unknown(task)
            elif msg_type == MessageType.SERVICE:
                await self._on_service(task)

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
                # повторил обработку после restart (at-least-once).
                logger.warning(
                    f"Pipeline НЕ завершён для RAW id={task.raw_id}; "
                    f"processed_at не помечен — W3 повторит после restart."
                )

    # ── Шаги pipeline ─────────────────────────────────────────────────

    def _task_session(self, task: RawTask) -> str:
        """Аккаунт-получатель сообщения (из задачи или по клиенту сообщения)."""
        if task.account_session:
            return task.account_session
        client = getattr(task.msg, "client", None) if task.msg is not None else None
        return self._session_for_client(client)

    def _is_auto_receiver(self, msg: object, chat_id: int | None = None) -> bool:
        """Сообщение пришло на аккаунт активного движка, и тот включён.

        ``chat_id`` задан — ещё и сообщение из чата Leo. Действуем в Telegram
        только при True: иначе реакция ушла бы не с того аккаунта.
        """
        if not self._auto_engine.enabled or msg is None:
            return False
        if chat_id is not None and chat_id != self._dvinchik_chat_id:
            return False
        return getattr(msg, "client", None) is self._auto_engine.client

    async def _check_like_rejection(
        self, task: RawTask, msg_type: MessageType,
    ) -> MessageType:
        """Stage 8.5.1: Leo отклонил лайк из-за дневного лимита.

        «Слишком много ❤️ за сегодня…» — лайк не дошёл: помечаем последний
        лайк чата rejected (не портит конверсию), движок перестаёт слать ❤️ до
        сброса. Лимит — свойство аккаунта: отказ на ДРУГОМ аккаунте (ручные
        лайки владельца) лимит исполнителя не трогает. Само сообщение —
        SERVICE, чтобы не давать ложный «Unknown message format».
        """
        if (
            msg_type in (MessageType.PROFILE, MessageType.MEDIA_ONLY)
            or task.chat_id != self._dvinchik_chat_id
        ):
            return msg_type
        try:
            if not self._parser.is_like_rejection(task.text):
                return msg_type
            await self._db.mark_like_rejected(task.chat_id)
            logger.info(f"Leo: лайк отклонён (лимит). message_id={task.message_id}")
            engine = self._auto_engine
            engine_session = engine.account_session if engine is not None else ""
            if (
                engine is not None
                and task.account_session
                and engine_session
                and task.account_session != engine_session
            ):
                logger.info(
                    f"Leo: лимит ❤️ на {task.account_session} — "
                    f"исполнитель {engine_session} не затронут"
                )
            elif engine is not None:
                try:
                    await engine.handle_like_rejection(task.chat_id)
                    self.stop_scroll_on_like_limit(engine_session or None)
                except Exception as e:
                    logger.error(f"AutoAction: обработка лимита лайков: {e}")
            return MessageType.SERVICE
        except Exception as e:
            logger.warning(f"like_rejection hook error: {e}")
            return msg_type

    async def _on_profile(self, task: RawTask) -> None:
        """Анкета: сохранить → фильтр → решение → фото-шаги → реакция."""
        chat_id = task.chat_id
        parsed = self._parser.parse_profile(
            task.text,
            source_message_id=task.message_id,
            source_chat_id=chat_id,
        )
        if self._stats:
            self._stats.record_profile(
                filter_match=parsed.filter_result.value == "FILTER_MATCH"
            )
        if not self._profile_service:
            self._print_profile(parsed)
            return

        account_session = self._task_session(task)
        friends = self._is_friends_session(account_session)
        try:
            profile = await self._profile_service.upsert_profile(
                parsed, account_session=account_session,
                audience="friends" if friends else "",
            )
        except Exception as e:
            logger.error(f"ProfileService error: {e}")
            self._print_profile(parsed)
            return

        # Анкета — «контекст» для следующих MEDIA_ONLY ТОГО ЖЕ аккаунта:
        # в памяти и в БД (переживает restart/reconnect, W2).
        self._pending_profiles[(account_session, chat_id)] = task.message_id
        try:
            await self._db.set_chat_profile_context(
                chat_id, task.message_id, account_session
            )
        except Exception as e:
            logger.error(f"Ошибка сохранения контекста чата: {e}")

        self._print_profile_stored(profile, is_new=profile.status == "NEW")
        card_key = (account_session or "", chat_id, task.message_id)
        # Теневой вердикт модели вкуса по фото (фон, не ждём).
        # Модель — про девушек: анкеты кентов её не касаются.
        if not friends:
            self._schedule_photo_shadow(
                task.msg, profile.id, account_session, chat_id, task.message_id,
            )

        filter_service, decision_service = self._services_for(friends)
        if not filter_service:
            return
        try:
            filter_result = await filter_service.evaluate(profile)
            self._print_filter_result(profile, filter_result)
        except Exception as e:
            logger.error(f"FilterService error: {e}")
            return

        if not decision_service:
            # Без сервиса решений фильтровые REJECT/REVIEW всё равно требуют
            # реакции: Leo ждёт её на показанную анкету — 👎, лента не встаёт.
            if (
                str(filter_result.decision) != "PASS"
                and self._is_auto_receiver(task.msg, chat_id)
            ):
                await self._react_to_card(
                    task, profile.id, AIDecision.DISLIKE,
                    reasons=[r.value for r in filter_result.reasons],
                    logged_decision=filter_result.decision.value,
                )
            return

        # Детерминированный скоринг (Stage 8): только текстовые правила.
        try:
            decision = await decision_service.evaluate(
                profile.id, filter_result=filter_result,
            )
            self._print_ai_decision(profile, decision)
        except Exception as e:
            logger.error(f"Decision service error: {e}")
            return
        # Stage 7: авто-действие — только если анкета пришла на авто-аккаунт.
        if decision is None or not self._is_auto_receiver(task.msg):
            return
        try:
            decision = await self._refine_decision(
                decision, profile.id, task.msg, card_key, friends,
            )
        except Exception as e:
            logger.error(f"AutoAction error: {e}")
            return
        await self._react_to_card(
            task, profile.id, decision.decision,
            reasons=decision.reasons,
            logged_decision=decision.decision.value,
            informative=decision.informative,
        )

    async def _refine_decision(
        self,
        decision: object,
        profile_id: int,
        msg: object,
        card_key: tuple[str, int, int],
        friends: bool,
    ) -> object:
        """Шаги после текста: фото-вкус → «уже был матч» → сигарета/алкоголь.

        Порядок важен: текстовые запреты главнее фото (photo_taste_applies),
        фото-порок проверяем только у будущих лайков. Кентам фото-шаги не нужны.
        """
        if not friends:
            decision = await self._apply_photo_taste(decision, card_key)
        # С кем уже был взаимный лайк — второй раз не лайкаем.
        if decision.decision == AIDecision.LIKE:
            decision = await self._apply_already_matched(decision, profile_id)
        if decision.decision == AIDecision.LIKE and not friends:
            decision = await self._apply_photo_vice(
                decision, msg, card_key=card_key,
            )
        return decision

    async def _react_to_card(
        self,
        task: RawTask,
        profile_id: int,
        decision: AIDecision,
        *,
        reasons: list[str],
        logged_decision: str,
        informative: bool | None = None,
    ) -> None:
        """Одна реакция на карточку: проверка журнала → действие → запись.

        Карточка «занимается» синхронно, до первого ``await``: журнал
        пишется только после отправки (а перед ней — фото-шаг на секунды),
        поэтому параллельный проход по той же карточке проверку журнала
        проходит. Прод 2026-11-02: живой хендлер и «активная анкета» после
        капчи послали два 👎, второй ушёл на следующую анкету (UNIQUE).
        """
        claim = (task.account_session or "", task.chat_id, task.message_id)
        if self._reacted_cards.is_known(claim):
            logger.info(
                f"AutoAction: карточка {task.message_id} уже обрабатывается"
            )
            return
        self._reacted_cards.mark(claim)
        try:
            if await self._db.has_reaction_for_message(
                task.chat_id, task.message_id
            ):
                logger.info(
                    f"AutoAction: карточка {task.message_id} уже в журнале"
                )
                return
            kwargs: dict[str, object] = {
                "message_id": task.message_id,
                "reasons": reasons,
                "card_text": task.text,
            }
            if informative is not None:
                kwargs["informative"] = informative
            action = await self._auto_engine.maybe_act(
                decision, profile_id, **kwargs,
            )
            if action == "LIKE_LIMIT":
                # Лимит уже был (например, до рестарта) — лента встала бы.
                self.stop_scroll_on_like_limit(
                    self._auto_engine.account_session or None
                )
            if action not in ("LIKE", "DISLIKE"):
                return
            try:
                await self._db.record_auto_action(
                    profile_id, action, logged_decision,
                    task.chat_id, task.message_id,
                )
            except Exception as e:
                logger.error(
                    f"AutoAction: действие отправлено, но не записано: {e}"
                )
        except Exception as e:
            logger.error(f"AutoAction error: {e}")

    async def _on_match(self, task: RawTask) -> None:
        """Взаимный лайк: журнал ответа (Stage 8.5) + привет девушке."""
        match = self._parser.parse_match(task.text)
        if match:
            self._print_match(match)
            # Дубликаты на карточку отбрасываются (INSERT OR IGNORE).
            try:
                await self._db.record_match_response(
                    match.name,
                    task.chat_id,
                    task.message_id,
                    telegram_username=match.telegram_username or "",
                )
            except Exception as e:
                logger.error(f"record_match_response error: {e}")
            await self._greet_match(task, task.msg, match)
        if self._stats:
            self._stats.record_match()

    async def _on_unknown(self, task: RawTask) -> None:
        """Незнакомое сообщение Leo: промо, меню, капча, карточка матча."""
        msg, text = task.msg, task.text
        # Штатные служебные тексты Leo (✨🔍 перед анкетой и т.п.) — в DEBUG,
        # чтобы не топить настоящие ошибки в логе.
        log_unknown = (
            logger.debug if self._is_routine_leo_text(text) else logger.warning
        )
        log_unknown(
            f"Unknown message format: message_id={task.message_id}, "
            f"preview={text[:100]}"
        )
        if self._stats:
            self._stats.record_unknown()
        if not self._is_auto_receiver(msg, task.chat_id):
            if self._is_liked_receiver(msg, task.chat_id) and self._is_match_card(msg):
                # «Скроллинг» выкл, «Авто» вкл — лайкнувших смотрим сами,
                # ленту не трогаем (владелец, 2026-11-04).
                await self._show_liked_if_needed()
            return
        try:
            texts = self._extract_button_texts(msg)
            if self._is_match_card(msg):
                # «Ты понравился N девушке, показать её?» — «Показать» сами
                # (auto_show_likes) или стоп до «Продолжить».
                await self._on_match_card()
            elif any(VIEW_BUTTON_FRAGMENT in t for t in texts):
                await self._press_view_button_if_needed()
            elif (
                len(texts) >= CAPTCHA_MIN_BUTTONS
                and any(m in (text or "").lower() for m in CAPTCHA_MARKERS)
            ):
                # Капча/сделка/проверка — выученный ответ или на сайт.
                await self._handle_captcha(msg)
            elif self._is_unknown_keyboard(msg):
                # Незнакомый вопрос с кнопками без маркеров — тоже капча.
                await self._handle_captcha(msg, any_keyboard=True)
            else:
                # Промо без кнопки: «Смотреть анкеты» / «Пока без Premium» /
                # меню «1 🚀» могут быть на соседнем сообщении — смотрим
                # историю (нажатия идемпотентны, не зацикливаемся).
                await self._continue_from_history()
        except Exception as e:
            logger.error(f"AutoAction: ошибка нажатия кнопки ленты: {e}")

    async def _on_service(self, task: RawTask) -> None:
        """Служебное сообщение Leo: капча под видом SERVICE или продолжение ленты."""
        if self._stats:
            self._stats.record_service()
        msg = task.msg
        if not self._is_auto_receiver(msg, task.chat_id):
            return
        try:
            # Капча под видом SERVICE (прод 2026-11-01: «Найди свой денежный
            # мэтч ❤️» [Подробнее | скип] — бот молча встал).
            if self._is_captcha_message(msg):
                await self._handle_captcha(msg)
            elif self._is_unknown_keyboard(msg):
                await self._handle_captcha(msg, any_keyboard=True)
            else:
                await self._continue_from_history()
        except Exception as e:
            logger.error(f"AutoAction: ошибка нажатия кнопки ленты (SERVICE): {e}")

    async def _continue_from_history(self) -> None:
        """Одна выборка истории на все проверки: карточка лайка или дальше лента."""
        history = await self._recent_history()
        if self._auto_show_likes():
            if not await self._show_liked_if_needed(history):
                await self._continue_stream_if_needed(history)
        elif not await self._stop_on_match_card_if_needed(history):
            await self._continue_stream_if_needed(history)

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

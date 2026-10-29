# Коллектор сообщений Telegram: перехват, сохранение RAW, классификация.
# Сохраняет RAW ПЕРВЫМ — парсер не может помешать сохранению.

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from loguru import logger
from rich.panel import Panel
from rich.table import Table
from telethon import events

from collectors.auto_action import (
    AutoActionEngine,
    DISLIKE_TEXT,
    LIKE_TEXT,
)
from collectors.leo_navigation import LeoNavigationMixin
from collectors.stream_control import StreamControlMixin
from collectors.history_sync import HistorySyncMixin
from collectors.match_dialogs import MatchDialogsMixin
from collectors.photo_steps import PhotoStepsMixin
from collectors.account_engines import AccountEnginesMixin
from collectors.outgoing import OutgoingMixin
from collectors.dedup import Dedup
from collectors.dvinchik_parser import DvinchikParser
from collectors.match_greeter import MatchGreeter
from collectors.dialog_logger import DialogLogger
from collectors.match_intro import INTRO_SENT, MatchIntro
from collectors.raw_worker import DvinchikRawWorker, RawTask
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
from app.config import AccountControls
from core.types import Mode
from models.decision import AIDecision
from models.raw import MessageType, ParsedMatch

if TYPE_CHECKING:
    from telethon import TelegramClient

    from app.config import AppConfig
    from database.database import Database
    from services.filter_service import FilterService
    from services.profile_service import ProfileService


class DvinchikCollector(LeoNavigationMixin, StreamControlMixin, HistorySyncMixin, MatchDialogsMixin, PhotoStepsMixin, AccountEnginesMixin, OutgoingMixin):
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
        # Автопереписка (AUTO): её ответ на привет → её анкета + закреп.
        self._match_intro = MatchIntro(
            config.auto_actions.match_intro, db, config.dvinchik.chat_id,
        )
        # Журнал переписок с девушками-матчами (данные для анализа общения).
        dialog_cfg = config.auto_actions.dialog_log
        self._dialog_log = DialogLogger(db, enabled=dialog_cfg.enabled)
        self._dialog_sync = bool(dialog_cfg.sync)
        if self._dialog_log.enabled:
            self._match_greeter.on_sent = self._dialog_log.log_sent
            self._match_intro.on_sent = self._dialog_log.log_sent
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
        """Выполняет pipeline: parse → filter → AI → decision.

        Вызывается worker'ом (в проде) или напрямую из хендлера (fallback).
        RAW к этому моменту уже сохранён — ошибки pipeline не теряют сырьё.
        """
        # Обработка — от имени аккаунта-получателя (его движок и режим).
        _CURRENT_SESSION.set(
            task.account_session
            or self._session_for_client(getattr(task.msg, "client", None))
            or None
        )
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

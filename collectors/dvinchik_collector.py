# Коллектор сообщений Telegram: перехват, сохранение RAW, классификация.
# Сохраняет RAW ПЕРВЫМ — парсер не может помешать сохранению.

from __future__ import annotations

import asyncio
import json
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
from collectors.raw_worker import DvinchikRawWorker, RawTask
from core.types import Mode
from models.decision import AIDecision
from models.raw import MessageType

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

#: На любые "проверки"/капчи Leo (сделка, подписка, подтверждение и т.п.)
#: авто-аккаунт всегда нажимает ПОСЛЕДНЮЮ reply-кнопку — это сбрасывает
#: диалог и продолжает ленту (в конкретном случае «Возможно позже»).
#: Срабатывает ТОЛЬКО на явные капчи/сделки: текст сообщения должен содержать
#: один из маркеров CAPTCHA_MARKERS (иначе легко зациклиться в меню/Premium).
#: Кнопок при этом должно быть >= CAPTCHA_MIN_BUTTONS.
CAPTCHA_MIN_BUTTONS: int = 2

#: Маркеры текста, по которым сообщение считается капчей/сделкой/проверкой
#: (а не меню/Premium-промо). Любой из них (без учёта регистра) включает
#: авто-ответ последней кнопкой.
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
)


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
        self._db = db
        self._dedup: Dedup = Dedup()
        self._config = config
        self._parser = DvinchikParser(config.filters)
        self._dvinchik_chat_id = config.dvinchik.chat_id
        self._allowed_chat_ids = set(
            config.sources.allowed_chat_ids
            or ([config.dvinchik.chat_id] if config.dvinchik.chat_id else [])
        )
        self._pending_profiles: dict[int, int] = {}
        self._stats = stats
        # C: per-chat блокировка для гарантии порядка PROFILE → MEDIA_ONLY
        # при конкурентных handlers (без глобальной блокировки).
        self._chat_locks: dict[int, asyncio.Lock] = {}
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

        Ищет индекс аккаунта с session == account_session и берёт
        соответствующий client (accounts/clients параллельны).
        Для авто-уведомлений ищет «другой» аккаунт (session != account_session)
        — на него пересылается карточка анкеты с причиной (Бармалей↔Меланхолик).
        """
        mode = self._config.project.mode
        auto_cfg = self._config.auto_actions
        target_session = auto_cfg.account_session

        client = None
        notify_client = None
        if target_session:
            for i, acc in enumerate(self._config.telegram.accounts):
                if i >= len(self._clients):
                    break
                if acc.session == target_session:
                    client = self._clients[i]
                elif notify_client is None:
                    # Первый аккаунт, не являющийся авто-аккаунтом, — получатель
                    # авто-уведомлений (для notify_chat_id == 0).
                    notify_client = self._clients[i]

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
        )

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
            if await self._process_active_profile_if_any():
                return True
            if await self._press_view_button_if_needed():
                return True
            return await self._press_captcha_button()
        except Exception as e:
            logger.error(f"AutoAction: ошибка обработки активной анкеты: {e}")
            return False

    async def _process_active_profile_if_any(self) -> bool:
        """Обрабатывает уже показанную активную анкету, если она есть.

        Механика Дайвинчика: анкета приходит обычным текстом
        ("Имя, возраст, город – …") и ждёт реакции простым текстом ❤️/👎
        (кнопок на анкете может не быть). Сканируем последние сообщения чата
        на авто-аккаунте и берём самую свежую анкету (PROFILE), на которую
        ещё не отправлена реакция (нет исходящего ❤️/👎 после неё). Прогоняем
        её через штатный pipeline (_process_message: parse → filter → AI →
        авто-действие). Если активной анкеты нет — возвращаем False.
        """
        client = self._auto_engine.client
        if client is None:
            return False
        try:
            latest_action_id: int | None = None
            active = None
            async for msg in client.iter_messages(self._dvinchik_chat_id, limit=15):
                text = (msg.text or "").strip()
                if getattr(msg, "out", False) and text in (LIKE_TEXT, DISLIKE_TEXT):
                    if latest_action_id is None:
                        latest_action_id = msg.id
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
            )
            await self._process_message(task)
            return True
        except Exception as e:
            logger.error(f"AutoAction: ошибка обработки активной анкеты: {e}")
            return False

    async def _press_view_button_if_needed(self) -> bool:
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
            card_msg = None
            button_text = ""
            async for msg in client.iter_messages(self._dvinchik_chat_id, limit=15):
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
            async for msg in client.iter_messages(
                self._dvinchik_chat_id, limit=15
            ):
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

    async def _press_captcha_button(self) -> bool:
        """Нажимает ПОСЛЕДНЮЮ кнопку на проверке/капче Leo (сбрасывает диалог).

        Leo присылает сделки/подписки/подтверждения с reply-кнопками
        («Готово»/«Возможно позже» и т.п.). Чтобы не зависала лента, авто-аккаунт
        нажимает ПОСЛЕДНЮЮ кнопку — сбрасывает диалог и продолжает ленту.
        Реагируем ТОЛЬКО на явные капчи/сделки: текст сообщения должен содержать
        один из CAPTCHA_MARKERS, а reply-кнопок должно быть >= CAPTCHA_MIN_BUTTONS.
        Иначе легко зациклиться, нажимая кнопки в главном меню/Premium-промо Leo.
        Идемпотентно: если после карточки уже отправлен текст кнопки — не нажимаем.
        """
        client = self._auto_engine.client
        if client is None or not self._auto_engine.enabled:
            return False
        try:
            card_msg = None
            button_text = ""
            async for msg in client.iter_messages(self._dvinchik_chat_id, limit=15):
                text = (getattr(msg, "text", None) or "").lower()
                if not any(m in text for m in CAPTCHA_MARKERS):
                    continue
                texts = self._extract_button_texts(msg)
                if len(texts) >= CAPTCHA_MIN_BUTTONS:
                    card_msg = msg
                    button_text = texts[-1]  # правая/последняя кнопка
                    break

            if card_msg is None or not button_text:
                return False

            sent_at = card_msg.id
            already_sent = False
            async for msg in client.iter_messages(
                self._dvinchik_chat_id, limit=15
            ):
                if msg.id <= sent_at:
                    break
                if (
                    getattr(msg, "out", False)
                    and (msg.text or "").strip() == button_text
                ):
                    already_sent = True
                    break

            if already_sent:
                logger.info("AutoAction: правая кнопка капчи уже нажата")
                return False

            logger.info(
                f"AutoAction: капча/проверка (msg={card_msg.id}) — "
                f"нажимаю правую кнопку «{button_text}»"
            )
            await self._auto_engine.send_text(button_text)
            return True
        except Exception as e:
            logger.error(f"AutoAction: ошибка нажатия кнопки капчи: {e}")
            return False

    def _has_action_buttons(self, msg: object) -> bool:
        """Есть ли у сообщения кнопки действий (❤️/👎) — активная карточка."""
        texts = self._extract_button_texts(msg)
        return "\u2764" in texts or "\U0001F44E" in texts

    def _extract_button_texts(self, msg: object) -> list[str]:
        """Извлекает тексты reply-кнопок сообщения (пусто, если их нет)."""
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

        # === DEDUP (in-memory, атомарно в asyncio) ===
        # Быстрый фильтр повторной доставки в рамках сессии. Проверка БЕЗ
        # добавления: сам факт обработки фиксируется только ПОСЛЕ успешного
        # RAW-save (см. ниже ``mark``), чтобы при сбое сохранения сообщение не
        # терялось до restart (MEDIUM-2). Авторитетная защита — UNIQUE
        # (chat_id, telegram_message_id) в БД: save_raw_message делает
        # INSERT OR IGNORE и возвращает None при дубликате (переживает
        # restart/reconnect, когда in-memory Dedup пуст).
        if self._dedup.is_known((chat_id, event.message.id)):
            logger.debug(
                f"Повторная доставка отброшена (in-memory dedup): "
                f"chat_id={chat_id}, message_id={event.message.id}"
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
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock
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
            self._dedup.mark((chat_id, event.message.id))

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

        Сохраняет RAW в raw_messages и помечает processed_at — pipeline
        (парсинг/фильтр/AI) НЕ запускается. Единственная цель — ground truth
        для реверса механики LIKE: что именно отправляет пользователь.
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

        await self._maybe_record_manual_review(chat_id, msg.id, text)

    async def _maybe_record_manual_review(
        self, chat_id: int, outgoing_tm_id: int, text: str,
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
        context = await self._db.get_chat_profile_context(chat_id)
        if context is None:
            context = self._pending_profiles.get(chat_id)
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

            if msg_type == MessageType.PROFILE:
                parsed = self._parser.parse_profile(
                    text,
                    source_message_id=task.message_id,
                    source_chat_id=chat_id,
                )

                if self._profile_service:
                    try:
                        profile = await self._profile_service.upsert_profile(parsed)

                        # PROFILE-сообщение становится "контекстом" для
                        # последующих MEDIA_ONLY того же чата. Фиксируем сразу
                        # после сохранения — независимо от вывода в консоль.
                        self._pending_profiles[chat_id] = task.message_id
                        # Персистентный контекст: переживает restart/reconnect,
                        # когда in-memory кэш пуст (W2).
                        try:
                            await self._db.set_chat_profile_context(
                                chat_id, task.message_id
                            )
                        except Exception as e:
                            logger.error(f"Ошибка сохранения контекста чата: {e}")

                        is_new = profile.status == "NEW"
                        self._print_profile_stored(profile, is_new=is_new)

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
                if self._stats:
                    self._stats.record_match()

            elif msg_type == MessageType.MEDIA_ONLY:
                await self._handle_media_only(chat_id, task.message_id)
                if self._stats:
                    self._stats.record_media_only()

            elif msg_type == MessageType.UNKNOWN:
                logger.warning(
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
                        if any(VIEW_BUTTON_FRAGMENT in t for t in texts):
                            await self._press_view_button_if_needed()
                        elif (
                            len(texts) >= CAPTCHA_MIN_BUTTONS
                            and any(m in (text or "").lower() for m in CAPTCHA_MARKERS)
                        ):
                            # Капча/сделка/проверка — нажимаем последнюю кнопку.
                            await self._press_captcha_button()
                        else:
                            # Рекламное/промо-сообщение без кнопки на самом себе:
                            # «🚀 Смотреть анкеты» может прийти на ОТДЕЛЬНОМ сообщении
                            # после рекламы. Сканируем последние сообщения чата —
                            # _press_view_button_if_needed уже идемпотентен (жмёт
                            # только если кнопка реально есть и ещё не нажата), а
                            # меню/Premium-промо такой кнопки не содержат → не
                            # зацикливаемся.
                            await self._press_view_button_if_needed()
                    except Exception as e:
                        logger.error(f"AutoAction: ошибка нажатия кнопки ленты: {e}")

            elif msg_type == MessageType.SERVICE:
                if self._stats:
                    self._stats.record_service()

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

    async def _handle_media_only(self, chat_id: int, message_id: int) -> None:
        """Обработка photo-only сообщений: привязка к ПРЕДЫДУЩЕЙ анкете.

        Контекст (последнее PROFILE-сообщение чата) восстанавливается из БД
        (переживает restart/reconnect), затем — из in-memory кэша. Найденный
        профиль связывается с медиа-сообщением через profile_messages
        (UNIQUE(profile_id, telegram_message_id) исключает дубли media).
        """
        # Сначала пытаемся восстановить профиль из БД, затем in-memory.
        prev = await self._db.get_chat_profile_context(chat_id)
        if prev is None:
            prev = self._pending_profiles.get(chat_id)

        if prev is None:
            logger.info(
                f"Media-only without profile context: msg={message_id}"
            )
            return

        if self._profile_service is not None:
            try:
                profile = await self._profile_service.find_profile_by_message(
                    chat_id, prev
                )
                if profile is not None:
                    await self._profile_service.link_message_to_profile(
                        profile.id, message_id, chat_id
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

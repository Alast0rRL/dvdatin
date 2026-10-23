# BotErrorReporter: любая ошибка бота (запись уровня ERROR в логе) →
#   1) сообщение владельцу в Telegram (на «твинк» — тот же получатель, что у
#      уведомлений о лайках: AutoActionEngine.notify_owner),
#   2) app_state «bot_error» → всплывающее окно на сайте (web/db.get_bot_error).
#
# Зачем: прод 2026-10-23 бот встал на первой анкете, которую модель фото
# решила лайкнуть — ошибка была только в journalctl, владелец увидел лишь, что
# лента стоит. Перехват через sink loguru ловит ВСЕ ошибки, включая
# непредусмотренные. Одинаковая ошибка (без цифр) шлётся в Telegram не чаще
# NOTIFY_COOLDOWN_SEC; на сайте всегда видна последняя.
#
# Sink синхронный и может вызываться из любого потока — записи кладутся в
# asyncio.Queue через call_soon_threadsafe, отправляет фоновая задача run().

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from database.database import Database

#: Ключ app_state с последней ошибкой (читает сайт).
BOT_ERROR_STATE_KEY = "bot_error"


class BotErrorReporter:
    """Пересылает ошибки бота владельцу (Telegram) и на сайт."""

    #: Одинаковая ошибка — в Telegram не чаще, чем раз в N секунд.
    NOTIFY_COOLDOWN_SEC = 600.0
    #: Сколько ошибок может ждать отправки (лавина ошибок не копится).
    QUEUE_MAX = 100
    #: Обрезка текста ошибки в уведомлении.
    MAX_TEXT = 700

    def __init__(
        self,
        db: Database,
        notify: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self._db = db
        self._notify = notify
        self._queue: asyncio.Queue | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sent_at: dict[str, float] = {}
        self._counts: dict[str, int] = {}
        # Сколько раз ошибка повторилась с последнего уведомления в Telegram.
        self._since_sent: dict[str, int] = {}
        self._sink_id: int | None = None

    # ── Перехват логов ───────────────────────────────────────────────

    def install(self) -> None:
        """Подключает sink loguru уровня ERROR (вызывать внутри event loop)."""
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=self.QUEUE_MAX)
        self._sink_id = logger.add(self._sink, level="ERROR", format="{message}")

    def uninstall(self) -> None:
        if self._sink_id is not None:
            try:
                logger.remove(self._sink_id)
            except ValueError:
                pass
            self._sink_id = None

    #: Строка access-лога веб-сервера («1.2.3.4 - - [дата] "GET …" 400»):
    #: кривые запросы сканеров из интернета werkzeug пишет как ERROR — это не
    #: ошибки бота, слать их владельцу — спам.
    _ACCESS_LOG = re.compile(r'^\S+ - - \[')

    @classmethod
    def is_noise(cls, name: str, message: str) -> bool:
        return (name or "").startswith("werkzeug") or bool(cls._ACCESS_LOG.match(message or ""))

    def _sink(self, message) -> None:  # noqa: ANN001 — loguru Message
        record = message.record
        if record["name"] == __name__:
            return  # ошибки самого репортёра не репортим (иначе петля)
        if self.is_noise(record["name"], str(record["message"])):
            return
        item = {
            "at": datetime.now(timezone.utc).isoformat(),
            "where": f"{record['name']}:{record['function']}:{record['line']}",
            "message": str(record["message"])[: self.MAX_TEXT],
        }
        loop, queue = self._loop, self._queue
        if loop is None or queue is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._put, item)
        except RuntimeError:
            pass

    def _put(self, item: dict) -> None:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            pass

    # ── Отправка ─────────────────────────────────────────────────────

    @staticmethod
    def signature(text: str) -> str:
        """Ключ «та же ошибка»: без чисел (id анкет, сообщений, время)."""
        return re.sub(r"\d+", "#", text)[:160]

    async def handle(self, item: dict) -> None:
        """Сохраняет ошибку для сайта и, если не было недавно, шлёт в Telegram."""
        key = self.signature(item["message"])
        self._counts[key] = self._counts.get(key, 0) + 1
        payload = dict(item, count=self._counts[key])
        try:
            await self._db.set_state(
                BOT_ERROR_STATE_KEY, json.dumps(payload, ensure_ascii=False),
            )
        except Exception as e:
            logger.warning(f"ErrorReporter: не сохранил ошибку для сайта: {e!r}")
        now = time.monotonic()
        last = self._sent_at.get(key)
        if last is not None and now - last < self.NOTIFY_COOLDOWN_SEC:
            self._since_sent[key] = self._since_sent.get(key, 0) + 1
            return
        self._sent_at[key] = now
        repeats = self._since_sent.pop(key, 0)
        if self._notify is None:
            return
        text = (
            "⚠️ Ошибка бота\n"
            f"{item['message']}\n\n"
            f"Где: {item['where']}"
        )
        if repeats:
            text += f"\nПовторялась с прошлого уведомления: {repeats} раз"
        try:
            await self._notify(text)
        except Exception as e:
            logger.warning(f"ErrorReporter: не отправил уведомление: {e!r}")

    async def run(self) -> None:
        """Фоновая задача: разбирает очередь ошибок."""
        if self._queue is None:
            self.install()
        while True:
            item = await self._queue.get()
            await self.handle(item)

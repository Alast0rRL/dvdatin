# Worker: выносит обработку RAW → parse → filter → AI из Telegram-хендлера.
#
# Хендлер Telegram быстро принимает сообщение, сохраняет RAW и кладёт его в
# RawQueue. DvinchikRawWorker потребляет очередь и выполняет дорогой pipeline
# (parse → filter → AI → decision) в фоновой задаче, чтобы сетевой/тяжёлый I/O
# не блокировал обработку входящих событий Telegram.

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from loguru import logger

from collectors.raw_queue import RawQueue


@dataclass
class RawTask:
    """Снимок необработанных данных сообщения для фоновой обработки.

    Несёт всё, что нужно pipeline (parse → filter → AI), включая ссылку на
    Telegraph Message (msg) для скачивания media после Filter PASS.
    """
    chat_id: int
    message_id: int
    sender_id: int
    sender_username: str
    sender_name: str
    text: str
    media_type: str
    entities_json: str
    reply_to: int | None
    received_at: str
    msg_date: str
    msg: object | None
    raw_id: int
    reply_markup_json: str = "[]"


#: Сигнатура функции обработки одного задания.
ProcessFn = Callable[[RawTask], Awaitable[None]]


#: Сигнальный элемент, завершающий цикл worker'а при graceful shutdown.
_SENTINEL: object = None


class DvinchikRawWorker:
    """Потребляет RawQueue и выполняет pipeline вне Telegram-хендлера.

    Ошибки обработки отдельного сообщения логируются и не останавливают
    worker — коллектор не падает.
    """

    def __init__(self, process: ProcessFn, maxsize: int = 1000) -> None:
        self._queue = RawQueue(maxsize=maxsize)
        self._process = process
        self._task: asyncio.Task | None = None

    @property
    def qsize(self) -> int:
        """Текущий размер очереди (для тестов и отладки)."""
        return self._queue.qsize

    @property
    def stats(self) -> dict:
        """Статистика очереди."""
        return self._queue.stats

    async def enqueue(self, item: RawTask) -> None:
        """Помещает задание в очередь."""
        await self._queue.put(item)

    def start(self) -> None:
        """Запускает фоновый обработчик очереди (идемпотентно)."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.get_event_loop().create_task(self._run())

    async def stop(self) -> None:
        """Плавно завершает worker: дообрабатывает очередь и выходит.

        Кладёт сигнальный элемент (None); цикл обрабатывает оставшиеся
        задания (FIFO), затем на сигнале выходит. Повторный вызов безопасен.
        """
        if self._task is None:
            return
        if not self._task.done():
            await self._queue.put(_SENTINEL)
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        """Цикл обработки заданий из очереди."""
        while True:
            item = await self._queue.get()
            if item is _SENTINEL:
                break
            try:
                await self._process(item)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"RawWorker: ошибка обработки сообщения: {e}")

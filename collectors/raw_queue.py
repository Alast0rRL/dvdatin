# Асинхронная очередь RAW-сообщений.

from __future__ import annotations

import asyncio
from typing import Any


class RawQueue:
    """Асинхронная очередь для буферизации RAW-сообщений."""

    def __init__(self, maxsize: int = 1000) -> None:
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self._total_enqueued = 0
        self._total_dequeued = 0

    async def put(self, item: Any) -> None:
        """Добавляет элемент в очередь."""
        await self._queue.put(item)
        self._total_enqueued += 1

    async def get(self) -> Any:
        """Извлекает элемент из очереди."""
        item = await self._queue.get()
        self._total_dequeued += 1
        return item

    @property
    def qsize(self) -> int:
        """Текущий размер очереди."""
        return self._queue.qsize()

    @property
    def stats(self) -> dict:
        """Статистика очереди."""
        return {
            "qsize": self.qsize,
            "total_enqueued": self._total_enqueued,
            "total_dequeued": self._total_dequeued,
        }

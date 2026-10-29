"""Общие моки Telegram-клиента для тестов.

У ``TelegramClient`` (Telethon) часть методов синхронна: ``iter_dialogs`` /
``iter_messages`` возвращают async-итератор, ``add_event_handler`` —
обычная регистрация. На голом ``AsyncMock()`` они возвращают корутину,
которую никто не ждёт → ``RuntimeWarning: coroutine ... was never awaited``
(а с ``filterwarnings = error::RuntimeWarning`` — падение теста).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import Any
from unittest.mock import AsyncMock, MagicMock


async def async_iter(items: Iterable[Any]) -> AsyncIterator[Any]:
    """Async-итератор по списку — замена ``client.iter_*`` в тестах."""
    for item in items:
        yield item


def make_tg_client() -> AsyncMock:
    """``AsyncMock``-клиент Telethon с синхронными методами как ``MagicMock``.

    ``send_message`` возвращает ``MagicMock`` (а не ``AsyncMock``): движок
    читает у отправленного сообщения ``id``/``date`` синхронно.
    """
    client = AsyncMock()
    client.iter_dialogs = MagicMock(side_effect=lambda *a, **k: async_iter([]))
    client.add_event_handler = MagicMock()
    client.remove_event_handler = MagicMock()
    client.send_message = AsyncMock(return_value=MagicMock(date=None))
    return client

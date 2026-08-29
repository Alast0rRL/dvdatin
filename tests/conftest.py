# Общая фикстура Stage 6.1: задаёт текущий event loop для каждого теста.
#
# Проект использует шаблон `asyncio.get_event_loop().run_until_complete()`
# (см. AGENTS.md) без pytest-asyncio. Если event loop не установлен,
# `asyncio.get_event_loop()` в Python 3.12 выдаёт DeprecationWarning
# "There is no current event loop". Эта autouse-фикстура заранее создаёт
# и устанавливает свежий loop на поток, убирая предупреждение без
# рефакторинга всех тестов.

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(autouse=True)
def _event_loop() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield
    loop.close()

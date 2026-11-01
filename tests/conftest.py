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
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _event_loop() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield
    loop.close()


#: Боевой конфиг разработчика (gitignored): тесты НЕ должны в него писать.
_REAL_CONFIG = (Path(__file__).resolve().parent.parent / "config" / "config.yaml").resolve()


@pytest.fixture(autouse=True)
def _protect_real_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Запись режима/аккаунта в config/config.yaml из тестов — ошибка.

    Коллектор по умолчанию пишет в ``config/config.yaml``; тест, не задавший
    свой путь, иначе тихо менял бы настоящий конфиг. Ошибку ловит
    ``set_mode`` (логирует), сам тест продолжает работать.
    """
    from app.config import AppConfig

    for name in ("persist_mode", "persist_account_controls", "persist_account_session"):
        original = getattr(AppConfig, name)

        def guarded(self, path, *args, _original=original, **kwargs):
            if Path(path).resolve() == _REAL_CONFIG:
                raise RuntimeError("тест пытался записать настоящий config/config.yaml")
            return _original(self, path, *args, **kwargs)

        monkeypatch.setattr(AppConfig, name, guarded)


@pytest.fixture(autouse=True)
def _telegram_notify_on(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Тесты уведомлений проверяют их содержимое — в тестах они включены.

    В проде ``auto_actions.notify_telegram`` по умолчанию False (аккаунты не
    пишут друг другу). Сам выключатель проверяет ``TestTelegramNotifySwitch``
    (маркер ``real_notify_switch``).
    """
    if request.node.get_closest_marker("real_notify_switch"):
        return
    from collectors.auto_action import AutoActionEngine

    monkeypatch.setattr(AutoActionEngine, "_telegram_notify_on", lambda self: True)

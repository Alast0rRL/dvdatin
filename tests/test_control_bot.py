# Unit-тесты Stage 7.5: ControlBot (панель управления режимом).

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.config import AppConfig
from collectors.auto_action import AutoActionEngine
from core.types import Mode
from telegram.control_bot import ControlBot


def make_config(**overrides) -> AppConfig:
    defaults = {
        "telegram": {"api_id": 123, "api_hash": "abc"},
        "dvinchik": {"chat_id": 1234060895},
        "control": {"enabled": True, "allowed_user_ids": [8525808108]},
    }
    defaults.update(overrides)
    return AppConfig(**defaults)


def make_bot(
    collector=None,
    config: AppConfig | None = None,
    db=None,
) -> tuple[ControlBot, AsyncMock, object]:
    client = AsyncMock()
    if collector is None:
        collector = _make_mock_collector()
    if db is None:
        db = AsyncMock()
        db.get_all_ai_decisions = AsyncMock(return_value=[])
    cfg = config if config is not None else make_config()
    bot = ControlBot(client, cfg, collector, db)
    return bot, client, collector


def _make_mock_collector():
    engine = MagicMock(spec=AutoActionEngine)
    engine.enabled = True
    engine.mode = Mode.OBSERVE
    engine.client = AsyncMock()
    engine.mode_label = "OBSERVE"

    collector = MagicMock()
    collector.auto_engine.return_value = engine
    collector.mode = Mode.OBSERVE
    collector.set_mode = MagicMock()
    collector.start_auto_stream = AsyncMock()
    return collector


def _event(sender_id: int, text: str = "") -> MagicMock:
    ev = MagicMock()
    ev.sender_id = sender_id
    ev.pattern_match = MagicMock()
    match = MagicMock()
    ev.pattern_match.group.return_value = text
    ev.respond = AsyncMock()
    ev.edit = AsyncMock()
    ev.answer = AsyncMock()
    return ev


class TestControlBotAuth:
    def test_denies_unauthorized(self) -> None:
        bot, client, collector = make_bot()
        ev = _event(sender_id=999999999)
        asyncio.get_event_loop().run_until_complete(
            bot._cmd_status(ev)
        )
        collector.set_mode.assert_not_called()
        ev.respond.assert_not_awaited()

    def test_allows_authorized(self) -> None:
        bot, client, collector = make_bot()
        ev = _event(sender_id=8525808108)
        asyncio.get_event_loop().run_until_complete(
            bot._cmd_status(ev)
        )
        ev.respond.assert_awaited_once()


class TestControlBotActions:
    def test_mode_set_helper(self) -> None:
        bot, client, collector = make_bot()
        ev = _event(sender_id=8525808108, text="on")
        ev.pattern_match.group.return_value = "on"
        asyncio.get_event_loop().run_until_complete(
            bot._cmd_mode(ev)
        )
        collector.set_mode.assert_called_once_with(Mode.SEMI_AUTO)

    def test_toggle_on_callback(self) -> None:
        bot, client, collector = make_bot()
        ev = _event(sender_id=8525808108)
        ev.data = b"control:on"
        ev.chat_id = 1234060895  # не используется, но гарантирует identity
        asyncio.get_event_loop().run_until_complete(
            bot._on_callback(ev)
        )
        collector.set_mode.assert_called_once_with(Mode.SEMI_AUTO)
        ev.edit.assert_awaited_once()

    def test_toggle_off_callback(self) -> None:
        bot, client, collector = make_bot()
        ev = _event(sender_id=8525808108)
        ev.data = b"control:off"
        asyncio.get_event_loop().run_until_complete(
            bot._on_callback(ev)
        )
        collector.set_mode.assert_called_once_with(Mode.OBSERVE)
        ev.edit.assert_awaited_once()

    def test_unrelated_callback_ignored(self) -> None:
        bot, client, collector = make_bot()
        ev = _event(sender_id=8525808108)
        ev.data = b"review:next"
        asyncio.get_event_loop().run_until_complete(
            bot._on_callback(ev)
        )
        collector.set_mode.assert_not_called()


class TestControlBotStream:
    def test_stream_when_enabled(self) -> None:
        bot, client, collector = make_bot()
        ev = _event(sender_id=8525808108)
        asyncio.get_event_loop().run_until_complete(
            bot._cmd_stream(ev)
        )
        collector.start_auto_stream.assert_awaited_once()

    def test_stream_when_disabled_no_action(self) -> None:
        collector = _make_mock_collector()
        engine = collector.auto_engine.return_value
        engine.enabled = False
        bot, client, _ = make_bot(collector=collector)
        ev = _event(sender_id=8525808108)
        asyncio.get_event_loop().run_until_complete(
            bot._cmd_stream(ev)
        )
        collector.start_auto_stream.assert_not_awaited()
        ev.respond.assert_awaited_once()  # ответ «выключено»

# Unit-тесты Stage 7: AutoActionEngine (авто-действия ❤️/👎).

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from collectors.auto_action import (
    AutoActionEngine,
    DISLIKE_TEXT,
    LIKE_TEXT,
)
from app.config import AutoActionsConfig
from core.types import Mode
from models.decision import AIDecision


def make_auto_config(**overrides) -> AutoActionsConfig:
    defaults = {
        "enabled": True,
        "account_session": "dvai_2",
        "interval_sec": 0.0,  # тесты: без реальной задержки
        "start_command": "\U00002728\U0001F50D",
    }
    defaults.update(overrides)
    return AutoActionsConfig(**defaults)


def make_client() -> AsyncMock:
    client = AsyncMock()
    client.is_connected.return_value = True
    client.send_message = AsyncMock(return_value=MagicMock())
    return client


def make_engine(
    mode: Mode = Mode.SEMI_AUTO,
    client: AsyncMock | None = None,
    config: AutoActionsConfig | None = None,
) -> AutoActionEngine:
    return AutoActionEngine(
        client=client if client is not None else make_client(),
        config=config if config is not None else make_auto_config(),
        mode=mode,
        chat_id=1234060895,
    )


class TestAutoActionGate:
    def test_disabled_in_observe_mode(self) -> None:
        e = make_engine(mode=Mode.OBSERVE)
        assert e.enabled is False

    def test_enabled_in_semi_auto(self) -> None:
        assert make_engine(mode=Mode.SEMI_AUTO).enabled is True

    def test_enabled_in_auto(self) -> None:
        assert make_engine(mode=Mode.AUTO).enabled is True

    def test_disabled_when_config_off(self) -> None:
        e = make_engine(config=make_auto_config(enabled=False))
        assert e.enabled is False

    def test_disabled_without_client(self) -> None:
        e = AutoActionEngine(
            client=None,
            config=make_auto_config(),
            mode=Mode.SEMI_AUTO,
            chat_id=1234060895,
        )
        assert e.enabled is False

    def test_observe_returns_gate(self) -> None:
        e = make_engine(mode=Mode.OBSERVE)
        result = asyncio.get_event_loop().run_until_complete(
            e.maybe_act(AIDecision.LIKE)
        )
        assert result == "GATE"


class TestAutoActionExec:
    def test_like_sends_heart(self) -> None:
        client = make_client()
        e = make_engine(client=client)
        result = asyncio.get_event_loop().run_until_complete(
            e.maybe_act(AIDecision.LIKE)
        )
        assert result == "LIKE"
        client.send_message.assert_called_once()
        args, _ = client.send_message.call_args
        assert args[1] == LIKE_TEXT

    def test_dislike_sends_thumbsdown(self) -> None:
        client = make_client()
        e = make_engine(client=client)
        result = asyncio.get_event_loop().run_until_complete(
            e.maybe_act(AIDecision.DISLIKE)
        )
        assert result == "DISLIKE"
        args, _ = client.send_message.call_args
        assert args[1] == DISLIKE_TEXT

    def test_review_skips(self) -> None:
        client = make_client()
        e = make_engine(client=client)
        result = asyncio.get_event_loop().run_until_complete(
            e.maybe_act(AIDecision.REVIEW)
        )
        assert result == "SKIP"
        client.send_message.assert_not_called()

    def test_none_decision_skips(self) -> None:
        client = make_client()
        e = make_engine(client=client)
        result = asyncio.get_event_loop().run_until_complete(
            e.maybe_act(None)
        )
        assert result == "SKIP"
        client.send_message.assert_not_called()

    def test_error_on_send_raises_auto_action_error(self) -> None:
        from collectors.auto_action import AutoActionError

        client = make_client()
        client.send_message = AsyncMock(side_effect=RuntimeError("network"))
        e = make_engine(client=client)
        with pytest.raises(AutoActionError):
            asyncio.get_event_loop().run_until_complete(
                e.maybe_act(AIDecision.LIKE)
            )


class TestAutoActionRateLimit:
    def test_rate_limit_delays_between_actions(self) -> None:
        import time

        client = make_client()
        e = make_engine(
            client=client, config=make_auto_config(interval_sec=0.05)
        )
        loop = asyncio.get_event_loop()
        loop.run_until_complete(e.maybe_act(AIDecision.LIKE))
        t0 = time.monotonic()
        loop.run_until_complete(e.maybe_act(AIDecision.DISLIKE))
        elapsed = time.monotonic() - t0
        # Второе действие должно подождать >= interval_sec.
        assert elapsed >= 0.04

    def test_start_stream_sends_start_command(self) -> None:
        client = make_client()
        e = make_engine(client=client)
        asyncio.get_event_loop().run_until_complete(e.start_stream())
        client.send_message.assert_called_once()
        args, _ = client.send_message.call_args
        assert args[1] == "\U00002728\U0001F50D"


class TestAutoActionConfig:
    def test_defaults(self) -> None:
        c = AutoActionsConfig()
        assert c.enabled is False
        assert c.account_session == ""
        assert c.interval_sec == 10.0

    def test_interval_positive(self) -> None:
        with pytest.raises(Exception):
            AutoActionsConfig(interval_sec=-1)


class TestAutoActionRuntimeMode:
    """Динамическое переключение режима на лету (ControlBot)."""

    def test_mode_setter_updates_gate(self) -> None:
        # Начинаем в OBSERVE → выключено.
        e = make_engine(mode=Mode.OBSERVE)
        assert e.enabled is False
        # Включаем SEMI_AUTO на лету.
        e.mode = Mode.SEMI_AUTO
        assert e.enabled is True

    def test_turn_off_at_runtime(self) -> None:
        e = make_engine(mode=Mode.SEMI_AUTO)
        assert e.enabled is True
        e.mode = Mode.OBSERVE
        assert e.enabled is False

    def test_mode_setter_persists_value(self) -> None:
        e = make_engine(mode=Mode.OBSERVE)
        e.mode = Mode.SEMI_AUTO
        assert e.mode == Mode.SEMI_AUTO
        assert e.mode_label == "SEMI_AUTO"

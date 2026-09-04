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
        "start_command": "",
    }
    defaults.update(overrides)
    return AutoActionsConfig(**defaults)


def make_client() -> AsyncMock:
    client = AsyncMock()
    client.is_connected.return_value = True
    client.send_message = AsyncMock(return_value=MagicMock())
    client.forward_messages = AsyncMock(return_value=MagicMock())
    return client


def make_engine(
    mode: Mode = Mode.SEMI_AUTO,
    client: AsyncMock | None = None,
    config: AutoActionsConfig | None = None,
    chat_id: int = 1234060895,
    notify_client: AsyncMock | None = None,
) -> AutoActionEngine:
    return AutoActionEngine(
        client=client if client is not None else make_client(),
        config=config if config is not None else make_auto_config(),
        mode=mode,
        chat_id=chat_id,
        notify_client=notify_client,
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

    def test_review_sends_dislike_to_keep_stream_moving(self) -> None:
        client = make_client()
        e = make_engine(client=client)
        result = asyncio.get_event_loop().run_until_complete(
            e.maybe_act(AIDecision.REVIEW)
        )
        assert result == "DISLIKE"
        args, _ = client.send_message.call_args
        assert args[1] == DISLIKE_TEXT

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

    def test_start_stream_without_command_does_not_send(self) -> None:
        client = make_client()
        e = make_engine(client=client)
        started = asyncio.get_event_loop().run_until_complete(e.start_stream())
        assert started is False
        client.send_message.assert_not_called()


class TestAutoActionConfig:
    def test_defaults(self) -> None:
        c = AutoActionsConfig()
        assert c.enabled is False
        assert c.account_session == ""
        assert c.interval_sec == 10.0
        assert c.notify_chat_id == 0

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


class TestAutoActionNotify:
    """Уведомления владельцу о лайке/дизлайке.

    Два режима: явный ``notify_chat_id > 0`` и авто-режим (0 + notify_client),
    когда уведомление уходит на «другой» аккаунт (Бармалей↔Меланхолик).
    """

    def _notify_engine(self, notify_chat_id: int = 8525808108) -> tuple[AsyncMock, AutoActionEngine]:
        client = make_client()
        e = make_engine(
            client=client,
            config=make_auto_config(notify_chat_id=notify_chat_id),
        )
        return client, e

    def test_no_notify_when_disabled(self) -> None:
        client, e = self._notify_engine(notify_chat_id=0)
        asyncio.get_event_loop().run_until_complete(
            e.maybe_act(AIDecision.LIKE, profile_id=1, message_id=900)
        )
        client.forward_messages.assert_not_called()
        # Основное действие всё равно отправлено.
        client.send_message.assert_called_once()
        args, _ = client.send_message.call_args
        assert args[1] == LIKE_TEXT

    def test_no_notify_without_message_id(self) -> None:
        client, e = self._notify_engine()
        asyncio.get_event_loop().run_until_complete(
            e.maybe_act(AIDecision.LIKE, profile_id=1)
        )
        client.forward_messages.assert_not_called()

    def test_like_forwards_and_explains(self) -> None:
        client, e = self._notify_engine()
        reasons = ["Фотографии выше среднего качества", "Проанализировано 2 фото"]
        result = asyncio.get_event_loop().run_until_complete(
            e.maybe_act(
                AIDecision.LIKE, profile_id=1,
                message_id=900, reasons=reasons,
            )
        )
        assert result == "LIKE"
        client.forward_messages.assert_awaited_once_with(
            8525808108, 900, from_peer=1234060895
        )
        # Одно сообщение — само действие (❤️), второе — объяснение.
        assert client.send_message.call_count == 2
        explain_call = client.send_message.call_args_list[1]
        text = explain_call.args[1]
        assert text.startswith("❤️ Лайк")
        assert "Фотографии выше среднего качества" in text
        assert "Проанализировано 2 фото" in text

    def test_dislike_explains(self) -> None:
        client, e = self._notify_engine()
        reasons = ["CITY_OUT_OF_RANGE", "Возраст не подходит"]
        result = asyncio.get_event_loop().run_until_complete(
            e.maybe_act(
                AIDecision.DISLIKE, profile_id=1,
                message_id=901, reasons=reasons,
            )
        )
        assert result == "DISLIKE"
        explain_call = client.send_message.call_args_list[1]
        text = explain_call.args[1]
        assert text.startswith("👎 Дизлайк")
        assert "Город не в списке" in text
        assert "Возраст не подходит" in text

    def test_review_notify_explains_no_data(self) -> None:
        """REVIEW → 👎 транспортно, но уведомление «На ревью» с причиной."""
        client, e = self._notify_engine()
        asyncio.get_event_loop().run_until_complete(
            e.maybe_act(
                AIDecision.REVIEW, profile_id=1,
                message_id=901, reasons=["NO_FEATURES_FOUND"],
            )
        )
        # Первое сообщение — 👎 в чат Leo, второе — уведомление владельцу.
        assert client.send_message.call_count == 2
        explain_call = client.send_message.call_args_list[1]
        text = explain_call.args[1]
        assert text.startswith("👎 На ревью")
        assert "Мало информации в анкете" in text
        assert "Дизлайк" not in text

    def test_decision_codes_are_filtered_out(self) -> None:
        client, e = self._notify_engine()
        reasons = ["LIKE_THRESHOLD", "BELOW_THRESHOLDS", "Фото выше среднего"]
        asyncio.get_event_loop().run_until_complete(
            e.maybe_act(
                AIDecision.LIKE, profile_id=1,
                message_id=902, reasons=reasons,
            )
        )
        text = client.send_message.call_args_list[1].args[1]
        assert "LIKE_THRESHOLD" not in text
        assert "BELOW_THRESHOLDS" not in text
        assert "Фото выше среднего" in text

    def test_user_skip_label_humanized(self) -> None:
        client, e = self._notify_engine()
        asyncio.get_event_loop().run_until_complete(
            e.maybe_act(
                AIDecision.DISLIKE, profile_id=1,
                message_id=903, reasons=["USER_SKIP:курит"],
            )
        )
        text = client.send_message.call_args_list[1].args[1]
        assert "Ваша стоп-метка: курит" in text

    def test_notify_error_does_not_break_action(self) -> None:
        client, e = self._notify_engine()
        client.forward_messages = AsyncMock(side_effect=RuntimeError("network"))
        result = asyncio.get_event_loop().run_until_complete(
            e.maybe_act(
                AIDecision.LIKE, profile_id=1,
                message_id=904, reasons=["ok"],
            )
        )
        # Действие не должно падать из-за ошибки уведомления.
        assert result == "LIKE"

    def test_empty_reasons_skips_explanation(self) -> None:
        client, e = self._notify_engine()
        asyncio.get_event_loop().run_until_complete(
            e.maybe_act(AIDecision.LIKE, profile_id=1, message_id=905)
        )
        # Пересылка есть, но пояснение не отправляется (нет причин).
        client.forward_messages.assert_awaited_once()
        assert client.send_message.call_count == 1

    def _notify_client_with_me(self, user_id: int = 1753676469) -> AsyncMock:
        nc = make_client()
        me = MagicMock()
        me.id = user_id
        nc.get_me = AsyncMock(return_value=me)
        return nc

    def test_auto_notify_goes_to_other_account(self) -> None:
        """notify_chat_id=0 + notify_client → уведомление на другой аккаунт."""
        client = make_client()
        notify_client = self._notify_client_with_me(user_id=1753676469)
        e = make_engine(
            client=client,
            config=make_auto_config(notify_chat_id=0),
            notify_client=notify_client,
        )
        result = asyncio.get_event_loop().run_until_complete(
            e.maybe_act(
                AIDecision.LIKE, profile_id=1,
                message_id=910, reasons=["ok"],
            )
        )
        assert result == "LIKE"
        notify_client.get_me.assert_awaited_once()
        client.forward_messages.assert_awaited_once_with(
            1753676469, 910, from_peer=1234060895
        )
        # Второе сообщение — объяснение, отправлено на Меланхолика.
        assert client.send_message.call_count == 2
        explain_call = client.send_message.call_args_list[1]
        assert explain_call.args[0] == 1753676469

    def test_auto_notify_me_is_cached(self) -> None:
        """user_id другого аккаунта кешируется (get_me не дёргается повторно)."""
        client = make_client()
        notify_client = self._notify_client_with_me(user_id=1753676469)
        e = make_engine(
            client=client,
            config=make_auto_config(notify_chat_id=0, interval_sec=0.0),
            notify_client=notify_client,
        )
        loop = asyncio.get_event_loop()
        loop.run_until_complete(
            e.maybe_act(AIDecision.LIKE, profile_id=1, message_id=911, reasons=["a"])
        )
        loop.run_until_complete(
            e.maybe_act(AIDecision.DISLIKE, profile_id=2, message_id=912, reasons=["b"])
        )
        # get_me вызывается один раз (кеш).
        assert notify_client.get_me.await_count == 1

    def test_auto_notify_skipped_without_notify_client(self) -> None:
        """notify_chat_id=0 и нет notify_client → уведомление пропускается."""
        client = make_client()
        e = make_engine(client=client, config=make_auto_config(notify_chat_id=0))
        asyncio.get_event_loop().run_until_complete(
            e.maybe_act(AIDecision.LIKE, profile_id=1, message_id=913)
        )
        client.forward_messages.assert_not_called()
        # Основное действие отправлено.
        assert client.send_message.call_count == 1


class TestFormatReason:
    def test_formats_filter_and_ai_reasons(self) -> None:
        text = AutoActionEngine._format_reason(
            "DISLIKE", ["CITY_OUT_OF_RANGE", "Фото низкого качества"],
        )
        assert text == "👎 Дизлайк\n• Город не в списке\n• Фото низкого качества"

    def test_filters_decision_codes(self) -> None:
        text = AutoActionEngine._format_reason("LIKE", ["LIKE_THRESHOLD"])
        assert text == "❤️ Лайк"

    def test_filters_prefix_labels(self) -> None:
        text = AutoActionEngine._format_reason(
            "DISLIKE", ["USER_SKIP:алкоголь"],
        )
        assert "• Ваша стоп-метка: алкоголь" in text

    def test_returns_empty_for_none(self) -> None:
        assert AutoActionEngine._format_reason("LIKE", None) == ""
        assert AutoActionEngine._format_reason("LIKE", []) == ""

    def test_review_labels_as_review_not_dislike(self) -> None:
        """REVIEW-уведомление — «На ревью», а не «Дизлайк»."""
        text = AutoActionEngine._format_reason(
            "REVIEW", ["NO_FEATURES_FOUND"],
        )
        assert text == "👎 На ревью\n• Мало информации в анкете"

    def test_review_no_features_is_humanized(self) -> None:
        """Детерминированный REVIEW с NO_FEATURES_FOUND пишет причину, а не вырезает её."""
        text = AutoActionEngine._format_reason("REVIEW", ["NO_FEATURES_FOUND"])
        assert "Дизлайк" not in text
        assert "Мало информации в анкете" in text

    def test_no_features_found_in_dislike_is_humanized(self) -> None:
        """NO_FEATURES_FOUND больше не вырезается как внутренний код."""
        text = AutoActionEngine._format_reason("DISLIKE", ["NO_FEATURES_FOUND"])
        assert "Мало информации в анкете" in text


class TestAutoActionIdempotency:
    """TEST GAP #13: same profile + new telegram_message_id → action allowed.

    Идемпотентность по telegram_message_id обеспечивается на уровне collector
    (has_auto_action_for_message в БД), а не в движке. Движок НЕ блокирует
    повторные действия по profile_id — это позволяет ленте Leo не замирать
    при повторных показах одного и того же человека.
    """

    def test_same_profile_id_allows_repeated_actions(self) -> None:
        """Один profile_id, два разных действия — оба отправляются."""
        client = make_client()
        e = make_engine(client=client)
        loop = asyncio.get_event_loop()
        r1 = loop.run_until_complete(
            e.maybe_act(AIDecision.LIKE, profile_id=42, message_id=100)
        )
        r2 = loop.run_until_complete(
            e.maybe_act(AIDecision.DISLIKE, profile_id=42, message_id=200)
        )
        assert r1 == "LIKE"
        assert r2 == "DISLIKE"
        assert client.send_message.await_count == 2

    def test_same_profile_id_allows_same_action_twice(self) -> None:
        """Два одинаковых действия подряд на один profile_id — оба проходят."""
        client = make_client()
        e = make_engine(client=client)
        loop = asyncio.get_event_loop()
        r1 = loop.run_until_complete(
            e.maybe_act(AIDecision.DISLIKE, profile_id=7, message_id=301)
        )
        r2 = loop.run_until_complete(
            e.maybe_act(AIDecision.DISLIKE, profile_id=7, message_id=302)
        )
        assert r1 == "DISLIKE"
        assert r2 == "DISLIKE"
        assert client.send_message.await_count == 2

    def test_no_profile_id_still_allows_repeated_actions(self) -> None:
        """Без profile_id — движок не отслеживает дубликаты."""
        client = make_client()
        e = make_engine(client=client)
        loop = asyncio.get_event_loop()
        r1 = loop.run_until_complete(e.maybe_act(AIDecision.LIKE))
        r2 = loop.run_until_complete(e.maybe_act(AIDecision.LIKE))
        assert r1 == "LIKE"
        assert r2 == "LIKE"
        assert client.send_message.await_count == 2

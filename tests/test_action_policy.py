# Unit-тесты слоя «Decision != Action» (§30): ActionPolicyResolver.

from __future__ import annotations

from models.action import ActionPolicy
from models.decision import AIDecision
from services.action_policy import ActionPolicyResolver

RES = ActionPolicyResolver()


class TestActionPolicyResolve:
    """Базовое сопоставление Decision + информативность → ActionPolicy."""

    def test_none_decision_no_action(self) -> None:
        assert RES.resolve(None) == ActionPolicy.NO_ACTION

    def test_disable_no_action_any_informative(self) -> None:
        assert RES.resolve(AIDecision.DISLIKE, informative=False) == ActionPolicy.DISLIKE_ONLY
        assert RES.resolve(AIDecision.DISLIKE, informative=True) == ActionPolicy.DISLIKE_ONLY

    def test_review_no_action(self) -> None:
        assert RES.resolve(AIDecision.REVIEW) == ActionPolicy.NO_ACTION

    def test_like_informative_goes_to_message(self) -> None:
        assert RES.resolve(AIDecision.LIKE, informative=True) == ActionPolicy.LIKE_AND_MESSAGE

    def test_like_not_informative_like_only(self) -> None:
        assert RES.resolve(AIDecision.LIKE, informative=False) == ActionPolicy.LIKE_ONLY


class TestActionPolicyConfig:
    """Конфиг-гейты: like и like_message — ОТДЕЛЬНЫЕ настройки."""

    def test_disable_only_ignores_like_gate(self) -> None:
        # 👎 не гейтается настройкой LIKE.
        assert (
            RES.resolve_config(
                ActionPolicy.DISLIKE_ONLY, like_enabled=False, like_message_enabled=False,
            )
            == ActionPolicy.DISLIKE_ONLY
        )

    def test_like_only_requires_like_enabled(self) -> None:
        assert (
            RES.resolve_config(ActionPolicy.LIKE_ONLY, like_enabled=True, like_message_enabled=False)
            == ActionPolicy.LIKE_ONLY
        )
        assert (
            RES.resolve_config(ActionPolicy.LIKE_ONLY, like_enabled=False, like_message_enabled=False)
            == ActionPolicy.NO_ACTION
        )

    def test_like_and_message_full_chain(self) -> None:
        assert (
            RES.resolve_config(
                ActionPolicy.LIKE_AND_MESSAGE, like_enabled=True, like_message_enabled=True,
            )
            == ActionPolicy.LIKE_AND_MESSAGE
        )

    def test_like_and_message_degrades_to_like_only_without_msg(self) -> None:
        # Сообщение выключено, но ❤️ включено → только LIKE (без сообщения).
        assert (
            RES.resolve_config(
                ActionPolicy.LIKE_AND_MESSAGE, like_enabled=True, like_message_enabled=False,
            )
            == ActionPolicy.LIKE_ONLY
        )

    def test_like_and_message_no_action_if_like_off(self) -> None:
        assert (
            RES.resolve_config(
                ActionPolicy.LIKE_AND_MESSAGE, like_enabled=False, like_message_enabled=True,
            )
            == ActionPolicy.NO_ACTION
        )

    def test_no_action_stays_no_action(self) -> None:
        assert (
            RES.resolve_config(ActionPolicy.NO_ACTION, like_enabled=True, like_message_enabled=True)
            == ActionPolicy.NO_ACTION
        )

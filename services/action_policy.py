# ActionPolicyResolver: детерминированно сопоставляет Decision с политикой
# Telegram-действия (LIKE_ONLY / LIKE_AND_MESSAGE / DISLIKE_ONLY / NO_ACTION).
#
# ЧАСТЬ АРХИТЕКТУРЫ «Decision != Action»:
# DecisionService возвращает только LIKE/REVIEW/DISLIKE. Этот слой решает,
# ЧТО делать в Telegram. Telegram-free, deterministic, полностью тестируем.

from __future__ import annotations

from models.action import ActionPolicy
from models.decision import AIDecision


class ActionPolicyResolver:
    """Резолвер политики действий по Decision.

    Базовое сопоставление (ДО учёта конфига отдельно решается, выполнять ли
    действие — это делает исполнитель действий, зная mode и auto_actions):

    Decision  | informative            | ActionPolicy
    ----------|------------------------|---------------------------
    DISLIKE   | (неважно)              | DISLIKE_ONLY
    REVIEW    | (неважно)              | NO_ACTION (ждём человека)
    LIKE      | True (информативная)   | LIKE_AND_MESSAGE
    LIKE      | False (короткая)       | LIKE_ONLY

    Исполнитель (AutoActionEngine) затем применяет конфиг-гейты: в OBSERVE
    действия не выполняются, а LIKE_AND_MESSAGE требует и ``like.enabled``,
    и ``like_message.enabled`` (см. resolve_config).
    """

    def resolve(
        self,
        decision: AIDecision | None,
        informative: bool = False,
    ) -> ActionPolicy:
        """Сопоставляет Decision + информативность → базовую ActionPolicy."""
        if decision is None:
            return ActionPolicy.NO_ACTION
        if decision == AIDecision.DISLIKE:
            return ActionPolicy.DISLIKE_ONLY
        if decision == AIDecision.REVIEW:
            return ActionPolicy.NO_ACTION
        # LIKE
        if informative:
            return ActionPolicy.LIKE_AND_MESSAGE
        return ActionPolicy.LIKE_ONLY

    def resolve_config(
        self,
        policy: ActionPolicy,
        *,
        like_enabled: bool,
        like_message_enabled: bool,
    ) -> ActionPolicy:
        """Применяет конфиг-гейты (LIKE и LIKE_MESSAGE — ОТДЕЛЬНЫЕ настройки).

        Правила:
        - DISLIKE_ONLY выполняется всегда (конфиг-гейт на LIKE его не трогает).
        - LIKE_ONLY → LIKE_ONLY только если ``like_enabled``, иначе NO_ACTION.
        - LIKE_AND_MESSAGE: если и ``like_enabled`` и ``like_message_enabled`` —
          полная цепочка; если только ``like_enabled`` — деградируем к LIKE_ONLY;
          иначе NO_ACTION.
        - NO_ACTION остаётся NO_ACTION.

        Mode-гейт (OBSERVE → NO_ACTION) применяется отдельно исполнителем.
        """
        if policy == ActionPolicy.DISLIKE_ONLY:
            return ActionPolicy.DISLIKE_ONLY
        if policy == ActionPolicy.LIKE_ONLY:
            return ActionPolicy.LIKE_ONLY if like_enabled else ActionPolicy.NO_ACTION
        if policy == ActionPolicy.LIKE_AND_MESSAGE:
            if like_message_enabled and like_enabled:
                return ActionPolicy.LIKE_AND_MESSAGE
            if like_enabled:
                return ActionPolicy.LIKE_ONLY
            return ActionPolicy.NO_ACTION
        return ActionPolicy.NO_ACTION

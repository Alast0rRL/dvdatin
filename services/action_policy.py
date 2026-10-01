# ActionPolicyResolver: детерминированно сопоставляет Decision с политикой
# Telegram-действия (LIKE_AND_MESSAGE / DISLIKE_ONLY / NO_ACTION).
# Обычного лайка без сообщения (LIKE_ONLY) больше НЕТ: LIKE всегда
# превращается в цепочку «Берем)», иначе действие не выполняется.
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
    LIKE      | (неважно)              | LIKE_AND_MESSAGE

    Исполнитель (AutoActionEngine) затем применяет конфиг-гейты: в OBSERVE
    действия не выполняются, а LIKE_AND_MESSAGE требует и ``like.enabled``,
    и ``like_message.enabled`` (см. resolve_config).
    """

    def resolve(
        self,
        decision: AIDecision | None,
        informative: bool = False,
    ) -> ActionPolicy:
        """Сопоставляет Decision → базовую ActionPolicy.

        ``informative`` оставлен для совместимости вызовов и НЕ влияет на
        результат: голый ❤️ удалён, поэтому и короткая, и информативная
        анкета дают LIKE_AND_MESSAGE (цепочка «Берем)»).
        """
        if decision is None:
            return ActionPolicy.NO_ACTION
        if decision == AIDecision.DISLIKE:
            return ActionPolicy.DISLIKE_ONLY
        if decision == AIDecision.REVIEW:
            return ActionPolicy.NO_ACTION
        # LIKE — только с сообщением.
        return ActionPolicy.LIKE_AND_MESSAGE

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
        - LIKE_AND_MESSAGE требует и ``like_enabled``, и ``like_message_enabled``;
          иначе NO_ACTION — деградации до голого ❤️ больше нет.
        - NO_ACTION остаётся NO_ACTION.

        Mode-гейт (OBSERVE → NO_ACTION) применяется отдельно исполнителем.
        """
        if policy == ActionPolicy.DISLIKE_ONLY:
            return ActionPolicy.DISLIKE_ONLY
        if policy == ActionPolicy.LIKE_AND_MESSAGE:
            if like_message_enabled and like_enabled:
                return ActionPolicy.LIKE_AND_MESSAGE
            return ActionPolicy.NO_ACTION
        return ActionPolicy.NO_ACTION

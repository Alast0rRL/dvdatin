# Pydantic-модели для Human Review (Stage 6).
# Слой ручной рецензии поверх AI Decision Engine. Только OBSERVE/REVIEW.

from __future__ import annotations

from enum import StrEnum

class HumanDecision(StrEnum):
    """Решение человека по AI-решению профиля.

    APPROVE — человек согласен с AI-решением.
    REJECT  — человек считает AI-решение неправильным.
    SKIP    — человек не хочет сейчас принимать решение (НЕ ошибка AI).
    """

    APPROVE = "APPROVE"
    REJECT = "REJECT"
    SKIP = "SKIP"


class AgreementStatus(StrEnum):
    """Вычисляемый статус согласия AI ↔ Human."""

    AGREEMENT = "AGREEMENT"
    DISAGREEMENT = "DISAGREEMENT"
    UNRESOLVED = "UNRESOLVED"

    @classmethod
    def from_human(cls, decision: HumanDecision) -> "AgreementStatus":
        """Определяет статус согласия по решению человека.

        APPROVE → AGREEMENT (согласен с выбранным AI-профилем).
        REJECT  → DISAGREEMENT (считает выбор AI неправильным).
        SKIP    → UNRESOLVED (не принято решение — не ошибка AI).
        """
        if decision == HumanDecision.APPROVE:
            return cls.AGREEMENT
        if decision == HumanDecision.REJECT:
            return cls.DISAGREEMENT
        return cls.UNRESOLVED

# Pydantic-модели для Human Review (Stage 6).
# Слой ручной рецензии поверх AI Decision Engine. Только OBSERVE/REVIEW.

from __future__ import annotations

import json
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


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


class HumanDecisionResult(BaseModel):
    """Результат ручной рецензии.

    Поля:
        id: ID записи в таблице human_decisions (0 до сохранения).
        profile_id: ID профиля.
        ai_decision_id: ID AI-решения (внешний ключ на ai_decisions.id).
        decision: Решение человека (APPROVE/REJECT/SKIP).
        agreement: Вычисляемый статус согласия (AGREEMENT/DISAGREEMENT/UNRESOLVED).
        created_at: Время рецензии (ISO).
    """

    id: int = 0
    profile_id: int = 0
    ai_decision_id: int = 0
    decision: HumanDecision = HumanDecision.SKIP
    agreement: AgreementStatus = AgreementStatus.UNRESOLVED
    created_at: str = ""

    model_config = ConfigDict(use_enum_values=True)


class HumanReview(BaseModel):
    """Расширенная запись для UI: решение + присоединённые данные AI.

    Собирается из human_decisions JOIN ai_decisions для отображения
    согласия/статистики без обращения к Telegram.
    """

    human: HumanDecisionResult
    ai_decision_id: int = 0
    ai_decision: str = ""
    combined_score: float = 0.0
    confidence: float = 0.0
    scoring_version: str = "v1"

    def reasons_json(self) -> str:
        """Сериализует причины в JSON (заглушка для симметрии с решениями)."""
        return json.dumps([], ensure_ascii=False)

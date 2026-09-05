# Pydantic-модели для AI Decision Engine (Stage 5).
# Decision — отдельный слой поверх AI scoring. Не равен score.

from __future__ import annotations

import json
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator


class AIDecision(StrEnum):
    """Решение AI по профилю.

    В OBSERVE-режиме решение ТОЛЬКО сохраняется и логируется.
    Никаких Telegram-действий decision не выполняет.
    """

    LIKE = "LIKE"
    REVIEW = "REVIEW"
    DISLIKE = "DISLIKE"


class AIDecisionResult(BaseModel):
    """Результат работы Decision Engine.

    Поля:
        id: ID записи в таблице ai_decisions (0 до сохранения).
        profile_id: ID профиля.
        decision: Финальное решение (LIKE/REVIEW/DISLIKE).
        combined_score: Объединённый скор (0.0-1.0).
        confidence: Уверенность решения (0.0-1.0).
        reasons: Причины решения.
        evaluated_at: Время оценки (ISO).
        scoring_version: Версия скоринга (например "deterministic-v2").
    """

    id: int = 0
    profile_id: int = 0
    decision: AIDecision = AIDecision.REVIEW
    combined_score: float = 0.0
    confidence: float = 0.0
    reasons: list[str] = []
    evaluated_at: str = ""
    scoring_version: str = "v1"

    model_config = ConfigDict(use_enum_values=False)

    @field_validator("combined_score", "confidence")
    @classmethod
    def score_in_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            msg = "Score должен быть от 0.0 до 1.0"
            raise ValueError(msg)
        return v

    def reasons_json(self) -> str:
        """Сериализует причины в JSON."""
        return json.dumps(self.reasons, ensure_ascii=False)

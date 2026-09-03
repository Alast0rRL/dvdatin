# Pydantic-модели для AI Scoring (детерминированный scoring, Stage 8).
# LLM/CLIP-зависимые модели (LLMScore, CLIPScore) УСТАРЕЛИ и не используются
# в текущей архитектуре. AIScore сохранён для обратной совместимости с БД.

from __future__ import annotations

import json
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator


class AIRecommendation(StrEnum):
    """Рекомендация AI по анкете."""

    LIKE = "LIKE"
    DISLIKE = "DISLIKE"
    REVIEW = "REVIEW"


class ProfileStatus(StrEnum):
    """Статус достаточности данных по анкете.

    Вычисляется из извлечённых признаков (детерминировано).
    """

    SUFFICIENT_DATA = "SUFFICIENT_DATA"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class HardNegative(BaseModel):
    """Подтверждённый жёсткий негатив (детерминированно извлечённый)."""

    criterion: str
    evidence: str = ""

    model_config = ConfigDict(use_enum_values=False)


class PositiveFactor(BaseModel):
    """Разрешённый положительный фактор (детерминированно извлечённый)."""

    criterion: str
    evidence: str = ""

    model_config = ConfigDict(use_enum_values=False)


class ConfidenceLevel(StrEnum):
    """Уровень уверенности."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class CLIPScore(BaseModel):
    """Результат анализа изображений через CLIP (УСТАРЕЛО)."""

    image_count: int = 0
    aesthetic_score: float = 0.0
    nsfw_score: float = 0.0
    description: str = ""
    model_version: str = ""

    model_config = ConfigDict(use_enum_values=False)

    @field_validator("aesthetic_score", "nsfw_score")
    @classmethod
    def score_in_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            msg = "Score должен быть от 0.0 до 1.0"
            raise ValueError(msg)
        return v


class LLMScore(BaseModel):
    """Результат оценки анкеты (УСТАРЕЛО — не используется)."""

    score: float = 0.0
    recommendation: AIRecommendation = AIRecommendation.REVIEW
    confidence: float = 0.0
    reasons: list[str] = []
    hard_negatives: list[HardNegative] = []
    positive_factors: list[PositiveFactor] = []
    unknown: list[str] = []
    status: ProfileStatus = ProfileStatus.INSUFFICIENT_DATA
    raw_response: str = ""
    model_version: str = ""
    prompt_version: str = "deterministic-v2"

    @field_validator("score", "confidence")
    @classmethod
    def score_in_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            msg = "Score должен быть от 0.0 до 1.0"
            raise ValueError(msg)
        return v


class AIScore(BaseModel):
    """Единый результат AI-анализа профиля (совместимость с БД)."""

    profile_id: int = 0
    clip_score: float | None = None
    llm_score: float | None = None
    combined_score: float = 0.0
    recommendation: AIRecommendation = AIRecommendation.REVIEW
    confidence: ConfidenceLevel = ConfidenceLevel.LOW
    confidence_score: float = 0.0
    reasons: list[str] = []
    hard_negatives: list[HardNegative] = []
    positive_factors: list[PositiveFactor] = []
    unknown: list[str] = []
    status: ProfileStatus = ProfileStatus.INSUFFICIENT_DATA
    model_version: str = ""
    created_at: str = ""
    prompt_version: str = "deterministic-v2"

    def reasons_json(self) -> str:
        """Сериализует причины в JSON."""
        return json.dumps(self.reasons, ensure_ascii=False)

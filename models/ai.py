# Pydantic-модели для AI Scoring: CLIP, LLM, объединённый скор.

from __future__ import annotations

import json
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator


class AIRecommendation(StrEnum):
    """Рекомендация AI по анкете."""

    LIKE = "LIKE"
    DISLIKE = "DISLIKE"
    REVIEW = "REVIEW"


class ConfidenceLevel(StrEnum):
    """Уровень уверенности."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class CLIPScore(BaseModel):
    """Результат анализа изображений через CLIP."""

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
    """Результат оценки анкеты через LLM."""

    score: float = 0.0
    recommendation: AIRecommendation = AIRecommendation.REVIEW
    confidence: float = 0.0
    reasons: list[str] = []
    raw_response: str = ""
    model_version: str = ""
    prompt_version: str = "llm-v1"

    @field_validator("score", "confidence")
    @classmethod
    def score_in_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            msg = "Score должен быть от 0.0 до 1.0"
            raise ValueError(msg)
        return v


class AIScore(BaseModel):
    """Единый результат AI-анализа анкеты."""

    profile_id: int = 0
    clip_score: float | None = None
    llm_score: float | None = None
    combined_score: float = 0.0
    recommendation: AIRecommendation = AIRecommendation.REVIEW
    confidence: ConfidenceLevel = ConfidenceLevel.LOW
    confidence_score: float = 0.0
    reasons: list[str] = []
    model_version: str = ""
    created_at: str = ""
    prompt_version: str = "llm-v1"

    def reasons_json(self) -> str:
        """Сериализует причины в JSON."""
        return json.dumps(self.reasons, ensure_ascii=False)

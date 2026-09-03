# Модели для детерминированного скоринга: признаки, результаты, решения.
# Заменяет LLM-зависимые модели (LLMScore, HardNegative, PositiveFactor).
# Telegram-free, deterministic, fully testable.

from __future__ import annotations

import json
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator


class FeatureType(StrEnum):
    """Тип признака."""

    HARD_NEGATIVE = "hard_negative"
    POSITIVE = "positive"
    NEUTRAL = "neutral"


class Feature(BaseModel):
    """Детерминированный признак, извлечённый из текста анкеты.

    Attributes:
        code: Уникальный код правила (H01, P01 и т.д.).
        type: Тип признака.
        name: Читаемое имя правила.
        value: Значение (True — признак обнаружен).
        evidence: Точная цитата из анкеты (обязательно).
        source: Источник (description, name, city и т.д.).
    """

    code: str
    type: FeatureType
    name: str
    value: bool = True
    evidence: str = ""
    source: str = "description"

    model_config = ConfigDict(use_enum_values=False)


class ScoringStatus(StrEnum):
    """Статус результата скоринга."""

    SUFFICIENT_DATA = "SUFFICIENT_DATA"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class ScoringResult(BaseModel):
    """Результат детерминированного скоринга профиля.

    Не зависит от LLM/CLIP/Telegram. Полностью воспроизводим.
    """

    profile_id: int = 0
    score: float = 0.0
    hard_negatives: list[Feature] = []
    positive_factors: list[Feature] = []
    status: ScoringStatus = ScoringStatus.INSUFFICIENT_DATA
    scoring_version: str = "deterministic-v2"

    @field_validator("score")
    @classmethod
    def score_in_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            msg = "Score должен быть от 0.0 до 1.0"
            raise ValueError(msg)
        return v

    def reasons_json(self) -> str:
        """Сериализует причины в JSON."""
        reasons = []
        for f in self.hard_negatives:
            reasons.append({"code": f.code, "type": "hard_negative", "name": f.name, "evidence": f.evidence})
        for f in self.positive_factors:
            reasons.append({"code": f.code, "type": "positive", "name": f.name, "evidence": f.evidence})
        return json.dumps(reasons, ensure_ascii=False)

    def reasons_flat(self) -> list[str]:
        """Плоский список причин для совместимости со старым API."""
        reasons = []
        for f in self.hard_negatives:
            ev = f"«{f.evidence}»" if f.evidence else ""
            reasons.append(f"HARD_NEGATIVE:{f.name}:{ev}")
        for f in self.positive_factors:
            ev = f"«{f.evidence}»" if f.evidence else ""
            reasons.append(f"POSITIVE:{f.name}:{ev}")
        if not reasons:
            reasons.append("NO_FEATURES_FOUND")
        return reasons


# Версия scoring-системы (сохраняется в БД).
SCORING_VERSION = "deterministic-v2"

# Pydantic-модели для результатов фильтрации.

from __future__ import annotations

import json
from enum import StrEnum

from pydantic import BaseModel


class FilterDecision(StrEnum):
    """Решение фильтра."""

    PASS = "PASS"
    REJECT = "REJECT"
    REVIEW = "REVIEW"


class FilterReason(StrEnum):
    """Причины фильтрации."""

    AGE_OK = "AGE_OK"
    CITY_OK = "CITY_OK"
    AGE_OUT_OF_RANGE = "AGE_OUT_OF_RANGE"
    CITY_OUT_OF_RANGE = "CITY_OUT_OF_RANGE"
    AGE_UNKNOWN = "AGE_UNKNOWN"
    CITY_UNKNOWN = "CITY_UNKNOWN"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class FilterResult(BaseModel):
    """Результат оценки профиля фильтром."""

    profile_id: int = 0
    decision: FilterDecision = FilterDecision.REVIEW
    reasons: list[FilterReason] = []
    rules_checked: int = 0
    evaluated_at: str = ""

    def reasons_json(self) -> str:
        """Сериализует причины в JSON."""
        return json.dumps([r.value for r in self.reasons], ensure_ascii=False)

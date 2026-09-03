# LLM Service: оценка анкет через LLM.
# Абстрактный интерфейс для замены провайдера. Не знает о Telegram.

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from loguru import logger
from pydantic import ValidationError

from models.ai import (
    AIRecommendation,
    HardNegative,
    LLMScore,
    PositiveFactor,
    ProfileStatus,
)

if TYPE_CHECKING:
    from app.config import LLMConfig

#: Нейтральный скор для анкеты без hard-negatives/positive-factors.
#: Выбран так, чтобы combined (при CLIP off) попадал в зону REVIEW, а не
#: проваливался в BELOW_THRESHOLDS (автоматический DISLIKE неясной анкеты).
NEUTRAL_SCORE = 0.6
#: Скор для hard-negative анкеты (сам по себе не нужен для решения —
#: DISLIKE выносится по факту обнаруженного критерия, см. DecisionService).
HARD_NEGATIVE_SCORE = 0.1
#: Скор для анкеты с подтверждённым positive-factor.
POSITIVE_SCORE = 0.9
#: Версия промпта (bump при изменении контракта извлечения признаков).
PROMPT_VERSION = "llm-v3"

#: Строгий whitelist разрешённых H-критериев (llm-v3 контракт).
HARD_NEGATIVE_CODES: frozenset[str] = frozenset({
    "H1", "H2", "H3", "H4", "H5", "H6", "H7", "H8",
})

#: Строгий whitelist разрешённых P-критериев (llm-v3 контракт).
POSITIVE_FACTOR_CODES: frozenset[str] = frozenset({
    "P1", "P2", "P3", "P4",
})


def detect_score_status(
    hard_negatives: list[HardNegative],
    positive_factors: list[PositiveFactor],
) -> tuple[float, ProfileStatus]:
    """Детерминированный скор и статус по извлечённым признакам.

    Правило (не зависит от мнения LLM):
    - есть hard negative → низкий скор, статус SUFFICIENT_DATA;
    - есть positive factor → высокий скор, SUFFICIENT_DATA;
    - иначе → нейтральный скор, INSUFFICIENT_DATA.
    """
    if hard_negatives:
        return HARD_NEGATIVE_SCORE, ProfileStatus.SUFFICIENT_DATA
    if positive_factors:
        return POSITIVE_SCORE, ProfileStatus.SUFFICIENT_DATA
    return NEUTRAL_SCORE, ProfileStatus.INSUFFICIENT_DATA


class BaseLLMService(ABC):
    """Абстрактный базовый класс для LLM-сервиса."""

    @abstractmethod
    async def evaluate_profile(
        self,
        name: str,
        age: int | None,
        city: str,
        description: str,
    ) -> LLMScore:
        """Оценивает анкету через LLM.

        Args:
            name: Имя анкеты.
            age: Возраст.
            city: Город.
            description: Описание анкеты.

        Returns:
            LLMScore с результатами оценки.
        """
        ...


class LLMService(BaseLLMService):
    """Реализация LLM-сервиса с поддержкой отключения и mock."""

    def __init__(self, config: LLMConfig) -> None:
        self._config = config

    @property
    def is_enabled(self) -> bool:
        """Проверяет, включён ли LLM."""
        return self._config.enabled

    async def evaluate_profile(
        self,
        name: str,
        age: int | None,
        city: str,
        description: str,
    ) -> LLMScore:
        """Оценивает анкету через LLM.

        Если LLM отключён — возвращает REVIEW с нулевым скором.
        """
        if not self.is_enabled:
            logger.debug("LLM: отключён в конфигурации")
            return LLMScore(
                score=0.0,
                recommendation=AIRecommendation.REVIEW,
                confidence=0.0,
                reasons=["LLM отключён"],
                model_version="disabled",
            )

        try:
            return await self._call_llm(name, age, city, description)
        except Exception as e:
            logger.error(f"LLM вызов не удался: {e}")
            return LLMScore(
                score=0.0,
                recommendation=AIRecommendation.REVIEW,
                confidence=0.0,
                reasons=[f"LLM ошибка: {e}"],
                model_version=self._config.model,
            )

    async def _call_llm(
        self,
        name: str,
        age: int | None,
        city: str,
        description: str,
    ) -> LLMScore:
        """Внутренняя реализация вызова LLM.

        Заглушка: возвращает базовые значения.
        Подключается реальный API при установке зависимостей.
        """
        prompt = self._build_prompt(name, age, city, description)
        logger.debug(
            f"LLM: оценка анкеты (model={self._config.model}), "
            f"prompt_len={len(prompt)}"
        )

        raw_response = json.dumps({
            "score": NEUTRAL_SCORE,
            "confidence": 0.5,
            "reasons": ["stub extractor"],
            "hard_negatives": [],
            "positive_factors": [],
            "unknown": [],
            "status": "INSUFFICIENT_DATA",
        }, ensure_ascii=False)

        return self._parse_response(raw_response)

    def _build_prompt(
        self,
        name: str,
        age: int | None,
        city: str,
        description: str,
    ) -> str:
        """Формирует промпт для LLM (извлечение признаков, не судейство)."""
        parts = [
            "Извлеки из анкеты только разрешённые признаки (не оценивай анкету):",
            f"Имя: {name}",
            f"Возраст: {age if age else 'неизвестен'}",
            f"Город: {city if city else 'неизвестен'}",
            f"Описание: {description if description else 'нет описания'}",
            "",
            "Верни JSON: {\"hard_negatives\":[{\"criterion\":\"...\",\"evidence\":\"...\"}], "
            "\"positive_factors\":[{\"criterion\":\"...\",\"evidence\":\"...\"}], "
            "\"unknown\":[\"...\"], \"confidence\": 0.0-1.0}",
        ]
        return "\n".join(parts)

    def _parse_response(self, raw: str) -> LLMScore:
        """Парсит ответ LLM в LLMScore."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning(f"LLM: невалидный JSON: {e}")
            return LLMScore(
                score=0.0,
                recommendation=AIRecommendation.REVIEW,
                confidence=0.0,
                reasons=["Невалидный JSON от LLM"],
                raw_response=raw,
                model_version=self._config.model,
            )

        return parse_feature_response(
            data, raw, self._config.model, PROMPT_VERSION,
        )


def _criterion_code(criterion: str) -> str:
    """Извлекает базовый код критерия (до ':') из строки.

    Примеры: ``"H1:not_looking"`` → ``"H1"``, ``"P3"`` → ``"P3"``.
    """
    return criterion.split(":", 1)[0].strip() if criterion else ""


def parse_feature_response(
    data: dict,
    raw: str,
    model_version: str,
    prompt_version: str,
) -> LLMScore:
    """Извлекает из JSON-ответа LLM признаки и детерминированный скор.

    Ответ не является «мнением модели»: жёсткие/положительные признаки берутся
    из структуры, а score вычисляется по правилу (см. ``detect_score_status``).

    Неизвестные критерии (вне H1-H8 / P1-P4) игнорируются и логируются,
    но НЕ попадают в итоговые features/reasons и НЕ влияют на score/decision.
    """
    try:
        hard_raw = data.get("hard_negatives", []) or []
        pos_raw = data.get("positive_factors", []) or []

        hard_negatives: list[HardNegative] = []
        for h in hard_raw:
            if not isinstance(h, dict):
                continue
            criterion = str(h.get("criterion", ""))
            code = _criterion_code(criterion)
            if code not in HARD_NEGATIVE_CODES:
                logger.debug(
                    f"LLM: неизвестный H-критерий '{criterion}' — игнорируется"
                )
                continue
            hard_negatives.append(HardNegative(
                criterion=criterion,
                evidence=str(h.get("evidence", "")),
            ))

        positive_factors: list[PositiveFactor] = []
        for p in pos_raw:
            if not isinstance(p, dict):
                continue
            criterion = str(p.get("criterion", ""))
            code = _criterion_code(criterion)
            if code not in POSITIVE_FACTOR_CODES:
                logger.debug(
                    f"LLM: неизвестный P-критерий '{criterion}' — игнорируется"
                )
                continue
            positive_factors.append(PositiveFactor(
                criterion=criterion,
                evidence=str(p.get("evidence", "")),
            ))
        unknown = [
            str(u) for u in (data.get("unknown", []) or [])
            if isinstance(u, str) and u
        ]

        score, status = detect_score_status(hard_negatives, positive_factors)
        reasons = _feature_reasons(hard_negatives, positive_factors, unknown)

        return LLMScore(
            score=score,
            confidence=float(data.get("confidence", 0.0)),
            reasons=reasons,
            hard_negatives=hard_negatives,
            positive_factors=positive_factors,
            unknown=unknown,
            status=status,
            raw_response=raw,
            model_version=model_version,
            prompt_version=prompt_version,
        )
    except (ValidationError, KeyError, TypeError, ValueError) as e:
        logger.warning(f"LLM: ошибка валидации ответа: {e}")
        return LLMScore(
            score=0.0,
            recommendation=AIRecommendation.REVIEW,
            confidence=0.0,
            reasons=[f"Ошибка валидации: {e}"],
            raw_response=raw,
            model_version=model_version,
            prompt_version=prompt_version,
        )


def _feature_reasons(
    hard_negatives: list[HardNegative],
    positive_factors: list[PositiveFactor],
    unknown: list[str],
) -> list[str]:
    """reasons — только наблюдаемые факты (критерий + evidence)."""
    reasons: list[str] = []
    for h in hard_negatives:
        ev = f": «{h.evidence}»" if h.evidence else ""
        reasons.append(f"skip:{h.criterion}{ev}")
    for p in positive_factors:
        ev = f": «{p.evidence}»" if p.evidence else ""
        reasons.append(f"like:{p.criterion}{ev}")
    if unknown:
        reasons.append(f"unknown:{','.join(unknown)}")
    if not reasons:
        reasons.append("insufficient_data")
    return reasons


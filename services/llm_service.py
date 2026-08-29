# LLM Service: оценка анкет через LLM.
# Абстрактный интерфейс для замены провайдера. Не знает о Telegram.

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from loguru import logger
from pydantic import ValidationError

from models.ai import AIRecommendation, LLMScore

if TYPE_CHECKING:
    from app.config import LLMConfig


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

        raw_response = (
            '{"score": 0.5, "recommendation": "REVIEW", '
            '"confidence": 0.5, "reasons": ["stub analysis"]}'
        )

        return self._parse_response(raw_response)

    def _build_prompt(
        self,
        name: str,
        age: int | None,
        city: str,
        description: str,
    ) -> str:
        """Формирует промпт для LLM."""
        parts = [
            f"Оцени анкету для знакомства:",
            f"Имя: {name}",
            f"Возраст: {age if age else 'неизвестен'}",
            f"Город: {city if city else 'неизвестен'}",
            f"Описание: {description if description else 'нет описания'}",
            "",
            "Верни JSON: {\"score\": 0.0-1.0, \"recommendation\": \"LIKE/DISLIKE/REVIEW\", "
            "\"confidence\": 0.0-1.0, \"reasons\": [\"...\"]}",
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

        try:
            return LLMScore(
                score=data.get("score", 0.0),
                recommendation=AIRecommendation(
                    data.get("recommendation", "REVIEW")
                ),
                confidence=data.get("confidence", 0.0),
                reasons=data.get("reasons", []),
                raw_response=raw,
                model_version=self._config.model,
            )
        except (ValidationError, KeyError) as e:
            logger.warning(f"LLM: ошибка валидации ответа: {e}")
            return LLMScore(
                score=0.0,
                recommendation=AIRecommendation.REVIEW,
                confidence=0.0,
                reasons=[f"Ошибка валидации: {e}"],
                raw_response=raw,
                model_version=self._config.model,
            )

# CLIP Service: анализ изображений через CLIP.
# Абстрактный интерфейс для замены модели. Не знает о Telegram.

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from loguru import logger

from models.ai import CLIPScore

if TYPE_CHECKING:
    from app.config import CLIPConfig


class BaseCLIPService(ABC):
    """Абстрактный базовый класс для CLIP-сервиса."""

    @abstractmethod
    async def score_images(self, image_data_list: list[bytes]) -> CLIPScore:
        """Оценивает список изображений.

        Args:
            image_data_list: Список байтов изображений.

        Returns:
            CLIPScore с результатами анализа.
        """
        ...


class CLIPService(BaseCLIPService):
    """Реализация CLIP-сервиса с поддержкой отключения и mock."""

    def __init__(self, config: CLIPConfig) -> None:
        self._config = config
        self._model = None

    @property
    def is_enabled(self) -> bool:
        """Проверяет, включён ли CLIP."""
        return self._config.enabled

    async def score_images(self, image_data_list: list[bytes]) -> CLIPScore:
        """Оценивает изображения.

        Если CLIP отключён или модель недоступна — возвращает score=0.0.
        """
        if not self.is_enabled:
            logger.debug("CLIP: отключён в конфигурации")
            return CLIPScore(
                image_count=len(image_data_list),
                aesthetic_score=0.0,
                nsfw_score=0.0,
                model_version="disabled",
            )

        if not image_data_list:
            return CLIPScore(
                image_count=0,
                aesthetic_score=0.0,
                nsfw_score=0.0,
                model_version=self._config.model,
            )

        try:
            return await self._analyze(image_data_list)
        except Exception as e:
            logger.error(f"CLIP анализ не удался: {e}")
            return CLIPScore(
                image_count=len(image_data_list),
                aesthetic_score=0.0,
                nsfw_score=0.0,
                model_version=self._config.model,
            )

    async def _analyze(self, image_data_list: list[bytes]) -> CLIPScore:
        """Внутренняя реализация анализа.

        Заглушка: возвращает базовые значения.
        Подключается реальная модель при установке зависимостей.
        """
        logger.debug(
            f"CLIP: анализ {len(image_data_list)} изображений "
            f"(model={self._config.model})"
        )

        return CLIPScore(
            image_count=len(image_data_list),
            aesthetic_score=0.5,
            nsfw_score=0.0,
            description="stub analysis",
            model_version=self._config.model,
        )

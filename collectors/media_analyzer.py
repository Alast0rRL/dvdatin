# Анализатор фото: стаб для будущей интеграции с CLIP/NSFW.

from __future__ import annotations

from loguru import logger


class MediaAnalyzer:
    """Анализатор изображений (заглушка)."""

    async def analyze(self, image_data: bytes) -> dict:
        """Анализирует изображение.

        Returns:
            Словарь с результатами анализа.
        """
        logger.debug("MediaAnalyzer: stub — анализ не реализован")
        return {
            "nsfw_score": 0.0,
            "descriptions": [],
            "analyzed": False,
        }

# Remote CLIP Client: обращение к удалённому AI-серверу для CLIP inference.
# Реализует BaseCLIPService, не знает о Telegram.

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from loguru import logger

from models.ai import CLIPScore
from services.clip_service import BaseCLIPService

if TYPE_CHECKING:
    from app.config import CLIPConfig, RemoteAIConfig


class RemoteCLIPClient(BaseCLIPService):
    """Клиент удалённого CLIP-сервиса через HTTP."""

    def __init__(
        self,
        config: CLIPConfig,
        remote_config: RemoteAIConfig,
    ) -> None:
        self._config = config
        self._remote = remote_config
        self._client: httpx.AsyncClient | None = None

    @property
    def is_enabled(self) -> bool:
        """Проверяет, включён ли CLIP."""
        return self._config.enabled

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Создаёт или возвращает существующий HTTP-клиент."""
        if self._client is None or self._client.is_closed:
            headers: dict[str, str] = {}
            api_key = self._remote.api_key_or_none()
            if api_key:
                headers["X-API-Key"] = api_key
            self._client = httpx.AsyncClient(
                base_url=self._remote.base_url,
                timeout=httpx.Timeout(self._remote.timeout),
                headers=headers,
            )
        return self._client

    async def score_images(self, image_data_list: list[bytes]) -> CLIPScore:
        """Оценивает изображения через удалённый CLIP-сервер."""
        if not self.is_enabled:
            logger.debug("Remote CLIP: отключён в конфигурации")
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

        last_error: Exception | None = None
        for attempt in range(1, self._remote.max_retries + 1):
            try:
                return await self._post_analyze(image_data_list)
            except httpx.TimeoutException as e:
                last_error = e
                logger.warning(
                    f"Remote CLIP timeout (attempt {attempt}/"
                    f"{self._remote.max_retries}): {e}"
                )
            except httpx.ConnectError as e:
                last_error = e
                logger.warning(
                    f"Remote CLIP connection error (attempt {attempt}/"
                    f"{self._remote.max_retries}): {e}"
                )
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status in (429, 500, 502, 503):
                    last_error = e
                    logger.warning(
                        f"Remote CLIP HTTP {status} (attempt {attempt}/"
                        f"{self._remote.max_retries})"
                    )
                else:
                    return self._handle_http_error(e, image_data_list)
            except Exception as e:
                return self._handle_unexpected_error(e, image_data_list)

        return self._handle_connection_failure(last_error, image_data_list)

    async def _post_analyze(
        self, image_data_list: list[bytes],
    ) -> CLIPScore:
        """Отправляет изображения и парсит ответ."""
        client = await self._ensure_client()

        files = []
        for i, img_data in enumerate(image_data_list):
            files.append(
                ("files", (f"image_{i}.jpg", img_data, "image/jpeg"))
            )

        response = await client.post("/v1/clip/analyze", files=files)
        response.raise_for_status()

        data = response.json()
        return self._parse_response(data, len(image_data_list))

    def _parse_response(self, data: dict, image_count: int) -> CLIPScore:
        """Парсит JSON-ответ сервера в CLIPScore.

        Сервер возвращает {clip_score, images_analyzed, images_failed, status}.
        clip_score маппится в aesthetic_score (памятка: CLIPScore из Stage 4).
        """
        try:
            aesthetic = float(data.get("clip_score", 0.0))
            nsfw = float(data.get("nsfw_score", 0.0))
            description = data.get("status", "")
            model_version = data.get("model_version", self._config.model)

            aesthetic = max(0.0, min(1.0, aesthetic))
            nsfw = max(0.0, min(1.0, nsfw))

            return CLIPScore(
                image_count=image_count,
                aesthetic_score=aesthetic,
                nsfw_score=nsfw,
                description=description,
                model_version=model_version,
            )
        except (KeyError, TypeError, ValueError) as e:
            logger.warning(f"Remote CLIP: ошибка парсинга ответа: {e}")
            return CLIPScore(
                image_count=image_count,
                aesthetic_score=0.0,
                nsfw_score=0.0,
                description=f"Ошибка парсинга: {e}",
                model_version=self._config.model,
            )

    def _handle_http_error(
        self,
        e: httpx.HTTPStatusError,
        image_data_list: list[bytes],
    ) -> CLIPScore:
        """Обрабатывает HTTP-ошибки, не подлежащие retry."""
        status = e.response.status_code
        logger.error(f"Remote CLIP: HTTP {status}")
        return CLIPScore(
            image_count=len(image_data_list),
            aesthetic_score=0.0,
            nsfw_score=0.0,
            description=f"CLIP HTTP ошибка: {status}",
            model_version=self._config.model,
        )

    def _handle_unexpected_error(
        self,
        e: Exception,
        image_data_list: list[bytes],
    ) -> CLIPScore:
        """Обрабатывает непредвиденные ошибки."""
        logger.error(f"Remote CLIP: непредвиденная ошибка: {e}")
        return CLIPScore(
            image_count=len(image_data_list),
            aesthetic_score=0.0,
            nsfw_score=0.0,
            description=f"CLIP ошибка: {e}",
            model_version=self._config.model,
        )

    def _handle_connection_failure(
        self,
        last_error: Exception | None,
        image_data_list: list[bytes],
    ) -> CLIPScore:
        """Обрабатывает полный отказ соединения."""
        reason = str(last_error) if last_error else "неизвестная ошибка"
        logger.error(
            f"Remote CLIP: сервер недоступен после retries: {reason}"
        )
        return CLIPScore(
            image_count=len(image_data_list),
            aesthetic_score=0.0,
            nsfw_score=0.0,
            description=f"CLIP сервер недоступен: {reason}",
            model_version=self._config.model,
        )

    async def health_check(self) -> bool:
        """Проверяет доступность AI-сервера."""
        try:
            client = await self._ensure_client()
            response = await client.get("/health")
            return response.status_code == 200
        except Exception as e:
            logger.warning(f"Remote CLIP health check failed: {e}")
            return False

    async def close(self) -> None:
        """Закрывает HTTP-клиент."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

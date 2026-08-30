# Remote LLM Client: обращение к удалённому AI-серверу для LLM inference.
# Реализует BaseLLMService, не знает о Telegram.

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
from loguru import logger
from pydantic import ValidationError

from models.ai import AIRecommendation, LLMScore
from services.llm_service import BaseLLMService

if TYPE_CHECKING:
    from app.config import LLMConfig, RemoteAIConfig

PROMPT_VERSION = "llm-v2"


class RemoteLLMClient(BaseLLMService):
    """Клиент удалённого LLM-сервиса через HTTP."""

    def __init__(
        self,
        config: LLMConfig,
        remote_config: RemoteAIConfig,
    ) -> None:
        self._config = config
        self._remote = remote_config
        self._client: httpx.AsyncClient | None = None

    @property
    def is_enabled(self) -> bool:
        """Проверяет, включён ли LLM."""
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

    async def evaluate_profile(
        self,
        name: str,
        age: int | None,
        city: str,
        description: str,
    ) -> LLMScore:
        """Оценивает анкету через удалённый LLM-сервер."""
        if not self.is_enabled:
            logger.debug("Remote LLM: отключён в конфигурации")
            return LLMScore(
                score=0.0,
                recommendation=AIRecommendation.REVIEW,
                confidence=0.0,
                reasons=["LLM отключён"],
                model_version="disabled",
            )

        payload = {
            "profile": {
                "name": name,
                "age": age,
                "city": city,
                "description": description,
            },
        }

        last_error: Exception | None = None
        for attempt in range(1, self._remote.max_retries + 1):
            try:
                return await self._post_evaluate(payload)
            except httpx.TimeoutException as e:
                last_error = e
                logger.warning(
                    f"Remote LLM timeout (attempt {attempt}/"
                    f"{self._remote.max_retries}): {e}"
                )
            except httpx.ConnectError as e:
                last_error = e
                logger.warning(
                    f"Remote LLM connection error (attempt {attempt}/"
                    f"{self._remote.max_retries}): {e}"
                )
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status in (429, 500, 502, 503):
                    last_error = e
                    logger.warning(
                        f"Remote LLM HTTP {status} (attempt {attempt}/"
                        f"{self._remote.max_retries})"
                    )
                else:
                    return self._handle_http_error(e)
            except Exception as e:
                return self._handle_unexpected_error(e)

        return self._handle_connection_failure(last_error)

    async def _post_evaluate(self, payload: dict) -> LLMScore:
        """Отправляет POST-запрос и парсит ответ."""
        client = await self._ensure_client()
        response = await client.post("/v1/llm/evaluate", json=payload)
        response.raise_for_status()

        data = response.json()
        return self._parse_response(data)

    def _parse_response(self, data: dict) -> LLMScore:
        """Парсит JSON-ответ сервера в LLMScore с валидацией."""
        try:
            score = float(data.get("score", 0.0))
            confidence = float(data.get("confidence", 0.0))
            reasons = data.get("reasons", [])

            score = max(0.0, min(1.0, score))
            confidence = max(0.0, min(1.0, confidence))

            if not isinstance(reasons, list):
                reasons = [str(reasons)]

            return LLMScore(
                score=score,
                recommendation=AIRecommendation.REVIEW,
                confidence=confidence,
                reasons=reasons,
                raw_response=json.dumps(data, ensure_ascii=False),
                model_version=self._config.model,
                prompt_version=PROMPT_VERSION,
            )
        except (ValidationError, KeyError, TypeError, ValueError) as e:
            logger.warning(f"Remote LLM: ошибка парсинга ответа: {e}")
            return LLMScore(
                score=0.0,
                recommendation=AIRecommendation.REVIEW,
                confidence=0.0,
                reasons=[f"Ошибка парсинга ответа: {e}"],
                raw_response=json.dumps(data, ensure_ascii=False),
                model_version=self._config.model,
            )

    def _handle_http_error(self, e: httpx.HTTPStatusError) -> LLMScore:
        """Обрабатывает HTTP-ошибки, не подлежащие retry."""
        status = e.response.status_code
        logger.error(f"Remote LLM: HTTP {status}")
        return LLMScore(
            score=0.0,
            recommendation=AIRecommendation.REVIEW,
            confidence=0.0,
            reasons=[f"LLM HTTP ошибка: {status}"],
            model_version=self._config.model,
        )

    def _handle_unexpected_error(self, e: Exception) -> LLMScore:
        """Обрабатывает непредвиденные ошибки."""
        logger.error(f"Remote LLM: непредвиденная ошибка: {e}")
        return LLMScore(
            score=0.0,
            recommendation=AIRecommendation.REVIEW,
            confidence=0.0,
            reasons=[f"LLM ошибка: {e}"],
            model_version=self._config.model,
        )

    def _handle_connection_failure(
        self, last_error: Exception | None,
    ) -> LLMScore:
        """Обрабатывает полный отказ соединения после всех retries."""
        reason = str(last_error) if last_error else "неизвестная ошибка"
        logger.error(f"Remote LLM: сервер недоступен после retries: {reason}")
        return LLMScore(
            score=0.0,
            recommendation=AIRecommendation.REVIEW,
            confidence=0.0,
            reasons=[f"LLM сервер недоступен: {reason}"],
            model_version=self._config.model,
        )

    async def health_check(self) -> bool:
        """Проверяет доступность AI-сервера."""
        try:
            client = await self._ensure_client()
            response = await client.get("/health")
            return response.status_code == 200
        except Exception as e:
            logger.warning(f"Remote LLM health check failed: {e}")
            return False

    async def close(self) -> None:
        """Закрывает HTTP-клиент."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

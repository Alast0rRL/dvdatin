# Парсер сообщений Дайвинчика: классификация и извлечение данных.
# Разделён от Collector: Collector = получение, Parser = обработка.

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from loguru import logger

from collectors.city_normalizer import normalize_city
from models.raw import FilterResult, MessageType, ParsedMatch, ParsedProfile

if TYPE_CHECKING:
    from app.config import FiltersConfig

# Паттерн анкеты: "Name, Age, City" с возможным описанием через разделитель
# Поддерживает: "сашк, 19, спб – описание", "Майя, 18, санкт петербург",
# "wimx, 18, Санкт-Петербург", "*)))^, 18, Санкт Петербург – ..."
PROFILE_RE = re.compile(
    r"^(?P<name>[^\n,]{1,50}?),\s*"
    r"(?P<age>\d{1,3}),\s*"
    r"(?P<city>[^\n,]+?)"
    r"(?:\s+[–—]\s+(?P<desc>.+))?$",
    re.DOTALL,
)

# Паттерн MATCH: "[Name](https://t.me/username)"
MATCH_RE = re.compile(
    r"\[(?P<name>[^\]]+)\]\(https?://t\.me/(?P<username>[^)\s?]+)",
)


class DvinchikParser:
    """Классификатор и парсер сообщений Дайвинчика."""

    def __init__(self, filters: FiltersConfig) -> None:
        self._filters = filters

    def classify(self, text: str, has_media: bool = False) -> MessageType:
        """Классифицирует сообщение по типу."""
        stripped = text.strip()

        if not stripped and has_media:
            return MessageType.MEDIA_ONLY

        if not stripped:
            return MessageType.SERVICE

        if PROFILE_RE.match(stripped):
            return MessageType.PROFILE

        if MATCH_RE.search(stripped):
            return MessageType.MATCH

        if self._looks_like_service(stripped):
            return MessageType.SERVICE

        return MessageType.UNKNOWN

    def parse_profile(
        self,
        text: str,
        source_message_id: int = 0,
        source_chat_id: int = 0,
    ) -> ParsedProfile:
        """Извлекает данные анкеты из текста."""
        match = PROFILE_RE.match(text.strip())
        if not match:
            return ParsedProfile(
                description=text,
                source_message_id=source_message_id,
                source_chat_id=source_chat_id,
            )

        name = match.group("name").strip()
        age = int(match.group("age"))
        raw_city = match.group("city").strip()
        description = (match.group("desc") or "").strip()
        normalized = normalize_city(raw_city)

        filter_result = self._check_filter(age, normalized)

        logger.info(
            f"Profile detected: name={name}, age={age}, "
            f"raw_city={raw_city}, normalized={normalized}"
        )

        return ParsedProfile(
            name=name,
            age=age,
            raw_city=raw_city,
            normalized_city=normalized,
            description=description,
            filter_result=filter_result,
            source_message_id=source_message_id,
            source_chat_id=source_chat_id,
        )

    def parse_match(self, text: str) -> ParsedMatch | None:
        """Извлекает данные из сообщения о взаимном лайке."""
        match = MATCH_RE.search(text)
        if not match:
            return None

        name = match.group("name").strip()
        username = match.group("username").strip()
        url = f"https://t.me/{username}"

        logger.info(f"Match detected: name={name}, username={username}")

        return ParsedMatch(
            name=name,
            telegram_username=username,
            telegram_url=url,
        )

    def _check_filter(self, age: int, city: str) -> FilterResult:
        """Проверяет анкету по фильтрам (только информационно)."""
        if (
            self._filters.age_min <= age <= self._filters.age_max
            and city in self._filters.city_allowed
        ):
            return FilterResult.FILTER_MATCH
        return FilterResult.FILTER_NOT_MATCH

    def _looks_like_service(self, text: str) -> bool:
        """Эвристика для сервисных сообщений."""
        service_keywords = [
            "лайк", "нравится", "мэтч", "совпадени",
            "написать", "напишите", "обоюдн", "начинай",
        ]
        text_lower = text.lower()
        return any(kw in text_lower for kw in service_keywords)

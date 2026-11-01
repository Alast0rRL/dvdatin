# Pydantic-модели для сырых сообщений и классифицированных данных.

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class MessageType(StrEnum):
    """Типы сообщений."""

    UNKNOWN = "UNKNOWN"
    PROFILE = "PROFILE"
    MEDIA_ONLY = "MEDIA_ONLY"
    SERVICE = "SERVICE"
    MATCH = "MATCH"


class FilterResult(StrEnum):
    """Результат фильтрации анкеты."""

    FILTER_MATCH = "FILTER_MATCH"
    FILTER_NOT_MATCH = "FILTER_NOT_MATCH"
    UNKNOWN = "UNKNOWN"


class ParsedProfile(BaseModel):
    """Распарсенная анкета."""

    name: str = ""
    age: int | None = None
    raw_city: str = ""
    normalized_city: str = ""
    description: str = ""
    filter_result: FilterResult = FilterResult.UNKNOWN
    source_message_id: int = 0
    source_chat_id: int = 0


class ParsedMatch(BaseModel):
    """Распарсенный матч."""

    name: str = ""
    telegram_username: str = ""
    telegram_url: str = ""
    #: Без @username Leo даёт ссылку tg://user?id=<id> — тогда пишем по id.
    telegram_user_id: int = 0

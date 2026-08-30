# Pydantic-модели для сырых сообщений и классифицированных данных.

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class MessageType(StrEnum):
    """Типы сообщений."""

    UNKNOWN = "UNKNOWN"
    PROFILE = "PROFILE"
    MEDIA_ONLY = "MEDIA_ONLY"
    SERVICE = "SERVICE"
    MATCH = "MATCH"
    OTHER = "OTHER"


class FilterResult(StrEnum):
    """Результат фильтрации анкеты."""

    FILTER_MATCH = "FILTER_MATCH"
    FILTER_NOT_MATCH = "FILTER_NOT_MATCH"
    UNKNOWN = "UNKNOWN"


class RawMessage(BaseModel):
    """Сырое Telegram-сообщение."""

    telegram_message_id: int
    chat_id: int
    sender_id: int
    sender_username: str = ""
    sender_name: str = ""
    message_date: datetime
    text: str = ""
    raw_entities: str = "[]"
    reply_markup: str = "[]"
    media_type: str = ""
    reply_to_message_id: int | None = None


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


class MessageGroup(BaseModel):
    """Группа сообщений, относящихся к одной анкете."""

    profile_message_id: int = 0
    media_message_ids: list[int] = []
    chat_id: int = 0

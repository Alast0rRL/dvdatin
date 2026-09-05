# Pydantic-модели для профилей и связанных сущностей.

from __future__ import annotations

import hashlib
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ProfileStatus(StrEnum):
    """Статусы жизненного цикла профиля."""

    NEW = "NEW"
    SEEN = "SEEN"
    LIKED = "LIKED"
    DISLIKED = "DISLIKED"
    MATCHED = "MATCHED"
    ARCHIVED = "ARCHIVED"


def compute_fingerprint(
    normalized_name: str,
    age: int,
    normalized_city: str,
) -> str:
    """Вычисляет deterministic fingerprint для профиля.

    Формируется из normalized_name + age + normalized_city.
    НЕ является гарантированным идентификатором человека.
    """
    raw = f"{normalized_name.lower().strip()}|{age}|{normalized_city.lower().strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Profile(BaseModel):
    """Полноценный профиль пользователя."""

    id: int = 0
    name: str
    age: int
    raw_city: str = ""
    normalized_city: str = ""
    description: str = ""
    fingerprint: str = ""

    source_chat_id: int = 0
    source_message_id: int = 0

    first_seen_at: str = ""
    last_seen_at: str = ""

    status: ProfileStatus = ProfileStatus.NEW

    message_count: int = 0

    model_config = ConfigDict(use_enum_values=True)

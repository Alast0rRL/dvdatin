# Модели action policy: независимый слой "что делать в Telegram".
# ВАЖНО: Decision (LIKE/REVIEW/DISLIKE) != Action (как реагировать в Telegram).
# DecisionService возвращает ТОЛЬКО LIKE/REVIEW/DISLIKE; этот слой решает,
# какое Telegram-действие соответствует. Telegram-free, deterministic.

from __future__ import annotations

from enum import StrEnum


class ActionPolicy(StrEnum):
    """Политика действий в Telegram.

    Это НЕ синоним Decision:
    - Decision=LIKE может соответствовать LIKE_ONLY (только ❤️) или
      LIKE_AND_MESSAGE (❤️ + "Берем)") в зависимости от профиля/конфига.
    - Decision=DISLIKE → DISLIKE_ONLY (👎, без сообщения).
    - Decision=REVIEW → NO_ACTION (ждём ручного решения владельца).
    - NO_ACTION также используется при OBSERVE / выключенных auto_actions.
    """

    NO_ACTION = "NO_ACTION"
    LIKE_ONLY = "LIKE_ONLY"
    LIKE_AND_MESSAGE = "LIKE_AND_MESSAGE"
    DISLIKE_ONLY = "DISLIKE_ONLY"

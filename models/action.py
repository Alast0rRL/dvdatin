# Модели action policy: независимый слой "что делать в Telegram".
# ВАЖНО: Decision (LIKE/REVIEW/DISLIKE) != Action (как реагировать в Telegram).
# DecisionService возвращает ТОЛЬКО LIKE/REVIEW/DISLIKE; этот слой решает,
# какое Telegram-действие соответствует. Telegram-free, deterministic.

from __future__ import annotations

from enum import StrEnum


class ActionPolicy(StrEnum):
    """Политика действий в Telegram.

    Это НЕ синоним Decision:
    - Decision=LIKE → ТОЛЬКО LIKE_AND_MESSAGE (❤️ + «Берем)»). Обычного
      лайка без сообщения больше нет: голое ❤️ оставляло анкету «висеть»
      без продолжения диалога (проблема сохранена на проде), поэтому
      LIKE_ONLY удалён как политика.
    - Decision=DISLIKE → DISLIKE_ONLY (👎, без сообщения).
    - Decision=REVIEW → NO_ACTION (ждём ручного решения владельца).
    - NO_ACTION также используется при OBSERVE / выключенных auto_actions,
      а также когда LIKE нельзя превратить в LIKE_AND_MESSAGE (выключен
      like / like_message, карточка без message_id).
    """

    NO_ACTION = "NO_ACTION"
    LIKE_AND_MESSAGE = "LIKE_AND_MESSAGE"
    DISLIKE_ONLY = "DISLIKE_ONLY"

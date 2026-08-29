# Дедупликация сообщений.

from __future__ import annotations

from loguru import logger


class Dedup:
    """Предотвращает обработку дубликатов сообщений."""

    def __init__(self, max_size: int = 10000) -> None:
        self._max_size = max_size
        self._seen: set[int] = set()

    def is_duplicate(self, message_id: int) -> bool:
        """Проверяет, было ли сообщение уже обработано (check + mark)."""
        if message_id in self._seen:
            logger.debug(f"Дубликат: message_id={message_id}")
            return True
        self._seen.add(message_id)
        if len(self._seen) > self._max_size:
            self._evict()
        return False

    def is_known(self, message_id: int) -> bool:
        """Только проверка БЕЗ добавления (dedup ДО save_raw_message).

        Используется в коллекторе: дубликат отбрасывается ДО попытки сохранения,
        но сам факт обработки фиксируется только ПОСЛЕ успешного RAW-save
        (см. ``mark``), чтобы не терять сообщения при сбое сохранения.
        """
        return message_id in self._seen

    def mark(self, message_id: int) -> None:
        """Помечает сообщение обработанным (вызывается ПОСЛЕ успешного save)."""
        self._seen.add(message_id)
        if len(self._seen) > self._max_size:
            self._evict()

    def _evict(self) -> None:
        """Удаляет старые записи при переполнении."""
        to_remove = len(self._seen) - self._max_size // 2
        if to_remove > 0:
            removed = 0
            for mid in list(self._seen):
                if removed >= to_remove:
                    break
                self._seen.discard(mid)
                removed += 1
            logger.debug(f"Dedup evicted {removed} old entries")

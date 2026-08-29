# Статистика коллектора.

from __future__ import annotations

import time

from loguru import logger


class CollectorStats:
    """Сбор статистики по обработанным сообщениям."""

    def __init__(self) -> None:
        self._start_time = time.time()
        self._total_messages = 0
        self._profiles = 0
        self._matches = 0
        self._media_only = 0
        self._service = 0
        self._unknown = 0
        self._filter_match = 0
        self._filter_not_match = 0

    def record_profile(self, filter_match: bool = False) -> None:
        """Регистрирует анкету."""
        self._profiles += 1
        if filter_match:
            self._filter_match += 1
        else:
            self._filter_not_match += 1
        self._total_messages += 1

    def record_match(self) -> None:
        """Регистрирует матч."""
        self._matches += 1
        self._total_messages += 1

    def record_media_only(self) -> None:
        """Регистрирует media-only."""
        self._media_only += 1
        self._total_messages += 1

    def record_service(self) -> None:
        """Регистрирует сервисное сообщение."""
        self._service += 1
        self._total_messages += 1

    def record_unknown(self) -> None:
        """Регистрирует неизвестное сообщение."""
        self._unknown += 1
        self._total_messages += 1

    @property
    def uptime(self) -> float:
        """Время работы в секундах."""
        return time.time() - self._start_time

    @property
    def summary(self) -> dict:
        """Сводка статистики."""
        return {
            "uptime_sec": round(self.uptime, 1),
            "total_messages": self._total_messages,
            "profiles": self._profiles,
            "matches": self._matches,
            "media_only": self._media_only,
            "service": self._service,
            "unknown": self._unknown,
            "filter_match": self._filter_match,
            "filter_not_match": self._filter_not_match,
        }

    def print_summary(self) -> None:
        """Выводит сводку в лог."""
        s = self.summary
        logger.info(
            f"Stats: {s['total_messages']} msgs, "
            f"{s['profiles']} profiles ({s['filter_match']} match), "
            f"{s['matches']} matches, "
            f"{s['media_only']} media, "
            f"uptime {s['uptime_sec']}s"
        )

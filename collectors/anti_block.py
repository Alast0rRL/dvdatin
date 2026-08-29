# Антиблокировка: rate-limiter для исходящих действий.

from __future__ import annotations

import asyncio
import random
import time

from loguru import logger


class RateLimiter:
    """Ограничивает частоту действий для избегания блокировки."""

    def __init__(
        self,
        max_per_day: int = 40,
        min_delay_sec: int = 5400,
        max_delay_sec: int = 14400,
    ) -> None:
        self._max_per_day = max_per_day
        self._min_delay = min_delay_sec
        self._max_delay = max_delay_sec
        self._action_times: list[float] = []
        self._lock = asyncio.Lock()

    async def wait_before_action(self) -> None:
        """Ждёт перед следующим действием."""
        async with self._lock:
            self._cleanup_old()
            delay = self._next_delay()
            logger.info(f"Rate limiter: жду {delay:.0f} сек")
            await asyncio.sleep(delay)
            self._action_times.append(time.time())

    def can_act(self) -> bool:
        """Проверяет, можно ли выполнить действие."""
        self._cleanup_old()
        return len(self._action_times) < self._max_per_day

    def _cleanup_old(self) -> None:
        """Удаляет записи старше 24 часов."""
        cutoff = time.time() - 86400
        self._action_times = [t for t in self._action_times if t > cutoff]

    def _next_delay(self) -> float:
        """Случайная задержка в пределах лимитов."""
        return random.uniform(self._min_delay, self._max_delay)

    @property
    def actions_today(self) -> int:
        """Количество действий за текущий день."""
        self._cleanup_old()
        return len(self._action_times)

    @property
    def remaining_today(self) -> int:
        """Сколько действий ещё доступно."""
        return max(0, self._max_per_day - self.actions_today)

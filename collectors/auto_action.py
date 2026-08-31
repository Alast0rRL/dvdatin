# Авто-действия (Stage 7, SEMI_AUTO): отправка ❤️/👎 на анкеты.
# Telegram-bound слой — работает только здесь (имеет доступ к клиентам).
# Решение (LIKE/DISLIKE/REVIEW) принимает DecisionService; этот модуль лишь
# выполняет действие (отправляет текст ❤️/👎) от имени настроенного аккаунта.

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from core.types import Mode
from models.decision import AIDecision

if TYPE_CHECKING:
    from telethon import TelegramClient

    from app.config import AutoActionsConfig

console = Console(force_terminal=True)

# Текст команд Дайвинчика (реверс механики):
LIKE_TEXT: str = "\u2764\ufe0f"      # ❤️ — лайк
DISLIKE_TEXT: str = "\U0001F44E"     # 👎 — дизлайк


class AutoActionError(Exception):
    """Ошибка выполнения авто-действия."""


class AutoActionEngine:
    """Выполняет действия (лайк/дизлайк) на анкетах от имени аккаунта.

    Гарантии:
    - Работает ТОЛЬКО если ``mode >= SEMI_AUTO`` и ``enabled`` в конфиге.
    - Интервальный rate-limiter по ``interval_sec`` (по умолчанию 10 сек = 6/мин),
      без жёсткого дневного лимита.
    - Ошибки отправки не роняют pipeline: перехватываются и логируются,
      RAW-сообщения не теряются.
    - REVIEW → пропуск (никакого действия).
    """

    def __init__(
        self,
        client: TelegramClient | None,
        config: AutoActionsConfig,
        mode: Mode,
        chat_id: int,
    ) -> None:
        self._client = client
        self._config = config
        self._mode = mode
        self._chat_id = chat_id
        self._last_action_at: float = 0.0
        self._actions: list[float] = []
        self._lock = asyncio.Lock()
        self._processed_profile_ids: set[int] = set()

    @property
    def mode(self) -> Mode:
        """Режим работы движка (OBSERVE/SEMI_AUTO/AUTO)."""
        return self._mode

    @mode.setter
    def mode(self, value: Mode) -> None:
        """Динамически меняет режим на лету (управление через ControlBot)."""
        self._mode = value

    @property
    def enabled(self) -> bool:
        """Гейт: авто-действия активны только при SEMI_AUTO/AUTO + enabled."""
        if self._mode not in (Mode.SEMI_AUTO, Mode.AUTO):
            return False
        if not self._config.enabled:
            return False
        if self._client is None:
            return False
        return True

    @property
    def client(self) -> TelegramClient | None:
        """Клиент, от имени которого выполняются действия."""
        return self._client

    @property
    def mode_label(self) -> str:
        return self._mode.value

    async def maybe_act(
        self, decision: AIDecision | None, profile_id: int | None = None,
    ) -> str:
        """Выполняет действие по решению (LIKE/DISLIKE), если он enabled.

        Возвращает строку-описание совершённого действия ("SKIP", "LIKE",
        "DISLIKE") либо слово "GATE" если авто-действия выключены.
        """
        if not self.enabled:
            return "GATE"
        if decision is None:
            return "SKIP"
        if decision == AIDecision.REVIEW:
            logger.info("AutoAction: REVIEW — пропуск (никакого действия)")
            return "SKIP"

        text = LIKE_TEXT if decision == AIDecision.LIKE else DISLIKE_TEXT
        action = decision.value
        async with self._lock:
            if profile_id is not None and profile_id in self._processed_profile_ids:
                logger.warning(f"AutoAction: profile={profile_id} уже обработан")
                return "DUPLICATE"
            await self._rate_limit_locked()
            await self._send(text)
            if profile_id is not None:
                self._processed_profile_ids.add(profile_id)
        logger.info(f"AutoAction: отправил {text!r} ({action}) на chat={self._chat_id}")
        self._print_action(action, text)
        return action

    async def start_stream(self) -> bool:
        """Отправляет явно настроенную команду открытия потока, если она есть."""
        if not self.enabled:
            return False
        if not self._config.start_command:
            logger.warning(
                "AutoAction: автозапуск потока отключён — команда не задана"
            )
            return False
        await self._send(self._config.start_command)
        logger.info(f"AutoAction: запущен поток анкет ({self._config.start_command!r})")
        return True

    async def _send(self, text: str) -> None:
        """Отправляет текст в чат от имени сконфигурированного клиента."""
        if self._client is None:
            msg = "AutoAction: клиент не задан"
            logger.warning(msg)
            raise AutoActionError(msg)
        try:
            await self._client.send_message(self._chat_id, text)
        except Exception as e:
            logger.error(f"AutoAction: ошибка отправки {text!r}: {e}")
            raise AutoActionError(str(e)) from e

    async def _rate_limit(self) -> None:
        """Интервальный rate-limiter: ждёт, пока не пройдёт interval_sec."""
        async with self._lock:
            await self._rate_limit_locked()

    async def _rate_limit_locked(self) -> None:
        """Применяет rate-limit при уже взятой блокировке движка."""
        now = time.time()
        if self._last_action_at:
            elapsed = now - self._last_action_at
            if elapsed < self._config.interval_sec:
                wait = self._config.interval_sec - elapsed
                logger.info(f"AutoAction: rate-limit, жду {wait:.1f} сек")
                await asyncio.sleep(wait)
        self._last_action_at = time.time()
        self._actions.append(self._last_action_at)

    def _print_action(self, action: str, text: str) -> None:
        color = "green" if action == "LIKE" else "red"
        table = Table(show_header=False, border_style="dim", padding=(0, 1))
        table.add_column("Key", style="bold magenta", width=14)
        table.add_column("Value")
        table.add_row("Chat ID", str(self._chat_id))
        table.add_row("Action", f"[bold {color}]{action}[/bold {color}]")
        table.add_row("Sent", repr(text))
        table.add_row("Mode", self.mode_label)
        console.print(Panel(
            table,
            title=f"[bold {color}]AUTO ACTION[/]",
            border_style=color,
        ))

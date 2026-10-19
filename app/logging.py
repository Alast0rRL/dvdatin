# Настройка логирования: Loguru для файлов, Rich для красивого вывода в консоль.

from __future__ import annotations

import logging
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any

from loguru import logger
from rich.console import Console

from app.config import AppConfig

LOG_DIR = Path("data/logs")
RUNTIME_LOG = LOG_DIR / "runtime.log"

# Сколько последних записей держим в памяти для веб-отладочной консоли.
LOG_BUFFER_SIZE = 500
# Уровень перехвата стороннего (stdlib) логирования: только проблемы,
# иначе Telethon/werkzeug зальют буфер INFO-шумом.
STDLIB_INTERCEPT_LEVEL = logging.WARNING

console = Console(force_terminal=True, file=sys.stderr)

# Кольцевой буфер последних записей — источник данных для отладочной консоли Веба.
# Читается из веб-потока, пишется из sinks loguru (тот же процесс, разные потоки).
LOG_BUFFER: deque[dict[str, Any]] = deque(maxlen=500)
_buffer_lock = threading.Lock()
_buffer_sink_id: int | None = None


def _buffer_sink(message: Any) -> None:
    """Складывает каждую запись Loguru в кольцевой буфер для веб-отладки.

    Loguru передаёт callable-синку объект ``Message`` (его ``record`` содержит
    уровень/имя логгера/текст) — формат применяется только через ``str()``.
    """
    record = message.record
    entry = {
        "ts": record["time"].strftime("%Y-%m-%d %H:%M:%S"),
        "level": record["level"].name,
        "no": record["level"].no,
        "name": record["name"],
        "message": record["message"],
    }
    with _buffer_lock:
        LOG_BUFFER.append(entry)


def _ensure_buffer_sink() -> None:
    """Регистрирует sink в память один раз за жизнь процесса."""
    global _buffer_sink_id
    if _buffer_sink_id is None:
        _buffer_sink_id = logger.add(_buffer_sink, level="DEBUG", format="{message}")


def get_recent_logs(limit: int = 200, min_level: str | None = None) -> list[dict[str, Any]]:
    """Возвращает последние логи из памяти для веб-консоли отладки.

    Args:
        limit: максимум строк (свежие в конце).
        min_level: минимальный уровень (``DEBUG``/``INFO``/``WARNING``/``ERROR``…);
            всё ниже отбрасывается. ``None`` — без фильтра.
    """
    _ensure_buffer_sink()
    threshold: int | None = None
    if min_level:
        try:
            threshold = logger.level(min_level.upper()).no
        except ValueError:
            threshold = None
    with _buffer_lock:
        items = list(LOG_BUFFER)
    if threshold is not None:
        items = [e for e in items if e["no"] >= threshold]
    if limit and len(items) > limit:
        items = items[-limit:]
    return items


class _InterceptHandler(logging.Handler):
    """Перенаправляет stdlib-логи (asyncio/telethon/werkzeug) в Loguru.

    Нужно, чтобы ошибки сторонних библиотек попадали в файл и в веб-консоль:
    Loguru не перехватывает stdlib ``logging`` сам по себе.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def _install_stdlib_interception() -> None:
    """Мостит stdlib ``logging`` в Loguru на уровне WARNING (только проблемы)."""
    logging.basicConfig(handlers=[_InterceptHandler()], level=logging.WARNING, force=True)


def setup_logging(config: AppConfig) -> None:
    """Настраивает Loguru-логгеры: консоль через Rich + runtime.log.

    Args:
        config: Конфигурация приложения.
    """
    global _buffer_sink_id
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger.remove()
    _buffer_sink_id = None

    logger.add(
        lambda msg: console.print(msg, end=""),
        level=config.logging.level.value,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
    )

    logger.add(
        str(RUNTIME_LOG),
        level="DEBUG",
        rotation="10 MB",
        retention="30 days",
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}",
    )

    _ensure_buffer_sink()
    _install_stdlib_interception()

    logger.info("Логирование инициализировано")

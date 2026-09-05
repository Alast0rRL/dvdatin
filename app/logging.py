# Настройка логирования: Loguru для файлов, Rich для красивого вывода в консоль.

import sys
from pathlib import Path

from loguru import logger
from rich.console import Console

from app.config import AppConfig

LOG_DIR = Path("data/logs")
RUNTIME_LOG = LOG_DIR / "runtime.log"

console = Console(force_terminal=True, file=sys.stderr)


def setup_logging(config: AppConfig) -> None:
    """Настраивает Loguru-логгеры: консоль через Rich + runtime.log.

    Args:
        config: Конфигурация приложения.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger.remove()

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

    logger.info("Логирование инициализировано")

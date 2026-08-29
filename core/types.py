# Перечисления проекта: режимы работы, типы событий, статусы.

from enum import StrEnum


class Mode(StrEnum):
    """Режим работы приложения."""

    OBSERVE = "OBSERVE"
    SEMI_AUTO = "SEMI_AUTO"
    AUTO = "AUTO"


class LogLevel(StrEnum):
    """Уровни логирования."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"

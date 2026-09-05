# Pydantic-модели конфигурации и загрузчик YAML-файла.
# Валидирует все поля при старте и выдаёт понятные ошибки.

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator, model_validator

from core.types import LogLevel, Mode


class ProxyConfig(BaseModel):
    """Настройки прокси для подключения к Telegram."""

    enabled: bool = False
    type: str = "socks5"
    host: str = ""
    port: int = 0
    username: str = ""
    password: str = ""


class TelegramConfig(BaseModel):
    """Параметры подключения к Telegram API (один аккаунт)."""

    api_id: int
    api_hash: str
    phone: str = ""
    session: str = ""
    proxy: ProxyConfig = ProxyConfig()

    @field_validator("api_id")
    @classmethod
    def api_id_must_be_positive(cls, v: int) -> int:
        if v <= 0:
            msg = "api_id должен быть положительным числом"
            raise ValueError(msg)
        return v

    @field_validator("api_hash")
    @classmethod
    def api_hash_not_empty(cls, v: str) -> str:
        if not v.strip():
            msg = "api_hash не может быть пустым"
            raise ValueError(msg)
        return v


class TelegramAccountsConfig(BaseModel):
    """Один или несколько Telegram-аккаунтов.

    Принимает как явный список ``accounts``, так и одиночный словарь
    (обратная совместимость со старым форматом ``telegram: {api_id, ...}``).
    """

    accounts: list[TelegramConfig] = []

    @model_validator(mode="before")
    @classmethod
    def _coerce_accounts(cls, data: object) -> object:
        """Нормализует разные формы задания аккаунтов в ``{accounts: [...]}``.

        Args:
            data: Сырое значение поля ``telegram`` (dict / list / TelegramConfig).

        Returns:
            Словарь с ключом ``accounts`` или исходное значение.
        """
        if isinstance(data, TelegramConfig):
            return {"accounts": [data]}
        if isinstance(data, list):
            return {"accounts": data}
        if isinstance(data, dict):
            if "accounts" in data:
                return data
            if "api_id" in data:
                return {"accounts": [data]}
        return data


class AgeFilterConfig(BaseModel):
    """Настройки фильтра возраста."""

    min: int = 18
    max: int = 19

    @field_validator("min", "max")
    @classmethod
    def age_in_range(cls, v: int) -> int:
        if not (14 <= v <= 100):
            msg = "Возраст должен быть от 14 до 100"
            raise ValueError(msg)
        return v


class CityFilterConfig(BaseModel):
    """Настройки фильтра города."""

    allowed: list[str] = ["Санкт-Петербург"]


class FiltersConfig(BaseModel):
    """Фильтры для отбора анкет."""

    age: AgeFilterConfig = AgeFilterConfig()
    city: CityFilterConfig = CityFilterConfig()

    city_allowed: list[str] = []
    age_min: int = 18
    age_max: int = 19

    def model_post_init(self, __context: object) -> None:
        """Инициализирует compat-поля из основных."""
        if not self.city_allowed:
            self.city_allowed = self.city.allowed
        if self.age_min == 18 and self.age.max != 19:
            self.age_min = self.age.min
        if self.age_max == 19 and self.age.max != 19:
            self.age_max = self.age.max
        self.age_min = self.age.min
        self.age_max = self.age.max
        self.city_allowed = self.city.allowed


class AutoActionsConfig(BaseModel):
    """Настройки авто-действий (Stage 7, SEMI_AUTO).

    Включает отправку ❤️/👎 на анкеты с решением LIKE/DISLIKE. Работает
    только когда ``project.mode >= SEMI_AUTO`` и аккаунт указан в
    ``account_session``. Rate-limiter — интервальный (6/мин по умолчанию),
    без жёсткого дневного лимита (пользователь контролирует вручную).
    """

    enabled: bool = False
    # Сессия аккаунта, от имени которого шлём действия (например "dvai_2").
    account_session: str = ""
    # Интервал между действиями в секундах (6/мин ≈ 10 сек).
    interval_sec: float = 10.0
    # Chat ID для уведомлений (user_id владельца). Если >0 — уведомления идут
    # именно туда. Если 0 — авто-режим: уведомление шлётся на «другой» аккаунт
    # из списка (тот, что не является авто-аккаунтом), т.е. с Бармалея на
    # Меланхолика и обратно. Пересылается карточка анкеты + причина.
    notify_chat_id: int = 0

    @field_validator("interval_sec")
    @classmethod
    def interval_positive(cls, v: float) -> float:
        if v < 0:
            msg = "interval_sec не может быть отрицательным"
            raise ValueError(msg)
        return v


class ControlConfig(BaseModel):
    """Настройки управляющего Telegram-бота (Stage 7.5).

    ``allowed_user_ids`` — только этим user_id разрешено отправлять команды
    (по умолчанию собственный account-id оператора 8525808108). Реальный
    бот-клиент — тот же ``telegram.accounts[0]``, что и у ReviewBot.
    """

    enabled: bool = False
    # Разрешённые user_id (команды принимаются только от них).
    allowed_user_ids: list[int] = [8525808108]


class ManualReviewConfig(BaseModel):
    """Настройки ручного ревью анкет REVIEW (Stage 8).

    Когда детерминированный scoring выдаёт REVIEW (не хватает информации/не
    уверен), бот НЕ действует сам: уведомляет владельца, что нужно его решение,
    и ждёт ручного действия в Дайвинчике (❤️/👎/сообщение с того же аккаунта,
    под которым слушает collector). Пойманное ручное действие привязывается к
    REVIEW-анкете и дописывается в файл.

    ``file`` — путь к файлу журнала; ``format`` — ``json`` (по умолчанию) или
    ``md`` (Markdown-таблица/список). ``enabled`` гейтит и запись, и
    «ждать ручное действие» на REVIEW.
    """

    enabled: bool = False
    # Путь к файлу журнала ручных решений.
    file: str = "data/reviews/review_log.json"
    # Формат записи: json | md.
    format: str = "json"

    @field_validator("format")
    @classmethod
    def format_valid(cls, v: str) -> str:
        if v not in {"json", "md"}:
            msg = f"format должен быть json или md, получено: {v}"
            raise ValueError(msg)
        return v


class DecisionConfig(BaseModel):
    """Настройки Decision Engine.

    Пороги используются для детерминированного scoring:
    - like_threshold: score >= → LIKE (при наличии positive factors).
    - review_threshold: score >= → REVIEW (иначе тоже REVIEW — DISLIKE только по hard-negative).
    """

    like_threshold: float = 0.75
    review_threshold: float = 0.50
    scoring_version: str = "deterministic-v2"

    @field_validator("like_threshold", "review_threshold")
    @classmethod
    def threshold_in_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            msg = "Порог должен быть от 0.0 до 1.0"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def validate_thresholds_order(self) -> DecisionConfig:
        """review_threshold должен быть строго меньше like_threshold."""
        if self.review_threshold >= self.like_threshold:
            msg = (
                f"review_threshold ({self.review_threshold}) должен быть "
                f"строго меньше like_threshold ({self.like_threshold})"
            )
            raise ValueError(msg)
        return self


class AIConfig(BaseModel):
    """Настройки scoring-решения (детерминированный, Stage 8)."""

    decision: DecisionConfig = DecisionConfig()


class LoggingConfig(BaseModel):
    """Настройки логирования."""

    level: LogLevel = LogLevel.INFO


class ProjectConfig(BaseModel):
    """Общие настройки проекта."""

    mode: Mode = Mode.OBSERVE


class DvinchikConfig(BaseModel):
    """Настройки определения Дайвинчика."""

    chat_id: int = 1234060895


class SourcesConfig(BaseModel):
    """Allowlist источников, из которых принимаются сообщения.

    Если список пуст, разрешённым считается только dvinchik.chat_id
    (см. AppConfig.model_post_init).
    """

    allowed_chat_ids: list[int] = []


class AppConfig(BaseModel):
    """Корневая модель конфигурации приложения."""

    telegram: TelegramAccountsConfig
    project: ProjectConfig = ProjectConfig()
    dvinchik: DvinchikConfig = DvinchikConfig()
    sources: SourcesConfig = SourcesConfig()
    filters: FiltersConfig = FiltersConfig()
    ai: AIConfig = AIConfig()
    auto_actions: AutoActionsConfig = AutoActionsConfig()
    control: ControlConfig = ControlConfig()
    manual_review: ManualReviewConfig = ManualReviewConfig()
    logging: LoggingConfig = LoggingConfig()

    def model_post_init(self, __context: object) -> None:
        """Seeds sources.allowed_chat_ids из dvinchik.chat_id, если список пуст."""
        if not self.sources.allowed_chat_ids and self.dvinchik.chat_id:
            self.sources.allowed_chat_ids = [self.dvinchik.chat_id]

    @classmethod
    def load(cls, path: Path) -> AppConfig:
        """Загружает и валидирует YAML-конфиг.

        Args:
            path: Путь к YAML-файлу конфигурации.

        Returns:
            Валидированный AppConfig.

        Raises:
            FileNotFoundError: Если файл не найден.
            ValueError: Если конфиг невалиден.
        """
        if not path.exists():
            msg = f"Файл конфигурации не найден: {path}"
            raise FileNotFoundError(msg)

        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        if raw is None:
            msg = "Файл конфигурации пуст"
            raise ValueError(msg)

        try:
            return cls(**raw)
        except Exception as e:
            msg = f"Ошибка валидации конфигурации: {e}"
            raise ValueError(msg) from e

    def persist_mode(self, path: Path, mode: "Mode") -> None:
        """Сохраняет project.mode в YAML-файл (без перезаписи остального).

        Используется ControlBot для персистентного переключения режима,
        который переживает restart приложения.
        """
        if path.exists():
            with open(path, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        else:
            raw = {}
        project = raw.get("project") or {}
        project["mode"] = mode.value
        raw["project"] = project
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False)
        self.project.mode = mode

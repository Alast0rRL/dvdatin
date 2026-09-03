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


class LimitsConfig(BaseModel):
    """Лимиты действий."""

    max_likes_per_day: int = 40
    min_delay_minutes: int = 90
    max_delay_minutes: int = 240

    @field_validator("max_likes_per_day")
    @classmethod
    def likes_positive(cls, v: int) -> int:
        if v < 0:
            msg = "max_likes_per_day не может быть отрицательным"
            raise ValueError(msg)
        return v


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
    # Команда запуска потока отключена по умолчанию: бот принимает её только
    # в определённом состоянии, которое клиент достоверно не определяет.
    start_command: str = ""
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


class CLIPConfig(BaseModel):
    """Настройки CLIP-анализа фото (оставлен для обратной совместимости)."""

    enabled: bool = False
    model: str = "clip-vit-base-patch32"


class LLMConfig(BaseModel):
    """Настройки LLM (УСТАРЕЛО — не используется в детерминированном scoring)."""

    enabled: bool = False
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    api_key: str = ""
    timeout: int = 30
    max_retries: int = 2


class RemoteAIConfig(BaseModel):
    """Настройки удалённого AI-сервера (УСТАРЕЛО)."""

    base_url: str = "http://localhost:8000"
    timeout: int = 60
    max_retries: int = 2
    api_key: str = ""

    def api_key_or_none(self) -> str | None:
        """Возвращает API key или None, если он пуст."""
        return self.api_key.strip() or None


class DecisionWeightsConfig(BaseModel):
    """Веса источников для объединённого скора (УСТАРЕЛО)."""

    llm: float = 0.70
    clip: float = 0.30

    @field_validator("llm", "clip")
    @classmethod
    def weight_in_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            msg = "Вес должен быть от 0.0 до 1.0"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def weights_not_all_zero(self) -> DecisionWeightsConfig:
        """Хотя бы один вес должен быть больше нуля."""
        if self.llm == 0.0 and self.clip == 0.0:
            msg = "Хотя бы один вес (llm/clip) должен быть больше нуля"
            raise ValueError(msg)
        return self


class DecisionConfig(BaseModel):
    """Настройки Decision Engine.

    Пороги используются для детерминированного scoring:
    - like_threshold: score >= → LIKE (при наличии positive factors).
    - review_threshold: score >= → REVIEW (иначе тоже REVIEW — DISLIKE только по hard-negative).
    """

    like_threshold: float = 0.75
    review_threshold: float = 0.50
    min_confidence: float = 0.60
    scoring_version: str = "deterministic-v2"
    weights: DecisionWeightsConfig = DecisionWeightsConfig()

    @field_validator("like_threshold", "review_threshold", "min_confidence")
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


class ImagesConfig(BaseModel):
    """Настройки скачивания изображений (УСТАРЕЛО — CLIP отключён)."""

    enabled: bool = False
    max_images: int = 5
    max_size_mb: int = 10
    timeout: int = 30


class ScoringConfig(BaseModel):
    """Настройки детерминированного скоринга (Stage 8)."""

    base_score: float = 0.5
    positive_weight: float = 0.10
    positive_cap: float = 0.35
    negative_penalty: float = 0.50

    @field_validator("base_score", "positive_weight", "positive_cap", "negative_penalty")
    @classmethod
    def weight_in_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            msg = "Значение должно быть от 0.0 до 1.0"
            raise ValueError(msg)
        return v


class AIConfig(BaseModel):
    """Настройки AI Scoring (детерминированный scoring, Stage 8)."""

    enabled: bool = False
    backend: str = "local"
    clip: CLIPConfig = CLIPConfig()
    llm: LLMConfig = LLMConfig()
    scoring: ScoringConfig = ScoringConfig()
    decision: DecisionConfig = DecisionConfig()
    remote: RemoteAIConfig = RemoteAIConfig()
    images: ImagesConfig = ImagesConfig()

    @field_validator("backend")
    @classmethod
    def backend_valid(cls, v: str) -> str:
        allowed = {"local", "remote"}
        if v not in allowed:
            msg = f"backend должен быть {allowed}, получено: {v}"
            raise ValueError(msg)
        return v


class LoggingConfig(BaseModel):
    """Настройки логирования."""

    level: LogLevel = LogLevel.INFO


class ProjectConfig(BaseModel):
    """Общие настройки проекта."""

    mode: Mode = Mode.OBSERVE


class DvinchikConfig(BaseModel):
    """Настройки определения Дайвинчика."""

    enabled: bool = True
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
    limits: LimitsConfig = LimitsConfig()
    auto_actions: AutoActionsConfig = AutoActionsConfig()
    control: ControlConfig = ControlConfig()
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
        from core.types import Mode as _Mode

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

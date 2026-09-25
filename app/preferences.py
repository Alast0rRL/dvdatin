# Настройки предпочтений пользователя для AI scoring.
# Хранятся в ОТДЕЛЬНОМ файле config/preferences.yaml (gitignored) — там
# перезаписываются SKIP/LIKE-правила. Пример — config/preferences.example.yaml,
# который коммитится. Репозиторий не содержит логики/значений правил в коде —
# только механизм их применения.
#
# Telegram-free, не зависит от collector/worker/DB.

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator

#: Живой файл предпочтений (не коммитится — .gitignore).
PREFERENCES_PATH = Path("config/preferences.yaml")
#: Пример-шаблон (коммитится).
PREFERENCES_EXAMPLE = Path("config/preferences.example.yaml")


class PreferenceRule(BaseModel):
    """Одно правило: label + список подстрок для поиска (lowercase)."""

    label: str
    match: list[str] = []


class ScoringPrefs(BaseModel):
    """Тонкая настройка применения правил (пороги 0.75/0.50 не трогаем)."""

    # SKIP-сигнал → всегда DISLIKE (CLIP не переворачивает).
    skip_is_hard: bool = True
    # CLIP (эстетика фото) не может отменить SKIP/поднять в LIKE без LIKE-фактора.
    clip_cannot_override_skip: bool = True


class LowInfoPrefs(BaseModel):
    """Что делать с «пустой» анкетой (мало значимых слов, нет признаков).

    По умолчанию ``action: review`` — историческое поведение Stage 8: мало
    информации → REVIEW (ждём ручного решения владельца).

    Если владелец хочет «скипать» пустые анкеты — ставит
    ``action: skip``: неинформативная анкета → DISLIKE (авто-👎), лента Leo
    не замирает. Это ЕДИНСТВЕННОЕ место, где «мало информации» может стать
    отрицательным решением (иначе работает инвариант
    NO_HARD_NEGATIVE_MUST_NOT_BECOME_DISLIKE).
    """

    #: ``review`` — мало инфы → REVIEW (по умолчанию), ``skip`` → DISLIKE.
    action: str = "review"
    #: Порог «мало инфы» в значимых словах (None → MIN_MEANINGFUL_WORDS).
    min_words: int | None = None

    @field_validator("action")
    @classmethod
    def _known_action(cls, v: str) -> str:
        v = (v or "review").strip().lower()
        if v not in ("review", "skip"):
            msg = f"low_info.action должен быть 'review' или 'skip', получено {v!r}"
            raise ValueError(msg)
        return v

    @property
    def skip(self) -> bool:
        """True, если неинформативные анкеты надо скипать (DISLIKE)."""
        return self.action == "skip"


class PreferencesConfig(BaseModel):
    """Корневая модель предпочтений."""

    skip: list[PreferenceRule] = []
    like: list[PreferenceRule] = []
    scoring: ScoringPrefs = ScoringPrefs()
    low_info: LowInfoPrefs = LowInfoPrefs()


class PreferencesEngine:
    """Оценивает текст анкеты по SKIP/LIKE-правилам пользователя."""

    def __init__(self, prefs: PreferencesConfig | None = None) -> None:
        self._prefs = prefs or PreferencesConfig()

    @property
    def enabled(self) -> bool:
        """Есть ли хоть одно правило."""
        return bool(self._prefs.skip or self._prefs.like)

    @property
    def scoring(self) -> ScoringPrefs:
        return self._prefs.scoring

    @property
    def low_info(self) -> LowInfoPrefs:
        """Правило «мало информации в анкете» (review по умолчанию)."""
        return self._prefs.low_info

    def evaluate(self, text: str) -> tuple[list[str], list[str]]:
        """Возвращает (skip_labels, like_labels), найденные в тексте.

        Текстовый поиск по подстрокам (lowercase). Порядок: порядок правил в
        файле. Пустой текст → пустые списки (правила не применяются).

        Отрицания учитываются: «не курю», «не пью», «никогда не пьёт» и т.п.
        НЕ считаются срабатыванием правила (иначе анкета «Не курю, не пью
        и против плохих привычек» ошибочно получала бы SKIP «курит»/«пьёт»).
        """
        if not text:
            return [], []
        low = text.lower()
        skip = [
            r.label for r in self._prefs.skip
            if any(_match_aware(low, k) for k in r.match)
        ]
        like = [
            r.label for r in self._prefs.like
            if any(_match_aware(low, k) for k in r.match)
        ]
        return skip, like


def load_preferences(path: Path | None = None) -> PreferencesEngine:
    """Загружает предпочтения.

    Приоритет: live-файл (config/preferences.yaml) → пример
    (config/preferences.example.yaml) → пустые правила (ничего не меняем).

    Args:
        path: Кастомный путь (для тестов). По умолчанию — PREFERENCES_PATH.
    """
    target = path or PREFERENCES_PATH
    source = target if target.exists() else PREFERENCES_EXAMPLE
    if source.exists():
        try:
            with open(source, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return PreferencesEngine(PreferencesConfig(**data))
        except Exception:
            return PreferencesEngine(PreferencesConfig())
    return PreferencesEngine(PreferencesConfig())


#: Отрицания: если keyword непосредственно предварён одним из них — не считаем.


def _match_aware(low: str, keyword: str) -> bool:
    """True, если ``keyword`` есть в ``low`` и не предварён отрицанием.

    Отрицанием считается оборот «не/ни/без + глагол» непосредственно перед
    keyword: «не курю» → «курю» НЕ матчит; «люблю пиво» → «пиво» матчит.

    Args:
        low: нижний регистр текста анкеты.
        keyword: подстрока-правило из конфига.
    """
    start = 0
    while True:
        idx = low.find(keyword, start)
        if idx == -1:
            return False
        if _before_is_negated(low, idx):
            start = idx + 1
            continue
        return True


def _before_is_negated(low: str, idx: int) -> bool:
    """True, если непосредственно перед позицией ``idx`` стоит «не/ни/без»."""
    prefix = low[:idx]
    if not prefix.strip():
        return False
    for word in ("не", "ни", "без"):
        pattern = re.compile(
            r"(?<!\w)" + re.escape(word) + r"[\s,;.!?…\-\u2013\u2014]*$",
            re.IGNORECASE,
        )
        if pattern.search(prefix):
            return True
    return False

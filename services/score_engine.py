# ScoreEngine: детерминированное вычисление score и информативности анкеты.
# Зависит ТОЛЬКО от извлечённых признаков, текста описания и конфигурации.
# Никакого LLM/CLIP/Telegram. Полностью воспроизводим.
#
# Информативность — это оценка КОЛИЧЕСТВА полезной информации в анкете
# (значимые слова + найденные известные признаки), а НЕ оценка личности.
# Никаких «хорошая/красивая/совместимая» — Python этого не знает.

from __future__ import annotations

import re
from dataclasses import dataclass

from models.features import Feature, ScoringResult, ScoringStatus, SCORING_VERSION


# Стоп-слова: короткие/служебные слова, не несущие смысловой нагрузки при
# подсчёте «значимых» слов анкеты. Изнач. инвариант: отсутствие информации
# НИКОГДА не является негативом.
_STOPWORDS: frozenset[str] = frozenset(
    "и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по "
    "только ее мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли если "
    "уже или ни быть был него до вас нибудь уж б например нету всем ох лучше".split()
)


# Минимальное число значимых слов, при котором описание считается «достаточно
# информативным» само по себе (без учёта признаков).
MIN_MEANINGFUL_WORDS: int = 10


def count_meaningful_words(text: str) -> int:
    """Считает число значимых слов в тексте.

    Значимым считается слово длиной >= 3 символов, не входящее в стоп-слова.
    """
    if not text:
        return 0
    tokens = re.findall(r"[a-zA-Zа-яА-ЯёЁ]{3,}", text)
    return sum(1 for t in tokens if t.lower() not in _STOPWORDS)


@dataclass
class ScoreConfig:
    """Конфигурация весов для score engine."""

    # Базовый score для анкеты без признаков (0.5 — нейтральный)
    base_score: float = 0.5

    # Бонус score за информативную анкету (инвариант: informative <= 0.5)
    informative_bonus: float = 0.10

    # Вес за каждый обнаруженный положительный фактор
    positive_weight: float = 0.10

    # Максимальный дополнительный score за все положительные факторы
    positive_cap: float = 0.35

    # Штраф за каждый подтверждённый hard-negative
    negative_penalty: float = 0.50

    # Минимальный score (hard negative не может быть полностью обнулён)
    min_score: float = 0.0

    # Максимальный score
    max_score: float = 1.0


# Конфигурация по умолчанию (можно переопределить через config.yaml)
DEFAULT_SCORE_CONFIG = ScoreConfig()


class ScoreEngine:
    """Детерминированный движок расчёта score и информативности.

    Принимает list[Feature] + текст описания и вычисляет числовой score
    (0.0–1.0) и флаг informative. Полностью детерминирован.
    """

    def __init__(self, config: ScoreConfig | None = None) -> None:
        self._config = config or DEFAULT_SCORE_CONFIG

    def compute(
        self,
        profile_id: int,
        hard_negatives: list[Feature],
        positive_factors: list[Feature],
        description: str = "",
    ) -> ScoringResult:
        """Вычисляет score и информативность, формирует результат.

        Args:
            profile_id: ID профиля.
            hard_negatives: Список подтверждённых hard-negative features.
            positive_factors: Список обнаруженных positive features.
            description: Текст описания анкеты (для подсчёта значимых слов).

        Returns:
            ScoringResult с score, статусом и флагом informative.
        """
        cfg = self._config
        has_negative = bool(hard_negatives)
        has_positive = bool(positive_factors)

        words = count_meaningful_words(description)
        # Информативная анкета: достаточно значимых слов ИЛИ есть хотя бы один
        # известный признак. Это НЕ оценка личности — только объём информации.
        informative = (
            words >= MIN_MEANINGFUL_WORDS or has_positive
        )

        # Статус: достаточно ли данных для confident решения
        status = (
            ScoringStatus.SUFFICIENT_DATA
            if (has_negative or has_positive or informative)
            else ScoringStatus.INSUFFICIENT_DATA
        )

        # Если есть hard negative — score минимальный (информативность НЕ
        # отменяет hard-negative: она побеждает на уровне решения, а score
        # всё равно обнуляется).
        if has_negative:
            score = max(cfg.min_score, cfg.base_score - cfg.negative_penalty)
        else:
            # Базовый score + бонус за информативность + бонус за positive
            score = cfg.base_score
            if informative:
                score += cfg.informative_bonus
            if has_positive:
                bonus = min(
                    len(positive_factors) * cfg.positive_weight,
                    cfg.positive_cap,
                )
                score += bonus
            score = min(cfg.max_score, score)

        score = round(min(max(score, cfg.min_score), cfg.max_score), 3)

        return ScoringResult(
            profile_id=profile_id,
            score=score,
            hard_negatives=hard_negatives,
            positive_factors=positive_factors,
            status=status,
            informative=informative,
            meaningful_words=words,
            scoring_version=SCORING_VERSION,
        )

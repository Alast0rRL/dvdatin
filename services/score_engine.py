# ScoreEngine: детерминированное вычисление числового score из признаков.
# Зависит ТОЛЬКО от извлечённых признаков и конфигурации.
# Никакого LLM/CLIP/Telegram. Полностью воспроизводим.

from __future__ import annotations

from dataclasses import dataclass

from models.features import Feature, FeatureType, ScoringResult, ScoringStatus, SCORING_VERSION


@dataclass
class ScoreConfig:
    """Конфигурация весов для score engine."""

    # Базовый score для анкеты без признаков (0.5 — нейтральный)
    base_score: float = 0.5

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
    """Детерминированный движок расчёта score.

    Принимает list[Feature] и вычисляет числовой score (0.0–1.0).
    Полностью детерминирован: одинаковые features → одинаковый score.
    """

    def __init__(self, config: ScoreConfig | None = None) -> None:
        self._config = config or DEFAULT_SCORE_CONFIG

    def compute(
        self,
        profile_id: int,
        hard_negatives: list[Feature],
        positive_factors: list[Feature],
    ) -> ScoringResult:
        """Вычисляет score и формирует результат.

        Args:
            profile_id: ID профиля.
            hard_negatives: Список подтверждённых hard-negative features.
            positive_factors: Список обнаруженных positive features.

        Returns:
            ScoringResult с score и статусом.
        """
        cfg = self._config
        has_negative = bool(hard_negatives)
        has_positive = bool(positive_factors)

        # Статус: достаточно ли данных для confident решения
        status = (
            ScoringStatus.SUFFICIENT_DATA
            if (has_negative or has_positive)
            else ScoringStatus.INSUFFICIENT_DATA
        )

        # Если есть hard negative — score минимальный
        if has_negative:
            score = max(cfg.min_score, cfg.base_score - cfg.negative_penalty)
        elif has_positive:
            # Базовый score + bonus за положительные факторы
            bonus = min(
                len(positive_factors) * cfg.positive_weight,
                cfg.positive_cap,
            )
            score = min(cfg.max_score, cfg.base_score + bonus)
        else:
            # Нет ни positive, ни negative → нейтральный
            score = cfg.base_score

        score = round(min(max(score, cfg.min_score), cfg.max_score), 3)

        return ScoringResult(
            profile_id=profile_id,
            score=score,
            hard_negatives=hard_negatives,
            positive_factors=positive_factors,
            status=status,
            scoring_version=SCORING_VERSION,
        )

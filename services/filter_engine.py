# Filter Engine: независимый движок фильтрации профилей.
# Работает только с Profile + Config. Не знает о Telegram.

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from loguru import logger

from models.filter import FilterDecision, FilterReason, FilterResult

if TYPE_CHECKING:
    from app.config import AppConfig
    from models.profile import Profile


class Rule(ABC):
    """Абстрактное правило фильтрации."""

    @abstractmethod
    def evaluate(self, profile: Profile, config: AppConfig) -> tuple[bool, FilterReason]:
        """Оценивает профиль по правилу.

        Returns:
            (passed, reason)
        """
        ...


class AgeRule(Rule):
    """Проверка возраста."""

    def evaluate(self, profile: Profile, config: AppConfig) -> tuple[bool, FilterReason]:
        if profile.age is None or profile.age == 0:
            return False, FilterReason.AGE_UNKNOWN

        if config.filters.age_min <= profile.age <= config.filters.age_max:
            return True, FilterReason.AGE_OK

        return False, FilterReason.AGE_OUT_OF_RANGE


class CityRule(Rule):
    """Проверка города."""

    def evaluate(self, profile: Profile, config: AppConfig) -> tuple[bool, FilterReason]:
        if not profile.normalized_city:
            return False, FilterReason.CITY_UNKNOWN

        if profile.normalized_city in config.filters.city_allowed:
            return True, FilterReason.CITY_OK

        return False, FilterReason.CITY_OUT_OF_RANGE


class DataCompletenessRule(Rule):
    """Проверка полноты данных."""

    def evaluate(self, profile: Profile, config: AppConfig) -> tuple[bool, FilterReason]:
        has_age = profile.age is not None and profile.age > 0
        has_city = bool(profile.normalized_city)

        if not has_age and not has_city:
            return False, FilterReason.INSUFFICIENT_DATA

        return True, FilterReason.AGE_OK


class FilterEngine:
    """Движок фильтрации профилей."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._rules: list[Rule] = [
            AgeRule(),
            CityRule(),
            DataCompletenessRule(),
        ]

    def evaluate(self, profile: Profile) -> FilterResult:
        """Оценивает профиль по всем правилам.

        Args:
            profile: Профиль для оценки.

        Returns:
            FilterResult с решением и всеми причинами.
        """
        reasons: list[FilterReason] = []
        has_reject = False
        has_review = False

        for rule in self._rules:
            passed, reason = rule.evaluate(profile, self._config)
            reasons.append(reason)

            if reason in (
                FilterReason.AGE_OUT_OF_RANGE,
                FilterReason.CITY_OUT_OF_RANGE,
            ):
                has_reject = True
            elif reason in (
                FilterReason.AGE_UNKNOWN,
                FilterReason.CITY_UNKNOWN,
                FilterReason.INSUFFICIENT_DATA,
            ):
                has_review = True

        if has_reject:
            decision = FilterDecision.REJECT
        elif has_review:
            decision = FilterDecision.REVIEW
        else:
            decision = FilterDecision.PASS

        # Уникальные причины в порядке появления (DataCompletenessRule может
        # дублировать AGE_OK из AgeRule). Решение выше вычислено по флагам,
        # поэтому дедуп не влияет на decision.
        seen: set[FilterReason] = set()
        unique_reasons: list[FilterReason] = []
        for r in reasons:
            if r not in seen:
                seen.add(r)
                unique_reasons.append(r)
        reasons = unique_reasons

        now = datetime.now(timezone.utc).isoformat()

        result = FilterResult(
            profile_id=profile.id,
            decision=decision,
            reasons=reasons,
            rules_checked=len(self._rules),
            evaluated_at=now,
        )

        self._log_result(profile, result)

        return result

    def _log_result(self, profile: Profile, result: FilterResult) -> None:
        """Логирует результат фильтрации."""
        if result.decision == FilterDecision.PASS:
            logger.info(
                f"Profile evaluated: profile_id={profile.id}, "
                f"decision=PASS"
            )
        elif result.decision == FilterDecision.REJECT:
            reject_reasons = [r.value for r in result.reasons if r.value.endswith("_OUT_OF_RANGE")]
            logger.info(
                f"Profile rejected: profile_id={profile.id}, "
                f"reason={','.join(reject_reasons)}"
            )
        else:
            review_reasons = [r.value for r in result.reasons if r.value.endswith("_UNKNOWN") or r == FilterReason.INSUFFICIENT_DATA]
            logger.warning(
                f"Profile requires review: profile_id={profile.id}, "
                f"reason={','.join(review_reasons)}"
            )

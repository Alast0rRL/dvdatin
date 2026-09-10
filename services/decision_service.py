# DecisionService: детерминированный Decision Engine.
# Заменяет LLM-зависимую версию. Полностью детерминированная.
#
# Pipeline: normalize → extract features → evaluate rules → calculate score → decision
#
# КРИТИЧЕСКИЙ ИНВАРИАНТ (NO_HARD_NEGATIVE_MUST_NOT_BECOME_DISLIKE):
# DISLIKE возможен ТОЛЬКО при:
#   1. Подтверждённом hard-negative (извлечённом из текста).
#   2. Hard filter reject (age/city вне диапазона).
#   3. User SKIP из preferences.yaml.
# Низкий score, отсутствие текста, пустая анкета, нет интересов —
# это всегда REVIEW, а не DISLIKE.
#
# Telegram-free: не импортирует Telethon.

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from loguru import logger

from app.preferences import PreferencesEngine
from models.decision import AIDecision, AIDecisionResult
from models.features import SCORING_VERSION
from models.filter import FilterDecision
from services.feature_extractor import FeatureExtractor
from services.score_engine import ScoreEngine

if TYPE_CHECKING:
    from app.config import AppConfig, DecisionConfig
    from database.database import Database
    from models.profile import Profile
    from services.filter_service import FilterService
    from services.profile_service import ProfileService


class DecisionService:
    """Сервис принятия детерминированных решений по профилям.

    Заменяет LLM-based DecisionService. Использует:
    1. PreferencesEngine (SKIP/LIKE из config/preferences.yaml).
    2. FeatureExtractor (детерминированное извлечение признаков).
    3. ScoreEngine (числовой score из признаков).
    4. Decision logic (LIKE/REVIEW/DISLIKE по порогам и правилам).
    """

    def __init__(
        self,
        db: Database,
        config: AppConfig,
        profile_service: ProfileService,
        filter_service: FilterService,
        preferences: PreferencesEngine | None = None,
    ) -> None:
        self._db = db
        self._config = config
        self._profile_service = profile_service
        self._filter_service = filter_service
        self._decision_cfg: DecisionConfig = config.ai.decision
        self._prefs = preferences if preferences is not None else PreferencesEngine()
        self._extractor = FeatureExtractor()
        self._score_engine = ScoreEngine()

    async def evaluate(
        self,
        profile_id: int,
        filter_result=None,
    ) -> AIDecisionResult | None:
        """Оценивает профиль по ID и сохраняет решение.

        Args:
            profile_id: ID профиля.
            filter_result: Уже вычисленный результат фильтра (опционально).

        Returns:
            AIDecisionResult или None, если профиль не найден.
        """
        profile = await self._profile_service.get_profile(profile_id)
        if profile is None:
            logger.warning(f"Decision: profile {profile_id} не найден")
            return None
        return await self.evaluate_profile(profile, filter_result=filter_result)

    async def evaluate_profile(
        self,
        profile: Profile,
        filter_result=None,
    ) -> AIDecisionResult:
        """Принимает решение по объекту Profile и сохраняет его.

        Полностью детерминированный pipeline:
        1. PreferencesEngine (user SKIP/LIKE).
        2. FeatureExtractor (hard negatives / positive factors).
        3. ScoreEngine (числовой score).
        4. Decision logic (LIKE/REVIEW/DISLIKE).
        """
        if filter_result is None:
            filter_result = await self._filter_service.evaluate(profile)
        filter_decision = filter_result.decision if filter_result else None

        # 1. Preferences: user SKIP/LIKE
        skip_labels: list[str] = []
        like_labels: list[str] = []
        text = profile.description or ""
        if self._prefs.enabled and text:
            skip_labels, like_labels = self._prefs.evaluate(text)

        # 2. Feature Extraction (детерминированно)
        extraction = self._extractor.extract(
            name=profile.name,
            age=profile.age,
            city=profile.normalized_city,
            description=profile.description or "",
        )

        # 3. Score Engine (детерминированно): score + информативность.
        scoring = self._score_engine.compute(
            profile_id=profile.id,
            hard_negatives=extraction.hard_negatives,
            positive_factors=extraction.positive_factors,
            description=profile.description or "",
        )

        # 4. Decision Logic
        filter_reason_codes = (
            [r.value for r in filter_result.reasons] if filter_result else []
        )
        decision, combined, reasons = self._decide(
            filter_decision=filter_decision,
            filter_reasons=filter_reason_codes,
            score=scoring.score,
            informative=scoring.informative,
            skip_labels=skip_labels,
            like_labels=like_labels,
            hard_negatives=scoring.hard_negatives,
            positive_factors=scoring.positive_factors,
        )

        now = datetime.now(timezone.utc).isoformat()

        result = AIDecisionResult(
            profile_id=profile.id,
            decision=decision,
            combined_score=combined,
            confidence=scoring.score,
            reasons=reasons,
            evaluated_at=now,
            scoring_version=SCORING_VERSION,
            informative=scoring.informative,
            meaningful_words=scoring.meaningful_words,
        )

        await self._save(result)
        self._log(result, profile)
        return result

    # ── Decision Logic ───────────────────────────────────────────────

    def _decide(
        self,
        filter_decision: FilterDecision | None,
        filter_reasons: list[str],
        score: float,
        informative: bool,
        skip_labels: list[str],
        like_labels: list[str],
        hard_negatives: list | None = None,
        positive_factors: list | None = None,
    ) -> tuple[AIDecision, float, list[str]]:
        """Вычисляет решение по правилам (бизнес-политика §13).

        Приоритет:
        1. HARD USER SKIP (из preferences.yaml) → DISLIKE.
        2. HARD NEGATIVE (извлечён из текста) → DISLIKE.
        3. HARD FILTER REJECT (age/city) → DISLIKE.
        4. FILTER REVIEW → REVIEW.
        5. PASS: информативная И чистая анкета → LIKE.
        6. Всё остальное (короткая/неинформативная, но чистая) → REVIEW.

        КЛЮЧЕВОЙ ИНВАРИАНТ: без hard-negative/скипа/reject НИКОГДА не DISLIKE.
        Информативность — только объём информации, а не оценка личности.
        """
        hard_negatives = list(hard_negatives or [])
        positive_factors = list(positive_factors or [])
        reasons: list[str] = []

        # 1. HARD USER SKIP
        if skip_labels and self._prefs.scoring.skip_is_hard:
            reasons.append(f"USER_SKIP:{skip_labels[0]}")
            for hn in hard_negatives:
                reasons.append(f"HARD_NEGATIVE:{hn.name}:{hn.evidence}")
            for pf in positive_factors:
                reasons.append(f"POSITIVE:{pf.name}:{pf.evidence}")
            return AIDecision.DISLIKE, score, reasons

        # 2. HARD NEGATIVE (from FeatureExtractor) — побеждает информативность
        if hard_negatives:
            hn = hard_negatives[0]
            reasons.append(f"HARD_NEGATIVE:{hn.name}:{hn.evidence}")
            if like_labels:
                reasons.append(f"USER_LIKE:{like_labels[0]}")
            for pf in positive_factors:
                reasons.append(f"POSITIVE:{pf.name}:{pf.evidence}")
            return AIDecision.DISLIKE, score, reasons

        # 3. HARD FILTER REJECT
        if filter_decision == FilterDecision.REJECT:
            reasons.append("FILTER_REJECTED")
            for fr in filter_reasons:
                if fr in ("AGE_OUT_OF_RANGE", "CITY_OUT_OF_RANGE"):
                    reasons.append(fr)
            if like_labels:
                reasons.append(f"USER_LIKE:{like_labels[0]}")
            return AIDecision.DISLIKE, score, reasons

        # 4. FILTER REVIEW
        if filter_decision == FilterDecision.REVIEW:
            reasons.append("FILTER_REVIEW")
            if like_labels:
                reasons.append(f"USER_LIKE:{like_labels[0]}")
            for pf in positive_factors:
                reasons.append(f"POSITIVE:{pf.name}:{pf.evidence}")
            return AIDecision.REVIEW, score, reasons

        # 5. PASS: информативная И чистая анкета → LIKE
        # (достаточно значимых слов ИЛИ известных признаков, и нет
        # ни одного противопоказания — проверено шагами 2-4).
        for pf in positive_factors:
            reasons.append(f"POSITIVE:{pf.name}:{pf.evidence}")
        if like_labels:
            reasons.append(f"USER_LIKE:{like_labels[0]}")
        if informative:
            reasons.append("INFORMATIVE_CLEAN")
            return AIDecision.LIKE, score, reasons

        # 6. Короткая/неинформативная, но чистая анкета → REVIEW
        # (НИКОГДА не DISLIKE без hard-negative).
        reasons.append("NO_FEATURES_FOUND")
        return AIDecision.REVIEW, score, reasons

    # ── Сохранение и чтение ──────────────────────────────────────────

    async def _save(self, result: AIDecisionResult) -> None:
        """Сохраняет решение в ai_decisions."""
        row_id = await self._db.save_ai_decision(
            profile_id=result.profile_id,
            decision=result.decision.value,
            combined_score=result.combined_score,
            confidence=result.confidence,
            reasons=result.reasons_json(),
            scoring_version=result.scoring_version,
            evaluated_at=result.evaluated_at,
        )
        result.id = row_id

    async def get_latest(self, profile_id: int) -> AIDecisionResult | None:
        """Получает последнее решение для профиля."""
        row = await self._db.get_latest_ai_decision(profile_id)
        if row is None:
            return None
        return self._row_to_result(row)

    async def get_history(self, profile_id: int) -> list[AIDecisionResult]:
        """Получает историю решений для профиля."""
        rows = await self._db.get_ai_decision_history(profile_id)
        return [self._row_to_result(row) for row in rows]

    @staticmethod
    def _row_to_result(row: dict) -> AIDecisionResult:
        """Преобразует dict из БД в AIDecisionResult."""
        reasons_raw = row.get("reasons", "[]")
        reasons = json.loads(reasons_raw) if isinstance(reasons_raw, str) else reasons_raw
        return AIDecisionResult(
            id=row.get("id", 0),
            profile_id=row["profile_id"],
            decision=AIDecision(row["decision"]),
            combined_score=row.get("combined_score", 0.0),
            confidence=row.get("confidence", 0.0),
            reasons=reasons,
            evaluated_at=row.get("evaluated_at", ""),
            scoring_version=row.get("scoring_version", "v1"),
        )

    def _log(self, result: AIDecisionResult, profile: Profile) -> None:
        """Логирует решение."""
        logger.info(
            f"DECISION: profile={profile.name} (#{result.profile_id}), "
            f"decision={result.decision}, score={result.combined_score:.2f}, "
            f"reasons={result.reasons[:3]}, "
            f"scoring_version={result.scoring_version}"
        )

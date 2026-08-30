# DecisionService: AI Decision Engine (Stage 5).
# Отдельный слой поверх AI scoring. Принимает финальное решение по профилю.
#
# ПРАВИЛА:
# - Работает ТОЛЬКО в OBSERVE-режиме. НЕ выполняет Telegram-действий.
# - Telegram-free: не импортирует Telethon.
# - НЕ делает score = decision: combined_score и decision — разные вещи.
# - Пороги/веса берутся из конфигурации ai.decision.

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from loguru import logger

from app.preferences import PreferencesEngine
from models.decision import AIDecision, AIDecisionResult
from models.filter import FilterDecision

if TYPE_CHECKING:
    from app.config import AppConfig, DecisionConfig
    from database.database import Database
    from models.profile import Profile
    from services.ai_scoring_service import AIScoringService
    from services.filter_service import FilterService
    from services.profile_service import ProfileService


class DecisionService:
    """Сервис принятия AI-решений по профилям."""

    def __init__(
        self,
        db: Database,
        config: AppConfig,
        profile_service: ProfileService,
        filter_service: FilterService,
        ai_scoring_service: AIScoringService,
        preferences: PreferencesEngine | None = None,
    ) -> None:
        self._db = db
        self._config = config
        self._profile_service = profile_service
        self._filter_service = filter_service
        self._ai = ai_scoring_service
        self._decision_cfg: DecisionConfig = config.ai.decision
        # Предпочтения пользователя (SKIP/LIKE) — отдельный файл config/preferences.yaml.
        # Если engine не передан — пустые правила (поведение не меняется).
        self._prefs = preferences if preferences is not None else PreferencesEngine()

    async def evaluate(
        self,
        profile_id: int,
        image_data_list: list[bytes] | None = None,
        filter_result: FilterResult | None = None,
    ) -> AIDecisionResult | None:
        """Оценивает профиль по ID и сохраняет решение.

        Args:
            profile_id: ID профиля.
            image_data_list: Байты изображений (опционально) для CLIP.
            filter_result: Уже вычисленный результат фильтра (опционально),
                чтобы не оценивать фильтр повторно.

        Returns:
            AIDecisionResult или None, если профиль не найден.
        """
        profile = await self._profile_service.get_profile(profile_id)
        if profile is None:
            logger.warning(f"Decision: profile {profile_id} не найден")
            return None
        return await self.evaluate_profile(
            profile, image_data_list=image_data_list, filter_result=filter_result,
        )

    async def evaluate_profile(
        self,
        profile: Profile,
        image_data_list: list[bytes] | None = None,
        filter_result: FilterResult | None = None,
    ) -> AIDecisionResult:
        """Принимает решение по объекту Profile и сохраняет его.

        Если filter_result не передан — вычисляет фильтр самостоятельно.
        Передача готового результата устраняет повторную оценку фильтра
        (и лишнюю запись в БД) при вызове из коллектора.
        """
        if filter_result is None:
            filter_result = await self._filter_service.evaluate(profile)
        filter_decision = filter_result.decision if filter_result else None

        # evaluate() сохраняет AIScore в ai_scores и возвращает его —
        # один вызов шлюза даёт и скор, и решение (без двойных сетевых запросов)
        ai_score = await self._ai.evaluate(
            profile, image_data_list=image_data_list,
        )

        decision, combined, reasons = self._decide(
            filter_decision=filter_decision,
            llm_score=ai_score.llm_score,
            clip_score=ai_score.clip_score,
            confidence=ai_score.confidence_score,
            ai_reasons=ai_score.reasons,
            text=profile.description or "",
        )

        now = datetime.now(timezone.utc).isoformat()

        result = AIDecisionResult(
            profile_id=profile.id,
            decision=decision,
            combined_score=combined,
            llm_score=ai_score.llm_score,
            clip_score=ai_score.clip_score,
            confidence=ai_score.confidence_score,
            reasons=reasons,
            evaluated_at=now,
            scoring_version=self._decision_cfg.scoring_version,
            prompt_version=ai_score.prompt_version,
        )

        await self._save(result)
        self._log(result, profile)
        return result

    # ── Логика решения ───────────────────────────────────────────────

    def _combine(
        self,
        llm_score: float | None,
        clip_score: float | None,
    ) -> float:
        """Комбинирует скоры весами Decision Engine.

        Если доступен только один сигнал — используется он целиком.
        Отсутствующие изображения не считаются нулём.
        """
        weights = self._decision_cfg.weights
        has_llm = llm_score is not None
        has_clip = clip_score is not None

        if has_llm and has_clip:
            raw = llm_score * weights.llm + clip_score * weights.clip
            return min(max(raw, 0.0), 1.0)

        if has_llm:
            return min(max(llm_score, 0.0), 1.0)

        if has_clip:
            return min(max(clip_score, 0.0), 1.0)

        return 0.0

    def _decide(
        self,
        filter_decision: FilterDecision | None,
        llm_score: float | None,
        clip_score: float | None,
        confidence: float,
        ai_reasons: list[str],
        text: str = "",
    ) -> tuple[AIDecision, float, list[str]]:
        """Вычисляет решение на основе hard filters + порогов + правил пользователя.

        Данные о предпочтениях пользователя (SKIP/LIKE) приходят из
        preferences-файла (config/preferences.yaml) и применяются как
        последний слой поверх порогов:
          - SKIP-сигнал (hard) → DISLIKE (CLIP не переворачивает).
          - LIKE-фактор → потенциальный DISLIKE подтягивается до REVIEW
            (анкета не теряется), при достижении порога — LIKE.

        Returns:
            (decision, combined_score, reasons)
        """
        combined = self._combine(llm_score, clip_score)
        reasons = list(ai_reasons)
        cfg = self._decision_cfg

        # Предпочтения пользователя — оценка текста анкеты.
        skip_labels, like_labels = (), ()
        scoring = self._prefs.scoring
        if self._prefs.enabled and text:
            skip_labels, like_labels = self._prefs.evaluate(text)

        # HARD USER SKIP: явный негатив → всегда DISLIKE. Самый высокий
        # приоритет: высокий CLIP / high combined не может перевернуть.
        if skip_labels and scoring.skip_is_hard:
            return AIDecision.DISLIKE, combined, [
                f"USER_SKIP:{skip_labels[0]}", *reasons,
            ]

        # HARD FILTER: REJECT → всегда DISLIKE
        if filter_decision == FilterDecision.REJECT:
            return AIDecision.DISLIKE, combined, [
                "FILTER_REJECTED", *reasons,
            ]

        # HARD FILTER: REVIEW → только REVIEW или DISLIKE, НЕ LIKE
        if filter_decision == FilterDecision.REVIEW:
            if combined >= cfg.review_threshold:
                return AIDecision.REVIEW, combined, [
                    "FILTER_REVIEW", *reasons,
                ]
            # LIKE-фактор не даёт потерять анкету, попавшую в REVIEW-фильтр.
            if like_labels and scoring.like_lifts_review:
                return AIDecision.REVIEW, combined, [
                    "FILTER_REVIEW", f"USER_LIKE:{like_labels[0]}", *reasons,
                ]
            return AIDecision.DISLIKE, combined, [
                "FILTER_REVIEW", *reasons,
            ]

        # PASS (или нет данных фильтра → трактуем как PASS)
        # AI недоступен (нет ни LLM, ни CLIP) → REVIEW/AI_UNAVAILABLE
        if llm_score is None and clip_score is None:
            return AIDecision.REVIEW, combined, [
                "AI_UNAVAILABLE", *reasons,
            ]

        if combined >= cfg.like_threshold:
            if confidence >= cfg.min_confidence:
                return AIDecision.LIKE, combined, [
                    "LIKE_THRESHOLD", *reasons,
                ]
            return AIDecision.REVIEW, combined, [
                "LOW_CONFIDENCE", *reasons,
            ]

        if combined >= cfg.review_threshold:
            return AIDecision.REVIEW, combined, [
                "REVIEW_THRESHOLD", *reasons,
            ]

        # BELOW_THRESHOLDS → DISLIKE, НО не теряем LIKE-факторы пользователя
        # (например «играет в игры» / «аниме» / «переехала в СПб») — они идут
        # в очередь REVIEW на подтверждение, а не в мусор.
        if like_labels and scoring.like_lifts_review:
            return AIDecision.REVIEW, combined, [
                "USER_LIKE", f"USER_LIKE:{like_labels[0]}", *reasons,
            ]

        return AIDecision.DISLIKE, combined, [
            "BELOW_THRESHOLDS", *reasons,
        ]

    # ── Сохранение и чтение ──────────────────────────────────────────

    async def _save(self, result: AIDecisionResult) -> None:
        """Сохраняет решение в ai_decisions."""
        row_id = await self._db.save_ai_decision(
            profile_id=result.profile_id,
            decision=result.decision.value,
            combined_score=result.combined_score,
            llm_score=result.llm_score,
            clip_score=result.clip_score,
            confidence=result.confidence,
            reasons=result.reasons_json(),
            scoring_version=result.scoring_version,
            evaluated_at=result.evaluated_at,
            prompt_version=result.prompt_version,
        )
        result.id = row_id

    async def get_latest(self, profile_id: int) -> AIDecisionResult | None:
        """Получает последнее AI-решение для профиля."""
        row = await self._db.get_latest_ai_decision(profile_id)
        if row is None:
            return None
        return self._row_to_result(row)

    async def get_history(self, profile_id: int) -> list[AIDecisionResult]:
        """Получает историю AI-решений для профиля."""
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
            llm_score=row.get("llm_score"),
            clip_score=row.get("clip_score"),
            confidence=row.get("confidence", 0.0),
            reasons=reasons,
            evaluated_at=row.get("evaluated_at", ""),
            scoring_version=row.get("scoring_version", "v1"),
            prompt_version=row.get("prompt_version", "llm-v1"),
        )

    def _log(self, result: AIDecisionResult, profile: Profile) -> None:
        """Логирует решение (OBSERVE — никаких действий)."""
        logger.info(
            f"AI DECISION: profile={profile.name} (#{result.profile_id}), "
            f"decision={result.decision}, combined={result.combined_score:.2f}, "
            f"confidence={result.confidence:.2f}, "
            f"reasons={result.reasons[:3]}, "
            f"scoring_version={result.scoring_version}"
        )

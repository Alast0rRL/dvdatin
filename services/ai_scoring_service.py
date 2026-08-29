# AI Scoring Service: объединение CLIP + LLM в единый AIScore (слой скоринга).
# Stage 5: добавляет API score_profile / score_text / score_images.
# НЕ выполняет Telegram-действий и НЕ принимает финальное решение (Decision Engine).

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from loguru import logger

from models.ai import AIScore, AIRecommendation, ConfidenceLevel, CLIPScore, LLMScore
from services.clip_service import BaseCLIPService
from services.llm_service import BaseLLMService

if TYPE_CHECKING:
    from app.config import AppConfig, ScoringConfig
    from database.database import Database
    from models.profile import Profile


class AIScoringService:
    """Сервис объединённого AI-скоринга профилей (слой скоринга).

    Отвечает только за получение скоров (LLM + CLIP), их комбинацию,
    confidence и причины. Финальное решение принимает DecisionService.
    """

    def __init__(
        self,
        db: Database,
        config: AppConfig,
        clip_service: BaseCLIPService,
        llm_service: BaseLLMService,
    ) -> None:
        self._db = db
        self._config = config
        self._clip = clip_service
        self._llm = llm_service
        self._scoring_config = config.ai.scoring

    @property
    def is_enabled(self) -> bool:
        """Проверяет, включён ли хотя бы один компонент AI."""
        return self._clip.is_enabled or self._llm.is_enabled

    # ── Stage 5 API ──────────────────────────────────────────────────

    async def score_text(self, profile: Profile) -> LLMScore | None:
        """Оценивает только текст анкеты через LLM.

        Возвращает LLMScore или None, если LLM отключён/недоступен.
        """
        if not self._llm.is_enabled:
            logger.debug("score_text: LLM отключён")
            return None
        try:
            return await self._llm.evaluate_profile(
                name=profile.name,
                age=profile.age,
                city=profile.normalized_city,
                description=profile.description,
            )
        except Exception as e:
            logger.error(f"score_text LLM ошибка для profile {profile.id}: {e}")
            return None

    async def score_images(
        self,
        profile: Profile,
        image_data_list: list[bytes] | None = None,
    ) -> CLIPScore | None:
        """Оценивает изображения через CLIP.

        Возвращает CLIPScore или None, если CLIP отключён/изображений нет.
        """
        if not self._clip.is_enabled:
            logger.debug("score_images: CLIP отключён")
            return None
        try:
            return await self._clip.score_images(image_data_list or [])
        except Exception as e:
            logger.error(f"score_images CLIP ошибка для profile {profile.id}: {e}")
            return None

    async def score_profile(
        self,
        profile: Profile,
        image_data_list: list[bytes] | None = None,
    ) -> AIScore:
        """Полный скоринг профиля (LLM + CLIP) без сохранения.

        Используется DecisionService. Не сохраняет результат в ai_scores.
        """
        return await self._compute(profile, image_data_list=image_data_list)

    # ── Существующий API (backward compat) ───────────────────────────

    async def evaluate(
        self,
        profile: Profile,
        image_data_list: list[bytes] | None = None,
    ) -> AIScore:
        """Оценивает профиль, комбинирует скоры и сохраняет результат."""
        ai_score = await self._compute(profile, image_data_list=image_data_list)
        await self._save(ai_score)

        logger.info(
            f"AI score: profile_id={profile.id}, "
            f"combined={ai_score.combined_score:.2f}, "
            f"recommendation={ai_score.recommendation.value}"
        )

        return ai_score

    # ── Внутренняя логика ────────────────────────────────────────────

    async def _compute(
        self,
        profile: Profile,
        image_data_list: list[bytes] | None = None,
    ) -> AIScore:
        """Вычисляет AIScore без сохранения."""
        clip_result = await self.score_images(profile, image_data_list)
        llm_result = await self.score_text(profile)

        # Сигналы считаются недоступными при отказе шлюза (истинный fail)
        llm_available = llm_result is not None and not self._llm_failed(llm_result)
        clip_available = (
            clip_result is not None
            and clip_result.image_count > 0
            and not self._clip_failed(clip_result)
        )

        clip_sig = clip_result if clip_available else None
        llm_sig = llm_result if llm_available else None

        combined = self._combine_scores(clip_sig, llm_sig)
        recommendation = self._determine_recommendation(combined)
        confidence_level, confidence_score = self._determine_confidence(clip_sig, llm_sig)
        reasons = self._collect_reasons(clip_sig, llm_sig)

        now = datetime.now(timezone.utc).isoformat()

        return AIScore(
            profile_id=profile.id,
            clip_score=clip_sig.aesthetic_score if clip_sig else None,
            llm_score=llm_sig.score if llm_sig else None,
            combined_score=combined,
            recommendation=recommendation,
            confidence=confidence_level,
            confidence_score=confidence_score,
            reasons=reasons,
            model_version=self._model_version(),
            created_at=now,
            prompt_version=(
                llm_sig.prompt_version if llm_sig else "llm-v1"
            ),
        )

    @staticmethod
    def _llm_failed(llm_result: LLMScore | None) -> bool:
        """True, если LLM не вернул полезный результат (шлюз недоступен)."""
        if llm_result is None:
            return True
        return any("недоступен" in r for r in llm_result.reasons)

    @classmethod
    def _clip_failed(cls, clip_result: CLIPScore | None) -> bool:
        """True, если CLIP не вернул полезный результат (шлюз недоступен)."""
        if clip_result is None:
            return True
        return "недоступен" in clip_result.description

    def _combine_scores(
        self,
        clip: CLIPScore | None,
        llm: LLMScore | None,
    ) -> float:
        """Объединяет скоры CLIP и LLM.

        Если один компонент отсутствует — веса пересчитываются.
        """
        scoring = self._scoring_config
        has_clip = clip is not None and clip.image_count > 0
        has_llm = llm is not None

        if not has_clip and not has_llm:
            return 0.0

        if has_clip and has_llm:
            raw = (
                clip.aesthetic_score * scoring.clip_weight
                + llm.score * scoring.llm_weight
            )
            return min(max(raw, 0.0), 1.0)

        if has_clip:
            return clip.aesthetic_score

        if has_llm:
            return llm.score

        return 0.0

    def _determine_recommendation(self, combined: float) -> AIRecommendation:
        """Определяет рекомендацию на основе combined score."""
        scoring = self._scoring_config

        if combined >= scoring.like_threshold:
            return AIRecommendation.LIKE

        if combined <= scoring.dislike_threshold:
            return AIRecommendation.DISLIKE

        return AIRecommendation.REVIEW

    def _determine_confidence(
        self,
        clip: CLIPScore | None,
        llm: LLMScore | None,
    ) -> tuple[ConfidenceLevel, float]:
        """Определяет уровень уверенности."""
        has_clip = clip is not None and clip.image_count > 0
        has_llm = llm is not None

        if has_clip and has_llm:
            avg_confidence = (
                (0.8 + llm.confidence) / 2
                if llm else 0.8
            )
            return ConfidenceLevel.HIGH, min(avg_confidence, 1.0)

        if has_llm:
            return ConfidenceLevel.MEDIUM, llm.confidence * 0.8

        if has_clip:
            return ConfidenceLevel.MEDIUM, 0.5

        return ConfidenceLevel.LOW, 0.0

    def _collect_reasons(
        self,
        clip: CLIPScore | None,
        llm: LLMScore | None,
    ) -> list[str]:
        """Собирает причины от обоих компонентов."""
        reasons: list[str] = []

        if clip and clip.image_count > 0:
            reasons.append(f"Проанализировано {clip.image_count} фото")
            if clip.aesthetic_score > 0.6:
                reasons.append("Фотографии выше среднего качества")
            elif clip.aesthetic_score < 0.3:
                reasons.append("Фотографии низкого качества")

        if llm:
            reasons.extend(llm.reasons)

        if not reasons:
            reasons.append("Нет данных для анализа")

        return reasons

    def _model_version(self) -> str:
        """Формирует строку версии моделей."""
        parts = []
        if self._clip.is_enabled:
            parts.append(f"clip={self._config.ai.clip.model}")
        if self._llm.is_enabled:
            parts.append(f"llm={self._config.ai.llm.model}")
        return "+".join(parts) if parts else "none"

    async def _save(self, score: AIScore) -> None:
        """Сохраняет результат в БД."""
        await self._db.save_ai_score(
            profile_id=score.profile_id,
            clip_score=score.clip_score,
            llm_score=score.llm_score,
            combined_score=score.combined_score,
            recommendation=score.recommendation.value,
            confidence=score.confidence.value,
            confidence_score=score.confidence_score,
            reasons=score.reasons_json(),
            model_version=score.model_version,
            created_at=score.created_at,
        )

    async def get_latest(self, profile_id: int) -> AIScore | None:
        """Получает последний AI-скор для профиля."""
        row = await self._db.get_latest_ai_score(profile_id)
        if row is None:
            return None
        return self._row_to_score(row)

    async def get_history(self, profile_id: int) -> list[AIScore]:
        """Получает историю AI-скоров для профиля."""
        rows = await self._db.get_ai_score_history(profile_id)
        return [self._row_to_score(row) for row in rows]

    @staticmethod
    def _row_to_score(row: dict) -> AIScore:
        """Преобразует dict из БД в AIScore."""
        import json
        reasons_raw = row.get("reasons", "[]")
        reasons = json.loads(reasons_raw) if isinstance(reasons_raw, str) else reasons_raw

        return AIScore(
            profile_id=row["profile_id"],
            clip_score=row.get("clip_score"),
            llm_score=row.get("llm_score"),
            combined_score=row.get("combined_score", 0.0),
            recommendation=AIRecommendation(row["recommendation"]),
            confidence=ConfidenceLevel(row.get("confidence", "LOW")),
            confidence_score=row.get("confidence_score", 0.0),
            reasons=reasons,
            model_version=row.get("model_version", ""),
            created_at=row.get("created_at", ""),
            prompt_version=row.get("prompt_version", "llm-v1"),
        )

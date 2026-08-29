# ReviewService: Human Review слой (Stage 6).
# Очередь рецензий + сохранение решений человека поверх AI Decision Engine.
#
# ПРАВИЛА:
# - Только OBSERVE/REVIEW. НИКАКИХ Telegram-действий.
# - Telegram-free: не импортирует Telethon.
# - История никогда не перезаписывается: новая рецензия — новая запись.
# - Одна AI-оценка (ai_decision_id) может быть рассмотрена один раз;
#   новая AI-оценка создаёт новый ai_decision_id и может снова попасть в очередь.

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from loguru import logger

from models.human_decision import (
    AgreementStatus,
    HumanDecision,
    HumanDecisionResult,
)

if TYPE_CHECKING:
    from database.database import Database
    from models.decision import AIDecision
    from models.profile import Profile
    from services.profile_service import ProfileService


@dataclass
class ReviewItem:
    """Элемент очереди рецензий: AI-оценка + профиль + фильтр (для UI)."""

    profile: Profile
    ai_decision_id: int
    ai_decision: str
    combined_score: float
    llm_score: float | None
    clip_score: float | None
    confidence: float
    reasons: list[str] = field(default_factory=list)
    scoring_version: str = "v1"
    prompt_version: str = "llm-v1"
    filter_decision: str = ""


class ReviewService:
    """Сервис ручной рецензии AI-решений."""

    def __init__(
        self,
        db: Database,
        profile_service: ProfileService,
    ) -> None:
        self._db = db
        self._profiles = profile_service

    # ── Очередь рецензий ─────────────────────────────────────────────

    async def get_next(self) -> ReviewItem | None:
        """Возвращает самую старую неразобранную AI-оценку (oldest first)."""
        row = await self._db.get_pending_review()
        if row is None:
            return None
        return await self._build_review_item(row)

    async def get_pending(self) -> list[dict]:
        """Возвращает все неразобранные AI-оценки (oldest first)."""
        return await self._db.get_pending_reviews()

    async def get_pending_count(self) -> int:
        """Количество неразобранных AI-оценок."""
        return await self._db.get_pending_count()

    async def get_profile_for_review(self, profile_id: int) -> ReviewItem | None:
        """Возвращает элемент для рецензии по конкретному профилю.

        Использует последнюю неразобранную AI-оценку профиля, если она есть.
        """
        row = await self._db.get_latest_ai_decision(profile_id)
        if row is None:
            return None
        if await self._db.is_human_reviewed(profile_id, row["id"]):
            return None
        return await self._build_review_item(row)

    # ── Сохранение решения ───────────────────────────────────────────

    async def save_decision(
        self,
        profile_id: int,
        ai_decision_id: int,
        decision: HumanDecision,
    ) -> HumanDecisionResult:
        """Сохраняет рецензию человека для конкретной AI-оценки.

        Raises:
            ValueError: Если AI-оценка не найдена / не связана с профилем,
                либо (profile_id, ai_decision_id) уже была рассмотрена.
        """
        row = await self._db.get_ai_decision_for_profile_prompt(
            profile_id, ai_decision_id,
        )
        if row is None:
            msg = (
                f"AI-решение {ai_decision_id} для профиля {profile_id} "
                f"не найдено"
            )
            raise ValueError(msg)

        if await self._db.is_human_reviewed(profile_id, ai_decision_id):
            msg = f"AI-решение {ai_decision_id} уже рассмотрено"
            raise ValueError(msg)

        human = HumanDecision(decision)
        agreement = AgreementStatus.from_human(human)
        now = datetime.now(timezone.utc).isoformat()

        review_id = await self._db.save_human_decision(
            profile_id=profile_id,
            ai_decision_id=ai_decision_id,
            decision=human.value,
            agreement=agreement.value,
            created_at=now,
        )

        result = HumanDecisionResult(
            id=review_id,
            profile_id=profile_id,
            ai_decision_id=ai_decision_id,
            decision=human,
            agreement=agreement,
            created_at=now,
        )

        logger.info(
            f"HUMAN DECISION: profile=#{profile_id}, ai_decision=#{ai_decision_id}, "
            f"human={human}, agreement={agreement}"
        )
        return result

    # ── История ──────────────────────────────────────────────────────

    async def get_history(self, profile_id: int) -> list[dict]:
        """Получает историю рецензий профиля (новейшие сверху)."""
        return await self._db.get_human_decision_history(profile_id)

    async def is_reviewed(self, profile_id: int, ai_decision_id: int) -> bool:
        """Проверяет, рассмотрена ли конкретная AI-оценка."""
        return await self._db.is_human_reviewed(profile_id, ai_decision_id)

    async def latest_for_profile(self, profile_id: int) -> dict | None:
        """Последняя рецензия профиля."""
        return await self._db.get_latest_human_decision(profile_id)

    async def resolve_ai_decision(self, ai_decision_id: int) -> int | None:
        """Возвращает profile_id для AI-решения или None, если его нет.

        Нужен UI (callback-обработке), чтобы узнать профиль AI-оценки
        перед сохранением рецензии.
        """
        row = await self._db.get_ai_decision(ai_decision_id)
        if row is None:
            return None
        return row["profile_id"]

    async def get_review_details(self, profile_id: int) -> dict | None:
        """Собирает всё, что нужно для отображения профиля в review UI.

        Возвращает dict с ключами:
            profile (dict|None), latest_filter (dict|None),
            latest_ai (dict|None), latest_human (dict|None),
            history (list[dict]).
        None, если профиль не существует.
        """
        profile = await self._db.get_profile_by_id(profile_id)
        if profile is None:
            return None
        return {
            "profile": profile,
            "latest_filter": await self._db.get_latest_filter_result(profile_id),
            "latest_ai": await self._db.get_latest_ai_decision(profile_id),
            "latest_human": await self._db.get_latest_human_decision(profile_id),
            "history": await self._db.get_human_decision_history(profile_id),
        }

    # ── Helpers ──────────────────────────────────────────────────────

    async def _build_review_item(self, row: dict) -> ReviewItem | None:
        """Собирает ReviewItem из строки ai_decisions + профиль + фильтр."""
        profile = await self._profiles.get_profile(row["profile_id"])
        if profile is None:
            logger.warning(
                f"Review: profile {row['profile_id']} не найден, пропуск"
            )
            return None

        filter_dec = ""
        latest_filter = await self._db.get_latest_filter_result(profile.id)
        if latest_filter:
            filter_dec = latest_filter.get("decision", "")

        reasons_raw = row.get("reasons", "[]")
        reasons = (
            json.loads(reasons_raw)
            if isinstance(reasons_raw, str)
            else reasons_raw
        )

        return ReviewItem(
            profile=profile,
            ai_decision_id=row["id"],
            ai_decision=row.get("decision", ""),
            combined_score=row.get("combined_score", 0.0),
            llm_score=row.get("llm_score"),
            clip_score=row.get("clip_score"),
            confidence=row.get("confidence", 0.0),
            reasons=reasons,
            scoring_version=row.get("scoring_version", "v1"),
            prompt_version=row.get("prompt_version", "llm-v1"),
            filter_decision=filter_dec,
        )

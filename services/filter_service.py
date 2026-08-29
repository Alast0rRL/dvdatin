# FilterService: оценка профилей + сохранение истории.

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from loguru import logger

from models.filter import FilterDecision, FilterResult
from models.profile import Profile
from services.filter_engine import FilterEngine

if TYPE_CHECKING:
    from database.database import Database
    from services.profile_service import ProfileService


class FilterService:
    """Сервис фильтрации профилей."""

    def __init__(
        self,
        db: Database,
        profile_service: ProfileService,
        filter_engine: FilterEngine,
    ) -> None:
        self._db = db
        self._profile_service = profile_service
        self._engine = filter_engine

    async def evaluate_profile(self, profile_id: int) -> FilterResult | None:
        """Оценивает профиль по ID и сохраняет результат."""
        profile = await self._profile_service.get_profile(profile_id)
        if profile is None:
            logger.warning(f"Profile {profile_id} not found for evaluation")
            return None
        return await self.evaluate(profile)

    async def evaluate(self, profile: Profile) -> FilterResult:
        """Оценивает профиль и сохраняет результат."""
        result = self._engine.evaluate(profile)

        await self._db.save_filter_result(
            profile_id=profile.id,
            decision=result.decision,
            reasons=result.reasons_json(),
            rules_checked=result.rules_checked,
            evaluated_at=result.evaluated_at,
        )

        logger.info(
            f"Filter evaluated: profile_id={profile.id}, "
            f"decision={result.decision}"
        )

        return result

    async def get_latest_result(self, profile_id: int) -> FilterResult | None:
        """Получает последний результат фильтрации."""
        row = await self._db.get_latest_filter_result(profile_id)
        if row is None:
            return None
        return self._row_to_result(row)

    async def get_history(self, profile_id: int) -> list[FilterResult]:
        """Получает историю фильтрации."""
        rows = await self._db.get_filter_history(profile_id)
        return [self._row_to_result(row) for row in rows]

    @staticmethod
    def _row_to_result(row: dict) -> FilterResult:
        """Преобразует dict из БД в FilterResult."""
        reasons_raw = json.loads(row.get("reasons", "[]"))
        from models.filter import FilterReason
        reasons = [FilterReason(r) for r in reasons_raw]

        return FilterResult(
            profile_id=row["profile_id"],
            decision=FilterDecision(row["decision"]),
            reasons=reasons,
            rules_checked=row.get("rules_checked", 0),
            evaluated_at=row.get("evaluated_at", ""),
        )

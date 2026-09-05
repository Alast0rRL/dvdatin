# AnalyticsService: агрегированная статистика системы (Stage 6).
#
# ПРАВИЛА:
# - READ ONLY: НЕ изменяет AI decisions, prompts, thresholds, weights, profiles.
# - Telegram-free: не импортирует Telethon.
# - Работает только с profile_id и агрегатами (privacy-safe).

from __future__ import annotations

from typing import TYPE_CHECKING

from models.human_decision import AgreementStatus

if TYPE_CHECKING:
    from database.database import Database


class AnalyticsService:
    """Read-only сервис аналитики."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ── Обзор ────────────────────────────────────────────────────────

    async def get_overview(self) -> dict:
        """Общая статистика системы."""
        return {
            "profiles": await self._db.count_profiles(),
            "filter": {
                "PASS": await self._db.count_filter_results("PASS"),
                "REVIEW": await self._db.count_filter_results("REVIEW"),
                "REJECT": await self._db.count_filter_results("REJECT"),
            },
            "ai": await self.get_ai_stats(),
            "human": await self.get_human_stats(),
        }

    # ── AI stats ─────────────────────────────────────────────────────

    async def get_ai_stats(self) -> dict:
        """Статистика AI-решений."""
        decisions = await self._db.get_all_ai_decisions()
        counts = {"LIKE": 0, "REVIEW": 0, "DISLIKE": 0}

        combined_vals, conf_vals = [], []
        for d in decisions:
            counts[d["decision"]] = counts.get(d["decision"], 0) + 1
            combined_vals.append(d.get("combined_score", 0.0))
            conf_vals.append(d.get("confidence", 0.0))

        total = len(decisions)
        ai_reviews = await self._db.get_all_human_history()

        return {
            "total": total,
            "counts": counts,
            "average": {
                "combined_score": _avg(combined_vals),
                "confidence": _avg(conf_vals),
            },
            "reviewed": len(ai_reviews),
            "pending": await self._db.get_pending_count(),
        }

    # ── Human stats ──────────────────────────────────────────────────

    async def get_human_stats(self) -> dict:
        """Статистика решений человека."""
        humans = await self._db.get_all_human_history()
        counts = {"APPROVE": 0, "REJECT": 0, "SKIP": 0}
        for h in humans:
            counts[h["decision"]] = counts.get(h["decision"], 0) + 1
        return counts

    # ── Agreement stats ──────────────────────────────────────────────

    async def get_agreement_stats(self) -> dict:
        """Статистика согласия AI ↔ Human (Agreement Rate)."""
        reviews = await self._db.get_human_reviews_with_ai()
        agree = sum(1 for r in reviews if r["agreement"] == AgreementStatus.AGREEMENT)
        disagree = sum(
            1 for r in reviews if r["agreement"] == AgreementStatus.DISAGREEMENT
        )
        unresolved = sum(
            1 for r in reviews if r["agreement"] == AgreementStatus.UNRESOLVED
        )

        # SKIP исключается из denominator; если denominator = 0 → rate = None
        denominator = agree + disagree
        rate = (agree / denominator) if denominator > 0 else None

        return {
            "agreement": agree,
            "disagreement": disagree,
            "unresolved": unresolved,
            "agreement_rate": rate,
        }

    # ── Disagreements ────────────────────────────────────────────────

    async def get_disagreements(
        self, sort: str = "newest",
    ) -> list[dict]:
        """Профили, где Human = REJECT для существующего AI-решения.

        Args:
            sort: "newest" | "score" | "confidence".
        """
        reviews = await self._db.get_human_reviews_with_ai()
        disagreements = [r for r in reviews if r["human_decision"] == "REJECT"]

        if sort == "score":
            disagreements.sort(key=lambda r: r["combined_score"], reverse=True)
        elif sort == "confidence":
            # низшая уверенность первой (требует внимания рецензента)
            disagreements.sort(key=lambda r: r["confidence"])
        else:  # newest
            disagreements.sort(key=lambda r: r["reviewed_at"], reverse=True)

        return disagreements

    # ── Pending count ────────────────────────────────────────────────

    async def get_pending_count(self) -> int:
        """Количество неразобранных AI-оценок."""
        return await self._db.get_pending_count()

def _avg(values: list[float]) -> float | None:
    """Среднее арифметическое или None для пустого списка."""
    if not values:
        return None
    return sum(values) / len(values)

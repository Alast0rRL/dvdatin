# AnalyticsService: агрегированная статистика системы (Stage 6).
#
# ПРАВИЛА:
# - READ ONLY: НЕ изменяет AI decisions, prompts, thresholds, weights, profiles.
# - Telegram-free: не импортирует Telethon.
# - Работает только с profile_id и агрегатами (privacy-safe).
# - Пороги распределения скоров берутся из config (ai.decision), не хардкодятся.

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from models.human_decision import AgreementStatus, HumanDecision

if TYPE_CHECKING:
    from app.config import AppConfig
    from database.database import Database


class AnalyticsService:
    """Read-only сервис аналитики."""

    def __init__(self, db: Database, config: AppConfig) -> None:
        self._db = db
        self._config = config
        self._decision_cfg = config.ai.decision

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

        llm_vals, clip_vals, combined_vals, conf_vals = [], [], [], []
        for d in decisions:
            counts[d["decision"]] = counts.get(d["decision"], 0) + 1
            if d.get("llm_score") is not None:
                llm_vals.append(d["llm_score"])
            if d.get("clip_score") is not None:
                clip_vals.append(d["clip_score"])
            combined_vals.append(d.get("combined_score", 0.0))
            conf_vals.append(d.get("confidence", 0.0))

        total = len(decisions)
        ai_reviews = await self._db.get_all_human_history()

        return {
            "total": total,
            "counts": counts,
            "average": {
                "llm_score": _avg(llm_vals),
                "clip_score": _avg(clip_vals),
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

    # ── Score distribution ───────────────────────────────────────────

    async def get_score_distribution(self) -> dict:
        """Распределение combined_score по диапазонам (пороги из config).

        LIKE    : [like_threshold, 1.00]
        REVIEW  : [review_threshold, like_threshold)
        DISLIKE : [0.00, review_threshold)
        """
        like_t = self._decision_cfg.like_threshold
        review_t = self._decision_cfg.review_threshold

        dist = {"LIKE": 0, "REVIEW": 0, "DISLIKE": 0}
        decisions = await self._db.get_all_ai_decisions()
        for d in decisions:
            s = d.get("combined_score", 0.0)
            if s >= like_t:
                dist["LIKE"] += 1
            elif s >= review_t:
                dist["REVIEW"] += 1
            else:
                dist["DISLIKE"] += 1

        return {
            "bins": {
                "LIKE": {"min": like_t, "max": 1.0, "count": dist["LIKE"]},
                "REVIEW": {
                    "min": review_t, "max": like_t, "count": dist["REVIEW"],
                },
                "DISLIKE": {"min": 0.0, "max": review_t, "count": dist["DISLIKE"]},
            },
            "thresholds": {"like": like_t, "review": review_t},
        }

    # ── Breakdowns ───────────────────────────────────────────────────

    async def get_ai_breakdown(self) -> dict:
        """AI Decision × Human Decision breakdown (bias/ошибки scoring)."""
        reviews = await self._db.get_human_reviews_with_ai()
        table: dict[str, dict[str, int]] = defaultdict(
            lambda: defaultdict(int),
        )
        for r in reviews:
            ai = r["ai_decision"]
            human = r["human_decision"]
            table[ai][human] += 1
        return {k: dict(v) for k, v in sorted(table.items())}

    async def get_filter_breakdown(self) -> dict:
        """Filter PASS → AI decision breakdown (по последнему фильтру профиля)."""
        decisions = await self._db.get_all_ai_decisions()
        by_id: dict[int, str] = {}
        for row in await self._db.get_profiles_last_filter():
            by_id[row["profile_id"]] = row["filter_decision"]

        table: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for d in decisions:
            fdec = by_id.get(d["profile_id"], "")
            table[fdec][d["decision"]] += 1
        return {k: dict(v) for k, v in sorted(table.items())}

    async def get_scoring_version_breakdown(self) -> dict:
        """Agreement Rate по scoring_version."""
        reviews = await self._db.get_human_reviews_with_ai()
        by_version: dict[str, dict] = {}
        for r in reviews:
            ver = r["scoring_version"]
            item = by_version.setdefault(ver, {"agreement": 0, "disagreement": 0})
            if r["agreement"] == AgreementStatus.AGREEMENT:
                item["agreement"] += 1
            elif r["agreement"] == AgreementStatus.DISAGREEMENT:
                item["disagreement"] += 1

        for item in by_version.values():
            denom = item["agreement"] + item["disagreement"]
            item["agreement_rate"] = (
                (item["agreement"] / denom) if denom > 0 else None
            )
        return by_version

    async def get_prompt_version_breakdown(self) -> dict:
        """Agreement Rate по prompt_version."""
        reviews = await self._db.get_human_reviews_with_ai()
        by_prompt: dict[str, dict] = {}
        for r in reviews:
            pv = r["prompt_version"]
            item = by_prompt.setdefault(pv, {"agreement": 0, "disagreement": 0})
            if r["agreement"] == AgreementStatus.AGREEMENT:
                item["agreement"] += 1
            elif r["agreement"] == AgreementStatus.DISAGREEMENT:
                item["disagreement"] += 1

        for item in by_prompt.values():
            denom = item["agreement"] + item["disagreement"]
            item["agreement_rate"] = (
                (item["agreement"] / denom) if denom > 0 else None
            )
        return by_prompt


def _avg(values: list[float]) -> float | None:
    """Среднее арифметическое или None для пустого списка."""
    if not values:
        return None
    return sum(values) / len(values)

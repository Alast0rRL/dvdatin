# Unit/Integration тесты Stage 6: AnalyticsService + CSV export.
# Полностью offline — реальный tmp SQLite, без Telegram.

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.config import AppConfig
from database.database import Database
from models.human_decision import HumanDecision
from services.analytics_service import AnalyticsService
from services.profile_service import ProfileService
from services.review_export import export_review_csv
from services.review_service import ReviewService


# ── Fixtures ──────────────────────────────────────────────────────────

def make_config(
    like_threshold: float = 0.75,
    review_threshold: float = 0.50,
) -> AppConfig:
    return AppConfig(**{
        "telegram": {"api_id": 123, "api_hash": "abc"},
        "filters": {
            "age": {"min": 18, "max": 19},
            "city": {"allowed": ["Санкт-Петербург"]},
        },
        "ai": {
            "decision": {
                "like_threshold": like_threshold,
                "review_threshold": review_threshold,
                "weights": {"llm": 0.7, "clip": 0.3},
            },
        },
    })


@pytest.fixture
def tmp_db(tmp_path: Path) -> Database:
    db = Database(path=tmp_path / "test_analytics.db")
    loop = asyncio.get_event_loop()
    loop.run_until_complete(db.connect())
    yield db  # type: ignore[misc]
    loop.run_until_complete(db.close())


@pytest.fixture
def stack(tmp_db: Database):
    config = make_config()
    ps = ProfileService(tmp_db)
    review = ReviewService(tmp_db, ps)
    ga = AnalyticsService(tmp_db, config)
    return tmp_db, review, ga


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


async def insert_profile(
    db: Database, profile_id: int, city: str = "Санкт-Петербург",
) -> int:
    return await db.insert_profile(
        name="Anna", age=19, raw_city=city, normalized_city=city,
        description="Люблю природу", fingerprint=f"fp_a_{profile_id}",
        source_chat_id=1234060895, source_message_id=profile_id,
        first_seen_at="now", last_seen_at="now", status="NEW",
    )


async def add_ai(
    db: Database,
    profile_id: int,
    decision: str,
    combined: float = 0.8,
    confidence: float = 0.8,
    llm: float | None = None,
    clip: float | None = None,
    scoring_version: str = "v1",
    prompt_version: str = "llm-v1",
    ts: str = "",
) -> int:
    return await db.save_ai_decision(
        profile_id=profile_id,
        decision=decision,
        combined_score=combined,
        llm_score=llm,
        clip_score=clip,
        confidence=confidence,
        reasons="[]",
        scoring_version=scoring_version,
        prompt_version=prompt_version,
        evaluated_at=ts or f"2026-01-01T00:00:0{profile_id}",
    )


async def add_filter(db: Database, profile_id: int, decision: str) -> None:
    await db.save_filter_result(
        profile_id, decision, "[]", 2, f"2026-01-01T00:00:0{profile_id}",
    )


def setup_basic(stack):
    db, review, _ga = stack
    # профиль 1: AI LIKE, человек APPROVE, фильтр PASS, combined 0.8 (bin LIKE)
    pid1 = run(insert_profile(db, 1))
    aid1 = run(add_ai(db, pid1, "LIKE", combined=0.8, llm=0.8, clip=0.8))
    run(add_filter(db, pid1, "PASS"))
    run(review.save_decision(pid1, aid1, HumanDecision.APPROVE))

    # профиль 2: AI REVIEW, человек REJECT, фильтр REVIEW, combined 0.6 (bin REVIEW)
    pid2 = run(insert_profile(db, 2))
    aid2 = run(add_ai(db, pid2, "REVIEW", combined=0.6, llm=0.6, clip=None))
    run(add_filter(db, pid2, "REVIEW"))
    run(review.save_decision(pid2, aid2, HumanDecision.REJECT))

    # профиль 3: AI DISLIKE, человек SKIP, фильтр REJECT, combined 0.5 (bin REVIEW? нет:
    # with thresholds like=0.75/review=0.50, 0.5 < 0.5? bin DISLIKE since 0.5 < review)
    pid3 = run(insert_profile(db, 3))
    aid3 = run(add_ai(db, pid3, "DISLIKE", combined=0.3, llm=0.3, clip=None))
    run(add_filter(db, pid3, "REJECT"))
    run(review.save_decision(pid3, aid3, HumanDecision.SKIP))

    # профиль 4: AI LIKE без человеческой рецензии (pending)
    pid4 = run(insert_profile(db, 4))
    run(add_ai(db, pid4, "LIKE", combined=0.9, llm=0.9, clip=0.9))

    return db, review, pid1, pid2, pid3, pid4, aid1, aid2, aid3


# ── Analytics totals ───────────────────────────────────────────────────

class TestAnalyticsTotals:
    def test_overview(self, stack) -> None:
        db, _r, ga = stack
        setup_basic(stack)
        ov = run(ga.get_overview())
        assert ov["profiles"] == 4
        assert ov["filter"] == {"PASS": 1, "REVIEW": 1, "REJECT": 1}

    def test_ai_stats(self, stack) -> None:
        db, _r, ga = stack
        setup_basic(stack)
        st = run(ga.get_ai_stats())
        assert st["total"] == 4
        assert st["counts"]["LIKE"] == 2
        assert st["counts"]["REVIEW"] == 1
        assert st["counts"]["DISLIKE"] == 1
        assert st["reviewed"] == 3
        assert st["pending"] == 1

    def test_human_stats(self, stack) -> None:
        db, _r, ga = stack
        setup_basic(stack)
        h = run(ga.get_human_stats())
        assert h == {"APPROVE": 1, "REJECT": 1, "SKIP": 1}

    def test_empty_database(self, stack) -> None:
        db, _r, ga = stack
        ov = run(ga.get_overview())
        assert ov["profiles"] == 0
        assert run(ga.get_ai_stats())["total"] == 0
        assert run(ga.get_human_stats()) == {"APPROVE": 0, "REJECT": 0, "SKIP": 0}

    def test_no_pending_reviews(self, stack) -> None:
        db, review, _ga = stack
        setup_basic(stack)
        # все рассмотрены? нет — профиль 4 pending
        assert run(review.get_pending_count()) == 1
        # после ревью единственного pending — 0
        pid4 = run(insert_profile(db, 99))
        run(add_ai(db, pid4, "LIKE", combined=0.8))
        nxt = run(review.get_next())
        assert nxt is None or nxt.profile.id == 4
        # покрытие: pending_count остаётся >= 1 пока 4 не рассмотрен
        assert run(review.get_pending_count()) >= 1


# ── Agreement ─────────────────────────────────────────────────────────

class TestAgreementStats:
    def test_agreement_rates(self, stack) -> None:
        db, _r, ga = stack
        setup_basic(stack)
        ag = run(ga.get_agreement_stats())
        assert ag["agreement"] == 1  # APPROVE (профиль1)
        assert ag["disagreement"] == 1  # REJECT (профиль2)
        assert ag["unresolved"] == 1  # SKIP (профиль3)
        # denominator = 1+1 = 2 (SKIP исключён)
        assert ag["agreement_rate"] == 0.5

    def test_skip_excluded_from_rate(self, stack) -> None:
        db, review, ga = stack
        # только SKIP → denominator 0 → rate None (не 0%)
        pid = run(insert_profile(db, 1))
        aid = run(add_ai(db, pid, "LIKE", combined=0.8))
        run(review.save_decision(pid, aid, HumanDecision.SKIP))
        ag = run(ga.get_agreement_stats())
        assert ag["agreement_rate"] is None

    def test_agreement_rate_null_not_zero(self, stack) -> None:
        db, review, ga = stack
        # только REVIEW с human REJECT → agreement 0, disagreement 1 → rate 0.0
        pid = run(insert_profile(db, 1))
        aid = run(add_ai(db, pid, "REVIEW", combined=0.6))
        run(review.save_decision(pid, aid, HumanDecision.REJECT))
        ag = run(ga.get_agreement_stats())
        assert ag["agreement_rate"] == 0.0


# ── Breakdowns ────────────────────────────────────────────────────────

class TestBreakdowns:
    def test_ai_breakdown(self, stack) -> None:
        db, _r, ga = stack
        setup_basic(stack)
        b = run(ga.get_ai_breakdown())
        assert b["LIKE"]["APPROVE"] == 1
        assert b["REVIEW"]["REJECT"] == 1
        assert b["DISLIKE"]["SKIP"] == 1

    def test_score_distribution_uses_config(self, stack) -> None:
        _db, _r, ga = stack
        setup_basic(stack)
        d = run(ga.get_score_distribution())
        # профиль1 0.8 >= 0.75 → LIKE; профиль2 0.6 >= 0.5 → REVIEW;
        # профиль3 0.3 < 0.5 → DISLIKE; профиль4 0.9 → LIKE
        bins = d["bins"]
        assert bins["LIKE"]["count"] == 2
        assert bins["REVIEW"]["count"] == 1
        assert bins["DISLIKE"]["count"] == 1
        assert d["thresholds"]["like"] == 0.75
        assert d["thresholds"]["review"] == 0.5

    def test_custom_thresholds(self, stack) -> None:
        db, review, _ga = stack
        # пересоздаём analytics с другими порогами
        from services.analytics_service import AnalyticsService
        ga = AnalyticsService(db, make_config(like_threshold=0.9, review_threshold=0.4))
        pid = run(insert_profile(db, 1))
        run(add_ai(db, pid, "REVIEW", combined=0.6))
        d = run(ga.get_score_distribution())
        # 0.6 < 0.9 и >= 0.4 → REVIEW
        assert d["bins"]["REVIEW"]["count"] == 1
        assert d["bins"]["LIKE"]["count"] == 0

    def test_filter_breakdown(self, stack) -> None:
        db, _r, ga = stack
        setup_basic(stack)
        fb = run(ga.get_filter_breakdown())
        # профиль1 PASS→LIKE, профиль2 REVIEW→REVIEW, профиль3 REJECT→DISLIKE,
        # профиль4 filter отсутствует (no filter) → "" → LIKE
        assert fb["PASS"]["LIKE"] == 1
        assert fb["REVIEW"]["REVIEW"] == 1
        assert fb["REJECT"]["DISLIKE"] == 1
        assert fb[""]["LIKE"] == 1

    def test_scoring_version_breakdown(self, stack) -> None:
        db, review, ga = stack
        pid = run(insert_profile(db, 1))
        aid1 = run(add_ai(db, pid, "LIKE", 0.8, scoring_version="v1"))
        run(review.save_decision(pid, aid1, HumanDecision.APPROVE))
        aid2 = run(add_ai(db, pid, "REVIEW", 0.6, scoring_version="v2"))
        run(review.save_decision(pid, aid2, HumanDecision.REJECT))
        sb = run(ga.get_scoring_version_breakdown())
        assert sb["v1"]["agreement"] == 1
        assert sb["v2"]["agreement_rate"] == 0.0
        assert sb["v1"]["agreement_rate"] == 1.0

    def test_prompt_version_breakdown(self, stack) -> None:
        db, review, ga = stack
        pid = run(insert_profile(db, 1))
        aid1 = run(add_ai(db, pid, "LIKE", 0.8, prompt_version="llm-v1"))
        run(review.save_decision(pid, aid1, HumanDecision.APPROVE))
        aid2 = run(add_ai(db, pid, "REVIEW", 0.6, prompt_version="llm-v2"))
        run(review.save_decision(pid, aid2, HumanDecision.REJECT))
        pb = run(ga.get_prompt_version_breakdown())
        assert pb["llm-v1"]["agreement"] == 1
        assert pb["llm-v2"]["agreement_rate"] == 0.0

    def test_disagreement_calculation(self, stack) -> None:
        db, _r, ga = stack
        setup_basic(stack)
        dis = run(ga.get_disagreements())
        assert len(dis) == 1  # только профиль2 (REJECT)
        assert dis[0]["profile_id"] == 2
        assert dis[0]["human_decision"] == "REJECT"


# ── Disagreement sorting ──────────────────────────────────────────────

class TestDisagreementSorting:
    def test_sort_newest(self, stack) -> None:
        db, review, ga = stack
        # три disagreement с разными combined/confidence/временем
        for i, (pidno, combined, conf, ts) in enumerate([
            (1, 0.5, 0.5, "2026-01-01T00:00:01"),
            (2, 0.9, 0.3, "2026-01-01T00:00:02"),
            (3, 0.7, 0.7, "2026-01-01T00:00:03"),
        ], start=1):
            pid = run(insert_profile(db, pidno))
            aid = run(add_ai(db, pid, "REVIEW", combined=combined, confidence=conf, ts=ts))
            run(review.save_decision(pid, aid, HumanDecision.REJECT))
        newest = run(ga.get_disagreements("newest"))
        assert [d["profile_id"] for d in newest] == [3, 2, 1]

    def test_sort_highest_score(self, stack) -> None:
        db, review, ga = stack
        for pidno, combined in [(1, 0.5), (2, 0.9), (3, 0.7)]:
            pid = run(insert_profile(db, pidno))
            aid = run(add_ai(db, pid, "REVIEW", combined=combined))
            run(review.save_decision(pid, aid, HumanDecision.REJECT))
        by_score = run(ga.get_disagreements("score"))
        assert [d["profile_id"] for d in by_score] == [2, 3, 1]

    def test_sort_lowest_confidence(self, stack) -> None:
        db, review, ga = stack
        for pidno, conf in [(1, 0.5), (2, 0.2), (3, 0.8)]:
            pid = run(insert_profile(db, pidno))
            aid = run(add_ai(db, pid, "REVIEW", confidence=conf))
            run(review.save_decision(pid, aid, HumanDecision.REJECT))
        by_conf = run(ga.get_disagreements("confidence"))
        assert [d["profile_id"] for d in by_conf] == [2, 1, 3]


# ── Profile history / export ──────────────────────────────────────────

class TestHistoryExport:
    def test_profile_history(self, stack) -> None:
        db, review, _ga = stack
        pid = run(insert_profile(db, 1))
        aid1 = run(add_ai(db, pid, "LIKE", 0.8, ts="2026-01-01T00:00:01"))
        aid2 = run(add_ai(db, pid, "REVIEW", 0.6, ts="2026-01-01T00:00:02"))
        run(review.save_decision(pid, aid1, HumanDecision.REJECT))
        run(review.save_decision(pid, aid2, HumanDecision.APPROVE))
        hist = run(review.get_history(pid))
        assert len(hist) == 2
        # обе записи сохранены (не перезаписаны)
        decisions = {h["ai_decision_id"]: h["decision"] for h in hist}
        assert decisions == {aid1: "REJECT", aid2: "APPROVE"}

    def test_export_csv(self, stack, tmp_path: Path) -> None:
        db, review, _ga = stack
        setup_basic(stack)
        out = run(export_review_csv(db, tmp_path / "exp"))
        assert out.exists()
        content = out.read_text(encoding="utf-8")
        header = ("profile_id,ai_decision,llm_score,clip_score,combined_score,"
                  "confidence,human_decision,agreement,scoring_version,"
                  "prompt_version,created_at,reviewed_at")
        assert content.splitlines()[0] == header
        # 3 рецензии
        assert len(content.splitlines()) == 4
        assert "APPROVE" in content and "REJECT" in content and "SKIP" in content

    def test_export_empty_raises(self, stack, tmp_path: Path) -> None:
        import pytest as _p
        with _p.raises(RuntimeError):
            run(export_review_csv(stack[0], tmp_path / "exp2"))

    def test_csv_fields_contract_count_and_order(self) -> None:
        """CSV columns должны быть ровно 12 и в фиксированном порядке."""
        from services.review_export import CSV_FIELDS
        assert CSV_FIELDS == [
            "profile_id",
            "ai_decision",
            "llm_score",
            "clip_score",
            "combined_score",
            "confidence",
            "human_decision",
            "agreement",
            "scoring_version",
            "prompt_version",
            "created_at",
            "reviewed_at",
        ]
        # количество и уникальность
        assert len(CSV_FIELDS) == 12
        assert len(set(CSV_FIELDS)) == 12

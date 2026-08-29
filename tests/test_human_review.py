# Unit/Integration тесты Stage 6: Human Review (ReviewService).
# Полностью offline — используется реальный tmp SQLite, без Telegram.

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.config import AppConfig
from database.database import Database
from models.human_decision import (
    AgreementStatus,
    HumanDecision,
    HumanDecisionResult,
)
from services.profile_service import ProfileService
from services.review_service import ReviewService


# ── Fixtures ──────────────────────────────────────────────────────────

def make_config() -> AppConfig:
    return AppConfig(**{
        "telegram": {"api_id": 123, "api_hash": "abc"},
        "filters": {
            "age": {"min": 18, "max": 19},
            "city": {"allowed": ["Санкт-Петербург"]},
        },
    })


@pytest.fixture
def tmp_db(tmp_path: Path) -> Database:
    db = Database(path=tmp_path / "test_hreview.db")
    loop = asyncio.get_event_loop()
    loop.run_until_complete(db.connect())
    yield db  # type: ignore[misc]
    loop.run_until_complete(db.close())


@pytest.fixture
def review(tmp_db: Database) -> ReviewService:
    ps = ProfileService(tmp_db)
    return ReviewService(tmp_db, ps)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


async def insert_profile(
    db: Database, profile_id: int, city: str = "Санкт-Петербург",
) -> int:
    return await db.insert_profile(
        name="Anna", age=19, raw_city=city, normalized_city=city,
        description="Люблю природу", fingerprint=f"fp_h_{profile_id}",
        source_chat_id=1234060895, source_message_id=profile_id,
        first_seen_at="now", last_seen_at="now", status="NEW",
    )


async def add_ai_decision(
    db: Database,
    profile_id: int,
    decision: str,
    combined: float = 0.8,
    confidence: float = 0.8,
    ts: str = "",
) -> int:
    return await db.save_ai_decision(
        profile_id=profile_id,
        decision=decision,
        combined_score=combined,
        llm_score=combined,
        clip_score=None,
        confidence=confidence,
        reasons="[]",
        scoring_version="v1",
        evaluated_at=ts or f"2026-01-01T00:00:0{profile_id}",
        prompt_version="llm-v1",
    )


# ── Модель ────────────────────────────────────────────────────────────

class TestHumanDecisionModel:
    def test_enum_values(self) -> None:
        assert HumanDecision.APPROVE == "APPROVE"
        assert HumanDecision.REJECT == "REJECT"
        assert HumanDecision.SKIP == "SKIP"

    def test_approve_agreement(self) -> None:
        assert AgreementStatus.from_human(HumanDecision.APPROVE) == AgreementStatus.AGREEMENT

    def test_reject_disagreement(self) -> None:
        assert AgreementStatus.from_human(HumanDecision.REJECT) == AgreementStatus.DISAGREEMENT

    def test_skip_unresolved(self) -> None:
        assert AgreementStatus.from_human(HumanDecision.SKIP) == AgreementStatus.UNRESOLVED

    def test_agreement_independent_of_ai(self) -> None:
        # APPROVE → AGREEMENT для любого AI-решения (LIKE/REVIEW/DISLIKE)
        for _ai in ("LIKE", "REVIEW", "DISLIKE"):
            assert AgreementStatus.from_human(HumanDecision.APPROVE) == AgreementStatus.AGREEMENT

    def test_result_model(self) -> None:
        r = HumanDecisionResult(
            profile_id=1, ai_decision_id=2, decision="APPROVE",
            agreement="AGREEMENT", created_at="now",
        )
        assert r.decision == HumanDecision.APPROVE
        assert r.agreement == AgreementStatus.AGREEMENT


# ── Сохранение решений ────────────────────────────────────────────────

class TestSaveDecision:
    def test_approve_save(self, tmp_db: Database, review: ReviewService) -> None:
        pid = run(insert_profile(tmp_db, 1))
        aid = run(add_ai_decision(tmp_db, pid, "LIKE", 0.8, 0.8, ts="2026-01-01T00:00:01"))
        res = run(review.save_decision(pid, aid, HumanDecision.APPROVE))
        assert res.decision == HumanDecision.APPROVE
        assert res.agreement == AgreementStatus.AGREEMENT
        assert res.id > 0

    def test_reject_save(self, tmp_db: Database, review: ReviewService) -> None:
        pid = run(insert_profile(tmp_db, 2))
        aid = run(add_ai_decision(tmp_db, pid, "LIKE", 0.9, 0.9))
        res = run(review.save_decision(pid, aid, HumanDecision.REJECT))
        assert res.decision == HumanDecision.REJECT
        assert res.agreement == AgreementStatus.DISAGREEMENT

    def test_skip_save(self, tmp_db: Database, review: ReviewService) -> None:
        pid = run(insert_profile(tmp_db, 3))
        aid = run(add_ai_decision(tmp_db, pid, "REVIEW", 0.6))
        res = run(review.save_decision(pid, aid, HumanDecision.SKIP))
        assert res.decision == HumanDecision.SKIP
        assert res.agreement == AgreementStatus.UNRESOLVED

    def test_save_missing_ai_raises(self, tmp_db: Database, review: ReviewService) -> None:
        pid = run(insert_profile(tmp_db, 4))
        with pytest.raises(ValueError):
            run(review.save_decision(pid, 999, HumanDecision.APPROVE))

    def test_double_review_raises(self, tmp_db: Database, review: ReviewService) -> None:
        pid = run(insert_profile(tmp_db, 5))
        aid = run(add_ai_decision(tmp_db, pid, "LIKE", 0.8))
        run(review.save_decision(pid, aid, HumanDecision.APPROVE))
        with pytest.raises(ValueError):
            run(review.save_decision(pid, aid, HumanDecision.REJECT))

    def test_multiple_review_history(self, tmp_db: Database, review: ReviewService) -> None:
        """Одна анкета с несколькими AI-оценками → несколько рецензий."""
        pid = run(insert_profile(tmp_db, 6))
        aid1 = run(add_ai_decision(tmp_db, pid, "LIKE", 0.8, ts="2026-01-01T00:00:01"))
        aid2 = run(add_ai_decision(tmp_db, pid, "REVIEW", 0.6, ts="2026-01-01T00:00:02"))
        run(review.save_decision(pid, aid1, HumanDecision.REJECT))
        run(review.save_decision(pid, aid2, HumanDecision.APPROVE))
        history = run(review.get_history(pid))
        assert len(history) == 2

    def test_is_reviewed(self, tmp_db: Database, review: ReviewService) -> None:
        pid = run(insert_profile(tmp_db, 7))
        aid = run(add_ai_decision(tmp_db, pid, "LIKE", 0.8))
        assert not run(review.is_reviewed(pid, aid))
        run(review.save_decision(pid, aid, HumanDecision.APPROVE))
        assert run(review.is_reviewed(pid, aid))

    def test_privacy_no_pii_in_history(self, tmp_db: Database, review: ReviewService) -> None:
        pid = run(insert_profile(tmp_db, 8))
        aid = run(add_ai_decision(tmp_db, pid, "LIKE", 0.8))
        run(review.save_decision(pid, aid, HumanDecision.REJECT))
        history = run(review.get_history(pid))
        row = history[0]
        assert "name" not in row and "description" not in row


# ── Очередь рецензий ──────────────────────────────────────────────────

class TestReviewQueue:
    def test_no_pending_empty_db(self, review: ReviewService) -> None:
        assert run(review.get_next()) is None
        assert run(review.get_pending_count()) == 0

    def test_only_ai_decision_enters_queue(self, tmp_db: Database, review: ReviewService) -> None:
        """Профиль без AI-решения НЕ попадает в очередь."""
        run(insert_profile(tmp_db, 10))
        assert run(review.get_next()) is None
        assert run(review.get_pending_count()) == 0

    def test_oldest_first(self, tmp_db: Database, review: ReviewService) -> None:
        for i in range(1, 4):
            pid = run(insert_profile(tmp_db, i))
            run(add_ai_decision(tmp_db, pid, "LIKE",
                ts=f"2026-01-01T00:00:0{i}"))
        first = run(review.get_next())
        # самый старый decision (evaluated_at 00:00:01) принадлежит профилю 1
        assert first is not None
        assert first.profile.id == 1

    def test_already_reviewed_excluded(self, tmp_db: Database, review: ReviewService) -> None:
        pid1 = run(insert_profile(tmp_db, 1))
        aid1 = run(add_ai_decision(tmp_db, pid1, "LIKE", ts="2026-01-01T00:00:01"))
        pid2 = run(insert_profile(tmp_db, 2))
        run(add_ai_decision(tmp_db, pid2, "LIKE", ts="2026-01-01T00:00:02"))
        run(review.save_decision(pid1, aid1, HumanDecision.APPROVE))
        # рассмотренное не должно вернуться; следующее — профиль 2
        nxt = run(review.get_next())
        assert nxt is not None and nxt.profile.id == 2

    def test_new_ai_decision_can_reenter(self, tmp_db: Database, review: ReviewService) -> None:
        """Новый ai_decision_id той же анкеты снова попадает в очередь."""
        pid = run(insert_profile(tmp_db, 1))
        aid1 = run(add_ai_decision(tmp_db, pid, "LIKE", ts="2026-01-01T00:00:01"))
        run(review.save_decision(pid, aid1, HumanDecision.REJECT))
        nxt = run(review.get_next())
        assert nxt is None  # остальных нет
        aid2 = run(add_ai_decision(tmp_db, pid, "REVIEW", ts="2026-01-01T00:00:02"))
        nxt = run(review.get_next())
        assert nxt is not None
        assert nxt.ai_decision_id == aid2
        assert not run(review.is_reviewed(pid, aid2))

    def test_get_pending_lists_all(self, tmp_db: Database, review: ReviewService) -> None:
        for i in range(1, 4):
            pid = run(insert_profile(tmp_db, i))
            run(add_ai_decision(tmp_db, pid, "LIKE"))
        pending = run(review.get_pending())
        assert len(pending) == 3

    def test_review_item_fields(self, tmp_db: Database, review: ReviewService) -> None:
        pid = run(insert_profile(tmp_db, 1))
        aid = run(add_ai_decision(tmp_db, pid, "LIKE", combined=0.8, confidence=0.85))
        run(tmp_db.save_filter_result(pid, "PASS", "[]", 2, "2026-01-01T00:00:00"))
        item = run(review.get_next())
        assert item is not None
        assert item.ai_decision == "LIKE"
        assert item.combined_score == 0.8
        assert item.confidence == 0.85
        assert item.filter_decision == "PASS"
        assert item.profile.id == pid


# ── Foreign keys / BД ─────────────────────────────────────────────────

class TestForeignKeys:
    def test_ai_decision_cascade(self, tmp_db: Database, review: ReviewService) -> None:
        pid = run(insert_profile(tmp_db, 1))
        aid = run(add_ai_decision(tmp_db, pid, "LIKE", 0.8))
        run(review.save_decision(pid, aid, HumanDecision.APPROVE))
        run(tmp_db.connection.execute("DELETE FROM ai_decisions WHERE id=?", (aid,)))
        run(tmp_db.connection.commit())
        history = run(review.get_history(pid))
        assert history == []

    def test_profile_cascade(self, tmp_db: Database, review: ReviewService) -> None:
        pid = run(insert_profile(tmp_db, 2))
        aid = run(add_ai_decision(tmp_db, pid, "LIKE", 0.8))
        run(review.save_decision(pid, aid, HumanDecision.APPROVE))
        run(tmp_db.connection.execute("DELETE FROM profiles WHERE id=?", (pid,)))
        run(tmp_db.connection.commit())
        cursor = run(tmp_db.connection.execute(
            "SELECT COUNT(*) FROM human_decisions WHERE profile_id=?", (pid,)))
        count = run(cursor.fetchone())[0]
        assert count == 0

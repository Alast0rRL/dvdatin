# Unit/Integration тесты Stage 8: Manual Review (ручные решения владельца).
# Полностью offline — реальный tmp SQLite + файлы журнала, без Telegram.

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from database.database import Database
from services.manual_review import (
    DISLIKE_TEXT,
    LIKE_TEXT,
    ManualAction,
    ManualReviewRecorder,
    classify_outgoing,
)

CHAT_ID = 1234060895


# ── Fixtures ──────────────────────────────────────────────────────────

def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def tmp_db(tmp_path: Path) -> Database:
    db = Database(path=tmp_path / "test_manual_review.db")
    loop = asyncio.get_event_loop()
    loop.run_until_complete(db.connect())
    yield db  # type: ignore[misc]
    loop.run_until_complete(db.close())


async def insert_review_profile(db: Database, pid: int = 1) -> int:
    """Вставляет профиль с REVIEW-решением и связывает с PROFILE-сообщением."""
    profile_id = await db.insert_profile(
        name="Дарья", age=18, raw_city="Санкт-Петербург",
        normalized_city="Санкт-Петербург",
        description="Немного информации", fingerprint=f"fp_review_{pid}",
        source_chat_id=CHAT_ID, source_message_id=pid * 100,
        first_seen_at="now", last_seen_at="now", status="NEW",
    )
    # PROFILE-сообщение, по которому ищется «текущая» анкета (chat_context).
    await db.link_profile_message(
        profile_id, pid * 100, CHAT_ID, "2026-01-01T00:00:00",
    )
    await db.save_ai_decision(
        profile_id=profile_id,
        decision="REVIEW",
        combined_score=0.55,
        llm_score=0.55,
        clip_score=None,
        confidence=0.5,
        reasons='["NO_FEATURES_FOUND"]',
        scoring_version="deterministic-v2",
        evaluated_at="2026-01-01T00:01:00",
        prompt_version="det-v2",
    )
    return profile_id


# ── Классификация исходящего ─────────────────────────────────────────

class TestClassifyOutgoing:
    def test_heart_is_like(self) -> None:
        assert classify_outgoing(LIKE_TEXT) == ManualAction.LIKE

    def test_thumbs_is_dislike(self) -> None:
        assert classify_outgoing(DISLIKE_TEXT) == ManualAction.DISLIKE

    def test_text_is_message(self) -> None:
        assert classify_outgoing("Привет, как дела?") == ManualAction.MESSAGE

    def test_empty_is_message_not_crash(self) -> None:
        assert classify_outgoing("") == ManualAction.MESSAGE


# ── Файл журнала: JSON ────────────────────────────────────────────────

class TestManualReviewJSON:
    def test_writes_single_record(self, tmp_db: Database, tmp_path: Path) -> None:
        pid = run(insert_review_profile(tmp_db))
        path = tmp_path / "review_log.json"
        rec = ManualReviewRecorder(tmp_db, path=path, enabled=True, file_format="json")
        ok = run(rec.handle_outgoing(CHAT_ID, 100, 901, LIKE_TEXT))
        assert ok is True
        data = json.loads(path.read_text(encoding="utf-8"))
        assert len(data) == 1
        assert data[0]["profile_id"] == pid
        assert data[0]["ai_decision"] == "REVIEW"
        assert data[0]["manual_action"] == "LIKE"
        assert data[0]["telegram_message_id"] == 901

    def test_appends_preserving_history(
        self, tmp_db: Database, tmp_path: Path,
    ) -> None:
        run(insert_review_profile(tmp_db))
        path = tmp_path / "review_log.json"
        rec = ManualReviewRecorder(tmp_db, path=path, enabled=True, file_format="json")
        run(rec.handle_outgoing(CHAT_ID, 100, 901, LIKE_TEXT))
        run(rec.handle_outgoing(CHAT_ID, 100, 902, DISLIKE_TEXT))
        data = json.loads(path.read_text(encoding="utf-8"))
        assert len(data) == 2
        assert [d["manual_action"] for d in data] == ["LIKE", "DISLIKE"]

    def test_message_records_text(self, tmp_db: Database, tmp_path: Path) -> None:
        run(insert_review_profile(tmp_db))
        path = tmp_path / "review_log.json"
        rec = ManualReviewRecorder(tmp_db, path=path, enabled=True, file_format="json")
        run(rec.handle_outgoing(CHAT_ID, 100, 903, "Приветик!"))
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data[0]["manual_action"] == "MESSAGE"
        assert data[0]["manual_text"] == "Приветик!"


# ── Файл журнала: Markdown ────────────────────────────────────────────

class TestManualReviewMD:
    def test_creates_header_and_row(self, tmp_db: Database, tmp_path: Path) -> None:
        run(insert_review_profile(tmp_db))
        path = tmp_path / "review_log.md"
        rec = ManualReviewRecorder(tmp_db, path=path, enabled=True, file_format="md")
        run(rec.handle_outgoing(CHAT_ID, 100, 901, LIKE_TEXT))
        content = path.read_text(encoding="utf-8")
        assert content.lstrip().startswith("| recorded_at |")
        assert "LIKE" in content

    def test_header_not_repeated_on_second_row(
        self, tmp_db: Database, tmp_path: Path,
    ) -> None:
        run(insert_review_profile(tmp_db))
        path = tmp_path / "review_log.md"
        rec = ManualReviewRecorder(tmp_db, path=path, enabled=True, file_format="md")
        run(rec.handle_outgoing(CHAT_ID, 100, 901, LIKE_TEXT))
        run(rec.handle_outgoing(CHAT_ID, 100, 902, DISLIKE_TEXT))
        content = path.read_text(encoding="utf-8")
        assert content.count("| recorded_at |") == 1
        assert content.count("|---|---|---|---|---|---|---|---|") == 1
        assert content.count("| #1 |") == 2


# ── Логика handle_outgoing ────────────────────────────────────────────

class TestManualReviewLogic:
    def test_disabled_does_nothing(self, tmp_db: Database, tmp_path: Path) -> None:
        run(insert_review_profile(tmp_db))
        path = tmp_path / "review_log.json"
        rec = ManualReviewRecorder(tmp_db, path=path, enabled=False, file_format="json")
        ok = run(rec.handle_outgoing(CHAT_ID, 100, 901, LIKE_TEXT))
        assert ok is False
        assert not path.exists()

    def test_no_context_returns_false(self, tmp_db: Database, tmp_path: Path) -> None:
        run(insert_review_profile(tmp_db))
        path = tmp_path / "review_log.json"
        rec = ManualReviewRecorder(tmp_db, path=path, enabled=True, file_format="json")
        ok = run(rec.handle_outgoing(CHAT_ID, None, 901, LIKE_TEXT))
        assert ok is False
        assert not path.exists()

    def test_profile_not_review_not_recorded(
        self, tmp_db: Database, tmp_path: Path,
    ) -> None:
        pid = run(insert_review_profile(tmp_db))
        # Добавляем НЕ-REVIEW решение последним по времени.
        run(
            tmp_db.save_ai_decision(
                profile_id=pid, decision="LIKE", combined_score=0.9,
                llm_score=0.9, clip_score=None, confidence=0.9,
                reasons="[]", scoring_version="deterministic-v2",
                evaluated_at="2026-01-01T00:02:00", prompt_version="det-v2",
            )
        )
        path = tmp_path / "review_log.json"
        rec = ManualReviewRecorder(tmp_db, path=path, enabled=True, file_format="json")
        ok = run(rec.handle_outgoing(CHAT_ID, 100, 901, LIKE_TEXT))
        assert ok is False
        assert not path.exists()

    def test_unknown_context_profile_not_recorded(
        self, tmp_db: Database, tmp_path: Path,
    ) -> None:
        run(insert_review_profile(tmp_db))
        path = tmp_path / "review_log.json"
        rec = ManualReviewRecorder(tmp_db, path=path, enabled=True, file_format="json")
        # context_message_id не связан ни с одним профилем.
        ok = run(rec.handle_outgoing(CHAT_ID, 999999, 901, LIKE_TEXT))
        assert ok is False
        assert not path.exists()

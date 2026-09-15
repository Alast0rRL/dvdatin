# Тесты экспорта анализа всех анкет (Stage 11) + очистки БД.
# Офлайн: реальный tmp SQLite, без Telegram.

from __future__ import annotations

import asyncio
import csv
from pathlib import Path

import pytest

from database.database import Database
from services.analysis_export import clear_database, export_analysis_csv


@pytest.fixture
def tmp_db(tmp_path: Path) -> Database:
    db = Database(path=tmp_path / "test_export.db")
    loop = asyncio.get_event_loop()
    loop.run_until_complete(db.connect())
    yield db
    loop.run_until_complete(db.close())


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


async def insert_profile(db: Database, profile_id: int, name: str = "Anna") -> int:
    return await db.insert_profile(
        name=name, age=19, raw_city="Санкт-Петербург",
        normalized_city="Санкт-Петербург", description="Люблю природу",
        fingerprint=f"fp_e_{profile_id}", source_chat_id=1234060895,
        source_message_id=profile_id, first_seen_at="now", last_seen_at="now",
        status="NEW",
    )


async def add_ai(
    db: Database, profile_id: int, decision: str, combined: float = 0.8,
    reasons: str = '["признак Х"]',
) -> int:
    return await db.save_ai_decision(
        profile_id=profile_id, decision=decision, combined_score=combined,
        confidence=0.8, reasons=reasons, scoring_version="deterministic-v2",
        evaluated_at=f"2026-01-01T00:00:0{profile_id}",
    )


def load_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ── Экспорт анализа ───────────────────────────────────────────────────

class TestExportAnalysis:
    def test_export_creates_csv_with_header(self, tmp_db, tmp_path: Path) -> None:
        run(insert_profile(tmp_db, 1))
        out_path, summary = run(export_analysis_csv(tmp_db, tmp_path))
        assert out_path.name.startswith("analysis_")
        assert out_path.suffix == ".csv"
        assert out_path.exists()
        rows = load_csv(out_path)
        assert len(rows) == 1
        assert set(rows[0].keys()) >= {
            "profile_id", "name", "age", "city", "action",
            "ai_decision", "ai_score", "ai_confidence", "filter_decision",
            "human_decision", "human_agreement",
            "auto_liked", "auto_disliked", "auto_message_text",
            "auto_first_action", "auto_first_action_at",
            "manual_action", "manual_text", "manual_at",
            "matched", "responded", "status",
            "first_seen_at", "last_seen_at", "action_reasons",
        }
        assert summary["total"] == 1

    def test_auto_like_classified_liked(self, tmp_db, tmp_path) -> None:
        pid = run(insert_profile(tmp_db, 1))
        run(add_ai(tmp_db, pid, "LIKE"))
        run(tmp_db.record_auto_action(
            pid, "LIKE", "LIKE", 1234060895, telegram_message_id=111,
        ))
        path, _s = run(export_analysis_csv(tmp_db, tmp_path))
        rows = load_csv(path)
        assert rows[0]["action"] == "liked"
        assert rows[0]["auto_liked"] == "1"
        assert rows[0]["ai_decision"] == "LIKE"

    def test_auto_dislike_classified_disliked(self, tmp_db, tmp_path) -> None:
        pid = run(insert_profile(tmp_db, 1))
        run(add_ai(tmp_db, pid, "DISLIKE"))
        run(tmp_db.record_auto_action(
            pid, "DISLIKE", "DISLIKE", 1234060895, telegram_message_id=112,
        ))
        path, _s = run(export_analysis_csv(tmp_db, tmp_path))
        rows = load_csv(path)
        assert rows[0]["action"] == "disliked"
        assert rows[0]["auto_disliked"] == "1"

    def test_manual_like_and_match_sets_responded(self, tmp_db, tmp_path) -> None:
        pid = run(insert_profile(tmp_db, 3, name="Katya"))
        run(add_ai(tmp_db, pid, "REVIEW"))
        run(tmp_db.record_sent_message(
            "❤️", 1234060895, 910, profile_id=pid, action="LIKE",
        ))
        run(tmp_db.record_sent_message(
            "Беру)", 1234060895, 911, profile_id=pid, action="MESSAGE",
        ))
        path, _s = run(export_analysis_csv(tmp_db, tmp_path))
        rows = load_csv(path)
        assert rows[0]["action"] == "liked"
        assert rows[0]["manual_action"] == "MESSAGE"
        assert rows[0]["manual_text"] == "Беру)"

    def test_review_is_pending(self, tmp_db, tmp_path) -> None:
        pid = run(insert_profile(tmp_db, 4))
        run(add_ai(tmp_db, pid, "REVIEW"))
        path, _s = run(export_analysis_csv(tmp_db, tmp_path))
        rows = load_csv(path)
        assert rows[0]["action"] == "pending"
        assert rows[0]["ai_decision"] == "REVIEW"

    def test_no_ai_decision_is_seen(self, tmp_db, tmp_path) -> None:
        run(insert_profile(tmp_db, 5))
        path, _s = run(export_analysis_csv(tmp_db, tmp_path))
        rows = load_csv(path)
        assert rows[0]["action"] == "seen"
        assert rows[0]["ai_decision"] == ""

    def test_fix_ai_like_no_action_is_reviewed(self, tmp_db, tmp_path) -> None:
        # AI вынес LIKE, но реакция (❤️) не отправлена — категория reviewed.
        pid = run(insert_profile(tmp_db, 6))
        run(add_ai(tmp_db, pid, "LIKE"))
        path, _s = run(export_analysis_csv(tmp_db, tmp_path))
        rows = load_csv(path)
        assert rows[0]["action"] == "reviewed"
        assert rows[0]["auto_liked"] == "0"
        assert rows[0]["ai_decision"] == "LIKE"

    def test_action_reasons_joined(self, tmp_db, tmp_path) -> None:
        pid = run(insert_profile(tmp_db, 7))
        run(add_ai(tmp_db, pid, "LIKE", reasons='["смелая", "длинный текст"]'))
        path, _s = run(export_analysis_csv(tmp_db, tmp_path))
        rows = load_csv(path)
        assert rows[0]["action_reasons"] == "смелая; длинный текст"

    def test_matched_flag_from_match_response(self, tmp_db, tmp_path) -> None:
        pid = run(insert_profile(tmp_db, 8, name="Nina"))
        run(add_ai(tmp_db, pid, "LIKE"))
        run(tmp_db.record_auto_action(
            pid, "LIKE", "LIKE", 1234060895, telegram_message_id=113,
        ))
        run(tmp_db.record_match_response("Nina", 1234060895, 700))
        path, _s = run(export_analysis_csv(tmp_db, tmp_path))
        rows = load_csv(path)
        assert rows[0]["matched"] == "1"
        assert rows[0]["responded"] == "1"


# ── Очистка БД ────────────────────────────────────────────────────────

class TestClearDatabase:
    def test_clear_removes_all_rows(self, tmp_db, tmp_path: Path) -> None:
        pid = run(insert_profile(tmp_db, 1))
        run(add_ai(tmp_db, pid, "LIKE"))
        run(tmp_db.record_auto_action(
            pid, "LIKE", "LIKE", 1234060895, telegram_message_id=114,
        ))
        assert run(tmp_db.count_profiles()) == 1

        backup_dir = tmp_path / "backups"
        backup = run(clear_database(tmp_db, backup_dir))

        assert backup.exists()
        assert backup.name.startswith("database_")
        assert run(tmp_db.count_profiles()) == 0
        # Все таблицы пусты.
        for table in [
            "profiles", "ai_decisions", "filter_results",
            "auto_actions_log", "human_decisions", "profile_messages",
            "sent_messages", "match_responses", "like_outcomes",
        ]:
            cursor = run(tmp_db._connection.execute(f"SELECT COUNT(*) FROM {table}"))
            count = run(cursor.fetchone())
            assert count[0] == 0, f"Таблица не очищена: {table}"
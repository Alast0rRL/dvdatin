# Unit-тесты Stage 6: Telegram Review UI (ReviewBot) — с mocks Telethon.
# Полностью offline: client, события и кнопки замоканы.
# DB открывается через fixture (teardown гарантированно вызывает close —
# иначе aiosqlite thread не даёт процессу pytest завершиться).

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import AppConfig
from database.database import Database
from services.analytics_service import AnalyticsService
from services.profile_service import ProfileService
from services.review_service import ReviewService
from telegram.review_bot import ReviewBot


def make_config() -> AppConfig:
    return AppConfig(**{
        "telegram": {"api_id": 123, "api_hash": "abc"},
        "filters": {
            "age": {"min": 18, "max": 19},
            "city": {"allowed": ["Санкт-Петербург"]},
        },
    })


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def tmp_db(tmp_path: Path) -> Database:
    db = Database(path=tmp_path / "ui.db")
    run(db.connect())
    yield db  # type: ignore[misc]
    run(db.close())


def populate(db: Database, review: ReviewService):
    """Наполняет БД: профиль + фильтр + 2 AI-решения (одно рассмотрено)."""
    pid = run(db.insert_profile(
        name="Anna", age=19, raw_city="Санкт-Петербург",
        normalized_city="Санкт-Петербург", description="Люблю гулять и музыку",
        fingerprint="fp_ui_1", source_chat_id=1, source_message_id=1,
        first_seen_at="now", last_seen_at="now", status="NEW"))
    run(db.save_filter_result(pid, "PASS", "[]", 2, "now"))
    aid_reviewed = run(db.save_ai_decision(
        profile_id=pid, decision="REVIEW", combined_score=0.55,
        confidence=0.5, reasons="[]",
        scoring_version="v1",
        evaluated_at="2026-01-01T00:00:01"))
    run(db.save_human_decision(pid, aid_reviewed, "REJECT", "DISAGREEMENT", "now"))
    run(db.save_ai_decision(
        profile_id=pid, decision="LIKE", combined_score=0.78,
        confidence=0.88, reasons="[]",
        scoring_version="v1",
        evaluated_at="2026-01-01T00:00:02"))
    return pid


class TestReviewBot:
    def test_register_adds_handlers(self, tmp_db: Database) -> None:
        client = MagicMock()
        bot = ReviewBot(
            client, make_config(),
            review_service=MagicMock(), analytics_service=MagicMock(),
        )
        bot.register()
        assert client.add_event_handler.call_count == 6
        buttons = bot._review_buttons(42)
        assert len(buttons) == 1 and len(buttons[0]) == 3
        datas = [b.data for b in buttons[0]]
        assert datas == [
            b"review:approve:42", b"review:reject:42", b"review:skip:42",
        ]

    def test_parse_review_callback(self) -> None:
        assert ReviewBot._parse_review_callback("review:approve:12") == ("approve", "12")
        assert ReviewBot._parse_review_callback("review:next") == ("next", "")

    def test_render_review_panel(self, tmp_db: Database) -> None:
        review = ReviewService(tmp_db, ProfileService(tmp_db))
        populate(tmp_db, review)
        item = run(review.get_next())
        assert item is not None
        text = ReviewBot._render_review(item)
        assert "AI REVIEW" in text
        assert "Profile #" in text
        assert "Anna, 19" in text
        assert "PASS" in text
        assert "AI SCORING" in text
        assert "AI DECISION:" in text
        bot = ReviewBot(
            MagicMock(), make_config(),
            review_service=review, analytics_service=MagicMock(),
        )
        buttons = bot._review_buttons(item.ai_decision_id)
        for b in buttons[0]:
            assert b.data.decode().rstrip().split(":")[2] == str(item.ai_decision_id)

    def test_full_review_flow_via_mocked_ui(self, tmp_db: Database) -> None:
        review = ReviewService(tmp_db, ProfileService(tmp_db))
        pid = populate(tmp_db, review)
        # последняя (неописанная) AI-оценка — pending
        nxt = run(review.get_next())
        assert nxt is not None
        aid = nxt.ai_decision_id

        client = MagicMock()
        config = make_config()
        analytics = AnalyticsService(tmp_db)
        bot = ReviewBot(client, config, review_service=review, analytics_service=analytics)

        event = MagicMock()
        event.data = f"review:reject:{aid}".encode()
        event.answer = AsyncMock()
        event.edit = AsyncMock()
        run(bot._on_callback(event))

        event.edit.assert_awaited_once()
        text = event.edit.call_args[0][0]
        assert "Human decision saved." in text
        assert "DISAGREEMENT" in text

        # рецензия сохранена; снова та же кнопка → alert (уже рассмотрено)
        run(bot._on_callback(event))
        event.answer.assert_awaited_once()

        # очередь пуста → next нет
        ctx = MagicMock()
        ctx.data = b"review:next"
        ctx.edit = AsyncMock()
        run(bot._on_callback(ctx))
        ctx.edit.assert_awaited_once()
        assert "Нет pending" in ctx.edit.call_args[0][0]

    def test_all_renderers(self, tmp_db: Database) -> None:
        review = ReviewService(tmp_db, ProfileService(tmp_db))
        pid = populate(tmp_db, review)
        cfg = make_config()
        ga = AnalyticsService(tmp_db)
        bot = ReviewBot(MagicMock(), cfg, review_service=review, analytics_service=ga)

        item = run(review.get_next())
        assert item is not None
        assert item.ai_decision == "LIKE"  # REVIEW уже рассмотрен, остался LIKE
        t = bot._render_review(item)
        assert "AI REVIEW" in t and "Anna, 19" in t and "0.780" in t

        p = run(bot._render_profile(pid))
        assert "LATEST AI DECISION" in p and "HISTORY" in p and "REJECT" in p

        s = run(bot._render_stats())
        assert "Profiles:" in s and "AI/Human:" in s

        a = run(bot._render_ai_stats())
        assert "Total AI decisions:" in a

        d = run(bot._render_disagreements("newest"))
        assert "REJECT" in d and "#" in d

"""Регрессии журнала и статуса Stage 7 auto-actions."""

from __future__ import annotations

import asyncio
import sqlite3

from database.database import Database
from models.raw import ParsedProfile
from services.profile_service import ProfileService


class TestAutoActionAudit:
    def _profile(self) -> ParsedProfile:
        return ParsedProfile(
            name="Анна", age=19, raw_city="СПб", normalized_city="Санкт-Петербург",
            source_chat_id=1234060895, source_message_id=1,
        )

    def test_successful_action_creates_one_log_and_updates_status(self, tmp_path) -> None:
        async def run() -> None:
            db = Database(tmp_path / "audit.db")
            await db.connect()
            try:
                profile = await ProfileService(db).create_profile(self._profile())
                await db.record_auto_action(profile.id, "DISLIKE", "DISLIKE", 1234060895, 100)
                assert await db.has_auto_action(profile.id) is True
                assert await db.has_auto_action_for_message(1234060895, 100) is True
                assert await db.has_auto_action_for_message(1234060895, 200) is False
                row = await db.get_profile_by_id(profile.id)
                assert row["status"] == "DISLIKED"
                cursor = await db._connection.execute(
                    "SELECT COUNT(*) FROM auto_actions_log WHERE profile_id = ?", (profile.id,)
                )
                assert (await cursor.fetchone())[0] == 1
            finally:
                await db.close()
        asyncio.get_event_loop().run_until_complete(run())

    def test_repeat_show_of_same_profile_gets_another_action(self, tmp_path) -> None:
        """Повторная карточка той же личности (новый telegram_message_id) —
        получает новую реакцию: идемпотентность по карточке, а не по имени."""
        async def run() -> None:
            db = Database(tmp_path / "repeat.db")
            await db.connect()
            try:
                profile = await ProfileService(db).create_profile(self._profile())
                # Первая карточка 100.
                await db.record_auto_action(profile.id, "DISLIKE", "DISLIKE", 1234060895, 100)
                assert await db.has_auto_action_for_message(1234060895, 100) is True
                # Повторная карточка 200 той же личности — НЕ считается обработанной.
                assert await db.has_auto_action_for_message(1234060895, 200) is False
                await db.record_auto_action(profile.id, "DISLIKE", "DISLIKE", 1234060895, 200)
                assert await db.has_auto_action_for_message(1234060895, 200) is True
                cursor = await db._connection.execute(
                    "SELECT COUNT(*) FROM auto_actions_log WHERE profile_id = ?", (profile.id,)
                )
                assert (await cursor.fetchone())[0] == 2
            finally:
                await db.close()
        asyncio.get_event_loop().run_until_complete(run())

    def test_failed_telegram_action_has_no_log(self, tmp_path) -> None:
        from collectors.auto_action import AutoActionEngine, AutoActionError
        from app.config import AutoActionsConfig
        from core.types import Mode
        from models.decision import AIDecision
        from unittest.mock import AsyncMock

        client = AsyncMock()
        client.send_message = AsyncMock(side_effect=RuntimeError("network"))
        engine = AutoActionEngine(client, AutoActionsConfig(enabled=True, interval_sec=0), Mode.SEMI_AUTO, 1)
        try:
            asyncio.get_event_loop().run_until_complete(engine.maybe_act(AIDecision.LIKE, 1))
        except AutoActionError:
            pass
        else:
            raise AssertionError("ожидалась ошибка Telegram")
        assert client.send_message.await_count == 1

    def test_migration_adds_log_to_existing_database(self, tmp_path) -> None:
        path = tmp_path / "database.db"
        connection = sqlite3.connect(path)
        connection.execute("""CREATE TABLE raw_messages (
            id INTEGER PRIMARY KEY, telegram_message_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL, sender_id INTEGER NOT NULL,
            sender_username TEXT DEFAULT '', sender_name TEXT DEFAULT '',
            message_date TEXT NOT NULL, text TEXT DEFAULT '',
            raw_entities TEXT DEFAULT '[]', media_type TEXT DEFAULT '',
            reply_to_message_id INTEGER, received_at TEXT NOT NULL
        )""")
        connection.close()

        async def run() -> None:
            db = Database(path)
            await db.connect()
            try:
                cursor = await db._connection.execute("PRAGMA table_info(auto_actions_log)")
                assert {row[1] for row in await cursor.fetchall()} == {
                    "id", "profile_id", "action", "decision", "chat_id", "sent_at",
                    "telegram_message_id",
                }
            finally:
                await db.close()
        asyncio.get_event_loop().run_until_complete(run())

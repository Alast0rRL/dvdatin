# Тесты аналитики лайков (Stage 8.5) и app_state — на уровне Database.
# Полностью offline — реальный tmp SQLite, без Telegram.

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from database.database import Database


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db(tmp_path: Path) -> Database:
    db = Database(path=tmp_path / "test_analytics.db")
    loop = asyncio.get_event_loop()
    loop.run_until_complete(db.connect())
    yield db  # type: ignore[misc]
    loop.run_until_complete(db.close())


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestLikeMessageAnalytics:
    """Конверсия текстов сообщений при лайке в ответ девушки."""

    async def _liked_profile(self, db: Database, name: str) -> int:
        pid = await db.insert_profile(
            name=name, age=19, raw_city="Москва", normalized_city="Москва",
            description="Описание", fingerprint=f"fp_{name}",
            source_chat_id=1234060895, source_message_id=hash(name) % 100000,
            first_seen_at="now", last_seen_at="now", status="NEW",
        )
        await db.record_auto_action(
            pid, "LIKE", "LIKE", 1234060895, tm := hash(name) % 100000 + 1,
            message_text="",
        )
        return pid

    def test_record_match_links_by_name_case_insensitive(self, tmp_db) -> None:
        """Взаимный лайк привязывается к профилю по имени (кириллица, регистр)."""
        db = tmp_db
        pid = run(self._liked_profile(db, "Кариша"))
        run(db.record_match_response("кариша", 1234060895, 5000))
        cursor = run(db._connection.execute(
            "SELECT profile_id FROM match_responses WHERE telegram_message_id = 5000"
        ))
        row = run(cursor.fetchone())
        assert row[0] == pid

    def test_record_match_unknown_name_no_profile(self, tmp_db) -> None:
        """Неизвестное имя → profile_id NULL (не падает)."""
        db = tmp_db
        run(db.record_match_response("Незнакомка", 1234060895, 7000))
        cursor = run(db._connection.execute(
            "SELECT profile_id FROM match_responses WHERE telegram_message_id = 7000"
        ))
        row = run(cursor.fetchone())
        assert row[0] is None

    def test_record_match_dedupe_by_card(self, tmp_db) -> None:
        """Дубль одной карточки (chat_id+tm_id) не записывается дважды."""
        db = tmp_db
        run(db.record_match_response("Катя", 1234060895, 8000))
        run(db.record_match_response("Катя", 1234060895, 8000))
        cursor = run(db._connection.execute(
            "SELECT COUNT(*) FROM match_responses WHERE telegram_message_id = 8000"
        ))
        assert run(cursor.fetchone())[0] == 1

    def test_message_response_stats_auto_and_manual(self, tmp_db) -> None:
        """«Берём)» (авто, MESSAGE) и ручные тексты в одной конверсии."""
        db = tmp_db
        pid1 = run(self._liked_profile(db, "Аня"))
        pid2 = run(self._liked_profile(db, "Лена"))
        # авто-сообщение «Берём)» для двух профилей; Аня ответила.
        run(db.record_auto_action(
            pid1, "MESSAGE", "LIKE", 1234060895, 10001, message_text="Берём)",
        ))
        run(db.record_auto_action(
            pid2, "MESSAGE", "LIKE", 1234060895, 10002, message_text="Берём)",
        ))
        run(db.record_match_response("Аня", 1234060895, 9000))
        # ручное «Беру)» Лене — она ещё не ответила.
        run(db.record_sent_message("Беру)", 1234060895, 20001, profile_id=pid2))

        rows = run(db.get_message_response_stats())
        by_text = {r["message_text"]: r for r in rows}
        assert by_text["Берём)"]["sent"] == 2
        assert by_text["Берём)"]["responded"] == 1
        # ручной текст тоже попадает в выдачу
        assert by_text["Беру)"]["sent"] == 1
        assert by_text["Беру)"]["responded"] == 0

    def test_top_liked_profiles(self, tmp_db) -> None:
        """Топ по количеству лайков + ответ."""
        db = tmp_db
        pid_a = run(self._liked_profile(db, "Аня"))
        run(db.record_auto_action(
            pid_a, "LIKE", "LIKE", 1234060895, 90011,
        ))
        pid_b = run(self._liked_profile(db, "Лена"))
        run(db.record_match_response("Лена", 1234060895, 9100))
        rows = run(db.get_top_liked_profiles(limit=5))
        by_name = {r["name"]: r for r in rows}
        assert by_name["Аня"]["likes"] == 2
        assert by_name["Аня"]["responded"] == 0
        assert by_name["Лена"]["likes"] == 1
        assert by_name["Лена"]["responded"] == 1

    def test_manual_like_counts_in_top_and_response(self, tmp_db) -> None:
        """Ручной лайк («сам лайкнул» ❤️) считается в топе и в ответе.

        Но НЕ попадает в конверсию текстов (там только MESSAGE).
        """
        db = tmp_db
        pid = run(db.insert_profile(
            name="Даша", age=19, raw_city="Москва", normalized_city="Москва",
            description="Описание", fingerprint="fp_manual", source_chat_id=1234060895,
            source_message_id=555, first_seen_at="now", last_seen_at="now",
            status="NEW",
        ))
        # владелец сам лайкнул (ручной LIKE) и написала «Беру)»
        run(db.record_sent_message("❤️", 1234060895, 6001, action="LIKE", profile_id=pid))
        run(db.record_sent_message("Беру)", 1234060895, 6002, action="MESSAGE", profile_id=pid))
        run(db.record_match_response("Даша", 1234060895, 9101))

        rows = run(db.get_top_liked_profiles(limit=5))
        by_name = {r["name"]: r for r in rows}
        assert by_name["Даша"]["likes"] == 1
        assert by_name["Даша"]["responded"] == 1
        assert by_name["Даша"]["message_text"] == "Беру)"

        # содержание сообщения прикреплено к ручному лайку + результат = 1
        cursor = run(db._connection.execute(
            """SELECT message_text, responded FROM like_outcomes
            WHERE profile_id = ? AND source = 'manual'""", (pid,)
        ))
        text, responded = run(cursor.fetchone())
        assert text == "Беру)"
        assert responded == 1

        stats = run(db.get_message_response_stats())
        by_text = {r["message_text"]: r for r in stats}
        # «❤️» (LIKE) в текстах отсутствует, только «Беру)»
        assert "❤️" not in by_text
        assert by_text["Беру)"]["sent"] == 1
        assert by_text["Беру)"]["responded"] == 1

    def test_fresh_db_has_analytics_tables(self, tmp_db) -> None:
        """Свежая БД сразу содержит match_responses/sent_messages/like_outcomes."""
        db = tmp_db
        cols = run(db._connection.execute("PRAGMA table_info(auto_actions_log)"))
        assert "message_text" in {row[1] for row in run(cols.fetchall())}
        cols = run(db._connection.execute("PRAGMA table_info(sent_messages)"))
        assert "action" in {row[1] for row in run(cols.fetchall())}
        cols = run(db._connection.execute("PRAGMA table_info(like_outcomes)"))
        names = {row[1] for row in run(cols.fetchall())}
        assert {"message_text", "responded", "source"}.issubset(names)
        for table in ("match_responses", "sent_messages", "like_outcomes"):
            cursor = run(db._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ))
            assert run(cursor.fetchone()) is not None

    def test_like_outcome_gets_message_text_and_result(self, tmp_db) -> None:
        """Лайк фиксируется с содержанием сообщения и результатом (0/1).

        LIKE_AND_MESSAGE: лайк + «Берем)» на одну карточку; затем взаимный
        лайк → responded=1 (лайкнула в ответ).
        """
        db = tmp_db
        pid = run(self._liked_profile(db, "Маша"))  # авто LIKE (без текста)
        # на ту же карточку ушло сообщение: tm карточки = tm в _liked_profile
        cursor = run(db._connection.execute(
            "SELECT like_tm_id FROM like_outcomes WHERE profile_id = ?", (pid,)
        ))
        like_tm = run(cursor.fetchone())[0]
        # МЕШАЕТ: у MESSAGE свой tm в _liked_profile нет; просто повторный LIKE
        run(db.record_auto_action(
            pid, "MESSAGE", "LIKE", 1234060895, like_tm, message_text="Берём)",
        ))
        cursor = run(db._connection.execute(
            "SELECT message_text, responded FROM like_outcomes WHERE profile_id = ?",
            (pid,),
        ))
        text, responded = run(cursor.fetchone())
        assert text == "Берём)"
        assert responded == 0
        # девушка лайкнула в ответ → результат по лайку = 1
        run(db.record_match_response("Маша", 1234060895, 9500))
        cursor = run(db._connection.execute(
            "SELECT responded FROM like_outcomes WHERE profile_id = ?", (pid,)
        ))
        assert run(cursor.fetchone())[0] == 1

    def test_like_outcome_idempotent_per_card(self, tmp_db) -> None:
        """Повторный ручной лайк на ту же карточку (тот же tm) не дублируется."""
        db = tmp_db
        pid = run(db.insert_profile(
            name="Оля", age=19, raw_city="Москва", normalized_city="Москва",
            description="Описание", fingerprint="fp_olya", source_chat_id=1234060895,
            source_message_id=7771, first_seen_at="now", last_seen_at="now",
            status="NEW",
        ))
        run(db.record_sent_message("❤️", 1234060895, 6007, action="LIKE", profile_id=pid))
        run(db.record_sent_message("❤️", 1234060895, 6007, action="LIKE", profile_id=pid))
        cursor = run(db._connection.execute(
            "SELECT COUNT(*) FROM like_outcomes WHERE profile_id = ?", (pid,)
        ))
        assert run(cursor.fetchone())[0] == 1

    # ── Stage 8.5.1: Leo отклонил лайк (лимит) ────────────────────────

    def test_fresh_db_has_rejected_column(self, tmp_db) -> None:
        """Свежая БД: like_outcomes содержит колонку rejected (default 0)."""
        db = tmp_db
        cols = run(db._connection.execute("PRAGMA table_info(like_outcomes)"))
        names = {row[1] for row in run(cols.fetchall())}
        assert "rejected" in names

    def test_legacy_db_gets_rejected_column_via_migration(
        self, tmp_path: Path,
    ) -> None:
        """Старая БД без rejected пополняется колонкой при connect (ALTER)."""
        import sqlite3

        path = tmp_path / "legacy.db"
        con = sqlite3.connect(path)
        con.execute(
            """CREATE TABLE like_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER,
                source TEXT NOT NULL DEFAULT 'auto',
                message_text TEXT NOT NULL DEFAULT '',
                chat_id INTEGER NOT NULL,
                like_tm_id INTEGER NOT NULL,
                liked_at TEXT NOT NULL,
                responded INTEGER NOT NULL DEFAULT 0,
                responded_at TEXT
            )"""
        )
        con.commit()
        con.close()

        from database.database import Database

        db = Database(path=path)
        run(db.connect())
        cols = run(db._connection.execute("PRAGMA table_info(like_outcomes)"))
        names = {row[1] for row in run(cols.fetchall())}
        assert "rejected" in names
        run(db.close())

    def test_mark_like_rejected_marks_latest_only(self, tmp_db) -> None:
        """Отказ помечает самый свежий неотклонённый лайк чата."""
        db = tmp_db
        pid = run(self._liked_profile(db, "Аня"))
        run(db.record_auto_action(pid, "LIKE", "LIKE", 1234060895, 90021))
        run(db.mark_like_rejected(1234060895))
        cursor = run(db._connection.execute(
            "SELECT like_tm_id, rejected FROM like_outcomes "
            "WHERE profile_id = ? ORDER BY id", (pid,),
        ))
        rows = run(cursor.fetchall())
        assert rows[-1] == (90021, 1)
        assert all(r[1] == 0 for r in rows[:-1])

    def test_mark_like_rejected_consumes_in_sequence(self, tmp_db) -> None:
        """Два отказа подряд помечают два последних лайка по очереди."""
        db = tmp_db
        pid = run(self._liked_profile(db, "Аня"))
        run(db.record_auto_action(pid, "LIKE", "LIKE", 1234060895, 90031))
        run(db.mark_like_rejected(1234060895))
        run(db.mark_like_rejected(1234060895))
        cursor = run(db._connection.execute(
            "SELECT COUNT(*) FROM like_outcomes "
            "WHERE profile_id = ? AND rejected = 1", (pid,),
        ))
        assert run(cursor.fetchone())[0] == 2

    def test_mark_like_rejected_without_likes_ok(self, tmp_db) -> None:
        """Нет лайков → отказам нечего помечать (не падает)."""
        db = tmp_db
        run(db.mark_like_rejected(1234060895))
        cursor = run(db._connection.execute("SELECT COUNT(*) FROM like_outcomes"))
        assert run(cursor.fetchone())[0] == 0

    def test_top_excludes_rejected_likes(self, tmp_db) -> None:
        """Отклонённые лайки не считаются в топе."""
        db = tmp_db
        pid_a = run(self._liked_profile(db, "Аня"))
        pid_b = run(self._liked_profile(db, "Лена"))
        run(db.record_auto_action(pid_b, "LIKE", "LIKE", 1234060895, 90100))
        # оба лайка Лены отклонены Leo (два отказа подряд)
        run(db.mark_like_rejected(1234060895))
        run(db.mark_like_rejected(1234060895))
        rows = run(db.get_top_liked_profiles(limit=5))
        by_name = {r["name"]: r for r in rows}
        assert "Лена" not in by_name
        assert by_name["Аня"]["likes"] == 1
        assert by_name["Аня"]["responded"] == 0

    def test_match_response_does_not_flip_rejected(self, tmp_db) -> None:
        """Отклонённый лайк не «отвечает»: флип responded их не трогает."""
        db = tmp_db
        pid = run(self._liked_profile(db, "Аня"))
        run(db.mark_like_rejected(1234060895))
        run(db.record_match_response("Аня", 1234060895, 9500))
        cursor = run(db._connection.execute(
            "SELECT responded, rejected FROM like_outcomes "
            "WHERE profile_id = ?", (pid,),
        ))
        responded, rejected = run(cursor.fetchone())
        assert responded == 0
        assert rejected == 1


class TestAppState:
    """Рантайм key/value (app_state): переживает рестарт (лимит ❤️ и пр.)."""

    def test_set_get_delete(self, tmp_db) -> None:
        db = tmp_db
        assert run(db.get_state("likes_exhausted_until")) is None
        run(db.set_state("likes_exhausted_until", "123.5"))
        assert run(db.get_state("likes_exhausted_until")) == "123.5"
        # upsert перезаписывает значение
        run(db.set_state("likes_exhausted_until", "456.0"))
        assert run(db.get_state("likes_exhausted_until")) == "456.0"
        run(db.delete_state("likes_exhausted_until"))
        assert run(db.get_state("likes_exhausted_until")) is None

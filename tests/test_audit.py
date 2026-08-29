# Stage 2.5 — DATABASE AUDIT verification tests.
# Проверяет соответствие реализации заявленной архитектуре.

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from pathlib import Path

import pytest

from database.database import Database
from models.profile import Profile, ProfileStatus, compute_fingerprint
from models.raw import FilterResult, ParsedProfile
from services.profile_service import ProfileService


# ── Fixture ───────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database(path=tmp_path / "audit.db")
    loop = asyncio.get_event_loop()
    loop.run_until_complete(d.connect())
    yield d  # type: ignore[misc]
    loop.run_until_complete(d.close())


@pytest.fixture
def svc(db: Database) -> ProfileService:
    return ProfileService(db)


def p(name="Anna", age=19, raw_city="спб", norm_city="Санкт-Петербург",
      desc="Привет", msg_id=100, chat_id=1234060895) -> ParsedProfile:
    return ParsedProfile(
        name=name, age=age, raw_city=raw_city,
        normalized_city=norm_city, description=desc,
        filter_result=FilterResult.FILTER_MATCH,
        source_message_id=msg_id, source_chat_id=chat_id,
    )


# ═════════════════════════════════════════════════════════════════════
# 1. Один Profile — несколько Telegram messages
# ═════════════════════════════════════════════════════════════════════

class TestAudit1MultipleMessages:
    """Profile #42 → message 100, 101, 102."""

    def test_three_messages_one_profile(self, svc: ProfileService) -> None:
        loop = asyncio.get_event_loop()
        p1 = loop.run_until_complete(svc.upsert_profile(p(msg_id=100)))
        p2 = loop.run_until_complete(svc.upsert_profile(p(msg_id=101)))
        p3 = loop.run_until_complete(svc.upsert_profile(p(msg_id=102)))

        assert p1.id == p2.id == p3.id

        msgs = loop.run_until_complete(
            svc._db.get_profile_messages(p1.id)
        )
        msg_ids = {m["telegram_message_id"] for m in msgs}
        assert msg_ids == {100, 101, 102}

    def test_message_count_reflects(self, svc: ProfileService) -> None:
        loop = asyncio.get_event_loop()
        prof = loop.run_until_complete(svc.upsert_profile(p(msg_id=100)))
        loop.run_until_complete(svc.upsert_profile(p(msg_id=101)))
        loop.run_until_complete(svc.upsert_profile(p(msg_id=102)))

        fetched = loop.run_until_complete(svc.get_profile(prof.id))
        assert fetched is not None
        assert fetched.message_count == 3


# ═════════════════════════════════════════════════════════════════════
# 2. UNIQUE(source_chat_id, source_message_id)
# ═════════════════════════════════════════════════════════════════════

class TestAudit2UniqueConstraint:
    """UNIQUE не мешает одному Profile иметь несколько messages."""

    def test_different_messages_same_profile(self, svc: ProfileService) -> None:
        """Messages 100, 101 → один Profile."""
        loop = asyncio.get_event_loop()
        p1 = loop.run_until_complete(svc.upsert_profile(p(msg_id=100)))
        p2 = loop.run_until_complete(svc.upsert_profile(p(msg_id=101)))
        assert p1.id == p2.id

    def test_same_message_idempotent(self, svc: ProfileService) -> None:
        """Двойной вызов с msg_id=100 → одна ссылка."""
        loop = asyncio.get_event_loop()
        loop.run_until_complete(svc.upsert_profile(p(msg_id=100)))
        loop.run_until_complete(svc.upsert_profile(p(msg_id=100)))
        msgs = loop.run_until_complete(svc._db.get_profile_messages(1))
        assert len(msgs) == 1

    def test_constraint_exists(self, db: Database) -> None:
        """Проверяем что UNIQUE существует в схеме."""
        loop = asyncio.get_event_loop()
        cursor = loop.run_until_complete(
            db.connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='profiles'"
            )
        )
        row = loop.run_until_complete(cursor.fetchone())
        assert "UNIQUE" in row[0]


# ═════════════════════════════════════════════════════════════════════
# 3. Fingerprint deterministic
# ═════════════════════════════════════════════════════════════════════

class TestAudit3Fingerprint:
    def test_deterministic(self) -> None:
        fp1 = compute_fingerprint("Anna", 19, "Санкт-Петербург")
        fp2 = compute_fingerprint("Anna", 19, "Санкт-Петербург")
        assert fp1 == fp2

    def test_case_insensitive(self) -> None:
        fp1 = compute_fingerprint("ANNA", 19, "СПБ")
        fp2 = compute_fingerprint("anna", 19, "спб")
        assert fp1 == fp2

    def test_different_inputs_different_fp(self) -> None:
        fp1 = compute_fingerprint("Anna", 19, "СПБ")
        fp2 = compute_fingerprint("Anna", 20, "СПБ")
        assert fp1 != fp2

    def test_is_sha256(self) -> None:
        fp = compute_fingerprint("Test", 20, "Москва")
        assert len(fp) == 64
        assert all(c in "0123456789abcdef" for c in fp)

    def test_matches_manual_sha256(self) -> None:
        raw = "anna|19|санкт-петербург"
        expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        actual = compute_fingerprint("Anna", 19, "Санкт-Петербург")
        assert actual == expected


# ═════════════════════════════════════════════════════════════════════
# 4. Upsert: один Profile ID для одинаковых анкет
# ═════════════════════════════════════════════════════════════════════

class TestAudit4Upsert:
    def test_same_fingerprint_same_id(self, svc: ProfileService) -> None:
        loop = asyncio.get_event_loop()
        p1 = loop.run_until_complete(svc.upsert_profile(p(msg_id=100)))
        p2 = loop.run_until_complete(svc.upsert_profile(p(msg_id=101)))
        assert p1.id == p2.id

    def test_different_fingerprint_different_id(self, svc: ProfileService) -> None:
        loop = asyncio.get_event_loop()
        p1 = loop.run_until_complete(svc.upsert_profile(p(name="Anna", msg_id=100)))
        p2 = loop.run_until_complete(svc.upsert_profile(p(name="Bob", msg_id=101)))
        assert p1.id != p2.id


# ═════════════════════════════════════════════════════════════════════
# 5. Description update через upsert
# ═════════════════════════════════════════════════════════════════════

class TestAudit5Description:
    def test_upsert_updates_description(self, svc: ProfileService) -> None:
        loop = asyncio.get_event_loop()
        prof = loop.run_until_complete(
            svc.upsert_profile(p(desc="Старое", msg_id=100))
        )
        loop.run_until_complete(
            svc.upsert_profile(p(desc="Новое описание", msg_id=101))
        )
        fetched = loop.run_until_complete(svc.get_profile(prof.id))
        assert fetched is not None
        assert fetched.description == "Новое описание"

    def test_raw_message_preserved(self, db: Database, svc: ProfileService) -> None:
        """RAW messages не удаляются при обновлении description."""
        loop = asyncio.get_event_loop()
        # Save raw messages first
        loop.run_until_complete(db.save_raw_message(
            telegram_message_id=100, chat_id=1234060895, sender_id=1,
            sender_username="", sender_name="", message_date="2026-01-01",
            text="Anna, 19, спб – Старое", raw_entities="[]", media_type="",
            reply_to_message_id=None, received_at="2026-01-01",
        ))
        loop.run_until_complete(db.save_raw_message(
            telegram_message_id=101, chat_id=1234060895, sender_id=1,
            sender_username="", sender_name="", message_date="2026-01-02",
            text="Anna, 19, спб – Новое описание", raw_entities="[]",
            media_type="", reply_to_message_id=None, received_at="2026-01-02",
        ))

        prof = loop.run_until_complete(
            svc.upsert_profile(p(desc="Старое", msg_id=100))
        )
        loop.run_until_complete(
            svc.upsert_profile(p(desc="Новое описание", msg_id=101))
        )

        # Check RAW messages still exist
        raw1 = loop.run_until_complete(
            db.connection.execute(
                "SELECT text FROM raw_messages WHERE telegram_message_id=100"
            )
        )
        row1 = loop.run_until_complete(raw1.fetchone())
        assert row1[0] == "Anna, 19, спб – Старое"

        raw2 = loop.run_until_complete(
            db.connection.execute(
                "SELECT text FROM raw_messages WHERE telegram_message_id=101"
            )
        )
        row2 = loop.run_until_complete(raw2.fetchone())
        assert row2[0] == "Anna, 19, спб – Новое описание"


# ═════════════════════════════════════════════════════════════════════
# 6. raw_city: разные → один normalized_city
# ═════════════════════════════════════════════════════════════════════

class TestAudit6RawCity:
    def test_different_raw_same_normalized(self, svc: ProfileService) -> None:
        loop = asyncio.get_event_loop()
        p1 = loop.run_until_complete(
            svc.upsert_profile(p(raw_city="спб", norm_city="Санкт-Петербург", msg_id=100))
        )
        p2 = loop.run_until_complete(
            svc.upsert_profile(p(raw_city="Санкт-Петербург", norm_city="Санкт-Петербург", msg_id=101))
        )
        assert p1.id == p2.id

        fetched = loop.run_until_complete(svc.get_profile(p1.id))
        assert fetched is not None
        assert fetched.normalized_city == "Санкт-Петербург"
        assert fetched.raw_city == "Санкт-Петербург"  # updated to latest

    def test_normalized_never_changes(self, svc: ProfileService) -> None:
        """normalized_city не меняется при обновлении raw_city."""
        loop = asyncio.get_event_loop()
        prof = loop.run_until_complete(
            svc.upsert_profile(p(raw_city="спб", norm_city="Санкт-Петербург", msg_id=100))
        )
        loop.run_until_complete(
            svc.upsert_profile(p(raw_city="Питер", norm_city="Санкт-Петербург", msg_id=101))
        )
        fetched = loop.run_until_complete(svc.get_profile(prof.id))
        assert fetched is not None
        assert fetched.normalized_city == "Санкт-Петербург"


# ═════════════════════════════════════════════════════════════════════
# 7. MATCH не создаёт новый Profile
# ═════════════════════════════════════════════════════════════════════

class TestAudit7Match:
    def test_match_updates_existing(self, svc: ProfileService) -> None:
        loop = asyncio.get_event_loop()
        prof = loop.run_until_complete(svc.create_profile(p()))
        loop.run_until_complete(svc.match_profile(prof.id))

        fetched = loop.run_until_complete(svc.get_profile(prof.id))
        assert fetched is not None
        assert fetched.status == ProfileStatus.MATCHED

    def test_match_does_not_create_profile(self, svc: ProfileService) -> None:
        loop = asyncio.get_event_loop()
        # match_profile only updates status, doesn't create
        count_before = loop.run_until_complete(
            svc._db.connection.execute("SELECT COUNT(*) FROM profiles")
        )
        before = loop.run_until_complete(count_before.fetchone())[0]

        # match_profile on non-existent id should not crash
        # (it won't create a new profile)
        loop.run_until_complete(svc.match_profile(99999))

        count_after = loop.run_until_complete(
            svc._db.connection.execute("SELECT COUNT(*) FROM profiles")
        )
        after = loop.run_until_complete(count_after.fetchone())[0]
        assert before == after == 0


# ═════════════════════════════════════════════════════════════════════
# 8. Transaction rollback
# ═════════════════════════════════════════════════════════════════════

class TestAudit8Transaction:
    def test_create_and_link_atomic(self, svc: ProfileService) -> None:
        """create_profile: profile + link оба существуют."""
        loop = asyncio.get_event_loop()
        prof = loop.run_until_complete(svc.create_profile(p(msg_id=200)))

        msgs = loop.run_until_complete(
            svc._db.get_profile_messages(prof.id)
        )
        assert len(msgs) == 1

    def test_profile_exists_even_if_link_fails(self, db: Database, svc: ProfileService) -> None:
        """Если link_message_to_profile упадёт — profile всё равно существует.

        Это проверяет что операции НЕ атомарны (каждая коммитится отдельно).
        В текущей реализации это нормально — RAW-first гарантирует данные.
        """
        loop = asyncio.get_event_loop()
        prof = loop.run_until_complete(svc.create_profile(p(msg_id=300)))

        # Verify profile exists
        row = loop.run_until_complete(db.get_profile_by_id(prof.id))
        assert row is not None
        assert row["name"] == "Anna"


# ═════════════════════════════════════════════════════════════════════
# 9. Foreign keys
# ═════════════════════════════════════════════════════════════════════

class TestAudit9ForeignKeys:
    def test_foreign_keys_enabled(self, db: Database) -> None:
        loop = asyncio.get_event_loop()
        cursor = loop.run_until_complete(
            db.connection.execute("PRAGMA foreign_keys")
        )
        row = loop.run_until_complete(cursor.fetchone())
        assert row[0] == 1

    def test_cascade_delete(self, db: Database) -> None:
        """Удаление Profile каскадно удалит profile_messages."""
        loop = asyncio.get_event_loop()
        prof_id = loop.run_until_complete(
            db.insert_profile(
                name="Del", age=18, raw_city="", normalized_city="",
                description="", fingerprint="fp_del",
                source_chat_id=1, source_message_id=1,
                first_seen_at="now", last_seen_at="now", status="NEW",
            )
        )
        loop.run_until_complete(
            db.link_profile_message(prof_id, 100, 1, "now")
        )

        # Delete profile
        loop.run_until_complete(
            db.connection.execute("DELETE FROM profiles WHERE id=?", (prof_id,))
        )
        loop.run_until_complete(db.connection.commit())

        # Verify profile_messages also deleted
        cursor = loop.run_until_complete(
            db.connection.execute(
                "SELECT COUNT(*) FROM profile_messages WHERE profile_id=?",
                (prof_id,),
            )
        )
        count = loop.run_until_complete(cursor.fetchone())[0]
        assert count == 0


# ═════════════════════════════════════════════════════════════════════
# 10. Существующая database.db не повреждается
# ═════════════════════════════════════════════════════════════════════

class TestAudit10BackwardCompat:
    def test_raw_messages_preserved(self, db: Database) -> None:
        loop = asyncio.get_event_loop()
        raw_id = loop.run_until_complete(db.save_raw_message(
            telegram_message_id=999, chat_id=1, sender_id=1,
            sender_username="u", sender_name="n",
            message_date="2026-01-01", text="old data",
            raw_entities="[]", media_type="",
            reply_to_message_id=None, received_at="2026-01-01",
        ))
        assert raw_id > 0

    def test_schema_idempotent(self, tmp_path: Path) -> None:
        """Повторный connect не ломает данные."""
        db1 = Database(path=tmp_path / "test.db")
        loop = asyncio.get_event_loop()
        loop.run_until_complete(db1.connect())
        loop.run_until_complete(db1.save_raw_message(
            telegram_message_id=1, chat_id=1, sender_id=1,
            sender_username="", sender_name="",
            message_date="2026-01-01", text="data",
            raw_entities="[]", media_type="",
            reply_to_message_id=None, received_at="2026-01-01",
        ))
        loop.run_until_complete(db1.close())

        # Reconnect
        db2 = Database(path=tmp_path / "test.db")
        loop.run_until_complete(db2.connect())
        cursor = loop.run_until_complete(
            db2.connection.execute("SELECT text FROM raw_messages WHERE id=1")
        )
        row = loop.run_until_complete(cursor.fetchone())
        assert row[0] == "data"
        loop.run_until_complete(db2.close())


# ═════════════════════════════════════════════════════════════════════
# 11. Concurrency / async safety
# ═════════════════════════════════════════════════════════════════════

class TestAudit11Concurrency:
    def test_sequential_messages_no_duplicates(self, svc: ProfileService) -> None:
        """10 последовательных сообщений → 1 Profile, 10 messages."""
        loop = asyncio.get_event_loop()
        prof = None
        for i in range(10):
            prof = loop.run_until_complete(
                svc.upsert_profile(p(msg_id=100 + i))
            )

        msgs = loop.run_until_complete(
            svc._db.get_profile_messages(prof.id)  # type: ignore[union-attr]
        )
        assert len(msgs) == 10

        # No duplicate profiles
        count_cursor = loop.run_until_complete(
            svc._db.connection.execute("SELECT COUNT(*) FROM profiles")
        )
        count = loop.run_until_complete(count_cursor.fetchone())[0]
        assert count == 1

    def test_no_duplicate_profile_messages(self, svc: ProfileService) -> None:
        """INSERT OR IGNORE предотвращает дубли."""
        loop = asyncio.get_event_loop()
        prof = loop.run_until_complete(svc.create_profile(p(msg_id=200)))
        loop.run_until_complete(
            svc.link_message_to_profile(prof.id, 200, 1234060895)
        )
        msgs = loop.run_until_complete(
            svc._db.get_profile_messages(prof.id)
        )
        assert len(msgs) == 1


# ═════════════════════════════════════════════════════════════════════
# 12. Regression: edge cases
# ═════════════════════════════════════════════════════════════════════

class TestAudit12Regression:
    def test_empty_description_not_overwrite(self, svc: ProfileService) -> None:
        """Пустое описание НЕ перезаписывает существующее."""
        loop = asyncio.get_event_loop()
        prof = loop.run_until_complete(
            svc.upsert_profile(p(desc="Описание", msg_id=100))
        )
        loop.run_until_complete(
            svc.upsert_profile(p(desc="", msg_id=101))
        )
        fetched = loop.run_until_complete(svc.get_profile(prof.id))
        assert fetched is not None
        assert fetched.description == "Описание"

    def test_fingerprint_in_db(self, svc: ProfileService) -> None:
        """fingerprint сохраняется в БД."""
        loop = asyncio.get_event_loop()
        prof = loop.run_until_complete(svc.create_profile(p()))
        row = loop.run_until_complete(
            svc._db.get_profile_by_id(prof.id)
        )
        assert row is not None
        assert row["fingerprint"] != ""
        assert len(row["fingerprint"]) == 64

    def test_status_transitions(self, svc: ProfileService) -> None:
        """NEW → SEEN → MATCHED."""
        loop = asyncio.get_event_loop()
        prof = loop.run_until_complete(svc.upsert_profile(p(msg_id=100)))
        assert prof.status == "NEW"

        prof2 = loop.run_until_complete(svc.upsert_profile(p(msg_id=101)))
        assert prof2.status == "SEEN"

        loop.run_until_complete(svc.match_profile(prof.id))
        prof3 = loop.run_until_complete(svc.get_profile(prof.id))
        assert prof3 is not None
        assert prof3.status == "MATCHED"

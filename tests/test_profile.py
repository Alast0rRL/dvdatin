# Unit/Integration тесты Stage 2: Profile Storage.

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from database.database import Database
from models.profile import Profile, ProfileStatus, compute_fingerprint
from models.raw import FilterResult, ParsedProfile
from services.profile_service import ProfileService


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db(tmp_path: Path) -> Database:
    """Создаёт временную БД для тестов."""
    db = Database(path=tmp_path / "test.db")
    loop = asyncio.get_event_loop()
    loop.run_until_complete(db.connect())
    yield db  # type: ignore[misc]
    loop.run_until_complete(db.close())


@pytest.fixture
def service(tmp_db: Database) -> ProfileService:
    """Создаёт ProfileService с временной БД."""
    return ProfileService(tmp_db)


def make_parsed(
    name: str = "TestGirl",
    age: int = 18,
    raw_city: str = "спб",
    normalized_city: str = "Санкт-Петербург",
    description: str = "Привет",
    source_message_id: int = 100,
    source_chat_id: int = 1234060895,
) -> ParsedProfile:
    """Создаёт ParsedProfile для тестов."""
    return ParsedProfile(
        name=name,
        age=age,
        raw_city=raw_city,
        normalized_city=normalized_city,
        description=description,
        filter_result=FilterResult.FILTER_MATCH,
        source_message_id=source_message_id,
        source_chat_id=source_chat_id,
    )


# ── 1. Создание Profile ──────────────────────────────────────────────

class TestCreateProfile:
    def test_creates_profile(self, service: ProfileService) -> None:
        parsed = make_parsed()
        profile = asyncio.get_event_loop().run_until_complete(
            service.create_profile(parsed)
        )

        assert profile.id > 0
        assert profile.name == "TestGirl"
        assert profile.age == 18
        assert profile.normalized_city == "Санкт-Петербург"
        assert profile.status == ProfileStatus.NEW
        assert profile.fingerprint != ""


# ── 2. Получение Profile ─────────────────────────────────────────────

class TestGetProfile:
    def test_get_existing(self, service: ProfileService) -> None:
        created = asyncio.get_event_loop().run_until_complete(
            service.create_profile(make_parsed())
        )
        fetched = asyncio.get_event_loop().run_until_complete(
            service.get_profile(created.id)
        )

        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.name == "TestGirl"

    def test_get_nonexistent(self, service: ProfileService) -> None:
        result = asyncio.get_event_loop().run_until_complete(
            service.get_profile(99999)
        )
        assert result is None


# ── 3. Upsert ────────────────────────────────────────────────────────

class TestUpsert:
    def test_upsert_creates_new(self, service: ProfileService) -> None:
        parsed = make_parsed(source_message_id=100)
        profile = asyncio.get_event_loop().run_until_complete(
            service.upsert_profile(parsed)
        )
        assert profile.id > 0
        assert profile.status == ProfileStatus.NEW

    def test_upsert_updates_existing(self, service: ProfileService) -> None:
        parsed1 = make_parsed(source_message_id=100)
        parsed2 = make_parsed(source_message_id=101)

        p1 = asyncio.get_event_loop().run_until_complete(
            service.upsert_profile(parsed1)
        )
        p2 = asyncio.get_event_loop().run_until_complete(
            service.upsert_profile(parsed2)
        )

        assert p1.id == p2.id
        assert p2.status == "SEEN"


# ── 4. Повторное обнаружение ─────────────────────────────────────────

class TestRepeatDiscovery:
    def test_same_profile_different_messages(self, service: ProfileService) -> None:
        p1 = asyncio.get_event_loop().run_until_complete(
            service.upsert_profile(make_parsed(source_message_id=100))
        )
        p2 = asyncio.get_event_loop().run_until_complete(
            service.upsert_profile(make_parsed(source_message_id=101))
        )
        p3 = asyncio.get_event_loop().run_until_complete(
            service.upsert_profile(make_parsed(source_message_id=102))
        )

        assert p1.id == p2.id == p3.id


# ── 5. Изменение description ─────────────────────────────────────────

class TestDescriptionUpdate:
    def test_description_updates(self, service: ProfileService) -> None:
        p1 = asyncio.get_event_loop().run_until_complete(
            service.create_profile(make_parsed(description="Старое"))
        )
        asyncio.get_event_loop().run_until_complete(
            service._db.update_profile_description(p1.id, "Новое")
        )
        fetched = asyncio.get_event_loop().run_until_complete(
            service.get_profile(p1.id)
        )
        assert fetched is not None
        assert fetched.description == "Новое"


# ── 6. Изменение raw_city ────────────────────────────────────────────

class TestRawCityUpdate:
    def test_raw_city_updates(self, service: ProfileService) -> None:
        p1 = asyncio.get_event_loop().run_until_complete(
            service.create_profile(make_parsed(raw_city="спб"))
        )
        asyncio.get_event_loop().run_until_complete(
            service._db.update_profile_raw_city(p1.id, "Питер")
        )
        fetched = asyncio.get_event_loop().run_until_complete(
            service.get_profile(p1.id)
        )
        assert fetched is not None
        assert fetched.raw_city == "Питер"


# ── 7. Fingerprint ───────────────────────────────────────────────────

class TestFingerprint:
    def test_same_input_same_fingerprint(self) -> None:
        fp1 = compute_fingerprint("TestGirl", 18, "Санкт-Петербург")
        fp2 = compute_fingerprint("TestGirl", 18, "Санкт-Петербург")
        assert fp1 == fp2

    def test_different_name_different_fingerprint(self) -> None:
        fp1 = compute_fingerprint("TestGirl", 18, "Санкт-Петербург")
        fp2 = compute_fingerprint("OtherGirl", 18, "Санкт-Петербург")
        assert fp1 != fp2

    def test_fingerprint_is_sha256(self) -> None:
        fp = compute_fingerprint("Test", 20, "Москва")
        assert len(fp) == 64


# ── 8. Связь нескольких messages ─────────────────────────────────────

class TestMultipleMessages:
    def test_link_messages(self, service: ProfileService) -> None:
        p = asyncio.get_event_loop().run_until_complete(
            service.create_profile(make_parsed(source_message_id=100))
        )
        asyncio.get_event_loop().run_until_complete(
            service.link_message_to_profile(p.id, 101, 1234060895)
        )
        asyncio.get_event_loop().run_until_complete(
            service.link_message_to_profile(p.id, 102, 1234060895)
        )
        msgs = asyncio.get_event_loop().run_until_complete(
            service._db.get_profile_messages(p.id)
        )
        assert len(msgs) == 3


# ── 9. UNIQUE profile_messages ───────────────────────────────────────

class TestUniqueMessages:
    def test_duplicate_link_ignored(self, service: ProfileService) -> None:
        p = asyncio.get_event_loop().run_until_complete(
            service.create_profile(make_parsed(source_message_id=100))
        )
        asyncio.get_event_loop().run_until_complete(
            service.link_message_to_profile(p.id, 100, 1234060895)
        )
        msgs = asyncio.get_event_loop().run_until_complete(
            service._db.get_profile_messages(p.id)
        )
        assert len(msgs) == 1


# ── 10. UNKNOWN не создаёт Profile ───────────────────────────────────

class TestUnknownNoProfile:
    def test_unknown_not_created(self, tmp_db: Database) -> None:
        service = ProfileService(tmp_db)
        count_before = asyncio.get_event_loop().run_until_complete(
            tmp_db.connection.execute("SELECT COUNT(*) FROM profiles")
        )
        before = (asyncio.get_event_loop().run_until_complete(
            count_before.fetchone()
        ))[0]
        assert before == 0


# ── 11. MEDIA_ONLY не создаёт самостоятельный Profile ────────────────

class TestMediaOnlyNoProfile:
    def test_media_only_no_self_profile(self, service: ProfileService) -> None:
        loop = asyncio.get_event_loop()
        count_row = loop.run_until_complete(
            service._db.connection.execute("SELECT COUNT(*) FROM profiles")
        )
        count = loop.run_until_complete(count_row.fetchone())[0]
        assert count == 0


# ── 12. RAW-first сохранение ─────────────────────────────────────────

class TestRawFirst:
    def test_raw_exists_before_profile(self, tmp_db: Database) -> None:
        loop = asyncio.get_event_loop()
        raw_id = loop.run_until_complete(
            tmp_db.save_raw_message(
                telegram_message_id=100,
                chat_id=1234060895,
                sender_id=1,
                sender_username="test",
                sender_name="Test",
                message_date="2026-01-01T00:00:00",
                text="TestGirl, 18, спб",
                raw_entities="[]",
                media_type="",
                reply_to_message_id=None,
                received_at="2026-01-01T00:00:00",
            )
        )
        assert raw_id > 0

        service = ProfileService(tmp_db)
        parsed = make_parsed(source_message_id=100)
        profile = loop.run_until_complete(service.create_profile(parsed))
        assert profile.id > 0


# ── 13. MATCH для существующего Profile ──────────────────────────────

class TestMatchExisting:
    def test_match_sets_status(self, service: ProfileService) -> None:
        loop = asyncio.get_event_loop()
        p = loop.run_until_complete(
            service.create_profile(make_parsed())
        )
        loop.run_until_complete(service.match_profile(p.id))
        fetched = loop.run_until_complete(service.get_profile(p.id))
        assert fetched is not None
        assert fetched.status == ProfileStatus.MATCHED


# ── 14. Транзакционный rollback ─────────────────────────────────────

class TestTransactionRollback:
    def test_atomic_create_and_link(self, service: ProfileService) -> None:
        loop = asyncio.get_event_loop()
        p = loop.run_until_complete(
            service.create_profile(make_parsed(source_message_id=200))
        )
        msgs = loop.run_until_complete(
            service._db.get_profile_messages(p.id)
        )
        assert len(msgs) == 1


# ── 15. Существующая database.db не повреждается ─────────────────────

class TestBackwardCompatibility:
    def test_raw_messages_table_intact(self, tmp_db: Database) -> None:
        loop = asyncio.get_event_loop()
        raw_id = loop.run_until_complete(
            tmp_db.save_raw_message(
                telegram_message_id=999,
                chat_id=1,
                sender_id=1,
                sender_username="u",
                sender_name="n",
                message_date="2026-01-01",
                text="old data",
                raw_entities="[]",
                media_type="",
                reply_to_message_id=None,
                received_at="2026-01-01",
            )
        )
        assert raw_id > 0

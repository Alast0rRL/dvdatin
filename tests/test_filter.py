# Unit/Integration тесты Stage 3: Filter Engine.

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.config import AppConfig
from database.database import Database
from models.filter import FilterDecision, FilterReason, FilterResult
from models.profile import Profile, ProfileStatus
from models.raw import FilterResult as ParsedFilterResult, ParsedProfile
from services.filter_engine import FilterEngine
from services.filter_service import FilterService
from services.profile_service import ProfileService


# ── Fixtures ──────────────────────────────────────────────────────────

def make_config(
    age_min: int = 18,
    age_max: int = 19,
    city_allowed: list[str] | None = None,
) -> AppConfig:
    """Создаёт конфиг для тестов."""
    if city_allowed is None:
        city_allowed = ["Санкт-Петербург"]
    return AppConfig(**{
        "telegram": {"api_id": 123, "api_hash": "abc"},
        "filters": {
            "age": {"min": age_min, "max": age_max},
            "city": {"allowed": city_allowed},
        },
    })


@pytest.fixture
def config() -> AppConfig:
    return make_config()


@pytest.fixture
def engine(config: AppConfig) -> FilterEngine:
    return FilterEngine(config)


@pytest.fixture
def tmp_db(tmp_path: Path) -> Database:
    db = Database(path=tmp_path / "test_filter.db")
    loop = asyncio.get_event_loop()
    loop.run_until_complete(db.connect())
    yield db  # type: ignore[misc]
    loop.run_until_complete(db.close())


@pytest.fixture
def profile_service(tmp_db: Database) -> ProfileService:
    return ProfileService(tmp_db)


@pytest.fixture
def filter_service(tmp_db: Database, profile_service: ProfileService, config: AppConfig) -> FilterService:
    engine = FilterEngine(config)
    return FilterService(tmp_db, profile_service, engine)


def make_profile(
    name: str = "Anna",
    age: int | None = 19,
    normalized_city: str = "Санкт-Петербург",
    profile_id: int = 1,
) -> Profile:
    return Profile(
        id=profile_id,
        name=name,
        age=age or 0,
        normalized_city=normalized_city,
    )


def make_parsed(
    name: str = "Anna",
    age: int | None = 19,
    raw_city: str = "спб",
    normalized_city: str = "Санкт-Петербург",
    msg_id: int = 100,
) -> ParsedProfile:
    return ParsedProfile(
        name=name, age=age, raw_city=raw_city,
        normalized_city=normalized_city,
        filter_result=ParsedFilterResult.FILTER_MATCH,
        source_message_id=msg_id, source_chat_id=1234060895,
    )


# ═════════════════════════════════════════════════════════════════════
# 1. CASE 1: age=18, city=СПб → PASS
# ═════════════════════════════════════════════════════════════════════

class TestCase1:
    def test_pass(self, engine: FilterEngine) -> None:
        p = make_profile(age=18, normalized_city="Санкт-Петербург")
        result = engine.evaluate(p)
        assert result.decision == FilterDecision.PASS
        assert FilterReason.AGE_OK in result.reasons
        assert FilterReason.CITY_OK in result.reasons


# ═════════════════════════════════════════════════════════════════════
# 2. CASE 2: age=19, city=СПб → PASS
# ═════════════════════════════════════════════════════════════════════

class TestCase2:
    def test_pass(self, engine: FilterEngine) -> None:
        p = make_profile(age=19, normalized_city="Санкт-Петербург")
        result = engine.evaluate(p)
        assert result.decision == FilterDecision.PASS


# ═════════════════════════════════════════════════════════════════════
# 3. CASE 3: age=17, city=СПб → REJECT (AGE_OUT_OF_RANGE)
# ═════════════════════════════════════════════════════════════════════

class TestCase3:
    def test_reject_age(self, engine: FilterEngine) -> None:
        p = make_profile(age=17, normalized_city="Санкт-Петербург")
        result = engine.evaluate(p)
        assert result.decision == FilterDecision.REJECT
        assert FilterReason.AGE_OUT_OF_RANGE in result.reasons


# ═════════════════════════════════════════════════════════════════════
# 4. CASE 4: age=20, city=СПб → REJECT (AGE_OUT_OF_RANGE)
# ═════════════════════════════════════════════════════════════════════

class TestCase4:
    def test_reject_age(self, engine: FilterEngine) -> None:
        p = make_profile(age=20, normalized_city="Санкт-Петербург")
        result = engine.evaluate(p)
        assert result.decision == FilterDecision.REJECT
        assert FilterReason.AGE_OUT_OF_RANGE in result.reasons


# ═════════════════════════════════════════════════════════════════════
# 5. CASE 5: age=19, city=Москва → REJECT (CITY_OUT_OF_RANGE)
# ═════════════════════════════════════════════════════════════════════

class TestCase5:
    def test_reject_city(self, engine: FilterEngine) -> None:
        p = make_profile(age=19, normalized_city="Москва")
        result = engine.evaluate(p)
        assert result.decision == FilterDecision.REJECT
        assert FilterReason.CITY_OUT_OF_RANGE in result.reasons


# ═════════════════════════════════════════════════════════════════════
# 6. CASE 6: age missing, city=СПб → REVIEW (AGE_UNKNOWN)
# ═════════════════════════════════════════════════════════════════════

class TestCase6:
    def test_review_age(self, engine: FilterEngine) -> None:
        p = make_profile(age=None, normalized_city="Санкт-Петербург")
        result = engine.evaluate(p)
        assert result.decision == FilterDecision.REVIEW
        assert FilterReason.AGE_UNKNOWN in result.reasons


# ═════════════════════════════════════════════════════════════════════
# 7. CASE 7: age=19, city missing → REVIEW (CITY_UNKNOWN)
# ═════════════════════════════════════════════════════════════════════

class TestCase7:
    def test_review_city(self, engine: FilterEngine) -> None:
        p = make_profile(age=19, normalized_city="")
        result = engine.evaluate(p)
        assert result.decision == FilterDecision.REVIEW
        assert FilterReason.CITY_UNKNOWN in result.reasons


# ═════════════════════════════════════════════════════════════════════
# 8. CASE 8: age missing, city missing → REVIEW (INSUFFICIENT_DATA)
# ═════════════════════════════════════════════════════════════════════

class TestCase8:
    def test_review_insufficient(self, engine: FilterEngine) -> None:
        p = make_profile(age=None, normalized_city="")
        result = engine.evaluate(p)
        assert result.decision == FilterDecision.REVIEW
        assert FilterReason.INSUFFICIENT_DATA in result.reasons


# ═════════════════════════════════════════════════════════════════════
# 9. Multiple reasons
# ═════════════════════════════════════════════════════════════════════

class TestMultipleReasons:
    def test_all_reasons_collected(self, engine: FilterEngine) -> None:
        """age=19, city=Москва → REJECT with AGE_OK + CITY_OUT_OF_RANGE."""
        p = make_profile(age=19, normalized_city="Москва")
        result = engine.evaluate(p)
        assert result.decision == FilterDecision.REJECT
        assert FilterReason.AGE_OK in result.reasons
        assert FilterReason.CITY_OUT_OF_RANGE in result.reasons

    def test_review_with_age_unknown(self, engine: FilterEngine) -> None:
        """age missing, city=Москва → REJECT (REJECT > REVIEW)."""
        p = make_profile(age=None, normalized_city="Москва")
        result = engine.evaluate(p)
        assert result.decision == FilterDecision.REJECT
        assert FilterReason.AGE_UNKNOWN in result.reasons
        assert FilterReason.CITY_OUT_OF_RANGE in result.reasons

    def test_reasons_are_unique(self, engine: FilterEngine) -> None:
        """AGE_OK от AgeRule и DataCompletenessRule не должен дублироваться."""
        p = make_profile(age=19, normalized_city="Санкт-Петербург")
        result = engine.evaluate(p)
        assert result.decision == FilterDecision.PASS
        assert FilterReason.AGE_OK in result.reasons
        assert result.reasons.count(FilterReason.AGE_OK) == 1
        assert len(result.reasons) == len(set(result.reasons))


# ═════════════════════════════════════════════════════════════════════
# 10. Priority: REJECT > REVIEW > PASS
# ═════════════════════════════════════════════════════════════════════

class TestPriority:
    def test_reject_beats_review(self, engine: FilterEngine) -> None:
        """REJECT always wins over REVIEW."""
        p = make_profile(age=None, normalized_city="Москва")
        result = engine.evaluate(p)
        assert result.decision == FilterDecision.REJECT

    def test_review_when_no_reject(self, engine: FilterEngine) -> None:
        """REVIEW when no REJECT."""
        p = make_profile(age=None, normalized_city="Санкт-Петербург")
        result = engine.evaluate(p)
        assert result.decision == FilterDecision.REVIEW

    def test_pass_when_clean(self, engine: FilterEngine) -> None:
        """PASS when all OK."""
        p = make_profile(age=19, normalized_city="Санкт-Петербург")
        result = engine.evaluate(p)
        assert result.decision == FilterDecision.PASS


# ═════════════════════════════════════════════════════════════════════
# 11. City normalization
# ═════════════════════════════════════════════════════════════════════

class TestCityNormalization:
    def test_normalized_city_used(self, engine: FilterEngine) -> None:
        """normalized_city is what matters, not raw_city."""
        p = make_profile(age=19, normalized_city="Санкт-Петербург")
        result = engine.evaluate(p)
        assert FilterReason.CITY_OK in result.reasons

    def test_wrong_city_rejected(self, engine: FilterEngine) -> None:
        p = make_profile(age=19, normalized_city="Казань")
        result = engine.evaluate(p)
        assert FilterReason.CITY_OUT_OF_RANGE in result.reasons


# ═════════════════════════════════════════════════════════════════════
# 12. Filter history
# ═════════════════════════════════════════════════════════════════════

class TestFilterHistory:
    def test_history_saved(self, filter_service: FilterService, profile_service: ProfileService) -> None:
        loop = asyncio.get_event_loop()
        prof = loop.run_until_complete(
            profile_service.create_profile(make_parsed())
        )
        loop.run_until_complete(filter_service.evaluate(prof))
        loop.run_until_complete(filter_service.evaluate(prof))

        history = loop.run_until_complete(filter_service.get_history(prof.id))
        assert len(history) == 2


# ═════════════════════════════════════════════════════════════════════
# 13. Repeated evaluation
# ═════════════════════════════════════════════════════════════════════

class TestRepeatedEvaluation:
    def test_idempotent(self, filter_service: FilterService, profile_service: ProfileService) -> None:
        loop = asyncio.get_event_loop()
        prof = loop.run_until_complete(
            profile_service.create_profile(make_parsed())
        )
        r1 = loop.run_until_complete(filter_service.evaluate(prof))
        r2 = loop.run_until_complete(filter_service.evaluate(prof))
        assert r1.decision == r2.decision == FilterDecision.PASS


# ═════════════════════════════════════════════════════════════════════
# 14. get_latest_result
# ═════════════════════════════════════════════════════════════════════

class TestGetLatestResult:
    def test_latest(self, filter_service: FilterService, profile_service: ProfileService) -> None:
        loop = asyncio.get_event_loop()
        prof = loop.run_until_complete(
            profile_service.create_profile(make_parsed())
        )
        loop.run_until_complete(filter_service.evaluate(prof))
        latest = loop.run_until_complete(filter_service.get_latest_result(prof.id))
        assert latest is not None
        assert latest.decision == FilterDecision.PASS

    def test_no_result(self, filter_service: FilterService) -> None:
        loop = asyncio.get_event_loop()
        latest = loop.run_until_complete(filter_service.get_latest_result(999))
        assert latest is None


# ═════════════════════════════════════════════════════════════════════
# 15. get_history
# ═════════════════════════════════════════════════════════════════════

class TestGetHistory:
    def test_history(self, filter_service: FilterService, profile_service: ProfileService) -> None:
        loop = asyncio.get_event_loop()
        prof = loop.run_until_complete(
            profile_service.create_profile(make_parsed())
        )
        loop.run_until_complete(filter_service.evaluate(prof))
        history = loop.run_until_complete(filter_service.get_history(prof.id))
        assert len(history) >= 1


# ═════════════════════════════════════════════════════════════════════
# 16. Database foreign key
# ═════════════════════════════════════════════════════════════════════

class TestForeignKey:
    def test_cascade_delete(self, tmp_db: Database) -> None:
        loop = asyncio.get_event_loop()
        prof_id = loop.run_until_complete(
            tmp_db.insert_profile(
                name="Del", age=18, raw_city="", normalized_city="Санкт-Петербург",
                description="", fingerprint="fp_del",
                source_chat_id=1, source_message_id=1,
                first_seen_at="now", last_seen_at="now", status="NEW",
            )
        )
        loop.run_until_complete(
            tmp_db.save_filter_result(
                profile_id=prof_id, decision="PASS",
                reasons='["AGE_OK"]', rules_checked=3, evaluated_at="now",
            )
        )
        loop.run_until_complete(
            tmp_db.connection.execute("DELETE FROM profiles WHERE id=?", (prof_id,))
        )
        loop.run_until_complete(tmp_db.connection.commit())

        cursor = loop.run_until_complete(
            tmp_db.connection.execute(
                "SELECT COUNT(*) FROM filter_results WHERE profile_id=?",
                (prof_id,),
            )
        )
        count = loop.run_until_complete(cursor.fetchone())[0]
        assert count == 0


# ═════════════════════════════════════════════════════════════════════
# 17. Config-driven age range
# ═════════════════════════════════════════════════════════════════════

class TestConfigAge:
    def test_custom_age_range(self) -> None:
        config = make_config(age_min=20, age_max=25)
        engine = FilterEngine(config)
        p = make_profile(age=20, normalized_city="Санкт-Петербург")
        result = engine.evaluate(p)
        assert result.decision == FilterDecision.PASS

    def test_outside_custom_range(self) -> None:
        config = make_config(age_min=20, age_max=25)
        engine = FilterEngine(config)
        p = make_profile(age=19, normalized_city="Санкт-Петербург")
        result = engine.evaluate(p)
        assert result.decision == FilterDecision.REJECT


# ═════════════════════════════════════════════════════════════════════
# 18. Config-driven city list
# ═════════════════════════════════════════════════════════════════════

class TestConfigCity:
    def test_custom_city_list(self) -> None:
        config = make_config(city_allowed=["Санкт-Петербург", "Москва"])
        engine = FilterEngine(config)
        p = make_profile(age=19, normalized_city="Москва")
        result = engine.evaluate(p)
        assert result.decision == FilterDecision.PASS

    def test_city_not_in_list(self) -> None:
        config = make_config(city_allowed=["Санкт-Петербург", "Москва"])
        engine = FilterEngine(config)
        p = make_profile(age=19, normalized_city="Казань")
        result = engine.evaluate(p)
        assert result.decision == FilterDecision.REJECT

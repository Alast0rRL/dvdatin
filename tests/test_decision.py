# Unit/Integration тесты DecisionService (детерминированный Decision Engine).
# Полностью offline — используется детерминированный scoring без LLM/CLIP.

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import AppConfig
from database.database import Database
from models.decision import AIDecision
from models.profile import Profile
from services.decision_service import DecisionService
from services.filter_engine import FilterEngine
from services.filter_service import FilterService
from services.profile_service import ProfileService


# ── Fixtures ──────────────────────────────────────────────────────────

def make_config(
    like_threshold: float = 0.75,
    review_threshold: float = 0.50,
    scoring_version: str = "deterministic-v2",
) -> AppConfig:
    return AppConfig(**{
        "telegram": {"api_id": 123, "api_hash": "abc"},
        "filters": {
            "age": {"min": 18, "max": 19},
            "city": {"allowed": ["Санкт-Петербург"]},
        },
        "ai": {
            "enabled": True,
            "decision": {
                "like_threshold": like_threshold,
                "review_threshold": review_threshold,
                "scoring_version": scoring_version,
            },
        },
    })


def make_profile(
    name="Anna", age=19, normalized_city="Санкт-Петербург",
    description="Люблю природу", profile_id=1,
) -> Profile:
    return Profile(
        id=profile_id, name=name, age=age,
        normalized_city=normalized_city, description=description,
    )


@pytest.fixture
def tmp_db(tmp_path: Path) -> Database:
    db = Database(path=tmp_path / "test_decision.db")
    loop = asyncio.get_event_loop()
    loop.run_until_complete(db.connect())
    yield db  # type: ignore[misc]
    loop.run_until_complete(db.close())


def build_stack(db, config, preferences=None) -> DecisionService:
    profile_service = ProfileService(db)
    filter_engine = FilterEngine(config)
    filter_service = FilterService(db, profile_service, filter_engine)
    return DecisionService(db, config, profile_service, filter_service, preferences)


async def insert_profile(
    db: Database,
    profile_id: int,
    city: str = "Санкт-Петербург",
    description: str = "Люблю природу",
) -> int:
    return await db.insert_profile(
        name="Anna", age=19, raw_city=city, normalized_city=city,
        description=description, fingerprint=f"fp_d_{profile_id}",
        source_chat_id=1234060895, source_message_id=profile_id,
        first_seen_at="now", last_seen_at="now", status="NEW",
    )


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── Модель ───────────────────────────────────────────────────────────

class TestAIDecisionModel:
    def test_enum_values(self) -> None:
        assert AIDecision.LIKE == "LIKE"
        assert AIDecision.REVIEW == "REVIEW"
        assert AIDecision.DISLIKE == "DISLIKE"

    def test_result_validation(self) -> None:
        r = AIDecisionResult_factory()
        assert r.decision == AIDecision.LIKE

    def test_score_validation(self) -> None:
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            AIDecisionResult_factory(combined_score=1.5)

    def test_reasons_json(self) -> None:
        r = AIDecisionResult_factory(reasons=["POSITIVE:games:игры", "ok"])
        import json
        assert json.loads(r.reasons_json()) == ["POSITIVE:games:игры", "ok"]

    def test_decision_field_is_enum(self) -> None:
        """Поле decision должно быть AIDecision, а не строкой."""
        r = AIDecisionResult_factory(decision="DISLIKE")
        assert r.decision == AIDecision.DISLIKE
        assert isinstance(r.decision, AIDecision)
        assert r.decision.value == "DISLIKE"


def AIDecisionResult_factory(**kw):
    from models.decision import AIDecisionResult
    base = dict(
        profile_id=1, decision="LIKE", combined_score=0.8,
        confidence=0.85,
        reasons=["POSITIVE:games:игры"], evaluated_at="now",
        scoring_version="deterministic-v2",
    )
    base.update(kw)
    return AIDecisionResult(**base)


# ── PASS: LIKE / REVIEW / DISLIKE ────────────────────────────────────

class TestDecisionBasic:
    def test_pass_positive_features_like(self, tmp_db: Database) -> None:
        """Профиль с несколькими положительными признаками → LIKE."""
        config = make_config()
        decision = build_stack(tmp_db, config)
        prof_id = run(insert_profile(
            tmp_db, 1,
            description="Люблю аниме, играю в доту, переехала в питер"
        ))
        result = run(decision.evaluate(prof_id))
        assert result.decision == AIDecision.LIKE
        assert any("POSITIVE:" in r for r in result.reasons)

    def test_pass_no_features_review(self, tmp_db: Database) -> None:
        """Профиль без положительных/отрицательных признаков → REVIEW."""
        config = make_config()
        decision = build_stack(tmp_db, config)
        prof_id = run(insert_profile(
            tmp_db, 2, description="Обычная анкета"
        ))
        result = run(decision.evaluate(prof_id))
        assert result.decision == AIDecision.REVIEW

    def test_hard_negative_becomes_dislike(self, tmp_db: Database) -> None:
        """Подтверждённый жёсткий негатив (ищу друга) → DISLIKE."""
        config = make_config()
        decision = build_stack(tmp_db, config)
        prof_id = run(insert_profile(
            tmp_db, 3, description="ищу друга для общения"
        ))
        result = run(decision.evaluate(prof_id))
        assert result.decision == AIDecision.DISLIKE
        assert any("HARD_NEGATIVE:" in r for r in result.reasons)

    def test_reuses_passed_filter_result(self, tmp_db: Database) -> None:
        """Переданный filter_result НЕ должен вызывать повторную оценку фильтра."""
        config = make_config()
        decision = build_stack(tmp_db, config)
        prof_id = run(insert_profile(tmp_db, 30))
        profile = run(decision._profile_service.get_profile(prof_id))
        fr = run(decision._filter_service.evaluate(profile))

        calls: list[int] = []
        original = decision._filter_service.evaluate

        async def spy(p):
            calls.append(1)
            return await original(p)

        decision._filter_service.evaluate = spy

        result = run(decision.evaluate_profile(profile, filter_result=fr))
        assert len(calls) == 0
        assert result.decision in (AIDecision.LIKE, AIDecision.REVIEW)


# ── HARD FILTERS ─────────────────────────────────────────────────────

class TestDecisionHardFilters:
    def test_reject_profile_dislike(self, tmp_db: Database) -> None:
        """Фильтр REJECT (город не в списке) → DISLIKE."""
        config = make_config()
        decision = build_stack(tmp_db, config)
        prof_id = run(insert_profile(tmp_db, 4, city="Москва"))
        result = run(decision.evaluate(prof_id))
        assert result.decision == AIDecision.DISLIKE
        assert "FILTER_REJECTED" in result.reasons

    def test_review_filter_review(self, tmp_db: Database) -> None:
        """Фильтр REVIEW (город не распознан) → REVIEW."""
        config = make_config()
        decision = build_stack(tmp_db, config)
        prof_id = run(insert_profile(tmp_db, 5, city=""))
        result = run(decision.evaluate(prof_id))
        assert result.decision == AIDecision.REVIEW
        assert "FILTER_REVIEW" in result.reasons


# ── SIGNALS: feature-based scoring ───────────────────────────────────

class TestDecisionSignals:
    def test_positive_feature_only_review(self, tmp_db: Database) -> None:
        """Одиночный положительный признак (score < like_threshold) → REVIEW."""
        config = make_config()
        decision = build_stack(tmp_db, config)
        prof_id = run(insert_profile(
            tmp_db, 7, description="Люблю аниме и мангу"
        ))
        result = run(decision.evaluate(prof_id))
        # один признак → score = 0.5 + 0.1 = 0.6 < 0.75 → REVIEW
        assert result.decision == AIDecision.REVIEW
        assert result.combined_score > 0

    def test_multiple_positive_features(self, tmp_db: Database) -> None:
        """Несколько положительных признаков → высокий score → LIKE."""
        config = make_config()
        decision = build_stack(tmp_db, config)
        prof_id = run(insert_profile(
            tmp_db, 9, description="Аниме, играю в доту, переехала в питер"
        ))
        result = run(decision.evaluate(prof_id))
        assert result.decision == AIDecision.LIKE
        assert result.combined_score >= config.ai.decision.like_threshold

    def test_no_features_not_zero_score(self, tmp_db: Database) -> None:
        """Отсутствие признаков → score = base_score (0.5), REVIEW."""
        config = make_config()
        decision = build_stack(tmp_db, config)
        prof_id = run(insert_profile(tmp_db, 10, description=""))
        result = run(decision.evaluate(prof_id))
        assert result.combined_score > 0
        assert result.decision == AIDecision.REVIEW

    def test_hard_negative_overrides_positive(self, tmp_db: Database) -> None:
        """Есть и негатив и позитив → негатив побеждает → DISLIKE."""
        config = make_config()
        decision = build_stack(tmp_db, config)
        prof_id = run(insert_profile(
            tmp_db, 11,
            description="ищу друга, люблю аниме и игры",
        ))
        result = run(decision.evaluate(prof_id))
        assert result.decision == AIDecision.DISLIKE

    def test_filter_reject_overrides_positive_features(self, tmp_db: Database) -> None:
        """Фильтр REJECT побеждает положительные признаки → DISLIKE."""
        config = make_config()
        decision = build_stack(tmp_db, config)
        prof_id = run(insert_profile(
            tmp_db, 12, city="Москва",
            description="Аниме, играю в доту",
        ))
        result = run(decision.evaluate(prof_id))
        assert result.decision == AIDecision.DISLIKE
        assert "FILTER_REJECTED" in result.reasons


# ── CONFIGURABLE thresholds ──────────────────────────────────────────

class TestDecisionConfig:
    def test_configurable_thresholds(self, tmp_db: Database) -> None:
        """Низкий like_threshold → положительные признаки дают LIKE."""
        config = make_config(like_threshold=0.50, review_threshold=0.40)
        decision = build_stack(tmp_db, config)
        prof_id = run(insert_profile(
            tmp_db, 12, description="Люблю аниме"
        ))
        result = run(decision.evaluate(prof_id))
        assert result.decision == AIDecision.LIKE

    def test_high_like_threshold_needs_more_features(self, tmp_db: Database) -> None:
        """Высокий like_threshold → один признак может не дотянуть до LIKE."""
        config = make_config(like_threshold=0.99)
        decision = build_stack(tmp_db, config)
        prof_id = run(insert_profile(
            tmp_db, 13, description="Люблю аниме"
        ))
        result = run(decision.evaluate(prof_id))
        # Один признак → score ≈ 0.6, что < 0.99 → REVIEW
        assert result.decision == AIDecision.REVIEW


# ── DATABASE / versions ──────────────────────────────────────────────

class TestDecisionDB:
    def test_decision_history(self, tmp_db: Database) -> None:
        config = make_config()
        decision = build_stack(tmp_db, config)
        prof_id = run(insert_profile(tmp_db, 20))
        run(decision.evaluate(prof_id))
        run(decision.evaluate(prof_id))
        history = run(decision.get_history(prof_id))
        assert len(history) == 2
        latest = run(decision.get_latest(prof_id))
        assert latest is not None
        assert latest.profile_id == prof_id

    def test_scoring_version_is_deterministic(self, tmp_db: Database) -> None:
        """Scoring version всегда deterministic-v2 (не зависит от конфигурации)."""
        config = make_config(scoring_version="ignored")
        decision = build_stack(tmp_db, config)
        prof_id = run(insert_profile(tmp_db, 21))
        result = run(decision.evaluate(prof_id))
        assert result.scoring_version == "deterministic-v2"

    def test_default_scoring_version(self, tmp_db: Database) -> None:
        config = make_config()
        decision = build_stack(tmp_db, config)
        prof_id = run(insert_profile(tmp_db, 22))
        result = run(decision.evaluate(prof_id))
        assert result.scoring_version == "deterministic-v2"

    def test_profile_not_found_returns_none(self, tmp_db: Database) -> None:
        config = make_config()
        decision = build_stack(tmp_db, config)
        result = run(decision.evaluate(9999))
        assert result is None

    def test_foreign_key_cascade(self, tmp_db: Database) -> None:
        config = make_config()
        decision = build_stack(tmp_db, config)
        prof_id = run(insert_profile(tmp_db, 23))
        run(decision.evaluate(prof_id))
        run(tmp_db.connection.execute(
            "DELETE FROM profiles WHERE id=?", (prof_id,)
        ))
        run(tmp_db.connection.commit())
        cursor = run(tmp_db.connection.execute(
            "SELECT COUNT(*) FROM ai_decisions WHERE profile_id=?", (prof_id,)
        ))
        count = run(cursor.fetchone())[0]
        assert count == 0

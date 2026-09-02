# Unit/Integration тесты Stage 4: AI Scoring.

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import AppConfig
from database.database import Database
from models.ai import (
    AIScore,
    AIRecommendation,
    CLIPScore,
    ConfidenceLevel,
    LLMScore,
    ProfileStatus,
)
from models.profile import Profile
from services.ai_scoring_service import AIScoringService
from services.clip_service import BaseCLIPService, CLIPService
from services.llm_service import BaseLLMService, LLMService


# ── Fixtures ──────────────────────────────────────────────────────────

def make_config(
    ai_enabled: bool = True,
    clip_enabled: bool = True,
    llm_enabled: bool = True,
    clip_weight: float = 0.5,
    llm_weight: float = 0.5,
    like_threshold: float = 0.75,
    dislike_threshold: float = 0.35,
) -> AppConfig:
    """Создаёт конфиг для тестов."""
    return AppConfig(**{
        "telegram": {"api_id": 123, "api_hash": "abc"},
        "filters": {
            "age": {"min": 18, "max": 19},
            "city": {"allowed": ["Санкт-Петербург"]},
        },
        "ai": {
            "enabled": ai_enabled,
            "clip": {"enabled": clip_enabled, "model": "test-clip"},
            "llm": {
                "enabled": llm_enabled,
                "model": "test-llm",
                "api_key": "test-key",
            },
            "scoring": {
                "clip_weight": clip_weight,
                "llm_weight": llm_weight,
                "like_threshold": like_threshold,
                "dislike_threshold": dislike_threshold,
            },
        },
    })


def make_profile(
    name: str = "Anna",
    age: int = 19,
    normalized_city: str = "Санкт-Петербург",
    description: str = "Любу природу",
    profile_id: int = 1,
) -> Profile:
    """Создаёт профиль для тестов."""
    return Profile(
        id=profile_id,
        name=name,
        age=age,
        normalized_city=normalized_city,
        description=description,
    )


async def insert_test_profile(
    db: Database,
    name: str = "Anna",
    age: int = 19,
    normalized_city: str = "Санкт-Петербург",
    profile_id: int = 1,
) -> int:
    """Вставляет тестовый профиль в БД для FK-связей."""
    return await db.insert_profile(
        name=name,
        age=age,
        raw_city="",
        normalized_city=normalized_city,
        description="",
        fingerprint=f"fp_test_{profile_id}",
        source_chat_id=1234060895,
        source_message_id=profile_id,
        first_seen_at="2025-01-01T00:00:00Z",
        last_seen_at="2025-01-01T00:00:00Z",
        status="NEW",
    )


def make_event(
    text: str = "",
    chat_id: int = 1234060895,
    msg_id: int = 1,
    sender_id: int = 100,
    media_type: object | None = None,
) -> MagicMock:
    """Создаёт мок Telegram-события."""
    from datetime import datetime, timezone

    event = MagicMock()
    event.chat_id = chat_id

    msg = MagicMock()
    msg.id = msg_id
    msg.text = text
    msg.date = datetime.now(timezone.utc)
    msg.entities = None
    msg.reply_to = None
    msg.reply_to_msg_id = None
    msg.media = media_type

    sender = MagicMock()
    sender.id = sender_id
    sender.username = "testuser"
    sender.first_name = "Test"
    sender.last_name = "User"

    event.message = msg
    event.__aiter__ = None

    async def get_sender():
        return sender

    msg.get_sender = get_sender
    return event


@pytest.fixture
def tmp_db(tmp_path: Path) -> Database:
    db = Database(path=tmp_path / "test_ai.db")
    loop = asyncio.get_event_loop()
    loop.run_until_complete(db.connect())
    yield db  # type: ignore[misc]
    loop.run_until_complete(db.close())


def make_clip_service(enabled: bool = True) -> CLIPService:
    """Создаёт CLIP-сервис для тестов."""
    config = AppConfig(**{
        "telegram": {"api_id": 123, "api_hash": "abc"},
        "ai": {
            "clip": {"enabled": enabled, "model": "test-clip"},
        },
    })
    return CLIPService(config.ai.clip)


def make_llm_service(enabled: bool = True) -> LLMService:
    """Создаёт LLM-сервис для тестов."""
    config = AppConfig(**{
        "telegram": {"api_id": 123, "api_hash": "abc"},
        "ai": {
            "llm": {
                "enabled": enabled,
                "model": "test-llm",
                "api_key": "test-key",
            },
        },
    })
    return LLMService(config.ai.llm)


# ═════════════════════════════════════════════════════════════════════
# 1. CLIPScore модель
# ═════════════════════════════════════════════════════════════════════

class TestCLIPScore:
    def test_defaults(self) -> None:
        score = CLIPScore()
        assert score.image_count == 0
        assert score.aesthetic_score == 0.0
        assert score.nsfw_score == 0.0

    def test_valid_scores(self) -> None:
        score = CLIPScore(aesthetic_score=0.8, nsfw_score=0.1)
        assert score.aesthetic_score == 0.8

    def test_score_out_of_range_high(self) -> None:
        with pytest.raises(Exception):
            CLIPScore(aesthetic_score=1.5)

    def test_score_out_of_range_low(self) -> None:
        with pytest.raises(Exception):
            CLIPScore(nsfw_score=-0.1)


# ═════════════════════════════════════════════════════════════════════
# 2. LLMScore модель
# ═════════════════════════════════════════════════════════════════════

class TestLLMScore:
    def test_defaults(self) -> None:
        score = LLMScore()
        assert score.score == 0.0
        assert score.recommendation == AIRecommendation.REVIEW

    def test_valid_like(self) -> None:
        score = LLMScore(score=0.9, recommendation=AIRecommendation.LIKE)
        assert score.recommendation == AIRecommendation.LIKE

    def test_score_out_of_range(self) -> None:
        with pytest.raises(Exception):
            LLMScore(score=2.0)

    def test_confidence_out_of_range(self) -> None:
        with pytest.raises(Exception):
            LLMScore(confidence=1.5)


# ═════════════════════════════════════════════════════════════════════
# 3. AIScore модель
# ═════════════════════════════════════════════════════════════════════

class TestAIScore:
    def test_defaults(self) -> None:
        score = AIScore()
        assert score.combined_score == 0.0
        assert score.recommendation == AIRecommendation.REVIEW
        assert score.confidence == ConfidenceLevel.LOW

    def test_reasons_json(self) -> None:
        score = AIScore(reasons=["photo ok", "good desc"])
        j = score.reasons_json()
        data = json.loads(j)
        assert data == ["photo ok", "good desc"]

    def test_full_score(self) -> None:
        score = AIScore(
            profile_id=42,
            clip_score=0.7,
            llm_score=0.8,
            combined_score=0.75,
            recommendation=AIRecommendation.LIKE,
            confidence=ConfidenceLevel.HIGH,
            confidence_score=0.85,
            reasons=["test"],
            model_version="clip=1+llm=2",
        )
        assert score.profile_id == 42
        assert score.combined_score == 0.75


# ═════════════════════════════════════════════════════════════════════
# 4. CLIPService: enabled / disabled
# ═════════════════════════════════════════════════════════════════════

class TestCLIPService:
    def test_enabled(self) -> None:
        svc = make_clip_service(enabled=True)
        assert svc.is_enabled is True

    def test_disabled(self) -> None:
        svc = make_clip_service(enabled=False)
        assert svc.is_enabled is False

    def test_disabled_returns_zero(self) -> None:
        svc = make_clip_service(enabled=False)
        result = asyncio.get_event_loop().run_until_complete(
            svc.score_images([b"fake"])
        )
        assert result.aesthetic_score == 0.0
        assert result.model_version == "disabled"

    def test_empty_images(self) -> None:
        svc = make_clip_service(enabled=True)
        result = asyncio.get_event_loop().run_until_complete(
            svc.score_images([])
        )
        assert result.image_count == 0
        assert result.aesthetic_score == 0.0

    def test_stub_analysis(self) -> None:
        svc = make_clip_service(enabled=True)
        result = asyncio.get_event_loop().run_until_complete(
            svc.score_images([b"img1", b"img2"])
        )
        assert result.image_count == 2
        assert result.aesthetic_score == 0.5


# ═════════════════════════════════════════════════════════════════════
# 5. LLMService: enabled / disabled
# ═════════════════════════════════════════════════════════════════════

class TestLLMService:
    def test_enabled(self) -> None:
        svc = make_llm_service(enabled=True)
        assert svc.is_enabled is True

    def test_disabled(self) -> None:
        svc = make_llm_service(enabled=False)
        assert svc.is_enabled is False

    def test_disabled_returns_review(self) -> None:
        svc = make_llm_service(enabled=False)
        result = asyncio.get_event_loop().run_until_complete(
            svc.evaluate_profile("Anna", 19, "СПб", "desc")
        )
        assert result.recommendation == AIRecommendation.REVIEW
        assert result.model_version == "disabled"

    def test_stub_returns_review(self) -> None:
        svc = make_llm_service(enabled=True)
        result = asyncio.get_event_loop().run_until_complete(
            svc.evaluate_profile("Anna", 19, "СПб", "desc")
        )
        assert result.recommendation == AIRecommendation.REVIEW
        # Стаб без признаков → детерминированный нейтральный скор (0.6).
        assert result.score == 0.6
        assert result.status == ProfileStatus.INSUFFICIENT_DATA

    def test_build_prompt(self) -> None:
        svc = make_llm_service(enabled=True)
        prompt = svc._build_prompt("Anna", 19, "СПб", "desc")
        assert "Anna" in prompt
        assert "19" in prompt

    def test_parse_valid_json_features(self) -> None:
        # Новый контракт: сервер возвращает признаки, score вычисляется
        # детерминированно (positive factor → 0.9).
        svc = make_llm_service(enabled=True)
        raw = (
            '{"confidence": 0.9, '
            '"hard_negatives": [], '
            '"positive_factors": [{"criterion": "P3:gaming", "evidence": "играю в игры"}], '
            '"unknown": []}'
        )
        result = svc._parse_response(raw)
        assert result.score == 0.9
        assert result.status == ProfileStatus.SUFFICIENT_DATA
        assert any("like:" in r for r in result.reasons)

    def test_parse_invalid_json(self) -> None:
        svc = make_llm_service(enabled=True)
        result = svc._parse_response("not json")
        assert result.recommendation == AIRecommendation.REVIEW
        assert "Невалидный JSON" in result.reasons[0]


# ═════════════════════════════════════════════════════════════════════
# 6. AIScoringService: combined scoring
# ═════════════════════════════════════════════════════════════════════

class TestAIScoringCombined:
    def test_both_enabled(self, tmp_db: Database) -> None:
        config = make_config(clip_weight=0.5, llm_weight=0.5)
        clip = make_clip_service(enabled=True)
        llm = make_llm_service(enabled=True)
        svc = AIScoringService(tmp_db, config, clip, llm)

        loop = asyncio.get_event_loop()
        prof_id = loop.run_until_complete(insert_test_profile(tmp_db, profile_id=1))
        profile = make_profile(profile_id=prof_id)
        result = loop.run_until_complete(svc.evaluate(profile, image_data_list=[b"img"]))
        assert isinstance(result, AIScore)
        assert result.clip_score == 0.5
        # Стаб LLM без признаков → нейтральный скор 0.6.
        assert result.llm_score == 0.6
        assert abs(result.combined_score - 0.55) < 0.01

    def test_only_clip(self, tmp_db: Database) -> None:
        config = make_config(clip_enabled=True, llm_enabled=False)
        clip = make_clip_service(enabled=True)
        llm = make_llm_service(enabled=False)
        svc = AIScoringService(tmp_db, config, clip, llm)

        loop = asyncio.get_event_loop()
        prof_id = loop.run_until_complete(insert_test_profile(tmp_db, profile_id=2))
        profile = make_profile(profile_id=prof_id)
        result = loop.run_until_complete(svc.evaluate(profile, image_data_list=[b"img"]))
        assert result.clip_score == 0.5
        assert result.llm_score is None
        assert result.combined_score == 0.5

    def test_only_llm(self, tmp_db: Database) -> None:
        config = make_config(clip_enabled=False, llm_enabled=True)
        clip = make_clip_service(enabled=False)
        llm = make_llm_service(enabled=True)
        svc = AIScoringService(tmp_db, config, clip, llm)

        loop = asyncio.get_event_loop()
        prof_id = loop.run_until_complete(insert_test_profile(tmp_db, profile_id=3))
        profile = make_profile(profile_id=prof_id)
        result = loop.run_until_complete(svc.evaluate(profile))
        assert result.clip_score is None
        assert result.llm_score == 0.6
        assert result.combined_score == 0.6

    def test_both_disabled(self, tmp_db: Database) -> None:
        config = make_config(clip_enabled=False, llm_enabled=False)
        clip = make_clip_service(enabled=False)
        llm = make_llm_service(enabled=False)
        svc = AIScoringService(tmp_db, config, clip, llm)

        assert svc.is_enabled is False

    def test_weight_normalization(self, tmp_db: Database) -> None:
        config = make_config(clip_weight=0.8, llm_weight=0.2)
        clip = make_clip_service(enabled=True)
        llm = make_llm_service(enabled=True)
        svc = AIScoringService(tmp_db, config, clip, llm)

        loop = asyncio.get_event_loop()
        prof_id = loop.run_until_complete(insert_test_profile(tmp_db, profile_id=4))
        profile = make_profile(profile_id=prof_id)
        result = loop.run_until_complete(svc.evaluate(profile, image_data_list=[b"img"]))
        # clip=0.5*0.8 + llm(нейтральный 0.6)*0.2 = 0.4+0.12 = 0.52
        assert abs(result.combined_score - 0.52) < 0.01
# ═════════════════════════════════════════════════════════════════════
# 7. Recommendation thresholds
# ═════════════════════════════════════════════════════════════════════

class TestRecommendationThresholds:
    def _make_svc(self, like: float, dislike: float) -> tuple[AIScoringService, Database]:
        config = make_config(clip_enabled=False, llm_enabled=True, like_threshold=like, dislike_threshold=dislike)
        clip = make_clip_service(enabled=False)
        llm = make_llm_service(enabled=False)
        db = Database(path=Path("data/test_thresh.db"))
        loop = asyncio.get_event_loop()
        loop.run_until_complete(db.connect())
        svc = AIScoringService(db, config, clip, llm)
        return svc, db

    def test_recommendation_no_hard_negative_is_never_dislike(self, tmp_db: Database) -> None:
        config = make_config(clip_enabled=False, llm_enabled=False)
        clip = make_clip_service(enabled=False)
        llm = make_llm_service(enabled=False)
        svc = AIScoringService(tmp_db, config, clip, llm)

        # Инвариант: без подтверждённого негатива DISLIKE невозможен,
        # даже при combined=0.0 (низкий скор → REVIEW).
        assert svc._determine_recommendation(0.0) == AIRecommendation.REVIEW
        assert svc._determine_recommendation(1.0) == AIRecommendation.LIKE
        assert svc._determine_recommendation(0.5) == AIRecommendation.REVIEW

    def test_recommendation_dislike_only_with_hard_negative(self, tmp_db: Database) -> None:
        from models.ai import HardNegative

        config = make_config(clip_enabled=False, llm_enabled=False)
        clip = make_clip_service(enabled=False)
        llm = make_llm_service(enabled=False)
        svc = AIScoringService(tmp_db, config, clip, llm)

        hn = [HardNegative(criterion="H1:not_looking", evidence="ищу друга")]
        assert svc._determine_recommendation(0.9, hard_negatives=hn) == AIRecommendation.DISLIKE


# ═════════════════════════════════════════════════════════════════════
# 8. Confidence levels
# ═════════════════════════════════════════════════════════════════════

class TestConfidenceLevels:
    def test_both_sources_high(self, tmp_db: Database) -> None:
        config = make_config()
        clip = make_clip_service(enabled=True)
        llm = make_llm_service(enabled=True)
        svc = AIScoringService(tmp_db, config, clip, llm)

        clip_result = CLIPScore(image_count=1, aesthetic_score=0.5)
        llm_result = LLMScore(score=0.5, confidence=0.7)
        level, score = svc._determine_confidence(clip_result, llm_result)
        assert level == ConfidenceLevel.HIGH

    def test_llm_only_medium(self, tmp_db: Database) -> None:
        config = make_config()
        clip = make_clip_service(enabled=True)
        llm = make_llm_service(enabled=True)
        svc = AIScoringService(tmp_db, config, clip, llm)

        llm_result = LLMScore(score=0.5, confidence=0.7)
        level, score = svc._determine_confidence(None, llm_result)
        assert level == ConfidenceLevel.MEDIUM

    def test_clip_only_medium(self, tmp_db: Database) -> None:
        config = make_config()
        clip = make_clip_service(enabled=True)
        llm = make_llm_service(enabled=True)
        svc = AIScoringService(tmp_db, config, clip, llm)

        clip_result = CLIPScore(image_count=1, aesthetic_score=0.5)
        level, score = svc._determine_confidence(clip_result, None)
        assert level == ConfidenceLevel.MEDIUM

    def test_nothing_low(self, tmp_db: Database) -> None:
        config = make_config()
        clip = make_clip_service(enabled=True)
        llm = make_llm_service(enabled=True)
        svc = AIScoringService(tmp_db, config, clip, llm)

        level, score = svc._determine_confidence(None, None)
        assert level == ConfidenceLevel.LOW
        assert score == 0.0


# ═════════════════════════════════════════════════════════════════════
# 9. Reasons collection
# ═════════════════════════════════════════════════════════════════════

class TestReasonsCollection:
    def test_no_data(self, tmp_db: Database) -> None:
        config = make_config()
        clip = make_clip_service(enabled=True)
        llm = make_llm_service(enabled=True)
        svc = AIScoringService(tmp_db, config, clip, llm)

        reasons = svc._collect_reasons(None, None)
        assert "Нет данных для анализа" in reasons

    def test_clip_high_quality(self, tmp_db: Database) -> None:
        config = make_config()
        clip = make_clip_service(enabled=True)
        llm = make_llm_service(enabled=True)
        svc = AIScoringService(tmp_db, config, clip, llm)

        clip_result = CLIPScore(image_count=3, aesthetic_score=0.8)
        reasons = svc._collect_reasons(clip_result, None)
        assert any("3 фото" in r for r in reasons)
        assert any("выше среднего" in r for r in reasons)

    def test_clip_low_quality(self, tmp_db: Database) -> None:
        config = make_config()
        clip = make_clip_service(enabled=True)
        llm = make_llm_service(enabled=True)
        svc = AIScoringService(tmp_db, config, clip, llm)

        clip_result = CLIPScore(image_count=1, aesthetic_score=0.1)
        reasons = svc._collect_reasons(clip_result, None)
        assert any("низкого качества" in r for r in reasons)

    def test_llm_reasons_included(self, tmp_db: Database) -> None:
        config = make_config()
        clip = make_clip_service(enabled=True)
        llm = make_llm_service(enabled=True)
        svc = AIScoringService(tmp_db, config, clip, llm)

        llm_result = LLMScore(reasons=["LLM причина 1", "LLM причина 2"])
        reasons = svc._collect_reasons(None, llm_result)
        assert "LLM причина 1" in reasons
        assert "LLM причина 2" in reasons


# ═════════════════════════════════════════════════════════════════════
# 10. Database CRUD: save + get
# ═════════════════════════════════════════════════════════════════════

class TestAIScoreDB:
    def test_save_and_get_latest(self, tmp_db: Database) -> None:
        loop = asyncio.get_event_loop()
        prof_id = loop.run_until_complete(insert_test_profile(tmp_db, profile_id=1))
        row_id = loop.run_until_complete(
            tmp_db.save_ai_score(
                profile_id=prof_id,
                clip_score=0.5,
                llm_score=0.6,
                combined_score=0.55,
                recommendation="LIKE",
                confidence="HIGH",
                confidence_score=0.8,
                reasons='["ok"]',
                model_version="clip=1+llm=2",
                created_at="2025-01-01T00:00:00Z",
            )
        )
        assert row_id is not None

        latest = loop.run_until_complete(tmp_db.get_latest_ai_score(prof_id))
        assert latest is not None
        assert latest["recommendation"] == "LIKE"
        assert latest["combined_score"] == 0.55

    def test_get_latest_none(self, tmp_db: Database) -> None:
        loop = asyncio.get_event_loop()
        latest = loop.run_until_complete(tmp_db.get_latest_ai_score(999))
        assert latest is None

    def test_get_history(self, tmp_db: Database) -> None:
        loop = asyncio.get_event_loop()
        prof_id = loop.run_until_complete(insert_test_profile(tmp_db, profile_id=2))
        loop.run_until_complete(
            tmp_db.save_ai_score(
                profile_id=prof_id, clip_score=None, llm_score=0.5,
                combined_score=0.5, recommendation="REVIEW",
                confidence="MEDIUM", confidence_score=0.5,
                reasons='[]', model_version="llm=1",
                created_at="2025-01-01T00:00:00Z",
            )
        )
        loop.run_until_complete(
            tmp_db.save_ai_score(
                profile_id=prof_id, clip_score=None, llm_score=0.8,
                combined_score=0.8, recommendation="LIKE",
                confidence="MEDIUM", confidence_score=0.6,
                reasons='["better"]', model_version="llm=2",
                created_at="2025-01-02T00:00:00Z",
            )
        )
        history = loop.run_until_complete(tmp_db.get_ai_score_history(prof_id))
        assert len(history) == 2
        assert history[0]["recommendation"] == "LIKE"  # latest first


# ═════════════════════════════════════════════════════════════════════
# 11. AIScoringService: DB integration
# ═════════════════════════════════════════════════════════════════════

class TestAIScoringDBIntegration:
    def test_evaluate_saves_to_db(self, tmp_db: Database) -> None:
        config = make_config(clip_enabled=False, llm_enabled=True)
        clip = make_clip_service(enabled=False)
        llm = make_llm_service(enabled=True)
        svc = AIScoringService(tmp_db, config, clip, llm)

        loop = asyncio.get_event_loop()
        prof_id = loop.run_until_complete(insert_test_profile(tmp_db, profile_id=10))
        profile = make_profile(profile_id=prof_id)
        result = loop.run_until_complete(svc.evaluate(profile))

        latest = loop.run_until_complete(tmp_db.get_latest_ai_score(prof_id))
        assert latest is not None
        assert latest["recommendation"] == result.recommendation.value

    def test_get_latest_via_service(self, tmp_db: Database) -> None:
        config = make_config(clip_enabled=False, llm_enabled=True)
        clip = make_clip_service(enabled=False)
        llm = make_llm_service(enabled=True)
        svc = AIScoringService(tmp_db, config, clip, llm)

        loop = asyncio.get_event_loop()
        prof_id = loop.run_until_complete(insert_test_profile(tmp_db, profile_id=20))
        profile = make_profile(profile_id=prof_id)
        loop.run_until_complete(svc.evaluate(profile))

        latest = loop.run_until_complete(svc.get_latest(prof_id))
        assert latest is not None
        assert latest.profile_id == prof_id

    def test_get_history_via_service(self, tmp_db: Database) -> None:
        config = make_config(clip_enabled=False, llm_enabled=True)
        clip = make_clip_service(enabled=False)
        llm = make_llm_service(enabled=True)
        svc = AIScoringService(tmp_db, config, clip, llm)

        loop = asyncio.get_event_loop()
        prof_id = loop.run_until_complete(insert_test_profile(tmp_db, profile_id=30))
        profile = make_profile(profile_id=prof_id)
        loop.run_until_complete(svc.evaluate(profile))
        loop.run_until_complete(svc.evaluate(profile))

        history = loop.run_until_complete(svc.get_history(prof_id))
        assert len(history) == 2


# ═════════════════════════════════════════════════════════════════════
# 12. DB foreign key cascade
# ═════════════════════════════════════════════════════════════════════

class TestAIScoreCascade:
    def test_cascade_delete(self, tmp_db: Database) -> None:
        loop = asyncio.get_event_loop()
        prof_id = loop.run_until_complete(
            tmp_db.insert_profile(
                name="Del", age=18, raw_city="", normalized_city="SPb",
                description="", fingerprint="fp_del",
                source_chat_id=1, source_message_id=1,
                first_seen_at="now", last_seen_at="now", status="NEW",
            )
        )
        loop.run_until_complete(
            tmp_db.save_ai_score(
                profile_id=prof_id, clip_score=None, llm_score=0.5,
                combined_score=0.5, recommendation="REVIEW",
                confidence="LOW", confidence_score=0.0,
                reasons='[]', model_version="test",
                created_at="now",
            )
        )
        loop.run_until_complete(
            tmp_db.connection.execute("DELETE FROM profiles WHERE id=?", (prof_id,))
        )
        loop.run_until_complete(tmp_db.connection.commit())

        cursor = loop.run_until_complete(
            tmp_db.connection.execute(
                "SELECT COUNT(*) FROM ai_scores WHERE profile_id=?",
                (prof_id,),
            )
        )
        count = loop.run_until_complete(cursor.fetchone())[0]
        assert count == 0


# ═════════════════════════════════════════════════════════════════════
# 13. Collector integration: PASS triggers AI
# ═════════════════════════════════════════════════════════════════════

class TestCollectorAIIntegration:
    def test_pass_triggers_ai(self) -> None:
        """PASS filter result should trigger AI scoring."""
        from collectors.dvinchik_collector import DvinchikCollector
        from collectors.stats import CollectorStats
        from models.filter import FilterDecision, FilterResult
        from models.raw import FilterResult as ParsedFilterResult, ParsedProfile
        from services.filter_service import FilterService

        client = AsyncMock()
        db = AsyncMock()
        db.save_raw_message = AsyncMock(return_value=1)

        config = make_config()
        stats = CollectorStats()

        mock_profile = make_profile()
        mock_filter_result = FilterResult(
            profile_id=1, decision=FilterDecision.PASS,
            reasons=[], rules_checked=3, evaluated_at="now",
        )
        mock_ai_score = AIScore(
            profile_id=1, combined_score=0.6,
            recommendation=AIRecommendation.LIKE,
            confidence=ConfidenceLevel.HIGH,
            confidence_score=0.8,
        )

        mock_ps = AsyncMock()
        mock_ps.upsert_profile = AsyncMock(return_value=mock_profile)

        mock_fs = AsyncMock()
        mock_fs.evaluate = AsyncMock(return_value=mock_filter_result)

        mock_ai = AsyncMock()
        mock_ai.is_enabled = True
        mock_ai.evaluate = AsyncMock(return_value=mock_ai_score)

        collector = DvinchikCollector(
            client, db, config,
            profile_service=mock_ps,
            filter_service=mock_fs,
            ai_scoring_service=mock_ai,
            stats=stats,
        )

        event = make_event(text="wimx, 18, Санкт-Петербург", msg_id=100)
        asyncio.get_event_loop().run_until_complete(
            collector._handle_new_message(event)
        )

        mock_ai.evaluate.assert_called_once()

    def test_reject_skips_ai(self) -> None:
        """REJECT filter result should skip AI scoring."""
        from collectors.dvinchik_collector import DvinchikCollector
        from collectors.stats import CollectorStats
        from models.filter import FilterDecision, FilterResult

        client = AsyncMock()
        db = AsyncMock()
        db.save_raw_message = AsyncMock(return_value=1)

        config = make_config()
        stats = CollectorStats()

        mock_profile = make_profile()
        mock_filter_result = FilterResult(
            profile_id=1, decision=FilterDecision.REJECT,
            reasons=[], rules_checked=3, evaluated_at="now",
        )

        mock_ps = AsyncMock()
        mock_ps.upsert_profile = AsyncMock(return_value=mock_profile)

        mock_fs = AsyncMock()
        mock_fs.evaluate = AsyncMock(return_value=mock_filter_result)

        mock_ai = AsyncMock()
        mock_ai.is_enabled = True
        mock_ai.evaluate = AsyncMock()

        collector = DvinchikCollector(
            client, db, config,
            profile_service=mock_ps,
            filter_service=mock_fs,
            ai_scoring_service=mock_ai,
            stats=stats,
        )

        event = make_event(text="wimx, 18, Москва", msg_id=101)
        asyncio.get_event_loop().run_until_complete(
            collector._handle_new_message(event)
        )

        mock_ai.evaluate.assert_not_called()

    def test_review_skips_ai(self) -> None:
        """REVIEW filter result should skip AI scoring."""
        from collectors.dvinchik_collector import DvinchikCollector
        from collectors.stats import CollectorStats
        from models.filter import FilterDecision, FilterResult

        client = AsyncMock()
        db = AsyncMock()
        db.save_raw_message = AsyncMock(return_value=1)

        config = make_config()
        stats = CollectorStats()

        mock_profile = make_profile()
        mock_filter_result = FilterResult(
            profile_id=1, decision=FilterDecision.REVIEW,
            reasons=[], rules_checked=3, evaluated_at="now",
        )

        mock_ps = AsyncMock()
        mock_ps.upsert_profile = AsyncMock(return_value=mock_profile)

        mock_fs = AsyncMock()
        mock_fs.evaluate = AsyncMock(return_value=mock_filter_result)

        mock_ai = AsyncMock()
        mock_ai.is_enabled = True
        mock_ai.evaluate = AsyncMock()

        collector = DvinchikCollector(
            client, db, config,
            profile_service=mock_ps,
            filter_service=mock_fs,
            ai_scoring_service=mock_ai,
            stats=stats,
        )

        event = make_event(text="wimx, 18, Санкт-Петербург", msg_id=102)
        asyncio.get_event_loop().run_until_complete(
            collector._handle_new_message(event)
        )

        mock_ai.evaluate.assert_not_called()

    def test_no_ai_service_still_works(self) -> None:
        """Collector works without AI service."""
        from collectors.dvinchik_collector import DvinchikCollector
        from collectors.stats import CollectorStats
        from models.filter import FilterDecision, FilterResult

        client = AsyncMock()
        db = AsyncMock()
        db.save_raw_message = AsyncMock(return_value=1)

        config = make_config()
        stats = CollectorStats()

        mock_profile = make_profile()
        mock_filter_result = FilterResult(
            profile_id=1, decision=FilterDecision.PASS,
            reasons=[], rules_checked=3, evaluated_at="now",
        )

        mock_ps = AsyncMock()
        mock_ps.upsert_profile = AsyncMock(return_value=mock_profile)

        mock_fs = AsyncMock()
        mock_fs.evaluate = AsyncMock(return_value=mock_filter_result)

        collector = DvinchikCollector(
            client, db, config,
            profile_service=mock_ps,
            filter_service=mock_fs,
            ai_scoring_service=None,
            stats=stats,
        )

        event = make_event(text="wimx, 18, Санкт-Петербург", msg_id=103)
        asyncio.get_event_loop().run_until_complete(
            collector._handle_new_message(event)
        )

        assert stats.summary["profiles"] == 1

    def test_ai_error_doesnt_break_collector(self) -> None:
        """AI scoring error should not break the collector."""
        from collectors.dvinchik_collector import DvinchikCollector
        from collectors.stats import CollectorStats
        from models.filter import FilterDecision, FilterResult

        client = AsyncMock()
        db = AsyncMock()
        db.save_raw_message = AsyncMock(return_value=1)

        config = make_config()
        stats = CollectorStats()

        mock_profile = make_profile()
        mock_filter_result = FilterResult(
            profile_id=1, decision=FilterDecision.PASS,
            reasons=[], rules_checked=3, evaluated_at="now",
        )

        mock_ps = AsyncMock()
        mock_ps.upsert_profile = AsyncMock(return_value=mock_profile)

        mock_fs = AsyncMock()
        mock_fs.evaluate = AsyncMock(return_value=mock_filter_result)

        mock_ai = AsyncMock()
        mock_ai.is_enabled = True
        mock_ai.evaluate = AsyncMock(side_effect=Exception("AI crashed"))

        collector = DvinchikCollector(
            client, db, config,
            profile_service=mock_ps,
            filter_service=mock_fs,
            ai_scoring_service=mock_ai,
            stats=stats,
        )

        event = make_event(text="wimx, 18, Санкт-Петербург", msg_id=104)
        asyncio.get_event_loop().run_until_complete(
            collector._handle_new_message(event)
        )

        # Collector still processed the message
        assert stats.summary["profiles"] == 1
        # AI error was caught
        mock_ai.evaluate.assert_called_once()


# ═════════════════════════════════════════════════════════════════════
# 14. Model version string
# ═════════════════════════════════════════════════════════════════════

class TestModelVersion:
    def test_both_enabled(self, tmp_db: Database) -> None:
        config = make_config()
        clip = make_clip_service(enabled=True)
        llm = make_llm_service(enabled=True)
        svc = AIScoringService(tmp_db, config, clip, llm)
        version = svc._model_version()
        assert "clip=test-clip" in version
        assert "llm=test-llm" in version

    def test_clip_only(self, tmp_db: Database) -> None:
        config = make_config(llm_enabled=False)
        clip = make_clip_service(enabled=True)
        llm = make_llm_service(enabled=False)
        svc = AIScoringService(tmp_db, config, clip, llm)
        version = svc._model_version()
        assert "clip=" in version
        assert "llm=" not in version

    def test_none_enabled(self, tmp_db: Database) -> None:
        config = make_config(clip_enabled=False, llm_enabled=False)
        clip = make_clip_service(enabled=False)
        llm = make_llm_service(enabled=False)
        svc = AIScoringService(tmp_db, config, clip, llm)
        version = svc._model_version()
        assert version == "none"


# ═════════════════════════════════════════════════════════════════════
# 15. _row_to_score conversion
# ═════════════════════════════════════════════════════════════════════

class TestRowToScore:
    def test_valid_row(self) -> None:
        row = {
            "profile_id": 5,
            "clip_score": 0.6,
            "llm_score": 0.7,
            "combined_score": 0.65,
            "recommendation": "LIKE",
            "confidence": "HIGH",
            "confidence_score": 0.8,
            "reasons": '["photo ok"]',
            "model_version": "clip=1",
            "created_at": "2025-01-01",
        }
        score = AIScoringService._row_to_score(row)
        assert score.profile_id == 5
        assert score.recommendation == AIRecommendation.LIKE
        assert score.confidence == ConfidenceLevel.HIGH
        assert score.reasons == ["photo ok"]

    def test_missing_optional_fields(self) -> None:
        row = {
            "profile_id": 1,
            "recommendation": "REVIEW",
        }
        score = AIScoringService._row_to_score(row)
        assert score.clip_score is None
        assert score.llm_score is None
        assert score.confidence == ConfidenceLevel.LOW


# ═════════════════════════════════════════════════════════════════════
# 16. Config validation: ScoringConfig thresholds order
# ═════════════════════════════════════════════════════════════════════

class TestScoringConfigValidation:
    def test_dislike_less_than_like(self) -> None:
        """dislike_threshold must be strictly less than like_threshold."""
        config = make_config(like_threshold=0.75, dislike_threshold=0.35)
        assert config.ai.scoring.dislike_threshold < config.ai.scoring.like_threshold

    def test_equal_thresholds_raises(self) -> None:
        """Equal thresholds should raise ValueError."""
        with pytest.raises(Exception):
            make_config(like_threshold=0.5, dislike_threshold=0.5)

    def test_dislike_greater_than_like_raises(self) -> None:
        """dislike > like should raise ValueError."""
        with pytest.raises(Exception):
            make_config(like_threshold=0.3, dislike_threshold=0.7)


# ═════════════════════════════════════════════════════════════════════
# 17. Config validation: AIConfig backend
# ═════════════════════════════════════════════════════════════════════

class TestAIConfigBackend:
    def test_local_backend(self) -> None:
        config = AppConfig(**{
            "telegram": {"api_id": 123, "api_hash": "abc"},
            "ai": {"enabled": True, "backend": "local"},
        })
        assert config.ai.backend == "local"

    def test_remote_backend(self) -> None:
        config = AppConfig(**{
            "telegram": {"api_id": 123, "api_hash": "abc"},
            "ai": {
                "enabled": True,
                "backend": "remote",
                "remote": {"base_url": "http://test:8000"},
            },
        })
        assert config.ai.backend == "remote"
        assert config.ai.remote.base_url == "http://test:8000"

    def test_invalid_backend_raises(self) -> None:
        with pytest.raises(Exception):
            AppConfig(**{
                "telegram": {"api_id": 123, "api_hash": "abc"},
                "ai": {"backend": "invalid"},
            })


# ═════════════════════════════════════════════════════════════════════
# 18. Config: ImagesConfig defaults
# ═════════════════════════════════════════════════════════════════════

class TestImagesConfig:
    def test_defaults(self) -> None:
        config = AppConfig(**{
            "telegram": {"api_id": 123, "api_hash": "abc"},
        })
        assert config.ai.images.enabled is True
        assert config.ai.images.max_images == 5
        assert config.ai.images.max_size_mb == 10
        assert config.ai.images.timeout == 30

    def test_custom_values(self) -> None:
        config = AppConfig(**{
            "telegram": {"api_id": 123, "api_hash": "abc"},
            "ai": {
                "images": {
                    "enabled": False,
                    "max_images": 3,
                    "max_size_mb": 5,
                    "timeout": 15,
                },
            },
        })
        assert config.ai.images.enabled is False
        assert config.ai.images.max_images == 3


# ═════════════════════════════════════════════════════════════════════
# 19. Config: RemoteAIConfig defaults
# ═════════════════════════════════════════════════════════════════════

class TestRemoteAIConfig:
    def test_defaults(self) -> None:
        config = AppConfig(**{
            "telegram": {"api_id": 123, "api_hash": "abc"},
        })
        assert config.ai.remote.base_url == "http://localhost:8000"
        assert config.ai.remote.timeout == 60
        assert config.ai.remote.max_retries == 2

    def test_custom_values(self) -> None:
        config = AppConfig(**{
            "telegram": {"api_id": 123, "api_hash": "abc"},
            "ai": {
                "remote": {
                    "base_url": "http://192.168.1.100:9000",
                    "timeout": 30,
                    "max_retries": 5,
                },
            },
        })
        assert config.ai.remote.base_url == "http://192.168.1.100:9000"
        assert config.ai.remote.timeout == 30
        assert config.ai.remote.max_retries == 5


# ═════════════════════════════════════════════════════════════════════
# 20. RemoteLLMClient: successful response
# ═════════════════════════════════════════════════════════════════════

class TestRemoteLLMClient:
    def _make_client(self, enabled: bool = True) -> tuple:
        from services.remote_llm_client import RemoteLLMClient
        config = AppConfig(**{
            "telegram": {"api_id": 123, "api_hash": "abc"},
            "ai": {
                "llm": {"enabled": enabled, "model": "test-llm"},
                "remote": {"base_url": "http://test:8000", "timeout": 5, "max_retries": 2},
            },
        })
        return RemoteLLMClient(config.ai.llm, config.ai.remote)

    def test_enabled(self) -> None:
        client = self._make_client(enabled=True)
        assert client.is_enabled is True

    def test_disabled(self) -> None:
        client = self._make_client(enabled=False)
        assert client.is_enabled is False

    def test_disabled_returns_review(self) -> None:
        client = self._make_client(enabled=False)
        result = asyncio.get_event_loop().run_until_complete(
            client.evaluate_profile("Anna", 19, "СПб", "desc")
        )
        assert result.recommendation == AIRecommendation.REVIEW
        assert result.model_version == "disabled"

    def test_parse_valid_response(self) -> None:
        client = self._make_client()
        data = {
            "confidence": 0.9,
            "hard_negatives": [],
            "positive_factors": [
                {"criterion": "P3:gaming", "evidence": "играю в игры"},
            ],
            "unknown": [],
        }
        result = client._parse_response(data)
        # Признак positive → детерминированный скор 0.9.
        assert result.score == 0.9
        assert result.confidence == 0.9
        assert result.status == ProfileStatus.SUFFICIENT_DATA
        assert any("like:" in r for r in result.reasons)

    def test_parse_response_no_features_is_neutral(self) -> None:
        client = self._make_client()
        # Серверный score/confidence больше не читаются; пустые признаки →
        # нейтральный скор 0.6 (недостаточность данных ≠ DISLIKE).
        data = {"score": 0.0, "confidence": 0.0}
        result = client._parse_response(data)
        assert result.score == 0.6
        assert result.status == ProfileStatus.INSUFFICIENT_DATA

    def test_parse_response_invalid_reasons(self) -> None:
        client = self._make_client()
        data = {"confidence": 0.5, "reasons": "not a list"}
        result = client._parse_response(data)
        assert isinstance(result.reasons, list)

    def test_parse_response_exception(self) -> None:
        client = self._make_client()
        result = client._parse_response({})
        # Пустой ответ (без негатива) → нейтральный REVIEW, не отказ.
        assert result.score == 0.6
        assert result.recommendation == AIRecommendation.REVIEW

    def test_successful_evaluate(self) -> None:
        """Мокаем HTTP-ответ и проверяем полный цикл."""
        from services.remote_llm_client import RemoteLLMClient

        client = self._make_client()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "confidence": 0.8,
            "hard_negatives": [],
            "positive_factors": [
                {"criterion": "P2:anime", "evidence": "люблю аниме"},
            ],
            "unknown": [],
        }

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_response)
        mock_http.is_closed = False
        client._client = mock_http

        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(
            client.evaluate_profile("Anna", 19, "СПб", "desc")
        )
        assert result.score == 0.9
        assert result.confidence == 0.8

    def test_timeout_retries(self) -> None:
        """Timeout should retry and then fail."""
        import httpx as httpx_mod
        from services.remote_llm_client import RemoteLLMClient

        client = self._make_client()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(
            side_effect=httpx_mod.TimeoutException("timeout")
        )
        mock_http.is_closed = False
        client._client = mock_http

        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(
            client.evaluate_profile("Anna", 19, "СПб", "desc")
        )
        assert result.score == 0.0
        assert "недоступен" in result.reasons[0] or "timeout" in result.reasons[0].lower()
        assert mock_http.post.call_count == 2

    def test_connection_error_retries(self) -> None:
        """Connection error should retry."""
        import httpx as httpx_mod
        from services.remote_llm_client import RemoteLLMClient

        client = self._make_client()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(
            side_effect=httpx_mod.ConnectError("connection refused")
        )
        mock_http.is_closed = False
        client._client = mock_http

        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(
            client.evaluate_profile("Anna", 19, "СПб", "desc")
        )
        assert result.score == 0.0
        assert mock_http.post.call_count == 2

    def test_http_429_retries(self) -> None:
        """HTTP 429 should retry."""
        import httpx as httpx_mod
        from services.remote_llm_client import RemoteLLMClient

        client = self._make_client()

        mock_response = MagicMock()
        mock_response.status_code = 429

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(
            side_effect=httpx_mod.HTTPStatusError(
                "429", request=MagicMock(), response=mock_response
            )
        )
        mock_http.is_closed = False
        client._client = mock_http

        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(
            client.evaluate_profile("Anna", 19, "СПб", "desc")
        )
        assert result.score == 0.0
        assert mock_http.post.call_count == 2

    def test_http_500_retries(self) -> None:
        """HTTP 500 should retry."""
        import httpx as httpx_mod
        from services.remote_llm_client import RemoteLLMClient

        client = self._make_client()

        mock_response = MagicMock()
        mock_response.status_code = 500

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(
            side_effect=httpx_mod.HTTPStatusError(
                "500", request=MagicMock(), response=mock_response
            )
        )
        mock_http.is_closed = False
        client._client = mock_http

        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(
            client.evaluate_profile("Anna", 19, "СПб", "desc")
        )
        assert result.score == 0.0
        assert mock_http.post.call_count == 2

    def test_http_503_retries(self) -> None:
        """HTTP 503 should retry."""
        import httpx as httpx_mod
        from services.remote_llm_client import RemoteLLMClient

        client = self._make_client()

        mock_response = MagicMock()
        mock_response.status_code = 503

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(
            side_effect=httpx_mod.HTTPStatusError(
                "503", request=MagicMock(), response=mock_response
            )
        )
        mock_http.is_closed = False
        client._client = mock_http

        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(
            client.evaluate_profile("Anna", 19, "СПб", "desc")
        )
        assert result.score == 0.0
        assert mock_http.post.call_count == 2

    def test_http_400_no_retry(self) -> None:
        """HTTP 400 should NOT retry."""
        import httpx as httpx_mod
        from services.remote_llm_client import RemoteLLMClient

        client = self._make_client()

        mock_response = MagicMock()
        mock_response.status_code = 400

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(
            side_effect=httpx_mod.HTTPStatusError(
                "400", request=MagicMock(), response=mock_response
            )
        )
        mock_http.is_closed = False
        client._client = mock_http

        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(
            client.evaluate_profile("Anna", 19, "СПб", "desc")
        )
        assert result.score == 0.0
        assert mock_http.post.call_count == 1

    def test_http_404_no_retry(self) -> None:
        """HTTP 404 should NOT retry."""
        import httpx as httpx_mod
        from services.remote_llm_client import RemoteLLMClient

        client = self._make_client()

        mock_response = MagicMock()
        mock_response.status_code = 404

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(
            side_effect=httpx_mod.HTTPStatusError(
                "404", request=MagicMock(), response=mock_response
            )
        )
        mock_http.is_closed = False
        client._client = mock_http

        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(
            client.evaluate_profile("Anna", 19, "СПб", "desc")
        )
        assert result.score == 0.0
        assert mock_http.post.call_count == 1

    def test_close(self) -> None:
        client = self._make_client()
        mock_http = AsyncMock()
        mock_http.is_closed = False
        client._client = mock_http

        loop = asyncio.get_event_loop()
        loop.run_until_complete(client.close())
        mock_http.aclose.assert_called_once()

    def test_http_401_auth_failure_no_retry_llm(self) -> None:
        """Отсутствующий/неверный API-ключ → сервер 401, retry НЕ выполняется."""
        import httpx as httpx_mod
        from services.remote_llm_client import RemoteLLMClient

        client = self._make_client()

        mock_response = MagicMock()
        mock_response.status_code = 401

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(
            side_effect=httpx_mod.HTTPStatusError(
                "401", request=MagicMock(), response=mock_response
            )
        )
        mock_http.is_closed = False
        client._client = mock_http

        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(
            client.evaluate_profile("Anna", 19, "СПб", "desc")
        )
        # 401 не подлежит retry; клиент не падает, возвращает безопасный REVIEW
        assert result.score == 0.0
        assert result.recommendation == AIRecommendation.REVIEW
        assert mock_http.post.call_count == 1

    def test_http_403_auth_failure_no_retry_llm(self) -> None:
        """Неверный API-ключ → сервер 403, retry НЕ выполняется."""
        import httpx as httpx_mod
        from services.remote_llm_client import RemoteLLMClient

        client = self._make_client()

        mock_response = MagicMock()
        mock_response.status_code = 403

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(
            side_effect=httpx_mod.HTTPStatusError(
                "403", request=MagicMock(), response=mock_response
            )
        )
        mock_http.is_closed = False
        client._client = mock_http

        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(
            client.evaluate_profile("Anna", 19, "СПб", "desc")
        )
        assert result.score == 0.0
        assert mock_http.post.call_count == 1

    def test_sends_x_api_key_header_llm(self) -> None:
        """При наличии remote.api_key клиент отправляет заголовок X-API-Key."""
        from services.remote_llm_client import RemoteLLMClient

        config = AppConfig(**{
            "telegram": {"api_id": 123, "api_hash": "abc"},
            "ai": {
                "llm": {"enabled": True, "model": "test-llm"},
                "remote": {
                    "base_url": "http://test:8000", "timeout": 5,
                    "max_retries": 1, "api_key": "super-secret-key",
                },
            },
        })
        client = RemoteLLMClient(config.ai.llm, config.ai.remote)

        # перехватываем конструктор AsyncClient, чтобы увидеть переданные заголовки
        captured: dict = {}

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                captured.update(kwargs.get("headers", {}))

            @property
            def is_closed(self) -> bool:
                return False

        loop = asyncio.get_event_loop()
        with patch("httpx.AsyncClient", FakeAsyncClient):
            loop.run_until_complete(client._ensure_client())
        assert captured.get("X-API-Key") == "super-secret-key"


# ═════════════════════════════════════════════════════════════════════
# 21. RemoteCLIPClient
# ═════════════════════════════════════════════════════════════════════

class TestRemoteCLIPClient:
    def _make_client(self, enabled: bool = True) -> tuple:
        from services.remote_clip_client import RemoteCLIPClient
        config = AppConfig(**{
            "telegram": {"api_id": 123, "api_hash": "abc"},
            "ai": {
                "clip": {"enabled": enabled, "model": "test-clip"},
                "remote": {"base_url": "http://test:8000", "timeout": 5, "max_retries": 2},
            },
        })
        return RemoteCLIPClient(config.ai.clip, config.ai.remote)

    def test_enabled(self) -> None:
        client = self._make_client(enabled=True)
        assert client.is_enabled is True

    def test_disabled(self) -> None:
        client = self._make_client(enabled=False)
        assert client.is_enabled is False

    def test_disabled_returns_zero(self) -> None:
        client = self._make_client(enabled=False)
        result = asyncio.get_event_loop().run_until_complete(
            client.score_images([b"fake"])
        )
        assert result.aesthetic_score == 0.0
        assert result.model_version == "disabled"

    def test_empty_images(self) -> None:
        client = self._make_client()
        result = asyncio.get_event_loop().run_until_complete(
            client.score_images([])
        )
        assert result.image_count == 0
        assert result.aesthetic_score == 0.0

    def test_parse_valid_response(self) -> None:
        client = self._make_client()
        data = {
            "clip_score": 0.8,
            "images_analyzed": 2,
            "images_failed": 0,
            "status": "success",
        }
        result = client._parse_response(data, 2)
        assert result.image_count == 2
        assert result.aesthetic_score == 0.8
        assert result.nsfw_score == 0.0

    def test_parse_response_clamps_scores(self) -> None:
        client = self._make_client()
        data = {"clip_score": 2.0, "nsfw_score": -0.5}
        result = client._parse_response(data, 1)
        assert result.aesthetic_score == 1.0
        assert result.nsfw_score == 0.0

    def test_parse_response_exception(self) -> None:
        client = self._make_client()
        result = client._parse_response({}, 3)
        assert result.image_count == 3
        assert result.aesthetic_score == 0.0

    def test_successful_analyze(self) -> None:
        from services.remote_clip_client import RemoteCLIPClient

        client = self._make_client()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "clip_score": 0.75,
            "images_analyzed": 2,
            "images_failed": 0,
            "status": "success",
        }

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_response)
        mock_http.is_closed = False
        client._client = mock_http

        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(
            client.score_images([b"img1", b"img2"])
        )
        assert result.aesthetic_score == 0.75
        assert result.image_count == 2

        # Контракт сервера: multipart-поле называется 'files' (не 'images')
        _, kwargs = mock_http.post.call_args
        field_names = [n for n, _ in kwargs.get("files", [])]
        assert field_names == ["files", "files"]

    def test_timeout_retries(self) -> None:
        import httpx as httpx_mod
        from services.remote_clip_client import RemoteCLIPClient

        client = self._make_client()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(
            side_effect=httpx_mod.TimeoutException("timeout")
        )
        mock_http.is_closed = False
        client._client = mock_http

        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(
            client.score_images([b"img"])
        )
        assert result.aesthetic_score == 0.0
        assert mock_http.post.call_count == 2

    def test_connection_error_retries(self) -> None:
        import httpx as httpx_mod
        from services.remote_clip_client import RemoteCLIPClient

        client = self._make_client()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(
            side_effect=httpx_mod.ConnectError("refused")
        )
        mock_http.is_closed = False
        client._client = mock_http

        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(
            client.score_images([b"img"])
        )
        assert result.aesthetic_score == 0.0
        assert mock_http.post.call_count == 2

    def test_http_500_retries(self) -> None:
        import httpx as httpx_mod
        from services.remote_clip_client import RemoteCLIPClient

        client = self._make_client()

        mock_response = MagicMock()
        mock_response.status_code = 500

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(
            side_effect=httpx_mod.HTTPStatusError(
                "500", request=MagicMock(), response=mock_response
            )
        )
        mock_http.is_closed = False
        client._client = mock_http

        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(
            client.score_images([b"img"])
        )
        assert result.aesthetic_score == 0.0
        assert mock_http.post.call_count == 2

    def test_http_400_no_retry(self) -> None:
        import httpx as httpx_mod
        from services.remote_clip_client import RemoteCLIPClient

        client = self._make_client()

        mock_response = MagicMock()
        mock_response.status_code = 400

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(
            side_effect=httpx_mod.HTTPStatusError(
                "400", request=MagicMock(), response=mock_response
            )
        )
        mock_http.is_closed = False
        client._client = mock_http

        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(
            client.score_images([b"img"])
        )
        assert result.aesthetic_score == 0.0
        assert mock_http.post.call_count == 1

    def test_close(self) -> None:
        client = self._make_client()
        mock_http = AsyncMock()
        mock_http.is_closed = False
        client._client = mock_http

        loop = asyncio.get_event_loop()
        loop.run_until_complete(client.close())
        mock_http.aclose.assert_called_once()

    def test_http_401_auth_failure_no_retry_clip(self) -> None:
        """Отсутствующий/неверный API-ключ → сервер 401, retry НЕ выполняется."""
        import httpx as httpx_mod
        from services.remote_clip_client import RemoteCLIPClient

        client = self._make_client()

        mock_response = MagicMock()
        mock_response.status_code = 401

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(
            side_effect=httpx_mod.HTTPStatusError(
                "401", request=MagicMock(), response=mock_response
            )
        )
        mock_http.is_closed = False
        client._client = mock_http

        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(client.score_images([b"img"]))
        # 401 не подлежит retry; клиент не падает, возвращает безопасный CLIPScore
        assert result.aesthetic_score == 0.0
        assert mock_http.post.call_count == 1

    def test_http_403_auth_failure_no_retry_clip(self) -> None:
        """Неверный API-ключ → сервер 403, retry НЕ выполняется."""
        import httpx as httpx_mod
        from services.remote_clip_client import RemoteCLIPClient

        client = self._make_client()

        mock_response = MagicMock()
        mock_response.status_code = 403

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(
            side_effect=httpx_mod.HTTPStatusError(
                "403", request=MagicMock(), response=mock_response
            )
        )
        mock_http.is_closed = False
        client._client = mock_http

        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(client.score_images([b"img"]))
        assert result.aesthetic_score == 0.0
        assert mock_http.post.call_count == 1

    def test_sends_x_api_key_header_clip(self) -> None:
        """При наличии remote.api_key CLIP-клиент отправляет X-API-Key."""
        from services.remote_clip_client import RemoteCLIPClient

        config = AppConfig(**{
            "telegram": {"api_id": 123, "api_hash": "abc"},
            "ai": {
                "clip": {"enabled": True, "model": "test-clip"},
                "remote": {
                    "base_url": "http://test:8000", "timeout": 5,
                    "max_retries": 1, "api_key": "super-secret-key",
                },
            },
        })
        client = RemoteCLIPClient(config.ai.clip, config.ai.remote)

        captured: dict = {}

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                captured.update(kwargs.get("headers", {}))

            @property
            def is_closed(self) -> bool:
                return False

        loop = asyncio.get_event_loop()
        with patch("httpx.AsyncClient", FakeAsyncClient):
            loop.run_until_complete(client._ensure_client())
        assert captured.get("X-API-Key") == "super-secret-key"


# ═════════════════════════════════════════════════════════════════════
# 22. Error isolation: CLIP fails but LLM works, and vice versa
# ═════════════════════════════════════════════════════════════════════

class TestErrorIsolation:
    def test_clip_fails_llm_still_works(self, tmp_db: Database) -> None:
        """If CLIP throws, LLM should still produce a score."""
        from services.clip_service import BaseCLIPService

        class BrokenCLIP(BaseCLIPService):
            @property
            def is_enabled(self) -> bool:
                return True

            async def score_images(self, image_data_list: list[bytes]) -> CLIPScore:
                raise RuntimeError("CLIP crashed")

        config = make_config(clip_enabled=True, llm_enabled=True)
        broken_clip = BrokenCLIP()
        llm = make_llm_service(enabled=True)
        svc = AIScoringService(tmp_db, config, broken_clip, llm)

        loop = asyncio.get_event_loop()
        prof_id = loop.run_until_complete(insert_test_profile(tmp_db, profile_id=50))
        profile = make_profile(profile_id=prof_id)
        result = loop.run_until_complete(svc.evaluate(profile))
        assert result.llm_score == 0.6
        assert result.clip_score is None

    def test_llm_fails_clip_still_works(self, tmp_db: Database) -> None:
        """If LLM throws, CLIP should still produce a score."""
        from services.llm_service import BaseLLMService

        class BrokenLLM(BaseLLMService):
            @property
            def is_enabled(self) -> bool:
                return True

            async def evaluate_profile(
                self, name: str, age: int | None, city: str, description: str,
            ) -> LLMScore:
                raise RuntimeError("LLM crashed")

        config = make_config(clip_enabled=True, llm_enabled=True)
        clip = make_clip_service(enabled=True)
        broken_llm = BrokenLLM()
        svc = AIScoringService(tmp_db, config, clip, broken_llm)

        loop = asyncio.get_event_loop()
        prof_id = loop.run_until_complete(insert_test_profile(tmp_db, profile_id=51))
        profile = make_profile(profile_id=prof_id)
        result = loop.run_until_complete(svc.evaluate(profile, image_data_list=[b"img"]))
        assert result.clip_score == 0.5
        assert result.llm_score is None


# ═════════════════════════════════════════════════════════════════════
# 23. Collector image pipeline integration
# ═════════════════════════════════════════════════════════════════════

class TestCollectorImagePipeline:
    def test_pass_downloads_images(self) -> None:
        """PASS should trigger image download before AI scoring."""
        from collectors.dvinchik_collector import DvinchikCollector
        from collectors.stats import CollectorStats
        from models.filter import FilterDecision, FilterResult

        client = AsyncMock()
        db = AsyncMock()
        db.save_raw_message = AsyncMock(return_value=1)

        config = make_config()
        stats = CollectorStats()

        mock_profile = make_profile()
        mock_filter_result = FilterResult(
            profile_id=1, decision=FilterDecision.PASS,
            reasons=[], rules_checked=3, evaluated_at="now",
        )
        mock_ai_score = AIScore(
            profile_id=1, combined_score=0.6,
            recommendation=AIRecommendation.LIKE,
            confidence=ConfidenceLevel.HIGH,
            confidence_score=0.8,
        )

        mock_ps = AsyncMock()
        mock_ps.upsert_profile = AsyncMock(return_value=mock_profile)

        mock_fs = AsyncMock()
        mock_fs.evaluate = AsyncMock(return_value=mock_filter_result)

        mock_ai = AsyncMock()
        mock_ai.is_enabled = True
        mock_ai.evaluate = AsyncMock(return_value=mock_ai_score)

        collector = DvinchikCollector(
            client, db, config,
            profile_service=mock_ps,
            filter_service=mock_fs,
            ai_scoring_service=mock_ai,
            stats=stats,
        )

        event = make_event(text="wimx, 18, Санкт-Петербург", msg_id=200)
        asyncio.get_event_loop().run_until_complete(
            collector._handle_new_message(event)
        )

        mock_ai.evaluate.assert_called_once()
        call_kwargs = mock_ai.evaluate.call_args
        assert "image_data_list" in call_kwargs.kwargs

    def test_reject_skips_image_download(self) -> None:
        """REJECT should NOT trigger image download."""
        from collectors.dvinchik_collector import DvinchikCollector
        from collectors.stats import CollectorStats
        from models.filter import FilterDecision, FilterResult

        client = AsyncMock()
        db = AsyncMock()
        db.save_raw_message = AsyncMock(return_value=1)

        config = make_config()
        stats = CollectorStats()

        mock_profile = make_profile()
        mock_filter_result = FilterResult(
            profile_id=1, decision=FilterDecision.REJECT,
            reasons=[], rules_checked=3, evaluated_at="now",
        )

        mock_ps = AsyncMock()
        mock_ps.upsert_profile = AsyncMock(return_value=mock_profile)

        mock_fs = AsyncMock()
        mock_fs.evaluate = AsyncMock(return_value=mock_filter_result)

        mock_ai = AsyncMock()
        mock_ai.is_enabled = True
        mock_ai.evaluate = AsyncMock()

        collector = DvinchikCollector(
            client, db, config,
            profile_service=mock_ps,
            filter_service=mock_fs,
            ai_scoring_service=mock_ai,
            stats=stats,
        )

        event = make_event(text="wimx, 18, Москва", msg_id=201)
        asyncio.get_event_loop().run_until_complete(
            collector._handle_new_message(event)
        )

        mock_ai.evaluate.assert_not_called()


# ═════════════════════════════════════════════════════════════════════
# 24. AIScoringService with remote clients (mock HTTP)
# ═════════════════════════════════════════════════════════════════════

class TestAIScoringWithRemoteClients:
    def test_remote_llm_produces_score(self, tmp_db: Database) -> None:
        """RemoteLLMClient should work as LLM provider for AIScoringService."""
        from services.remote_llm_client import RemoteLLMClient

        config = make_config(clip_enabled=False, llm_enabled=True)
        remote_config = config.ai.remote
        llm_config = config.ai.llm

        client = RemoteLLMClient(llm_config, remote_config)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "confidence": 0.9,
            "hard_negatives": [],
            "positive_factors": [
                {"criterion": "P2:anime", "evidence": "люблю аниме"},
            ],
            "unknown": [],
        }

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_response)
        mock_http.is_closed = False
        client._client = mock_http

        from services.clip_service import CLIPService
        clip = CLIPService(config.ai.clip)
        clip._config.enabled = False

        svc = AIScoringService(tmp_db, config, clip, client)

        loop = asyncio.get_event_loop()
        prof_id = loop.run_until_complete(insert_test_profile(tmp_db, profile_id=60))
        profile = make_profile(profile_id=prof_id)
        result = loop.run_until_complete(svc.evaluate(profile))

        # Positive factor → детерминированный скор 0.9 → LIKE (порог 0.75).
        assert result.llm_score == 0.9
        assert result.combined_score == 0.9
        assert result.recommendation == AIRecommendation.LIKE

    def test_remote_clip_produces_score(self, tmp_db: Database) -> None:
        """RemoteCLIPClient should work as CLIP provider for AIScoringService."""
        from services.remote_clip_client import RemoteCLIPClient

        config = make_config(clip_enabled=True, llm_enabled=False)
        remote_config = config.ai.remote
        clip_config = config.ai.clip

        client = RemoteCLIPClient(clip_config, remote_config)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "clip_score": 0.9,
            "images_analyzed": 1,
            "images_failed": 0,
            "status": "success",
        }

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_response)
        mock_http.is_closed = False
        client._client = mock_http

        from services.llm_service import LLMService
        llm = LLMService(config.ai.llm)
        llm._config.enabled = False

        svc = AIScoringService(tmp_db, config, client, llm)

        loop = asyncio.get_event_loop()
        prof_id = loop.run_until_complete(insert_test_profile(tmp_db, profile_id=61))
        profile = make_profile(profile_id=prof_id)
        result = loop.run_until_complete(
            svc.evaluate(profile, image_data_list=[b"img"])
        )

        assert result.clip_score == 0.9
        assert result.combined_score == 0.9

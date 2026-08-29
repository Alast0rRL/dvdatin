# Unit/Integration тесты Stage 5: DecisionService (AI Decision Engine).
# Полностью offline — используются MockLLMClient / MockCLIPClient,
# RemoteLLMClient/RemoteCLIPClient с mock-http для тестов недоступности/retry.

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import AppConfig
from database.database import Database
from models.ai import AIRecommendation, CLIPScore, LLMScore
from models.decision import AIDecision
from models.profile import Profile
from services.ai_scoring_service import AIScoringService
from services.clip_service import BaseCLIPService
from services.decision_service import DecisionService
from services.filter_engine import FilterEngine
from services.filter_service import FilterService
from services.llm_service import BaseLLMService
from services.profile_service import ProfileService


# ── Mock AI клиенты (offline) ────────────────────────────────────────

class MockLLMClient(BaseLLMService):
    def __init__(self, enabled=True, score=0.7, confidence=0.8, reasons=None):
        self._enabled = enabled
        self._score = score
        self._confidence = confidence
        self._reasons = reasons or ["mock llm reason"]

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    async def evaluate_profile(self, name, age, city, description) -> LLMScore:
        return LLMScore(
            score=self._score, confidence=self._confidence,
            reasons=self._reasons, model_version="mock-llm",
            prompt_version="llm-v1",
        )


class MockCLIPClient(BaseCLIPService):
    def __init__(self, enabled=True, score=0.6, image_count=1):
        self._enabled = enabled
        self._score = score
        self._image_count = image_count

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    async def score_images(self, image_data_list: list[bytes]) -> CLIPScore:
        return CLIPScore(
            image_count=self._image_count, aesthetic_score=self._score,
            nsfw_score=0.0, model_version="mock-clip",
        )


# ── Fixtures ──────────────────────────────────────────────────────────

def make_config(
    like_threshold: float = 0.75,
    review_threshold: float = 0.50,
    min_confidence: float = 0.60,
    llm_weight: float = 0.70,
    clip_weight: float = 0.30,
    clip_weight_scoring: float = 0.5,
    llm_weight_scoring: float = 0.5,
    scoring_version: str = "v1",
    clip_enabled: bool = True,
    llm_enabled: bool = True,
) -> AppConfig:
    return AppConfig(**{
        "telegram": {"api_id": 123, "api_hash": "abc"},
        "filters": {
            "age": {"min": 18, "max": 19},
            "city": {"allowed": ["Санкт-Петербург"]},
        },
        "ai": {
            "enabled": True,
            "clip": {"enabled": clip_enabled, "model": "test-clip"},
            "llm": {"enabled": llm_enabled, "model": "test-llm", "api_key": "k"},
            "scoring": {
                "clip_weight": clip_weight_scoring,
                "llm_weight": llm_weight_scoring,
            },
            "decision": {
                "like_threshold": like_threshold,
                "review_threshold": review_threshold,
                "min_confidence": min_confidence,
                "scoring_version": scoring_version,
                "weights": {"llm": llm_weight, "clip": clip_weight},
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


def build_stack(db, config, llm, clip) -> DecisionService:
    profile_service = ProfileService(db)
    filter_engine = FilterEngine(config)
    filter_service = FilterService(db, profile_service, filter_engine)
    ai = AIScoringService(db, config, clip, llm)
    return DecisionService(db, config, profile_service, filter_service, ai)


async def insert_profile(
    db: Database,
    profile_id: int,
    city: str = "Санкт-Петербург",
) -> int:
    return await db.insert_profile(
        name="Anna", age=19, raw_city=city, normalized_city=city,
        description="Люблю природу", fingerprint=f"fp_d_{profile_id}",
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
        r = AIDecisionResult_factory(reasons=["LIKE_THRESHOLD", "ok"])
        import json
        assert json.loads(r.reasons_json()) == ["LIKE_THRESHOLD", "ok"]

    def test_decision_field_is_enum(self) -> None:
        """Поле decision должно быть AIDecision, а не строкой.

        Регрессия на 'str' object has no attribute 'value': вызов
        result.decision.value должен работать (decision — enum).
        """
        r = AIDecisionResult_factory(decision="DISLIKE")
        assert r.decision == AIDecision.DISLIKE
        assert isinstance(r.decision, AIDecision)
        assert r.decision.value == "DISLIKE"


def AIDecisionResult_factory(**kw):
    from models.decision import AIDecisionResult
    base = dict(
        profile_id=1, decision="LIKE", combined_score=0.8,
        llm_score=0.9, clip_score=0.7, confidence=0.85,
        reasons=["LIKE_THRESHOLD"], evaluated_at="now",
        scoring_version="v1",
    )
    base.update(kw)
    return AIDecisionResult(**base)


# ── PASS: LIKE / REVIEW / DISLIKE ────────────────────────────────────

class TestDecisionBasic:
    def test_pass_high_score_like(self, tmp_db: Database) -> None:
        config = make_config()
        decision = build_stack(
            tmp_db, config,
            MockLLMClient(score=0.9, confidence=0.9),
            MockCLIPClient(score=0.8, image_count=1),
        )
        prof_id = run(insert_profile(tmp_db, 1))  # PASS (СПб, 19)
        result = run(decision.evaluate(prof_id, [b"img"]))
        assert result.decision == AIDecision.LIKE
        assert "LIKE_THRESHOLD" in result.reasons

    def test_pass_medium_score_review(self, tmp_db: Database) -> None:
        config = make_config()
        decision = build_stack(
            tmp_db, config,
            MockLLMClient(score=0.6, confidence=0.8),
            MockCLIPClient(score=0.5, image_count=1),
        )
        prof_id = run(insert_profile(tmp_db, 2))
        result = run(decision.evaluate(prof_id, [b"img"]))
        # combined = 0.6*0.7 + 0.5*0.3 = 0.57 → REVIEW
        assert result.decision == AIDecision.REVIEW
        assert "REVIEW_THRESHOLD" in result.reasons

    def test_pass_low_score_dislike(self, tmp_db: Database) -> None:
        config = make_config()
        decision = build_stack(
            tmp_db, config,
            MockLLMClient(score=0.2, confidence=0.8),
            MockCLIPClient(score=0.1, image_count=1),
        )
        prof_id = run(insert_profile(tmp_db, 3))
        result = run(decision.evaluate(prof_id, [b"img"]))
        assert result.decision == AIDecision.DISLIKE
        assert "BELOW_THRESHOLDS" in result.reasons

    def test_reuses_passed_filter_result(self, tmp_db: Database) -> None:
        """Переданный filter_result НЕ должен вызывать повторную оценку фильтра."""
        config = make_config()
        decision = build_stack(
            tmp_db, config,
            MockLLMClient(score=0.9, confidence=0.9),
            MockCLIPClient(score=0.8, image_count=1),
        )
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
        assert result.decision == AIDecision.LIKE


# ── HARD FILTERS ─────────────────────────────────────────────────────

class TestDecisionHardFilters:
    def test_reject_high_score_dislike(self, tmp_db: Database) -> None:
        config = make_config()
        decision = build_stack(
            tmp_db, config,
            MockLLMClient(score=0.95, confidence=0.95),
            MockCLIPClient(score=0.9, image_count=1),
        )
        prof_id = run(insert_profile(tmp_db, 4, city="Москва"))  # REJECT
        result = run(decision.evaluate(prof_id, [b"img"]))
        assert result.decision == AIDecision.DISLIKE
        assert "FILTER_REJECTED" in result.reasons

    def test_review_filter_high_score_review(self, tmp_db: Database) -> None:
        config = make_config()
        decision = build_stack(
            tmp_db, config,
            MockLLMClient(score=0.95, confidence=0.95),
            MockCLIPClient(score=0.9, image_count=1),
        )
        prof_id = run(insert_profile(tmp_db, 5, city=""))  # REVIEW (city unknown)
        result = run(decision.evaluate(prof_id, [b"img"]))
        assert result.decision == AIDecision.REVIEW
        assert "FILTER_REVIEW" in result.reasons
        assert result.decision != AIDecision.LIKE


# ── CONFIDENCE ───────────────────────────────────────────────────────

class TestDecisionConfidence:
    def test_low_confidence_review(self, tmp_db: Database) -> None:
        config = make_config(min_confidence=0.60)
        decision = build_stack(
            tmp_db, config,
            MockLLMClient(score=0.9, confidence=0.3),
            MockCLIPClient(score=0.8, image_count=1),
        )
        prof_id = run(insert_profile(tmp_db, 6))
        result = run(decision.evaluate(prof_id, [b"img"]))
        # confidence = (0.8 + 0.3)/2 = 0.55 < 0.60 → REVIEW
        assert result.decision == AIDecision.REVIEW
        assert "LOW_CONFIDENCE" in result.reasons


# ── SIGNALS: LLM/CLIP комбинации ─────────────────────────────────────

class TestDecisionSignals:
    def test_llm_only(self, tmp_db: Database) -> None:
        config = make_config(clip_enabled=False)
        decision = build_stack(
            tmp_db, config,
            MockLLMClient(score=0.9, confidence=0.9),
            MockCLIPClient(enabled=False),
        )
        prof_id = run(insert_profile(tmp_db, 7))
        result = run(decision.evaluate(prof_id))
        assert result.clip_score is None
        assert result.llm_score == 0.9
        assert result.combined_score == 0.9
        assert result.decision == AIDecision.LIKE

    def test_clip_only_low_confidence_review(self, tmp_db: Database) -> None:
        config = make_config(llm_enabled=False)
        decision = build_stack(
            tmp_db, config,
            MockLLMClient(enabled=False),
            MockCLIPClient(score=0.86, image_count=1),
        )
        prof_id = run(insert_profile(tmp_db, 8))
        result = run(decision.evaluate(prof_id, [b"img"]))
        assert result.llm_score is None
        assert result.clip_score == 0.86
        # clip-only confidence = 0.5 < min_confidence 0.6 → REVIEW
        assert result.decision == AIDecision.REVIEW
        assert "LOW_CONFIDENCE" in result.reasons

    def test_llm_and_clip(self, tmp_db: Database) -> None:
        config = make_config()
        decision = build_stack(
            tmp_db, config,
            MockLLMClient(score=0.9, confidence=0.9),
            MockCLIPClient(score=0.8, image_count=1),
        )
        prof_id = run(insert_profile(tmp_db, 9))
        result = run(decision.evaluate(prof_id, [b"img"]))
        assert result.llm_score == 0.9
        assert result.clip_score == 0.8
        assert abs(result.combined_score - (0.9*0.7 + 0.8*0.3)) < 0.01

    def test_no_images_not_zero(self, tmp_db: Database) -> None:
        config = make_config()
        decision = build_stack(
            tmp_db, config,
            MockLLMClient(score=0.8, confidence=0.9),
            MockCLIPClient(score=0.0, image_count=0),  # нет изображений
        )
        prof_id = run(insert_profile(tmp_db, 10))
        result = run(decision.evaluate(prof_id))  # без image_data_list
        # отсутствующие изображения → clip_score = None, combined = llm = 0.8
        assert result.clip_score is None
        assert result.combined_score == 0.8
        assert result.decision == AIDecision.LIKE

    def test_ai_unavailable_review(self, tmp_db: Database) -> None:
        config = make_config(clip_enabled=False, llm_enabled=False)
        decision = build_stack(
            tmp_db, config,
            MockLLMClient(enabled=False),
            MockCLIPClient(enabled=False),
        )
        prof_id = run(insert_profile(tmp_db, 11))
        result = run(decision.evaluate(prof_id))
        assert result.decision == AIDecision.REVIEW
        assert "AI_UNAVAILABLE" in result.reasons


# ── CONFIGURABLE thresholds / weights ────────────────────────────────

class TestDecisionConfig:
    def test_combined_score_method(self, tmp_db: Database) -> None:
        config = make_config(llm_weight=0.3, clip_weight=0.7)
        decision = build_stack(
            tmp_db, config,
            MockLLMClient(score=1.0), MockCLIPClient(score=0.0),
        )
        # _combine(1.0, 0.0) с весами 0.3/0.7 → 0.3
        assert abs(decision._combine(1.0, 0.0) - 0.3) < 0.01
        # только CLIP → 0.8
        assert abs(decision._combine(None, 0.8) - 0.8) < 0.01
        # нет сигналов → 0.0
        assert decision._combine(None, None) == 0.0

    def test_configurable_thresholds(self, tmp_db: Database) -> None:
        config = make_config(
            like_threshold=0.50, review_threshold=0.30, min_confidence=0.0,
        )
        decision = build_stack(
            tmp_db, config,
            MockLLMClient(score=0.6, confidence=0.9),
            MockCLIPClient(score=0.0, image_count=0),
        )
        prof_id = run(insert_profile(tmp_db, 12))
        # llm only → combined 0.6 >= like 0.5 → LIKE (с дефолтными порогами был бы REVIEW)
        result = run(decision.evaluate(prof_id))
        assert result.decision == AIDecision.LIKE

    def test_configurable_weights(self, tmp_db: Database) -> None:
        config = make_config(llm_weight=0.0, clip_weight=1.0)
        decision = build_stack(
            tmp_db, config,
            MockLLMClient(score=0.9, confidence=0.9),
            MockCLIPClient(score=0.4, image_count=1),
        )
        # combined = 0.9*0 + 0.4*1.0 = 0.4 → REVIEW
        assert abs(decision._combine(0.9, 0.4) - 0.4) < 0.01


# ── REMOTE GATEWAY FAILURES: timeout / retry ─────────────────────────

class TestDecisionRemoteFailures:
    def _make_remote_llm(self, max_retries=2):
        from services.remote_llm_client import RemoteLLMClient
        config = make_config()
        cfg = AppConfig(**{
            "telegram": {"api_id": 1, "api_hash": "a"},
            "ai": {
                "llm": {"enabled": True, "model": "t"},
                "remote": {"base_url": "http://test:8000", "timeout": 5, "max_retries": max_retries},
            },
        })
        client = RemoteLLMClient(cfg.ai.llm, cfg.ai.remote)
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=__import__("httpx").TimeoutException("t"))
        mock_http.is_closed = False
        client._client = mock_http
        return client, mock_http

    def test_timeout_retries_then_zero(self) -> None:
        client, mock_http = self._make_remote_llm(max_retries=2)
        result = run(client.evaluate_profile("A", 19, "СПб", "d"))
        assert result.score == 0.0
        assert mock_http.post.call_count == 2  # retry limit соблюдён

    def test_retry_limit_not_infinite(self) -> None:
        client, mock_http = self._make_remote_llm(max_retries=3)
        run(client.evaluate_profile("A", 19, "СПб", "d"))
        assert mock_http.post.call_count == 3

    def test_remote_failure_leads_to_ai_unavailable(self, tmp_db: Database) -> None:
        from services.remote_llm_client import RemoteLLMClient
        from services.remote_clip_client import RemoteCLIPClient
        import httpx

        config = make_config()
        cfg = AppConfig(**{
            "telegram": {"api_id": 1, "api_hash": "a"},
            "ai": {
                "clip": {"enabled": True, "model": "c"},
                "llm": {"enabled": True, "model": "l"},
                "remote": {"base_url": "http://test:8000", "timeout": 5, "max_retries": 1},
                "decision": {
                    "like_threshold": 0.75, "review_threshold": 0.5,
                    "min_confidence": 0.6, "weights": {"llm": 0.7, "clip": 0.3},
                },
            },
        })
        llm = RemoteLLMClient(cfg.ai.llm, cfg.ai.remote)
        clip = RemoteCLIPClient(cfg.ai.clip, cfg.ai.remote)
        for c in (llm, clip):
            m = AsyncMock()
            m.post = AsyncMock(side_effect=httpx.TimeoutException("boom"))
            m.is_closed = False
            c._client = m

        decision = build_stack(tmp_db, cfg, llm, clip)
        prof_id = run(insert_profile(tmp_db, 13))
        result = run(decision.evaluate(prof_id, [b"img"]))
        assert result.decision == AIDecision.REVIEW
        assert "AI_UNAVAILABLE" in result.reasons


# ── DATABASE / versions ──────────────────────────────────────────────

class TestDecisionDB:
    def test_decision_history(self, tmp_db: Database) -> None:
        config = make_config()
        decision = build_stack(
            tmp_db, config, MockLLMClient(score=0.9, confidence=0.9),
            MockCLIPClient(score=0.8),
        )
        prof_id = run(insert_profile(tmp_db, 20))
        run(decision.evaluate(prof_id, [b"img"]))
        run(decision.evaluate(prof_id, [b"img"]))
        history = run(decision.get_history(prof_id))
        assert len(history) == 2
        latest = run(decision.get_latest(prof_id))
        assert latest is not None
        assert latest.profile_id == prof_id

    def test_scoring_version(self, tmp_db: Database) -> None:
        config = make_config(scoring_version="v2")
        decision = build_stack(
            tmp_db, config, MockLLMClient(score=0.9, confidence=0.9),
            MockCLIPClient(score=0.8),
        )
        prof_id = run(insert_profile(tmp_db, 21))
        result = run(decision.evaluate(prof_id, [b"img"]))
        assert result.scoring_version == "v2"

    def test_prompt_version_in_llm(self) -> None:
        llm = MockLLMClient(score=0.8, confidence=0.9)
        result = run(llm.evaluate_profile("A", 19, "СПб", "d"))
        assert result.prompt_version == "llm-v1"

    def test_foreign_key_cascade(self, tmp_db: Database) -> None:
        config = make_config()
        decision = build_stack(
            tmp_db, config, MockLLMClient(score=0.9, confidence=0.9),
            MockCLIPClient(score=0.8),
        )
        prof_id = run(insert_profile(tmp_db, 22))
        run(decision.evaluate(prof_id, [b"img"]))
        run(tmp_db.connection.execute(
            "DELETE FROM profiles WHERE id=?", (prof_id,)
        ))
        run(tmp_db.connection.commit())
        cursor = run(tmp_db.connection.execute(
            "SELECT COUNT(*) FROM ai_decisions WHERE profile_id=?", (prof_id,)
        ))
        count = run(cursor.fetchone())[0]
        assert count == 0

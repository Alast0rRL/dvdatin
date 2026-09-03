# Unit/Integration тесты AIScoringService (score_profile/text/images).
# Полностью offline — используются MockLLMClient / MockCLIPClient.

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.config import AppConfig
from database.database import Database
from models.ai import AIRecommendation, CLIPScore, LLMScore
from models.profile import Profile
from services.ai_scoring_service import AIScoringService
from services.clip_service import BaseCLIPService
from services.llm_service import BaseLLMService
from services.remote_llm_client import PROMPT_VERSION


# ── Mock AI клиенты (offline) ────────────────────────────────────────

class MockLLMClient(BaseLLMService):
    """Контролируемый LLM-клиент для тестов."""

    def __init__(
        self,
        enabled: bool = True,
        score: float = 0.7,
        confidence: float = 0.8,
        reasons: list[str] | None = None,
    ) -> None:
        self._enabled = enabled
        self._score = score
        self._confidence = confidence
        self._reasons = reasons or ["mock llm reason"]

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    async def evaluate_profile(self, name, age, city, description) -> LLMScore:
        return LLMScore(
            score=self._score,
            confidence=self._confidence,
            reasons=self._reasons,
            model_version="mock-llm",
            prompt_version=PROMPT_VERSION,
        )


class MockCLIPClient(BaseCLIPService):
    """Контролируемый CLIP-клиент для тестов."""

    def __init__(
        self,
        enabled: bool = True,
        score: float = 0.6,
        image_count: int = 1,
    ) -> None:
        self._enabled = enabled
        self._score = score
        self._image_count = image_count

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    async def score_images(self, image_data_list: list[bytes]) -> CLIPScore:
        return CLIPScore(
            image_count=self._image_count,
            aesthetic_score=self._score,
            nsfw_score=0.0,
            model_version="mock-clip",
        )


# ── Fixtures ──────────────────────────────────────────────────────────

def make_config(
    ai_enabled: bool = True,
    clip_enabled: bool = True,
    llm_enabled: bool = True,
) -> AppConfig:
    """Создаёт конфиг с decision-блоком для тестов."""
    return AppConfig(**{
        "telegram": {"api_id": 123, "api_hash": "abc"},
        "filters": {
            "age": {"min": 18, "max": 19},
            "city": {"allowed": ["Санкт-Петербург"]},
        },
        "ai": {
            "enabled": ai_enabled,
            "clip": {"enabled": clip_enabled, "model": "test-clip"},
            "llm": {"enabled": llm_enabled, "model": "test-llm", "api_key": "k"},
            "scoring": {},
            "decision": {
                "like_threshold": 0.75,
                "review_threshold": 0.50,
                "min_confidence": 0.60,
            },
        },
    })


def make_profile(
    name: str = "Anna",
    age: int = 19,
    normalized_city: str = "Санкт-Петербург",
    description: str = "Люблю природу",
    profile_id: int = 1,
) -> Profile:
    return Profile(
        id=profile_id,
        name=name,
        age=age,
        normalized_city=normalized_city,
        description=description,
    )


@pytest.fixture
def tmp_db(tmp_path: Path) -> Database:
    db = Database(path=tmp_path / "test_ai_scoring.db")
    loop = asyncio.get_event_loop()
    loop.run_until_complete(db.connect())
    yield db  # type: ignore[misc]
    loop.run_until_complete(db.close())


async def insert_test_profile(db: Database, profile_id: int = 1) -> int:
    return await db.insert_profile(
        name="Anna", age=19, raw_city="", normalized_city="Санкт-Петербург",
        description="", fingerprint=f"fp_ai_{profile_id}",
        source_chat_id=1234060895, source_message_id=profile_id,
        first_seen_at="2025-01-01T00:00:00Z",
        last_seen_at="2025-01-01T00:00:00Z", status="NEW",
    )


# ── Tests ────────────────────────────────────────────────────────────

class TestAIScoringServiceStage5:
    def test_score_text_returns_llm_score(self, tmp_db: Database) -> None:
        config = make_config()
        llm = MockLLMClient(score=0.82, confidence=0.9)
        clip = MockCLIPClient()
        svc = AIScoringService(tmp_db, config, clip, llm)

        prof_id = asyncio.get_event_loop().run_until_complete(
            insert_test_profile(tmp_db, 1)
        )
        profile = make_profile(profile_id=prof_id)
        result = asyncio.get_event_loop().run_until_complete(
            svc.score_text(profile)
        )
        assert result is not None
        assert result.score == 0.82
        assert result.prompt_version == PROMPT_VERSION

    def test_score_text_none_when_llm_disabled(self, tmp_db: Database) -> None:
        config = make_config()
        llm = MockLLMClient(enabled=False)
        clip = MockCLIPClient()
        svc = AIScoringService(tmp_db, config, clip, llm)

        prof_id = asyncio.get_event_loop().run_until_complete(
            insert_test_profile(tmp_db, 2)
        )
        result = asyncio.get_event_loop().run_until_complete(
            svc.score_text(make_profile(profile_id=prof_id))
        )
        assert result is None

    def test_score_images_returns_clip_score(self, tmp_db: Database) -> None:
        config = make_config()
        llm = MockLLMClient()
        clip = MockCLIPClient(score=0.71, image_count=2)
        svc = AIScoringService(tmp_db, config, clip, llm)

        prof_id = asyncio.get_event_loop().run_until_complete(
            insert_test_profile(tmp_db, 3)
        )
        result = asyncio.get_event_loop().run_until_complete(
            svc.score_images(make_profile(profile_id=prof_id), [b"img"])
        )
        assert result is not None
        assert result.aesthetic_score == 0.71
        assert result.image_count == 2

    def test_score_images_none_when_clip_disabled(self, tmp_db: Database) -> None:
        config = make_config()
        llm = MockLLMClient()
        clip = MockCLIPClient(enabled=False)
        svc = AIScoringService(tmp_db, config, clip, llm)

        prof_id = asyncio.get_event_loop().run_until_complete(
            insert_test_profile(tmp_db, 4)
        )
        result = asyncio.get_event_loop().run_until_complete(
            svc.score_images(make_profile(profile_id=prof_id), [b"img"])
        )
        assert result is None

    def test_evaluate_disabled_gate(self, tmp_db: Database) -> None:
        """AIScoringService is_enabled отражает наличие хотя бы одного компонента."""
        config = make_config()
        llm = MockLLMClient(score=0.8, confidence=0.9)
        clip = MockCLIPClient(enabled=False)
        svc = AIScoringService(tmp_db, config, clip, llm)
        # Только LLM → сервис включён (score_text возвращает LLM-скор).
        assert svc.is_enabled is True
        prof_id = asyncio.get_event_loop().run_until_complete(
            insert_test_profile(tmp_db, 5)
        )
        result = asyncio.get_event_loop().run_until_complete(
            svc.score_text(make_profile(profile_id=prof_id))
        )
        assert result is not None
        assert result.score == 0.8

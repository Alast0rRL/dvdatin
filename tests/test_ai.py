# Unit/Integration тесты Stage 4: AI Scoring.
# Внимание: после миграции на детерминированный Decision Engine (Stage 8)
# LLM/CLIP-скоринг больше НЕ является частью активного pipeline.
# Оставлены: чистые model-тесты, config-тесты, deprecated remote-клиенты,
# DB-тесты и интеграция Collector → DecisionService.
# Детерминированный скоринг покрыт отдельно в test_deterministic_scoring.py.

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
)
from models.profile import Profile


# ── Fixtures ──────────────────────────────────────────────────────────

def make_config(
    ai_enabled: bool = True,
    clip_enabled: bool = True,
    llm_enabled: bool = True,
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
            "decision": {
                "like_threshold": 0.75,
                "review_threshold": 0.50,
                "min_confidence": 0.60,
                "scoring_version": "deterministic-v2",
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
    msg.sender_id = sender_id

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


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def tmp_db(tmp_path: Path) -> Database:
    db = Database(path=tmp_path / "test_ai.db")
    loop = asyncio.get_event_loop()
    loop.run_until_complete(db.connect())
    yield db  # type: ignore[misc]
    loop.run_until_complete(db.close())


# ═════════════════════════════════════════════════════════════════════
# 1. CLIPScore модель (deprecated, но совместима)
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
# 2. LLMScore модель (deprecated, но совместима)
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
# 3. AIScore модель (deprecated, но совместима)
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
# 4. AIScore DB CRUD (таблица ai_scores, ещё существует в схеме)
# ═════════════════════════════════════════════════════════════════════

class TestAIScoreDB:
    def test_save_and_get_latest(self, tmp_db: Database) -> None:
        prof_id = run(insert_test_profile(tmp_db, profile_id=1))
        row_id = run(
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

        latest = run(tmp_db.get_latest_ai_score(prof_id))
        assert latest is not None
        assert latest["recommendation"] == "LIKE"
        assert latest["combined_score"] == 0.55

    def test_get_latest_none(self, tmp_db: Database) -> None:
        latest = run(tmp_db.get_latest_ai_score(999))
        assert latest is None

    def test_get_history(self, tmp_db: Database) -> None:
        prof_id = run(insert_test_profile(tmp_db, profile_id=2))
        run(
            tmp_db.save_ai_score(
                profile_id=prof_id, clip_score=None, llm_score=0.5,
                combined_score=0.5, recommendation="REVIEW",
                confidence="MEDIUM", confidence_score=0.5,
                reasons='[]', model_version="llm=1",
                created_at="2025-01-01T00:00:00Z",
            )
        )
        run(
            tmp_db.save_ai_score(
                profile_id=prof_id, clip_score=None, llm_score=0.8,
                combined_score=0.8, recommendation="LIKE",
                confidence="MEDIUM", confidence_score=0.6,
                reasons='["better"]', model_version="llm=2",
                created_at="2025-01-02T00:00:00Z",
            )
        )
        history = run(tmp_db.get_ai_score_history(prof_id))
        assert len(history) == 2
        assert history[0]["recommendation"] == "LIKE"  # latest first


# ═════════════════════════════════════════════════════════════════════
# 5. DB FK cascade (ai_scores, ещё существует в схеме)
# ═════════════════════════════════════════════════════════════════════

class TestAIScoreCascade:
    def test_cascade_delete(self, tmp_db: Database) -> None:
        prof_id = run(
            tmp_db.insert_profile(
                name="Del", age=18, raw_city="", normalized_city="SPb",
                description="", fingerprint="fp_del",
                source_chat_id=1, source_message_id=1,
                first_seen_at="now", last_seen_at="now", status="NEW",
            )
        )
        run(
            tmp_db.save_ai_score(
                profile_id=prof_id, clip_score=None, llm_score=0.5,
                combined_score=0.5, recommendation="REVIEW",
                confidence="LOW", confidence_score=0.0,
                reasons='[]', model_version="test",
                created_at="now",
            )
        )
        run(
            tmp_db.connection.execute("DELETE FROM profiles WHERE id=?", (prof_id,))
        )
        run(tmp_db.connection.commit())

        cursor = run(
            tmp_db.connection.execute(
                "SELECT COUNT(*) FROM ai_scores WHERE profile_id=?",
                (prof_id,),
            )
        )
        count = run(cursor.fetchone())[0]
        assert count == 0


# ═════════════════════════════════════════════════════════════════════
# 6. Config: AIConfig backend
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
# 7. Config: ImagesConfig defaults (CLIP deprecated → enabled=False)
# ═════════════════════════════════════════════════════════════════════

class TestImagesConfig:
    def test_defaults(self) -> None:
        config = AppConfig(**{
            "telegram": {"api_id": 123, "api_hash": "abc"},
        })
        # CLIP deprecated → загрузка изображений выключена по умолчанию.
        assert config.ai.images.enabled is False
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
# 8. Config: RemoteAIConfig
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
# 9. RemoteLLMClient (deprecated — больше НЕ используется в pipeline,
#    но модуль ещё существует и покрыт тестами retry/timeout/контрактов)
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
        result = run(client.evaluate_profile("Anna", 19, "СПб", "desc"))
        assert result.recommendation == AIRecommendation.REVIEW
        assert result.model_version == "disabled"

    def test_close(self) -> None:
        client = self._make_client()
        mock_http = AsyncMock()
        mock_http.is_closed = False
        client._client = mock_http

        run(client.close())
        mock_http.aclose.assert_called_once()

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

        captured: dict = {}

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                captured.update(kwargs.get("headers", {}))

            @property
            def is_closed(self) -> bool:
                return False

        with patch("httpx.AsyncClient", FakeAsyncClient):
            run(client._ensure_client())
        assert captured.get("X-API-Key") == "super-secret-key"


# ═════════════════════════════════════════════════════════════════════
# 10. RemoteCLIPClient (deprecated — больше НЕ используется в pipeline,
#     но модуль ещё существует и покрыт тестами retry/timeout/контрактов)
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
        result = run(client.score_images([b"fake"]))
        assert result.aesthetic_score == 0.0
        assert result.model_version == "disabled"

    def test_empty_images(self) -> None:
        client = self._make_client()
        result = run(client.score_images([]))
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

    def test_close(self) -> None:
        client = self._make_client()
        mock_http = AsyncMock()
        mock_http.is_closed = False
        client._client = mock_http

        run(client.close())
        mock_http.aclose.assert_called_once()

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

        with patch("httpx.AsyncClient", FakeAsyncClient):
            run(client._ensure_client())
        assert captured.get("X-API-Key") == "super-secret-key"


# ═════════════════════════════════════════════════════════════════════
# 11. Collector → DecisionService integration
#     Проверяем, что коллектор вызывает DecisionService (НЕ AI-scoring),
#     и что фильтр REJECT/REVIEW пропускает decision, а PASS — вызывает.
# ═════════════════════════════════════════════════════════════════════

def _collector_stack(client, db, config, decision_service, **kw):
    """Собирает DvinchikCollector с профиль/фильтр/decision службами."""
    from collectors.dvinchik_collector import DvinchikCollector
    from collectors.stats import CollectorStats

    mock_ps = AsyncMock()
    mock_ps.upsert_profile = AsyncMock(return_value=make_profile())
    return DvinchikCollector(
        client, db, config,
        profile_service=mock_ps,
        filter_service=decision_service_filter(),
        decision_service=decision_service,
        stats=CollectorStats(),
        **kw,
    )


def decision_service_filter():
    """Возвращает mock filter_service, чей evaluate просто возвращает PASS."""
    from models.filter import FilterDecision, FilterResult

    fr = FilterResult(
        profile_id=1, decision=FilterDecision.PASS,
        reasons=[], rules_checked=3, evaluated_at="now",
    )
    mock_fs = AsyncMock()
    mock_fs.evaluate = AsyncMock(return_value=fr)
    return mock_fs


class TestCollectorDecisionIntegration:
    def test_pass_triggers_decision(self) -> None:
        """PASS фильтр → коллектор вызывает decision_service.evaluate()."""
        from collectors.dvinchik_collector import DvinchikCollector
        from models.decision import AIDecisionResult

        client = AsyncMock()
        db = AsyncMock(spec=Database)
        db.save_raw_message = AsyncMock(return_value=1)
        db.has_auto_action_for_message = AsyncMock(return_value=False)

        config = make_config()
        mock_decision = AsyncMock()
        mock_decision.evaluate = AsyncMock(return_value=AIDecisionResult(
            profile_id=1, decision="REVIEW",
            reasons=["NO_FEATURES_FOUND"], evaluated_at="now",
            scoring_version="deterministic-v2",
        ))

        collector = _collector_stack(client, db, config, mock_decision)

        event = make_event(text="wimx, 18, Санкт-Петербург", msg_id=100)
        run(collector._handle_new_message(event))

        mock_decision.evaluate.assert_called_once()
        call_kwargs = mock_decision.evaluate.call_args
        assert "filter_result" in call_kwargs.kwargs
        assert call_kwargs.kwargs["filter_result"].decision.value == "PASS"

    def test_reject_passes_filter_result_to_decision(self) -> None:
        """REJECT фильтр → decision_service вызывается с этим filter_result."""
        from models.decision import AIDecisionResult
        from models.filter import FilterDecision, FilterResult

        client = AsyncMock()
        db = AsyncMock(spec=Database)
        db.save_raw_message = AsyncMock(return_value=1)
        db.has_auto_action_for_message = AsyncMock(return_value=False)

        config = make_config()

        mock_fs = AsyncMock()
        mock_fs.evaluate = AsyncMock(return_value=FilterResult(
            profile_id=1, decision=FilterDecision.REJECT,
            reasons=[], rules_checked=3, evaluated_at="now",
        ))

        from collectors.dvinchik_collector import DvinchikCollector
        from collectors.stats import CollectorStats

        mock_ps = AsyncMock()
        mock_ps.upsert_profile = AsyncMock(return_value=make_profile())

        mock_decision = AsyncMock()
        mock_decision.evaluate = AsyncMock(return_value=AIDecisionResult(
            profile_id=1, decision="DISLIKE",
            reasons=["FILTER_REJECTED"], evaluated_at="now",
            scoring_version="deterministic-v2",
        ))

        collector = DvinchikCollector(
            client, db, config,
            profile_service=mock_ps,
            filter_service=mock_fs,
            decision_service=mock_decision,
            stats=CollectorStats(),
        )

        event = make_event(text="wimx, 18, Москва", msg_id=101)
        run(collector._handle_new_message(event))

        mock_decision.evaluate.assert_called_once()
        call_kwargs = mock_decision.evaluate.call_args
        assert call_kwargs.kwargs["filter_result"].decision.value == "REJECT"

    def test_review_passes_filter_result_to_decision(self) -> None:
        """REVIEW фильтр → decision_service вызывается с этим filter_result."""
        from models.decision import AIDecisionResult
        from models.filter import FilterDecision, FilterResult

        client = AsyncMock()
        db = AsyncMock(spec=Database)
        db.save_raw_message = AsyncMock(return_value=1)
        db.has_auto_action_for_message = AsyncMock(return_value=False)

        config = make_config()

        mock_fs = AsyncMock()
        mock_fs.evaluate = AsyncMock(return_value=FilterResult(
            profile_id=1, decision=FilterDecision.REVIEW,
            reasons=[], rules_checked=3, evaluated_at="now",
        ))

        from collectors.dvinchik_collector import DvinchikCollector
        from collectors.stats import CollectorStats

        mock_ps = AsyncMock()
        mock_ps.upsert_profile = AsyncMock(return_value=make_profile())

        mock_decision = AsyncMock()
        mock_decision.evaluate = AsyncMock(return_value=AIDecisionResult(
            profile_id=1, decision="REVIEW",
            reasons=["FILTER_REVIEW"], evaluated_at="now",
            scoring_version="deterministic-v2",
        ))

        collector = DvinchikCollector(
            client, db, config,
            profile_service=mock_ps,
            filter_service=mock_fs,
            decision_service=mock_decision,
            stats=CollectorStats(),
        )

        event = make_event(text="wimx, 18, ???", msg_id=102)
        run(collector._handle_new_message(event))

        mock_decision.evaluate.assert_called_once()
        call_kwargs = mock_decision.evaluate.call_args
        assert call_kwargs.kwargs["filter_result"].decision.value == "REVIEW"

    def test_no_decision_service_still_works(self) -> None:
        """Коллектор работает и без decision_service (никак не падает)."""
        from collectors.dvinchik_collector import DvinchikCollector
        from collectors.stats import CollectorStats
        from models.filter import FilterDecision, FilterResult

        client = AsyncMock()
        db = AsyncMock(spec=Database)
        db.save_raw_message = AsyncMock(return_value=1)

        config = make_config()

        mock_fs = AsyncMock()
        mock_fs.evaluate = AsyncMock(return_value=FilterResult(
            profile_id=1, decision=FilterDecision.PASS,
            reasons=[], rules_checked=3, evaluated_at="now",
        ))

        mock_ps = AsyncMock()
        mock_ps.upsert_profile = AsyncMock(return_value=make_profile())

        collector = DvinchikCollector(
            client, db, config,
            profile_service=mock_ps,
            filter_service=mock_fs,
            decision_service=None,
            stats=CollectorStats(),
        )

        event = make_event(text="wimx, 18, Санкт-Петербург", msg_id=103)
        run(collector._handle_new_message(event))

        assert collector._stats.summary["profiles"] == 1

    def test_decision_error_doesnt_break_collector(self) -> None:
        """Ошибка DecisionService не должна ломать коллектор."""
        from collectors.dvinchik_collector import DvinchikCollector

        client = AsyncMock()
        db = AsyncMock(spec=Database)
        db.save_raw_message = AsyncMock(return_value=1)
        db.has_auto_action_for_message = AsyncMock(return_value=False)

        config = make_config()

        mock_decision = AsyncMock()
        mock_decision.evaluate = AsyncMock(side_effect=Exception("decision crashed"))

        collector = _collector_stack(client, db, config, mock_decision)

        event = make_event(text="wimx, 18, Санкт-Петербург", msg_id=104)
        run(collector._handle_new_message(event))

        # Коллектор всё равно обработал сообщение
        assert collector._stats.summary["profiles"] == 1
        # Ошибка была поймана
        mock_decision.evaluate.assert_called_once()


# ═════════════════════════════════════════════════════════════════════
# 12. Regression: NO_UNKNOWN_TO_DISLIKE (детерминированный инвариант).
#     Дублирует ключевой инвариант на уровне DecisionService._decide,
#     гарантируя, что отсутствие hard-negative НИКОГДА не даёт DISLIKE.
# ═════════════════════════════════════════════════════════════════════

class TestNoUnknownToDislikeInvariant:
    def _make_decision(self) -> "DecisionService":
        from services.decision_service import DecisionService

        return DecisionService(
            db=MagicMock(),
            config=make_config(),
            profile_service=MagicMock(),
            filter_service=MagicMock(),
        )

    def test_low_score_never_dislike(self) -> None:
        """Низкий score без hard-negative → REVIEW, не DISLIKE."""
        svc = self._make_decision()
        from models.filter import FilterDecision
        decision, _, reasons = svc._decide(
            filter_decision=FilterDecision.PASS, score=0.0,
            skip_labels=[], like_labels=[],
        )
        assert decision.value == "REVIEW"
        assert "NO_FEATURES_FOUND" in reasons

    def test_no_negative_without_hard_negative(self) -> None:
        """Отсутствие negative-признаков → REVIEW (никогда не DISLIKE)."""
        svc = self._make_decision()
        from models.filter import FilterDecision
        decision, _, _ = svc._decide(
            filter_decision=FilterDecision.PASS, score=0.0,
            skip_labels=[], like_labels=[], hard_negatives=[],
        )
        assert decision.value == "REVIEW"

    def test_score_100_without_negative_like(self) -> None:
        """Максимальный score без hard-negative → LIKE (не DISLIKE)."""
        svc = self._make_decision()
        from models.filter import FilterDecision
        decision, _, _ = svc._decide(
            filter_decision=FilterDecision.PASS, score=1.0,
            skip_labels=[], like_labels=[],
            positive_factors=[make_feature("P01", "spbpu", positive=True)],
        )
        assert decision.value == "LIKE"

    def test_hard_negative_becomes_dislike(self) -> None:
        """Подтверждённый hard-negative → DISLIKE (единственный путь)."""
        svc = self._make_decision()
        from models.filter import FilterDecision
        decision, _, reasons = svc._decide(
            filter_decision=FilterDecision.PASS, score=0.0,
            skip_labels=[], like_labels=[],
            hard_negatives=[make_feature("H01", "not_relationships")],
        )
        assert decision.value == "DISLIKE"
        assert any("HARD_NEGATIVE:" in r for r in reasons)

    def test_unknown_fields_never_cause_dislike(self) -> None:
        """Пустой текст/нет признаков → REVIEW. Проверка не зависима от unknown."""
        svc = self._make_decision()
        from models.filter import FilterDecision
        decision, _, reasons = svc._decide(
            filter_decision=FilterDecision.PASS, score=0.5,
            skip_labels=[], like_labels=[], hard_negatives=[],
            positive_factors=[],
        )
        assert decision.value == "REVIEW"
        assert "NO_FEATURES_FOUND" in reasons


def make_feature(code: str, name: str, positive: bool = False):
    """Создаёт Feature для тестов _decide."""
    from models.features import Feature, FeatureType
    ftype = FeatureType.POSITIVE if positive else FeatureType.HARD_NEGATIVE
    return Feature(code=code, type=ftype, name=name, value=True)

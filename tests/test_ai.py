# Тесты DecisionService: интеграция Collector → DecisionService + инварианты.
# Детерминированный скоринг покрыт отдельно в test_deterministic_scoring.py.

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import AppConfig
from database.database import Database
from models.profile import Profile


def make_config(**overrides) -> AppConfig:
    defaults = {
        "telegram": {"api_id": 123, "api_hash": "abc"},
        "filters": {
            "age": {"min": 18, "max": 19},
            "city": {"allowed": ["Санкт-Петербург"]},
        },
    }
    defaults.update(overrides)
    return AppConfig(**defaults)


def make_profile(
    name: str = "Anna",
    age: int = 19,
    normalized_city: str = "Санкт-Петербург",
    description: str = "Любу природу",
    profile_id: int = 1,
) -> Profile:
    return Profile(
        id=profile_id,
        name=name,
        age=age,
        normalized_city=normalized_city,
        description=description,
    )


def make_event(
    text: str = "",
    chat_id: int = 1234060895,
    msg_id: int = 1,
    sender_id: int = 100,
    media_type: object | None = None,
) -> MagicMock:
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


def _collector_stack(client, db, config, decision_service, **kw):
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
    from models.filter import FilterDecision, FilterResult

    fr = FilterResult(
        profile_id=1, decision=FilterDecision.PASS,
        reasons=[], rules_checked=3, evaluated_at="now",
    )
    mock_fs = AsyncMock()
    mock_fs.evaluate = AsyncMock(return_value=fr)
    return mock_fs


# ═════════════════════════════════════════════════════════════════════
# Collector → DecisionService integration
# ═════════════════════════════════════════════════════════════════════

class TestCollectorDecisionIntegration:
    def test_pass_triggers_decision(self) -> None:
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

        assert collector._stats.summary["profiles"] == 1
        mock_decision.evaluate.assert_called_once()


# ═════════════════════════════════════════════════════════════════════
# Regression: NO_UNKNOWN_TO_DISLIKE invariant
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
        svc = self._make_decision()
        from models.filter import FilterDecision
        decision, _, reasons = svc._decide(
            filter_decision=FilterDecision.PASS, score=0.0,
            skip_labels=[], like_labels=[],
        )
        assert decision.value == "REVIEW"
        assert "NO_FEATURES_FOUND" in reasons

    def test_no_negative_without_hard_negative(self) -> None:
        svc = self._make_decision()
        from models.filter import FilterDecision
        decision, _, _ = svc._decide(
            filter_decision=FilterDecision.PASS, score=0.0,
            skip_labels=[], like_labels=[], hard_negatives=[],
        )
        assert decision.value == "REVIEW"

    def test_score_100_without_negative_like(self) -> None:
        svc = self._make_decision()
        from models.filter import FilterDecision
        decision, _, _ = svc._decide(
            filter_decision=FilterDecision.PASS, score=1.0,
            skip_labels=[], like_labels=[],
            positive_factors=[make_feature("P01", "spbpu", positive=True)],
        )
        assert decision.value == "LIKE"

    def test_hard_negative_becomes_dislike(self) -> None:
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
    from models.features import Feature, FeatureType
    ftype = FeatureType.POSITIVE if positive else FeatureType.HARD_NEGATIVE
    return Feature(code=code, type=ftype, name=name)

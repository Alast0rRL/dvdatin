# Regression-тесты калибровки AI scoring по предпочтениям пользователя.
# Покрывают найденные при анализе случаи:
#   - FP: «ищу друга/подругу» при высоком CLIP → должно быть DISLIKE;
#   - FN: «играю во многие игры» при низких скорах → не должно быть DISLIKE.
#
# Оффлайн (без сети, без Telegram). Предпочтения задаются прямо в тесте.

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.config import AppConfig
from app.preferences import (
    PreferenceRule,
    PreferencesConfig,
    PreferencesEngine,
    ScoringPrefs,
)
from database.database import Database
from models.decision import AIDecision
from models.filter import FilterDecision
from models.profile import Profile
from services.ai_scoring_service import AIScoringService
from services.clip_service import BaseCLIPService
from services.decision_service import DecisionService
from services.filter_engine import FilterEngine
from services.filter_service import FilterService
from services.llm_service import BaseLLMService
from services.profile_service import ProfileService
from models.ai import CLIPScore, LLMScore


def make_prefs() -> PreferencesConfig:
    """Правила, отражающие пользовательские SKIP/LIKE (подмножество)."""
    return PreferencesConfig(
        skip=[
            PreferenceRule(label="не ищет отношений", match=["ищу друга", "ищу подругу", "пообщаться"]),
            PreferenceRule(label="курит", match=["курю", "курит", "сигарет"]),
            PreferenceRule(label="пьёт", match=["пью", "алкогол", "вино"]),
            PreferenceRule(label="instagram", match=["instagram", "инстаграм"]),
        ],
        like=[
            PreferenceRule(label="игры", match=["играю в", "играть в", "игры"]),
            PreferenceRule(label="аниме", match=["аниме"]),
            PreferenceRule(label="переехала в спб", match=["переехала", "недавно в питер"]),
        ],
        scoring=ScoringPrefs(skip_is_hard=True, like_lifts_review=True),
    )


def make_config() -> AppConfig:
    return AppConfig(**{
        "telegram": {"api_id": 123, "api_hash": "abc"},
        "filters": {"age": {"min": 18, "max": 19}, "city": {"allowed": ["Санкт-Петербург"]}},
        "ai": {
            "enabled": True,
            "clip": {"enabled": True},
            "llm": {"enabled": True, "api_key": "k"},
            "decision": {
                "like_threshold": 0.75, "review_threshold": 0.50,
                "min_confidence": 0.60,
                "weights": {"llm": 0.7, "clip": 0.3},
            },
        },
    })


def make_service(prefs: PreferencesEngine | None = None) -> DecisionService:
    config = make_config()
    db = MagicMock()
    return DecisionService(
        db, config,
        profile_service=MagicMock(),
        filter_service=MagicMock(),
        ai_scoring_service=MagicMock(),
        preferences=prefs if prefs is not None else PreferencesEngine(),
    )


class TestPreferencesEngine:
    def test_evaluate_skip_and_like(self) -> None:
        e = PreferencesEngine(make_prefs())
        skip, like = e.evaluate("Ищу друга, люблю аниме и игры")
        assert "не ищет отношений" in skip
        assert "аниме" in like
        assert "игры" in like

    def test_no_text_no_rules(self) -> None:
        e = PreferencesEngine(make_prefs())
        assert e.evaluate("") == ([], [])
        assert e.evaluate(None or "") == ([], [])

    def test_disabled_engine_hits_nothing(self) -> None:
        e = PreferencesEngine(PreferencesConfig())
        assert e.evaluate("ищу друга игры аниме") == ([], [])


class TestDecisionHardRules:
    """Слой правил поверх порогов (прямой вызов _decide)."""

    def test_skip_hard_overrides_high_clip(self) -> None:
        # FP: «ищу друга/подругу» + высокий clip → без правил было бы LIKE.
        svc = make_service(PreferencesEngine(make_prefs()))
        decision, _, reasons = svc._decide(
            filter_decision=FilterDecision.PASS,
            llm_score=0.7, clip_score=0.99, confidence=0.88,
            ai_reasons=["..."] , text="Ищу друга/подругу, люблю аниме, читать книги",
        )
        assert decision == AIDecision.DISLIKE
        assert any(r.startswith("USER_SKIP:") for r in reasons)

    def test_skip_smoking_hard(self) -> None:
        svc = make_service(PreferencesEngine(make_prefs()))
        decision, _, reasons = svc._decide(
            FilterDecision.PASS, 0.8, 0.8, 0.9, ["..."], 
            text="Иногда курю, но весёлая",
        )
        assert decision == AIDecision.DISLIKE

    def test_like_factor_lifts_below_threshold_to_review(self) -> None:
        # FN: «играю в игры» при низких скорах → REVIEW, а не DISLIKE.
        svc = make_service(PreferencesEngine(make_prefs()))
        decision, _, reasons = svc._decide(
            FilterDecision.PASS, 0.4, 0.3, 0.6, ["..."],
            text="весёлая девочка, играю во многие игры",
        )
        assert decision == AIDecision.REVIEW
        assert any("USER_LIKE" in r for r in reasons)

    def test_like_factor_does_not_override_filter_reject(self) -> None:
        svc = make_service(PreferencesEngine(make_prefs()))
        decision, _, reasons = svc._decide(
            FilterDecision.REJECT, 0.8, 0.8, 0.9, ["..."],
            text="люблю игры и аниме",
        )
        assert decision == AIDecision.DISLIKE

    def test_no_prefs_no_hard_negative_is_review(self) -> None:
        # Инвариант NO_HARD_NEGATIVE_MUST_NOT_BECOME_DISLIKE: без preferences
        # и без подтверждённого негатива низкий скор → REVIEW, не DISLIKE.
        svc = make_service()
        decision, _, _ = svc._decide(
            FilterDecision.PASS, 0.4, 0.3, 0.6, ["..."],
            text="играю во многие игры",
        )
        assert decision == AIDecision.REVIEW


class TestDecisionIntegration:
    """Сквозной прогон через DecisionService.evaluate_profile (оффлайн)."""

    def make_stack(self, tmp_db: Database, prefs: PreferencesEngine):
        config = make_config()
        ps = ProfileService(tmp_db)
        fe = FilterEngine(config)
        fs = FilterService(tmp_db, ps, fe)
        ai = AIScoringService(tmp_db, config, MockCLIP(0.4), MockLLM(0.4))
        return DecisionService(tmp_db, config, ps, fs, ai, preferences=prefs)

    async def _insert(self, db: Database, pid: int, desc: str) -> int:
        return await db.insert_profile(
            name="Anna", age=19, raw_city="Санкт-Петербург",
            normalized_city="Санкт-Петербург", description=desc,
            fingerprint=f"fp_p_{pid}", source_chat_id=1234060895,
            source_message_id=pid, first_seen_at="now", last_seen_at="now", status="NEW",
        )

    def test_observed_fp_gamer_friend_is_disliked(
        self, tmp_path: Path,
    ) -> None:
        # Реальный кейс #1: «Ищу друга/подругу ... аниме» — высокий clip,
        # но пользователь хочет DISLIKE (не ищет отношений).
        db = Database(path=tmp_path / "t.db")
        loop = asyncio.get_event_loop()
        loop.run_until_complete(db.connect())
        try:
            pid = loop.run_until_complete(self._insert(db, 1, "Ищу друга/подругу, который любит смотреть аниме"))
            svc = self.make_stack(db, PreferencesEngine(make_prefs()))
            res = loop.run_until_complete(svc.evaluate(pid))
            assert res is not None
            assert res.decision == AIDecision.DISLIKE
            assert any(r.startswith("USER_SKIP:") for r in res.reasons)
        finally:
            loop.run_until_complete(db.close())

    def test_observed_fn_gamer_is_not_lost(
        self, tmp_path: Path,
    ) -> None:
        # Реальный кейс: «играю во многие игры» при слабых скорах →
        # уходит в REVIEW (human), а не в DISLIKE.
        db = Database(path=tmp_path / "t.db")
        loop = asyncio.get_event_loop()
        loop.run_until_complete(db.connect())
        try:
            pid = loop.run_until_complete(self._insert(db, 2, "весёлая девочка, играю во многие игры, не скучная"))
            svc = self.make_stack(db, PreferencesEngine(make_prefs()))
            res = loop.run_until_complete(svc.evaluate(pid))
            assert res is not None
            assert res.decision == AIDecision.REVIEW
            assert any("USER_LIKE" in r for r in res.reasons)
        finally:
            loop.run_until_complete(db.close())


# ── Оффлайн моки LLM/CLIP (как в test_decision.py) ──────────────────

class MockLLM(BaseLLMService):
    def __init__(self, score=0.7, confidence=0.8):
        self._score = score
        self._confidence = confidence

    @property
    def is_enabled(self) -> bool:
        return True

    async def evaluate_profile(self, name, age, city, description) -> LLMScore:
        return LLMScore(score=self._score, confidence=self._confidence,
                        reasons=["mock"], model_version="mock-llm")


class MockCLIP(BaseCLIPService):
    def __init__(self, score=0.6, image_count=1):
        self._score = score
        self._image_count = image_count

    @property
    def is_enabled(self) -> bool:
        return True

    async def score_images(self, image_data_list) -> CLIPScore:
        return CLIPScore(image_count=self._image_count,
                         aesthetic_score=self._score, nsfw_score=0.0,
                         model_version="mock-clip")

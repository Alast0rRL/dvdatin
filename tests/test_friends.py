"""Режим «кенты» (config.friends): аккаунт ищет парней-друзей, а не девушек."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import AppConfig, FriendsConfig
from app.preferences import load_preferences
from collectors.dvinchik_collector import DvinchikCollector
from collectors.raw_worker import RawTask
from database.database import Database
from models.decision import AIDecision, AIDecisionResult
from models.filter import FilterDecision, FilterResult
from models.profile import Profile, ProfileStatus
from models.raw import ParsedProfile
from services.decision_service import DecisionService
from services.filter_engine import FilterEngine, nearby_km
from services.filter_service import FilterService
from services.profile_service import ProfileService
from tests.mocks import make_tg_client
from tests.test_collector import make_config, make_db_mock
from tests.test_match_greeter import MATCH_URL
from tests.test_web import sync_db  # noqa: F401  (фикстура)

LEO = 1234060895


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def friends_app_config(enabled: bool = True) -> AppConfig:
    """Два аккаунта: dvai — девушки, dvai_2 — кенты и авто-движок."""
    data = make_config().model_dump()
    data["telegram"] = {
        "accounts": [
            {"api_id": 1, "api_hash": "a" * 32, "session": "dvai"},
            {"api_id": 2, "api_hash": "b" * 32, "session": "dvai_2"},
        ]
    }
    data["project"] = {"mode": "SEMI_AUTO"}
    data["auto_actions"] = {
        "enabled": True, "account_session": "dvai_2", "interval_sec": 0.0,
        "like": {"enabled": True},
        "like_message": {"enabled": True, "text": "Берем)"},
    }
    data["friends"] = {
        "enabled": enabled, "account_session": "dvai_2",
        "like_message": "Здарова! Го знакомиться 🤝",
    }
    return AppConfig(**data)


@pytest.fixture
def tmp_db(tmp_path: Path) -> Database:
    db = Database(path=tmp_path / "friends.db")
    run(db.connect())
    yield db  # type: ignore[misc]
    run(db.close())


class TestFriendsConfig:
    def test_only_enabled_account_is_friends(self) -> None:
        cfg = FriendsConfig(enabled=True, account_session="dvai_2")
        assert cfg.is_friends("dvai_2")
        assert not cfg.is_friends("dvai")
        assert not cfg.is_friends(None)
        assert not FriendsConfig(account_session="dvai_2").is_friends("dvai_2")
        assert not FriendsConfig(enabled=True).is_friends("")

    def test_like_message_string_becomes_list(self) -> None:
        assert FriendsConfig(like_message="Здарова").like_message == ["Здарова"]

    def test_defaults_are_off(self) -> None:
        assert AppConfig(telegram={"api_id": 1, "api_hash": "x"}).friends.enabled is False


class TestNearbyCity:
    """Leo вместо города пишет «📍2 км» — человек рядом, проходит фильтр."""

    def test_parse_distance(self) -> None:
        assert nearby_km("📍2 км") == 2.0
        assert nearby_km("📍 1,5 км") == 1.5
        assert nearby_km("Санкт-Петербург") is None
        assert nearby_km("") is None

    def test_city_rule_accepts_nearby_within_limit(self) -> None:
        engine = FilterEngine(make_config())
        near = Profile(id=1, name="A", age=18, normalized_city="📍2 км")
        far = Profile(id=2, name="B", age=18, normalized_city="📍80 км")
        assert engine.evaluate(near).decision == FilterDecision.PASS
        assert engine.evaluate(far).decision == FilterDecision.REJECT


class TestFriendsProfiles:
    """Парень и девушка с одним «имя, возраст, город» не склеиваются."""

    def test_audiences_do_not_merge(self, tmp_db: Database) -> None:
        ps = ProfileService(tmp_db)
        parsed = ParsedProfile(
            name="Саша", age=20, normalized_city="Санкт-Петербург",
            description="Люблю спорт и прогулки по городу",
            source_chat_id=LEO, source_message_id=10,
        )
        girl = run(ps.upsert_profile(parsed, account_session="dvai"))
        parsed2 = parsed.model_copy(update={"source_message_id": 11})
        friend = run(ps.upsert_profile(
            parsed2, account_session="dvai_2", audience="friends",
        ))
        assert friend.id != girl.id
        assert girl.audience == "" and friend.audience == "friends"
        # Повторный показ кента находит его же анкету.
        parsed3 = parsed.model_copy(update={"source_message_id": 12})
        again = run(ps.upsert_profile(
            parsed3, account_session="dvai_2", audience="friends",
        ))
        assert again.id == friend.id

    def test_audience_column_migrated_on_old_db(self, tmp_path: Path) -> None:
        path = tmp_path / "old.db"
        db = Database(path=path)
        run(db.connect())
        run(db.close())
        with sqlite3.connect(path) as conn:
            conn.execute("ALTER TABLE profiles DROP COLUMN audience")
        db = Database(path=path)
        run(db.connect())
        try:
            with sqlite3.connect(path) as conn:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(profiles)")}
            assert "audience" in cols
        finally:
            run(db.close())


class TestFriendsDecision:
    """Встроенные «девичьи» признаки на кентов не действуют."""

    @staticmethod
    def _service(db: Database, builtin: bool) -> tuple[DecisionService, ProfileService]:
        config = make_config()
        ps = ProfileService(db)
        fs = FilterService(db, ps, FilterEngine(config))
        ds = DecisionService(
            db, config, ps, fs, preferences=load_preferences(
                Path("missing.yaml"), example=Path("missing.example.yaml"),
            ),
            builtin_features=builtin,
        )
        return ds, ps

    def test_builtin_hard_negative_ignored_for_friends(self, tmp_db: Database) -> None:
        text = (
            "Курю, ищу друга для прогулок по городу, люблю кино, музыку, "
            "футбол и долгие разговоры обо всём на свете вечерами"
        )
        parsed = ParsedProfile(
            name="Дима", age=19, normalized_city="Санкт-Петербург",
            description=text, source_chat_id=LEO, source_message_id=20,
        )
        girls_ds, ps = self._service(tmp_db, builtin=True)
        prof = run(ps.upsert_profile(parsed, account_session="dvai"))
        girls = run(girls_ds.evaluate(prof.id))
        assert girls.decision == AIDecision.DISLIKE

        friends_ds, ps = self._service(tmp_db, builtin=False)
        friends = run(friends_ds.evaluate(prof.id))
        assert friends.decision != AIDecision.DISLIKE
        assert not any("HARD_NEGATIVE" in r for r in friends.reasons)

    def test_example_fallback_used_when_live_file_missing(self, tmp_path: Path) -> None:
        example = tmp_path / "ex.yaml"
        example.write_text(
            "skip:\n  - label: x\n    match: ['ищу девушку']\n", encoding="utf-8",
        )
        prefs = load_preferences(tmp_path / "nope.yaml", example=example)
        assert len(prefs._prefs.skip) == 1


class TestFriendsCollector:
    """Pipeline на аккаунте кентов: свои сервисы, без фото, свой 💌."""

    @staticmethod
    def _task(client: object, text: str, session: str = "dvai_2") -> RawTask:
        msg = MagicMock()
        msg.client = client
        return RawTask(
            chat_id=LEO, message_id=900, sender_id=LEO, sender_username="",
            sender_name="", text=text, media_type="", entities_json="[]",
            reply_markup_json="[]", reply_to=None, received_at="now",
            msg_date="now", msg=msg, raw_id=1, account_session=session,
        )

    @staticmethod
    def _collector(enabled: bool = True) -> tuple[DvinchikCollector, AsyncMock, AsyncMock]:
        other, auto = AsyncMock(), make_tg_client()
        auto.is_connected.return_value = True
        collector = DvinchikCollector(
            [other, auto], make_db_mock(), friends_app_config(enabled),
            friends_filter_service=AsyncMock(),
            friends_decision_service=AsyncMock(),
        )
        profile = Profile(
            id=1, name="Саша", age=20, normalized_city="Санкт-Петербург",
            status=ProfileStatus.NEW,
        )
        collector._profile_service = AsyncMock()
        collector._profile_service.upsert_profile = AsyncMock(return_value=profile)
        passed = FilterResult(decision=FilterDecision.PASS, reasons=[])
        decision = AIDecisionResult(
            decision=AIDecision.LIKE, combined_score=0.8, confidence=0.7,
            reasons=["test"], scoring_version="deterministic-v2",
        )
        for fs, ds in (
            (collector._friends_filter_service, collector._friends_decision_service),
        ):
            fs.evaluate = AsyncMock(return_value=passed)
            ds.evaluate = AsyncMock(return_value=decision)
        collector._filter_service = AsyncMock()
        collector._filter_service.evaluate = AsyncMock(return_value=passed)
        collector._decision_service = AsyncMock()
        collector._decision_service.evaluate = AsyncMock(return_value=decision)
        collector._apply_photo_taste = AsyncMock(side_effect=lambda d, k: d)
        collector._apply_photo_vice = AsyncMock(side_effect=lambda d, m, card_key: d)
        collector._schedule_photo_shadow = MagicMock()
        return collector, other, auto

    def test_friends_profile_uses_friends_services_and_skips_photo(self) -> None:
        collector, _, auto = self._collector()
        run(collector._process_message(self._task(auto, "Саша, 20, Санкт-Петербург")))
        kwargs = collector._profile_service.upsert_profile.await_args.kwargs
        assert kwargs["audience"] == "friends"
        collector._friends_decision_service.evaluate.assert_awaited_once()
        collector._decision_service.evaluate.assert_not_called()
        collector._apply_photo_taste.assert_not_called()
        collector._apply_photo_vice.assert_not_called()
        collector._schedule_photo_shadow.assert_not_called()
        collector._db.record_auto_action.assert_awaited_once()

    def test_disabled_friends_uses_girls_pipeline(self) -> None:
        collector, _, auto = self._collector(enabled=False)
        run(collector._process_message(self._task(auto, "Саша, 20, Санкт-Петербург")))
        kwargs = collector._profile_service.upsert_profile.await_args.kwargs
        assert kwargs["audience"] == ""
        collector._decision_service.evaluate.assert_awaited_once()
        collector._friends_decision_service.evaluate.assert_not_called()
        collector._apply_photo_taste.assert_awaited_once()
        collector._schedule_photo_shadow.assert_called_once()

    def test_friends_engine_sends_own_like_text(self) -> None:
        collector, _, _ = self._collector()
        engine = collector.auto_engine("dvai_2")
        assert engine._pick_message_text() == "Здарова! Го знакомиться 🤝"
        assert collector.auto_engine("dvai")._pick_message_text() == "Берем)"

    def test_switch_friends_on_the_fly(self) -> None:
        """Кнопка на сайте: кенты включаются/переезжают без перезапуска."""
        collector, _, auto = self._collector(enabled=False)
        collector.set_friends_account("dvai_2")
        assert collector.auto_engine("dvai_2")._pick_message_text() == "Здарова! Го знакомиться 🤝"
        run(collector._process_message(self._task(auto, "Саша, 20, Санкт-Петербург")))
        collector._friends_decision_service.evaluate.assert_awaited_once()
        collector.set_friends_account("")
        assert collector.auto_engine("dvai_2")._pick_message_text() == "Берем)"
        assert not collector._is_friends_session("dvai_2")

    def test_match_with_friend_only_notifies_owner(self) -> None:
        collector, _, auto = self._collector()
        collector._match_greeter.greet = AsyncMock(return_value="SENT")
        collector._track_dialog = AsyncMock()
        collector._auto_engine.notify_owner = AsyncMock(return_value=True)
        run(collector._process_message(self._task(auto, MATCH_URL)))
        collector._match_greeter.greet.assert_not_called()
        collector._track_dialog.assert_awaited_once()
        text = collector._auto_engine.notify_owner.await_args.args[0]
        assert "кентом" in text and "dvai_2" in text


class TestFriendsNotInRateQueue:
    """Анкеты кентов не идут в /rate (модель вкуса — про девушек)."""

    def test_friends_profile_excluded(self, sync_db) -> None:  # noqa: F811
        sync, db_path, _ = sync_db
        before = {r["profile_id"] for r in sync.get_rating_queue(limit=10)}
        assert 1 in before
        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE profiles SET audience = 'friends' WHERE id = 1")
        after = {r["profile_id"] for r in sync.get_rating_queue(limit=10)}
        assert 1 not in after
        assert sync.get_photo_rating_stats()["left"] == len(before) - 1

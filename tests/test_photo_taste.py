# Тесты теневой модели вкуса по фото (services/photo_taste.py), фонового
# расчёта в коллекторе и таблицы photo_scores. CLIP не грузится —
# эмбеддинги и энкодер подменяются.

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from app.config import PhotoTasteConfig
from database.database import Database
from services.decision_service import DecisionService
from services.photo_taste import PhotoShadowScorer, PhotoTasteModel


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def make_model(tmp_path: Path, **kw) -> PhotoTasteModel:
    """Модель на 3-мерных «эмбеддингах»: вкус = первая координата."""
    data = {
        "version": "test-v1",
        "weights": [4.0, 0.0, 0.0],
        "bias": 0.0,
        "like_threshold": 0.8,
        "dislike_threshold": 0.3,
    }
    data.update(kw)
    path = tmp_path / "taste.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return PhotoTasteModel(PhotoTasteConfig(enabled=True, model_path=str(path)))


@pytest.fixture
def tmp_db(tmp_path: Path):
    db = Database(path=tmp_path / "taste.db")
    run(db.connect())
    yield db
    run(db.close())


class TestTasteModel:
    def test_zones(self, tmp_path: Path) -> None:
        m = make_model(tmp_path)
        like = m.score(np.array([[1.0, 0.0, 0.0]]))       # z=4 → 0.98
        dislike = m.score(np.array([[-1.0, 0.0, 0.0]]))   # z=-4 → 0.02
        review = m.score(np.array([[0.0, 1.0, 0.0]]))     # z=0 → 0.5
        assert like.verdict == "LIKE" and like.score > 0.97
        assert dislike.verdict == "DISLIKE" and dislike.score < 0.03
        assert review.verdict == "REVIEW" and abs(review.score - 0.5) < 1e-6

    def test_photos_are_averaged_then_normalized(self, tmp_path: Path) -> None:
        """Как при обучении: среднее по фото карточки, затем L2-нормировка."""
        m = make_model(tmp_path)
        two = m.score(np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]))
        expected = 1 / (1 + np.exp(-4.0 / np.sqrt(2)))
        assert abs(two.score - expected) < 1e-6

    def test_missing_file_disables(self, tmp_path: Path) -> None:
        m = PhotoTasteModel(PhotoTasteConfig(enabled=True, model_path=str(tmp_path / "nope.json")))
        assert not m.enabled
        assert m.score(np.array([[1.0, 0.0, 0.0]])) is None

    def test_disabled_does_not_read_file(self, tmp_path: Path) -> None:
        m = PhotoTasteModel(PhotoTasteConfig(enabled=False, model_path=str(tmp_path / "x.json")))
        assert not m.enabled

    def test_dim_mismatch_is_an_error(self, tmp_path: Path) -> None:
        m = make_model(tmp_path)
        with pytest.raises(ValueError):
            m.score(np.array([[1.0, 0.0]]))


class TestShadowScorer:
    def test_score_with_vice_probs(self, tmp_path: Path) -> None:
        encoder = MagicMock()
        encoder.failed = False
        encoder.encode_images = MagicMock(
            return_value=(np.array([[1.0, 0.0, 0.0], [0.9, 0.1, 0.0]]), [0, 2]),
        )
        vice = MagicMock()
        vice.probs_from_embeddings = MagicMock(return_value=[
            {"smoke": 0.2, "alcohol": 0.1}, {"smoke": 0.7, "alcohol": 0.05},
        ])
        scorer = PhotoShadowScorer(encoder, make_model(tmp_path), vice)
        out = scorer.score_images([b"a", b"broken", b"c"])
        assert out["verdict"] == "LIKE"
        assert out["n_photos"] == 2
        assert out["smoke"] == 0.7 and out["alcohol"] == 0.1
        assert out["model_version"] == "test-v1"

    def test_no_readable_photos(self, tmp_path: Path) -> None:
        encoder = MagicMock()
        encoder.failed = False
        encoder.encode_images = MagicMock(return_value=(None, []))
        scorer = PhotoShadowScorer(encoder, make_model(tmp_path))
        assert scorer.score_images([b"x"]) is None


class TestPhotoScoresTable:
    def test_save_is_idempotent_per_card(self, tmp_db: Database) -> None:
        async def _seed() -> int:
            pid = await tmp_db.insert_profile(
                name="A", age=19, raw_city="СПб", normalized_city="СПб", description="",
                fingerprint="fp", source_chat_id=1, source_message_id=1,
                first_seen_at="2026-01-01", last_seen_at="2026-01-01", status="NEW",
            )
            for score in (0.9, 0.1):  # повтор той же карточки — no-op
                await tmp_db.save_photo_score(
                    profile_id=pid, account_session="dvai", chat_id=1,
                    telegram_message_id=10, score=score, verdict="LIKE",
                    smoke_prob=0.01, alcohol_prob=None, n_photos=3, model_version="v",
                )
            cur = await tmp_db._connection.execute(
                "SELECT score, verdict, n_photos FROM photo_scores"
            )
            return await cur.fetchall()

        rows = run(_seed())
        assert [tuple(r) for r in rows] == [(0.9, "LIKE", 3)]


class TestCollectorShadow:
    @staticmethod
    def _collector(shadow, max_pending: int = 20):
        from collectors.dvinchik_collector import DvinchikCollector

        col = DvinchikCollector.__new__(DvinchikCollector)
        col._photo_shadow = shadow
        col._photo_shadow_pending = {}
        col._config = MagicMock()
        col._config.photo_taste.max_pending = max_pending
        col._config.photo_taste.max_photos = 4
        col._db = MagicMock()
        col._db.save_photo_score = AsyncMock()
        return col

    @staticmethod
    def _msg():
        msg = MagicMock()
        msg.grouped_id = None
        msg.photo = object()
        msg.download_media = AsyncMock(return_value=b"jpeg")
        return msg

    @staticmethod
    def _shadow(result):
        shadow = MagicMock()
        shadow.enabled = True
        shadow.score_images = MagicMock(return_value=result)
        return shadow

    def test_background_score_is_saved(self) -> None:
        shadow = self._shadow({
            "score": 0.81, "verdict": "LIKE", "n_photos": 1,
            "model_version": "v", "smoke": 0.02, "alcohol": 0.01,
        })
        col = self._collector(shadow)

        async def _go() -> None:
            col._schedule_photo_shadow(self._msg(), 5, "dvai", 1, 77)
            assert len(col._photo_shadow_pending) == 1
            await asyncio.gather(*list(col._photo_shadow_pending.values()))

        run(_go())
        shadow.score_images.assert_called_once_with([b"jpeg"])
        kw = col._db.save_photo_score.call_args.kwargs
        assert kw["profile_id"] == 5 and kw["telegram_message_id"] == 77
        assert kw["verdict"] == "LIKE" and kw["account_session"] == "dvai"

    def test_queue_cap_drops_extra_cards(self) -> None:
        col = self._collector(self._shadow(None), max_pending=1)

        async def _go() -> None:
            col._schedule_photo_shadow(self._msg(), 1, "dvai", 1, 1)
            col._schedule_photo_shadow(self._msg(), 2, "dvai", 1, 2)
            assert len(col._photo_shadow_pending) == 1
            await asyncio.gather(*list(col._photo_shadow_pending.values()))

        run(_go())

    def test_disabled_or_no_photo_does_nothing(self) -> None:
        shadow = self._shadow(None)
        shadow.enabled = False
        col = self._collector(shadow)
        no_photo = self._msg()
        no_photo.photo = None

        async def _go() -> None:
            col._schedule_photo_shadow(self._msg(), 1, "dvai", 1, 1)
            col._photo_shadow = self._shadow(None)
            col._schedule_photo_shadow(no_photo, 1, "dvai", 1, 2)
            assert not col._photo_shadow_pending

        run(_go())

    def test_errors_are_swallowed(self) -> None:
        shadow = self._shadow(None)
        shadow.score_images = MagicMock(side_effect=RuntimeError("clip boom"))
        col = self._collector(shadow)

        async def _go() -> None:
            col._schedule_photo_shadow(self._msg(), 1, "dvai", 1, 1)
            await asyncio.gather(*list(col._photo_shadow_pending.values()))

        run(_go())  # не падает
        col._db.save_photo_score.assert_not_awaited()


class TestViceReusesShadow:
    """Проверка сигарет/алкоголя для лайка берёт готовый теневой результат."""

    def test_vice_uses_shadow_result_without_second_clip_run(self) -> None:
        from app.config import PhotoCheckConfig
        from collectors.dvinchik_collector import DvinchikCollector
        from models.decision import AIDecision, AIDecisionResult
        from services.photo_vice import PhotoViceDetector

        col = DvinchikCollector.__new__(DvinchikCollector)
        encoder = MagicMock()
        encoder.failed = False
        detector = PhotoViceDetector(PhotoCheckConfig(enabled=True), encoder)
        detector.check_async = AsyncMock()  # не должен вызываться
        col._photo_vice = detector
        col._decision_service = MagicMock()
        col._decision_service.apply_photo_vice = AsyncMock(return_value="DISLIKED")
        dec = AIDecisionResult(
            profile_id=1, decision=AIDecision.LIKE, combined_score=0.6, confidence=0.6,
            reasons=[], evaluated_at="x",
        )

        async def _go():
            fut = asyncio.get_event_loop().create_future()
            fut.set_result({"smoke": 0.996, "alcohol": 0.01})
            col._photo_shadow_pending = {("dvai", 1, 5): fut}
            return await col._apply_photo_vice(dec, MagicMock(), card_key=("dvai", 1, 5))

        assert run(_go()) == "DISLIKED"
        detector.check_async.assert_not_awaited()
        reason = col._decision_service.apply_photo_vice.call_args.args[1]
        assert reason.startswith("PHOTO_VICE:smoke:")



class TestHotReload:
    def test_new_model_file_is_picked_up(self, tmp_path: Path) -> None:
        import os

        m = make_model(tmp_path)
        assert m.score(np.array([[1.0, 0.0, 0.0]])).verdict == "LIKE"
        path = tmp_path / "taste.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(version="test-v2", weights=[-4.0, 0.0, 0.0])
        path.write_text(json.dumps(data), encoding="utf-8")
        os.utime(path, (1, 1))          # гарантированно другой mtime
        m._checked_at = -1e9            # не ждать 30 с
        v = m.score(np.array([[1.0, 0.0, 0.0]]))
        assert m.version == "test-v2" and v.verdict == "DISLIKE"

    def test_broken_new_file_keeps_old_model(self, tmp_path: Path) -> None:
        import os

        m = make_model(tmp_path)
        m.score(np.array([[1.0, 0.0, 0.0]]))
        path = tmp_path / "taste.json"
        path.write_text("{broken", encoding="utf-8")
        os.utime(path, (2, 2))
        m._checked_at = -1e9
        v = m.score(np.array([[1.0, 0.0, 0.0]]))
        assert v is not None and v.verdict == "LIKE" and m.version == "test-v1"


def _seed_profile(db: Database, n: int, tm_ids: list[int], session: str = "dvai") -> int:
    async def _go() -> int:
        pid = await db.insert_profile(
            name=f"P{n}", age=19, raw_city="СПб", normalized_city="СПб", description="",
            fingerprint=f"fp{n}", source_chat_id=1, source_message_id=n,
            first_seen_at="2026-01-01", last_seen_at="2026-01-01", status="NEW",
        )
        for tm in tm_ids:
            await db.link_profile_message(
                profile_id=pid, telegram_message_id=tm, chat_id=1,
                created_at="2026-01-01", account_session=session,
            )
            await db.save_raw_message(
                telegram_message_id=tm, chat_id=1, sender_id=1, sender_username="",
                sender_name="Leo", message_date="2026-01-01", text="", raw_entities="[]",
                reply_markup="[]", media_type="photo", received_at="2026-01-01",
            )
        return pid
    return run(_go())


class TestBackfillSelection:
    def test_unrated_first_last_showing_first_photos(self, tmp_db: Database) -> None:
        rated = _seed_profile(tmp_db, 1, [10, 11])
        unrated = _seed_profile(tmp_db, 2, [20, 21, 50, 51, 52, 53])  # два показа

        async def _go():
            await tmp_db._connection.execute(
                "INSERT INTO photo_ratings (profile_id, rating, created_at) VALUES (?, 'LIKE', 'x')",
                (rated,),
            )
            await tmp_db._connection.commit()
            return await tmp_db.get_photo_backfill_batch(limit=10, max_photos=3)

        batch = run(_go())
        assert [b["profile_id"] for b in batch] == [unrated, rated]
        # Последний показ (50..53), первые 3 фото альбома.
        assert [p["message_id"] for p in batch[0]["photos"]] == [50, 51, 52]

    def test_failed_marker_is_not_retried(self, tmp_db: Database) -> None:
        pid = _seed_profile(tmp_db, 3, [30])
        run(tmp_db.save_photo_embedding(pid, None, 0, ""))
        assert run(tmp_db.get_photo_backfill_batch()) == []


class TestBackfillLoop:
    def test_one_pass_scores_and_stores(self) -> None:
        from unittest.mock import patch

        from collectors.dvinchik_collector import DvinchikCollector

        col = DvinchikCollector.__new__(DvinchikCollector)
        shadow = MagicMock()
        shadow.enabled = True
        emb = bytes(12)
        shadow.score_images = MagicMock(return_value={
            "score": 0.5, "verdict": "REVIEW", "n_photos": 1, "model_version": "v",
            "smoke": 0.0, "alcohol": 0.0, "embedding": emb,
        })
        col._photo_shadow = shadow
        col._config = MagicMock()
        col._config.photo_taste.enabled = True
        col._config.photo_taste.backfill = True
        col._config.photo_taste.max_photos = 3
        col._config.photo_taste.backfill_pause_sec = 0
        col._db = MagicMock()
        col._db.get_photo_backfill_batch = AsyncMock(return_value=[{
            "profile_id": 9,
            "photos": [{"message_id": 5, "chat_id": 1, "account_session": "dvai"}],
        }])
        col._db.save_photo_embedding = AsyncMock()
        col._db.save_photo_score = AsyncMock()
        col._backfill_photo_bytes = AsyncMock(return_value=[b"jpeg"])

        calls = {"n": 0}

        async def fake_sleep(_sec):
            calls["n"] += 1
            if calls["n"] >= 3:   # старт, пауза после анкеты, затем выходим
                raise asyncio.CancelledError

        with patch("collectors.dvinchik_collector.asyncio.sleep", fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                run(col.run_photo_backfill())
        col._db.save_photo_embedding.assert_awaited_with(9, emb, 1, "v")
        kw = col._db.save_photo_score.call_args.kwargs
        assert kw["profile_id"] == 9 and kw["telegram_message_id"] == 5

    def test_cache_hit_and_download(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        import web.photos as photos_mod
        from collectors.dvinchik_collector import DvinchikCollector

        col = DvinchikCollector.__new__(DvinchikCollector)
        client = MagicMock()
        fresh = MagicMock()
        fresh.photo = object()
        fresh.download_media = AsyncMock(return_value=b"downloaded")
        client.get_messages = AsyncMock(return_value=[fresh])
        col._clients_for_session = MagicMock(return_value=(client, None))

        with patch.object(photos_mod, "MEDIA_ROOT", tmp_path):
            cached = photos_mod._cache_filename(1, 7, "dvai")
            cached.parent.mkdir(parents=True)
            cached.write_bytes(b"cached")
            out = run(col._backfill_photo_bytes([
                {"profile_id": 1, "message_id": 7, "chat_id": 1, "account_session": "dvai"},
                {"profile_id": 1, "message_id": 8, "chat_id": 1, "account_session": "dvai"},
            ]))
            assert out == [b"cached", b"downloaded"]
            # Скачанное легло в кэш — /rate откроет сразу.
            assert photos_mod._cache_filename(1, 8, "dvai").read_bytes() == b"downloaded"
        client.get_messages.assert_awaited_once_with(1, ids=[8])


class TestTrainScript:
    def test_train_writes_model(self, tmp_path: Path) -> None:
        pytest.importorskip("sklearn")
        import sqlite3
        import subprocess
        import sys

        rng = np.random.default_rng(0)
        db = tmp_path / "t.db"
        con = sqlite3.connect(db)
        con.executescript(
            "CREATE TABLE photo_embeddings(profile_id INTEGER PRIMARY KEY, emb BLOB, "
            "n_photos INT, model TEXT, created_at TEXT);"
            "CREATE TABLE photo_ratings(profile_id INTEGER PRIMARY KEY, rating TEXT, created_at TEXT);"
            "CREATE TABLE photo_scores(id INTEGER PRIMARY KEY AUTOINCREMENT, profile_id INT, "
            "score REAL, verdict TEXT, model_version TEXT);"
        )
        con.execute("INSERT INTO photo_scores (profile_id, score, verdict, model_version) "
                    "VALUES (0, 0.5, 'REVIEW', 'old')")
        for pid in range(300):
            like = pid % 5 == 0
            v = rng.normal(size=8) + (2.0 if like else 0.0) * np.eye(8)[0]
            v /= np.linalg.norm(v)
            con.execute("INSERT INTO photo_embeddings VALUES (?, ?, 1, 'm', 'x')",
                        (pid, v.astype("<f4").tobytes()))
            con.execute("INSERT INTO photo_ratings VALUES (?, ?, 'x')",
                        (pid, "LIKE" if like else "DISLIKE"))
        con.commit()
        con.close()
        model = tmp_path / "m.json"
        script = Path(__file__).resolve().parent.parent / "scripts" / "train_photo_taste.py"
        res = subprocess.run(
            [sys.executable, str(script), "--db", str(db), "--model", str(model)],
            capture_output=True, text=True, encoding="utf-8",
        )
        assert res.returncode == 0, res.stdout + res.stderr
        data = json.loads(model.read_text(encoding="utf-8"))
        assert len(data["weights"]) == 8 and data["oof_auc"] > 0.8
        assert data["dislike_threshold"] < data["like_threshold"]
        # Вердикт пересчитан новой моделью (профиль 0 — «лайк» в синтетике).
        con = sqlite3.connect(db)
        version, verdict = con.execute(
            "SELECT model_version, verdict FROM photo_scores"
        ).fetchone()
        con.close()
        assert version == data["version"] and verdict in ("LIKE", "REVIEW", "DISLIKE")



class TestDecideMode:
    """photo_taste.mode = decide: лайк решает модель вкуса по фото."""

    @staticmethod
    def _decision(reasons: list[str], decision=None):
        from models.decision import AIDecision, AIDecisionResult

        return AIDecisionResult(
            profile_id=7, decision=decision or AIDecision.DISLIKE, combined_score=0.3,
            confidence=0.3, reasons=reasons, evaluated_at="x",
            scoring_version="deterministic-v2",
        )

    @staticmethod
    def _collector(mode: str = "decide", share: float = 0.2, threshold: float = 0.6):
        from collectors.dvinchik_collector import DvinchikCollector
        from app.config import PhotoTasteConfig

        col = DvinchikCollector.__new__(DvinchikCollector)
        col._config = MagicMock()
        col._config.photo_taste = PhotoTasteConfig(enabled=True, mode=mode, like_share=share)
        shadow = MagicMock()
        shadow.like_threshold = MagicMock(return_value=threshold)
        col._photo_shadow = shadow
        db = MagicMock()
        db.save_ai_decision = AsyncMock(return_value=1)
        col._decision_service = DecisionService(db, MagicMock(), MagicMock(), MagicMock())
        col._photo_shadow_pending = {}
        col._photo_shadow_results = {}
        return col

    def test_low_info_dislike_becomes_like_by_photo(self) -> None:
        """Пустая анкета: раньше скип «мало текста», теперь решает фото."""
        from models.decision import AIDecision

        col = self._collector()
        key = ("dvai", 1, 5)
        col._remember_photo_result(key, {"score": 0.71, "model_version": "m1"})
        out = run(col._apply_photo_taste(self._decision(["LOW_INFO_SKIP:words=0"]), key))
        assert out.decision == AIDecision.LIKE
        assert out.reasons[0] == "PHOTO_TASTE:like:0.710"
        assert out.scoring_version == "photo-taste/m1"
        col._photo_shadow.like_threshold.assert_called_once_with(0.2)

    def test_text_like_becomes_dislike_below_threshold(self) -> None:
        """«Информативная» анкета больше не лайкается за длину текста."""
        from models.decision import AIDecision

        col = self._collector()
        key = ("dvai", 1, 6)
        col._remember_photo_result(key, {"score": 0.4, "model_version": "m1"})
        out = run(col._apply_photo_taste(
            self._decision(["INFORMATIVE_CLEAN"], AIDecision.LIKE), key,
        ))
        assert out.decision == AIDecision.DISLIKE
        assert out.reasons[0].startswith("PHOTO_TASTE:dislike:")

    def test_text_bans_and_strong_plus_win(self) -> None:
        from models.decision import AIDecision

        col = self._collector()
        key = ("dvai", 1, 7)
        col._remember_photo_result(key, {"score": 0.99, "model_version": "m1"})
        for reasons, decision in (
            (["USER_SKIP:курит"], AIDecision.DISLIKE),
            (["HARD_NEGATIVE:has_boyfriend:есть парень"], AIDecision.DISLIKE),
            (["FILTER_REJECTED", "AGE_OUT_OF_RANGE"], AIDecision.DISLIKE),
            (["USER_LIKE_HARD:политех"], AIDecision.LIKE),
        ):
            dec = self._decision(reasons, decision)
            assert run(col._apply_photo_taste(dec, key)) is dec

    def test_shadow_mode_never_decides(self) -> None:
        col = self._collector(mode="shadow")
        key = ("dvai", 1, 8)
        col._remember_photo_result(key, {"score": 0.99, "model_version": "m1"})
        dec = self._decision(["LOW_INFO_SKIP:words=0"])
        assert run(col._apply_photo_taste(dec, key)) is dec

    def test_no_photo_result_keeps_old_rule(self) -> None:
        col = self._collector()
        dec = self._decision(["LOW_INFO_SKIP:words=0"])
        assert run(col._apply_photo_taste(dec, ("dvai", 1, 9))) is dec

    def test_waits_for_running_shadow_task(self) -> None:
        from models.decision import AIDecision

        col = self._collector()

        async def _go():
            fut = asyncio.get_event_loop().create_future()
            col._photo_shadow_pending[("dvai", 1, 10)] = fut
            asyncio.get_event_loop().call_later(
                0.01, fut.set_result, {"score": 0.8, "model_version": "m1"},
            )
            return await col._apply_photo_taste(
                self._decision(["NO_FEATURES_FOUND"], AIDecision.REVIEW), ("dvai", 1, 10),
            )

        assert run(_go()).decision == AIDecision.LIKE

    def test_result_cache_is_bounded(self) -> None:
        col = self._collector()
        for i in range(col.PHOTO_RESULTS_KEEP + 5):
            col._remember_photo_result(("dvai", 1, i), {"score": 0.1})
        assert len(col._photo_shadow_results) == col.PHOTO_RESULTS_KEEP
        assert ("dvai", 1, 0) not in col._photo_shadow_results


class TestLikeShareThreshold:
    def test_nearest_share_and_fallback(self, tmp_path: Path) -> None:
        m = make_model(tmp_path, like_thresholds={"0.15": 0.7, "0.20": 0.6, "0.30": 0.5})
        assert m.like_threshold(0.2) == 0.6
        assert m.like_threshold(0.27) == 0.5
        plain = make_model(tmp_path)
        assert plain.like_threshold(0.2) == 0.8   # нет таблицы — порог зоны LIKE

    def test_reason_labels(self) -> None:
        from services.reason_labels import describe_reason

        assert describe_reason("PHOTO_TASTE:like:0.712") == "Фото во вкусе (71%)"
        assert describe_reason("PHOTO_TASTE:dislike:0.1") == "Фото не во вкусе (10%)"

    def test_config_mode_validation(self) -> None:
        from pydantic import ValidationError

        assert PhotoTasteConfig(enabled=True, mode="DECIDE").decides
        assert not PhotoTasteConfig(enabled=False, mode="decide").decides
        with pytest.raises(ValidationError):
            PhotoTasteConfig(mode="auto")

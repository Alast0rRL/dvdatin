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

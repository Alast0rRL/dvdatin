# Тесты проверки фото на сигарету/алкоголь (services/photo_vice.py) и её
# проводки: DecisionService.apply_photo_vice, DvinchikCollector._apply_photo_vice,
# подпись причины. Настоящий CLIP не грузится — вероятности подменяются.

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.config import PhotoCheckConfig
from models.decision import AIDecision, AIDecisionResult
from services.decision_service import DecisionService
from services.clip_encoder import ClipEncoder
from services.photo_vice import PhotoViceDetector, ViceHit
from services.reason_labels import describe_reason


def make_config(**kw) -> PhotoCheckConfig:
    base = {"enabled": True, "smoke_threshold": 0.99, "alcohol_threshold": 0.97}
    base.update(kw)
    return PhotoCheckConfig(**base)


def make_decision(decision: AIDecision = AIDecision.LIKE) -> AIDecisionResult:
    return AIDecisionResult(
        profile_id=7, decision=decision, combined_score=0.6, confidence=0.6,
        reasons=["INFORMATIVE_CLEAN"], evaluated_at="2026-10-22T00:00:00+00:00",
        scoring_version="deterministic-v2", informative=True, meaningful_words=12,
    )


def make_detector(**kw) -> PhotoViceDetector:
    """Детектор с фиктивным (исправным) энкодером — CLIP не грузится."""
    encoder = MagicMock()
    encoder.failed = False
    return PhotoViceDetector(make_config(**kw), encoder)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestDetector:
    def test_disabled_never_checks(self) -> None:
        det = make_detector(enabled=False)
        det.class_probs = MagicMock()  # type: ignore[method-assign]
        assert not det.enabled
        assert det.check([b"img"]) is None
        det.class_probs.assert_not_called()

    def test_hit_above_threshold_picks_strongest(self) -> None:
        det = make_detector()
        det.class_probs = MagicMock(return_value=[  # type: ignore[method-assign]
            {"smoke": 0.10, "alcohol": 0.05, "neutral": 0.85},
            {"smoke": 0.995, "alcohol": 0.0, "neutral": 0.005},
            {"smoke": 0.0, "alcohol": 0.999, "neutral": 0.001},
        ])
        hit = det.check([b"a", b"b", b"c"])
        assert hit == ViceHit(kind="alcohol", prob=0.999, photo_index=2)
        assert hit.reason() == "PHOTO_VICE:alcohol:0.999"

    def test_below_strict_threshold_is_clean(self) -> None:
        """Пороги строгие: «наверное сигарета» (0.9) — не скип."""
        det = make_detector()
        det.class_probs = MagicMock(return_value=[  # type: ignore[method-assign]
            {"smoke": 0.90, "alcohol": 0.0, "neutral": 0.10},
            {"smoke": 0.0, "alcohol": 0.95, "neutral": 0.05},
        ])
        assert det.check([b"a", b"b"]) is None

    def test_missing_clip_disables_without_crash(self) -> None:
        """Нет torch/open_clip (или веса не скачались) — детектор выключается."""
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name in ("open_clip", "torch"):
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        encoder = ClipEncoder()
        det = PhotoViceDetector(make_config(), encoder)
        builtins.__import__ = fake_import
        try:
            assert encoder.load() is False
            assert det.check([b"img"]) is None
        finally:
            builtins.__import__ = real_import
        assert encoder.failed
        assert not det.enabled

    def test_no_encoder_means_disabled(self) -> None:
        assert not PhotoViceDetector(make_config()).enabled

    def test_probs_from_embeddings_softmax_by_class(self) -> None:
        """Вероятности классов — softmax по всем фразам, сумма по классу."""
        import numpy as np

        det = make_detector()
        n = sum(len(v) for v in __import__("services.photo_vice", fromlist=["x"]).VICE_PROMPTS.values())
        text = np.zeros((n, 3), dtype=np.float32)
        text[0] = [1, 0, 0]          # первая фраза «smoke» совпадает с фото
        det._text = text
        det._labels = (["smoke"] * 4) + (["alcohol"] * 5) + (["neutral"] * 10)
        probs = det.probs_from_embeddings(np.array([[1.0, 0.0, 0.0]], dtype=np.float32))
        assert probs[0]["smoke"] > 0.99
        assert abs(sum(probs[0].values()) - 1.0) < 1e-6


class TestDecisionOverride:
    def test_like_becomes_dislike_and_is_saved(self) -> None:
        db = MagicMock()
        db.save_ai_decision = AsyncMock(return_value=99)
        svc = DecisionService(db, MagicMock(), MagicMock(), MagicMock())
        res = run(svc.apply_photo_vice(make_decision(), "PHOTO_VICE:smoke:0.995"))
        assert res.decision == AIDecision.DISLIKE
        assert res.reasons[0] == "PHOTO_VICE:smoke:0.995"
        assert "INFORMATIVE_CLEAN" in res.reasons
        assert res.id == 99
        kwargs = db.save_ai_decision.call_args.kwargs
        assert kwargs["decision"] == "DISLIKE"


class TestReasonLabel:
    def test_photo_vice_is_readable(self) -> None:
        assert describe_reason("PHOTO_VICE:smoke:0.995") == "На фото сигарета/вейп (100%)"
        assert describe_reason("PHOTO_VICE:alcohol:0.98") == "На фото алкоголь (98%)"


class TestCollectorPhotoVice:
    """DvinchikCollector._apply_photo_vice: проводка без Telegram."""

    @staticmethod
    def _collector(detector, decision_service):
        from collectors.dvinchik_collector import DvinchikCollector

        col = DvinchikCollector.__new__(DvinchikCollector)
        col._photo_vice = detector
        col._decision_service = decision_service
        return col

    @staticmethod
    def _msg(photo: bool = True):
        msg = MagicMock()
        msg.grouped_id = None
        msg.photo = object() if photo else None
        msg.download_media = AsyncMock(return_value=b"jpeg-bytes")
        return msg

    @staticmethod
    def _detector(hit):
        det = MagicMock()
        det.enabled = True
        det.max_photos = 4
        det.check_async = AsyncMock(return_value=hit)
        return det

    def test_hit_turns_like_into_dislike(self) -> None:
        svc = MagicMock()
        overridden = make_decision(AIDecision.DISLIKE)
        svc.apply_photo_vice = AsyncMock(return_value=overridden)
        det = self._detector(ViceHit("smoke", 0.995, 0))
        col = self._collector(det, svc)
        out = run(col._apply_photo_vice(make_decision(), self._msg()))
        assert out is overridden
        det.check_async.assert_awaited_once_with([b"jpeg-bytes"])
        svc.apply_photo_vice.assert_awaited_once()
        assert svc.apply_photo_vice.call_args.args[1] == "PHOTO_VICE:smoke:0.995"

    def test_clean_photo_keeps_like(self) -> None:
        svc = MagicMock()
        svc.apply_photo_vice = AsyncMock()
        col = self._collector(self._detector(None), svc)
        dec = make_decision()
        assert run(col._apply_photo_vice(dec, self._msg())) is dec
        svc.apply_photo_vice.assert_not_awaited()

    def test_errors_never_change_decision(self) -> None:
        det = self._detector(None)
        det.check_async = AsyncMock(side_effect=RuntimeError("boom"))
        col = self._collector(det, MagicMock())
        dec = make_decision()
        assert run(col._apply_photo_vice(dec, self._msg())) is dec

    def test_disabled_detector_skips_download(self) -> None:
        det = self._detector(None)
        det.enabled = False
        msg = self._msg()
        col = self._collector(det, MagicMock())
        dec = make_decision()
        assert run(col._apply_photo_vice(dec, msg)) is dec
        msg.download_media.assert_not_awaited()

    def test_album_photos_are_collected(self) -> None:
        """Альбом: соседние сообщения с тем же grouped_id тоже проверяются."""
        det = self._detector(None)
        col = self._collector(det, MagicMock())

        def part(mid: int, gid: int | None, photo: bool = True):
            m = MagicMock()
            m.id = mid
            m.grouped_id = gid
            m.photo = object() if photo else None
            m.download_media = AsyncMock(return_value=f"p{mid}".encode())
            return m

        msg = part(100, 5)
        msg.get_input_chat = AsyncMock(return_value="leo")
        msg.client = MagicMock()
        msg.client.get_messages = AsyncMock(return_value=[
            None, part(99, 4), msg, part(101, 5), part(102, 5), part(103, None),
        ])
        run(col._apply_photo_vice(make_decision(), msg))
        det.check_async.assert_awaited_once_with([b"p100", b"p101", b"p102"])

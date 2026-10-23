# Тесты: ошибки бота → Telegram «твинку» + всплывающее окно на сайте
# (collectors/error_reporter.py), и регресс прод-бага 2026-10-23: фото-проверка
# роняла авто-действие (UnboundLocalError 'images') — лента вставала.

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from loguru import logger

from collectors.error_reporter import BOT_ERROR_STATE_KEY, BotErrorReporter
from models.decision import AIDecision, AIDecisionResult


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def make_db() -> MagicMock:
    db = MagicMock()
    db.set_state = AsyncMock()
    return db


def make_decision() -> AIDecisionResult:
    return AIDecisionResult(
        profile_id=4025, decision=AIDecision.LIKE, combined_score=0.64,
        confidence=0.64, reasons=["PHOTO_TASTE:like:0.640"], evaluated_at="x",
    )


class TestErrorReporter:
    def test_error_is_saved_for_site_and_sent_once(self) -> None:
        db = make_db()
        notify = AsyncMock(return_value=True)
        rep = BotErrorReporter(db, notify=notify)
        item = {"at": "2026-10-23T13:01:48+00:00", "where": "collector:x:1",
                "message": "AutoAction error: boom 742201"}
        run(rep.handle(item))
        key, raw = db.set_state.call_args.args
        assert key == BOT_ERROR_STATE_KEY
        saved = json.loads(raw)
        assert saved["message"] == item["message"] and saved["count"] == 1
        notify.assert_awaited_once()
        assert "⚠️ Ошибка бота" in notify.call_args.args[0]
        assert "boom" in notify.call_args.args[0]

    def test_same_error_is_not_spammed(self) -> None:
        """Та же ошибка (другие числа) — на сайте обновляется, в TG не шлётся."""
        db = make_db()
        notify = AsyncMock(return_value=True)
        rep = BotErrorReporter(db, notify=notify)
        for n in (1, 2, 3):
            run(rep.handle({"at": f"t{n}", "where": "w", "message": f"boom card {n}00"}))
        assert notify.await_count == 1
        assert json.loads(db.set_state.call_args.args[1])["count"] == 3
        # После паузы — снова шлём, с числом повторов.
        rep._sent_at = {k: v - rep.NOTIFY_COOLDOWN_SEC - 1 for k, v in rep._sent_at.items()}
        run(rep.handle({"at": "t4", "where": "w", "message": "boom card 400"}))
        assert notify.await_count == 2
        assert "Повторялась с прошлого уведомления: 2 раз" in notify.call_args.args[0]

    def test_different_errors_each_notify(self) -> None:
        notify = AsyncMock(return_value=True)
        rep = BotErrorReporter(make_db(), notify=notify)
        run(rep.handle({"at": "t1", "where": "w", "message": "first"}))
        run(rep.handle({"at": "t2", "where": "w", "message": "second"}))
        assert notify.await_count == 2

    def test_logger_error_reaches_reporter_but_not_its_own(self) -> None:
        """ERROR в логе любого модуля → очередь; ошибки репортёра — нет (петля)."""
        rep = BotErrorReporter(make_db(), notify=AsyncMock(return_value=True))

        async def _go():
            rep.install()
            try:
                logger.error("PhotoVice: сбой проверки фото")
                logger.warning("просто предупреждение")
                logger.bind().patch(
                    lambda r: r.update(name="collectors.error_reporter")
                ).error("ошибка внутри репортёра")
                await asyncio.sleep(0.05)
                items = []
                while not rep._queue.empty():
                    items.append(rep._queue.get_nowait())
                return items
            finally:
                rep.uninstall()

        items = run(_go())
        assert [i["message"] for i in items] == ["PhotoVice: сбой проверки фото"]
        assert "test_error_reporter" in items[0]["where"]

    def test_notify_failure_does_not_raise(self) -> None:
        rep = BotErrorReporter(make_db(), notify=AsyncMock(side_effect=RuntimeError("tg down")))
        run(rep.handle({"at": "t", "where": "w", "message": "x"}))  # не падает


class TestPhotoChecksNeverBreakActions:
    """Регресс прод 2026-10-23: бот встал на первой анкете, которую модель фото
    решила лайкнуть."""

    @staticmethod
    def _collector():
        from app.config import PhotoCheckConfig
        from collectors.dvinchik_collector import DvinchikCollector
        from services.photo_vice import PhotoViceDetector

        col = DvinchikCollector.__new__(DvinchikCollector)
        encoder = MagicMock()
        encoder.failed = False
        col._photo_vice = PhotoViceDetector(PhotoCheckConfig(enabled=True), encoder)
        col._decision_service = MagicMock()
        col._decision_service.apply_photo_vice = AsyncMock()
        col._photo_shadow_pending = {}
        col._photo_shadow_results = {}
        return col

    def test_clean_shadow_result_keeps_like(self) -> None:
        """Чистое фото из теневого расчёта → лайк остаётся, исключения нет."""
        col = self._collector()
        key = ("dvai", 1234060895, 742201)
        col._remember_photo_result(key, {"smoke": 0.01, "alcohol": 0.02, "score": 0.64})
        dec = make_decision()
        assert run(col._apply_photo_vice(dec, MagicMock(), card_key=key)) is dec
        col._decision_service.apply_photo_vice.assert_not_awaited()

    def test_any_error_inside_keeps_decision(self) -> None:
        col = self._collector()
        col._apply_photo_vice_impl = AsyncMock(side_effect=UnboundLocalError("images"))
        col._apply_photo_taste_impl = AsyncMock(side_effect=KeyError("score"))
        dec = make_decision()
        assert run(col._apply_photo_vice(dec, MagicMock(), card_key=None)) is dec
        assert run(col._apply_photo_taste(dec, ("dvai", 1, 1))) is dec

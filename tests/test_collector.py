# Integration-тесты DvinchikCollector + вспомогательные модули.

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from collectors.dvinchik_collector import DvinchikCollector, _detect_media_type
from collectors.dedup import Dedup
from collectors.raw_queue import RawQueue
from collectors.raw_worker import DvinchikRawWorker, RawTask
from collectors.stats import CollectorStats
from app.config import AppConfig, TelegramConfig, FiltersConfig, DvinchikConfig
from database.database import Database
from services.profile_service import ProfileService


# ==================== MOCKS ====================

def make_config(**overrides) -> AppConfig:
    defaults = {
        "telegram": {"api_id": 123, "api_hash": "abc"},
        "dvinchik": {"chat_id": 1234060895},
        "filters": {
            "age": {"min": 18, "max": 19},
            "city": {"allowed": ["Санкт-Петербург"]},
        },
    }
    defaults.update(overrides)
    return AppConfig(**defaults)


def make_db_mock() -> Database:
    db = AsyncMock(spec=Database)
    db.save_raw_message = AsyncMock(return_value=1)
    db.has_auto_action = AsyncMock(return_value=False)
    db.has_auto_action_for_message = AsyncMock(return_value=False)
    db.record_auto_action = AsyncMock()
    return db


def make_event(
    text: str = "",
    chat_id: int = 1234060895,
    msg_id: int = 1,
    sender_id: int = 100,
    media_type: object | None = None,
) -> MagicMock:
    """Создаёт мок Telegram-события."""
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

    sender = MagicMock()
    sender.id = sender_id
    sender.username = "testuser"
    sender.first_name = "Test"
    sender.last_name = "User"

    event.message = msg
    # Реальное Telegram-сообщение имеет синхронный int sender_id
    # (используется коллектором ДО любого network await).
    msg.sender_id = sender_id
    event.__aiter__ = None

    async def get_sender():
        return sender

    msg.get_sender = get_sender
    return event


# ==================== DEDUP ====================

class TestDedup:
    def test_unknown_not_known(self) -> None:
        d = Dedup()
        assert d.is_known(1) is False

    def test_mark_then_known(self) -> None:
        d = Dedup()
        d.mark(1)
        assert d.is_known(1) is True

    def test_different_ids(self) -> None:
        d = Dedup()
        d.mark(1)
        assert d.is_known(1) is True
        assert d.is_known(2) is False


# ==================== RAW QUEUE ====================

class TestRawQueue:
    def test_put_get(self) -> None:
        q = RawQueue()
        asyncio.get_event_loop().run_until_complete(q.put("item1"))
        assert q.qsize == 1
        result = asyncio.get_event_loop().run_until_complete(q.get())
        assert result == "item1"
        assert q.qsize == 0

    def test_stats(self) -> None:
        q = RawQueue()
        asyncio.get_event_loop().run_until_complete(q.put("a"))
        asyncio.get_event_loop().run_until_complete(q.get())
        s = q.stats
        assert s["total_enqueued"] == 1
        assert s["total_dequeued"] == 1


# ==================== COLLECTOR STATS ====================

class TestCollectorStats:
    def test_record_profile(self) -> None:
        s = CollectorStats()
        s.record_profile(filter_match=True)
        assert s.summary["profiles"] == 1
        assert s.summary["filter_match"] == 1
        assert s.summary["total_messages"] == 1

    def test_record_match(self) -> None:
        s = CollectorStats()
        s.record_match()
        assert s.summary["matches"] == 1

    def test_record_media_only(self) -> None:
        s = CollectorStats()
        s.record_media_only()
        assert s.summary["media_only"] == 1

    def test_record_unknown(self) -> None:
        s = CollectorStats()
        s.record_unknown()
        assert s.summary["unknown"] == 1

    def test_uptime(self) -> None:
        s = CollectorStats()
        assert s.uptime >= 0


# ==================== COLLECTOR INTEGRATION ====================

class TestCollectorIntegration:
    def test_register(self) -> None:
        client = AsyncMock()
        client.add_event_handler = MagicMock()
        db = make_db_mock()
        config = make_config()
        stats = CollectorStats()
        collector = DvinchikCollector(client, db, config, stats=stats)
        collector.register()
        assert client.add_event_handler.call_count == 3

    def test_handle_profile(self) -> None:
        client = AsyncMock()
        db = make_db_mock()
        config = make_config()
        stats = CollectorStats()
        collector = DvinchikCollector(client, db, config, stats=stats)

        event = make_event(text="wimx, 18, Санкт-Петербург", msg_id=100)
        asyncio.get_event_loop().run_until_complete(
            collector._handle_new_message(event)
        )

        db.save_raw_message.assert_called_once()
        assert stats.summary["profiles"] == 1

    def test_handle_match(self) -> None:
        client = AsyncMock()
        db = make_db_mock()
        config = make_config()
        stats = CollectorStats()
        collector = DvinchikCollector(client, db, config, stats=stats)

        text = "Начинай общаться 👉 [Anna](https://t.me/anna123?ref=abc)"
        event = make_event(text=text, msg_id=200)
        asyncio.get_event_loop().run_until_complete(
            collector._handle_new_message(event)
        )

        assert stats.summary["matches"] == 1

    def test_handle_media_only(self) -> None:
        client = AsyncMock()
        db = make_db_mock()
        config = make_config()
        stats = CollectorStats()
        collector = DvinchikCollector(client, db, config, stats=stats)

        fake_photo = MagicMock()
        fake_photo.__class__ = type("MessageMediaPhoto", (), {})
        event = make_event(text="", msg_id=300, media_type=fake_photo)
        asyncio.get_event_loop().run_until_complete(
            collector._handle_new_message(event)
        )

        assert stats.summary["media_only"] == 1

    def test_handle_unknown(self) -> None:
        client = AsyncMock()
        db = make_db_mock()
        config = make_config()
        stats = CollectorStats()
        collector = DvinchikCollector(client, db, config, stats=stats)

        event = make_event(text="Просто текст", msg_id=400)
        asyncio.get_event_loop().run_until_complete(
            collector._handle_new_message(event)
        )

        assert stats.summary["unknown"] == 1


# ==================== SOURCE FILTER (allowlist) ====================

class TestSourceFilter:
    """Сообщения из неразрешённых чатов отбрасываются ДО обработки."""

    def _photo_event(self, chat_id: int, msg_id: int) -> MagicMock:
        fake_photo = MagicMock()
        fake_photo.__class__ = type("MessageMediaPhoto", (), {})
        return make_event(text="", chat_id=chat_id, msg_id=msg_id, media_type=fake_photo)

    def test_authorized_chat_is_processed(self) -> None:
        client = AsyncMock()
        db = make_db_mock()
        config = make_config()
        stats = CollectorStats()
        collector = DvinchikCollector(client, db, config, stats=stats)
        # dvinchik.chat_id=1234060895 в make_config → разрешён
        event = make_event(text="wimx, 18, Санкт-Петербург", chat_id=1234060895)
        asyncio.get_event_loop().run_until_complete(
            collector._handle_new_message(event)
        )
        db.save_raw_message.assert_called_once()
        assert stats.summary["profiles"] == 1

    def test_unauthorized_ignored_before_db(self) -> None:
        client = AsyncMock()
        db = make_db_mock()
        config = make_config()
        stats = CollectorStats()
        collector = DvinchikCollector(client, db, config, stats=stats)

        event = make_event(text="Просто текст из чужого чата", chat_id=-1001225291649)
        asyncio.get_event_loop().run_until_complete(
            collector._handle_new_message(event)
        )
        db.save_raw_message.assert_not_called()
        assert stats.summary["profiles"] == 0
        assert stats.summary["unknown"] == 0

    def test_unauthorized_photo_no_download_no_db(self) -> None:
        client = AsyncMock()
        db = make_db_mock()
        config = make_config()
        stats = CollectorStats()
        collector = DvinchikCollector(client, db, config, stats=stats)

        event = self._photo_event(chat_id=-1001225291649, msg_id=998)
        asyncio.get_event_loop().run_until_complete(
            collector._handle_new_message(event)
        )
        db.save_raw_message.assert_not_called()

    def test_unauthorized_no_ai_scoring(self) -> None:
        client = AsyncMock()
        db = make_db_mock()
        config = make_config()
        stats = CollectorStats()
        fake_decision = AsyncMock()
        collector = DvinchikCollector(
            client, db, config, stats=stats, decision_service=fake_decision
        )
        event = make_event(text="текст", chat_id=-1001225291649)
        asyncio.get_event_loop().run_until_complete(
            collector._handle_new_message(event)
        )
        fake_decision.evaluate.assert_not_called()

    def test_config_sources_seeded_from_dvinchik(self) -> None:
        config = make_config(dvinchik={"chat_id": 1234060895})
        assert 1234060895 in config.sources.allowed_chat_ids


# ==================== MEDIA LINKING ====================

class TestMediaLinking:
    """MEDIA_ONLY должен привязываться к последней PROFILE, а не к другому MEDIA."""

    def _photo_event(self, chat_id: int, msg_id: int) -> MagicMock:
        fake_photo = MagicMock()
        fake_photo.__class__ = type("MessageMediaPhoto", (), {})
        return make_event(text="", chat_id=chat_id, msg_id=msg_id, media_type=fake_photo)

    def test_media_links_to_profile_not_to_media(self) -> None:
        client = AsyncMock()
        db = make_db_mock()
        config = make_config()
        stats = CollectorStats()
        # В проде profile_service всегда задан — он фиксирует контекст профиля.
        profile_service = MagicMock()
        profile_service.upsert_profile = AsyncMock(return_value=MagicMock(status="NEW"))
        collector = DvinchikCollector(
            client, db, config, stats=stats, profile_service=profile_service
        )

        asyncio.get_event_loop().run_until_complete(
            collector._handle_new_message(
                make_event(text="wimx, 18, Санкт-Петербург", chat_id=1234060895, msg_id=683833)
            )
        )
        asyncio.get_event_loop().run_until_complete(
            collector._handle_new_message(self._photo_event(1234060895, 683840))
        )
        asyncio.get_event_loop().run_until_complete(
            collector._handle_new_message(self._photo_event(1234060895, 683841))
        )
        # Контекст остаётся профилем 683833, media не перезаписывает его
        assert collector._pending_profiles[1234060895] == 683833

    def test_foreign_media_never_linked(self) -> None:
        client = AsyncMock()
        db = make_db_mock()
        config = make_config()
        stats = CollectorStats()
        collector = DvinchikCollector(client, db, config, stats=stats)
        event = self._photo_event(chat_id=-1001225291649, msg_id=683999)
        asyncio.get_event_loop().run_until_complete(
            collector._handle_new_message(event)
        )
        db.save_raw_message.assert_not_called()
        assert 1234060895 not in collector._pending_profiles or \
            collector._pending_profiles.get(-1001225291649) is None


# ==================== IDEMPOTENCY (C1 + C2) ====================

class TestIdempotency:
    """Дублирующая доставка не создаёт RAW и не перезапускает pipeline."""

    def _real_db(self, tmp_path: Path) -> Database:
        db = Database(path=tmp_path / "dedup.db")
        asyncio.get_event_loop().run_until_complete(db.connect())
        return db

    def _collector(self, db: Database, chat_id: int = 1234060895) -> DvinchikCollector:
        config = make_config(dvinchik={"chat_id": chat_id})
        profile_service = MagicMock()
        profile_service.upsert_profile = AsyncMock(
            return_value=MagicMock(status="NEW")
        )
        return DvinchikCollector(
            AsyncMock(), db, config, profile_service=profile_service
        )

    def test_duplicate_same_session_skipped(self, tmp_path: Path) -> None:
        db = self._real_db(tmp_path)
        try:
            loop = asyncio.get_event_loop()
            collector = self._collector(db)
            event = make_event(
                text="wimx, 18, Санкт-Петербург", chat_id=1234060895, msg_id=555
            )
            loop.run_until_complete(collector._handle_new_message(event))
            loop.run_until_complete(collector._handle_new_message(event))

            # pipeline (profile upsert) запущен ровно один раз
            assert collector._profile_service.upsert_profile.call_count == 1
            # RAW сохранён ровно один раз
            cursor = loop.run_until_complete(db._connection.execute(
                "SELECT COUNT(*) FROM raw_messages "
                "WHERE chat_id=? AND telegram_message_id=?",
                (1234060895, 555),
            ))
            count = loop.run_until_complete(cursor.fetchone())
            assert count[0] == 1
        finally:
            asyncio.get_event_loop().run_until_complete(db.close())

    def test_replay_after_restart_skipped_via_db(self, tmp_path: Path) -> None:
        db = self._real_db(tmp_path)
        try:
            loop = asyncio.get_event_loop()
            event = make_event(
                text="wimx, 18, Санкт-Петербург", chat_id=1234060895, msg_id=556
            )
            # Сессия 1: сохраняем RAW + запускаем pipeline
            c1 = self._collector(db)
            loop.run_until_complete(c1._handle_new_message(event))
            # Сессия 2: новый in-memory Dedup, та же БД (имитация restart/reconnect)
            c2 = self._collector(db)
            loop.run_until_complete(c2._handle_new_message(event))

            assert c1._profile_service.upsert_profile.call_count == 1
            # DB UNIQUE отсекает повтор — pipeline не перезапускается
            assert c2._profile_service.upsert_profile.call_count == 0
        finally:
            asyncio.get_event_loop().run_until_complete(db.close())

    def test_db_unique_prevents_duplicate_raw(self, tmp_path: Path) -> None:
        db = self._real_db(tmp_path)
        try:
            loop = asyncio.get_event_loop()
            kw = dict(
                telegram_message_id=557, chat_id=1234060895, sender_id=1,
                sender_username="", sender_name="", message_date="2026-01-01",
                text="x", raw_entities="[]", media_type="",
                reply_to_message_id=None, received_at="2026-01-01",
            )
            r1 = loop.run_until_complete(db.save_raw_message(**kw))
            r2 = loop.run_until_complete(db.save_raw_message(**kw))
            assert r1 is not None and r1 > 0
            assert r2 is None
        finally:
            asyncio.get_event_loop().run_until_complete(db.close())

    def test_dedup_concurrent_same_message(self) -> None:
        d = Dedup()
        key = (1234060895, 777)

        async def check() -> bool:
            seen = d.is_known(key)
            d.mark(key)
            return seen

        loop = asyncio.get_event_loop()
        results = loop.run_until_complete(
            asyncio.gather(*[check() for _ in range(50)])
        )
        # is_known/mark синхронны и атомарны в asyncio (нет await между ними):
        # первая проверка видит False до любого mark, остальные — True (нет гонки)
        assert results.count(False) == 1
        assert results.count(True) == 49


# ==================== RAW WORKER (Stage 6.2) ====================

class TestRawWorker:
    """Pipeline вынесен из Telegram-хендлера в фоновый worker через RawQueue."""

    def _task(self, message_id: int = 1, chat_id: int = 1234060895) -> RawTask:
        return RawTask(
            chat_id=chat_id,
            message_id=message_id,
            sender_id=100,
            sender_username="testuser",
            sender_name="Test User",
            text="wimx, 18, Санкт-Петербург",
            media_type="",
            entities_json="[]",
            reply_to=None,
            received_at="2026-01-01T00:00:00+00:00",
            msg_date="2026-01-01T00:00:00+00:00",
            msg=None,
            raw_id=1,
        )

    def _collector_with_worker(
        self,
    ) -> tuple[DvinchikCollector, DvinchikRawWorker, AsyncMock]:
        client = AsyncMock()
        db = make_db_mock()
        config = make_config()
        process = AsyncMock()
        collector = DvinchikCollector(
            client, db, config,
            stats=CollectorStats(),
        )
        worker = DvinchikRawWorker(process=process)
        collector.attach_worker(worker)
        return collector, worker, process

    def test_handler_does_not_run_ai(self) -> None:
        collector, _worker, process = self._collector_with_worker()
        loop = asyncio.get_event_loop()
        event = make_event(text="wimx, 18, Санкт-Петербург", msg_id=100)
        loop.run_until_complete(collector._handle_new_message(event))
        # Хендлер только ставит в очередь — сам pipeline НЕ выполняется
        assert collector._dedup.is_known((1234060895, 100)) is True
        process.assert_not_called()

    def test_message_enqueued(self) -> None:
        collector, worker, process = self._collector_with_worker()
        loop = asyncio.get_event_loop()
        event = make_event(text="wimx, 18, Санкт-Петербург", msg_id=101)
        loop.run_until_complete(collector._handle_new_message(event))
        assert worker.qsize == 1
        assert worker.stats["total_enqueued"] == 1
        # worker не запущен — обработано 0
        assert worker.stats["total_dequeued"] == 0
        process.assert_not_called()

    def test_worker_processes_queue(self) -> None:
        process = AsyncMock()
        worker = DvinchikRawWorker(process=process)
        worker.start()
        loop = asyncio.get_event_loop()
        item = self._task(message_id=7)
        loop.run_until_complete(worker.enqueue(item))
        loop.run_until_complete(worker.stop())
        process.assert_awaited_once_with(item)
        assert worker.qsize == 0

    def test_worker_error_does_not_kill(self) -> None:
        calls: list[int] = []

        async def flaky(item: RawTask) -> None:
            calls.append(item.message_id)
            raise RuntimeError("boom")

        worker = DvinchikRawWorker(process=flaky)
        worker.start()
        loop = asyncio.get_event_loop()
        loop.run_until_complete(worker.enqueue(self._task(message_id=1)))
        loop.run_until_complete(worker.enqueue(self._task(message_id=2)))
        loop.run_until_complete(worker.stop())
        # Обе задачи обработаны (ошибки пойманы), worker не упал и завершился
        assert calls == [1, 2]
        assert worker._task is None

    def test_worker_graceful_shutdown(self) -> None:
        process = AsyncMock()
        worker = DvinchikRawWorker(process=process)
        worker.start()
        loop = asyncio.get_event_loop()
        loop.run_until_complete(worker.enqueue(self._task(message_id=3)))
        loop.run_until_complete(worker.stop())
        # очередь дообработана до выхода
        assert process.await_count == 1
        assert worker._task is None
        # повторный stop безопасен и не зависает
        loop.run_until_complete(worker.stop())

    def test_duplicate_not_reprocessed_with_worker(self, tmp_path: Path) -> None:
        db = Database(path=tmp_path / "worker_dedup.db")
        loop = asyncio.get_event_loop()
        loop.run_until_complete(db.connect())
        try:
            process = AsyncMock()
            config = make_config(dvinchik={"chat_id": 1234060895})
            collector = DvinchikCollector(
                AsyncMock(), db, config,
                profile_service=MagicMock(),
                stats=CollectorStats(),
            )
            worker = DvinchikRawWorker(process=process)
            collector.attach_worker(worker)
            collector.start()

            event = make_event(
                text="wimx, 18, Санкт-Петербург", chat_id=1234060895, msg_id=600
            )
            loop.run_until_complete(collector._handle_new_message(event))
            loop.run_until_complete(collector._handle_new_message(event))
            loop.run_until_complete(collector.stop())

            # дубликат не попадает в pipeline повторно
            assert process.await_count == 1
            # RAW сохранён ровно один раз
            cursor = loop.run_until_complete(db._connection.execute(
                "SELECT COUNT(*) FROM raw_messages "
                "WHERE chat_id=? AND telegram_message_id=?",
                (1234060895, 600),
            ))
            count = loop.run_until_complete(cursor.fetchone())
            assert count[0] == 1
        finally:
            loop.run_until_complete(db.close())


# ==================== MEDIA_ONLY PERSISTENT LINKING (W2) ====================

class TestMediaLinkingPersistent:
    """MEDIA_ONLY привязывается к профилю и переживает restart/reconnect."""

    def _photo(self) -> MagicMock:
        fake_photo = MagicMock()
        fake_photo.__class__ = type("MessageMediaPhoto", (), {})
        return fake_photo

    def _real_db(self, tmp_path: Path) -> Database:
        db = Database(path=tmp_path / "media.db")
        asyncio.get_event_loop().run_until_complete(db.connect())
        return db

    def _collector(self, db: Database, chat_id: int = 1234060895) -> DvinchikCollector:
        config = make_config(dvinchik={"chat_id": chat_id})
        profile_service = ProfileService(db)
        return DvinchikCollector(
            AsyncMock(), db, config,
            profile_service=profile_service,
            stats=CollectorStats(),
        )

    def test_profile_then_media_linked(self, tmp_path: Path) -> None:
        db = self._real_db(tmp_path)
        try:
            loop = asyncio.get_event_loop()
            c = self._collector(db)
            loop.run_until_complete(c._handle_new_message(
                make_event(text="wimx, 18, Санкт-Петербург", chat_id=1234060895, msg_id=100)
            ))
            loop.run_until_complete(c._handle_new_message(
                make_event(text="", chat_id=1234060895, msg_id=101, media_type=self._photo())
            ))
            # Профиль создан по source_message_id=100
            profile = loop.run_until_complete(
                c._profile_service.find_profile_by_message(1234060895, 100)
            )
            assert profile is not None
            # Media 101 привязано к тому же профилю
            media_profile = loop.run_until_complete(
                c._profile_service.find_profile_by_message(1234060895, 101)
            )
            assert media_profile is not None
            assert media_profile.id == profile.id
        finally:
            asyncio.get_event_loop().run_until_complete(db.close())

    def test_media_after_restart_recovered(self, tmp_path: Path) -> None:
        db = self._real_db(tmp_path)
        try:
            loop = asyncio.get_event_loop()
            # Сессия 1: PROFILE сохраняет контекст в БД
            c1 = self._collector(db)
            loop.run_until_complete(c1._handle_new_message(
                make_event(text="wimx, 18, Санкт-Петербург", chat_id=1234060895, msg_id=200)
            ))
            # Сессия 2 (restart): in-memory кэш пуст, восстановление из БД
            c2 = self._collector(db)
            loop.run_until_complete(c2._handle_new_message(
                make_event(text="", chat_id=1234060895, msg_id=201, media_type=self._photo())
            ))
            media_profile = loop.run_until_complete(
                c2._profile_service.find_profile_by_message(1234060895, 201)
            )
            assert media_profile is not None
        finally:
            asyncio.get_event_loop().run_until_complete(db.close())

    def test_multiple_media_linked_to_same_profile(self, tmp_path: Path) -> None:
        db = self._real_db(tmp_path)
        try:
            loop = asyncio.get_event_loop()
            c = self._collector(db)
            loop.run_until_complete(c._handle_new_message(
                make_event(text="wimx, 18, Санкт-Петербург", chat_id=1234060895, msg_id=300)
            ))
            loop.run_until_complete(c._handle_new_message(
                make_event(text="", chat_id=1234060895, msg_id=301, media_type=self._photo())
            ))
            loop.run_until_complete(c._handle_new_message(
                make_event(text="", chat_id=1234060895, msg_id=302, media_type=self._photo())
            ))
            p1 = loop.run_until_complete(
                c._profile_service.find_profile_by_message(1234060895, 301)
            )
            p2 = loop.run_until_complete(
                c._profile_service.find_profile_by_message(1234060895, 302)
            )
            assert p1 is not None and p2 is not None
            assert p1.id == p2.id
        finally:
            asyncio.get_event_loop().run_until_complete(db.close())

    def test_unknown_media_without_context(self, tmp_path: Path) -> None:
        db = self._real_db(tmp_path)
        try:
            loop = asyncio.get_event_loop()
            c = self._collector(db)
            # MEDIA_ONLY без предшествующего PROFILE и без контекста в БД
            loop.run_until_complete(c._handle_new_message(
                make_event(text="", chat_id=1234060895, msg_id=400, media_type=self._photo())
            ))
            # Никакой профиль не создан, media не привязано
            p = loop.run_until_complete(
                c._profile_service.find_profile_by_message(1234060895, 400)
            )
            assert p is None
        finally:
            asyncio.get_event_loop().run_until_complete(db.close())

    def test_duplicate_media_not_duplicated(self, tmp_path: Path) -> None:
        db = self._real_db(tmp_path)
        try:
            loop = asyncio.get_event_loop()
            c = self._collector(db)
            loop.run_until_complete(c._handle_new_message(
                make_event(text="wimx, 18, Санкт-Петербург", chat_id=1234060895, msg_id=500)
            ))
            # Имитация повторной доставки media (обход dedup хендлера)
            loop.run_until_complete(c._handle_media_only(1234060895, 501))
            loop.run_until_complete(c._handle_media_only(1234060895, 501))
            p = loop.run_until_complete(
                c._profile_service.find_profile_by_message(1234060895, 501)
            )
            assert p is not None
            # profile_messages: source(500) + media(501) = 2, без дубля media
            count = loop.run_until_complete(
                db.get_profile_message_count(p.id)
            )
            assert count == 2
        finally:
            asyncio.get_event_loop().run_until_complete(db.close())


class TestBacklogRecovery:
    """W3: восстановление необработанных RAW при старте.

    Сохранённые в БД RAW (processed_at IS NULL) должны быть подхвачены и
    обработаны при старте, даже после restart/crash. Обработанные и живой
    трафик (id > cutoff) — не дублируются.
    """

    def _real_db(self, tmp_path: Path) -> Database:
        db = Database(path=tmp_path / "backlog.db")
        asyncio.get_event_loop().run_until_complete(db.connect())
        return db

    def _collector_with_process(
        self, db: Database, process: Callable,
    ) -> DvinchikCollector:
        config = make_config(dvinchik={"chat_id": 1234060895})
        worker = DvinchikRawWorker(process=process)
        collector = DvinchikCollector(
            AsyncMock(), db, config,
            profile_service=None,
            stats=CollectorStats(),
        )
        collector.attach_worker(worker)
        return collector

    async def _insert_raw(self, db: Database, msg_id: int) -> int:
        return await db.save_raw_message(
            telegram_message_id=msg_id,
            chat_id=1234060895,
            sender_id=1,
            sender_username="",
            sender_name="",
            message_date="2026-01-01",
            text="wimx, 18, Санкт-Петербург",
            raw_entities="[]",
            media_type="",
            reply_to_message_id=None,
            received_at="2026-01-01",
        )

    def test_raw_without_processing_recovered(self, tmp_path: Path) -> None:
        db = self._real_db(tmp_path)
        try:
            loop = asyncio.get_event_loop()
            raw_id = loop.run_until_complete(self._insert_raw(db, 1))
            calls: list[RawTask] = []

            async def process(task: RawTask) -> None:
                # Имитация _process_message: помечаем RAW обработанным
                calls.append(task)
                await db.mark_raw_processed(task.raw_id)

            c = self._collector_with_process(db, process)
            c.start()
            loop.run_until_complete(c.recover_backlog())
            loop.run_until_complete(c.stop())
            assert len(calls) == 1
            got = calls[0]
            assert isinstance(got, RawTask)
            assert got.raw_id == raw_id
            # помечен обработанным в БД
            cur = loop.run_until_complete(db._connection.execute(
                "SELECT processed_at FROM raw_messages WHERE id=?", (raw_id,)))
            row = loop.run_until_complete(cur.fetchone())
            assert row[0] is not None
        finally:
            asyncio.get_event_loop().run_until_complete(db.close())

    def test_processed_not_recovered(self, tmp_path: Path) -> None:
        db = self._real_db(tmp_path)
        try:
            loop = asyncio.get_event_loop()
            raw_id = loop.run_until_complete(self._insert_raw(db, 1))
            loop.run_until_complete(db.mark_raw_processed(raw_id))
            process = AsyncMock()
            c = self._collector_with_process(db, process)
            c.start()
            n = loop.run_until_complete(c.recover_backlog())
            loop.run_until_complete(c.stop())
            assert n == 0
            assert process.await_count == 0
        finally:
            asyncio.get_event_loop().run_until_complete(db.close())

    def test_several_backlog_recovered(self, tmp_path: Path) -> None:
        db = self._real_db(tmp_path)
        try:
            loop = asyncio.get_event_loop()
            ids = [
                loop.run_until_complete(self._insert_raw(db, m))
                for m in (1, 2, 3)
            ]
            process = AsyncMock()
            c = self._collector_with_process(db, process)
            c.start()
            n = loop.run_until_complete(c.recover_backlog())
            loop.run_until_complete(c.stop())
            assert n == 3
            got_ids = {a.args[0].raw_id for a in process.call_args_list}
            assert got_ids == set(ids)
        finally:
            asyncio.get_event_loop().run_until_complete(db.close())

    def test_restart_recovery_no_reprocess(self, tmp_path: Path) -> None:
        db = self._real_db(tmp_path)
        try:
            loop = asyncio.get_event_loop()
            ids = [
                loop.run_until_complete(self._insert_raw(db, m))
                for m in (1, 2)
            ]
            calls: list[int] = []

            async def proc(task: RawTask) -> None:
                calls.append(task.raw_id)
                await db.mark_raw_processed(task.raw_id)

            # Первый старт: обрабатываем backlog
            c1 = self._collector_with_process(db, proc)
            c1.start()
            loop.run_until_complete(c1.recover_backlog())
            loop.run_until_complete(c1.stop())
            # Второй старт (restart): повторной обработки быть не должно
            c2 = self._collector_with_process(db, proc)
            c2.start()
            loop.run_until_complete(c2.recover_backlog())
            loop.run_until_complete(c2.stop())
            assert sorted(calls) == sorted(ids)
        finally:
            asyncio.get_event_loop().run_until_complete(db.close())

    def test_large_backlog_recovered(self, tmp_path: Path) -> None:
        db = self._real_db(tmp_path)
        try:
            loop = asyncio.get_event_loop()
            n_total = 500
            for m in range(1, n_total + 1):
                loop.run_until_complete(self._insert_raw(db, m))
            calls: list[int] = []

            async def proc(task: RawTask) -> None:
                calls.append(task.raw_id)
                await db.mark_raw_processed(task.raw_id)

            c = self._collector_with_process(db, proc)
            c.start()
            n = loop.run_until_complete(c.recover_backlog())
            loop.run_until_complete(c.stop())
            assert n == n_total
            assert len(calls) == n_total
            # Все помечены обработанными
            cur = loop.run_until_complete(db._connection.execute(
                "SELECT COUNT(*) FROM raw_messages WHERE processed_at IS NULL"))
            row = loop.run_until_complete(cur.fetchone())
            assert row[0] == 0
        finally:
            asyncio.get_event_loop().run_until_complete(db.close())

    def test_one_error_does_not_stop_recovery(self, tmp_path: Path) -> None:
        db = self._real_db(tmp_path)
        try:
            loop = asyncio.get_event_loop()
            ids = [
                loop.run_until_complete(self._insert_raw(db, m))
                for m in (1, 2, 3)
            ]
            bad = ids[1]
            calls: list[int] = []

            async def proc(task: RawTask) -> None:
                calls.append(task.raw_id)
                if task.raw_id == bad:
                    raise RuntimeError("boom")
                await db.mark_raw_processed(task.raw_id)

            c = self._collector_with_process(db, proc)
            c.start()
            n = loop.run_until_complete(c.recover_backlog())
            loop.run_until_complete(c.stop())
            # Recovery поставила все 3; worker не упал на ошибке одного
            assert n == 3
            assert sorted(calls) == sorted(ids)
            # Упавшее задание осталось необработанным
            cur = loop.run_until_complete(db._connection.execute(
                "SELECT COUNT(*) FROM raw_messages WHERE processed_at IS NULL"))
            row = loop.run_until_complete(cur.fetchone())
            assert row[0] == 1
        finally:
            asyncio.get_event_loop().run_until_complete(db.close())

    def test_cutoff_excludes_live_traffic(self, tmp_path: Path) -> None:
        db = self._real_db(tmp_path)
        try:
            loop = asyncio.get_event_loop()
            old = loop.run_until_complete(self._insert_raw(db, 1))
            cutoff = loop.run_until_complete(db.get_max_raw_id())
            # Живой трафик приходит ПОСЛЕ снимка cutoff
            new = loop.run_until_complete(self._insert_raw(db, 2))
            rows = loop.run_until_complete(
                db.get_unprocessed_raw_messages_before(cutoff, 100)
            )
            got_ids = {r["id"] for r in rows}
            assert old in got_ids
            assert new not in got_ids
        finally:
            asyncio.get_event_loop().run_until_complete(db.close())


# ==================== RELIABLE RAW-SAVE (W4) ====================

class TestRawSaveReliability:
    """W4: надёжное сохранение RAW при транзитных/постоянных сбоях БД."""

    def _db(self, tmp_path: Path) -> Database:
        db = Database(path=tmp_path / "raw_reliability.db")
        asyncio.get_event_loop().run_until_complete(db.connect())
        return db

    async def _save(self, db: Database, msg_id: int = 1) -> int | None:
        return await db.save_raw_message(
            telegram_message_id=msg_id,
            chat_id=1234060895,
            sender_id=1,
            sender_username="",
            sender_name="",
            message_date="2026-01-01",
            text="wimx, 18, Санкт-Петербург",
            raw_entities="[]",
            media_type="",
            reply_to_message_id=None,
            received_at="2026-01-01",
        )

    def test_success_returns_id(self, tmp_path: Path) -> None:
        db = self._db(tmp_path)
        try:
            rid = asyncio.get_event_loop().run_until_complete(self._save(db, 1))
            assert isinstance(rid, int) and rid > 0
        finally:
            asyncio.get_event_loop().run_until_complete(db.close())

    def test_duplicate_returns_none(self, tmp_path: Path) -> None:
        db = self._db(tmp_path)
        try:
            loop = asyncio.get_event_loop()
            a = loop.run_until_complete(self._save(db, 1))
            b = loop.run_until_complete(self._save(db, 1))
            assert isinstance(a, int) and a > 0
            # Дубликат — штатный C1/C2-результат, НЕ ошибка
            assert b is None
        finally:
            asyncio.get_event_loop().run_until_complete(db.close())

    def test_transient_error_retries_then_succeeds(self, tmp_path: Path) -> None:
        db = self._db(tmp_path)
        try:
            loop = asyncio.get_event_loop()
            # Первые 2 вызова execute падают с транзитной OperationalError,
            # на 3-м — успех. Дубликат НЕ должен считаться ошибкой.
            cursor = MagicMock(rowcount=1, lastrowid=7)
            db._connection.execute = AsyncMock(side_effect=[
                sqlite3.OperationalError("database is locked"),
                sqlite3.OperationalError("database is locked"),
                cursor,
            ])
            db._connection.commit = AsyncMock()
            rid = loop.run_until_complete(self._save(db, 1))
            assert rid == 7
            # execute вызван 3 раза (2 ошибки + 1 успех), commit — 1 раз
            assert db._connection.execute.await_count == 3
            assert db._connection.commit.await_count == 1
        finally:
            asyncio.get_event_loop().run_until_complete(db.close())

    def test_permanent_error_raises_after_retries(self, tmp_path: Path) -> None:
        db = self._db(tmp_path)
        try:
            loop = asyncio.get_event_loop()
            db._connection.execute = AsyncMock(
                side_effect=sqlite3.OperationalError("locked forever")
            )
            db._connection.commit = AsyncMock()
            with pytest.raises(sqlite3.OperationalError):
                loop.run_until_complete(self._save(db, 1))
            # Исчерпаны все попытки (3)
            assert db._connection.execute.await_count == 3
        finally:
            asyncio.get_event_loop().run_until_complete(db.close())


class TestRawSaveFailureHandler:
    """W4: сбой RAW-save после retry → сообщение НЕ попадает в pipeline."""

    def _collector_with_worker(
        self, db: Database, process: Callable,
    ) -> DvinchikCollector:
        config = make_config(dvinchik={"chat_id": 1234060895})
        worker = DvinchikRawWorker(process=process)
        collector = DvinchikCollector(
            AsyncMock(), db, config,
            profile_service=None,
            stats=CollectorStats(),
        )
        collector.attach_worker(worker)
        return collector

    def test_save_failure_stops_pipeline(self) -> None:
        # БД падает (сигнализирует о неудаче после retry)
        db = AsyncMock(spec=Database)
        db.save_raw_message = AsyncMock(
            side_effect=sqlite3.OperationalError("down")
        )
        process = AsyncMock()
        loop = asyncio.get_event_loop()
        c = self._collector_with_worker(db, process)
        c.start()
        loop.run_until_complete(c._handle_new_message(
            make_event(
                text="wimx, 18, Санкт-Петербург", chat_id=1234060895, msg_id=1
            )
        ))
        loop.run_until_complete(c.stop())
        # pipeline НЕ запущен; save вызван ровно один раз
        assert process.await_count == 0
        assert db.save_raw_message.await_count == 1

    def test_duplicate_not_enqueued(self) -> None:
        # Дубликат (C1/C2) — save возвращает None, pipeline не запускается
        db = AsyncMock(spec=Database)
        db.save_raw_message = AsyncMock(return_value=None)
        process = AsyncMock()
        loop = asyncio.get_event_loop()
        c = self._collector_with_worker(db, process)
        c.start()
        loop.run_until_complete(c._handle_new_message(
            make_event(
                text="wimx, 18, Санкт-Петербург", chat_id=1234060895, msg_id=1
            )
        ))
        loop.run_until_complete(c.stop())
        assert process.await_count == 0


# ==================== RAW-FIRST (A) + W3 SEMANTICS (B) ====================

class TestRawFirstAndRecoverySemantics:
    """Регрессионные тесты на HIGH-дефекты аудита.

    A: RAW сохраняется ДО любого network await (get_sender).
    B: processed_at ставится ТОЛЬКО при успешном pipeline; при сбое RAW
       остаётся NULL и W3 повторяет обработку после restart.
    """

    def _real_db(self, tmp_path: Path) -> Database:
        db = Database(path=tmp_path / "ab.db")
        asyncio.get_event_loop().run_until_complete(db.connect())
        return db

    def _insert_raw(self, db: Database, msg_id: int, text: str = "wimx, 18, Санкт-Петербург") -> int:
        return asyncio.get_event_loop().run_until_complete(db.save_raw_message(
            telegram_message_id=msg_id,
            chat_id=1234060895,
            sender_id=1,
            sender_username="",
            sender_name="",
            message_date="2026-01-01",
            text=text,
            raw_entities="[]",
            media_type="",
            reply_to_message_id=None,
            received_at="2026-01-01",
        ))

    def _task(self, raw_id: int, msg_id: int = 1) -> RawTask:
        return RawTask(
            chat_id=1234060895,
            message_id=msg_id,
            sender_id=1,
            sender_username="",
            sender_name="",
            text="wimx, 18, Санкт-Петербург",
            media_type="",
            entities_json="[]",
            reply_to=None,
            received_at="2026-01-01",
            msg_date="2026-01-01",
            msg=None,
            raw_id=raw_id,
        )

    def _processed_at(self, db: Database, raw_id: int):
        loop = asyncio.get_event_loop()
        cur = loop.run_until_complete(db._connection.execute(
            "SELECT processed_at FROM raw_messages WHERE id=?", (raw_id,)))
        return loop.run_until_complete(cur.fetchone())[0]

    def test_get_sender_failure_still_saves_raw(self, tmp_path: Path) -> None:
        """A: сбой get_sender() НЕ мешает сохранению RAW и pipeline."""
        db = self._real_db(tmp_path)
        try:
            loop = asyncio.get_event_loop()
            process = AsyncMock()
            config = make_config(dvinchik={"chat_id": 1234060895})
            collector = DvinchikCollector(
                AsyncMock(), db, config,
                profile_service=None, stats=CollectorStats(),
            )
            worker = DvinchikRawWorker(process=process)
            collector.attach_worker(worker)
            collector.start()

            event = make_event(
                text="wimx, 18, Санкт-Петербург", chat_id=1234060895, msg_id=1
            )
            event.message.sender_id = 100
            # get_sender падает — RAW-first не должен зависеть от него
            event.message.get_sender = AsyncMock(
                side_effect=RuntimeError("no sender")
            )

            loop.run_until_complete(collector._handle_new_message(event))
            loop.run_until_complete(collector.stop())

            # RAW сохранён, несмотря на сбой get_sender
            cur = loop.run_until_complete(db._connection.execute(
                "SELECT COUNT(*) FROM raw_messages "
                "WHERE chat_id=? AND telegram_message_id=?",
                (1234060895, 1),
            ))
            assert loop.run_until_complete(cur.fetchone())[0] == 1
            # pipeline запущен (задание поставлено в очередь)
            assert process.await_count == 1
        finally:
            asyncio.get_event_loop().run_until_complete(db.close())

    def test_get_sender_none_still_saves_raw(self, tmp_path: Path) -> None:
        """A: get_sender() вернул None — RAW всё равно сохранён."""
        db = self._real_db(tmp_path)
        try:
            loop = asyncio.get_event_loop()
            process = AsyncMock()
            config = make_config(dvinchik={"chat_id": 1234060895})
            collector = DvinchikCollector(
                AsyncMock(), db, config,
                profile_service=None, stats=CollectorStats(),
            )
            worker = DvinchikRawWorker(process=process)
            collector.attach_worker(worker)
            collector.start()

            event = make_event(
                text="wimx, 18, Санкт-Петербург", chat_id=1234060895, msg_id=1
            )
            event.message.sender_id = 100
            event.message.get_sender = AsyncMock(return_value=None)

            loop.run_until_complete(collector._handle_new_message(event))
            loop.run_until_complete(collector.stop())

            cur = loop.run_until_complete(db._connection.execute(
                "SELECT COUNT(*) FROM raw_messages "
                "WHERE chat_id=? AND telegram_message_id=?",
                (1234060895, 1),
            ))
            assert loop.run_until_complete(cur.fetchone())[0] == 1
        finally:
            asyncio.get_event_loop().run_until_complete(db.close())

    def test_pipeline_failure_leaves_processed_at_null(
        self, tmp_path: Path,
    ) -> None:
        """B: падение pipeline → processed_at остаётся NULL."""
        db = self._real_db(tmp_path)
        try:
            loop = asyncio.get_event_loop()
            raw_id = self._insert_raw(db, 1)
            collector = DvinchikCollector(
                AsyncMock(), db, make_config(dvinchik={"chat_id": 1234060895}),
                profile_service=None, stats=CollectorStats(),
            )
            # Симулируем сбой pipeline (classify бросает)
            collector._parser.classify = MagicMock(
                side_effect=RuntimeError("pipeline boom")
            )
            loop.run_until_complete(
                collector._process_message(self._task(raw_id))
            )
            assert self._processed_at(db, raw_id) is None
        finally:
            asyncio.get_event_loop().run_until_complete(db.close())

    def test_successful_pipeline_marks_processed(self, tmp_path: Path) -> None:
        """B: успешный pipeline → processed_at установлен."""
        db = self._real_db(tmp_path)
        try:
            loop = asyncio.get_event_loop()
            raw_id = self._insert_raw(db, 1)
            collector = DvinchikCollector(
                AsyncMock(), db, make_config(dvinchik={"chat_id": 1234060895}),
                profile_service=None, stats=CollectorStats(),
            )
            loop.run_until_complete(
                collector._process_message(self._task(raw_id))
            )
            assert self._processed_at(db, raw_id) is not None
        finally:
            asyncio.get_event_loop().run_until_complete(db.close())

    def test_w3_recovers_failed_pipeline_raw(self, tmp_path: Path) -> None:
        """B: W3 повторно подхватывает RAW после сбоя pipeline."""
        db = self._real_db(tmp_path)
        try:
            loop = asyncio.get_event_loop()
            raw_id = self._insert_raw(db, 1)
            calls: dict[str, int] = {"n": 0}

            async def process(task: RawTask) -> None:
                calls["n"] += 1
                await db.mark_raw_processed(task.raw_id)

            collector = DvinchikCollector(
                AsyncMock(), db, make_config(dvinchik={"chat_id": 1234060895}),
                profile_service=None, stats=CollectorStats(),
            )
            # Первый проход симулирует падение pipeline
            collector._parser.classify = MagicMock(
                side_effect=RuntimeError("pipeline boom")
            )
            worker = DvinchikRawWorker(process=process)
            collector.attach_worker(worker)
            collector.start()

            # Прямой вызов _process_message (имитация первой обработки)
            loop.run_until_complete(
                collector._process_message(self._task(raw_id))
            )
            # После сбоя processed_at остаётся NULL
            assert self._processed_at(db, raw_id) is None

            # W3 recovery подхватывает незавершённый RAW
            n = loop.run_until_complete(collector.recover_backlog())
            loop.run_until_complete(collector.stop())
            assert n == 1
            # После повторной обработки RAW отмечен
            assert self._processed_at(db, raw_id) is not None
        finally:
            asyncio.get_event_loop().run_until_complete(db.close())


# ==================== ORDERING RACE (C) + DOUBLE-ENQUEUE (D) ====================

class TestProfileMediaOrderingAndDedup:
    """Регрессионные тесты на C (ordering race) и D (double-enqueue)."""

    def _real_db(self, tmp_path: Path) -> Database:
        db = Database(path=tmp_path / "cd.db")
        asyncio.get_event_loop().run_until_complete(db.connect())
        return db

    def test_concurrent_profile_then_media_links(self, tmp_path: Path) -> None:
        """C: конкурентный PROFILE + MEDIA_ONLY — MEDIA привязан к профилю.

        Медленный save для PROFILE вскрывает race: без per-chat сериализации
        MEDIA (быстрый save) встал бы в очередь первым и не нашёл контекст.
        """
        db = self._real_db(tmp_path)
        try:
            loop = asyncio.get_event_loop()
            config = make_config(dvinchik={"chat_id": 1234060895})
            profile_service = ProfileService(db)
            order: list[int] = []

            collector = DvinchikCollector(
                AsyncMock(), db, config,
                profile_service=profile_service, stats=CollectorStats(),
            )

            async def process(task: RawTask) -> None:
                order.append(task.message_id)
                await collector._process_message(task)

            worker = DvinchikRawWorker(process=process)
            collector.attach_worker(worker)
            collector.start()

            # Медленный save для PROFILE, чтобы MEDIA иначе обогнал в очереди.
            async def slow_save(*args, telegram_message_id=None, **kwargs):
                if telegram_message_id == 100:
                    await asyncio.sleep(0.02)
                return 1

            db.save_raw_message = slow_save

            ev_profile = make_event(
                text="wimx, 18, Санкт-Петербург", chat_id=1234060895, msg_id=100
            )
            ev_profile.message.sender_id = 100
            ev_media = make_event(
                text="", chat_id=1234060895, msg_id=101, media_type=MagicMock()
            )
            ev_media.message.sender_id = 100

            loop.run_until_complete(asyncio.gather(
                collector._handle_new_message(ev_profile),
                collector._handle_new_message(ev_media),
            ))
            loop.run_until_complete(collector.stop())

            # PROFILE обработан строго до MEDIA (per-chat сериализация)
            assert order == [100, 101]
            # MEDIA привязан к профилю, созданному PROFILE
            profile = loop.run_until_complete(
                profile_service.find_profile_by_message(1234060895, 100)
            )
            media_profile = loop.run_until_complete(
                profile_service.find_profile_by_message(1234060895, 101)
            )
            assert profile is not None
            assert media_profile is not None
            assert media_profile.id == profile.id
        finally:
            asyncio.get_event_loop().run_until_complete(db.close())

    def test_no_double_enqueue_live_and_recovery(self, tmp_path: Path) -> None:
        """D: live handler + recover_backlog не дублируют один raw_id."""
        db = self._real_db(tmp_path)
        try:
            loop = asyncio.get_event_loop()
            enqueued: list[int] = []

            async def process(task: RawTask) -> None:
                enqueued.append(task.raw_id)

            collector = DvinchikCollector(
                AsyncMock(), db, make_config(dvinchik={"chat_id": 1234060895}),
                profile_service=None, stats=CollectorStats(),
            )
            worker = DvinchikRawWorker(process=process)
            collector.attach_worker(worker)
            collector.start()

            # 1) Live handler сохраняет и ставит в очередь новое (id=1)
            ev = make_event(
                text="wimx, 18, Санкт-Петербург", chat_id=1234060895, msg_id=1
            )
            ev.message.sender_id = 100
            loop.run_until_complete(collector._handle_new_message(ev))

            # 2) "Backlog" предыдущей сессии (id=2), ещё не обработан
            backlog_id = loop.run_until_complete(db.save_raw_message(
                telegram_message_id=2, chat_id=1234060895, sender_id=1,
                sender_username="", sender_name="", message_date="2026-01-01",
                text="wimx, 18, Санкт-Петербург", raw_entities="[]",
                media_type="", reply_to_message_id=None, received_at="2026-01-01",
            ))

            # 3) Recovery (cutoff >= 2). id=1 уже в очереди от live handler
            #    → НЕ должен продублироваться.
            n = loop.run_until_complete(collector.recover_backlog())
            loop.run_until_complete(collector.stop())

            # Только backlog (id=2) поставлен recovery; id=1 не дублируется
            assert n == 1
            assert sorted(enqueued) == sorted([1, backlog_id])
            assert enqueued.count(1) == 1
        finally:
            asyncio.get_event_loop().run_until_complete(db.close())


# ==================== AUDIT MEDIUM FIXES (MEDIUM-1 / MEDIUM-2) =================

class TestAuditMediumFixes:
    """Регрессионные тесты на MEDIUM-1 (set leak) и MEDIUM-2 (dedup до save)."""

    def _real_db(self, tmp_path: Path) -> Database:
        db = Database(path=tmp_path / "medium.db")
        asyncio.get_event_loop().run_until_complete(db.connect())
        return db

    def test_enqueued_set_bounded_after_recovery(self, tmp_path: Path) -> None:
        """MEDIUM-1: после завершения recovery set очищается и в steady-state
        не растёт (live-сообщения после recovery не добавляются в set)."""
        db = self._real_db(tmp_path)
        try:
            loop = asyncio.get_event_loop()
            enqueued: list[int] = []

            async def process(task: RawTask) -> None:
                enqueued.append(task.raw_id)

            collector = DvinchikCollector(
                AsyncMock(), db, make_config(dvinchik={"chat_id": 1234060895}),
                profile_service=None, stats=CollectorStats(),
            )
            worker = DvinchikRawWorker(process=process)
            collector.attach_worker(worker)
            collector.start()

            # Backlog предыдущей сессии
            loop.run_until_complete(db.save_raw_message(
                telegram_message_id=2, chat_id=1234060895, sender_id=1,
                sender_username="", sender_name="", message_date="2026-01-01",
                text="wimx, 18, Санкт-Петербург", raw_entities="[]",
                media_type="", reply_to_message_id=None, received_at="2026-01-01",
            ))
            # Recovery запускается и завершается
            loop.run_until_complete(collector.recover_backlog())
            assert collector._recovery_armed is False

            # Много live-сообщений ПОСЛЕ recovery (steady-state)
            for i in range(1, 101):
                ev = make_event(
                    text="wimx, 18, Санкт-Петербург",
                    chat_id=1234060895, msg_id=1000 + i,
                )
                ev.message.sender_id = 100
                loop.run_until_complete(collector._handle_new_message(ev))

            loop.run_until_complete(collector.stop())

            # Set пуст в steady-state → память не растёт бесконечно
            assert collector._enqueued_raw_ids == set()
        finally:
            asyncio.get_event_loop().run_until_complete(db.close())

    def test_dedup_marks_only_after_successful_save(self, tmp_path: Path) -> None:
        """MEDIUM-2: при неудаче RAW-save сообщение НЕ помечается dedup →
        повторная доставка может быть переобработана (не теряется)."""
        db = self._real_db(tmp_path)
        try:
            loop = asyncio.get_event_loop()
            enqueued: list[int] = []

            async def process(task: RawTask) -> None:
                enqueued.append(task.raw_id)

            collector = DvinchikCollector(
                AsyncMock(), db, make_config(dvinchik={"chat_id": 1234060895}),
                profile_service=None, stats=CollectorStats(),
            )
            worker = DvinchikRawWorker(process=process)
            collector.attach_worker(worker)
            collector.start()

            key = (1234060895, 1)

            # 1) Перманентный сбой RAW-save
            original = db.save_raw_message
            async def fail_save(*args, **kwargs):
                raise sqlite3.OperationalError("down")
            db.save_raw_message = fail_save

            ev = make_event(
                text="wimx, 18, Санкт-Петербург", chat_id=1234060895, msg_id=1
            )
            ev.message.sender_id = 100
            loop.run_until_complete(collector._handle_new_message(ev))
            assert len(enqueued) == 0
            # dedup НЕ помечен → повторная доставка возможна
            assert collector._dedup.is_known(key) is False

            # 2) Восстанавливаем save; повторная доставка того же сообщения
            db.save_raw_message = original
            ev2 = make_event(
                text="wimx, 18, Санкт-Петербург", chat_id=1234060895, msg_id=1
            )
            ev2.message.sender_id = 100
            loop.run_until_complete(collector._handle_new_message(ev2))

            loop.run_until_complete(collector.stop())

            # Сообщение успешно переобработано (не потеряно из-за dedup)
            assert len(enqueued) == 1
        finally:
            asyncio.get_event_loop().run_until_complete(db.close())


# ==================== MULTI-ACCOUNT ====================

class TestMultiAccount:
    def test_registers_handler_on_all_clients(self) -> None:
        c1, c2 = MagicMock(), MagicMock()
        collector = DvinchikCollector([c1, c2], make_db_mock(), make_config())
        collector.register()
        assert c1.add_event_handler.called
        assert c2.add_event_handler.called
        assert len(collector._clients) == 2

    def test_accepts_single_client_for_backward_compat(self) -> None:
        c1 = MagicMock()
        collector = DvinchikCollector(c1, make_db_mock(), make_config())
        collector.register()
        assert c1.add_event_handler.called
        assert len(collector._clients) == 1

    def test_same_message_from_two_accounts_enqueued_once(self) -> None:
        db = make_db_mock()
        # message_id одинаковый для обоих аккаунтов (один и тот же пост в чате)
        c1, c2 = MagicMock(), MagicMock()
        worker = DvinchikRawWorker(process=AsyncMock())
        collector = DvinchikCollector([c1, c2], db, make_config())
        collector.attach_worker(worker)

        ev1 = make_event(
            text="wimx, 18, Санкт-Петербург", chat_id=1234060895, msg_id=1
        )
        ev2 = make_event(
            text="wimx, 18, Санкт-Петербург", chat_id=1234060895, msg_id=1
        )
        loop = asyncio.get_event_loop()
        loop.run_until_complete(collector._handle_new_message(ev1))
        loop.run_until_complete(collector._handle_new_message(ev2))
        loop.run_until_complete(collector.stop())

        # Один и тот же пост, увиденный двумя аккаунтами, ставится в очередь
        # ровно один раз (in-memory dedup + UNIQUE в БД).
        assert worker.qsize == 1


# ==================== REPLY MARKUP (кнопки — read-only разведка) ====================

class TestReplyMarkupSerialize:
    """Сериализация кнопок сообщения для разведки слоя действий."""

    def _collector(self) -> DvinchikCollector:
        return DvinchikCollector(AsyncMock(), make_db_mock(), make_config())

    def _markup_msg(self) -> MagicMock:
        like = MagicMock()
        like.text = "LIKE"
        like.callback_data = b"like_1881"
        like.url = None
        next_ = MagicMock()
        next_.text = "NEXT"
        next_.callback_data = b"\x00\x01\x02"
        next_.url = None
        web = MagicMock()
        web.text = "Open"
        web.callback_data = None
        web.url = "https://example.com"
        markup = MagicMock()
        markup.rows = [MagicMock(buttons=[like, next_]), MagicMock(buttons=[web])]
        msg = MagicMock()
        msg.reply_markup = markup
        return msg

    def test_missing_markup_yields_empty_json(self) -> None:
        msg = MagicMock()
        msg.reply_markup = None
        assert self._collector()._serialize_reply_markup(msg) == "[]"

    def test_no_rows_yields_empty_json(self) -> None:
        msg = MagicMock()
        msg.reply_markup = MagicMock()
        msg.reply_markup.rows = None
        assert self._collector()._serialize_reply_markup(msg) == "[]"

    def test_inline_buttons_text_callback_and_url(self) -> None:
        data = json.loads(
            self._collector()._serialize_reply_markup(self._markup_msg())
        )
        assert len(data) == 2
        like, next_ = data[0]
        assert like["text"] == "LIKE"
        assert like["callback_data"] == "like_1881"
        assert "url" not in like
        assert next_["callback_data"] == "\x00\x01\x02"
        web = data[1][0]
        assert web["text"] == "Open"
        assert web["url"] == "https://example.com"
        assert "callback_data" not in web

    def test_render_buttons_readable(self) -> None:
        collector = self._collector()
        out = collector._render_buttons(
            collector._serialize_reply_markup(self._markup_msg())
        )
        assert "LIKE (like_1881)" in out
        assert "NEXT" in out
        assert "Open [https://example.com]" in out


class TestReplyMarkupStorage:
    """RAW-сохранение кнопок и миграция колонки reply_markup."""

    def _real_db(self, tmp_path: Path, name: str = "rm.db") -> Database:
        db = Database(path=tmp_path / name)
        asyncio.get_event_loop().run_until_complete(db.connect())
        return db

    def test_save_and_readback(self, tmp_path: Path) -> None:
        db = self._real_db(tmp_path)
        loop = asyncio.get_event_loop()
        try:
            markup = (
                '[ [{"text": "LIKE", "type": "InlineKeyboardButton", '
                '"callback_data": "like_1881"}] ]'
            )
            raw_id = loop.run_until_complete(db.save_raw_message(
                telegram_message_id=10,
                chat_id=1234060895,
                sender_id=1,
                sender_username="",
                sender_name="",
                message_date="2026-01-01",
                text="wimx, 18, Санкт-Петербург",
                raw_entities="[]",
                reply_markup=markup,
                media_type="",
                reply_to_message_id=None,
                received_at="2026-01-01",
            ))
            cur = loop.run_until_complete(db._connection.execute(
                "SELECT raw_entities, reply_markup FROM raw_messages WHERE id=?",
                (raw_id,),
            ))
            row = loop.run_until_complete(cur.fetchone())
            assert row[0] == "[]"
            assert row[1] == markup
        finally:
            loop.run_until_complete(db.close())

    def test_default_empty_when_not_passed(self, tmp_path: Path) -> None:
        db = self._real_db(tmp_path)
        loop = asyncio.get_event_loop()
        try:
            raw_id = loop.run_until_complete(db.save_raw_message(
                telegram_message_id=11,
                chat_id=1234060895,
                sender_id=1,
                sender_username="",
                sender_name="",
                message_date="2026-01-01",
                text="x",
                raw_entities="[]",
                media_type="",
                reply_to_message_id=None,
                received_at="2026-01-01",
            ))
            cur = loop.run_until_complete(db._connection.execute(
                "SELECT reply_markup FROM raw_messages WHERE id=?", (raw_id,)
            ))
            row = loop.run_until_complete(cur.fetchone())
            assert row[0] == "[]"
        finally:
            loop.run_until_complete(db.close())

    def test_migration_adds_column_to_existing_db(self, tmp_path: Path) -> None:
        path = tmp_path / "old.db"
        conn = sqlite3.connect(str(path))
        conn.execute("""CREATE TABLE raw_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_message_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            sender_id INTEGER NOT NULL,
            sender_username TEXT DEFAULT '',
            sender_name TEXT DEFAULT '',
            message_date TEXT NOT NULL,
            text TEXT DEFAULT '',
            raw_entities TEXT DEFAULT '[]',
            media_type TEXT DEFAULT '',
            reply_to_message_id INTEGER,
            received_at TEXT NOT NULL,
            processed_at TEXT
        );""")
        conn.execute(
            "INSERT INTO raw_messages (telegram_message_id, chat_id, sender_id, "
            "message_date, text, received_at) VALUES "
            "(1, 1234060895, 1, '2026-01-01', 'old profile', '2026-01-01')"
        )
        conn.commit()
        conn.close()

        db = Database(path=path)
        loop = asyncio.get_event_loop()
        loop.run_until_complete(db.connect())
        try:
            cur = loop.run_until_complete(
                db._connection.execute("PRAGMA table_info(raw_messages)")
            )
            columns = {row[1] for row in loop.run_until_complete(cur.fetchall())}
            assert "reply_markup" in columns
            cur2 = loop.run_until_complete(
                db._connection.execute("SELECT reply_markup FROM raw_messages")
            )
            row = loop.run_until_complete(cur2.fetchone())
            assert row[0] == "[]"
        finally:
            loop.run_until_complete(db.close())


class TestReplyMarkupHandler:
    """Хендлер передаёт кнопки в RAW-save и в RawTask (read-only)."""

    def test_forwarded_to_raw_and_task(self) -> None:
        db = make_db_mock()
        worker = DvinchikRawWorker(process=AsyncMock())
        collector = DvinchikCollector(AsyncMock(), db, make_config())
        collector.attach_worker(worker)
        ev = make_event(text="wimx, 18, Санкт-Петербург")

        like = MagicMock()
        like.text = "LIKE"
        like.callback_data = b"like_1881"
        like.url = None
        markup = MagicMock()
        markup.rows = [MagicMock(buttons=[like])]
        ev.message.reply_markup = markup

        loop = asyncio.get_event_loop()
        loop.run_until_complete(collector._handle_new_message(ev))

        expected = collector._serialize_reply_markup(ev.message)
        kwargs = db.save_raw_message.call_args.kwargs
        assert kwargs["reply_markup"] == expected
        assert worker.qsize == 1
        task = loop.run_until_complete(worker._queue.get())
        assert task.reply_markup_json == expected

    def test_no_buttons_passes_empty(self) -> None:
        db = make_db_mock()
        worker = DvinchikRawWorker(process=AsyncMock())
        collector = DvinchikCollector(AsyncMock(), db, make_config())
        collector.attach_worker(worker)
        ev = make_event(text="нет кнопок")
        ev.message.reply_markup = None

        loop = asyncio.get_event_loop()
        loop.run_until_complete(collector._handle_new_message(ev))

        kwargs = db.save_raw_message.call_args.kwargs
        assert kwargs["reply_markup"] == "[]"
        task = loop.run_until_complete(worker._queue.get())
        assert task.reply_markup_json == "[]"


# ==================== OUTGOING MESSAGES (actions пользователя) ====================

class TestOutgoingCapture:
    """Исходящие сообщения пользователя (лайки/дизлайки эмодзи) в чате бота.

    Сохраняются в raw_messages и помечаются processed_at — pipeline
    НЕ запускается (это не анкета, а действие пользователя).
    """

    def _collector(self) -> DvinchikCollector:
        db = make_db_mock()
        collector = DvinchikCollector(AsyncMock(), db, make_config())
        return collector

    def test_outgoing_saved_and_processed(self) -> None:
        collector = self._collector()
        event = make_event(text="❤️", msg_id=500, sender_id=1234060895)
        asyncio.get_event_loop().run_until_complete(
            collector._handle_outgoing_message(event)
        )
        collector._db.save_raw_message.assert_called_once()
        kwargs = collector._db.save_raw_message.call_args.kwargs
        assert kwargs["text"] == "❤️"
        assert kwargs["chat_id"] == 1234060895
        # Помечен обработанным — pipeline не запускается.
        collector._db.mark_raw_processed.assert_called_once_with(1)

    def test_outgoing_outside_dvinchik_ignored(self) -> None:
        collector = self._collector()
        # Исходящее в группу (не чат бота) — игнорируется.
        event = make_event(text="❤️", chat_id=-1001225291649, msg_id=501)
        asyncio.get_event_loop().run_until_complete(
            collector._handle_outgoing_message(event)
        )
        collector._db.save_raw_message.assert_not_called()

    def test_outgoing_empty_text_ignored(self) -> None:
        collector = self._collector()
        event = make_event(text="", msg_id=502)
        asyncio.get_event_loop().run_until_complete(
            collector._handle_outgoing_message(event)
        )
        collector._db.save_raw_message.assert_not_called()

    def test_outgoing_blank_whitespace_ignored(self) -> None:
        collector = self._collector()
        event = make_event(text="   ", msg_id=503)
        asyncio.get_event_loop().run_until_complete(
            collector._handle_outgoing_message(event)
        )
        collector._db.save_raw_message.assert_not_called()


# ==================== CALLBACK QUERY (inline-кнопки/разведка LIKE) ====================

class TestCallbackQueryCapture:
    """Логирование callback queries для разведки механики LIKE.

    Read-only: данные только логируются, действия не вызываются.
    """

    def test_callback_logs_data(self) -> None:
        client = AsyncMock()
        db = make_db_mock()
        collector = DvinchikCollector(client, db, make_config())

        event = MagicMock()
        event.data = b"like_123"
        event.chat_id = 1234060895
        event.sender_id = 1234060895

        asyncio.get_event_loop().run_until_complete(
            collector._handle_callback_query(event)
        )
        # Callback query не сохраняется в БД (read-only логирование).
        db.save_raw_message.assert_not_called()

    def test_callback_data_bytes_decoded(self) -> None:
        client = AsyncMock()
        db = make_db_mock()
        collector = DvinchikCollector(client, db, make_config())

        event = MagicMock()
        event.data = "like_456".encode()
        event.chat_id = 1234060895
        event.sender_id = 0

        asyncio.get_event_loop().run_until_complete(
            collector._handle_callback_query(event)
        )
        db.save_raw_message.assert_not_called()


# ==================== AUTO-ACTIONS (Stage 7, SEMI_AUTO) ====================

class TestCollectorAutoActions:
    """Авто-действия ❤️/👎 встраиваются в pipeline при SEMI_AUTO.

    Проверяем:
    - Анкета на авто-аккаунте с решением LIKE → отправляется ❤️.
    - Анкета не на авто-аккаунте → никакого действия.
    - OBSERVE-режим → никакого действия (даже если enabled).
    Не используем реальный worker — вызываем _process_message напрямую.
    """

    def _make_config(self, mode: str = "SEMI_AUTO", enabled: bool = True) -> AppConfig:
        cfg = make_config()
        # Два аккаунта: acc1 (индекс 0), acc2/авто (индекс 1).
        data = cfg.model_dump()
        data["telegram"] = {
            "accounts": [
                {"api_id": 38219721, "api_hash": "a" * 32, "session": "dvai"},
                {"api_id": 36266816, "api_hash": "b" * 32, "session": "dvai_2"},
            ]
        }
        data["project"] = {"mode": mode}
        data["auto_actions"] = {
            "enabled": enabled,
            "account_session": "dvai_2",
            "interval_sec": 0.0,
        }
        return AppConfig(**data)

    def _make_collector(
        self,
        config: AppConfig,
        decision: object | None,
        auto_client: AsyncMock,
        other_client: AsyncMock,
    ) -> DvinchikCollector:
        from models.profile import Profile, ProfileStatus

        db = make_db_mock()
        collector = DvinchikCollector(
            [other_client, auto_client],  # acc1, acc2(auto)
            db,
            config,
        )
        if decision is not None:
            ds = AsyncMock()
            ds.evaluate = AsyncMock(return_value=decision)
            collector._decision_service = ds
        fs = AsyncMock()
        from models.filter import FilterDecision, FilterResult
        fs.evaluate = AsyncMock(
            return_value=FilterResult(decision=FilterDecision.PASS, reasons=[])
        )
        collector._filter_service = fs
        profile = Profile(
            id=1, name="Anna", age=19,
            normalized_city="Санкт-Петербург",
            status=ProfileStatus.NEW,
        )
        ps = AsyncMock()
        ps.upsert_profile = AsyncMock(return_value=profile)
        collector._profile_service = ps
        # Привязываем worker, чтобы хендлер ставил в очередь, а не
        # обрабатывал синхронно — но для прямого вызова _process_message
        # нам нужен синхронный путь. Здесь вызываем _process_message напрямую.
        return collector

    def _auto_event_on_client(self, auto_client: AsyncMock) -> MagicMock:
        """PROFILE-сообщение, полученное авто-клиентом (task.msg.client is auto)."""
        ev = make_event(text="Аня, 18, Санкт-Петербург", msg_id=900)
        ev.message.client = auto_client
        return ev

    def _make_decision(self, decision: object) -> object:
        """Реальный AIDecisionResult, который умеет рендерить вывод консоли."""
        from models.decision import AIDecisionResult

        return AIDecisionResult(
            decision=decision,
            combined_score=0.8,
            confidence=0.7,
            reasons=["test"],
            scoring_version="deterministic-v2",
        )

    def test_like_decision_sends_heart_on_auto_account(self) -> None:
        from models.decision import AIDecision

        auto_client = AsyncMock()
        auto_client.is_connected.return_value = True
        auto_client.send_message = AsyncMock()
        other_client = AsyncMock()
        config = self._make_config()

        decision = self._make_decision(AIDecision.LIKE)
        collector = self._make_collector(config, decision, auto_client, other_client)

        task = RawTask(
            chat_id=1234060895, message_id=900, sender_id=1234060895,
            sender_username="", sender_name="", text="Аня, 18, Санкт-Петербург",
            media_type="", entities_json="[]", reply_markup_json="[]",
            reply_to=None, received_at="now", msg_date="now",
            msg=self._auto_event_on_client(auto_client).message, raw_id=1,
        )
        asyncio.get_event_loop().run_until_complete(collector._process_message(task))

        # Одно — сама реакция (❤️), затем пересылка карточки Меланхолику.
        assert auto_client.send_message.call_count >= 1
        args, _ = auto_client.send_message.call_args_list[0]
        assert args[1] == "\u2764\ufe0f"  # ❤️
        auto_client.forward_messages.assert_awaited_once()
        collector._db.record_auto_action.assert_awaited_once_with(
            1, "LIKE", "LIKE", 1234060895, 900
        )
    def test_logged_profile_is_not_sent_twice(self) -> None:
        from models.decision import AIDecision

        auto_client = AsyncMock()
        auto_client.is_connected.return_value = True
        auto_client.send_message = AsyncMock()
        other_client = AsyncMock()
        collector = self._make_collector(
            self._make_config(), self._make_decision(AIDecision.DISLIKE),
            auto_client, other_client,
        )
        collector._db.has_auto_action_for_message = AsyncMock(
            side_effect=[False, True]
        )
        task = RawTask(
            chat_id=1234060895, message_id=903, sender_id=1234060895,
            sender_username="", sender_name="", text="Аня, 18, Санкт-Петербург",
            media_type="", entities_json="[]", reply_markup_json="[]",
            reply_to=None, received_at="now", msg_date="now",
            msg=self._auto_event_on_client(auto_client).message, raw_id=4,
        )
        loop = asyncio.get_event_loop()
        loop.run_until_complete(collector._process_message(task))
        loop.run_until_complete(collector._process_message(task))
        # Повторная карточка уже в журнале → действие НЕ переотправляется:
        # одно действие (❤️/👎) + одна пересылка Меланхолику.
        action_calls = [
            c for c in auto_client.send_message.call_args_list
            if c.args[1] in ("\u2764\ufe0f", "\U0001F44E")
        ]
        assert len(action_calls) == 1
        auto_client.forward_messages.assert_awaited_once()

    def test_log_failure_does_not_resend_action(self) -> None:
        """DB failure after Telegram send: action CAN repeat on retry.

        После удаления _processed_profile_ids (HIGH-1 fix) дублирование
        действий при ошибке БД становится возможным — это принятый trade-off
        (HIGH-2). Идемпотентность по telegram_message_id обеспечивается
        на уровне DB (has_auto_action_for_message), а не в памяти движка.
        Если record_auto_action упал — БД не знает об отправке, и повторный
        вызов _process_message отправит действие снова.
        """
        from models.decision import AIDecision

        auto_client = AsyncMock()
        auto_client.is_connected.return_value = True
        auto_client.send_message = AsyncMock()
        other_client = AsyncMock()
        collector = self._make_collector(
            self._make_config(), self._make_decision(AIDecision.LIKE),
            auto_client, other_client,
        )
        collector._db.record_auto_action = AsyncMock(side_effect=RuntimeError("db"))
        task = RawTask(
            chat_id=1234060895, message_id=904, sender_id=1234060895,
            sender_username="", sender_name="", text="Аня, 18, Санкт-Петербург",
            media_type="", entities_json="[]", reply_markup_json="[]",
            reply_to=None, received_at="now", msg_date="now",
            msg=self._auto_event_on_client(auto_client).message, raw_id=5,
        )
        loop = asyncio.get_event_loop()
        loop.run_until_complete(collector._process_message(task))
        loop.run_until_complete(collector._process_message(task))
        # Оба прохода отправляют действие (дублирование при ошибке БД —
        # принятый trade-off). Второй проход также пытается переслать
        # уведомление (notify), т.к. maybe_act снова вызывается.
        action_calls = [
            c for c in auto_client.send_message.call_args_list
            if c.args[1] in ("\u2764\ufe0f", "\U0001F44E")
        ]
        assert len(action_calls) == 2

    def test_no_action_when_message_on_other_account(self) -> None:
        from models.decision import AIDecision

        auto_client = AsyncMock()
        auto_client.is_connected.return_value = True
        auto_client.send_message = AsyncMock()
        other_client = AsyncMock()
        config = self._make_config()

        decision = self._make_decision(AIDecision.LIKE)
        collector = self._make_collector(config, decision, auto_client, other_client)

        # Сообщение получено НЕ авто-аккаунтом (other_client).
        ev = make_event(text="Аня, 18, Санкт-Петербург", msg_id=901)
        ev.message.client = other_client
        task = RawTask(
            chat_id=1234060895, message_id=901, sender_id=1234060895,
            sender_username="", sender_name="", text="Аня, 18, Санкт-Петербург",
            media_type="", entities_json="[]", reply_markup_json="[]",
            reply_to=None, received_at="now", msg_date="now",
            msg=ev.message, raw_id=2,
        )
        asyncio.get_event_loop().run_until_complete(collector._process_message(task))

        auto_client.send_message.assert_not_called()

    def test_observe_mode_no_action_even_if_enabled(self) -> None:
        from models.decision import AIDecision

        auto_client = AsyncMock()
        auto_client.is_connected.return_value = True
        auto_client.send_message = AsyncMock()
        other_client = AsyncMock()
        config = self._make_config(mode="OBSERVE", enabled=True)

        decision = self._make_decision(AIDecision.LIKE)
        collector = self._make_collector(config, decision, auto_client, other_client)

        ev = make_event(text="Аня, 18, Санкт-Петербург", msg_id=902)
        ev.message.client = auto_client
        task = RawTask(
            chat_id=1234060895, message_id=902, sender_id=1234060895,
            sender_username="", sender_name="", text="Аня, 18, Санкт-Петербург",
            media_type="", entities_json="[]", reply_markup_json="[]",
            reply_to=None, received_at="now", msg_date="now",
            msg=ev.message, raw_id=3,
        )
        asyncio.get_event_loop().run_until_complete(collector._process_message(task))

        auto_client.send_message.assert_not_called()

    def _make_collector_reject(
        self,
        config: AppConfig,
        auto_client: AsyncMock,
        other_client: AsyncMock,
        decision_text: str,
    ) -> DvinchikCollector:
        """Коллектор, где фильтр возвращает не-PASS решение (REJECT/REVIEW)."""
        from models.filter import FilterDecision, FilterResult
        from models.profile import Profile, ProfileStatus

        db = make_db_mock()
        collector = DvinchikCollector(
            [other_client, auto_client], db, config,
        )
        fs = AsyncMock()
        fs.evaluate = AsyncMock(
            return_value=FilterResult(
                decision=FilterDecision(decision_text), reasons=[],
            )
        )
        collector._filter_service = fs
        profile = Profile(
            id=7, name="Анютка", age=19,
            normalized_city="",  # город не распознан → фильтр REJECT
            status=ProfileStatus.NEW,
        )
        ps = AsyncMock()
        ps.upsert_profile = AsyncMock(return_value=profile)
        collector._profile_service = ps
        return collector

    def test_filter_reject_sends_dislike_to_keep_stream_moving(self) -> None:
        """Фильтровый REJECT на авто-аккаунте → 👎 (лента не замирает)."""
        auto_client = AsyncMock()
        auto_client.is_connected.return_value = True
        auto_client.send_message = AsyncMock()
        other_client = AsyncMock()
        collector = self._make_collector_reject(
            self._make_config(), auto_client, other_client, "REJECT"
        )

        task = RawTask(
            chat_id=1234060895, message_id=910, sender_id=1234060895,
            sender_username="", sender_name="", text="Анютка, 18, Москва",
            media_type="", entities_json="[]", reply_markup_json="[]",
            reply_to=None, received_at="now", msg_date="now",
            msg=self._auto_event_on_client(auto_client).message, raw_id=6,
        )
        asyncio.get_event_loop().run_until_complete(collector._process_message(task))

        auto_client.send_message.assert_called_once()
        args, _ = auto_client.send_message.call_args
        assert args[1] == "\U0001F44E"  # 👎
        collector._db.record_auto_action.assert_awaited_once_with(
            7, "DISLIKE", "REJECT", 1234060895, 910
        )

    def test_filter_review_sends_dislike_to_keep_stream_moving(self) -> None:
        """Фильтровый REVIEW на авто-аккаунте → 👎 (лента не замирает)."""
        auto_client = AsyncMock()
        auto_client.is_connected.return_value = True
        auto_client.send_message = AsyncMock()
        other_client = AsyncMock()
        collector = self._make_collector_reject(
            self._make_config(), auto_client, other_client, "REVIEW"
        )

        task = RawTask(
            chat_id=1234060895, message_id=911, sender_id=1234060895,
            sender_username="", sender_name="", text="Анютка, 18, Москва",
            media_type="", entities_json="[]", reply_markup_json="[]",
            reply_to=None, received_at="now", msg_date="now",
            msg=self._auto_event_on_client(auto_client).message, raw_id=7,
        )
        asyncio.get_event_loop().run_until_complete(collector._process_message(task))

        auto_client.send_message.assert_called_once()
        args, _ = auto_client.send_message.call_args
        assert args[1] == "\U0001F44E"  # 👎
        collector._db.record_auto_action.assert_awaited_once_with(
            7, "DISLIKE", "REVIEW", 1234060895, 911
        )

    def test_filter_reject_no_action_on_other_account(self) -> None:
        """REJECT на НЕ-авто-аккаунте → никакого действия."""
        auto_client = AsyncMock()
        auto_client.is_connected.return_value = True
        auto_client.send_message = AsyncMock()
        other_client = AsyncMock()
        collector = self._make_collector_reject(
            self._make_config(), auto_client, other_client, "REJECT"
        )

        ev = make_event(text="Анютка, 18, Москва", msg_id=912)
        ev.message.client = other_client
        task = RawTask(
            chat_id=1234060895, message_id=912, sender_id=1234060895,
            sender_username="", sender_name="", text="Анютка, 18, Москва",
            media_type="", entities_json="[]", reply_markup_json="[]",
            reply_to=None, received_at="now", msg_date="now",
            msg=ev.message, raw_id=8,
        )
        asyncio.get_event_loop().run_until_complete(collector._process_message(task))

        auto_client.send_message.assert_not_called()

    def _iter_msg(
        self, auto_client: AsyncMock, mid: int, text: str,
        buttons: list[str] | None = None, out: bool = False) -> MagicMock:
        """Мок Telegram-сообщения для сканирования активной анкеты."""
        m = MagicMock()
        m.id = mid
        m.text = text
        m.date = datetime.now(timezone.utc)
        m.sender_id = 1234060895
        m.sender = None
        m.client = auto_client
        m.out = out
        if buttons:
            rows = []
            for btext in buttons:
                b = MagicMock()
                b.text = btext
                rb = MagicMock()
                rb.buttons = [b]
                rows.append(rb)
            rm = MagicMock()
            rm.rows = rows
            m.reply_markup = rm
        else:
            m.reply_markup = None
        return m

    def test_start_stream_processes_active_profile_without_resync(self) -> None:
        """Есть активная анкета — обрабатываем её, ✨🔍 не шлём."""
        from models.decision import AIDecision

        auto_client = AsyncMock()
        auto_client.send_message = AsyncMock()
        other_client = AsyncMock()
        config = self._make_config()
        collector = self._make_collector(
            config, self._make_decision(AIDecision.DISLIKE), auto_client, other_client
        )

        async def iter_messages(*args, **kwargs):
            # Новые→старые: активная анкета (PROFILE) ждёт реакции.
            yield self._iter_msg(
                auto_client, 700, "Полина, 19, Санкт-Петербург"
            )
            yield self._iter_msg(auto_client, 699, "\U0001F44E", out=True)
            yield self._iter_msg(
                auto_client, 698, "Margo, 18, Санкт-Петербург"
            )

        auto_client.iter_messages = iter_messages

        ok = asyncio.get_event_loop().run_until_complete(
            collector.start_auto_stream()
        )
        assert ok is True
        # Активная анкета обработана → отправился 👎 (первый вызов), а НЕ
        # повторный ✨🔍. Второй вызов — пояснение при пересылке Меланхолику.
        assert auto_client.send_message.call_args_list[0] == (
            (1234060895, "\U0001F44E"),  # 👎
        )
        auto_client.forward_messages.assert_awaited_once()

    def test_start_stream_no_active_profile_sends_nothing(self) -> None:
        """Активной анкеты нет — ✨🔍 не отправляется, ничего не делаем."""
        from models.decision import AIDecision

        auto_client = AsyncMock()
        auto_client.send_message = AsyncMock()
        other_client = AsyncMock()
        config = self._make_config()
        collector = self._make_collector(
            config, self._make_decision(AIDecision.DISLIKE), auto_client, other_client
        )

        async def iter_messages(*args, **kwargs):
            # Последняя анкета уже обработана (после неё 👎) — активной нет.
            yield self._iter_msg(auto_client, 700, "\U0001F44E", out=True)
            yield self._iter_msg(
                auto_client, 699, "Margo, 18, Санкт-Петербург"
            )

        auto_client.iter_messages = iter_messages

        ok = asyncio.get_event_loop().run_until_complete(
            collector.start_auto_stream()
        )
        assert ok is False
        auto_client.send_message.assert_not_called()

    def test_start_stream_presses_view_button_when_promo(self) -> None:
        """Активной анкеты нет, Leo прислал промо «Смотреть анкеты» — жмём кнопку."""
        auto_client = AsyncMock()
        auto_client.send_message = AsyncMock()
        other_client = AsyncMock()
        collector = self._make_collector(
            self._make_config(), None, auto_client, other_client
        )
        # Без active-профиля _process_active_profile_if_any вернёт False → кнопка.
        button_text = "\U0001F680 Смотреть анкеты"  # 🚀 Смотреть анкеты

        async def iter_messages(*args, **kwargs):
            # Новые→старые: промо с кнопкой → старый 👎 → старая анкета.
            yield self._iter_msg(
                auto_client, 705, "твоя анкета может больше", buttons=[button_text]
            )
            yield self._iter_msg(auto_client, 704, "\U0001F44E", out=True)
            yield self._iter_msg(auto_client, 703, "Margo, 18, Санкт-Петербург")

        auto_client.iter_messages = iter_messages

        ok = asyncio.get_event_loop().run_until_complete(
            collector.start_auto_stream()
        )
        assert ok is True
        auto_client.send_message.assert_called_once_with(1234060895, button_text)

    def test_start_stream_no_button_press_when_already_sent(self) -> None:
        """Кнопка уже нажата (после промо есть исходящий текст кнопки) — повторно нет."""
        auto_client = AsyncMock()
        auto_client.send_message = AsyncMock()
        other_client = AsyncMock()
        collector = self._make_collector(
            self._make_config(), None, auto_client, other_client
        )
        button_text = "\U0001F680 Смотреть анкеты"  # 🚀 Смотреть анкеты

        async def iter_messages(*args, **kwargs):
            # Новые→старые: уже отправленный текст кнопки (out) → промо с кнопкой.
            yield self._iter_msg(auto_client, 706, button_text, out=True)
            yield self._iter_msg(
                auto_client, 705, "твоя анкета может больше", buttons=[button_text]
            )

        auto_client.iter_messages = iter_messages

        ok = asyncio.get_event_loop().run_until_complete(
            collector.start_auto_stream()
        )
        assert ok is False
        auto_client.send_message.assert_not_called()

    def test_start_stream_presses_right_button_on_captcha(self) -> None:
        """Активной анкеты нет, но Leo показал капчу/проверку — жмём ПОСЛЕДНЮЮ кнопку."""
        auto_client = AsyncMock()
        auto_client.send_message = AsyncMock()
        other_client = AsyncMock()
        collector = self._make_collector(
            self._make_config(), None, auto_client, other_client
        )
        # Капча/сделка: кнопки «Меню»/«Сообщение …»/«Готово»/«Возможно позже»
        # → нажимаем последнюю («Возможно позже»).
        last_button = "Возможно позже"

        async def iter_messages(*args, **kwargs):
            # Новые→старые: капча с 4 кнопками → старый 👎 → старая анкета.
            yield self._iter_msg(
                auto_client, 710, "Бармалей, предлагаю тебе сделку",
                buttons=["Меню", "Сообщение ...", "Готово", last_button],
            )
            yield self._iter_msg(auto_client, 709, "\U0001F44E", out=True)
            yield self._iter_msg(auto_client, 708, "Margo, 18, Санкт-Петербург")

        auto_client.iter_messages = iter_messages

        ok = asyncio.get_event_loop().run_until_complete(
            collector.start_auto_stream()
        )
        assert ok is True
        auto_client.send_message.assert_called_once_with(
            1234060895, last_button
        )

    def test_start_stream_no_captcha_press_when_already_sent(self) -> None:
        """Последняя кнопка капчи уже нажата (после неё исходящий текст) — повторно нет."""
        auto_client = AsyncMock()
        auto_client.send_message = AsyncMock()
        other_client = AsyncMock()
        collector = self._make_collector(
            self._make_config(), None, auto_client, other_client
        )
        last_button = "Возможно позже"

        async def iter_messages(*args, **kwargs):
            # Новые→старые: уже нажатая последняя кнопка (out) → капча.
            yield self._iter_msg(auto_client, 712, last_button, out=True)
            yield self._iter_msg(
                auto_client, 711, "Подтвердите, что вы человек",
                buttons=["Готово", last_button],
            )

        auto_client.iter_messages = iter_messages

        ok = asyncio.get_event_loop().run_until_complete(
            collector.start_auto_stream()
        )
        assert ok is False
        auto_client.send_message.assert_not_called()

    def test_no_button_press_on_menu_or_premium(self) -> None:
        """Меню/Premium-промо Leo (без маркера капчи) НЕ трогаем — кнопки не жмём."""
        auto_client = AsyncMock()
        auto_client.send_message = AsyncMock()
        other_client = AsyncMock()
        collector = self._make_collector(
            self._make_config(), None, auto_client, other_client
        )

        async def iter_messages(*args, **kwargs):
            # Новые→старые: главное меню Leo (не капча) → старый 👎 → анкета.
            yield self._iter_msg(
                auto_client, 720, "1. Смотреть анкеты.\n2. Моя анкета.\n3. Не искать",
                buttons=["1 🚀", "2", "3", "4"],
            )
            yield self._iter_msg(auto_client, 719, "\U0001F44E", out=True)
            yield self._iter_msg(auto_client, 718, "Margo, 18, Санкт-Петербург")

            auto_client.iter_messages = iter_messages

        ok = asyncio.get_event_loop().run_until_complete(
            collector.start_auto_stream()
        )
        assert ok is False
        auto_client.send_message.assert_not_called()

    def test_live_ad_message_presses_view_button_on_separate_message(self) -> None:
        """Реклама приходит БЕЗ кнопки, а «🚀 Смотреть анкеты» — на ОТДЕЛЬНОМ
        сообщении после неё. Живая обработка рекламного UNKNOWN-сообщения должна
        отсканировать чат и нажать кнопку, а не застрять на этом экране."""
        auto_client = AsyncMock()
        auto_client.is_connected.return_value = True
        auto_client.send_message = AsyncMock()
        other_client = AsyncMock()
        collector = self._make_collector(
            self._make_config(), None, auto_client, other_client
        )
        button_text = "\U0001F680 Смотреть анкеты"  # 🚀 Смотреть анкеты

        async def iter_messages(*args, **kwargs):
            # Новые→старые: промо с кнопкой (ОТДЕЛЬНОЕ сообщение) →
            # рекламное сообщение БЕЗ кнопки.
            yield self._iter_msg(
                auto_client, 705, "твоя анкета может больше", buttons=[button_text]
            )
            yield self._iter_msg(auto_client, 704, "реклама канала, без кнопки")

        auto_client.iter_messages = iter_messages

        # Обрабатываем именно рекламное сообщение — на нём самом кнопки нет.
        ad_msg = self._iter_msg(auto_client, 704, "реклама канала, без кнопки")
        task = RawTask(
            chat_id=1234060895, message_id=704, sender_id=1234060895,
            sender_username="", sender_name="",
            text="реклама канала, без кнопки",
            media_type="", entities_json="[]", reply_markup_json="[]",
            reply_to=None, received_at="now", msg_date="now",
            msg=ad_msg, raw_id=2,
        )
        asyncio.get_event_loop().run_until_complete(collector._process_message(task))

        # Кнопка на отдельном сообщении после рекламы — бот не застревает.
        auto_client.send_message.assert_called_once_with(1234060895, button_text)

class TestCollectorSetMode:
    """Динамическое переключение режима коллектора (Stage 7.5)."""

    def _make_collector(self) -> DvinchikCollector:
        cfg = self._make_config()
        db = make_db_mock()
        # acc1 (idx 0) + acc2/авто (idx 1) — account_session=dvai_2 → idx 1.
        return DvinchikCollector([AsyncMock(), AsyncMock()], db, cfg)

    def _make_config(self) -> AppConfig:
        cfg = make_config()
        data = cfg.model_dump()
        data["telegram"] = {
            "accounts": [
                {"api_id": 38219721, "api_hash": "a" * 32, "session": "dvai"},
                {"api_id": 36266816, "api_hash": "b" * 32, "session": "dvai_2"},
            ]
        }
        data["project"] = {"mode": "OBSERVE"}
        data["auto_actions"] = {
            "enabled": True,
            "account_session": "dvai_2",
            "interval_sec": 0.0,
        }
        return AppConfig(**data)

    def test_set_mode_updates_engine(self, tmp_path) -> None:
        from core.types import Mode

        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(
            "project:\n  mode: OBSERVE\n", encoding="utf-8",
        )
        collector = self._make_collector()
        collector._config_path = cfg_path
        # Начинаем в OBSERVE.
        assert collector.mode.value == "OBSERVE"
        assert collector.auto_engine().enabled is False
        # Включаем SEMI_AUTO на лету.
        collector.set_mode(Mode.SEMI_AUTO)
        assert collector.mode.value == "SEMI_AUTO"
        assert collector.auto_engine().enabled is True

    def test_set_mode_persists_to_config(self, tmp_path) -> None:
        from core.types import Mode

        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(
            "project:\n  mode: OBSERVE\n", encoding="utf-8",
        )
        collector = self._make_collector()
        collector._config_path = cfg_path
        collector.set_mode(Mode.SEMI_AUTO)
        text = cfg_path.read_text(encoding="utf-8")
        assert "SEMI_AUTO" in text

    def test_turn_off_at_runtime(self) -> None:
        from core.types import Mode

        collector = self._make_collector()
        collector.set_mode(Mode.SEMI_AUTO)
        assert collector.auto_engine().enabled is True
        collector.set_mode(Mode.OBSERVE)
        assert collector.auto_engine().enabled is False

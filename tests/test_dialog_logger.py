# Журнал переписок с девушками после взаимного лайка (dialog_contacts/messages).

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.errors import FloodWaitError

from collectors.dialog_logger import BOT, HER, OWNER, DialogLogger, media_kind, message_text
from database.database import Database
from models.raw import ParsedMatch
from tests.test_match_greeter import LEO
from tests.test_match_greeter import TestCollectorGreetsOnMatch as _Greeting

DATE = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "t.db")
    run(database.connect())
    yield database
    run(database.close())


def tg_msg(mid: int, text: str = "", *, out: bool = False, chat_id: int = 77,
           **extra) -> SimpleNamespace:
    """Похожее на Telethon сообщение (без вложений по умолчанию)."""
    fields = dict(
        id=mid, message=text, out=out, chat_id=chat_id, date=DATE,
        sticker=None, video_note=None, voice=None, gif=None, photo=None,
        video=None, audio=None, document=None, media=None, fwd_from=None,
        reply_to_msg_id=None, file=None,
    )
    fields.update(extra)
    return SimpleNamespace(**fields)


def messages(db: Database, session: str = "dvai", user_id: int = 77) -> list[dict]:
    async def _q() -> list[dict]:
        cur = await db._connection.execute(
            "SELECT * FROM dialog_messages WHERE account_session = ? AND user_id = ? "
            "ORDER BY telegram_message_id", (session, user_id),
        )
        return [db._row_to_dict(cur, r) for r in await cur.fetchall()]
    return run(_q())


class History:
    """client.iter_messages: свежие первыми, с учётом min_id."""

    def __init__(self, msgs: list) -> None:
        self.msgs = msgs
        self.calls: list[dict] = []

    def __call__(self, peer, limit=None, min_id=0):
        self.calls.append({"peer": peer, "limit": limit, "min_id": min_id})
        items = [m for m in sorted(self.msgs, key=lambda m: -m.id) if m.id > min_id]

        async def gen():
            for m in items[:limit]:
                yield m
        return gen()


class TestDialogDb:
    def test_upsert_contact_new_then_fills_blanks(self, db) -> None:
        assert run(db.upsert_dialog_contact("dvai", 77, name="Роза")) is True
        assert run(db.upsert_dialog_contact(
            "dvai", 77, username="roza", name="Другая", profile_id=5,
        )) is False
        row = run(db.get_dialog_sync_queue("dvai", older_than="9999"))[0]
        assert row["username"] == "roza"
        assert row["name"] == "Роза"          # имя первого матча не затираем
        assert row["profile_id"] == 5
        assert run(db.get_dialog_contact_ids("dvai")) == {77}
        assert run(db.get_dialog_contact_ids("dvai_2")) == set()

    def test_message_dedup_and_bot_label_wins(self, db) -> None:
        run(db.upsert_dialog_contact("dvai", 77))
        kw = dict(is_outgoing=True, message_date=DATE.isoformat(), text="🔥")
        assert run(db.save_dialog_message("dvai", 77, 10, sent_by=OWNER, **kw))
        assert not run(db.save_dialog_message("dvai", 77, 10, sent_by=OWNER, **kw))
        # Эхо-событие записало стикер бота как «владелец» — бот исправляет.
        assert not run(db.save_dialog_message("dvai", 77, 10, sent_by=BOT, **kw))
        rows = messages(db)
        assert len(rows) == 1 and rows[0]["sent_by"] == BOT

    def test_message_updates_last_message_at(self, db) -> None:
        run(db.upsert_dialog_contact("dvai", 77))
        run(db.save_dialog_message(
            "dvai", 77, 1, is_outgoing=False, sent_by=HER,
            message_date="2026-10-29T12:00:00+00:00",
        ))
        row = run(db.get_dialog_sync_queue("dvai", older_than="9999"))[0]
        assert row["last_message_at"] == "2026-10-29T12:00:00+00:00"
        assert row["last_tm_id"] == 1

    def test_sync_queue_skips_recently_synced(self, db) -> None:
        run(db.upsert_dialog_contact("dvai", 77))
        run(db.upsert_dialog_contact("dvai", 78))
        run(db.mark_dialog_synced("dvai", 77, at="2026-10-29T12:00:00+00:00"))
        rows = run(db.get_dialog_sync_queue(
            "dvai", older_than="2026-10-29T11:00:00+00:00",
        ))
        assert [r["user_id"] for r in rows] == [78]

    def test_backfill_from_match_dialogs(self, db) -> None:
        run(db.add_match_dialog("dvai_2", 99, username="liza", name="Лиза",
                                profile_id=3))
        run(db._backfill_dialog_contacts())
        assert run(db.get_dialog_contact_ids("dvai_2")) == {99}

    def test_untracked_matches_by_account(self, db) -> None:
        for tm, session, user in ((500, "dvai", "Roza"), (30, "dvai_2", "liza")):
            run(db.save_raw_message(
                telegram_message_id=tm, chat_id=LEO, sender_id=LEO,
                sender_username="", sender_name="", message_date=DATE.isoformat(),
                text="Начинай общаться", raw_entities="[]",
                account_session=session,
            ))
            run(db.record_match_response(user, LEO, tm, telegram_username=user))
        rows = run(db.list_untracked_matches("dvai"))
        assert [r["telegram_username"] for r in rows] == ["Roza"]
        run(db.upsert_dialog_contact("dvai", 77, username="roza"))
        assert run(db.list_untracked_matches("dvai")) == []


class TestMessageShape:
    def test_sticker_text_is_emoji(self) -> None:
        msg = tg_msg(1, sticker=object(), media=object(),
                     file=SimpleNamespace(emoji="👋"))
        assert media_kind(msg) == "sticker"
        assert message_text(msg, "sticker") == "👋"

    def test_plain_text(self) -> None:
        msg = tg_msg(1, "привет")
        assert media_kind(msg) == "" and message_text(msg, "") == "привет"

    def test_voice(self) -> None:
        assert media_kind(tg_msg(1, voice=object(), document=object())) == "voice"


class TestDialogLogger:
    def test_track_by_user_id(self, db) -> None:
        log = DialogLogger(db)
        match = ParsedMatch(name="Роза", telegram_user_id=77)
        assert run(log.track(None, "dvai", match, 5)) == 77
        assert run(log.is_tracked("dvai", 77))
        assert not run(log.is_tracked("dvai_2", 77))

    def test_track_resolves_username(self, db) -> None:
        log = DialogLogger(db)
        client = AsyncMock()
        client.get_input_entity = AsyncMock(return_value=SimpleNamespace(user_id=88))
        match = ParsedMatch(name="Лиза", telegram_username="liza")
        assert run(log.track(client, "dvai", match)) == 88

    def test_untracked_chat_not_logged(self, db) -> None:
        """Переписка владельца с другими людьми в журнал не попадает."""
        log = DialogLogger(db)
        assert not run(log.log_event("dvai", tg_msg(1, "hi"), outgoing=False,
                                     user_id=12345))
        assert messages(db, user_id=12345) == []

    def test_her_and_owner_messages(self, db) -> None:
        log = DialogLogger(db)
        run(log.track(None, "dvai", ParsedMatch(name="Роза", telegram_user_id=77)))
        run(log.log_event("dvai", tg_msg(1, "привет)"), outgoing=False, user_id=77))
        run(log.log_event("dvai", tg_msg(2, "как дела?", out=True), outgoing=True,
                          user_id=77))
        rows = messages(db)
        assert [(r["sent_by"], r["text"], r["is_outgoing"]) for r in rows] == [
            (HER, "привет)", 0), (OWNER, "как дела?", 1),
        ]

    def test_bot_sends_logged(self, db) -> None:
        log = DialogLogger(db)
        run(log.track(None, "dvai", ParsedMatch(name="Роза", telegram_user_id=77)))
        sent = [tg_msg(5, "", out=True, photo=object(), media=object(),
                       fwd_from=object()),
                tg_msg(6, "Роза, 19", out=True, fwd_from=object())]
        assert run(log.log_sent("dvai", 77, sent)) == 2
        rows = messages(db)
        assert {r["sent_by"] for r in rows} == {BOT}
        assert rows[0]["media_type"] == "photo" and rows[0]["is_forward"] == 1

    def test_disabled_logs_nothing(self, db) -> None:
        log = DialogLogger(db, enabled=False)
        assert run(log.track(None, "dvai", ParsedMatch(telegram_user_id=77))) == 0
        assert not run(log.log_event("dvai", tg_msg(1, "x"), outgoing=False,
                                     user_id=77))

    def test_sync_pulls_history_and_labels(self, db) -> None:
        log = DialogLogger(db)
        run(log.track(None, "dvai", ParsedMatch(name="Роза", telegram_user_id=77)))
        client = AsyncMock()
        client.get_input_entity = AsyncMock(return_value="peer")
        history = History([
            tg_msg(1, "", out=True, sticker=object(), media=object(),
                   file=SimpleNamespace(emoji="👋")),
            tg_msg(2, "приветик"),
            tg_msg(3, "", out=True, photo=object(), media=object(),
                   fwd_from=object()),
            tg_msg(4, "куда пойдём?", out=True),
        ])
        client.iter_messages = history
        assert run(log.sync(client, "dvai")) == 4
        assert [r["sent_by"] for r in messages(db)] == [BOT, HER, BOT, OWNER]
        assert messages(db)[0]["text"] == "👋"
        # Повторный проход в пределах интервала — без запросов.
        assert run(log.sync(client, "dvai")) == 0
        assert len(history.calls) == 1

    def test_second_sync_only_newer(self, db) -> None:
        log = DialogLogger(db)
        run(log.track(None, "dvai", ParsedMatch(telegram_user_id=77)))
        run(log.log_event("dvai", tg_msg(2, "старое"), outgoing=False, user_id=77))
        client = AsyncMock()
        client.get_input_entity = AsyncMock(return_value="peer")
        history = History([tg_msg(2, "старое"), tg_msg(3, "новое")])
        client.iter_messages = history
        assert run(log.sync(client, "dvai")) == 1
        assert history.calls[0]["min_id"] == 2

    def test_sync_flood_wait_stops_pass(self, db) -> None:
        log = DialogLogger(db)
        run(log.track(None, "dvai", ParsedMatch(telegram_user_id=77)))
        run(log.track(None, "dvai", ParsedMatch(telegram_user_id=78)))
        client = AsyncMock()
        client.get_input_entity = AsyncMock(
            side_effect=FloodWaitError(request=None, capture=30),
        )
        assert run(log.sync(client, "dvai")) == 0
        assert client.get_input_entity.await_count == 1
        # Не сверенные чаты остаются в очереди.
        assert len(run(db.get_dialog_sync_queue("dvai", older_than="9999"))) == 2

    def test_sync_adds_old_matches(self, db) -> None:
        run(db.save_raw_message(
            telegram_message_id=500, chat_id=LEO, sender_id=LEO,
            sender_username="", sender_name="", message_date=DATE.isoformat(),
            text="Начинай общаться", raw_entities="[]", account_session="dvai",
        ))
        run(db.record_match_response("Роза", LEO, 500, telegram_username="Roza"))
        log = DialogLogger(db)
        client = AsyncMock()
        client.get_input_entity = AsyncMock(return_value=SimpleNamespace(user_id=77))
        client.iter_messages = History([tg_msg(1, "привет")])
        run(log.sync(client, "dvai"))
        assert run(db.get_dialog_contact_ids("dvai")) == {77}
        assert [r["text"] for r in messages(db)] == ["привет"]


class TestCollectorDialogLog:
    def _collector(self):
        collector, other, auto = _Greeting()._collector("SEMI_AUTO")
        collector._db.find_match_profile = AsyncMock(return_value=3)
        collector._dialog_log.track = AsyncMock(return_value=77)
        collector._dialog_log.log_event = AsyncMock(return_value=True)
        return collector, other, auto

    def test_match_tracks_before_greeting(self) -> None:
        collector, _, auto = self._collector()
        order: list[str] = []
        collector._dialog_log.track = AsyncMock(
            side_effect=lambda *a, **k: order.append("track") or 77,
        )
        collector._match_greeter.greet = AsyncMock(
            side_effect=lambda *a, **k: order.append("greet") or "SENT",
        )
        run(collector._process_message(_Greeting._task(auto)))
        assert order == ["track", "greet"]
        args = collector._dialog_log.track.await_args.args
        assert args[0] is auto and args[1] == "dvai_2" and args[3] == 3

    def test_match_tracked_even_if_not_greeted(self) -> None:
        collector, _, auto = self._collector()
        collector._match_greeter.greet = AsyncMock(return_value="HAS_CHAT")
        run(collector._process_message(_Greeting._task(auto)))
        collector._dialog_log.track.assert_awaited_once()

    def test_private_incoming_logged(self) -> None:
        collector, _, auto = self._collector()
        event = MagicMock(chat_id=77, sender_id=77, client=auto)
        run(collector._handle_private_message(event))
        call = collector._dialog_log.log_event.await_args
        assert call.args[0] == "dvai_2"
        assert call.kwargs == {"outgoing": False, "user_id": 77}

    def test_private_outgoing_logged(self) -> None:
        collector, _, auto = self._collector()
        event = MagicMock(chat_id=77, client=auto, is_private=True)
        run(collector._handle_outgoing_message(event))
        call = collector._dialog_log.log_event.await_args
        assert call.args[0] == "dvai_2"
        assert call.kwargs == {"outgoing": True, "user_id": 77}
        collector._db.save_raw_message.assert_not_called()

    def test_group_outgoing_not_logged(self) -> None:
        collector, _, auto = self._collector()
        event = MagicMock(chat_id=-100, client=auto, is_private=False)
        run(collector._handle_outgoing_message(event))
        collector._dialog_log.log_event.assert_not_called()

    def test_bot_sends_hooked(self) -> None:
        collector, _, _ = self._collector()
        assert collector._match_greeter.on_sent is not None
        assert collector._match_intro.on_sent is not None

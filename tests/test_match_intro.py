# Автопереписка: её ответ на привет → через 30 мин её анкета + закреп.

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.errors import FloodWaitError

from app.config import AppConfig, MatchIntroConfig
from collectors.dvinchik_collector import DvinchikCollector
from collectors.match_intro import (
    ERROR,
    INTRO_SENT,
    NO_PROFILE,
    OWNER_CHAT,
    SCHEDULED,
    WAITING_REPLY,
    MatchIntro,
)
from database.database import Database
from models.raw import ParsedMatch
from tests.test_collector import make_config, make_db_mock
from tests.test_match_greeter import LEO
from tests.test_match_greeter import TestCollectorGreetsOnMatch as _Greeting

NOW = "2026-10-29T10:00:00+00:00"


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "t.db")
    run(database.connect())
    yield database
    run(database.close())


async def _profile(db: Database, name: str, card_id: int, session: str,
                   photo_ids: list[int]) -> int:
    pid = await db.insert_profile(
        name, 19, "", "", "", f"fp-{name}-{card_id}", LEO, card_id,
        NOW, NOW, "NEW",
    )
    await db.save_raw_message(
        telegram_message_id=card_id, chat_id=LEO, sender_id=LEO,
        sender_username="", sender_name="", message_date=NOW,
        text=f"{name}, 19, Санкт-Петербург – люблю котиков",
        raw_entities="[]", account_session=session,
    )
    await db.link_profile_message(pid, card_id, LEO, NOW, account_session=session)
    for mid in photo_ids:
        await db.link_profile_message(pid, mid, LEO, NOW, account_session=session)
    return pid


class TestMatchDialogDb:
    def test_add_is_idempotent(self, db: Database) -> None:
        assert run(db.add_match_dialog("dvai_2", 5, name="Роза"))
        assert not run(db.add_match_dialog("dvai_2", 5, name="Другая"))
        row = run(db.get_match_dialog("dvai_2", 5))
        assert row["status"] == WAITING_REPLY and row["name"] == "Роза"
        assert run(db.get_match_dialog("dvai", 5)) is None

    def test_update_and_list(self, db: Database) -> None:
        run(db.add_match_dialog("dvai_2", 5))
        run(db.update_match_dialog("dvai_2", 5, status=SCHEDULED, due_at=NOW))
        rows = run(db.list_match_dialogs("dvai_2", SCHEDULED))
        assert [r["user_id"] for r in rows] == [5]
        with pytest.raises(ValueError):
            run(db.update_match_dialog("dvai_2", 5, greeted_at="x"))

    def test_card_ids_last_showing_of_account(self, db: Database) -> None:
        """Последний показ на ЭТОМ аккаунте: подпись + фото рядом."""
        pid = run(_profile(db, "Роза", 1000, "dvai_2", [998, 999]))
        # Повторный показ позже — берём его.
        run(db.save_raw_message(
            telegram_message_id=2000, chat_id=LEO, sender_id=LEO,
            sender_username="", sender_name="", message_date=NOW,
            text="Роза, 19 – снова", raw_entities="[]", account_session="dvai_2",
        ))
        run(db.link_profile_message(pid, 2000, LEO, NOW, account_session="dvai_2"))
        run(db.link_profile_message(pid, 1999, LEO, NOW, account_session="dvai_2"))
        # Чужой аккаунт — не берём.
        run(db.link_profile_message(pid, 700000, LEO, NOW, account_session="dvai"))
        assert run(db.get_profile_card_message_ids(pid, "dvai_2")) == [1999, 2000]
        assert run(db.get_profile_card_message_ids(pid, "other")) == []

    def test_find_match_profile_by_context(self, db: Database) -> None:
        pid = run(_profile(db, "Роза", 1000, "dvai_2", []))
        run(db.set_chat_profile_context(LEO, 1000, "dvai_2"))
        assert run(db.find_match_profile("роза", LEO, "dvai_2")) == pid
        # Имя не совпало с текущей карточкой → по имени среди лайкнутых (нет).
        assert run(db.find_match_profile("Олеся", LEO, "dvai_2")) is None


def make_intro(db: object, **cfg: object) -> MatchIntro:
    return MatchIntro(MatchIntroConfig(**cfg), db, LEO)


def make_db(row: dict | None = None) -> MagicMock:
    db = MagicMock()
    db.add_match_dialog = AsyncMock(return_value=True)
    db.get_match_dialog = AsyncMock(return_value=row)
    db.update_match_dialog = AsyncMock()
    db.list_match_dialogs = AsyncMock(return_value=[])
    db.get_profile_card_message_ids = AsyncMock(return_value=[11, 12])
    return db


def make_client(sent: object = None) -> AsyncMock:
    client = AsyncMock()
    client.get_input_entity = AsyncMock(return_value=SimpleNamespace(user_id=77))
    client.forward_messages = AsyncMock(return_value=sent if sent is not None else [
        SimpleNamespace(id=1, text=""),
        SimpleNamespace(id=2, text="Роза, 19 – люблю котиков"),
    ])
    client.pin_message = AsyncMock()

    async def iter_messages(*args, **kwargs):
        return
        yield

    client.iter_messages = iter_messages
    return client


def due_row(**kw: object) -> dict:
    row = {
        "user_id": 77, "username": "rosa", "name": "Роза", "profile_id": 3,
        "status": SCHEDULED, "greeted_at": NOW,
        "due_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
    }
    row.update(kw)
    return row


class TestMatchIntro:
    def test_enroll_resolves_user_id_by_username(self) -> None:
        db = make_db()
        intro = make_intro(db)
        match = ParsedMatch(name="Роза", telegram_username="rosa")
        assert run(intro.enroll(make_client(), "dvai_2", match, 3))
        args = db.add_match_dialog.await_args
        assert args.args == ("dvai_2", 77) and args.kwargs["profile_id"] == 3

    def test_enroll_disabled(self) -> None:
        db = make_db()
        intro = make_intro(db, enabled=False)
        match = ParsedMatch(name="Роза", telegram_user_id=77)
        assert not run(intro.enroll(make_client(), "dvai_2", match, 3))
        db.add_match_dialog.assert_not_called()

    def test_reply_schedules_in_30_min(self) -> None:
        db = make_db({"status": WAITING_REPLY, "name": "Роза"})
        intro = make_intro(db)
        at = datetime(2026, 10, 29, 10, 0, tzinfo=timezone.utc)
        assert run(intro.note_reply("dvai_2", 77, "AUTO", at=at))
        kw = db.update_match_dialog.await_args.kwargs
        assert kw["status"] == SCHEDULED
        assert kw["due_at"] == (at + timedelta(minutes=30)).isoformat()

    @pytest.mark.parametrize("mode", ["OBSERVE", "SEMI_AUTO"])
    def test_reply_ignored_outside_auto(self, mode: str) -> None:
        db = make_db({"status": WAITING_REPLY})
        assert not run(make_intro(db).note_reply("dvai_2", 77, mode))
        db.update_match_dialog.assert_not_called()

    def test_second_reply_does_not_reschedule(self) -> None:
        db = make_db({"status": SCHEDULED})
        assert not run(make_intro(db).note_reply("dvai_2", 77, "AUTO"))
        db.update_match_dialog.assert_not_called()

    def test_unknown_sender_ignored(self) -> None:
        db = make_db(None)
        assert not run(make_intro(db).note_reply("dvai_2", 99, "AUTO"))

    def test_due_intro_forwarded_and_pinned_for_both(self) -> None:
        db = make_db()
        db.list_match_dialogs = AsyncMock(side_effect=[[], [due_row()]])
        client = make_client()
        results = run(make_intro(db).process(client, "dvai_2", "AUTO"))
        assert [s for _, s in results] == [INTRO_SENT]
        client.forward_messages.assert_awaited_once()
        fargs = client.forward_messages.await_args
        assert fargs.args[1] == [11, 12] and fargs.kwargs["from_peer"] == LEO
        pin = client.pin_message.await_args
        assert pin.args[1].id == 2  # сообщение с подписью анкеты
        assert pin.kwargs["pm_oneside"] is False and pin.kwargs["notify"] is False
        kw = db.update_match_dialog.await_args.kwargs
        assert kw["status"] == INTRO_SENT and kw["pinned"] == 1

    def test_not_due_yet_waits(self) -> None:
        db = make_db()
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        db.list_match_dialogs = AsyncMock(side_effect=[[], [due_row(due_at=future)]])
        client = make_client()
        assert run(make_intro(db).process(client, "dvai_2", "AUTO")) == []
        client.forward_messages.assert_not_called()

    def test_scheduled_waits_when_mode_not_auto(self) -> None:
        db = make_db()
        db.list_match_dialogs = AsyncMock(return_value=[due_row()])
        client = make_client()
        assert run(make_intro(db).process(client, "dvai_2", "SEMI_AUTO")) == []
        client.forward_messages.assert_not_called()

    def test_no_profile_messages(self) -> None:
        db = make_db()
        db.get_profile_card_message_ids = AsyncMock(return_value=[])
        status = run(make_intro(db).send_intro(make_client(), "dvai_2", due_row()))
        assert status == NO_PROFILE

    def test_pin_disabled(self) -> None:
        db = make_db()
        client = make_client()
        status = run(make_intro(db, pin=False).send_intro(client, "dvai_2", due_row()))
        assert status == INTRO_SENT
        client.pin_message.assert_not_called()

    def test_pin_error_still_sent(self) -> None:
        db = make_db()
        client = make_client()
        client.pin_message = AsyncMock(side_effect=RuntimeError("no rights"))
        status = run(make_intro(db).send_intro(client, "dvai_2", due_row()))
        assert status == INTRO_SENT
        kw = db.update_match_dialog.await_args.kwargs
        assert kw["pinned"] == 0 and "pin" in kw["error"]

    def test_forward_error_marks_error(self) -> None:
        db = make_db()
        client = make_client()
        client.forward_messages = AsyncMock(side_effect=RuntimeError("privacy"))
        status = run(make_intro(db).send_intro(client, "dvai_2", due_row()))
        assert status == ERROR
        assert db.update_match_dialog.await_args.kwargs["status"] == ERROR

    def test_flood_postpones(self) -> None:
        db = make_db()
        client = make_client()
        client.forward_messages = AsyncMock(
            side_effect=FloodWaitError(request=None, capture=120)
        )
        status = run(make_intro(db).send_intro(client, "dvai_2", due_row()))
        assert status == "FLOOD"
        kw = db.update_match_dialog.await_args.kwargs
        assert "status" not in kw and "due_at" in kw

    @staticmethod
    def _relay_setup(copies: list) -> tuple[MagicMock, AsyncMock, AsyncMock, MatchIntro]:
        """Анкета есть только в чате Leo dvai; пишет девушке dvai_2."""
        db = make_db()
        db.get_profile_card_message_ids = AsyncMock(
            side_effect=lambda pid, session: [11, 12] if session == "dvai" else []
        )
        src = make_client(sent=[
            SimpleNamespace(id=501, message=""),
            SimpleNamespace(id=502, message="Роза, 19 – люблю котиков"),
        ])
        src.get_me = AsyncMock(return_value=SimpleNamespace(id=1, username="src_acc"))
        dst = make_client()
        dst.get_me = AsyncMock(return_value=SimpleNamespace(id=2, username="dst_acc"))
        now = datetime.now(timezone.utc)

        def iter_messages(*args, **kwargs):
            async def gen():
                for m in copies:
                    yield SimpleNamespace(date=now, out=False, **m)
            return gen()

        dst.iter_messages = MagicMock(side_effect=iter_messages)
        intro = make_intro(db)
        intro.clients = {"dvai": src, "dvai_2": dst}
        return db, src, dst, intro

    def test_card_relayed_from_account_that_saw_it(self) -> None:
        """Перенаправление лички: анкету видел dvai → он шлёт её dvai_2, тот — ей."""
        fwd = SimpleNamespace(from_id=LEO)
        db, src, dst, intro = self._relay_setup([
            {"id": 9, "message": "Роза, 19 – люблю котиков", "fwd_from": fwd},
            {"id": 8, "message": "", "fwd_from": fwd},
            {"id": 7, "message": "старая чужая", "fwd_from": fwd},
        ])
        status = run(intro.send_intro(dst, "dvai_2", due_row()))
        assert status == INTRO_SENT
        sargs = src.forward_messages.await_args
        assert sargs.args[1] == [11, 12] and sargs.kwargs["from_peer"] == LEO
        assert src.get_input_entity.await_args.args[0] == "dst_acc"
        dargs = dst.forward_messages.await_args
        assert dargs.args[1] == [8, 9]
        # Пересылает из своего чата с dvai, а не из чата Leo.
        assert dargs.kwargs["from_peer"] != LEO
        looked_up = [c.args[0] for c in dst.get_input_entity.await_args_list]
        assert "src_acc" in looked_up
        assert db.update_match_dialog.await_args.kwargs["status"] == INTRO_SENT

    def test_relay_copies_not_found_is_error(self) -> None:
        db, src, dst, intro = self._relay_setup([])
        status = run(intro.send_intro(dst, "dvai_2", due_row()))
        assert status == ERROR
        dst.forward_messages.assert_not_called()
        assert "relay" in db.update_match_dialog.await_args.kwargs["error"]

    def test_own_card_preferred_over_other_account(self) -> None:
        db = make_db()
        src = make_client()
        intro = make_intro(db)
        intro.clients = {"dvai": src, "dvai_2": make_client()}
        client = intro.clients["dvai_2"]
        assert run(intro.send_intro(client, "dvai_2", due_row())) == INTRO_SENT
        src.forward_messages.assert_not_called()
        assert client.forward_messages.await_args.kwargs["from_peer"] == LEO

    @staticmethod
    def _chat(*msgs: tuple[bool, str]) -> MagicMock:
        """История её чата (новые первыми): (наше исходящее?, время ISO)."""
        items = [
            SimpleNamespace(out=out, date=datetime.fromisoformat(at))
            for out, at in msgs
        ]

        def iter_messages(*args, **kwargs):
            async def gen():
                for m in items:
                    yield m
            return gen()

        return MagicMock(side_effect=iter_messages)

    def test_owner_chatting_cancels_intro(self) -> None:
        """Прод 2026-10-29 (@stfu_ckup): она ответила на стикер, владелец сам
        с ней переписывается — анкету посреди разговора не шлём."""
        db = make_db()
        client = make_client()
        client.iter_messages = self._chat(
            (False, "2026-10-29T10:03:00+00:00"),
            (True, "2026-10-29T10:02:00+00:00"),   # владелец
            (False, "2026-10-29T10:01:00+00:00"),  # её ответ
            (True, "2026-10-29T09:59:59+00:00"),   # стикер-привет бота
        )
        status = run(make_intro(db).send_intro(client, "dvai_2", due_row()))
        assert status == OWNER_CHAT
        client.forward_messages.assert_not_called()
        assert db.update_match_dialog.await_args.kwargs["status"] == OWNER_CHAT

    def test_only_her_reply_after_greeting_sends(self) -> None:
        db = make_db()
        client = make_client()
        client.iter_messages = self._chat(
            (False, "2026-10-29T10:01:00+00:00"),
            (True, "2026-10-29T09:59:59+00:00"),   # стикер-привет бота
        )
        status = run(make_intro(db).send_intro(client, "dvai_2", due_row()))
        assert status == INTRO_SENT

    def test_missed_reply_found_in_history(self) -> None:
        """Ответ пришёл, пока бот лежал — сверка по истории планирует анкету."""
        greeted = datetime.now(timezone.utc) - timedelta(hours=1)
        waiting = {"user_id": 77, "username": "rosa", "name": "Роза",
                   "status": WAITING_REPLY, "greeted_at": greeted.isoformat()}
        db = make_db(waiting)
        db.list_match_dialogs = AsyncMock(side_effect=[[waiting], []])
        client = make_client()
        reply_at = greeted + timedelta(minutes=10)

        async def iter_messages(*args, **kwargs):
            yield SimpleNamespace(out=True, date=greeted)
            yield SimpleNamespace(out=False, date=reply_at)

        client.iter_messages = iter_messages
        run(make_intro(db).process(client, "dvai_2", "AUTO"))
        kw = db.update_match_dialog.await_args.kwargs
        assert kw["status"] == SCHEDULED
        assert kw["replied_at"] == reply_at.isoformat()


class TestCollectorIntro:
    def _collector(self, mode: str = "AUTO"):
        collector, other, auto = _Greeting()._collector(mode)
        collector._db.find_match_profile = AsyncMock(return_value=3)
        collector._match_intro.enroll = AsyncMock(return_value=True)
        return collector, other, auto

    def test_greeting_sent_enrolls(self) -> None:
        collector, _, auto = self._collector()
        run(collector._process_message(_Greeting._task(auto)))
        args = collector._match_intro.enroll.await_args.args
        assert args[0] is auto and args[1] == "dvai_2" and args[3] == 3

    def test_greeting_not_sent_does_not_enroll(self) -> None:
        collector, _, auto = self._collector()
        collector._match_greeter.greet = AsyncMock(return_value="HAS_CHAT")
        run(collector._process_message(_Greeting._task(auto)))
        collector._match_intro.enroll.assert_not_called()

    def test_private_reply_goes_to_intro(self) -> None:
        collector, _, auto = self._collector()
        collector._match_intro.note_reply = AsyncMock(return_value=True)
        event = MagicMock(chat_id=77, sender_id=77, client=auto)
        event.message.date = datetime(2026, 10, 29, tzinfo=timezone.utc)
        run(collector._handle_private_message(event))
        args = collector._match_intro.note_reply.await_args.args
        assert args[:3] == ("dvai_2", 77, "AUTO")

    def test_leo_chat_is_not_a_reply(self) -> None:
        collector, _, auto = self._collector()
        collector._match_intro.note_reply = AsyncMock()
        event = MagicMock(chat_id=LEO, sender_id=LEO, client=auto)
        run(collector._handle_private_message(event))
        collector._match_intro.note_reply.assert_not_called()

    def test_run_notifies_owner_on_sent(self) -> None:
        collector, _, _ = self._collector()
        row = due_row(pinned=1)
        collector._match_intro.process = AsyncMock(
            side_effect=[[], [(row, INTRO_SENT)]]
        )
        run(collector.run_match_intros())
        text = collector._auto_engine.notify_owner.await_args.args[0]
        assert "Роза" in text and "анкету" in text and "закрепил" in text

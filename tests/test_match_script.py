# Сценарий знакомства после анкеты: ветки схемы владельца, таймеры, удаление чата.

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.errors import FloodWaitError

from app.config import MatchScriptConfig
from collectors.match_dialogs import script_notice
from collectors.match_intro import INTRO_SENT
from collectors.match_script import SILENCE, MatchScript
from database.database import Database
from services import match_script_rules as R
from tests.mocks import async_iter
from tests.test_match_intro import TestCollectorIntro as _Intro
from tests.test_match_intro import due_row

S = "dvai_2"
UID = 77


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def ago(**kw: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "t.db")
    run(database.connect())
    run(database.add_match_dialog(S, UID, username="rosa", name="Роза"))
    run(database.update_match_dialog(S, UID, status=INTRO_SENT))
    yield database
    run(database.close())


def make_script(db: object, **cfg: object) -> MatchScript:
    cfg.setdefault("quiet_hours", [0, 0])
    # «АЛООО» — в любое время (окно на сутки), кроме тестов самого окна.
    cfg.setdefault("wake_up_window_hours", 24)
    cfg.setdefault("reply_delay_sec", [0, 0])
    return MatchScript(MatchScriptConfig(**cfg), db)


#: Служебное «закрепил» — исходящее с action, сценарий его не считает владельцем.
PIN_SERVICE = SimpleNamespace(id=1, out=True, message="", action=object())


def make_client(history: list | None = None) -> AsyncMock:
    client = AsyncMock()
    client.get_input_entity = AsyncMock(return_value=SimpleNamespace(user_id=UID))
    client.send_message = AsyncMock(return_value=SimpleNamespace(id=300))
    client.delete_dialog = AsyncMock()
    # В её чате всегда есть хотя бы служебное «закрепил» — пустой чат значит
    # «владелец удалил» (бот молчит), это проверяется отдельно.
    items = history or [PIN_SERVICE]
    client.iter_messages = MagicMock(side_effect=lambda *a, **k: async_iter(items))
    return client


def her(msg_id: int, text: str) -> SimpleNamespace:
    return SimpleNamespace(id=msg_id, out=False, message=text, action=None)


def mine(msg_id: int, action: object = None) -> SimpleNamespace:
    return SimpleNamespace(id=msg_id, out=True, message="", action=action)


def row(db: Database) -> dict:
    return run(db.get_match_dialog(S, UID))


def step_to(db: Database, step: str, **fields: object) -> None:
    fields.setdefault("script_at", ago(minutes=1))
    fields.setdefault("script_msg_id", 100)
    run(db.update_match_dialog(S, UID, script_step=step, **fields))


def reply(db: Database, script: MatchScript, text: str, client=None):
    assert run(script.note_message(S, UID, text, "AUTO"))
    client = client or make_client()
    return client, run(script.process(client, S, "AUTO"))


def sent_text(client: AsyncMock) -> str | None:
    if not client.send_message.await_count:
        return None
    return client.send_message.await_args.args[1]


class TestClassify:
    @pytest.mark.parametrize("text,kind", [
        ("Что это?", R.ASKS_WHAT),
        ("зачем ты мне мою анкету скинул", R.ASKS_PIN),
        ("Почему ты это скинул мне", R.ASKS_PIN),
        ("зачем отправил", R.ASKS_PIN),
        ("а чего ты мне это прислал", R.ASKS_PIN),
        ("я тебе фото скинула", R.OTHER),  # её «скинула» — не вопрос про анкету
        ("а нахрена", R.ASKS_WHAT),
        ("А для чего закрепил?", R.ASKS_PIN),
        ("зачем закрепил мою анкету", R.ASKS_PIN),
        ("а чего запинил", R.ASKS_PIN),
        ("?", R.ABOUT_WHAT),
        ("что?", R.ABOUT_WHAT),
        ("О чём?", R.ABOUT_WHAT),
        ("а что рассказывать", R.ABOUT_WHAT),
        ("Слишком много с кем общаешься, решил чтобы не перепутать?", R.ASKS_TOO_MANY),
        ("Помечаешь чтоб не забыть или че", R.ASKS_TOO_MANY),
        ("зачем пометил?", R.ASKS_PIN),
        ("а чего помечаешь", R.ASKS_PIN),
        ("я не дамочка", R.NOT_DAMOCHKA),
        ("Какая я тебе дамочка", R.NOT_DAMOCHKA),
        ("ты бот?", R.ASKS_BOT),
        ("не пиши мне больше", R.GO_AWAY),
        ("у меня есть парень", R.GO_AWAY),
        ("иди нахуй", R.RUDE),
        ("ты дебил", R.RUDE),
        ("ахах пиздец", R.OTHER),       # мат не по адресу — не грубость
        ("Мне 20, учусь в политехе", R.OTHER),
        ("", R.GREETING),
        ("😊", R.GREETING),
        ("Привет)", R.GREETING),
        ("хай", R.GREETING),
        ("Привет, я Лена", R.OTHER),
        ("привлекательная", R.OTHER),
        ("работаю дизайнером", R.OTHER),  # «бот» внутри слова
    ])
    def test_kinds(self, text: str, kind: str) -> None:
        assert R.classify(text) == kind

    def test_strongest_prefers_stop(self) -> None:
        assert R.strongest([R.ASKS_WHAT, R.GO_AWAY, R.OTHER]) == R.GO_AWAY
        assert R.strongest([R.OTHER, R.ASKS_WHAT]) == R.ASKS_WHAT

    def test_pin_question_beats_generic_what(self) -> None:
        """Прод 2026-11-02: «Ну да, я» + «А для чего закрепил?» → не «Пусть лежит здесь»."""
        kinds = [R.classify("Ну да, я"), R.classify("А для чего закрепил?")]
        assert R.strongest(kinds) == R.ASKS_PIN
        assert R.strongest([R.ASKS_PIN, R.ASKS_WHAT]) == R.ASKS_PIN


class TestHerQuestion:
    """Её свой вопрос — бот молчит, переписка владельцу (прод 2026-11-04,
    @Pivo2007: «а че» + «ну вы любите розы?» → бот ответил «О себе конечно»)."""

    def test_unknown_question_is_her_question(self) -> None:
        assert R.classify("ну вы любите розы?") == R.HER_QUESTION
        assert R.classify("ну вы любите розы") == R.OTHER

    @pytest.mark.parametrize("text,kind", [
        ("?", R.ABOUT_WHAT), ("а че", R.ABOUT_WHAT), ("о чём?", R.ABOUT_WHAT),
        ("что это?", R.ASKS_WHAT), ("а для чего закрепил?", R.ASKS_PIN),
        ("ты бот?", R.ASKS_BOT),
    ])
    def test_known_questions_keep_their_kind(self, text: str, kind: str) -> None:
        assert R.classify(text) == kind

    def test_her_question_beats_reask(self) -> None:
        kinds = [R.classify("а че"), R.classify("ну вы любите розы?")]
        kind = R.strongest(kinds)
        assert kind == R.HER_QUESTION
        assert R.decide(R.TELL_ME, kind) == (None, R.HANDOFF)

    def test_pin_question_beats_her_question(self) -> None:
        assert R.strongest([R.ASKS_PIN, R.HER_QUESTION]) == R.ASKS_PIN

    @pytest.mark.parametrize("step", list(R.ACTIVE_STEPS))
    def test_her_question_hands_off_on_any_step(self, step: str) -> None:
        assert R.decide(step, R.HER_QUESTION) == (None, R.HANDOFF)


class TestDecide:
    @pytest.mark.parametrize("step,kind,expected", [
        (R.AFTER_PIN, R.ASKS_WHAT, ("card_here", R.WAIT_MORE)),
        (R.AFTER_PIN, R.ABOUT_WHAT, ("card_here", R.WAIT_MORE)),
        (R.AFTER_PIN, R.ASKS_TOO_MANY, ("convenient", R.WAIT_MORE)),
        (R.AFTER_PIN, R.ASKS_PIN, ("pin_why", R.WAIT_MORE)),
        (R.WAIT_MORE, R.ASKS_PIN, ("pin_why", R.WAIT_MORE)),
        (R.AFTER_PIN, R.GREETING, ("tell_me", R.TELL_ME)),
        (R.AFTER_PIN, R.OTHER, (None, R.HANDOFF)),
        (R.AFTER_PIN, R.NOT_DAMOCHKA, (None, R.HANDOFF)),
        (R.WAIT_MORE, R.ASKS_TOO_MANY, ("convenient", R.WAIT_MORE)),
        (R.WAIT_MORE, R.OTHER, (None, R.HANDOFF)),
        (R.WAIT_MORE, R.ASKS_WHAT, (None, R.HANDOFF)),
        (R.WAKE_UP, R.OTHER, (None, R.HANDOFF)),
        (R.WAKE_UP, R.GO_AWAY, (None, R.DELETED)),
        (R.TELL_ME, R.NOT_DAMOCHKA, ("who_then", R.HANDOFF)),
        (R.TELL_ME, R.ABOUT_WHAT, ("about_what", R.HANDOFF)),
        (R.TELL_ME, R.OTHER, (None, R.HANDOFF)),
        (R.TELL_ME, R.GO_AWAY, (None, R.DELETED)),
        (R.AFTER_PIN, R.RUDE, (None, R.DELETED)),
        (R.AFTER_PIN, R.ASKS_BOT, (None, R.HANDOFF)),
    ])
    def test_table(self, step: str, kind: str, expected: tuple) -> None:
        assert R.decide(step, kind) == expected

    @pytest.mark.parametrize("step", [R.AFTER_PIN, R.WAIT_MORE, R.TELL_ME, R.WAKE_UP])
    def test_unrecognized_text_hands_off(self, step: str) -> None:
        """«Не знаешь, что ответить — передавай мне» (владелец, 2026-11-07)."""
        assert R.decide(step, R.OTHER) == (None, R.HANDOFF)

    def test_silence(self) -> None:
        assert R.decide_silence(R.AFTER_PIN) == ("tell_me", R.TELL_ME)
        assert R.decide_silence(R.WAIT_MORE) == ("tell_me", R.TELL_ME)
        assert R.decide_silence(R.TELL_ME) == ("wake_up", R.WAKE_UP)
        assert R.decide_silence(R.WAKE_UP) == (None, R.STOPPED)

    def test_hour_window(self) -> None:
        at = datetime(2026, 11, 2, 19, 30, tzinfo=timezone.utc)  # 22:30 МСК
        assert R.in_hour_window(at, 22, 2, 3)
        assert not R.in_hour_window(at - timedelta(hours=1), 22, 2, 3)
        assert not R.in_hour_window(at + timedelta(hours=2), 22, 2, 3)
        assert R.in_hour_window(at, 23, 2, 3) is False
        assert R.in_hour_window(at + timedelta(hours=1), 23, 2, 3)
        assert not R.in_hour_window(at, 22, 0, 3)


class TestScriptDb:
    def test_old_db_gets_script_columns(self, tmp_path) -> None:
        path = tmp_path / "old.db"
        con = sqlite3.connect(path)
        con.execute(
            """CREATE TABLE match_dialogs (account_session TEXT NOT NULL,
            user_id INTEGER NOT NULL, username TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '', profile_id INTEGER,
            status TEXT NOT NULL, greeted_at TEXT NOT NULL, replied_at TEXT,
            due_at TEXT, sent_at TEXT, pinned INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (account_session, user_id))"""
        )
        con.execute(
            "INSERT INTO match_dialogs (account_session, user_id, status, "
            "greeted_at) VALUES ('dvai', 5, 'INTRO_SENT', 'x')"
        )
        con.commit()
        con.close()
        for _ in range(2):  # миграция идемпотентна
            database = Database(path)
            run(database.connect())
            got = run(database.get_match_dialog("dvai", 5))
            run(database.close())
        assert got["script_step"] == "" and got["script_msg_id"] == 0

    def test_list_only_active_intro_sent(self, db: Database) -> None:
        run(db.add_match_dialog(S, 8))
        run(db.update_match_dialog(S, 8, script_step=R.AFTER_PIN))  # WAITING
        assert run(db.list_match_script_dialogs(S, R.ACTIVE_STEPS)) == []
        step_to(db, R.TELL_ME)
        rows = run(db.list_match_script_dialogs(S, R.ACTIVE_STEPS))
        assert [r["user_id"] for r in rows] == [UID]
        step_to(db, R.HANDOFF)
        assert run(db.list_match_script_dialogs(S, R.ACTIVE_STEPS)) == []


class TestMatchScript:
    def test_start_after_intro(self, db: Database) -> None:
        assert run(make_script(db).start(S, UID, 120))
        got = row(db)
        assert got["script_step"] == R.AFTER_PIN and got["script_msg_id"] == 120

    def test_asks_what_card_here(self, db: Database) -> None:
        step_to(db, R.AFTER_PIN)
        client, results = reply(db, make_script(db), "Что это?")
        assert sent_text(client) == "Это твоя анкета из Дайвинчика) Закрепил, чтобы не потерять тебя среди чатов"
        got = row(db)
        assert got["script_step"] == R.WAIT_MORE
        assert got["script_msg_id"] == 300 and got["script_pending"] == ""
        assert got["script_variant"] == "card_here:0"
        assert results[0][1] == R.WAIT_MORE

    def test_pin_question_answered_not_card_here(self, db: Database) -> None:
        """Прод 2026-11-02 (Рина): «Ну да, я» + «А для чего закрепил?»."""
        step_to(db, R.AFTER_PIN)
        script = make_script(db)
        assert run(script.note_message(S, UID, "Ну да, я", "AUTO"))
        client, _ = reply(db, script, "А для чего закрепил?")
        assert sent_text(client) == "Это твоя анкета из Дайвинчика) Закрепил, чтобы не потерять тебя среди чатов"
        got = row(db)
        assert got["script_step"] == R.WAIT_MORE
        assert got["script_variant"] == "pin_why:0"

    def test_why_sent_answered_not_card_here(self, db: Database) -> None:
        """Прод 2026-11-03: «Почему ты это скинул мне» → не «Пусть лежит здесь»."""
        step_to(db, R.AFTER_PIN)
        client, _ = reply(db, make_script(db), "Почему ты это скинул мне")
        assert sent_text(client) == "Это твоя анкета из Дайвинчика) Закрепил, чтобы не потерять тебя среди чатов"
        assert row(db)["script_variant"] == "pin_why:0"

    def test_mark_not_to_forget_answered_convenient(self, db: Database) -> None:
        """Прод 2026-11-04: «Помечаешь чтоб не забыть или че» → не «Рассказывайте»."""
        step_to(db, R.AFTER_PIN)
        client, _ = reply(db, make_script(db), "Помечаешь чтоб не забыть или че")
        assert sent_text(client) == "Это так-то удобно"
        assert row(db)["script_step"] == R.WAIT_MORE

    def test_too_many_then_convenient(self, db: Database) -> None:
        step_to(db, R.WAIT_MORE)
        client, _ = reply(
            db, make_script(db), "много с кем общаешься, чтобы не перепутать?",
        )
        assert sent_text(client) == "Это так-то удобно"
        assert row(db)["script_step"] == R.WAIT_MORE

    def test_reply_waits_for_delay(self, db: Database) -> None:
        step_to(db, R.AFTER_PIN)
        script = make_script(db, reply_delay_sec=[60, 180])
        assert run(script.note_message(S, UID, "что это", "AUTO"))
        client = make_client()
        assert run(script.process(client, S, "AUTO")) == []
        client.send_message.assert_not_called()
        assert row(db)["script_pending"] == R.ASKS_WHAT

    def test_silence_day_tell_me(self, db: Database) -> None:
        """Схема владельца 2026-11-02: молчит сутки после анкеты."""
        step_to(db, R.AFTER_PIN, script_at=ago(hours=24, minutes=1))
        client = make_client()
        run(make_script(db).process(client, S, "AUTO"))
        assert sent_text(client) == "Рассказывайте, дамочка"
        assert row(db)["script_step"] == R.TELL_ME

    def test_silence_not_yet(self, db: Database) -> None:
        step_to(db, R.AFTER_PIN, script_at=ago(hours=23, minutes=50))
        client = make_client()
        assert run(make_script(db).process(client, S, "AUTO")) == []
        client.send_message.assert_not_called()

    def test_quiet_hours_hold_nudge(self, db: Database) -> None:
        step_to(db, R.AFTER_PIN, script_at=ago(hours=25))
        hour = datetime.now(timezone.utc).hour
        script = make_script(
            db, quiet_hours=[hour, (hour + 1) % 24], utc_offset_hours=0,
        )
        client = make_client()
        run(script.process(client, S, "AUTO"))
        client.send_message.assert_not_called()
        assert row(db)["script_step"] == R.AFTER_PIN

    def test_not_damochka(self, db: Database) -> None:
        step_to(db, R.TELL_ME)
        client, results = reply(db, make_script(db), "я не дамочка")
        assert sent_text(client) == "А кто? Парнишка?"
        assert row(db)["script_step"] == R.HANDOFF
        assert results[0][0]["script_text"] == "А кто? Парнишка?"

    def test_about_what_ab_variant_recorded(self, db: Database) -> None:
        step_to(db, R.TELL_ME)
        client, _ = reply(db, make_script(db), "о чём?")
        variants = MatchScriptConfig().texts.about_what
        text = sent_text(client)
        assert text in variants
        assert row(db)["script_variant"] == f"about_what:{variants.index(text)}"

    def test_her_story_goes_to_owner(self, db: Database) -> None:
        step_to(db, R.TELL_ME)
        client, results = reply(db, make_script(db), "Мне 20, учусь в политехе")
        client.send_message.assert_not_called()
        assert row(db)["script_step"] == R.HANDOFF
        assert results[0][1] == R.HANDOFF

    def test_go_away_deletes_chat_at_once(self, db: Database) -> None:
        step_to(db, R.AFTER_PIN)
        script = make_script(db, reply_delay_sec=[600, 600])
        client, results = reply(db, script, "не пиши мне")
        client.delete_dialog.assert_awaited_once()
        assert client.delete_dialog.await_args.kwargs["revoke"] is True
        client.send_message.assert_not_called()
        assert row(db)["script_step"] == R.DELETED
        assert results[0][1] == R.DELETED

    def test_delete_only_mine(self, db: Database) -> None:
        step_to(db, R.TELL_ME)
        client, _ = reply(db, make_script(db, delete_for_both=False), "отвали")
        assert client.delete_dialog.await_args.kwargs["revoke"] is False

    def test_asks_bot_hands_off_without_lying(self, db: Database) -> None:
        step_to(db, R.AFTER_PIN)
        client, results = reply(db, make_script(db), "ты бот?")
        client.send_message.assert_not_called()
        assert results[0][1:] == (R.HANDOFF, R.ASKS_BOT)

    def test_owner_wrote_silences_bot(self, db: Database) -> None:
        step_to(db, R.AFTER_PIN)
        client = make_client([her(103, "что это"), mine(102), her(101, "ой")])
        _, results = reply(db, make_script(db), "что это", client)
        client.send_message.assert_not_called()
        assert row(db)["script_step"] == R.OWNER
        assert results[0][1] == R.OWNER

    def test_own_wake_message_is_not_owner(self, db: Database) -> None:
        """Своё «АЛООО» (wake_msg_id) не считается сообщением владельца."""
        step_to(db, R.AFTER_PIN, wake_msg_id=120)
        client = make_client([her(130, "привет"), mine(120), her(101, "привет")])
        run(make_script(db).process(client, S, "AUTO"))
        assert row(db)["script_step"] != R.OWNER

    def test_deleted_chat_silences_bot(self, db: Database) -> None:
        """Владелец удалил её чат (история пуста) — бот не пишет «АЛООО» /
        «Рассказывайте» туда, чат не возвращается (владелец, 2026-11-03)."""
        step_to(db, R.AFTER_PIN, script_at=ago(hours=25))
        client = make_client()
        client.iter_messages = MagicMock(side_effect=lambda *a, **k: async_iter([]))
        run(make_script(db).process(client, S, "AUTO"))
        client.send_message.assert_not_called()
        assert row(db)["script_step"] == R.OWNER

    def test_pin_service_message_is_not_owner(self, db: Database) -> None:
        step_to(db, R.AFTER_PIN)
        client = make_client([her(102, "что это"), mine(101, action=object()),
                              mine(100)])
        reply(db, make_script(db), "что это", client)
        assert sent_text(client) == "Это твоя анкета из Дайвинчика) Закрепил, чтобы не потерять тебя среди чатов"

    def test_missed_reply_found_on_silence(self, db: Database) -> None:
        """Ответила, пока бот лежал: по таймеру — ответ на её вопрос."""
        step_to(db, R.AFTER_PIN, script_at=ago(hours=25))
        client = make_client([her(101, "зачем это"), mine(100)])
        run(make_script(db).process(client, S, "AUTO"))
        assert sent_text(client) == "Это твоя анкета из Дайвинчика) Закрепил, чтобы не потерять тебя среди чатов"

    def test_wake_up_after_tell_me_silence(self, db: Database) -> None:
        """Молчит сутки после «Рассказывайте» → «АЛООО ПРОСЫПАЕМСЯ»."""
        step_to(db, R.TELL_ME, script_at=ago(hours=25))
        client = make_client()
        results = run(make_script(db).process(client, S, "AUTO"))
        assert sent_text(client) == "АЛООО ПРОСЫПАЕМСЯ"
        assert row(db)["script_step"] == R.WAKE_UP
        assert results[0][1:] == (R.WAKE_UP, SILENCE)

    def test_wake_up_waits_for_evening(self, db: Database) -> None:
        """«АЛООО» — только в вечернее окно (ближайшие 22:00)."""
        step_to(db, R.TELL_ME, script_at=ago(hours=25))
        hour = (datetime.now(timezone.utc).hour + 2) % 24
        script = make_script(
            db, wake_up_hour=hour, wake_up_window_hours=1, utc_offset_hours=0,
        )
        client = make_client()
        assert run(script.process(client, S, "AUTO")) == []
        client.send_message.assert_not_called()
        assert row(db)["script_step"] == R.TELL_ME

    def test_reply_to_wake_up_goes_to_owner(self, db: Database) -> None:
        step_to(db, R.WAKE_UP)
        client, results = reply(db, make_script(db), "ой, привет")
        client.send_message.assert_not_called()
        assert row(db)["script_step"] == R.HANDOFF
        assert results[0][1] == R.HANDOFF

    def test_give_up_after_wake_up(self, db: Database) -> None:
        step_to(db, R.WAKE_UP, script_at=ago(hours=25))
        client = make_client()
        results = run(make_script(db).process(client, S, "AUTO"))
        client.send_message.assert_not_called()
        assert row(db)["script_step"] == R.STOPPED
        assert results[0][1:] == (R.STOPPED, SILENCE)

    def test_specific_reply_not_overridden_by_chatter(self, db: Database) -> None:
        step_to(db, R.AFTER_PIN)
        script = make_script(db, reply_delay_sec=[600, 600])
        run(script.note_message(S, UID, "что это?", "AUTO"))
        run(script.note_message(S, UID, "ахах", "AUTO"))
        assert row(db)["script_pending"] == R.ASKS_WHAT
        run(script.note_message(S, UID, "отстань", "AUTO"))
        assert row(db)["script_pending"] == R.GO_AWAY

    @pytest.mark.parametrize("mode", ["OBSERVE", "SEMI_AUTO"])
    def test_only_in_auto(self, db: Database, mode: str) -> None:
        step_to(db, R.AFTER_PIN)
        assert not run(make_script(db).note_message(S, UID, "что это", mode))

    def test_ignored_before_script_start(self, db: Database) -> None:
        run(db.update_match_dialog(S, UID, script_step=""))
        assert not run(make_script(db).note_message(S, UID, "что это", "AUTO"))

    def test_old_message_ignored(self, db: Database) -> None:
        step_to(db, R.AFTER_PIN)
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        assert not run(make_script(db).note_message(S, UID, "что", "AUTO", at=old))

    def test_flood_postpones(self, db: Database) -> None:
        step_to(db, R.AFTER_PIN)
        client = make_client()
        client.send_message = AsyncMock(side_effect=FloodWaitError(request=None, capture=120))
        _, results = reply(db, make_script(db), "что это", client)
        assert results == []
        got = row(db)
        assert got["script_step"] == R.AFTER_PIN
        assert got["script_pending"] == R.ASKS_WHAT and got["script_due_at"]

    def test_error_marks_row_and_continues(self, db: Database) -> None:
        step_to(db, R.AFTER_PIN)
        client = make_client()
        client.send_message = AsyncMock(side_effect=RuntimeError("boom"))
        _, results = reply(db, make_script(db), "что это", client)
        assert results == []
        assert row(db)["script_step"] == R.ERROR

    def test_disabled(self, db: Database) -> None:
        step_to(db, R.AFTER_PIN, script_at=ago(hours=5))
        script = make_script(db, enabled=False)
        assert not run(script.start(S, UID, 1))
        assert run(script.process(make_client(), S, "AUTO")) == []


class TestAndSo:
    """«и?» / «и что?» — бот молчит, владельцу (владелец, 2026-11-07)."""

    @pytest.mark.parametrize("text", [
        "и?", "И что?", "ну и?", "и чё?", "Моя анкета в дв, и?",
    ])
    def test_classified(self, text: str) -> None:
        assert R.classify(text) == R.AND_SO

    @pytest.mark.parametrize("step", [R.AFTER_PIN, R.WAIT_MORE])
    def test_handoff_without_reply(self, step: str) -> None:
        assert R.decide(step, R.AND_SO) == (None, R.HANDOFF)

    def test_pin_question_still_answered(self) -> None:
        assert R.classify("зачем закрепил и?") == R.ASKS_PIN
        assert R.classify("что это?") == R.ASKS_WHAT

    def test_end_to_end_silent(self, db: Database) -> None:
        step_to(db, R.AFTER_PIN)
        client, _ = reply(db, make_script(db), "Моя анкета в дв, и?")
        assert sent_text(client) is None
        assert row(db)["script_step"] == R.HANDOFF


class TestAsksKnow:
    """«Мы знакомы?» / «кто ты?» (владелец, 2026-11-07)."""

    @pytest.mark.parametrize("text", [
        "мы знакомы", "знакомы?", "кто ты", "Кто это?", "откуда ты", "откуда ты меня",
        "мы где-то виделись",
    ])
    def test_classified(self, text: str) -> None:
        assert R.classify(text) == R.ASKS_KNOW

    def test_priority(self) -> None:
        assert R.strongest([R.HER_QUESTION, R.ASKS_KNOW]) == R.ASKS_KNOW
        assert R.strongest([R.ASKS_KNOW, R.ASKS_PIN]) == R.ASKS_PIN

    @pytest.mark.parametrize("step", [R.AFTER_PIN, R.WAIT_MORE])
    def test_decide(self, step: str) -> None:
        assert R.decide(step, R.ASKS_KNOW) == ("know_after", R.WAIT_MORE)
        assert R.decide(R.TELL_ME, R.ASKS_KNOW) == (None, R.HANDOFF)

    def test_reply_text_and_step(self, db: Database) -> None:
        step_to(db, R.AFTER_PIN)
        client, _ = reply(db, make_script(db), "мы знакомы?")
        assert sent_text(client) == "Пока нет, лайкнули друг друга в Дайвинчике)"
        assert row(db)["script_step"] == R.WAIT_MORE

    def test_before_card_decision(self) -> None:
        assert R.decide_before_card(["?"]) == "know"
        assert R.decide_before_card(["привет", "кто ты?"]) == "know"
        assert R.decide_before_card(["скинешь фотку?"]) == "handoff"
        assert R.decide_before_card(["привет", ""]) == "card"


class TestOneBatchOneReply:
    """Прод 2026-11-07 (Looney): «Моя анкета в дв, и?» → «Это твоя анкета…»,
    через 39 с без её сообщения — «Рассказывайте, дамочка»."""

    def test_after_other_text_hands_off_silently(self, db: Database) -> None:
        step_to(db, R.AFTER_PIN)
        client, results = reply(db, make_script(db), "Мне 20, учусь в политехе")
        assert sent_text(client) is None
        assert row(db)["script_step"] == R.HANDOFF
        assert results[0][1] == R.HANDOFF

    def test_after_sticker_tell_me(self, db: Database) -> None:
        step_to(db, R.AFTER_PIN)
        client, _ = reply(db, make_script(db), "")
        assert sent_text(client) == "Рассказывайте, дамочка"

    def test_concurrent_passes_reply_once(self, db: Database) -> None:
        step_to(db, R.AFTER_PIN)
        script = make_script(db)
        assert run(script.note_message(S, UID, "Что это?", "AUTO"))
        client = make_client()

        async def both():
            return await asyncio.gather(
                script.process(client, S, "AUTO"), script.process(client, S, "AUTO"),
            )

        run(both())
        assert client.send_message.await_count == 1
        assert row(db)["script_step"] == R.WAIT_MORE

    def test_stale_snapshot_row_is_not_answered(self, db: Database) -> None:
        """Строка из выборки устарела (шаг уже ушёл вперёд) — по ней не пишем."""
        step_to(db, R.WAIT_MORE, script_pending="", script_due_at="")
        script = make_script(db)
        stale = dict(row(db), script_step=R.AFTER_PIN, script_pending=SILENCE,
                     script_at=ago(hours=30), script_due_at=ago(minutes=1))
        client = make_client()
        assert run(script._process_row(client, S, stale)) is None
        client.send_message.assert_not_called()

    def test_leftover_silence_timer_after_fresh_reply_dropped(self, db: Database) -> None:
        """Висящий отложенный таймер молчания не срабатывает сразу после
        свежего ответа бота."""
        step_to(db, R.WAIT_MORE, script_pending=SILENCE, script_due_at=ago(minutes=1))
        client = make_client()
        assert run(make_script(db).process(client, S, "AUTO")) == []
        client.send_message.assert_not_called()
        assert row(db)["script_pending"] == ""


class TestSilentWakeUpDeletesChat:
    """Молчит и после «АЛООО» (шаг wake_up) → stopped + чат удалён у владельца."""

    def test_deleted_for_owner_only(self, db: Database) -> None:
        step_to(db, R.WAKE_UP, script_at=ago(hours=25))
        client = make_client()
        db.set_dialog_chat_deleted = AsyncMock()
        results = run(make_script(db).process(client, S, "AUTO"))
        assert results[0][1] == R.STOPPED
        db.set_dialog_chat_deleted.assert_awaited_once_with(S, UID, True)
        client.delete_dialog.assert_awaited_once()
        assert client.delete_dialog.await_args.kwargs["revoke"] is False
        assert row(db)["script_step"] == R.STOPPED

    def test_not_deleted_when_disabled(self, db: Database) -> None:
        step_to(db, R.WAKE_UP, script_at=ago(hours=25))
        client = make_client()
        run(make_script(db, delete_silent_chat=False).process(client, S, "AUTO"))
        client.delete_dialog.assert_not_called()
        assert row(db)["script_step"] == R.STOPPED

    def test_delete_error_still_stops(self, db: Database) -> None:
        step_to(db, R.WAKE_UP, script_at=ago(hours=25))
        client = make_client()
        client.delete_dialog = AsyncMock(side_effect=RuntimeError("boom"))
        run(make_script(db).process(client, S, "AUTO"))
        assert row(db)["script_step"] == R.STOPPED


class TestCollectorScript:
    def test_intro_sent_starts_script(self) -> None:
        collector, _, _ = _Intro()._collector()
        row = due_row(pinned=1, sent_msg_id=12)
        collector._match_intro.process = AsyncMock(
            side_effect=[[], [(row, INTRO_SENT)]]
        )
        collector._match_script.start = AsyncMock(return_value=True)
        collector._match_script.process = AsyncMock(return_value=[])
        run(collector.run_match_intros())
        assert collector._match_script.start.await_args.args == (S, 77, 12)

    def test_handoff_notifies_owner(self) -> None:
        collector, _, _ = _Intro()._collector()
        collector._match_intro.process = AsyncMock(return_value=[])
        row = {"user_id": 77, "username": "rosa", "name": "Роза",
               "script_text": "А кто? Парнишка?"}
        collector._match_script.process = AsyncMock(
            side_effect=[[], [(row, R.HANDOFF, R.NOT_DAMOCHKA)]]
        )
        run(collector.run_match_intros())
        text = collector._auto_engine.notify_owner.await_args.args[0]
        assert "Роза" in text and "переписка твоя" in text

    def test_private_message_text_goes_to_script(self) -> None:
        collector, _, auto = _Intro()._collector()
        collector._match_intro.note_reply = AsyncMock(return_value=False)
        collector._match_script.note_message = AsyncMock(return_value=True)
        event = MagicMock(chat_id=77, sender_id=77, client=auto)
        event.message.message = "что это?"
        event.message.date = datetime.now(timezone.utc)
        run(collector._handle_private_message(event))
        args = collector._match_script.note_message.await_args.args
        assert args[:4] == (S, 77, "что это?", "AUTO")


class TestScriptNotice:
    ROW = {"user_id": 77, "username": "rosa", "name": "Роза"}

    def test_deleted(self) -> None:
        assert "чат удалён" in script_notice(dict(self.ROW), R.DELETED, R.GO_AWAY, S)

    def test_bot_question(self) -> None:
        assert "не бот ли" in script_notice(dict(self.ROW), R.HANDOFF, R.ASKS_BOT, S)

    def test_owner_takeover_is_silent(self) -> None:
        assert script_notice(dict(self.ROW), R.OWNER, R.OTHER, S) == ""

    def test_notice_has_no_her_text(self) -> None:
        text = script_notice(dict(self.ROW), R.HANDOFF, R.OTHER, S)
        assert "не по сценарию" in text


class TestDialogsLabel:
    def test_script_step_shown_after_intro(self) -> None:
        from web.blueprints.dialogs import intro_label

        assert intro_label({"intro_status": "INTRO_SENT", "script_step": "tell_me"}) \
            == "сценарий: «Рассказывайте, дамочка»"
        assert intro_label({"intro_status": "INTRO_SENT", "script_step": ""}) \
            == "анкета отправлена"
        assert intro_label({"intro_status": "OWNER_CHAT", "script_step": "x"}) \
            == "пишешь сам — анкету не слал"

# Архив Telegram для переписок, которые ведёт бот (владелец, 2026-11-04).

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from collectors.chat_archive import (
    ARCHIVE,
    ARCHIVE_FOLDER,
    MAIN_FOLDER,
    MAX_MOVES_PER_PASS,
    UNARCHIVE,
    ChatArchiver,
    archive_target,
)
from collectors.match_intro import (
    INTRO_SENT,
    MAIN_CHAT,
    NO_PROFILE,
    NO_REPLY,
    OWNER_CHAT,
    SCHEDULED,
    WAITING_REPLY,
)
from database.database import Database
from services import match_script_rules as R

S = "dvai_2"


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "t.db")
    run(database.connect())
    yield database
    run(database.close())


def add(db: Database, uid: int, **fields: object) -> None:
    run(db.add_match_dialog(S, uid, username=f"girl{uid}", name=f"G{uid}"))
    if fields:
        run(db.update_match_dialog(S, uid, **fields))


def make_client() -> AsyncMock:
    client = AsyncMock()
    client.get_input_entity = AsyncMock(side_effect=lambda who: f"peer:{who}")
    client.edit_folder = AsyncMock(return_value=None)
    return client


def moves(client: AsyncMock) -> dict[str, int]:
    return {c.args[0]: c.args[1] for c in client.edit_folder.await_args_list}


class TestArchiveTarget:
    @pytest.mark.parametrize("row", [
        {"status": WAITING_REPLY},
        {"status": SCHEDULED},
        {"status": INTRO_SENT, "script_step": ""},
        {"status": INTRO_SENT, "script_step": R.AFTER_PIN},
        {"status": INTRO_SENT, "script_step": R.WAIT_MORE},
        {"status": INTRO_SENT, "script_step": R.TELL_ME},
        {"status": INTRO_SENT, "script_step": R.WAKE_UP},
    ])
    def test_bot_leads_archived(self, row: dict) -> None:
        assert archive_target(row, bot_on=True) == ARCHIVE

    def test_bot_off_leaves_chat_for_owner(self) -> None:
        row = {"status": INTRO_SENT, "script_step": R.TELL_ME}
        assert archive_target(row, bot_on=False) == UNARCHIVE

    @pytest.mark.parametrize("row", [
        {"status": NO_REPLY},
        {"status": MAIN_CHAT},
        {"status": INTRO_SENT, "script_step": R.STOPPED},
    ])
    def test_silent_girl_archived(self, row: dict) -> None:
        assert archive_target(row, bot_on=True) == ARCHIVE
        assert archive_target(row, bot_on=False) == ARCHIVE

    @pytest.mark.parametrize("row", [
        {"status": INTRO_SENT, "script_step": R.HANDOFF},
        {"status": INTRO_SENT, "script_step": R.OWNER},
        {"status": INTRO_SENT, "script_step": R.ERROR},
        {"status": OWNER_CHAT},
        {"status": NO_PROFILE},
    ])
    def test_needs_owner_unarchived(self, row: dict) -> None:
        assert archive_target(row, bot_on=True) == UNARCHIVE

    def test_deleted_chat_untouched(self) -> None:
        row = {"status": INTRO_SENT, "script_step": R.DELETED}
        assert archive_target(row, bot_on=True) is None


class TestChatArchiver:
    def test_bot_chat_archived_handoff_unarchived(self, db: Database) -> None:
        add(db, 1)  # WAITING_REPLY
        add(db, 2, status=INTRO_SENT, script_step=R.HANDOFF)
        client = make_client()
        assert run(ChatArchiver(db).sync(client, S, bot_on=True)) == 2
        assert moves(client) == {
            "peer:girl1": ARCHIVE_FOLDER, "peer:girl2": MAIN_FOLDER,
        }

    def test_same_state_not_moved_twice(self, db: Database) -> None:
        add(db, 1)
        archiver = ChatArchiver(db)
        run(archiver.sync(make_client(), S, bot_on=True))
        client = make_client()
        assert run(archiver.sync(client, S, bot_on=True)) == 0
        client.edit_folder.assert_not_awaited()

    def test_bot_answer_archives_again(self, db: Database) -> None:
        """Её сообщение вынуло чат из архива; бот ответил — снова в архив."""
        add(db, 1, status=INTRO_SENT, script_step=R.AFTER_PIN,
            script_at="2026-11-04T10:00:00+00:00")
        archiver = ChatArchiver(db)
        run(archiver.sync(make_client(), S, bot_on=True))
        run(db.update_match_dialog(
            S, 1, script_step=R.WAIT_MORE,
            script_at=datetime.now(timezone.utc).isoformat(),
        ))
        client = make_client()
        run(archiver.sync(client, S, bot_on=True))
        assert moves(client) == {"peer:girl1": ARCHIVE_FOLDER}

    def test_handoff_takes_chat_out(self, db: Database) -> None:
        add(db, 1, status=INTRO_SENT, script_step=R.WAIT_MORE)
        archiver = ChatArchiver(db)
        run(archiver.sync(make_client(), S, bot_on=True))
        run(db.update_match_dialog(S, 1, script_step=R.HANDOFF))
        client = make_client()
        run(archiver.sync(client, S, bot_on=True))
        assert moves(client) == {"peer:girl1": MAIN_FOLDER}

    def test_error_marks_and_does_not_retry(self, db: Database) -> None:
        add(db, 1)
        client = make_client()
        client.edit_folder = AsyncMock(side_effect=ValueError("no peer"))
        archiver = ChatArchiver(db)
        assert run(archiver.sync(client, S, bot_on=True)) == 0
        again = make_client()
        run(archiver.sync(again, S, bot_on=True))
        again.edit_folder.assert_not_awaited()

    def test_moves_capped_per_pass(self, db: Database) -> None:
        for uid in range(1, MAX_MOVES_PER_PASS + 5):
            add(db, uid)
        client = make_client()
        assert run(ChatArchiver(db).sync(client, S, bot_on=True)) == (
            MAX_MOVES_PER_PASS
        )

    def test_disabled_does_nothing(self, db: Database) -> None:
        add(db, 1)
        client = make_client()
        assert run(ChatArchiver(db, enabled=False).sync(client, S, True)) == 0
        client.edit_folder.assert_not_awaited()

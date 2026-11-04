# Архив и папки Telegram для чатов девушек (владелец, 2026-11-04):
# New — вышла из автоматики, Active — общение устоялось, 50/50 — не идёт,
# Auto — ведёт бот. Все чаты — в архиве; перенёс сам — бот не трогает.

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.tl.functions.messages import (
    GetDialogFiltersRequest,
    UpdateDialogFilterRequest,
)
from telethon.tl.types import (
    DialogFilter,
    DialogFilterDefault,
    InputPeerUser,
    TextWithEntities,
)

from app.config import ChatFoldersConfig
from collectors.chat_folders import ARCHIVE_FOLDER, MAX_MOVES_PER_PASS, ChatFolders
from collectors.match_intro import INTRO_SENT, NO_REPLY, OWNER_CHAT, SCHEDULED
from database.database import Database
from services import match_script_rules as R
from services.chat_folders import ACTIVE, AUTO, FIFTY, NEW, folder_target

S = "dvai_2"
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "t.db")
    run(database.connect())
    yield database
    run(database.close())


def girl(
    db: Database, uid: int, *, her: int = 0, owner: int = 0,
    last_hours: float = 1.0, intro: dict | None = None,
) -> None:
    """Девушка-матч: контакт, сообщения (её / владельца), автопереписка."""
    run(db.upsert_dialog_contact(
        S, uid, username=f"girl{uid}", name=f"G{uid}", matched_at=ago(200),
    ))
    msg_id = 1
    for _ in range(her):
        run(db.save_dialog_message(
            S, uid, msg_id, is_outgoing=False, sent_by="her",
            message_date=ago(last_hours + 0.5),
        ))
        msg_id += 1
    for _ in range(owner):
        run(db.save_dialog_message(
            S, uid, msg_id, is_outgoing=True, sent_by="owner",
            message_date=ago(last_hours),
        ))
        msg_id += 1
    if intro is not None:
        run(db.add_match_dialog(S, uid, username=f"girl{uid}", name=f"G{uid}"))
        if intro:
            run(db.update_match_dialog(S, uid, **intro))


def folder(fid: int, title: str, *uids: int) -> DialogFilter:
    return DialogFilter(
        id=fid, title=TextWithEntities(text=title, entities=[]),
        pinned_peers=[],
        include_peers=[InputPeerUser(user_id=u, access_hash=1) for u in uids],
        exclude_peers=[],
    )


class FakeTelegram:
    """Папки аккаунта + запросы к ним."""

    def __init__(self, *filters: object) -> None:
        self.filters: list = [DialogFilterDefault(), *filters]
        self.updates: list[UpdateDialogFilterRequest] = []
        self.fail_update = False
        self.client = AsyncMock(side_effect=self._call)
        self.client.get_input_entity = AsyncMock(side_effect=self._entity)
        self.client.edit_folder = AsyncMock(return_value=None)

    @staticmethod
    def _entity(who: object) -> InputPeerUser:
        uid = int(str(who).removeprefix("girl"))
        return InputPeerUser(user_id=uid, access_hash=1)

    def _call(self, req: object) -> object:
        if isinstance(req, GetDialogFiltersRequest):
            return SimpleNamespace(filters=list(self.filters))
        if isinstance(req, UpdateDialogFilterRequest):
            if self.fail_update:
                raise RuntimeError("FILTER_INCLUDE_TOO_MUCH")
            self.updates.append(req)
            self.filters = [f for f in self.filters if getattr(f, "id", None) != req.id]
            if req.filter is not None:
                self.filters.append(req.filter)
            return True
        raise AssertionError(f"неожиданный запрос {req!r}")

    def members(self, title: str) -> set[int]:
        for f in self.filters:
            t = getattr(getattr(f, "title", None), "text", None)
            if t == title:
                return {p.user_id for p in f.include_peers}
        return set()


def folders(db: Database, *, archive: bool = False, **cfg: object) -> ChatFolders:
    return ChatFolders(db, ChatFoldersConfig(**cfg), archive=archive)


class TestFolderTarget:
    def _row(self, **kw: object) -> dict:
        base = {"her_count": 0, "owner_count": 0, "last_by": "",
                "last_message_at": NOW.isoformat(), "matched_at": NOW.isoformat()}
        base.update(kw)
        return base

    @pytest.mark.parametrize("intro", [
        {"intro_status": "WAITING_REPLY"},
        {"intro_status": SCHEDULED},
        {"intro_status": INTRO_SENT, "script_step": R.TELL_ME},
        {"intro_status": INTRO_SENT, "script_step": ""},
    ])
    def test_bot_leads_goes_to_auto(self, intro: dict) -> None:
        assert folder_target(self._row(**intro), bot_on=True, now=NOW) == AUTO

    def test_bot_off_is_new(self) -> None:
        row = self._row(intro_status=INTRO_SENT, script_step=R.TELL_ME)
        assert folder_target(row, bot_on=False, now=NOW) == NEW

    def test_handoff_is_new(self) -> None:
        row = self._row(intro_status=INTRO_SENT, script_step=R.HANDOFF,
                        her_count=3, last_by="her")
        assert folder_target(row, bot_on=True, now=NOW) == NEW

    def test_settled_talk_is_active(self) -> None:
        row = self._row(intro_status=INTRO_SENT, script_step=R.HANDOFF,
                        her_count=10, owner_count=3, last_by="owner")
        assert folder_target(row, bot_on=True, now=NOW) == ACTIVE

    def test_faded_is_fifty(self) -> None:
        row = self._row(intro_status=INTRO_SENT, script_step=R.HANDOFF,
                        her_count=12, owner_count=5, last_by="owner",
                        last_message_at=(NOW - timedelta(hours=80)).isoformat())
        assert folder_target(row, bot_on=True, now=NOW) == FIFTY

    def test_stopped_is_fifty(self) -> None:
        row = self._row(intro_status=INTRO_SENT, script_step=R.STOPPED, her_count=1)
        assert folder_target(row, bot_on=True, now=NOW) == FIFTY

    def test_old_no_reply_is_fifty(self) -> None:
        assert folder_target(self._row(intro_status=NO_REPLY), bot_on=True, now=NOW) == FIFTY

    @pytest.mark.parametrize("kw", [
        {"chat_deleted_at": "2026-11-04T10:00:00+00:00"},
        {"intro_status": INTRO_SENT, "script_step": R.DELETED},
    ])
    def test_deleted_chat_in_no_folder(self, kw: dict) -> None:
        assert folder_target(self._row(**kw), bot_on=True, now=NOW) is None


class TestChatFolders:
    def test_creates_missing_folders_and_sorts(self, db: Database) -> None:
        girl(db, 1, intro={})                                        # ждёт ответа
        girl(db, 2, her=3, intro={"status": INTRO_SENT, "script_step": R.HANDOFF})
        girl(db, 3, her=10, owner=3, intro={"status": INTRO_SENT, "script_step": R.HANDOFF})
        girl(db, 4, her=2, owner=2, last_hours=100)                  # затухла
        tg = FakeTelegram()
        assert run(folders(db).sync(tg.client, S, bot_on=True)) == 4
        assert tg.members("Auto") == {1}
        assert tg.members("New") == {2}
        assert tg.members("Active") == {3}
        assert tg.members("50/50") == {4}

    def test_existing_folder_found_by_title(self, db: Database) -> None:
        girl(db, 2, her=1, intro={"status": OWNER_CHAT})
        tg = FakeTelegram(folder(5, "new", 999))
        run(folders(db).sync(tg.client, S, bot_on=True))
        assert tg.members("new") == {999, 2}
        assert [u.id for u in tg.updates] == [5]

    def test_moves_between_folders_one_request_per_folder(self, db: Database) -> None:
        girl(db, 1, intro={})
        girl(db, 2, intro={})
        tg = FakeTelegram()
        f = folders(db)
        run(f.sync(tg.client, S, bot_on=True))
        assert tg.members("Auto") == {1, 2}
        tg.updates.clear()
        # «Авто» выключили — бот больше не ведёт, чаты в New.
        assert run(f.sync(tg.client, S, bot_on=False)) == 2
        assert tg.members("New") == {1, 2}
        # Auto опустела — Telegram пустую папку не держит, удалена.
        assert tg.members("Auto") == set()
        assert len(tg.updates) == 2

    def test_same_state_no_requests(self, db: Database) -> None:
        girl(db, 1, intro={})
        tg = FakeTelegram()
        f = folders(db)
        run(f.sync(tg.client, S, bot_on=True))
        tg.updates.clear()
        assert run(f.sync(tg.client, S, bot_on=True)) == 0
        assert tg.updates == []

    def test_owner_move_is_respected(self, db: Database) -> None:
        girl(db, 1, intro={})
        tg = FakeTelegram()
        f = folders(db)
        run(f.sync(tg.client, S, bot_on=True))
        # Владелец перенёс её из Auto в Active руками.
        auto = next(x for x in tg.filters if getattr(x, "id", 0) and x.title.text == "Auto")
        auto.include_peers.append(InputPeerUser(user_id=99, access_hash=1))
        auto.include_peers = [p for p in auto.include_peers if p.user_id != 1]
        tg.filters.append(folder(9, "Active", 1))
        tg.updates.clear()
        assert run(f.sync(tg.client, S, bot_on=False)) == 0
        assert tg.updates == []
        row = run(db.list_folder_dialogs(S))[0]
        assert row["folder_manual"] == 1 and row["chat_folder"] == ACTIVE
        # И дальше не трогает.
        run(f.sync(tg.client, S, bot_on=True))
        assert tg.members("Active") == {1}

    def test_chat_already_in_owner_folder_is_manual(self, db: Database) -> None:
        """Первый запуск: владелец сам положил её в папку — не трогаем."""
        girl(db, 1, intro={})
        tg = FakeTelegram(folder(5, "50/50", 1))
        run(folders(db).sync(tg.client, S, bot_on=True))
        assert tg.members("50/50") == {1}
        assert tg.members("Auto") == set()
        assert run(db.list_folder_dialogs(S))[0]["folder_manual"] == 1

    def test_deleted_chat_removed_from_folder(self, db: Database) -> None:
        girl(db, 1, intro={})
        girl(db, 2, intro={})
        tg = FakeTelegram()
        f = folders(db)
        run(f.sync(tg.client, S, bot_on=True))
        run(db.set_dialog_chat_deleted(S, 1, True))
        run(f.sync(tg.client, S, bot_on=True))
        assert tg.members("Auto") == {2}
        assert run(db.list_folder_dialogs(S))[1]["chat_folder"] in ("", AUTO)

    def test_failed_update_is_retried_not_manual(self, db: Database) -> None:
        girl(db, 1, intro={})
        tg = FakeTelegram()
        tg.fail_update = True
        f = folders(db)
        assert run(f.sync(tg.client, S, bot_on=True)) == 0
        tg.fail_update = False
        assert run(f.sync(tg.client, S, bot_on=True)) == 1
        assert tg.members("Auto") == {1}
        assert run(db.list_folder_dialogs(S))[0]["folder_manual"] == 0

    def test_full_folder_skips_new_chats(self, db: Database) -> None:
        girl(db, 1, intro={})
        tg = FakeTelegram(folder(5, "Auto", 90, 91))
        run(folders(db, max_chats_per_folder=2).sync(tg.client, S, bot_on=True))
        assert tg.members("Auto") == {90, 91}

    def test_friends_not_in_folders(self, db: Database) -> None:
        pid = run(db._connection.execute(
            "INSERT INTO profiles (fingerprint, name, age, raw_city, description, "
            "source_chat_id, source_message_id, first_seen_at, last_seen_at, "
            "status, audience) VALUES ('k', 'Кент', 20, 'спб', '', 1, 1, '', '', "
            "'new', 'friends')"
        )).lastrowid
        run(db._connection.commit())
        run(db.upsert_dialog_contact(S, 7, username="girl7", name="Кент", profile_id=pid))
        assert run(db.list_folder_dialogs(S)) == []

    def test_folders_switched_off(self, db: Database) -> None:
        girl(db, 1, intro={})
        tg = FakeTelegram()
        run(folders(db, enabled=False).sync(tg.client, S, bot_on=True))
        assert tg.updates == []


class TestArchive:
    def test_every_chat_archived_once(self, db: Database) -> None:
        girl(db, 1, intro={})
        girl(db, 2, her=10, owner=3, intro={"status": INTRO_SENT, "script_step": R.HANDOFF})
        tg = FakeTelegram()
        f = folders(db, archive=True, enabled=False)
        assert run(f.sync(tg.client, S, bot_on=True)) == 2
        assert {c.args[1] for c in tg.client.edit_folder.await_args_list} == {ARCHIVE_FOLDER}
        tg.client.edit_folder.reset_mock()
        # Владелец вынул чат сам — бот обратно не кладёт.
        assert run(f.sync(tg.client, S, bot_on=True)) == 0
        tg.client.edit_folder.assert_not_awaited()

    def test_deleted_chat_not_archived(self, db: Database) -> None:
        girl(db, 1, intro={"status": INTRO_SENT, "script_step": R.DELETED})
        tg = FakeTelegram()
        run(folders(db, archive=True, enabled=False).sync(tg.client, S, bot_on=True))
        tg.client.edit_folder.assert_not_awaited()

    def test_archive_error_not_retried(self, db: Database) -> None:
        girl(db, 1, intro={})
        tg = FakeTelegram()
        tg.client.edit_folder = AsyncMock(side_effect=ValueError("no peer"))
        f = folders(db, archive=True, enabled=False)
        run(f.sync(tg.client, S, bot_on=True))
        tg.client.edit_folder.reset_mock()
        run(f.sync(tg.client, S, bot_on=True))
        tg.client.edit_folder.assert_not_awaited()

    def test_archive_capped_per_pass(self, db: Database) -> None:
        for uid in range(1, MAX_MOVES_PER_PASS + 5):
            girl(db, uid, intro={})
        tg = FakeTelegram()
        f = folders(db, archive=True, enabled=False)
        assert run(f.sync(tg.client, S, bot_on=True)) == MAX_MOVES_PER_PASS

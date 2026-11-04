# Архив и папки Telegram для чатов девушек (владелец, 2026-11-04):
# Auto — бот ведёт сейчас, Active — разговор идёт несколько дней, 50/50 —
# молчит 48 ч+, New — остальное. Все чаты — в архиве; перенёс сам — не трогаем.

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.tl.functions.account import UpdateNotifySettingsRequest
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
from collectors.match_intro import INTRO_SENT, OWNER_CHAT, SCHEDULED
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
    db: Database, uid: int, *, her_days: int = 0, owner: int = 0,
    last_hours: float = 1.0, bot: int = 1, intro: dict | None = None,
) -> None:
    """Девушка-матч: контакт, сообщения (бот; она — по одному в день
    ``her_days`` дней подряд, последнее ``last_hours`` назад; владелец)."""
    run(db.upsert_dialog_contact(
        S, uid, username=f"girl{uid}", name=f"G{uid}", matched_at=ago(200),
    ))
    msg_id = 1
    for _ in range(bot):
        run(db.save_dialog_message(
            S, uid, msg_id, is_outgoing=True, sent_by="bot",
            message_date=ago(last_hours + 24 * her_days + 1),
        ))
        msg_id += 1
    for day in range(her_days):
        run(db.save_dialog_message(
            S, uid, msg_id, is_outgoing=False, sent_by="her",
            message_date=ago(last_hours + 24 * day),
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
        self.muted: dict[int, bool] = {}
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
        if isinstance(req, UpdateNotifySettingsRequest):
            self.muted[req.peer.peer.user_id] = req.settings.mute_until > 0
            return True
        raise AssertionError(f"неожиданный запрос {req!r}")

    def members(self, title: str) -> set[int]:
        for f in self.filters:
            t = getattr(getattr(f, "title", None), "text", None)
            if t == title:
                return {p.user_id for p in f.include_peers}
        return set()


def folders(
    db: Database, *, archive: bool = False, delete_silent: bool = False,
    **cfg: object,
) -> ChatFolders:
    return ChatFolders(
        db, ChatFoldersConfig(**cfg), archive=archive, delete_silent=delete_silent,
    )


class TestFolderTarget:
    """Правило подобрано с владельцем 2026-11-04 на живых переписках."""

    def _row(self, **kw: object) -> dict:
        base = {"total": 3, "her_days": 0, "last_her_at": None,
                "greeted_at": NOW.isoformat()}
        base.update(kw)
        return base

    @staticmethod
    def _her(hours: float) -> str:
        return (NOW - timedelta(hours=hours)).isoformat()

    @pytest.mark.parametrize("intro", [
        {"intro_status": SCHEDULED},
        {"intro_status": INTRO_SENT, "script_step": R.AFTER_PIN},
        {"intro_status": INTRO_SENT, "script_step": R.TELL_ME},
        {"intro_status": INTRO_SENT, "script_step": R.WAKE_UP},
        {"intro_status": "WAITING_REPLY"},
    ])
    def test_bot_leads_goes_to_auto(self, intro: dict) -> None:
        assert folder_target(self._row(**intro), bot_on=True, now=NOW) == AUTO

    def test_intro_before_script_is_not_auto(self) -> None:
        """Анкета ушла до сценария (шаг пуст) — бот её не ведёт (прод: Дарья)."""
        row = self._row(intro_status=INTRO_SENT, script_step="",
                        her_days=7, last_her_at=self._her(3))
        assert folder_target(row, bot_on=True, now=NOW) == ACTIVE

    def test_old_greeting_is_not_auto(self) -> None:
        """Привет недельной давности без ответа — бот уже ничего не делает."""
        row = self._row(intro_status="WAITING_REPLY",
                        greeted_at=(NOW - timedelta(days=6)).isoformat())
        assert folder_target(row, bot_on=True, now=NOW) == FIFTY

    def test_greeting_after_wake_up_is_auto(self) -> None:
        row = self._row(intro_status="WAITING_REPLY", wake_at=self._her(10),
                        greeted_at=(NOW - timedelta(days=4)).isoformat())
        assert folder_target(row, bot_on=True, now=NOW) == AUTO

    def test_bot_off_not_auto(self) -> None:
        row = self._row(intro_status=INTRO_SENT, script_step=R.TELL_ME,
                        her_days=1, last_her_at=self._her(2))
        assert folder_target(row, bot_on=False, now=NOW) == NEW

    def test_talk_for_days_is_active(self) -> None:
        row = self._row(her_days=3, last_her_at=self._her(23))
        assert folder_target(row, bot_on=True, now=NOW) == ACTIVE

    def test_fresh_talk_is_new(self) -> None:
        row = self._row(intro_status=INTRO_SENT, script_step=R.HANDOFF,
                        her_days=2, last_her_at=self._her(1))
        assert folder_target(row, bot_on=True, now=NOW) == NEW

    def test_our_last_word_unanswered_a_day_is_fifty(self) -> None:
        """Прод 2026-11-04, Рина Грейс: «Йооу» владельца сутки без ответа."""
        row = self._row(her_days=1, last_her_at=self._her(40),
                        last_by="owner", last_msg_at=self._her(24))
        assert folder_target(row, bot_on=True, now=NOW) == FIFTY

    def test_our_fresh_last_word_stays(self) -> None:
        row = self._row(her_days=1, last_her_at=self._her(20),
                        last_by="owner", last_msg_at=self._her(19))
        assert folder_target(row, bot_on=True, now=NOW) == NEW

    @pytest.mark.parametrize("kw", [
        {"her_days": 5, "last_her_at": (NOW - timedelta(hours=48)).isoformat()},
        {"her_days": 0, "last_her_at": None},
    ])
    def test_silence_is_fifty(self, kw: dict) -> None:
        assert folder_target(self._row(**kw), bot_on=True, now=NOW) == FIFTY

    @pytest.mark.parametrize("kw", [
        {"chat_deleted_at": "2026-11-04T10:00:00+00:00"},
        {"intro_status": INTRO_SENT, "script_step": R.DELETED},
        {"total": 0},
    ])
    def test_deleted_or_empty_chat_in_no_folder(self, kw: dict) -> None:
        assert folder_target(self._row(**kw), bot_on=True, now=NOW) is None


class TestFolderRows:
    def test_her_days_counted_in_local_time(self, db: Database) -> None:
        """21:30 UTC = 00:30 по Питеру — уже следующий день."""
        run(db.upsert_dialog_contact(S, 1, username="girl1", name="G1"))
        for i, at in enumerate((
            "2026-11-03T19:30:00+00:00", "2026-11-03T20:30:00+00:00",
            "2026-11-03T21:30:00+00:00",
        )):
            run(db.save_dialog_message(
                S, 1, i + 1, is_outgoing=False, sent_by="her", message_date=at,
            ))
        row = run(db.list_folder_dialogs(S, 3.0))[0]
        assert row["her_days"] == 2
        assert row["total"] == 3
        assert row["last_her_at"] == "2026-11-03T21:30:00+00:00"


class TestChatFolders:
    def test_creates_missing_folders_and_sorts(self, db: Database) -> None:
        girl(db, 1, intro={})                                        # ждёт ответа
        girl(db, 2, her_days=1, intro={"status": INTRO_SENT, "script_step": R.HANDOFF})
        girl(db, 3, her_days=3, owner=3,
             intro={"status": INTRO_SENT, "script_step": R.HANDOFF})
        girl(db, 4, her_days=2, owner=2, last_hours=100)             # затихла
        girl(db, 5, bot=0)                                           # пустой чат
        tg = FakeTelegram()
        assert run(folders(db).sync(tg.client, S, bot_on=True)) == 4
        assert tg.members("Auto") == {1}
        assert tg.members("New") == {2}
        assert tg.members("Active") == {3}
        assert tg.members("50/50") == {4}

    def test_existing_folder_found_by_title(self, db: Database) -> None:
        girl(db, 2, her_days=1, intro={"status": OWNER_CHAT})
        tg = FakeTelegram(folder(5, "new", 999))
        run(folders(db).sync(tg.client, S, bot_on=True))
        assert tg.members("new") == {999, 2}
        assert [u.id for u in tg.updates] == [5]

    def test_moves_between_folders_one_request_per_folder(self, db: Database) -> None:
        girl(db, 1, her_days=1, intro={"status": INTRO_SENT, "script_step": R.TELL_ME})
        girl(db, 2, her_days=1, intro={"status": INTRO_SENT, "script_step": R.TELL_ME})
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

    def test_deleted_chat_pruned_by_telegram_is_not_manual(self, db: Database) -> None:
        """Telegram сам выкинул удалённый чат из папки — не «перенёс владелец»."""
        girl(db, 1, intro={})
        girl(db, 2, intro={})
        tg = FakeTelegram()
        f = folders(db)
        run(f.sync(tg.client, S, bot_on=True))
        run(db.set_dialog_chat_deleted(S, 1, True))
        auto = next(x for x in tg.filters if getattr(x, "id", 0) and x.title.text == "Auto")
        auto.include_peers = [p for p in auto.include_peers if p.user_id != 1]
        run(f.sync(tg.client, S, bot_on=True))
        row = next(r for r in run(db.list_folder_dialogs(S)) if r["user_id"] == 1)
        assert row["folder_manual"] == 0 and row["chat_folder"] == ""

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


class TestDeleteSilent:
    """Молчит и после «АЛООО» — чат удаляется у владельца, и старые тоже
    (прод 2026-11-04: Guangxi лежала в 50/50)."""

    def test_old_no_reply_chat_deleted_and_out_of_folders(self, db: Database) -> None:
        girl(db, 1, bot=2, intro={"status": "NO_REPLY"})
        tg = FakeTelegram()
        tg.client.delete_dialog = AsyncMock(return_value=None)
        run(folders(db, delete_silent=True).sync(tg.client, S, bot_on=True))
        tg.client.delete_dialog.assert_awaited_once()
        assert tg.client.delete_dialog.await_args.kwargs == {"revoke": False}
        assert run(db.list_folder_dialogs(S))[0]["chat_deleted_at"]
        assert tg.members("50/50") == set()

    def test_she_wrote_not_deleted(self, db: Database) -> None:
        girl(db, 1, her_days=1, last_hours=100, intro={"status": "NO_REPLY"})
        tg = FakeTelegram()
        tg.client.delete_dialog = AsyncMock(return_value=None)
        run(folders(db, delete_silent=True).sync(tg.client, S, bot_on=True))
        tg.client.delete_dialog.assert_not_called()

    def test_switched_off_not_deleted(self, db: Database) -> None:
        girl(db, 1, bot=2, intro={"status": "NO_REPLY"})
        tg = FakeTelegram()
        tg.client.delete_dialog = AsyncMock(return_value=None)
        run(folders(db).sync(tg.client, S, bot_on=True))
        tg.client.delete_dialog.assert_not_called()


class TestSound:
    """Звук — только Active и New; Auto и 50/50 без звука (владелец, 2026-11-04)."""

    def test_sound_follows_folder(self, db: Database) -> None:
        girl(db, 1, intro={})                                        # Auto
        girl(db, 2, her_days=1, intro={"status": INTRO_SENT, "script_step": R.HANDOFF})
        girl(db, 3, her_days=3, owner=3)                             # Active
        girl(db, 4, her_days=2, last_hours=100)                      # 50/50
        tg = FakeTelegram()
        f = folders(db)
        run(f.sync(tg.client, S, bot_on=True))
        assert tg.muted == {1: True, 2: False, 3: False, 4: True}
        tg.muted.clear()
        run(f.sync(tg.client, S, bot_on=True))
        assert tg.muted == {}  # уже выставлено — повторно не шлём

    def test_left_automation_gets_sound(self, db: Database) -> None:
        girl(db, 1, her_days=1, intro={"status": INTRO_SENT, "script_step": R.TELL_ME})
        tg = FakeTelegram()
        f = folders(db)
        run(f.sync(tg.client, S, bot_on=True))
        assert tg.muted == {1: True}
        run(db.update_match_dialog(S, 1, script_step=R.HANDOFF))
        run(f.sync(tg.client, S, bot_on=True))
        assert tg.members("New") == {1}
        assert tg.muted == {1: False}

    def test_manual_chat_sound_untouched(self, db: Database) -> None:
        girl(db, 1, intro={})
        tg = FakeTelegram(folder(5, "Active", 1))
        run(folders(db).sync(tg.client, S, bot_on=True))
        assert tg.muted == {}

    def test_hide_now_archives_and_mutes(self, db: Database) -> None:
        """Сразу после стикера-привета, не дожидаясь её ответа."""
        run(db.upsert_dialog_contact(S, 7, username="girl7", name="G7"))
        tg = FakeTelegram()
        run(folders(db, archive=True).hide_now(tg.client, S, username="girl7"))
        tg.client.edit_folder.assert_awaited_once()
        assert tg.client.edit_folder.await_args.args[1] == ARCHIVE_FOLDER
        assert tg.muted == {7: True}
        row = run(db.list_folder_dialogs(S))[0]
        assert row["archived_at"] and row["notify_mark"] == "muted"

    def test_hide_now_error_does_not_raise(self, db: Database) -> None:
        tg = FakeTelegram()
        tg.client.get_input_entity = AsyncMock(side_effect=ValueError("no peer"))
        run(folders(db, archive=True).hide_now(tg.client, S, user_id=5))
        tg.client.edit_folder.assert_not_awaited()


class TestArchive:
    def test_every_chat_archived_once(self, db: Database) -> None:
        girl(db, 1, intro={})
        girl(db, 2, her_days=3, owner=3,
             intro={"status": INTRO_SENT, "script_step": R.HANDOFF})
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

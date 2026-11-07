# Архив и папки Telegram для чатов девушек (владелец, 2026-11-04):
# Auto — бот ведёт сейчас, Active — разговор идёт несколько дней, 50/50 —
# молчит 48 ч+, New — остальное. Все чаты — в архиве; перенёс сам — не трогаем.

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.tl.functions.account import (
    GetNotifySettingsRequest,
    UpdateNotifySettingsRequest,
)
from telethon.tl.functions.messages import (
    GetDialogFiltersRequest,
    UpdateDialogFilterRequest,
)
from telethon.tl.functions.users import GetUsersRequest
from telethon.tl.types import (
    DialogFilter,
    DialogFilterDefault,
    InputPeerUser,
    TextWithEntities,
    UserStatusEmpty,
    UserStatusRecently,
)

from app.config import ChatFoldersConfig
from collectors.chat_folders import ARCHIVE_FOLDER, MAX_MOVES_PER_PASS, ChatFolders
from collectors.dialog_logger import DialogLogger
from collectors.match_intro import INTRO_SENT, OWNER_CHAT, SCHEDULED
from database.database import Database
from services import match_script_rules as R
from services.chat_folders import (
    ACTIVE,
    AUTO,
    BLOCKED,
    FIFTY,
    NEW,
    folder_target,
    owner_took_over,
)

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
        self.notifies: list[UpdateNotifySettingsRequest] = []
        #: user_id -> (status, photo) для users.GetUsers.
        self.users: dict[int, tuple[object, object]] = {}
        #: user_id -> mute_until, уже стоящий в Telegram (владелец заглушил).
        self.muted: dict[int, int] = {}
        #: user_id -> готовый объект пользователя (UserEmpty / deleted).
        self.raw_users: dict[int, object] = {}
        self.client = AsyncMock(side_effect=self._call)
        self.client.get_input_entity = AsyncMock(side_effect=self._entity)
        self.client.edit_folder = AsyncMock(return_value=None)

    @staticmethod
    def _entity(who: object) -> InputPeerUser:
        uid = int(str(who).removeprefix("girl"))
        return InputPeerUser(user_id=uid, access_hash=1)

    def _call(self, req: object) -> object:
        if isinstance(req, GetDialogFiltersRequest):
            # Копия: бот правит объекты фильтров у себя, сервер меняется
            # только успешным UpdateDialogFilterRequest.
            return SimpleNamespace(filters=deepcopy(self.filters))
        if isinstance(req, UpdateDialogFilterRequest):
            if self.fail_update:
                raise RuntimeError("FILTER_INCLUDE_TOO_MUCH")
            self.updates.append(req)
            self.filters = [f for f in self.filters if getattr(f, "id", None) != req.id]
            if req.filter is not None:
                self.filters.append(req.filter)
            return True
        if isinstance(req, GetNotifySettingsRequest):
            uid = req.peer.peer.user_id
            return SimpleNamespace(mute_until=self.muted.get(uid, 0))
        if isinstance(req, UpdateNotifySettingsRequest):
            self.notifies.append(req)
            return True
        if isinstance(req, GetUsersRequest):
            out = []
            for p in req.id:
                if p.user_id in self.raw_users:
                    out.append(self.raw_users[p.user_id])
                    continue
                status, photo = self.users.get(p.user_id, (UserStatusRecently(), object()))
                out.append(SimpleNamespace(id=p.user_id, status=status, photo=photo))
            return out
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

    @pytest.mark.parametrize("step", [R.HANDOFF, R.OWNER])
    def test_handoff_waiting_for_owner_is_new_at_once(self, step: str) -> None:
        """Владелец, 2026-11-07: передала управление — из Auto в New сразу,
        хоть 3+ дня общения (Active) или давняя тишина (50/50)."""
        for kw in (
            {"her_days": 5, "last_her_at": self._her(30)},
            {"her_days": 1, "last_her_at": self._her(100)},
        ):
            row = self._row(intro_status=INTRO_SENT, script_step=step,
                            last_by="her", last_msg_at=kw["last_her_at"], **kw)
            assert folder_target(row, bot_on=True, now=NOW) == NEW

    def test_handoff_after_owner_replied_uses_normal_rules(self) -> None:
        row = self._row(intro_status=INTRO_SENT, script_step=R.HANDOFF,
                        her_days=5, last_her_at=self._her(30),
                        last_by="owner", last_msg_at=self._her(5))
        assert folder_target(row, bot_on=True, now=NOW) == ACTIVE

    def test_blocked_beats_everything_but_deleted(self) -> None:
        row = self._row(intro_status=INTRO_SENT, script_step=R.TELL_ME,
                        blocked_at=NOW.isoformat())
        assert folder_target(row, bot_on=True, now=NOW) == BLOCKED
        row["chat_deleted_at"] = NOW.isoformat()
        assert folder_target(row, bot_on=True, now=NOW) is None

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

    def test_owner_wrote_after_bot_is_not_auto(self) -> None:
        """Владелец написал позже бота — бот её не ведёт (прод: Вера)."""
        row = self._row(
            intro_status=INTRO_SENT, script_step=R.TELL_ME, has_match_dialog=1,
            her_days=1, last_her_at=self._her(2),
            last_by="owner", last_msg_at=self._her(1),
            last_owner_at=self._her(1), last_bot_at=self._her(2),
        )
        assert folder_target(row, bot_on=True, now=NOW) == NEW

    def test_no_match_row_is_not_auto(self) -> None:
        """Строки match_dialogs нет — бот тут не работал, Auto не выбираем."""
        row = self._row(has_match_dialog=0, greeted_at=NOW.isoformat())
        assert folder_target(row, bot_on=True, now=NOW) != AUTO

    def test_owner_later_with_empty_status_is_not_auto(self) -> None:
        row = self._row(
            intro_status="", has_match_dialog=1, her_days=1,
            last_her_at=self._her(2), last_by="owner",
            last_msg_at=self._her(1), last_owner_at=self._her(1),
            last_bot_at=self._her(2),
        )
        assert folder_target(row, bot_on=True, now=NOW) != AUTO

    @pytest.mark.parametrize("owner_hours,bot_hours,took", [
        (None, None, True),      # ни бота, ни владельца — не Auto
        (None, 2, False),        # бот писал, владелец нет — не takeover
        (3, 2, False),           # владелец раньше бота — не takeover
        (1, 2, True),            # владелец позже бота — takeover
    ])
    def test_no_row_uses_message_times(
        self, owner_hours: float | None, bot_hours: float | None, took: bool,
    ) -> None:
        row = self._row(
            has_match_dialog=0, intro_status="",
            last_owner_at=self._her(owner_hours) if owner_hours else None,
            last_bot_at=self._her(bot_hours) if bot_hours else None,
        )
        assert owner_took_over(row) is took
        if bot_hours is None:
            assert folder_target(row, bot_on=True, now=NOW) != AUTO


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

    def test_chat_placed_by_owner_before_bot_is_not_manual(self, db: Database) -> None:
        """Бот ещё никуда не клал её — это не «владелец перенёс»: правила
        применяются, папка ставится по факту (владелец, 2026-11-07)."""
        girl(db, 1, intro={})
        tg = FakeTelegram(folder(5, "50/50", 1))
        run(folders(db).sync(tg.client, S, bot_on=True))
        assert tg.members("Auto") == {1}
        assert tg.members("50/50") == set()
        assert run(db.list_folder_dialogs(S))[0]["folder_manual"] == 0

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

    def test_owner_takeover_beats_auto(self, db: Database) -> None:
        """Владелец пишет позже бота — в Auto не кладём, чат ждёт его."""
        girl(db, 1, her_days=1, owner=1,
             intro={"status": INTRO_SENT, "script_step": R.TELL_ME})
        tg = FakeTelegram()
        run(folders(db).sync(tg.client, S, bot_on=True))
        assert tg.members("Auto") == set()
        assert tg.members("New") == {1}

    def test_old_bad_manual_flag_is_cleared_when_bot_never_placed(
        self, db: Database,
    ) -> None:
        """Старая отметка «владелец перенёс» без folder_placed — ошибка:
        бот её не клал, значит правила снова работают (прод: Вера)."""
        girl(db, 1, her_days=2, owner=1)
        run(db.update_dialog_contact(
            S, 1, folder_manual=1, chat_folder=NEW,
        ))
        tg = FakeTelegram(folder(5, "New", 1))
        run(folders(db).sync(tg.client, S, bot_on=True))
        row = run(db.list_folder_dialogs(S))[0]
        assert row["folder_manual"] == 0
        assert row["folder_placed"] == NEW
        assert row["chat_folder"] == NEW

    def test_failed_move_keeps_actual_folder_not_intended(
        self, db: Database,
    ) -> None:
        """Перенос не приняли — chat_folder = факт из Telegram, не цель."""
        girl(db, 1, intro={})
        run(db.update_dialog_contact(S, 1, chat_folder=AUTO))
        tg = FakeTelegram(folder(6, "New", 1))
        tg.fail_update = True
        assert run(folders(db).sync(tg.client, S, bot_on=True)) == 0
        row = run(db.list_folder_dialogs(S))[0]
        assert row["chat_folder"] == NEW
        assert row["folder_placed"] == ""

    def test_pin_service_after_intro_stays_auto(self, db: Database) -> None:
        """Закреп анкеты (служебное исходящее) — бот, Auto не теряется."""
        girl(db, 1, intro={"status": INTRO_SENT, "script_step": R.TELL_ME})
        run(DialogLogger(db).log_event(
            S,
            SimpleNamespace(
                id=99, out=True, action=object(), message="",
                date=datetime.now(timezone.utc),
            ),
            outgoing=True, user_id=1,
        ))
        row = run(db.list_folder_dialogs(S))[0]
        assert row["last_bot_at"] is not None
        tg = FakeTelegram()
        run(folders(db).sync(tg.client, S, bot_on=True))
        assert tg.members("Auto") == {1}

    def test_redirected_bot_chat_on_target_account_is_auto(
        self, db: Database,
    ) -> None:
        """При перенаправлении и матч, и контакт — на аккаунте-цели: Auto."""
        target = "dvai"
        run(db.upsert_dialog_contact(
            target, 1, username="girl1", name="G1", matched_at=ago(200),
        ))
        run(db.add_match_dialog(target, 1, username="girl1", name="G1"))
        run(db.update_match_dialog(
            target, 1, status=INTRO_SENT, script_step=R.TELL_ME,
        ))
        run(db.save_dialog_message(
            target, 1, 1, is_outgoing=True, sent_by="bot",
            message_date=ago(1),
        ))
        tg = FakeTelegram()
        run(folders(db).sync(tg.client, target, bot_on=True))
        assert tg.members("Auto") == {1}

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

    def test_blocked_chat_deleted_after_confirmed_block(self, db: Database) -> None:
        """Она кинула в ЧС (2 проверки подряд) — чат удаляется у владельца
        (владелец, 2026-11-08, @Kimli123); после первой проверки — ещё нет."""
        girl(db, 1, intro={})
        tg = FakeTelegram()
        tg.users[1] = (UserStatusEmpty(), None)
        tg.client.delete_dialog = AsyncMock(return_value=None)
        f = folders(db, delete_silent=True)
        run(f.sync(tg.client, S, bot_on=True))
        tg.client.delete_dialog.assert_not_called()
        run(db._connection.execute(
            "UPDATE dialog_contacts SET block_checked_at = ?", (ago(7),)
        ))
        run(f.sync(tg.client, S, bot_on=True))
        tg.client.delete_dialog.assert_awaited_once()
        assert tg.client.delete_dialog.await_args.kwargs == {"revoke": False}
        assert run(db.list_folder_dialogs(S))[0]["chat_deleted_at"]

    def test_switched_off_not_deleted(self, db: Database) -> None:
        girl(db, 1, bot=2, intro={"status": "NO_REPLY"})
        tg = FakeTelegram()
        tg.client.delete_dialog = AsyncMock(return_value=None)
        run(folders(db).sync(tg.client, S, bot_on=True))
        tg.client.delete_dialog.assert_not_called()


class TestHideNow:
    """Сразу после стикера-привета её чат — в архив, не дожидаясь ответа.
    Звук hide_now не трогает."""

    @staticmethod
    def _notify_requests(tg: FakeTelegram) -> list:
        return [
            c.args[0] for c in tg.client.await_args_list
            if isinstance(c.args[0], UpdateNotifySettingsRequest)
        ]

    def test_hide_now_archives(self, db: Database) -> None:
        run(db.upsert_dialog_contact(S, 7, username="girl7", name="G7"))
        tg = FakeTelegram()
        run(folders(db, archive=True).hide_now(tg.client, S, username="girl7"))
        tg.client.edit_folder.assert_awaited_once()
        assert tg.client.edit_folder.await_args.args[1] == ARCHIVE_FOLDER
        assert run(db.list_folder_dialogs(S))[0]["archived_at"]
        assert self._notify_requests(tg) == []

    def test_hide_now_without_archive_does_nothing(self, db: Database) -> None:
        tg = FakeTelegram()
        run(folders(db).hide_now(tg.client, S, username="girl7"))
        tg.client.get_input_entity.assert_not_called()

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


class TestMuteAuto:
    """Чат, который бот кладёт в Auto, глушится (папка глушит только текущих
    участников); при выходе из Auto звук возвращается (владелец, 2026-11-07).
    Чаты вне Auto и не заглушённые ботом не трогаем."""

    @staticmethod
    def _mark(db: Database, uid: int) -> str:
        rows = run(db.list_folder_dialogs(S))
        return next(r for r in rows if r["user_id"] == uid)["notify_mark"]

    def test_chat_put_in_auto_is_muted(self, db: Database) -> None:
        girl(db, 1, intro={})
        tg = FakeTelegram()
        f = folders(db)
        run(f.sync(tg.client, S, bot_on=True))
        assert tg.members("Auto") == {1}
        assert len(tg.notifies) == 1
        req = tg.notifies[0]
        assert req.peer.peer.user_id == 1 and req.settings.mute_until == 2**31 - 1
        assert self._mark(db, 1) == "bot_muted"
        run(f.sync(tg.client, S, bot_on=True))
        assert len(tg.notifies) == 1

    def test_leaving_auto_unmutes(self, db: Database) -> None:
        girl(db, 1, her_days=1, intro={"status": INTRO_SENT, "script_step": R.TELL_ME})
        tg = FakeTelegram()
        f = folders(db)
        run(f.sync(tg.client, S, bot_on=True))
        # Передача владельцу, последнее слово её: New сразу.
        run(db.update_match_dialog(S, 1, script_step=R.HANDOFF))
        run(f.sync(tg.client, S, bot_on=True))
        assert tg.members("New") == {1}
        assert [n.settings.mute_until for n in tg.notifies] == [2**31 - 1, 0]
        assert self._mark(db, 1) == "on"

    def test_chat_outside_auto_never_touched(self, db: Database) -> None:
        girl(db, 2, her_days=3, owner=3)                             # Active
        girl(db, 3, her_days=2, last_hours=100)                      # 50/50
        tg = FakeTelegram()
        run(folders(db, archive=True).sync(tg.client, S, bot_on=True))
        assert tg.members("Active") == {2} and tg.members("50/50") == {3}
        assert tg.notifies == []

    def test_not_bot_muted_chat_is_not_unmuted(self, db: Database) -> None:
        """Заглушил не бот (mute_auto выключен) — при выходе из Auto не трогаем."""
        girl(db, 1, her_days=1, intro={"status": INTRO_SENT, "script_step": R.TELL_ME})
        tg = FakeTelegram()
        f = folders(db, mute_auto=False)
        run(f.sync(tg.client, S, bot_on=True))
        run(f.sync(tg.client, S, bot_on=False))
        assert tg.notifies == []


class TestMuteExisting:
    """Чаты, уже лежащие в Auto (до деплоя), глушатся; заглушённые владельцем
    не трогаем и не возвращаем."""

    def test_chat_already_in_auto_gets_muted(self, db: Database) -> None:
        girl(db, 1, intro={})
        tg = FakeTelegram(folder(2, "Auto", 1))
        run(db.update_dialog_contact(S, 1, chat_folder=AUTO))
        run(folders(db).sync(tg.client, S, bot_on=True))
        assert len(tg.notifies) == 1
        assert tg.notifies[0].settings.mute_until == 2**31 - 1

    def test_owner_muted_chat_is_marked_and_never_unmuted(self, db: Database) -> None:
        girl(db, 1, her_days=1, intro={"status": INTRO_SENT, "script_step": R.TELL_ME})
        tg = FakeTelegram()
        tg.muted[1] = 2**31 - 1
        f = folders(db)
        run(f.sync(tg.client, S, bot_on=True))
        rows = run(db.list_folder_dialogs(S))
        assert rows[0]["notify_mark"] == "owner_muted"
        run(db.update_match_dialog(S, 1, script_step=R.HANDOFF))
        run(f.sync(tg.client, S, bot_on=True))
        assert tg.members("New") == {1}
        assert tg.notifies == []


class TestBlocked:
    """Эвристика «заблокировала»: пустой статус и нет фото (2026-11-07)."""

    @staticmethod
    def _recheck(db: Database) -> None:
        run(db._connection.execute(
            "UPDATE dialog_contacts SET block_checked_at = ?", (ago(7),)
        ))

    def test_blocked_needs_two_consecutive_checks(self, db: Database) -> None:
        girl(db, 1, intro={})
        tg = FakeTelegram()
        tg.users[1] = (UserStatusEmpty(), None)
        f = folders(db)
        run(f.sync(tg.client, S, bot_on=True))
        assert run(db.is_dialog_blocked(S, 1)) is False
        # Отрицательная проверка сбрасывает счётчик.
        tg.users[1] = (UserStatusRecently(), object())
        self._recheck(db)
        run(f.sync(tg.client, S, bot_on=True))
        tg.users[1] = (UserStatusEmpty(), None)
        self._recheck(db)
        run(f.sync(tg.client, S, bot_on=True))
        assert run(db.is_dialog_blocked(S, 1)) is False
        self._recheck(db)
        run(f.sync(tg.client, S, bot_on=True))
        assert run(db.is_dialog_blocked(S, 1)) is True

    def test_user_empty_and_deleted_are_not_blocked(self, db: Database) -> None:
        from telethon.tl.types import UserEmpty

        girl(db, 1, intro={})
        girl(db, 2, intro={})
        tg = FakeTelegram()
        tg.raw_users[1] = UserEmpty(id=1)
        tg.raw_users[2] = SimpleNamespace(
            id=2, deleted=True, status=UserStatusEmpty(), photo=None)
        f = folders(db)
        for _ in range(3):
            run(f.sync(tg.client, S, bot_on=True))
            self._recheck(db)
        assert run(db.is_dialog_blocked(S, 1)) is False
        assert run(db.is_dialog_blocked(S, 2)) is False

    def test_blocked_goes_to_chs_and_clears(self, db: Database) -> None:
        girl(db, 1, intro={})
        girl(db, 2, intro={})
        tg = FakeTelegram()
        tg.users[1] = (UserStatusEmpty(), None)
        f = folders(db)
        run(f.sync(tg.client, S, bot_on=True))
        assert run(db.is_dialog_blocked(S, 1)) is False  # первая проверка
        self._recheck(db)
        run(f.sync(tg.client, S, bot_on=True))
        run(f.sync(tg.client, S, bot_on=True))
        assert tg.members("ЧС") == {1}
        assert tg.members("Auto") == {2}
        assert run(db.is_dialog_blocked(S, 1)) is True
        assert run(db.is_dialog_blocked(S, 2)) is False
        # Не чаще раза в 6 ч: статус вернулся, но проверки ещё нет.
        tg.users[1] = (UserStatusRecently(), object())
        run(f.sync(tg.client, S, bot_on=True))
        assert run(db.is_dialog_blocked(S, 1)) is True
        run(db._connection.execute(
            "UPDATE dialog_contacts SET block_checked_at = ?", (ago(7),)
        ))
        run(f.sync(tg.client, S, bot_on=True))
        assert run(db.is_dialog_blocked(S, 1)) is False
        assert tg.members("ЧС") == set() and tg.members("Auto") == {1, 2}

    def test_empty_status_with_photo_is_not_blocked(self, db: Database) -> None:
        girl(db, 1, intro={})
        tg = FakeTelegram()
        tg.users[1] = (UserStatusEmpty(), object())
        run(folders(db).sync(tg.client, S, bot_on=True))
        assert run(db.is_dialog_blocked(S, 1)) is False

    def test_check_batch_limit_and_switch(self, db: Database) -> None:
        for uid in (1, 2, 3):
            girl(db, uid, intro={})
        tg = FakeTelegram()
        run(folders(db, block_check_batch=2).sync(tg.client, S, bot_on=True))
        checked = [r for r in run(db.list_folder_dialogs(S)) if r["block_checked_at"]]
        assert len(checked) == 2
        tg2 = FakeTelegram()
        run(folders(db, check_blocked=False).sync(tg2.client, S, bot_on=True))
        assert not any(
            isinstance(c.args[0], GetUsersRequest) for c in tg2.client.await_args_list
        )


class TestDeadChats:
    """Не находящийся собеседник: WARNING один раз за запуск, дальше молча."""

    def test_lost_chat_warned_once_per_run(self, db: Database) -> None:
        from loguru import logger

        girl(db, 1, intro={})
        tg = FakeTelegram()
        tg.client.get_input_entity = AsyncMock(
            side_effect=ValueError("No user has girl1 as username"))
        msgs: list[str] = []
        sink = logger.add(lambda m: msgs.append(str(m)), level="WARNING")
        try:
            f = folders(db, archive=True)
            for _ in range(3):
                run(f.sync(tg.client, S, bot_on=True))
        finally:
            logger.remove(sink)
        assert len([m for m in msgs if "G1" in m or "girl1" in m]) == 1
        assert tg.client.get_input_entity.await_count == 1

"""Итог переписки (services/dialog_outcome.py): удачные / идут / неудачные."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from services import dialog_outcome as o

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def make_row(**kw: Any) -> dict[str, Any]:
    """Сводка переписки (как строка get_dialog_list); last — часов назад."""
    hours_ago = kw.pop("last", 1)
    row: dict[str, Any] = {
        "intro_status": "INTRO_SENT",
        "script_step": "",
        "her_count": 0,
        "last_by": "bot",
        "matched_at": (NOW - timedelta(days=5)).isoformat(),
        "greeted_at": (NOW - timedelta(days=5)).isoformat(),
        "last_message_at": (NOW - timedelta(hours=hours_ago)).isoformat(),
        "first_her_at": None,
        "wake_at": "",
    }
    row.update(kw)
    return row


class TestClassify:
    def test_deleted_chat_is_rejected(self) -> None:
        out = o.classify(make_row(script_step="deleted", her_count=2), NOW)
        assert (out.category, out.reason) == (o.FAILED, o.REJECTED)

    def test_no_reply_after_wake_up_is_silent(self) -> None:
        out = o.classify(make_row(intro_status="NO_REPLY", last=30), NOW)
        assert (out.category, out.reason) == (o.FAILED, o.SILENT)

    def test_fresh_match_without_reply_is_active(self) -> None:
        out = o.classify(make_row(intro_status="WAITING_REPLY", last=2), NOW)
        assert out.category == o.ACTIVE and not out.needs_you

    def test_bot_still_waiting_is_not_silent_yet(self) -> None:
        """Бот ещё ведёт (ждёт после «АЛООО») — не «молчит», даже после 72 ч."""
        out = o.classify(make_row(script_step="wake_up", last=80), NOW)
        assert out.category == o.ACTIVE

    def test_no_access_needs_owner(self) -> None:
        out = o.classify(make_row(intro_status="NO_ACCESS", last=2), NOW)
        assert out.category == o.ACTIVE and out.needs_you

    def test_old_never_replied_is_silent(self) -> None:
        out = o.classify(make_row(intro_status="NO_ACCESS", last=100), NOW)
        assert (out.category, out.reason) == (o.FAILED, o.SILENT)

    def test_replied_then_quiet_is_faded(self) -> None:
        """Она отвечала, наше сообщение без ответа 3+ суток — тупик."""
        out = o.classify(make_row(her_count=3, last_by="owner", last=80), NOW)
        assert (out.category, out.reason) == (o.FAILED, o.FADED)

    def test_her_message_unanswered_long_is_abandoned(self) -> None:
        out = o.classify(make_row(her_count=2, last_by="her", script_step="handoff", last=80), NOW)
        assert (out.category, out.reason) == (o.FAILED, o.ABANDONED)
        assert out.needs_you

    def test_handoff_is_good_and_waits_for_owner(self) -> None:
        out = o.classify(make_row(her_count=2, last_by="her", script_step="handoff", last=3), NOW)
        assert out.category == o.GOOD and out.needs_you

    def test_many_her_messages_is_good(self) -> None:
        out = o.classify(make_row(her_count=o.GOOD_HER_MESSAGES, last_by="owner", last=3), NOW)
        assert out.category == o.GOOD and not out.needs_you

    def test_bot_answers_her_so_not_needs_you(self) -> None:
        out = o.classify(make_row(her_count=1, last_by="her", script_step="tell_me", last=1), NOW)
        assert out.category == o.ACTIVE and not out.needs_you

    def test_good_is_easy_without_wake_up_and_fast_reply(self) -> None:
        row = make_row(her_count=6, last_by="owner", last=2,
                       first_her_at=(NOW - timedelta(days=5) + timedelta(hours=1)).isoformat())
        assert o.classify(row, NOW).ease == "easy"

    def test_good_is_hard_after_wake_up(self) -> None:
        row = make_row(her_count=6, last_by="owner", last=2, wake_at=NOW.isoformat())
        assert o.classify(row, NOW).ease == "hard"

    def test_good_is_hard_after_slow_first_reply(self) -> None:
        row = make_row(her_count=6, last_by="owner", last=2,
                       first_her_at=(NOW - timedelta(days=3)).isoformat())
        assert o.classify(row, NOW).ease == "hard"

    def test_determinism_and_bad_dates(self) -> None:
        row = make_row(her_count=1, last_message_at="мусор", matched_at=None)
        assert o.classify(row, NOW) == o.classify(row, NOW)


class TestSort:
    def test_sorts(self) -> None:
        rows = [
            {"user_id": 1, "her_count": 1, "last_message_at": "2026-11-01T10:00:00+00:00",
             "matched_at": "2026-10-01T10:00:00+00:00"},
            {"user_id": 2, "her_count": 9, "last_message_at": "2026-10-20T10:00:00+00:00",
             "matched_at": "2026-10-20T10:00:00+00:00"},
            {"user_id": 3, "her_count": 0, "last_message_at": None,
             "matched_at": "2026-11-02T10:00:00+00:00"},
        ]
        ids = lambda s: [r["user_id"] for r in o.sort_rows(rows, s)]  # noqa: E731
        assert ids("recent") == [3, 1, 2]
        assert ids("her") == [2, 1, 3]
        assert ids("quiet") == [2, 1, 3]
        assert ids("matched") == [3, 2, 1]
        assert ids("bogus") == ids("recent")

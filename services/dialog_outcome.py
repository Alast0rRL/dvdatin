"""Итог переписки с девушкой-матчем для сайта (/dialogs), Telegram-free.

Чистые правила по сводке переписки (одна строка ``get_dialog_list``):
сколько она написала, кто писал последним, сколько тишины, на каком шаге
автопереписка. Одна сводка + один момент «сейчас» → один итог.

Итоги (``category``):

* ``good``   — удачные: знакомство состоялось (сценарий пройден или она
  написала ``GOOD_HER_MESSAGES``+ сообщений). ``ease``: ``easy`` — легко
  (без «АЛООО ПРОСЫПАЕМСЯ», первый ответ быстрый), ``hard`` — со скрипом.
* ``active`` — идёт: ждём её ответа или разговор живой.
* ``failed`` — неудачные, с причиной (``reason``): молчит, затухла (тупик —
  перестала отвечать), ты не ответил, отшила (бот удалил чат: грубость /
  «не пиши»), чат удалён в Telegram (сверка нашла его пустым).

``needs_you`` — отдельный флаг поверх итога: бот дальше не ведёт, а её
сообщение без ответа (или бот не смог написать) — нужен владелец.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

GOOD: str = "good"
ACTIVE: str = "active"
FAILED: str = "failed"

CATEGORIES: tuple[str, ...] = (GOOD, ACTIVE, FAILED)

#: Причины неудачи.
SILENT: str = "silent"        # так и не ответила
FADED: str = "faded"          # ответила, потом перестала — тупик
ABANDONED: str = "abandoned"  # её сообщение без ответа давно — не ответил ты
REJECTED: str = "rejected"    # грубость / «не пиши» — чат удалил бот
DELETED: str = "deleted"      # чат удалён в Telegram (удалил ты)

#: Она написала столько сообщений — знакомство состоялось.
GOOD_HER_MESSAGES: int = 5
#: Тишина дольше — разговор считается мёртвым.
DEAD_AFTER: timedelta = timedelta(hours=72)
#: Первый ответ не позже — «легко».
EASY_FIRST_REPLY: timedelta = timedelta(hours=12)

CATEGORY_LABELS: dict[str, str] = {
    GOOD: "Удачные",
    ACTIVE: "Идут",
    FAILED: "Неудачные",
}

REASON_LABELS: dict[str, str] = {
    SILENT: "молчит",
    FADED: "затухла",
    ABANDONED: "не ответил ты",
    REJECTED: "отшила",
    DELETED: "чат удалён",
}

REASON_HINTS: dict[str, str] = {
    SILENT: "так и не ответила",
    FADED: "ответила, потом перестала — тупик",
    ABANDONED: "её сообщение давно без ответа",
    REJECTED: "грубость или «не пиши» — чат удалён",
    DELETED: "чат удалён в Telegram",
}

EASE_LABELS: dict[str, str] = {"easy": "легко", "hard": "со скрипом"}

#: Шаги сценария, на которых бот сам ответит на её сообщение.
_BOT_STEPS: frozenset[str] = frozenset({"after_pin", "wait_more", "tell_me", "wake_up"})
#: Статусы, при которых бот ещё ведёт переписку.
_BOT_STATUSES: frozenset[str] = frozenset({"WAITING_REPLY", "SCHEDULED"})
#: Бот не смог написать — пишет владелец.
_OWNER_STATUSES: frozenset[str] = frozenset({"NO_ACCESS", "NO_PROFILE", "OTHER_ACCOUNT", "ERROR"})


@dataclass(frozen=True)
class DialogOutcome:
    """Итог переписки."""

    category: str
    reason: str = ""        # для failed
    ease: str = ""          # для good: easy | hard
    needs_you: bool = False

    @property
    def label(self) -> str:
        """Короткая подпись бейджа."""
        if self.category == FAILED:
            return REASON_LABELS.get(self.reason, CATEGORY_LABELS[FAILED])
        if self.category == GOOD:
            return "удачно · " + EASE_LABELS.get(self.ease, "")
        return "идёт"


def parse_time(value: object) -> datetime | None:
    """ISO-строка из БД → aware datetime (UTC), мусор → None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _bot_leads(row: dict[str, Any]) -> bool:
    """Бот ещё сам ведёт переписку (ответит на её сообщение)."""
    status = row.get("intro_status") or ""
    if status in _BOT_STATUSES:
        return True
    return status == "INTRO_SENT" and (row.get("script_step") or "") in _BOT_STEPS


def _ease(row: dict[str, Any]) -> str:
    """Легко: без «АЛООО» и первый её ответ быстро после матча."""
    if row.get("wake_at"):
        return "hard"
    start = parse_time(row.get("greeted_at")) or parse_time(row.get("matched_at"))
    first = parse_time(row.get("first_her_at"))
    if start and first and first - start > EASY_FIRST_REPLY:
        return "hard"
    return "easy"


def classify(row: dict[str, Any], now: datetime | None = None) -> DialogOutcome:
    """Итог переписки по её сводке (строка ``get_dialog_list``)."""
    now = now or datetime.now(timezone.utc)
    status = row.get("intro_status") or ""
    step = row.get("script_step") or ""
    her = int(row.get("her_count") or 0)
    last_by = row.get("last_by") or ""
    last_at = parse_time(row.get("last_message_at")) or parse_time(row.get("matched_at"))
    quiet = (now - last_at) if last_at else timedelta(0)
    dead = quiet > DEAD_AFTER

    if step == "deleted":
        return DialogOutcome(FAILED, reason=REJECTED)
    if row.get("chat_deleted_at"):
        return DialogOutcome(FAILED, reason=DELETED)

    if her == 0:
        if status == "NO_REPLY" or step == "stopped":
            return DialogOutcome(FAILED, reason=SILENT)
        if dead and not _bot_leads(row):
            return DialogOutcome(FAILED, reason=SILENT)
        return DialogOutcome(ACTIVE, needs_you=status in _OWNER_STATUSES)

    # Она писала. Её последнее сообщение без ответа, и бот его не подхватит.
    waits_for_you = last_by == "her" and not _bot_leads(row)
    if dead and last_by == "her" and waits_for_you:
        return DialogOutcome(FAILED, reason=ABANDONED, needs_you=True)
    if dead and last_by != "her":
        return DialogOutcome(FAILED, reason=FADED)
    if step == "stopped":
        return DialogOutcome(FAILED, reason=FADED)

    if step == "handoff" or her >= GOOD_HER_MESSAGES:
        return DialogOutcome(GOOD, ease=_ease(row), needs_you=waits_for_you)
    return DialogOutcome(ACTIVE, needs_you=waits_for_you)


#: Сортировки списка: ключ → подпись.
SORTS: dict[str, str] = {
    "recent": "свежие",
    "her": "больше её сообщений",
    "quiet": "дольше тишина",
    "matched": "новые матчи",
}


def sort_rows(rows: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    """Сортирует сводки переписок (неизвестная сортировка — «свежие»)."""
    epoch = datetime.min.replace(tzinfo=timezone.utc)

    def last(r: dict[str, Any]) -> datetime:
        return parse_time(r.get("last_message_at")) or parse_time(r.get("matched_at")) or epoch

    if sort == "her":
        return sorted(rows, key=lambda r: (int(r.get("her_count") or 0), last(r)), reverse=True)
    if sort == "quiet":
        return sorted(rows, key=last)
    if sort == "matched":
        return sorted(rows, key=lambda r: parse_time(r.get("matched_at")) or epoch, reverse=True)
    return sorted(rows, key=last, reverse=True)

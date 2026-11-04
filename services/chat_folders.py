"""В какую папку Telegram положить чат девушки (владелец, 2026-11-04), Telegram-free.

Все чаты девушек лежат в архиве аккаунта, общение — через четыре папки:

* ``auto``   — **Auto**: бот ведёт её сам (привет без ответа, анкета в
  очереди, шаг сценария) и «Авто» аккаунта включено;
* ``new``    — **New**: только что вышла из автоматики, ни туда ни сюда;
* ``active`` — **Active**: общение устоялось — она написала
  ``active_her_messages``+, владелец ``active_owner_messages``+, разговор живой;
* ``fifty``  — **50/50**: знакомство прошло, но не идёт (``dialog_outcome``:
  молчит, затухла, не ответил ты) — удалять жалко.

Удалённый чат (бот удалил: грубость / «не пиши» / молчит и после «АЛООО»,
или сверка нашла его пустым) — ни в одной папке (None).

Одна сводка переписки + «сейчас» → одна папка; детерминировано, без LLM.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from services.dialog_outcome import FAILED, classify
from services.match_script_rules import ACTIVE_STEPS, DELETED

NEW: str = "new"
ACTIVE: str = "active"
FIFTY: str = "fifty"
AUTO: str = "auto"

#: Порядок папок (номер папки владельца = индекс + 1).
FOLDERS: tuple[str, ...] = (NEW, ACTIVE, FIFTY, AUTO)

#: Статусы автопереписки, при которых бот её ведёт.
_BOT_STATUSES: frozenset[str] = frozenset({"WAITING_REPLY", "SCHEDULED"})


def bot_leads(row: dict[str, Any]) -> bool:
    """Бот сам ведёт переписку: ждёт ответа на привет, анкета в очереди или
    идёт сценарий."""
    status = str(row.get("intro_status") or "")
    step = str(row.get("script_step") or "")
    if status in _BOT_STATUSES:
        return True
    return status == "INTRO_SENT" and (step in ACTIVE_STEPS or not step)


def folder_target(
    row: dict[str, Any],
    *,
    bot_on: bool,
    active_her_messages: int = 10,
    active_owner_messages: int = 3,
    now: datetime | None = None,
) -> str | None:
    """Папка для её чата (``FOLDERS``) или None — ни в одной.

    ``row`` — сводка переписки (как ``get_dialog_list``: ``intro_status``,
    ``script_step``, ``her_count``, ``owner_count``, ``last_by``,
    ``last_message_at``, ``chat_deleted_at``…). ``bot_on`` — «Авто» аккаунта.
    """
    if row.get("chat_deleted_at") or (row.get("script_step") or "") == DELETED:
        return None
    if bot_on and bot_leads(row):
        return AUTO
    outcome = classify(row, now)
    if outcome.category == FAILED:
        return FIFTY
    her = int(row.get("her_count") or 0)
    owner = int(row.get("owner_count") or 0)
    if her >= active_her_messages and owner >= active_owner_messages:
        return ACTIVE
    return NEW

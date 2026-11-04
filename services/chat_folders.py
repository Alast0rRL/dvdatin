"""В какую папку Telegram положить чат девушки (владелец, 2026-11-04), Telegram-free.

Все чаты девушек лежат в архиве аккаунта, общение — через четыре папки:

* ``auto``   — **Auto**: бот прямо сейчас её ведёт (анкета в очереди, шаг
  сценария, свежий привет без ответа — до ``bot_wait_hours`` или после
  «АЛООО») и «Авто» аккаунта включено;
* ``active`` — **Active**: разговор активно идёт несколько дней — она писала
  в ``active_days``+ разных дней и молчит меньше ``silence_hours``;
* ``fifty``  — **50/50**: переписка не идёт — она молчит ``silence_hours``+
  или так и не написала;
* ``new``    — **New**: всё остальное (вышла из автоматики, старые переписки
  владельца — ещё не устоялось и не затихло).

Ни в одной папке (None): удалённый чат (бот удалил: грубость / «не пиши» /
молчит и после «АЛООО», или сверка нашла его пустым) и пустой чат (никто ни
разу не писал). Правило подобрано с владельцем на живых переписках
2026-11-04. Одна сводка + «сейчас» → одна папка; без LLM.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from services.dialog_outcome import parse_time
from services.match_script_rules import ACTIVE_STEPS, DELETED

NEW: str = "new"
ACTIVE: str = "active"
FIFTY: str = "fifty"
AUTO: str = "auto"

#: Порядок папок (номер папки владельца = индекс + 1).
FOLDERS: tuple[str, ...] = (NEW, ACTIVE, FIFTY, AUTO)


def bot_leads(
    row: dict[str, Any], *, bot_wait_hours: float = 72.0,
    now: datetime | None = None,
) -> bool:
    """Бот прямо сейчас её ведёт.

    Анкета в очереди; идёт шаг сценария (пустой шаг — анкета ушла до
    сценария, бот её больше не ведёт); привет без ответа — пока свежий
    (``bot_wait_hours``) или бот уже написал «АЛООО» (ждёт ещё сутки).
    """
    now = now or datetime.now(timezone.utc)
    status = str(row.get("intro_status") or "")
    step = str(row.get("script_step") or "")
    if status == "SCHEDULED":
        return True
    if status == "INTRO_SENT":
        return step in ACTIVE_STEPS
    if status == "WAITING_REPLY":
        if row.get("wake_at"):
            return True
        greeted = parse_time(row.get("greeted_at"))
        return greeted is not None and now - greeted < timedelta(hours=bot_wait_hours)
    return False


def folder_target(
    row: dict[str, Any],
    *,
    bot_on: bool,
    active_days: int = 3,
    silence_hours: float = 48.0,
    bot_wait_hours: float = 72.0,
    now: datetime | None = None,
) -> str | None:
    """Папка для её чата (``FOLDERS``) или None — ни в одной.

    ``row`` — сводка (``Database.list_folder_dialogs``): ``intro_status``,
    ``script_step``, ``greeted_at``, ``wake_at``, ``total`` (сообщений в
    чате), ``her_days`` (разных дней, когда она писала), ``last_her_at``,
    ``chat_deleted_at``. ``bot_on`` — «Авто» аккаунта.
    """
    now = now or datetime.now(timezone.utc)
    if row.get("chat_deleted_at") or (row.get("script_step") or "") == DELETED:
        return None
    if int(row.get("total") or 0) == 0:
        return None
    if bot_on and bot_leads(row, bot_wait_hours=bot_wait_hours, now=now):
        return AUTO
    last_her = parse_time(row.get("last_her_at"))
    if last_her is None or now - last_her >= timedelta(hours=silence_hours):
        return FIFTY
    if int(row.get("her_days") or 0) >= active_days:
        return ACTIVE
    return NEW

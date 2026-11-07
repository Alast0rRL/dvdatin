"""В какую папку Telegram положить чат девушки (владелец, 2026-11-04), Telegram-free.

Все чаты девушек лежат в архиве аккаунта, общение — через четыре папки:

* ``auto``   — **Auto**: бот прямо сейчас её ведёт (анкета в очереди, шаг
  сценария, свежий привет без ответа — до ``bot_wait_hours`` или после
  «АЛООО») и «Авто» аккаунта включено;
* ``active`` — **Active**: разговор активно идёт несколько дней — она писала
  в ``active_days``+ разных дней и молчит меньше ``silence_hours``;
* ``fifty``  — **50/50**: переписка не идёт — она молчит ``silence_hours``+,
  так и не написала, или последнее сообщение наше (владелец / бот) и она не
  отвечает ``unanswered_hours``+;
* ``blocked`` — **ЧС**: она заблокировала нас (``blocked_at``), важнее всех;
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
from services.match_script_rules import ACTIVE_STEPS, DELETED, HANDOFF, OWNER

NEW: str = "new"
ACTIVE: str = "active"
FIFTY: str = "fifty"
AUTO: str = "auto"
BLOCKED: str = "blocked"

#: Порядок папок (номер папки владельца = индекс + 1).
FOLDERS: tuple[str, ...] = (NEW, ACTIVE, FIFTY, AUTO, BLOCKED)

#: Статусы, после которых бот уже не ведёт переписку (владелец сам пишет).
OWNER_STATUSES: frozenset[str] = frozenset({"OWNER_CHAT", "HAS_CHAT"})


def owner_took_over(row: dict[str, Any]) -> bool:
    """Владелец забрал переписку — бот её больше не ведёт.

    Признаки: нет строки ``match_dialogs`` (бота тут не было); статус
    ``OWNER_CHAT`` / ``HAS_CHAT`` (была переписка до привета); владелец
    написал позже последнего сообщения бота.
    """
    has_row = row.get("has_match_dialog")
    status = str(row.get("intro_status") or "")
    owner_at = parse_time(row.get("last_owner_at"))
    bot_at = parse_time(row.get("last_bot_at"))
    if has_row is not None and not int(has_row):
        # Строки match_dialogs нет: бот мог писать до её появления (история
        # подтянула стикер / анкету) — тогда всё решает время, а не отсутствие
        # строки. Совсем без сообщений бота — это чат владельца.
        if bot_at is None:
            return True
        return owner_at is not None and owner_at > bot_at
    if status in OWNER_STATUSES:
        return True
    if owner_at is not None and (bot_at is None or owner_at > bot_at):
        return True
    if not status:
        # Нет строки match_dialogs (флага нет — тестовые dict) — бота тут не было.
        return has_row is None
    return False


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
    if owner_took_over(row):
        return False
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
    unanswered_hours: float = 24.0,
    bot_wait_hours: float = 72.0,
    now: datetime | None = None,
) -> str | None:
    """Папка для её чата (``FOLDERS``) или None — ни в одной.

    ``row`` — сводка (``Database.list_folder_dialogs``): ``intro_status``,
    ``script_step``, ``greeted_at``, ``wake_at``, ``total`` (сообщений в
    чате), ``her_days`` (разных дней, когда она писала), ``last_her_at``,
    ``last_by`` + ``last_msg_at`` (кто и когда писал последним),
    ``last_owner_at`` / ``last_bot_at`` (владелец vs бот), ``chat_deleted_at``.
    ``bot_on`` — «Авто» аккаунта.
    """
    now = now or datetime.now(timezone.utc)
    if row.get("chat_deleted_at") or (row.get("script_step") or "") == DELETED:
        return None
    if int(row.get("total") or 0) == 0:
        return None
    # Она заблокировала нас (эвристика) — ЧС важнее всех папок (2026-11-07).
    if row.get("blocked_at"):
        return BLOCKED
    # Владелец забрал переписку (или бота тут не было) — Auto не бывает,
    # даже если привет свежий / анкета в очереди (владелец, 2026-11-07).
    has_row = row.get("has_match_dialog")
    no_row = has_row is not None and not int(has_row)
    took_over = owner_took_over(row)
    step = str(row.get("script_step") or "")
    if ((took_over and not no_row) or step in (HANDOFF, OWNER)) and (
        (row.get("last_by") or "") == "her"
    ):
        # Последнее слово её — ждёт владельца: New сразу, какие бы 48 ч /
        # 3 дня ни стояли (владелец, 2026-11-07).
        return NEW
    if bot_on and not took_over and bot_leads(
        row, bot_wait_hours=bot_wait_hours, now=now,
    ):
        return AUTO
    last_her = parse_time(row.get("last_her_at"))
    if last_her is None or now - last_her >= timedelta(hours=silence_hours):
        return FIFTY
    # Последнее слово наше (владелец / бот), она не отвечает сутки — не идёт
    # (прод 2026-11-04: Рина Грейс — «Йооу» без ответа лежало в New).
    last_msg = parse_time(row.get("last_msg_at"))
    if (
        (row.get("last_by") or "") in ("owner", "bot") and last_msg is not None
        and now - last_msg >= timedelta(hours=unanswered_hours)
    ):
        return FIFTY
    if int(row.get("her_days") or 0) >= active_days:
        return ACTIVE
    return NEW

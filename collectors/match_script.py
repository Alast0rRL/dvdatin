"""Сценарий знакомства после анкеты с закрепом (режим AUTO).

Схема владельца (2026-10-29), шаги и переходы — ``services/match_script_rules``:

    анкета + закреп (MatchIntro) → AFTER_PIN
      «что это / зачем»                   → «Пусть лежит здесь»   → WAIT_MORE
      «много с кем общаешься, не путать?» → «Это так-то удобно»   → WAIT_MORE
      молчит ``silence_min`` (3 ч) или ответила не про анкету
                                          → «Рассказывайте, дамочка» → TELL_ME
    TELL_ME
      «я не дамочка» → «А кто? Парнишка?»                 → HANDOFF
      «о чём? / что?» → «О себе конечно» | «А о чём обычно…» → HANDOFF
      что-то другое (рассказывает о себе)                 → HANDOFF
      молчит ``give_up_hours`` (сутки)                    → STOPPED

Везде: «не пиши» / «есть парень» / прямое оскорбление → чат удаляется
(DELETED); «ты бот?» → HANDOFF (бот не врёт, отвечает владелец); владелец
сам написал ей после бота → OWNER, бот замолкает. HANDOFF / DELETED /
STOPPED — уведомление владельцу (коллектор).

Её сообщение (живое событие) → ``note_message``: вид ответа и срок ответа
(пауза ``reply_delay_sec``, удаление — сразу) пишутся в строку; отправка — в
``process`` (фоновый цикл раз в минуту). Перед каждым действием — проверка
её чата: исходящее новее последнего сообщения бота (не служебное) значит, что
пишет владелец. Её ответы, пропущенные пока бот лежал, находятся там же при
срабатывании таймера молчания. Текст переписки в лог не пишется.
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from loguru import logger

from collectors.match_greeter import flood_pause_sec
from collectors.match_intro import INTRO_SENT
from services.match_script_rules import (
    ACTIVE_STEPS,
    AFTER_PIN,
    DELETED,
    ERROR,
    OWNER,
    STOP_KINDS,
    TELL_ME,
    WAIT_MORE,
    classify,
    decide,
    decide_silence,
    priority,
    strongest,
)

if TYPE_CHECKING:
    from app.config import MatchScriptConfig
    from database.database import Database

#: Отложенное действие «она молчит» (повтор после FloodWait).
SILENCE: str = "silence"
#: Сколько последних сообщений её чата смотреть перед действием.
SCAN_HISTORY: int = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _msg_text(msg: Any) -> str:
    text = getattr(msg, "message", None) or getattr(msg, "text", None) or ""
    return text if isinstance(text, str) else ""


class MatchScript:
    """Ведёт девушку по веткам схемы до передачи владельцу."""

    def __init__(self, config: MatchScriptConfig, db: Database | None) -> None:
        self._config = config
        self._db = db
        self._rng = random.Random()
        # Колбэк «бот отправил девушке» — журнал переписок (DialogLogger.log_sent).
        self.on_sent: Callable[[str, int, Any], Awaitable[Any]] | None = None

    def allowed_in(self, mode: str) -> bool:
        return bool(self._config.enabled) and mode in self._config.modes

    async def start(
        self, account_session: str, user_id: int, msg_id: object = 0,
    ) -> bool:
        """Анкета отправлена → сценарий с шага AFTER_PIN. ``msg_id`` —
        последнее сообщение бота в её чате (анкета)."""
        if self._db is None or not self._config.enabled or not user_id:
            return False
        try:
            await self._db.update_match_dialog(
                account_session, int(user_id),
                script_step=AFTER_PIN, script_at=_now().isoformat(),
                script_msg_id=int(msg_id or 0),
                script_pending="", script_due_at="",
            )
        except Exception as e:
            logger.error(f"MatchScript: не начал сценарий: {e}")
            return False
        return True

    async def note_message(
        self,
        account_session: str,
        user_id: int,
        text: str,
        mode: str,
        at: datetime | None = None,
    ) -> bool:
        """Её сообщение в личке → отложенный ответ. True — записан."""
        if self._db is None or not self.allowed_in(mode) or not user_id:
            return False
        try:
            row = await self._db.get_match_dialog(account_session, int(user_id))
        except Exception as e:
            logger.error(f"MatchScript: не прочитал переписку: {e}")
            return False
        if not isinstance(row, dict) or row.get("status") != INTRO_SENT:
            return False
        if row.get("script_step") not in ACTIVE_STEPS:
            return False
        started = _parse_dt(row.get("script_at"))
        if at is not None and started is not None:
            when = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
            if when < started:
                return False
        kind = classify(text if isinstance(text, str) else "")
        pending = row.get("script_pending") or ""
        if pending and priority(kind) < priority(pending):
            return True
        now = _now()
        if kind in STOP_KINDS:
            due = now
        else:
            low, high = self._delay_range()
            due = now + timedelta(seconds=self._rng.uniform(low, high))
        try:
            await self._db.update_match_dialog(
                account_session, int(user_id),
                script_pending=kind, script_due_at=due.isoformat(),
            )
        except Exception as e:
            logger.error(f"MatchScript: не записал ответ: {e}")
            return False
        logger.info(
            f"MatchScript: {row.get('name') or user_id} ответила ({kind}) — "
            f"шаг {row.get('script_step')}"
        )
        return True

    async def process(
        self, client: Any, account_session: str, mode: str,
    ) -> list[tuple[dict, str, str]]:
        """Один проход: созревшие ответы и таймеры молчания.

        [(строка, новый шаг, вид её ответа | ``silence``)] — для уведомлений.
        """
        if self._db is None or not self.allowed_in(mode):
            return []
        try:
            rows = await self._db.list_match_script_dialogs(
                account_session, ACTIVE_STEPS,
            )
        except Exception as e:
            logger.error(f"MatchScript: не прочитал очередь: {e}")
            return []
        results: list[tuple[dict, str, str]] = []
        for row in rows:
            try:
                res = await self._process_row(client, account_session, row)
            except Exception as e:
                logger.error(
                    f"MatchScript: {row.get('name') or row.get('user_id')}: {e}"
                )
                await self._update(
                    account_session, int(row["user_id"]),
                    script_step=ERROR, error=f"script: {e}"[:300],
                )
                continue
            if res is not None:
                results.append(res)
        return results

    async def _process_row(
        self, client: Any, account_session: str, row: dict,
    ) -> tuple[dict, str, str] | None:
        user_id = int(row["user_id"])
        step = str(row.get("script_step") or "")
        now = _now()
        pending = str(row.get("script_pending") or "")
        if pending:
            due = _parse_dt(row.get("script_due_at"))
            if due is not None and due > now:
                return None
            silence = pending == SILENCE
        else:
            started = _parse_dt(row.get("script_at"))
            if step == TELL_ME:
                limit = timedelta(hours=float(self._config.give_up_hours))
            else:
                limit = timedelta(minutes=float(self._config.silence_min))
            if started is None or now - started < limit:
                return None
            silence = True
        if silence and step in (AFTER_PIN, WAIT_MORE) and self._quiet(now):
            return None

        peer = await client.get_input_entity(row.get("username") or user_id)
        owner_wrote, her_texts = await self._scan(client, peer, row)
        if owner_wrote:
            logger.info(
                f"MatchScript: с {row.get('name') or user_id} пишет владелец — "
                "бот молчит"
            )
            await self._update(
                account_session, user_id,
                script_step=OWNER, script_pending="", script_due_at="",
            )
            return row, OWNER, pending

        kind = pending
        if silence and her_texts:
            # Ответила, пока бот лежал (событие пропущено).
            kind = strongest([classify(t) for t in her_texts])
            silence = False
        if silence:
            key, nxt = decide_silence(step)
            kind = SILENCE
        else:
            key, nxt = decide(step, kind)

        if nxt == DELETED:
            await client.delete_dialog(
                peer, revoke=bool(self._config.delete_for_both),
            )
            logger.info(f"MatchScript: {row.get('name') or user_id} — чат удалён")
            await self._update(
                account_session, user_id,
                script_step=DELETED, script_pending="", script_due_at="",
            )
            return row, DELETED, kind

        fields: dict[str, object] = {
            "script_step": nxt, "script_pending": "", "script_due_at": "",
        }
        text, idx = self._pick(key)
        if text:
            try:
                sent = await client.send_message(peer, text)
            except Exception as e:
                pause = flood_pause_sec(e)
                if pause is None:
                    raise
                due = now + timedelta(seconds=pause)
                logger.warning(
                    f"MatchScript: {account_session} FloodWait — повтор через "
                    f"{pause // 60} мин"
                )
                await self._update(
                    account_session, user_id,
                    script_pending=SILENCE if silence else kind,
                    script_due_at=due.isoformat(), error=str(e)[:300],
                )
                return None
            if self.on_sent is not None:
                try:
                    await self.on_sent(account_session, user_id, sent)
                except Exception as e:
                    logger.error(f"MatchScript: журнал переписки: {e}")
            sent_id = getattr(sent, "id", None)
            fields["script_at"] = _now().isoformat()
            if isinstance(sent_id, int) and sent_id:
                fields["script_msg_id"] = sent_id
            variants = str(row.get("script_variant") or "")
            fields["script_variant"] = (
                f"{variants},{key}:{idx}" if variants else f"{key}:{idx}"
            )
            row["script_text"] = text
        await self._update(account_session, user_id, **fields)
        logger.info(
            f"MatchScript: {row.get('name') or user_id}: {step} → {nxt}"
            f"{f' ({key})' if text else ''}"
        )
        return row, nxt, kind

    async def _scan(
        self, client: Any, peer: Any, row: dict,
    ) -> tuple[bool, list[str]]:
        """(владелец писал после бота, её тексты после бота — новые первыми).

        Граница — последнее сообщение бота (``script_msg_id``), иначе время
        ``script_at``. Служебные исходящие (закреп) не считаются.
        """
        last_id = int(row.get("script_msg_id") or 0)
        since = _parse_dt(row.get("script_at"))
        owner_wrote = False
        texts: list[str] = []
        async for msg in client.iter_messages(peer, limit=SCAN_HISTORY):
            msg_id = getattr(msg, "id", None)
            if last_id and isinstance(msg_id, int):
                if msg_id <= last_id:
                    break
            else:
                date = getattr(msg, "date", None)
                if isinstance(date, datetime) and since is not None:
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)
                    if date <= since:
                        break
            if getattr(msg, "out", False):
                if getattr(msg, "action", None) is None:
                    owner_wrote = True
                continue
            texts.append(_msg_text(msg))
        return owner_wrote, texts

    def _pick(self, key: str | None) -> tuple[str, int]:
        """Случайный вариант фразы (текст, номер); нет фразы — ("", -1)."""
        if not key:
            return "", -1
        variants = [v for v in getattr(self._config.texts, key, []) if v]
        if not variants:
            return "", -1
        idx = self._rng.randrange(len(variants))
        return variants[idx], idx

    def _delay_range(self) -> tuple[float, float]:
        raw = list(self._config.reply_delay_sec or [0.0])
        low = float(raw[0])
        high = float(raw[1]) if len(raw) > 1 else low
        return min(low, high), max(low, high)

    def _quiet(self, now: datetime) -> bool:
        """Ночь по местному времени — таймер молчания не пишет."""
        hours = list(self._config.quiet_hours or [])
        if len(hours) < 2 or hours[0] == hours[1]:
            return False
        local = now + timedelta(hours=float(self._config.utc_offset_hours))
        start, end, h = int(hours[0]), int(hours[1]), local.hour
        return start <= h < end if start < end else (h >= start or h < end)

    async def _update(
        self, account_session: str, user_id: int, **fields: object,
    ) -> None:
        try:
            await self._db.update_match_dialog(account_session, user_id, **fields)
        except Exception as e:
            logger.error(f"MatchScript: не записал переписку: {e}")

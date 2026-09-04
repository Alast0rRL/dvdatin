# Manual Review Recorder (Stage 8): фиксация РУЧНОГО решения владельца по REVIEW-анкетам.
# Telegram-free слой: не импортирует Telethon. Работает с Profile/str и Database.
#
# Сценарий: детерминированный скоринг выдал REVIEW (бот «не справляется» — не хватает
# информации/уверенности). Бот вместо авто-действия уведомляет владельца, что нужно его
# решение. Владелец сам заходит в Дайвинчик и ставит лайк/дизлайк (или пишет сообщение)
# с того же аккаунта, под которым слушает collector. Исходящее действие перехватывается,
# привязывается к активной REVIEW-анкете и дописывается в файл журнала (JSON или Markdown).

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from database.database import Database

# Эмодзи-действия Дайвинчика (транспорт): те же, что в collector/AutoActionEngine.
LIKE_TEXT: str = "\u2764\ufe0f"      # ❤️
DISLIKE_TEXT: str = "\U0001F44E"     # 👎


class ManualAction(StrEnum):
    """Классифицированное ручное действие владельца в Дайвинчике."""

    LIKE = "LIKE"
    DISLIKE = "DISLIKE"
    MESSAGE = "MESSAGE"


def classify_outgoing(text: str) -> ManualAction:
    """Классифицирует исходящий текст владельца в действие.

    ``❤️`` → LIKE, ``👎`` → DISLIKE, любой другой непустой текст → MESSAGE
    (владелец написал анкете сообщение — фиксируем и его текст).
    """
    stripped = (text or "").strip()
    if stripped == LIKE_TEXT:
        return ManualAction.LIKE
    if stripped == DISLIKE_TEXT:
        return ManualAction.DISLIKE
    return ManualAction.MESSAGE


def _obj_record(
    profile: dict,
    ai: dict,
    action: ManualAction,
    text: str,
    outgoing_tm_id: int,
    recorded_at: str,
) -> dict:
    """Собирает JSON-запись о ручном решении по REVIEW-анкете."""
    return {
        "recorded_at": recorded_at,
        "profile_id": profile.get("id"),
        "name": profile.get("name", ""),
        "age": profile.get("age"),
        "city": profile.get("normalized_city") or profile.get("raw_city", ""),
        "description": profile.get("description", ""),
        "ai_decision": ai.get("decision", ""),
        "ai_decision_id": ai.get("id"),
        "combined_score": ai.get("combined_score"),
        "confidence": ai.get("confidence"),
        "reasons": _reasons(ai),
        "manual_action": action.value,
        "manual_text": text,
        "telegram_message_id": outgoing_tm_id,
    }


def _reasons(ai: dict) -> list[str]:
    """Нормализует reasons (JSON-строка или список) в список строк."""
    raw = ai.get("reasons", "[]")
    if isinstance(raw, list):
        return [str(r) for r in raw]
    try:
        parsed = json.loads(raw or "[]")
        if isinstance(parsed, list):
            return [str(r) for r in parsed]
    except (ValueError, TypeError):
        pass
    return [str(raw)] if raw else []


class ManualReviewRecorder:
    """Записывает ручные решения владельца по REVIEW-анкетам в файл.

    Журнал хранится в ``data/reviews/review_log.json`` (JSON-список записей)
    или ``.md`` (Markdown-таблица). При каждой записи файл переписывается
    целиком (история сохраняется). Ошибки файла/БД не роняют collector:
    перехватываются и логируются.
    """

    def __init__(
        self,
        db: "Database",
        path: Path = Path("data/reviews/review_log.json"),
        enabled: bool = False,
        file_format: str = "json",
    ) -> None:
        self._db = db
        self._path = path
        self._enabled = enabled
        self._fmt = file_format

    @property
    def enabled(self) -> bool:
        """Гейт записи ручных решений."""
        return self._enabled

    async def handle_outgoing(
        self,
        chat_id: int,
        context_message_id: int | None,
        outgoing_tm_id: int,
        text: str,
    ) -> bool:
        """Пытается зафиксировать ручное действие по активной REVIEW-анкете.

        Возвращает ``True``, если действие привязано к REVIEW-анкете и записано,
        иначе ``False`` (нет контекста / профиль не REVIEW / не включено).

        ``context_message_id`` — PROFILE-сообщение активной анкеты чата
        (``chat_context``/``_pending_profiles``); ``outgoing_tm_id`` — исходящее
        сообщение владельца с действием.
        """
        if not self._enabled:
            return False
        if not context_message_id:
            return False
        if not (text or "").strip():
            return False

        try:
            profile = await self._db.find_profile_by_message(
                chat_id, context_message_id,
            )
        except Exception as e:
            logger.error(f"ManualReview: ошибка поиска профиля по контексту: {e}")
            return False
        if profile is None:
            return False

        try:
            ai = await self._db.get_latest_ai_decision(profile["id"])
        except Exception as e:
            logger.error(f"ManualReview: ошибка получения AI-решения: {e}")
            return False
        if ai is None or ai.get("decision") != "REVIEW":
            return False

        action = classify_outgoing(text)
        recorded_at = datetime.now(timezone.utc).isoformat()
        record = _obj_record(
            profile, ai, action, (text or "").strip(), outgoing_tm_id, recorded_at,
        )

        try:
            self._append(record)
        except Exception as e:
            logger.error(f"ManualReview: ошибка записи в файл: {e}")
            return False

        logger.info(
            f"ManualReview: зафиксировано ручное {action} по профилю "
            f"#{profile['id']} (REVIEW #{ai.get('id')}), msg={outgoing_tm_id}"
        )
        return True

    def _append(self, record: dict) -> None:
        """Дописывает запись в файл журнала (JSON или Markdown)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._fmt == "md":
            self._append_md(record)
        else:
            self._append_json(record)

    def _append_json(self, record: dict) -> None:
        """Переписывает JSON-файл, сохраняя историю записей."""
        records: list[dict] = []
        if self._path.exists():
            try:
                records = json.loads(self._path.read_text(encoding="utf-8"))
                if not isinstance(records, list):
                    records = []
            except (ValueError, OSError):
                records = []
        records.append(record)
        self._path.write_text(
            json.dumps(records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _append_md(self, record: dict) -> None:
        """Дописывает запись в Markdown-файл (строка таблицы)."""
        reasons = ", ".join(record.get("reasons", []))[:200]
        line = (
            f"| {record['recorded_at']} | #{record['profile_id']} | "
            f"{record['name']}, {record['age']} | {record['city']} | "
            f"{record['manual_action']} | {record['manual_text']!r} | "
            f"{record['combined_score']} | {reasons} |\n"
        )
        first_write = not self._path.exists() or self._path.stat().st_size == 0
        if first_write:
            self._write_md_header()
        else:
            head = self._path.read_text(encoding="utf-8").splitlines()
            if not any(line.startswith("| recorded_at |") for line in head):
                self._write_md_header(append=True)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line)

    def _write_md_header(self, append: bool = False) -> None:
        """Пишет заголовок Markdown-таблицы (create или append)."""
        header = (
            "| recorded_at | profile | name,age | city | manual_action | "
            "manual_text | score | reasons |\n"
            "|---|---|---|---|---|---|---|---|\n"
        )
        mode = "a" if append else "w"
        with open(self._path, mode, encoding="utf-8") as f:
            f.write(header)

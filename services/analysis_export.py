# Анализ и очистка БД — экспорт всех анкёт в CSV + сброс данных.
#
# CLI:
#   python main.py --export-analysis   → data/exports/analysis_<date>.csv
#   python main.py --clear-db          → бэкап + очистка всех таблиц
#
# Экспорт НЕ содержит Telegram-данных (токены, session, api keys).
# Очистка создаёт резервную копию перед удалением.

from __future__ import annotations

import csv
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from database.database import Database

EXPORTS_DIR = Path("data/exports")


async def export_analysis_csv(
    db: Database, directory: Path = EXPORTS_DIR,
) -> tuple[Path, dict]:
    """Экспортирует все анкеты в CSV для анализа.

    Возвращает (путь_к_файлу, сводка_по_категориям).

    Категории (action):
      - liked     — лайк (авто или ручной)
      - disliked  — дизлайк (авто или ручной)
      - skipped   — SKIP через ручное решение
      - approved  — APPROVE через ручное решение
      - reviewed  — AI вынес вердикт (LIKE/DISLIKE), но реакция не отправлена
      - pending   — REVIEW, ожидает решения
      - seen      — без AI-решения
    """
    directory.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = directory / f"analysis_{ts}.csv"

    rows = await db.get_profiles_for_analysis()

    counts: dict[str, int] = {}
    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            parsed = _parse_row(row)
            action = parsed["action"]
            counts[action] = counts.get(action, 0) + 1
            writer.writerow(parsed)

    total = len(rows)
    summary = {"total": total, "breakdown": counts}
    logger.info(
        f"Анализ экспортирован: {out} ({total} анкет) — {counts}"
    )
    return out, summary


async def clear_database(
    db: Database,
    backup_dir: Path = EXPORTS_DIR / "backups",
) -> Path:
    """Очищает все данные из БД после бэкапа.

    Возвращает путь к бэкап-файлу. Бэкап делается из фактического
    файла БД (``db.path``), чтобы корректно работать с tmp-базами в тестах.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = backup_dir / f"database_{ts}.db"

    shutil.copy2(db.path, backup)
    logger.info(f"Бэкап создан: {backup}")

    await db.clear_all_data()
    logger.info(f"БД очищена (бэкап: {backup})")
    return backup


# ── Internal ──────────────────────────────────────────────────────

_CSV_FIELDS = [
    "profile_id",
    "name",
    "age",
    "city",
    "description",
    "action",
    "ai_decision",
    "ai_score",
    "ai_confidence",
    "filter_decision",
    "human_decision",
    "human_agreement",
    "auto_liked",
    "auto_disliked",
    "auto_message_text",
    "auto_first_action",
    "auto_first_action_at",
    "manual_liked",
    "manual_disliked",
    "manual_action",
    "manual_text",
    "manual_at",
    "matched",
    "responded",
    "status",
    "first_seen_at",
    "last_seen_at",
    "action_reasons",
]


def _parse_row(row: dict) -> dict:
    """Определяет action (категорию) на основе данных строки."""
    status = row.get("status")
    human_decision = row.get("human_decision")
    ai_decision = row.get("ai_decision")
    auto_liked = row.get("auto_liked")
    auto_disliked = row.get("auto_disliked")
    manual_liked = row.get("manual_liked")
    manual_disliked = row.get("manual_disliked")

    # Human-решение — верхний приоритет
    if human_decision == "APPROVE":
        action = "approved"
    elif human_decision == "REJECT":
        action = "disliked"
    elif human_decision == "SKIP":
        action = "skipped"
    # Авто-действие (LIKE/DISLIKE отправлены ботом)
    elif auto_liked:
        action = "liked"
    elif auto_disliked:
        action = "disliked"
    # Ручное действие (сам лайкнул/дизлайкнул)
    elif manual_liked:
        action = "liked"
    elif manual_disliked:
        action = "disliked"
    # AI решил, но реакция не отправлена
    elif ai_decision in ("LIKE", "DISLIKE"):
        action = "reviewed"
    # REVIEW — ожидает решения
    elif ai_decision == "REVIEW":
        action = "pending"
    # Без решения
    else:
        action = "seen"

    reasons_raw = row.get("ai_reasons") or "[]"
    try:
        reasons_list = (
            json.loads(reasons_raw) if isinstance(reasons_raw, str) else reasons_raw
        )
    except (json.JSONDecodeError, TypeError):
        reasons_list = []

    return {
        "profile_id": row.get("profile_id"),
        "name": row.get("name") or "",
        "age": row.get("age") or "",
        "city": row.get("city") or "",
        "description": (row.get("description") or "")[:500],
        "action": action,
        "ai_decision": ai_decision or "",
        "ai_score": _fmt(row.get("ai_score")),
        "ai_confidence": _fmt(row.get("ai_confidence")),
        "filter_decision": row.get("filter_decision") or "",
        "human_decision": human_decision or "",
        "human_agreement": row.get("human_agreement") or "",
        "auto_liked": row.get("auto_liked") or 0,
        "auto_disliked": row.get("auto_disliked") or 0,
        "auto_message_text": row.get("auto_message_text") or "",
        "auto_first_action": row.get("auto_first_action") or "",
        "auto_first_action_at": row.get("auto_first_action_at") or "",
        "manual_liked": row.get("manual_liked") or 0,
        "manual_disliked": row.get("manual_disliked") or 0,
        "manual_action": row.get("manual_action") or "",
        "manual_text": row.get("manual_text") or "",
        "manual_at": row.get("manual_at") or "",
        "matched": row.get("matched") or 0,
        "responded": row.get("responded") or 0,
        "status": status or "",
        "first_seen_at": row.get("first_seen_at") or "",
        "last_seen_at": row.get("last_seen_at") or "",
        "action_reasons": "; ".join(reasons_list) if reasons_list else "",
    }


def _fmt(value) -> str:
    """Форматирует число для CSV."""
    if value is None:
        return ""
    return f"{float(value):.3f}"

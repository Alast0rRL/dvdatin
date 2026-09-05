# Review Export (Stage 6): выгрузка review-датасета в CSV.
# CLI: python main.py --export-review
#
# БЕЗОПАСНОСТЬ:
# - НЕ экспортирует Telegram auth data, API keys, session, phone.
# - Только profile_id (без персональных данных человека).

from __future__ import annotations

import csv
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from database.database import Database

EXPORTS_DIR = Path("data/exports")

CSV_FIELDS = [
    "profile_id",
    "ai_decision",
    "combined_score",
    "confidence",
    "human_decision",
    "agreement",
    "scoring_version",
    "created_at",
    "reviewed_at",
]


async def export_review_csv(db: Database, directory: Path = EXPORTS_DIR) -> Path:
    """Экспортирует review dataset в CSV и возвращает путь к файлу.

    Raises:
        RuntimeError: Если нет рецензий для экспорта.
    """
    reviews = await db.get_human_reviews_with_ai()
    if not reviews:
        msg = "Нет рецензий для экспорта"
        raise RuntimeError(msg)

    directory.mkdir(parents=True, exist_ok=True)
    out = directory / "review_export.csv"

    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for r in reviews:
            writer.writerow({
                "profile_id": r.get("profile_id"),
                "ai_decision": r.get("ai_decision"),
                "combined_score": _num(r.get("combined_score")),
                "confidence": _num(r.get("confidence")),
                "human_decision": r.get("human_decision"),
                "agreement": r.get("agreement"),
                "scoring_version": r.get("scoring_version"),
                "created_at": r.get("evaluated_at") or r.get("reviewed_at"),
                "reviewed_at": r.get("reviewed_at"),
            })

    logger.info(
        f"Review dataset exported: {out} "
        f"({len(reviews)} записей)"
    )
    return out


def _num(value) -> str:
    """Красиво форматирует число для CSV."""
    if value is None:
        return ""
    return f"{value:g}"

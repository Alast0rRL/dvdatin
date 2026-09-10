# Sync DB adapter — bridges Flask (sync) with async Database (aiosqlite).
#
# Использует прямой синхронный sqlite3-доступ к файлу БД.
# Все read-only операции идут через sqlite3 (быстро, безопасно в WAL mode).
# Write-операции (save_human_decision и т.д.) идут через существующие async services
# через event loop bridge.

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class SyncDB:
    """Синхронная обёртка над SQLite для Flask.

    Открывает отдельное соединение (read-only safe в WAL mode).
    НЕ дублирует бизнес-логику — только читает данные для отображения.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _query_one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def _execute(self, sql: str, params: tuple = ()) -> int | None:
        conn = self._connect()
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    # ── Profiles ──────────────────────────────────────────────────

    def get_profiles_paginated(
        self, offset: int = 0, limit: int = 20,
        decision_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Получает профили с пагинацией, объединённые с AI-решениями."""
        sql = """
            SELECT
                p.id, p.name, p.age, p.raw_city, p.normalized_city,
                p.description, p.status, p.first_seen_at, p.last_seen_at,
                ad.id as ai_decision_id, ad.decision, ad.combined_score,
                ad.confidence, ad.reasons, ad.evaluated_at,
                fr.decision as filter_decision, fr.reasons as filter_reasons,
                hd.decision as human_decision, hd.agreement,
                hd.created_at as human_decided_at
            FROM profiles p
            LEFT JOIN ai_decisions ad ON ad.id = (
                SELECT id FROM ai_decisions
                WHERE profile_id = p.id
                ORDER BY evaluated_at DESC LIMIT 1
            )
            LEFT JOIN filter_results fr ON fr.id = (
                SELECT id FROM filter_results
                WHERE profile_id = p.id
                ORDER BY evaluated_at DESC LIMIT 1
            )
            LEFT JOIN human_decisions hd ON hd.ai_decision_id = ad.id
        """
        params: list[Any] = []
        if decision_filter:
            sql += " WHERE ad.decision = ?"
            params.append(decision_filter)
        sql += " ORDER BY p.last_seen_at DESC, p.id DESC"
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return self._query(sql, tuple(params))

    def get_profiles_count(self, decision_filter: str | None = None) -> int:
        sql = """
            SELECT COUNT(*) as cnt
            FROM profiles p
            LEFT JOIN ai_decisions ad ON ad.id = (
                SELECT id FROM ai_decisions
                WHERE profile_id = p.id
                ORDER BY evaluated_at DESC LIMIT 1
            )
        """
        params: list[Any] = []
        if decision_filter:
            sql += " WHERE ad.decision = ?"
            params.append(decision_filter)
        row = self._query_one(sql, tuple(params))
        return row["cnt"] if row else 0

    def get_profile_full(self, profile_id: int) -> dict[str, Any] | None:
        """Получает полный профиль со всеми связанными данными."""
        profile = self._query_one(
            "SELECT * FROM profiles WHERE id = ?", (profile_id,)
        )
        if not profile:
            return None

        # AI decisions history
        profile["ai_decisions"] = self._query(
            "SELECT * FROM ai_decisions WHERE profile_id = ? ORDER BY evaluated_at DESC",
            (profile_id,),
        )
        # Filter results
        profile["filter_results"] = self._query(
            "SELECT * FROM filter_results WHERE profile_id = ? ORDER BY evaluated_at DESC",
            (profile_id,),
        )
        # Human decisions
        profile["human_decisions"] = self._query(
            "SELECT * FROM human_decisions WHERE profile_id = ? ORDER BY created_at DESC",
            (profile_id,),
        )
        # Profile messages (photos, etc.)
        profile["messages"] = self._query(
            """
            SELECT pm.*, rm.text as raw_text, rm.media_type
            FROM profile_messages pm
            JOIN raw_messages rm ON rm.telegram_message_id = pm.telegram_message_id
                AND rm.chat_id = pm.chat_id
            WHERE pm.profile_id = ?
            ORDER BY pm.created_at ASC
            """,
            (profile_id,),
        )
        return profile

    def get_profile_messages(self, profile_id: int) -> list[dict[str, Any]]:
        return self._query(
            """
            SELECT pm.*, rm.text as raw_text, rm.media_type
            FROM profile_messages pm
            JOIN raw_messages rm ON rm.telegram_message_id = pm.telegram_message_id
                AND rm.chat_id = pm.chat_id
            WHERE pm.profile_id = ?
            ORDER BY pm.created_at ASC
            """,
            (profile_id,),
        )

    # ── AI Decisions ──────────────────────────────────────────────

    def get_latest_ai_decision(self, profile_id: int) -> dict[str, Any] | None:
        return self._query_one(
            "SELECT * FROM ai_decisions WHERE profile_id = ? ORDER BY evaluated_at DESC LIMIT 1",
            (profile_id,),
        )

    def get_ai_decision(self, ai_decision_id: int) -> dict[str, Any] | None:
        return self._query_one(
            "SELECT * FROM ai_decisions WHERE id = ?", (ai_decision_id,),
        )

    # ── Filter Results ────────────────────────────────────────────

    def get_latest_filter_result(self, profile_id: int) -> dict[str, Any] | None:
        return self._query_one(
            "SELECT * FROM filter_results WHERE profile_id = ? ORDER BY evaluated_at DESC LIMIT 1",
            (profile_id,),
        )

    # ── Human Decisions ───────────────────────────────────────────

    def get_human_decision(self, profile_id: int) -> dict[str, Any] | None:
        return self._query_one(
            "SELECT * FROM human_decisions WHERE profile_id = ? ORDER BY created_at DESC LIMIT 1",
            (profile_id,),
        )

    def is_already_reviewed(self, ai_decision_id: int) -> bool:
        row = self._query_one(
            "SELECT 1 FROM human_decisions WHERE ai_decision_id = ?",
            (ai_decision_id,),
        )
        return row is not None

    def save_human_decision(
        self,
        profile_id: int,
        ai_decision_id: int,
        decision: str,
        agreement: str = "UNRESOLVED",
    ) -> int | None:
        """Сохраняет решение человека (APPROVE/REJECT/SKIP)."""
        now = datetime.now(timezone.utc).isoformat()
        return self._execute(
            """
            INSERT INTO human_decisions (profile_id, ai_decision_id, decision, agreement, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (profile_id, ai_decision_id, decision, agreement, now),
        )

    # ── Stats ─────────────────────────────────────────────────────

    def count_profiles(self) -> int:
        row = self._query_one("SELECT COUNT(*) as cnt FROM profiles")
        return row["cnt"] if row else 0

    def count_decisions(self) -> dict[str, int]:
        rows = self._query(
            "SELECT decision, COUNT(*) as cnt FROM ai_decisions GROUP BY decision"
        )
        return {r["decision"]: r["cnt"] for r in rows}

    def count_human_decisions(self) -> dict[str, int]:
        rows = self._query(
            "SELECT decision, COUNT(*) as cnt FROM human_decisions GROUP BY decision"
        )
        return {r["decision"]: r["cnt"] for r in rows}

    def get_pending_review_count(self) -> int:
        row = self._query_one("""
            SELECT COUNT(*) as cnt FROM ai_decisions ad
            WHERE ad.decision = 'REVIEW'
            AND NOT EXISTS (
                SELECT 1 FROM human_decisions hd WHERE hd.ai_decision_id = ad.id
            )
        """)
        return row["cnt"] if row else 0

    # ── Settings ──────────────────────────────────────────────────

    def get_filter_config(self, config_path: Path) -> dict[str, Any]:
        """Читает текущие настройки фильтров из config.yaml."""
        import yaml
        if not config_path.exists():
            return {"age_min": 18, "age_max": 19, "city_allowed": []}
        with open(config_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        filters = raw.get("filters", {})
        age = filters.get("age", {})
        city = filters.get("city", {})
        return {
            "age_min": age.get("min", 18),
            "age_max": age.get("max", 19),
            "city_allowed": city.get("allowed", []),
        }

    def get_mode(self, config_path: Path) -> str:
        """Читает текущий режим из config.yaml."""
        import yaml
        if not config_path.exists():
            return "OBSERVE"
        with open(config_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return raw.get("project", {}).get("mode", "OBSERVE")

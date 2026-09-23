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

    def get_profile_message(
        self, profile_id: int, telegram_message_id: int,
    ) -> dict[str, Any] | None:
        """Отдаёт связку сообщение↔профиль (chat_id, account_session и т.д.).

        Нужно фото-роуту, чтобы качать фото ТЕМ аккаунтом, который реально
        получил сообщение (account_session), и не подсунуть фото другой анкеты.
        """
        return self._query_one(
            """
            SELECT pm.*, rm.text as raw_text, rm.media_type
            FROM profile_messages pm
            LEFT JOIN raw_messages rm
                ON rm.telegram_message_id = pm.telegram_message_id
                AND rm.chat_id = pm.chat_id
            WHERE pm.profile_id = ? AND pm.telegram_message_id = ?
            """,
            (profile_id, telegram_message_id),
        )

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

    def get_feed_signals(self) -> dict[str, Any]:
        """Signals для авто-обновления ленты.

        Ловит и новые анкеты (MAX(profiles.id)), и повторы уже известных
        (MAX(profiles.last_seen_at)), и любую новую активность в чате
        (MAX(raw_messages.id) — RAW пишется ДО обработки).
        """
        row = self._query_one("""
            SELECT
                (SELECT MAX(id) FROM profiles) AS max_profile_id,
                (SELECT MAX(last_seen_at) FROM profiles) AS max_last_seen,
                (SELECT MAX(id) FROM raw_messages) AS max_raw_id
        """)
        return row or {
            "max_profile_id": None,
            "max_last_seen": None,
            "max_raw_id": None,
        }

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

    # ── Chat feed (Stage 8.4 — единая вкладка «Чат») ────────────────

    def get_chat_feed(
        self,
        page: int = 1,
        per_page: int = 50,
        chat_id: int | None = None,
    ) -> dict[str, Any]:
        """Собирает хронологическую ленту «Чат» — сырой поток Leo как TG-чат.

        Merge с трёх источников по времени (ASC):
          * raw_messages        — входящие (пузыри слева, Leo)
          * sent_messages       — ручные/веб исходящие (пузыри справа)
          * auto_actions_log    — авто-действия (LIKE/DISLIKE/MESSAGE, справа)
        Каждая входящая запись обогащается inline:
          * профиль  — если raw → profile (через profile_messages join),
            с фото (media_type='photo'), AI-решением и человеческим решением;
          * капча    — если текст совпадает по сигнатуре с captcha_memory
            (pending → форма ответа inline, known → выученный ответ).
        Возвращает {items, total, page, total_pages} — пагинация как у ленты.
        """
        offset = (max(1, page) - 1) * per_page

        # ── Входящие: raw_messages ──
        raw_sql = """
            SELECT
                'raw' AS kind, rm.id AS row_id, rm.chat_id, rm.telegram_message_id,
                rm.sender_name, rm.sender_username, rm.media_type,
                rm.text AS text, rm.message_date AS ts, rm.media_type AS media_type
            FROM raw_messages rm
        """
        raw_params: list[Any] = []
        if chat_id is not None:
            raw_sql += " WHERE rm.chat_id = ?"
            raw_params.append(chat_id)
        raw_sql += " ORDER BY rm.message_date ASC, rm.id ASC"

        # ── Исходящие: sent_messages (ручные/веб) ──
        sent_sql = """
            SELECT
                'sent' AS kind, sm.id AS row_id, sm.chat_id,
                sm.telegram_message_id, NULL AS sender_name, NULL AS sender_username,
                NULL AS media_type, sm.text AS text, sm.sent_at AS ts,
                sm.action, sm.source
            FROM sent_messages sm
        """
        sent_params: list[Any] = []
        if chat_id is not None:
            sent_sql += " WHERE sm.chat_id = ?"
            sent_params.append(chat_id)
        sent_sql += " ORDER BY sm.sent_at ASC, sm.id ASC"

        # ── Исходящие: auto_actions_log (авто-действия) ──
        auto_sql = """
            SELECT
                'auto' AS kind, a.id AS row_id, a.chat_id,
                a.telegram_message_id, NULL AS sender_name, NULL AS sender_username,
                NULL AS media_type,
                COALESCE(a.message_text, '') AS text, a.sent_at AS ts,
                a.action, a.decision
            FROM auto_actions_log a
        """
        auto_params: list[Any] = []
        if chat_id is not None:
            auto_sql += " WHERE a.chat_id = ?"
            auto_params.append(chat_id)
        auto_sql += " ORDER BY a.sent_at ASC, a.id ASC"

        raws = self._query(raw_sql, tuple(raw_params))
        sents = self._query(sent_sql, tuple(sent_params))
        autos = self._query(auto_sql, tuple(auto_params))

        # Индексы капч по сигнатуре (зеркало коллектора — см. collector.py
        # captcha_signature; web остаётся Telegram-free).
        try:
            captcha_by_sig = {
                c["signature"]: c
                for c in self._query("SELECT * FROM captcha_memory")
            }
        except Exception:
            captcha_by_sig = {}

        # Входящие: профиль + капча inline, исходящие: флаг manual/auto.
        rows = []
        for r in raws:
            rows.append(self._enrich_incoming(r, captcha_by_sig))
        for s in sents:
            rows.append(self._enrich_outgoing(s, manual=True))
        for a in autos:
            rows.append(self._enrich_outgoing(a, manual=False))

        rows.sort(key=lambda r: (r["ts"] or "", r["kind"]))

        total = len(rows)
        items = rows[offset:offset + per_page]
        return {
            "items": items,
            "total": total,
            "page": page,
            "total_pages": max(1, (total + per_page - 1) // per_page),
        }

    @staticmethod
    def _captcha_signature(text: str) -> str:
        """Зеркалит ``collectors.dvinchik_collector.captcha_signature``.

        Web-слой не импортирует Telethon, поэтому та же нормализация текста
        (нижний регистр + схлопывание пробелов) продублирована локально:
        сигнатуры в captcha_memory записывает коллектор именно этой функцией.
        """
        import re
        return " ".join((text or "").lower().split())

    def _enrich_incoming(self, r: dict[str, Any], captcha_by_sig: dict) -> dict[str, Any]:
        """Входящий raw → пузырь слева + inline профиль/капча."""
        item = {
            "direction": "in",
            "kind": r["kind"],
            "row_id": r["row_id"],
            "chat_id": r["chat_id"],
            "telegram_message_id": r["telegram_message_id"],
            "sender": r.get("sender_name") or r.get("sender_username") or "Leo",
            "sender_username": r.get("sender_username") or "",
            "text": r.get("text") or "",
            "ts": r.get("ts") or "",
            "media_type": r.get("media_type") or "",
            "profile": None,
            "captcha": None,
            "action_label": "",
        }

        # Капча inline (по сигнатуре текста, как учит коллектор).
        sig = self._captcha_signature(item["text"])
        c = captcha_by_sig.get(sig)
        if c is not None and c.get("signature"):
            item["captcha"] = c
            item["kind"] = "captcha"

        # Профиль inline (raw → profile через profile_messages).
        try:
            prof = self._query_one(
                """
                SELECT p.id, p.name, p.age, p.raw_city, p.normalized_city,
                       p.description, p.status,
                       ad.decision AS ai_decision,
                       hd.decision AS human_decision
                FROM profile_messages pm
                JOIN profiles p ON p.id = pm.profile_id
                LEFT JOIN ai_decisions ad ON ad.id = (
                    SELECT id FROM ai_decisions
                    WHERE profile_id = p.id ORDER BY evaluated_at DESC LIMIT 1
                )
                LEFT JOIN human_decisions hd ON hd.ai_decision_id = ad.id
                WHERE pm.telegram_message_id = ? AND pm.chat_id = ?
                ORDER BY pm.created_at DESC LIMIT 1
                """,
                (item["telegram_message_id"], item["chat_id"]),
            )
        except Exception:
            prof = None
        if prof:
            prof["photos"] = self._query(
                """
                SELECT pm.*, rm.text as raw_text, rm.media_type
                FROM profile_messages pm
                JOIN profiles p ON p.id = pm.profile_id
                LEFT JOIN raw_messages rm ON rm.telegram_message_id = pm.telegram_message_id
                    AND rm.chat_id = pm.chat_id
                WHERE pm.profile_id = ? AND rm.media_type = 'photo'
                ORDER BY pm.created_at ASC
                """,
                (prof["id"],),
            )
            item["profile"] = prof
            item["kind"] = "profile"
        return item

    @staticmethod
    def _enrich_outgoing(r: dict[str, Any], manual: bool) -> dict[str, Any]:
        """Исходящее (sent/auto) → пузырь справа."""
        action = (r.get("action") or "").upper()
        labels = {
            "LIKE": "❤️ LIKE",
            "DISLIKE": "👎 DISLIKE",
            "MESSAGE": "💬 Сообщение",
            "CAPTCHA": "🔑 Ответ на капчу",
        }
        return {
            "direction": "out",
            "kind": "action",
            "row_id": r["row_id"],
            "chat_id": r["chat_id"],
            "telegram_message_id": r["telegram_message_id"],
            "sender": "Я",
            "sender_username": "",
            "text": r.get("text") or "",
            "ts": r.get("ts") or "",
            "media_type": "",
            "profile": None,
            "captcha": None,
            "action": action,
            "action_label": labels.get(action, action),
            "manual": manual,
        }

    def get_chat_signals(self) -> dict[str, Any]:
        """Сигнатура для polling вкладки «Чат».

        Меняется при новой входящей (raw), новой отправке (sent) или
        новом авто-действии (auto_actions_log) — фронт сравнивает
        сигнатуру и перерисовывает ленту, не перезагружая страницу.
        """
        def _max(table: str, col: str) -> Any:
            return self._query_one(
                f"SELECT MAX({col}) AS m FROM {table}"
            )["m"] or ""

        return {
            "max_raw": _max("raw_messages", "id"),
            "max_sent": _max("sent_messages", "id"),
            "max_auto": _max("auto_actions_log", "id"),
            "pending_captchas": len(self.get_pending_captchas(limit=999)),
        }

    def get_captcha_by_id(self, captcha_id: int) -> dict[str, Any] | None:
        return self.get_captcha(int(captcha_id))

    def set_captcha_answer_and_mark(self, captcha_id: int, answer: str) -> str:
        """Сохраняет выученный ответ и сразу отправляет его Leo (inline)."""
        from web.actions import send_captcha_answer_sync
        status = self.set_captcha_answer(captcha_id, answer)
        if not status:
            return "ERROR"
        send_status = send_captcha_answer_sync(answer)
        return "SENT" if send_status == "SENT" else ("KNOWN" if send_status == "DISABLED" else "ERROR")

    # ── Captcha memory (Stage 7.6) ────────────────────────────────

    def get_pending_captchas(self, limit: int = 50) -> list[dict[str, Any]]:
        """Неизвестные капчи, ждущие ответа владельца (свежие сначала)."""
        return self._query(
            """
            SELECT * FROM captcha_memory
            WHERE answer IS NULL
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (int(limit),),
        )

    def get_known_captchas(self, limit: int = 50) -> list[dict[str, Any]]:
        """Капчи с выученными ответами (свежие сначала)."""
        return self._query(
            """
            SELECT * FROM captcha_memory
            WHERE answer IS NOT NULL
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (int(limit),),
        )

    def get_captcha(self, captcha_id: int) -> dict[str, Any] | None:
        return self._query_one(
            "SELECT * FROM captcha_memory WHERE id = ?", (int(captcha_id),)
        )

    def set_captcha_answer(self, captcha_id: int, answer: str) -> int | None:
        """Сохраняет выученный ответ владельца на капчу."""
        now = datetime.now(timezone.utc).isoformat()
        return self._execute(
            """
            UPDATE captcha_memory
            SET answer = ?, answered_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (answer.strip(), now, now, int(captcha_id)),
        )

    def delete_captcha(self, captcha_id: int) -> int | None:
        return self._execute(
            "DELETE FROM captcha_memory WHERE id = ?", (int(captcha_id),)
        )

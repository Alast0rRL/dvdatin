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

    def _execute_rowcount(self, sql: str, params: tuple = ()) -> int:
        """Как ``_execute``, но возвращает число затронутых строк.

        Для UPDATE/DELETE ``lastrowid`` бесполезен (обычно 0), из-за чего
        проверка вида ``if not db.set_...()`` ложно срабатывала как ошибка.
        """
        conn = self._connect()
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.rowcount
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
        account_session: str | None = None,
    ) -> dict[str, Any]:
        """Лента — прямое дублирование Telegram-чата с Leo.

        ОДНО сообщение Telegram = ОДНА строка, в хронологическом порядке (как в
        самом чате): и входящие от Leo, и наши исходящие. Никаких склеек с
        ``sent_messages``/``auto_actions_log`` — это журналы действий, а не
        события чата; их номер указывает на КАРТОЧКУ анкеты, а не на само
        сообщение-реакцию, из-за чего лента съезжала на соседнюю анкету.

        Фильтр по аккаунту-исполнителю — СТРОГИЙ: у каждого аккаунта свой чат с
        Leo в ОДНОМ ``chat_id`` (свои диапазоны message id), поэтому лента
        показывает сообщения РОВНО одного аккаунта (строки без сессии не
        подмешиваются — иначе соседний аккаунт «протекает» в ленту).

        Порядок — по ``telegram_message_id`` (нативный порядок чата Telegram),
        а не по времени сохранения: ``message_date`` у двух аккаунтов идёт из
        разных устройств, а id — это и есть последовательность сообщений Leo.

        Включаем только непустые сообщения (текст или медиа). Пагинация:
        page=1 — последние (как в Telegram), внутри страницы ASC.

        Возвращает {items, total, page, total_pages}.
        """
        page = max(1, page)
        per_page = max(1, per_page)

        where = ["1=1"]
        params: list[Any] = []
        if chat_id is not None:
            where.append("chat_id = ?")
            params.append(chat_id)
        if account_session:
            where.append("account_session = ?")
            params.append(account_session)
        where.append(
            "(TRIM(COALESCE(text, '')) <> '' OR COALESCE(media_type, '') <> '')"
        )
        clause = " AND ".join(where)

        total_row = self._query_one(
            f"SELECT COUNT(*) AS c FROM raw_messages WHERE {clause}",
            tuple(params),
        )
        total = int(total_row["c"] or 0) if total_row else 0

        rows = self._query(
            f"""
            SELECT id, telegram_message_id, chat_id, sender_id, sender_name,
                   sender_username, text, message_date AS ts, media_type,
                   is_outgoing, account_session
            FROM raw_messages
            WHERE {clause}
            ORDER BY telegram_message_id DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            tuple(params) + (per_page, (page - 1) * per_page),
        )
        rows.reverse()  # на странице сверху старые, снизу новые
        return {
            "items": [self._feed_item(r) for r in rows],
            "total": total,
            "page": page,
            "total_pages": max(1, (total + per_page - 1) // per_page),
        }

    def _feed_item(self, r: dict[str, Any]) -> dict[str, Any]:
        """Сырое сообщение → строка ленты (плоское дублирование чата).

        ``profile_id`` нужен только чтобы отдать фото анкеты (маршрут
        ``photos.serve_photo``); ``decision``/``name`` — скрытые атрибуты для
        клиентского фильтра/поиска. Никаких видимых карточек/бейджей.
        """
        outgoing = bool(r.get("is_outgoing"))
        media_type = r.get("media_type") or ""
        item: dict[str, Any] = {
            "direction": "out" if outgoing else "in",
            "kind": "photo" if media_type == "photo" else "message",
            "row_id": r["id"],
            "chat_id": r["chat_id"],
            "telegram_message_id": r["telegram_message_id"],
            "sender": "Я" if outgoing else "Leo",
            "text": r.get("text") or "",
            "ts": r.get("ts") or "",
            "media_type": media_type,
            "profile_id": None,
            "decision": "",
            "name": "",
        }
        if not outgoing:
            prof = self._profile_for_message(
                r["chat_id"], r["telegram_message_id"],
                r.get("account_session") or "",
            )
            if prof:
                item["profile_id"] = prof["id"]
                item["decision"] = prof.get("ai_decision") or ""
                item["name"] = prof.get("name") or ""
        return item

    def _profile_for_message(
        self,
        chat_id: int,
        telegram_message_id: int,
        account_session: str = "",
    ) -> dict[str, Any] | None:
        """Анкета, к которой привязано сообщение (для фото и фильтра).

        ``account_session`` (когда задан) ограничивает поиск тем же аккаунтом:
        у аккаунтов свои пространства message id в одном чате.
        """
        sql = """
            SELECT p.id, p.name, ad.decision AS ai_decision
            FROM profile_messages pm
            JOIN profiles p ON p.id = pm.profile_id
            LEFT JOIN ai_decisions ad ON ad.id = (
                SELECT id FROM ai_decisions
                WHERE profile_id = p.id ORDER BY evaluated_at DESC LIMIT 1
            )
            WHERE pm.telegram_message_id = ? AND pm.chat_id = ?
        """
        params: list[Any] = [telegram_message_id, chat_id]
        if account_session:
            sql += " AND (pm.account_session = ? OR pm.account_session = '')"
            params.append(account_session)
        sql += " ORDER BY pm.created_at DESC LIMIT 1"
        try:
            return self._query_one(sql, tuple(params))
        except Exception:
            return None

    def get_app_state(self, key: str) -> str | None:
        """Значение из app_state (None, если не задано)."""
        row = self._query_one(
            "SELECT value FROM app_state WHERE key = ?", (key,)
        )
        return row["value"] if row else None

    def get_stream_error(self) -> dict[str, Any] | None:
        """Последний сбой автопотока (JSON из app_state) или None.

        Пишется коллектором, когда бот не смог продолжить ленту анкет
        (см. ``STREAM_ERROR_STATE_KEY``). Используется для красного баннера.
        """
        raw = self.get_app_state("stream_error")
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def get_chat_signals(self, account_session: str = "") -> dict[str, Any]:
        """Сигнатура для polling вкладки «Чат».

        Меняется при новой входящей (raw), новой отправке (sent) или
        новом авто-действии (auto_actions_log) — фронт сравнивает
        сигнатуру и перерисовывает ленту, не перезагружая страницу.

        ``account_session``: ``max_raw`` считается ТОЛЬКО по этому аккаунту —
        иначе активность соседнего аккаунта (свой чат с Leo в том же chat_id)
        заставляла бы перерисовывать чужую ленту.
        """
        def _max(table: str, col: str) -> Any:
            return self._query_one(
                f"SELECT MAX({col}) AS m FROM {table}"
            )["m"] or ""

        if account_session:
            row = self._query_one(
                "SELECT MAX(id) AS m FROM raw_messages "
                "WHERE account_session = ?",
                (account_session,),
            )
            max_raw = (row or {}).get("m") or ""
        else:
            max_raw = _max("raw_messages", "id")

        return {
            "max_raw": max_raw,
            "max_sent": _max("sent_messages", "id"),
            "max_auto": _max("auto_actions_log", "id"),
            "pending_captchas": len(self.get_pending_captchas(limit=999)),
            "stream_error_at": (self.get_stream_error() or {}).get("at") or "",
        }

    # ── Debug console (Stage 9) ───────────────────────────────────

    def get_debug_counts(self) -> dict[str, int]:
        """Счётчики ключевых таблиц — сводка для панели отладки."""

        def _count(table: str) -> int:
            row = self._query_one(f"SELECT COUNT(*) AS c FROM {table}")
            return int(row["c"] or 0)

        pending = self._query_one(
            "SELECT COUNT(*) AS c FROM captcha_memory WHERE answer IS NULL"
        )
        return {
            "raw_messages": _count("raw_messages"),
            "sent_messages": _count("sent_messages"),
            "auto_actions": _count("auto_actions_log"),
            "profiles": self.get_profiles_count(),
            "profile_messages": _count("profile_messages"),
            "pending_captchas": int(pending["c"] or 0),
        }

    def get_recent_auto_actions(self, limit: int = 12) -> list[dict[str, Any]]:
        """Последние авто-действия (для отладки «почему лента встала»)."""
        rows = self._query(
            "SELECT id, profile_id, action, decision, chat_id, sent_at, "
            "telegram_message_id, message_text "
            "FROM auto_actions_log ORDER BY sent_at DESC, id DESC LIMIT ?",
            (int(limit),),
        )
        return [dict(r) for r in rows]

    def get_debug_states(self) -> dict[str, Any]:
        """Служебные значения ``app_state`` (сбои, лимиты) для панели отладки."""
        return {
            "stream_error": self.get_stream_error(),
            "likes_exhausted_until": self.get_app_state("likes_exhausted_until") or "",
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

    def set_captcha_answer(self, captcha_id: int, answer: str) -> int:
        """Сохраняет выученный ответ владельца на капчу.

        Возвращает число изменённых строк (>0 — ответ записан). Не ``lastrowid``:
        у UPDATE он 0, из-за чего веб-ответ ошибочно показывал «Ошибка
        сохранения ответа», хотя запись проходила.
        """
        now = datetime.now(timezone.utc).isoformat()
        return self._execute_rowcount(
            """
            UPDATE captcha_memory
            SET answer = ?, answered_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (answer.strip(), now, now, int(captcha_id)),
        )

    def delete_captcha(self, captcha_id: int) -> int:
        return self._execute_rowcount(
            "DELETE FROM captcha_memory WHERE id = ?", (int(captcha_id),)
        )

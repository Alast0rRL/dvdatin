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
        page=1 — последние; на странице свежие СВЕРХУ (под панелью управления).

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
        # Альбомы склеиваются в хронологическом порядке (подпись — у ПЕРВОГО
        # фото альбома), затем лента разворачивается: свежие СВЕРХУ — сразу под
        # панелью управления, более ранние ниже.
        rows.reverse()
        items = self._group_albums([self._feed_item(r) for r in rows])
        items.reverse()
        return {
            "items": items,
            "total": total,
            "page": page,
            "total_pages": max(1, (total + per_page - 1) // per_page),
        }

    @staticmethod
    def _group_albums(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Склеивает анкету-альбом в одну строку, как Telegram.

        Leo шлёт анкету с N фото альбомом: первое сообщение — фото + подпись
        («Имя, возраст, город»), следом N-1 фото без текста. Telegram рисует это
        одной карточкой, а лента рисовала N строк — «безымянные» фото выглядели
        как лишние анкеты. Фото без текста, привязанное к той же анкете, что и
        предыдущая входящая строка, дописывается в её ``photos``.
        """
        out: list[dict[str, Any]] = []
        for item in items:
            prev = out[-1] if out else None
            if (
                prev is not None
                and item["direction"] == "in"
                and prev["direction"] == "in"
                and item["kind"] == "photo"
                and not item["text"]
                and item["profile_id"] is not None
                and item["profile_id"] == prev["profile_id"]
            ):
                prev["photos"].extend(item["photos"])
                continue
            out.append(item)
        return out

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
        # Фото строки (альбом анкеты — несколько, см. _group_albums).
        item["photos"] = (
            [r["telegram_message_id"]]
            if media_type == "photo" and item["profile_id"] is not None
            else []
        )
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

    # ── Улучшенная лента: решения бота с причинами ─────────────────

    def get_decision_feed(
        self,
        page: int = 1,
        per_page: int = 50,
        chat_id: int | None = None,
        account_session: str | None = None,
    ) -> dict[str, Any]:
        """Только анкеты, на которые бот поставил ❤️ или 👎, и почему.

        Источник — ``auto_actions_log`` (LIKE/DISLIKE на конкретную карточку),
        причины — решение ``ai_decisions`` на момент действия (последнее до
        ``sent_at``; нет такого — последнее вообще). Аккаунт — СТРОГО, через
        входящую карточку в ``raw_messages`` (у аккаунтов свои message id).
        Свежие сверху. Возвращает {items, total, page, total_pages}.
        """
        from services.reason_labels import describe_reasons

        page = max(1, page)
        per_page = max(1, per_page)
        where = ["a.action IN ('LIKE', 'DISLIKE')"]
        params: list[Any] = []
        if chat_id is not None:
            where.append("a.chat_id = ?")
            params.append(chat_id)
        if account_session:
            where.append("r.id IS NOT NULL")
        clause = " AND ".join(where)
        join_params: list[Any] = [account_session or ""]
        base = f"""
            FROM auto_actions_log a
            JOIN profiles p ON p.id = a.profile_id
            LEFT JOIN raw_messages r
                ON r.chat_id = a.chat_id
                AND r.telegram_message_id = a.telegram_message_id
                AND r.is_outgoing = 0
                AND r.account_session = ?
            WHERE {clause}
        """
        total_row = self._query_one(
            f"SELECT COUNT(*) AS c {base}", tuple(join_params + params)
        )
        total = int(total_row["c"] or 0) if total_row else 0
        rows = self._query(
            f"""
            SELECT a.id, a.profile_id, a.action, a.decision AS action_decision,
                   a.chat_id, a.sent_at, a.telegram_message_id,
                   p.name, p.age, p.normalized_city, p.raw_city, p.description,
                   r.text AS card_text, r.media_type AS card_media,
                   (SELECT m.message_text FROM auto_actions_log m
                     WHERE m.chat_id = a.chat_id
                       AND m.telegram_message_id = a.telegram_message_id
                       AND m.action = 'MESSAGE' LIMIT 1) AS message_text,
                   COALESCE(
                       (SELECT ad.reasons FROM ai_decisions ad
                         WHERE ad.profile_id = a.profile_id
                           AND ad.evaluated_at <= a.sent_at
                         ORDER BY ad.evaluated_at DESC LIMIT 1),
                       (SELECT ad.reasons FROM ai_decisions ad
                         WHERE ad.profile_id = a.profile_id
                         ORDER BY ad.evaluated_at DESC LIMIT 1),
                       '[]'
                   ) AS reasons
            {base}
            ORDER BY a.sent_at DESC, a.id DESC
            LIMIT ? OFFSET ?
            """,
            tuple(join_params + params) + (per_page, (page - 1) * per_page),
        )
        items: list[dict[str, Any]] = []
        for r in rows:
            try:
                raw_reasons = json.loads(r.get("reasons") or "[]")
            except Exception:
                raw_reasons = []
            card_text = r.get("card_text") or ""
            reasons = describe_reasons(
                raw_reasons if isinstance(raw_reasons, list) else [],
                card_text or None,
            )
            if not reasons and r.get("action_decision") in ("REJECT", "REVIEW"):
                # Фильтр-уровень без записанного скоринга.
                reasons = ["Не прошла фильтр (возраст/город)"]
            items.append({
                "kind": "decision",
                "action": r["action"],
                "decision": r["action"],
                "profile_id": r["profile_id"],
                "name": r.get("name") or "",
                "age": r.get("age"),
                "city": r.get("normalized_city") or r.get("raw_city") or "",
                "text": card_text or r.get("description") or "",
                "ts": r.get("sent_at") or "",
                "message_text": r.get("message_text") or "",
                "reasons": reasons,
                "photos": self._decision_photos(
                    r, account_session or "",
                ),
            })
        return {
            "items": items,
            "total": total,
            "page": page,
            "total_pages": max(1, (total + per_page - 1) // per_page),
        }

    def _decision_photos(
        self, r: dict[str, Any], account_session: str, limit: int = 6,
    ) -> list[int]:
        """Фото карточки: сама карточка + альбом сразу за ней (та же анкета)."""
        tm = r.get("telegram_message_id")
        if tm is None:
            return []
        sql = """
            SELECT DISTINCT rm.telegram_message_id AS tm
            FROM raw_messages rm
            JOIN profile_messages pm
                ON pm.telegram_message_id = rm.telegram_message_id
                AND pm.chat_id = rm.chat_id
                AND pm.profile_id = ?
            WHERE rm.chat_id = ? AND rm.is_outgoing = 0
              AND rm.media_type = 'photo'
              AND rm.telegram_message_id BETWEEN ? AND ?
        """
        params: list[Any] = [r["profile_id"], r["chat_id"], tm, tm + 10]
        if account_session:
            sql += " AND rm.account_session = ?"
            params.append(account_session)
        sql += " ORDER BY rm.telegram_message_id LIMIT ?"
        params.append(limit)
        try:
            return [row["tm"] for row in self._query(sql, tuple(params))]
        except Exception:
            return []

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

    def get_bot_error(self) -> dict[str, Any] | None:
        """Последняя ошибка бота (collectors/error_reporter.py) или None.

        JSON: ``at``, ``where``, ``message``, ``count`` — для всплывающего окна.
        """
        raw = self.get_app_state("bot_error")
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def get_chat_signals(
        self, account_session: str = "", chat_id: int | None = None,
    ) -> dict[str, Any]:
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

        where, params = [], []
        if account_session:
            where.append("account_session = ?")
            params.append(account_session)
        if chat_id is not None:
            # Только чат Leo: активность в других чатах не дёргает ленту.
            where.append("chat_id = ?")
            params.append(chat_id)
        if where:
            row = self._query_one(
                "SELECT MAX(id) AS m FROM raw_messages WHERE "
                + " AND ".join(where),
                tuple(params),
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
            "bot_error_at": (self.get_bot_error() or {}).get("at") or "",
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
            # Лимит ❤️ — на аккаунт: {"dvai_2": "<ts>", …} (legacy-ключ — "").
            "likes_exhausted_until": {
                (r["key"].partition(":")[2] or ""): r["value"]
                for r in self._query(
                    "SELECT key, value FROM app_state "
                    "WHERE key LIKE 'likes_exhausted_until%'"
                )
            },
        }

    def get_captcha_by_id(self, captcha_id: int) -> dict[str, Any] | None:
        return self.get_captcha(int(captcha_id))

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

    # ── Разметка внешности (/rate) ────────────────────────────────

    #: Допустимые оценки фото. VICE — на фото сигарета/вейп/алкоголь (для
    #: владельца это 👎; метки учат детектор пороков на фото).
    PHOTO_RATINGS = ("LIKE", "DISLIKE", "SKIP", "VICE")

    #: Сколько фото анкеты показываем на странице разметки.
    RATE_MAX_PHOTOS = 4

    # Фото анкеты — сообщения из profile_messages, которые в raw_messages
    # помечены как фото. Только с известным аккаунтом-получателем: фото
    # качается строго им (иначе чужой message_id → фото другой анкеты).
    # Условие на rm.account_session обязательно: индекс idx_raw_unique
    # начинается с account_session, и без него SQLite сканировал всю
    # raw_messages на каждую строку (прод: 7.9 с на запрос вместо 0.02 с —
    # между анкетами висело «Загружаю…»). '' — legacy-строки до разметки.
    _RATE_PHOTO_SQL = """
        FROM profile_messages pm
        WHERE pm.account_session <> ''
          AND EXISTS (
              SELECT 1 FROM raw_messages rm
              WHERE rm.account_session IN (pm.account_session, '')
                AND rm.chat_id = pm.chat_id
                AND rm.telegram_message_id = pm.telegram_message_id
                AND rm.is_outgoing = 0
                AND rm.media_type = 'photo'
          )
    """

    def save_photo_rating(self, profile_id: int, rating: str) -> None:
        """Оценка внешности анкеты; повторная перезаписывает прежнюю."""
        if rating not in self.PHOTO_RATINGS:
            raise ValueError(f"неизвестная оценка {rating!r}")
        self._execute(
            "INSERT OR REPLACE INTO photo_ratings (profile_id, rating, created_at) "
            "VALUES (?, ?, ?)",
            (int(profile_id), rating, datetime.now(timezone.utc).isoformat()),
        )

    def delete_photo_rating(self, profile_id: int) -> int:
        """Отмена оценки (кнопка «Назад»)."""
        return self._execute_rowcount(
            "DELETE FROM photo_ratings WHERE profile_id = ?", (int(profile_id),)
        )

    def save_photo_item_rating(
        self, profile_id: int, message_id: int, rating: str, account_session: str = "",
    ) -> None:
        """Оценка ОДНОГО фото анкеты; повторная перезаписывает прежнюю."""
        if rating not in self.PHOTO_RATINGS:
            raise ValueError(f"неизвестная оценка {rating!r}")
        self._execute(
            "INSERT OR REPLACE INTO photo_item_ratings "
            "(profile_id, telegram_message_id, account_session, rating, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (int(profile_id), int(message_id), account_session or "", rating,
             datetime.now(timezone.utc).isoformat()),
        )

    def delete_photo_item_rating(self, profile_id: int, message_id: int) -> int:
        """Отмена оценки фото (кнопка «Назад»)."""
        self._rate_done_profiles().discard(int(profile_id))
        return self._execute_rowcount(
            "DELETE FROM photo_item_ratings WHERE profile_id = ? AND telegram_message_id = ?",
            (int(profile_id), int(message_id)),
        )

    def get_photo_rating_stats(self) -> dict[str, int]:
        """Счётчики разметки: оценки фото (по видам), старые оценки анкет
        целиком и сколько анкет с фото ещё не тронуто."""
        stats = {r: 0 for r in self.PHOTO_RATINGS}
        for row in self._query(
            "SELECT rating, COUNT(*) AS cnt FROM photo_item_ratings GROUP BY rating"
        ):
            stats[row["rating"]] = row["cnt"]
        stats["rated"] = sum(stats[r] for r in self.PHOTO_RATINGS)
        old = self._query_one("SELECT COUNT(*) AS cnt FROM photo_ratings")
        stats["profiles_rated"] = old["cnt"] if old else 0
        left = self._query_one(
            "SELECT COUNT(DISTINCT pm.profile_id) AS cnt " + self._RATE_PHOTO_SQL
            + " AND pm.profile_id NOT IN (SELECT profile_id FROM photo_ratings)"
            " AND pm.profile_id NOT IN (SELECT profile_id FROM photo_item_ratings)"
        )
        stats["left"] = left["cnt"] if left else 0
        return stats

    def get_rating_queue(
        self,
        limit: int = 3,
        exclude: list[int] | None = None,
        prefer: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Следующие неоценённые анкеты с фото.

        Сначала — анкеты, в которых модель вкуса сомневается больше всего
        (последний score в photo_scores ближе всего к 0.5): на них разметка
        учит модель сильнее всего. Каждая RATE_EXPLORE_EVERY-я — из обычного
        перемешанного порядка (хэш id), чтобы метки не перекосило в сторону
        «пограничных» анкет. ``prefer`` — анкеты с фото в кэше на диске (в
        обычном порядке идут раньше: не дёргаем Telegram). ``exclude`` — уже
        стоящие в очереди браузера.
        """
        exclude = [int(x) for x in (exclude or [])][:200]
        ex_sql = ""
        params: list[Any] = []
        if exclude:
            ex_sql = f" AND pm.profile_id NOT IN ({','.join('?' * len(exclude))})"
            params += exclude
        rows = self._query(
            "SELECT DISTINCT pm.profile_id AS pid " + self._RATE_PHOTO_SQL
            + " AND pm.profile_id NOT IN (SELECT profile_id FROM photo_ratings)"
            + ex_sql
            + " ORDER BY (pm.profile_id * 2654435761) % 1000003",
            tuple(params),
        )
        done = self._rate_done_profiles()
        pids = [r["pid"] for r in rows if r["pid"] not in done]
        if prefer:
            pids.sort(key=lambda p: p not in prefer)  # стабильная сортировка
        scores = self._latest_photo_scores()
        uncertain = sorted(
            (p for p in pids if p in scores), key=lambda p: abs(scores[p] - 0.5),
        )
        # «Случайные» — обычный перемешанный порядок ВСЕХ неоценённых (после
        # фонового досчёта вердикт есть у всех, поэтому брать только анкеты
        # без вердикта нельзя — разведка бы пропала).
        explore = list(pids)
        ordered: list[int] = []
        explore_slots: set[int] = set()
        taken: set[int] = set()
        want = max(1, int(limit))
        rated = self._rated_photo_ids()
        out: list[dict[str, Any]] = []
        while len(out) < want and len(taken) < len(pids):
            take_explore = (len(ordered) + 1) % self.RATE_EXPLORE_EVERY == 0
            source = explore if (take_explore or not uncertain) else uncertain
            while source and source[0] in taken:
                source.pop(0)
            if not source:
                source = uncertain if source is explore else explore
                while source and source[0] in taken:
                    source.pop(0)
                if not source:
                    break
            pid = source.pop(0)
            if source is explore:
                explore_slots.add(pid)
            taken.add(pid)
            ordered.append(pid)
            # Оценка по одному фото: отдаём только ещё не оценённые фото анкеты
            # (вкладку могли закрыть посреди анкеты); все оценены — дальше.
            photos = [
                p for p in self._rating_photos(pid)
                if (pid, p["message_id"]) not in rated
            ]
            if not photos:
                done.add(pid)  # все фото оценены — больше не проверяем
                continue
            out.append({
                "profile_id": pid,
                "photos": photos,
                "score": scores.get(pid),
                "uncertain": pid in scores and pid not in explore_slots,
            })
        if len(out) < want:
            out += self._rerate_queue(want - len(out), exclude, done, rated, scores)
        return out

    def _rerate_queue(
        self,
        want: int,
        exclude: list[int],
        done: set[int],
        rated: set[tuple[int, int]],
        scores: dict[int, float],
    ) -> list[dict[str, Any]]:
        """Новые анкеты кончились — старые, оценённые целиком (``photo_ratings``),
        у которых 2+ фото: переоценка по одному фото (одно фото — старая оценка
        и так про него). Сомнительные для модели — первыми."""
        skip = set(exclude) | done
        pids = [
            r["profile_id"] for r in self._query("SELECT profile_id FROM photo_ratings")
            if r["profile_id"] not in skip
        ]
        pids.sort(key=lambda p: abs(scores.get(p, 0.5) - 0.5))
        out: list[dict[str, Any]] = []
        for pid in pids:
            if len(out) >= want:
                break
            all_photos = self._rating_photos(pid)
            photos = [p for p in all_photos if (pid, p["message_id"]) not in rated]
            if len(all_photos) < self.RERATE_MIN_PHOTOS or not photos:
                done.add(pid)  # нечего переоценивать — больше не проверяем
                continue
            out.append({
                "profile_id": pid,
                "photos": photos,
                "score": scores.get(pid),
                "uncertain": False,
                "rerate": True,
            })
        return out

    #: Каждая N-я анкета очереди /rate — из обычного порядка, а не «сомнительных».
    RATE_EXPLORE_EVERY = 5

    #: Старую анкету (оценена целиком) переоцениваем по фото, если фото столько и больше.
    RERATE_MIN_PHOTOS = 2

    def _rate_done_profiles(self) -> set[int]:
        """Анкеты, у которых оценены все фото (кэш в памяти процесса: иначе
        очередь на каждый запрос заново перебирала бы их фото)."""
        done = getattr(self, "_rate_done", None)
        if done is None:
            done = self._rate_done = set()
        return done

    def _rated_photo_ids(self) -> set[tuple[int, int]]:
        """(profile_id, message_id) уже оценённых фото."""
        try:
            rows = self._query(
                "SELECT profile_id, telegram_message_id FROM photo_item_ratings"
            )
        except sqlite3.Error:
            return set()
        return {(r["profile_id"], r["telegram_message_id"]) for r in rows}

    def _latest_photo_scores(self) -> dict[int, float]:
        """Последний теневой score модели вкуса по каждой анкете."""
        try:
            rows = self._query(
                "SELECT profile_id, score FROM photo_scores WHERE id IN "
                "(SELECT MAX(id) FROM photo_scores GROUP BY profile_id)"
            )
        except sqlite3.Error:
            return {}
        return {r["profile_id"]: float(r["score"]) for r in rows}

    #: Альбом одного показа анкеты — сообщения с номерами подряд; больший
    #: разрыв означает предыдущий показ (Leo показывает анкету повторно, и
    #: фото дублировались бы).
    RATE_ALBUM_GAP = 2

    def _rating_photos(self, profile_id: int) -> list[dict[str, Any]]:
        """Фото ПОСЛЕДНЕГО показа анкеты с одного аккаунта, до RATE_MAX_PHOTOS."""
        rows = self._query(
            "SELECT pm.telegram_message_id AS message_id, pm.account_session "
            + self._RATE_PHOTO_SQL
            + " AND pm.profile_id = ? ORDER BY pm.telegram_message_id",
            (int(profile_id),),
        )
        if not rows:
            return []
        session_name = rows[-1]["account_session"]
        same = [r for r in rows if r["account_session"] == session_name]
        start = len(same) - 1
        while start > 0 and (
            same[start]["message_id"] - same[start - 1]["message_id"] <= self.RATE_ALBUM_GAP
        ):
            start -= 1
        # Первые фото альбома — те же, что смотрит бот (_card_photos) и досчёт.
        return same[start:][: self.RATE_MAX_PHOTOS]

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


# Насколько скоро после карточки анкеты приходит наша реакция — Leo держит
# одну активную карточку, а мы жмём кнопку сразу. Нужен на случай, когда в
# аннотации нет profile_id (см. _profile_acted).
ACTION_TS_WINDOW_SEC = 300


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
        """Собирает хронологическую ленту «Чат» — сырой поток Leo как TG-chat.

        ОДНО сообщение Telegram = ОДНА строка ленты. Коллектор пишет каждое
        сообщение чата (и входящее от Leo, и наше исходящее) в ``raw_messages`` —
        это и есть хребет ленты, уникальный по ``(chat_id, telegram_message_id)``.
        ``sent_messages`` и ``auto_actions_log`` — не отдельные события, а
        АННОТАЦИИ исходящих: по ``telegram_message_id`` они приклеиваются к
        строке-хребту (значок «👎 DISLIKE», «вручную/авто», текст «Берем)»).

        Без склейки одна анкета давала три строки: карточка профиля, голый
        эмодзи «👎» из raw_messages и «👎 DISLIKE вручную» из sent_messages —
        при том что эмодзи и sent-строка были ОДНИМ и тем же сообщением
        Telegram. Реклейм профиля разводит ещё и «действие уже есть» → AI-бейдж
        в карточке скрывается (см. ``_enrich_message``).

        Enrichment строки-входящей (profile/captcha) — как раньше:
          * профиль — raw → profile через profile_messages join, с фото
            (``media_type='photo'``), AI-решением и человеческим решением;
          * капча — по сигнатуре текста из ``captcha_memory``
            (pending → форма ответа inline, known → выученный ответ).

        Строки-исходящие без пары в ``raw_messages`` (RAW не сохранился,
        сырые записи веб открыл раньше миграции) добавляются отдельными
        строками — лента не теряет действия.

        ПАГИНАЦИЯ «С КОНЦА»: page=1 — самые свежие сообщения (как в Telegram),
        page=2 — предыдущие и т.д. Внутри страницы хронологически ASC
        (сверху старые, снизу новые).

        Возвращает {items, total, page, total_pages}.
        """
        page = max(1, page)
        per_page = max(1, per_page)

        # ── Шаг 1: страница ключей (chat_id, telegram_message_id) ──
        # Объединение ключей ПАГИНИРУЕТСЯ В SQL, а не собирается в Python:
        # если брать «последние N» из каждой таблицы отдельно и потом
        # склеивать, окна не совпадают и на соседних страницах сообщения
        # повторяются/пропускаются (проверено на реальной базе: 578 дублей).
        total = self._chat_total(chat_id)
        keys = self._feed_keys(chat_id, per_page, (page - 1) * per_page)
        if not keys:
            return {
                "items": [], "total": total, "page": page,
                "total_pages": max(1, (total + per_page - 1) // per_page),
            }

        in_keys = [(c, t) for c, t, d in keys if d == "in"]
        out_keys = [(c, t) for c, t, d in keys if d == "out"]

        def _clause(items: list[tuple[Any, Any]]) -> tuple[str | None, tuple]:
            """``(chat_id, telegram_message_id) IN (...)`` для списка ключей.

            ``None`` — ключей нет, запрос по ветке надо пропустить целиком:
            заглушка ``(NULL, NULL)`` невалидна для row-value ``IN`` в SQLite.
            """
            if not items:
                return None, ()
            clause = "(" + ",".join("(?,?)" for _ in items) + ")"
            return clause, tuple(v for pair in items for v in pair)

        in_clause, in_params = _clause(in_keys)
        out_clause, out_params = _clause(out_keys)

        # ── Шаг 2: сырые записи только этой страницы ──
        # Запросов ДВА, а не один: ключ строки — (chat, tm, направление), и
        # номер входящего сообщения Leo может совпасть с номером исходящего
        # нашего (у аккаунтов свои диапазоны id). Один запрос по (chat, tm)
        # возвращал бы оба и на соседних страницах строки дублировались бы.
        raw_sql = """
            SELECT
                'raw' AS kind, rm.id AS row_id, rm.chat_id, rm.telegram_message_id,
                rm.sender_id, rm.sender_name, rm.sender_username,
                rm.text AS text, rm.message_date AS ts, rm.media_type AS media_type,
                rm.is_outgoing AS is_outgoing
            FROM raw_messages rm
            WHERE rm.is_outgoing = {is_out}
              AND (rm.chat_id, rm.telegram_message_id) IN {clause}
            ORDER BY rm.id DESC
        """
        raws: list[dict[str, Any]] = []
        for is_out, clause, params in (
            (0, in_clause, in_params), (1, out_clause, out_params)
        ):
            if clause is None:
                continue
            raws += self._query(
                raw_sql.format(is_out=is_out, clause=clause), params
            )

        # ── Шаг 3: аннотации исходящих этой же страницы ──
        sent_sql = """
            SELECT
                'sent' AS kind, sm.id AS row_id, sm.chat_id,
                sm.telegram_message_id, sm.profile_id, NULL AS decision,
                NULL AS media_type, sm.text AS text, sm.sent_at AS ts,
                sm.action AS action, sm.source AS source
            FROM sent_messages sm
            WHERE (sm.chat_id, sm.telegram_message_id) IN {clause}
            ORDER BY sm.id DESC
        """
        auto_sql = """
            SELECT
                'auto' AS kind, a.id AS row_id, a.chat_id,
                a.telegram_message_id, a.profile_id, a.decision AS decision,
                NULL AS media_type,
                COALESCE(a.message_text, '') AS text, a.sent_at AS ts,
                a.action AS action, NULL AS source
            FROM auto_actions_log a
            WHERE (a.chat_id, a.telegram_message_id) IN {clause}
            ORDER BY a.id DESC
        """
        sents: list[dict[str, Any]] = []
        autos: list[dict[str, Any]] = []
        if out_clause is not None:
            sents = self._query(sent_sql.format(clause=out_clause), out_params)
            autos = self._query(auto_sql.format(clause=out_clause), out_params)

        # Аннотации по (chat_id, telegram_message_id). Авто важнее ручной:
        # у него есть decision (что решил AI) и message_text («Берем)»).
        marks: dict[tuple[Any, Any], dict[str, Any]] = {}
        for s in sents:
            marks[(s["chat_id"], s["telegram_message_id"])] = s
        for a in autos:
            marks[(a["chat_id"], a["telegram_message_id"])] = a

        # Профили, по которым в этом окне уже есть действие — их AI-бейдж
        # в карточке скрываем, иначе рядом с «👎 DISLIKE» стоит второй 👎.
        acted_profile_ids = {
            m["profile_id"] for m in marks.values() if m.get("profile_id")
        }
        # Аннотации этой страницы (chat_id, время) — для признака «реакция
        # почти сразу после карточки», когда profile_id в аннотации нет
        # (см. _profile_acted).
        action_marks = [
            (m["chat_id"], m.get("ts")) for m in marks.values()
        ]

        # Индексы капч по сигнатуре (зеркало коллектора — см. collector.py
        # captcha_signature; web остаётся Telegram-free).
        try:
            captcha_by_sig = {
                c["signature"]: c
                for c in self._query("SELECT * FROM captcha_memory")
            }
        except Exception:
            captcha_by_sig = {}

        rows = []
        seen_keys: set[tuple[Any, Any, str]] = set()
        for r in raws:
            direction = "out" if r.get("is_outgoing") else "in"
            key = (r["chat_id"], r["telegram_message_id"], direction)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            # Аннотация — только к ИСХОДЯЩЕМУ: tm входящих Leo и tm наших
            # сообщений живут в разных пространствах id (см. _feed_keys).
            mark = marks.get((r["chat_id"], r["telegram_message_id"])) \
                if direction == "out" else None
            rows.append(
                self._enrich_message(
                    r, mark, captcha_by_sig, acted_profile_ids, action_marks
                )
            )
        # Исходящие без сырой записи (не сохранился RAW / до миграции) —
        # показываем отдельной строкой, лента не теряет действие.
        for mkey, mark in marks.items():
            key = (mkey[0], mkey[1], "out")
            if key in seen_keys:
                continue
            seen_keys.add(key)
            rows.append(
                self._enrich_outgoing(mark, manual=mark["kind"] == "sent")
            )

        # Хронология внутри страницы: ASC (сверху старые, снизу новые).
        rows.sort(key=lambda r: (r["ts"] or "", r.get("row_id") or 0))
        return {
            "items": rows,
            "total": total,
            "page": page,
            "total_pages": max(1, (total + per_page - 1) // per_page),
        }

    def _feed_keys(
        self, chat_id: int | None, limit: int, offset: int,
    ) -> list[tuple[Any, Any, str]]:
        """Ключи сообщений ленты — одна строка на ОДНО сообщение Telegram.

        Ключ — ``(chat_id, telegram_message_id, направление)``, и направление
        в ключе обязательно: у каждого аккаунта Telegram СВОЙ диапазон id
        сообщений в одном чате, поэтому номер исходящего сообщения
        пересекается с номерами входящих от Leo. На реальной базе входящие
        и исходящие не пересекаются вообще (1729 против 799) — склеивать их
        по одному лишь ``telegram_message_id`` нельзя, иначе наш ❤️ с id
        708197 склеится с карточкой анкеты Leo с тем же номером.

        UNION ALL четырёх ветвей, группировка по ключу:
          * входящие ``raw_messages`` (is_outgoing=0) — карточки, тексты Leo;
          * исходящие ``raw_messages`` (is_outgoing=1) — всё, что отправили мы;
          * ``sent_messages`` / ``auto_actions_log`` — аннотации исходящих;
          пропускаются пустые сообщения (фото ``MEDIA_ONLY``, кнопки меню) —
            показывать нечего, фото уже есть внутри карточки анкеты.

        Порядок — по времени (``message_date`` / ``sent_at``), а не по id:
        ветви лежат в разных пространствах id, и сортировка по номеру была бы
        бессмысленной. Пагинация делается здесь, в SQL: если брать «последние
        N» из каждой таблицы отдельно, окна не совпадают и на соседних
        страницах сообщения повторяются (проверено: 578 дублей).
        """
        where = " AND {a}.chat_id = ?" if chat_id is not None else ""
        params: tuple = (chat_id,) if chat_id is not None else ()
        branches = self._feed_branches(where)
        sql = f"""
            SELECT chat_id, telegram_message_id, direction, MAX(ts) AS ts FROM (
                {branches}
            ) u
            GROUP BY chat_id, telegram_message_id, direction
            ORDER BY ts DESC, telegram_message_id DESC, chat_id DESC, direction DESC
            LIMIT ? OFFSET ?
        """
        try:
            rows = self._query(sql, params * 4 + (limit, offset))
        except Exception:
            return []
        return [
            (r["chat_id"], r["telegram_message_id"], r["direction"])
            for r in rows
        ]

    def _chat_total(self, chat_id: int | None) -> int:
        """Число строк ленты = |объединение ключей сырых и аннотаций|.

        Считается по тем же правилам, что и ``_feed_keys`` (в т.ч. с
        направлением и без пустых сообщений), иначе ``total`` разошёлся бы
        с пагинацией.
        """
        where = " AND {a}.chat_id = ?" if chat_id is not None else ""
        params: tuple = (chat_id,) if chat_id is not None else ()
        # GROUP BY обязателен: ветви UNION ALL пересекаются (сырое исходящее
        # + его же аннотация в sent_messages), и без склейки по ключу total
        # завышался на число аннотаций.
        sql = f"""
            SELECT COUNT(*) AS c FROM (
                SELECT chat_id, telegram_message_id, direction, MAX(ts) AS ts
                FROM (
                    {self._feed_branches(where)}
                ) u
                GROUP BY chat_id, telegram_message_id, direction
            ) g
        """
        try:
            row = self._query_one(sql, params * 4)
            return int(row["c"] or 0)
        except Exception:
            return 0

    @staticmethod
    def _feed_branches(where: str) -> str:
        """Четыре ветви ленты (сырое входящее/исходящее + аннотации).

        ``where`` — шаблон ``" AND {a}.chat_id = ?"`` (или пустой), чтобы
        фильтр по чату подставлялся во все ветви с правильным алиасом.
        """
        return f"""
                SELECT rm.chat_id AS chat_id,
                       rm.telegram_message_id AS telegram_message_id,
                       'in' AS direction, rm.message_date AS ts
                FROM raw_messages rm
                WHERE rm.telegram_message_id IS NOT NULL
                  AND rm.is_outgoing = 0
                  AND TRIM(COALESCE(rm.text, '')) <> ''{where.format(a="rm")}
                UNION ALL
                SELECT rm.chat_id, rm.telegram_message_id,
                       'out', rm.message_date
                FROM raw_messages rm
                WHERE rm.telegram_message_id IS NOT NULL
                  AND rm.is_outgoing = 1
                  AND TRIM(COALESCE(rm.text, '')) <> ''{where.format(a="rm")}
                UNION ALL
                SELECT sm.chat_id, sm.telegram_message_id,
                       'out', sm.sent_at
                FROM sent_messages sm
                WHERE sm.telegram_message_id IS NOT NULL{where.format(a="sm")}
                UNION ALL
                SELECT a.chat_id, a.telegram_message_id,
                       'out', a.sent_at
                FROM auto_actions_log a
                WHERE a.telegram_message_id IS NOT NULL{where.format(a="a")}
        """

    @staticmethod
    def _captcha_signature(text: str) -> str:
        """Зеркалит ``collectors.dvinchik_collector.captcha_signature``.

        Web-слой не импортирует Telethon, поэтому та же нормализация текста
        (нижний регистр + схлопывание пробелов) продублирована локально:
        сигнатуры в captcha_memory записывает коллектор именно этой функцией.
        """
        import re
        return " ".join((text or "").lower().split())

    def _enrich_message(
        self,
        r: dict[str, Any],
        mark: dict[str, Any] | None,
        captcha_by_sig: dict,
        acted_profile_ids: set[Any] | None = None,
        action_marks: list[tuple[Any, Any]] | None = None,
    ) -> dict[str, Any]:
        """Строка-хребет ``raw_messages`` → элемент ленты.

        Наше исходящее (is_outgoing) рисуется как действие справа — даже если
        аннотации (sent/auto) нет: коллектор пишет в raw ВСЁ, что мы отправили,
        включая кнопки меню Leo. Иначе наш же 👎 выглядел бы как сообщение
        от бота (sender «Leo», стрелка «входящее»).
        """
        outgoing = bool(r.get("is_outgoing"))
        item = {
            "direction": "out" if outgoing else "in",
            "kind": r["kind"],
            "row_id": r["row_id"],
            "chat_id": r["chat_id"],
            "telegram_message_id": r["telegram_message_id"],
            "sender": "Я" if outgoing else (
                r.get("sender_name") or r.get("sender_username") or "Leo"
            ),
            "sender_username": r.get("sender_username") or "",
            "text": r.get("text") or "",
            "ts": r.get("ts") or "",
            "media_type": r.get("media_type") or "",
            "profile": None,
            "captcha": None,
            "action": "",
            "action_label": "",
            "manual": None,
        }

        if outgoing:
            action = (mark or {}).get("action") or ""
            item["kind"] = "action"
            item["action"] = str(action).upper()
            item["action_label"] = self._action_label(item["action"])
            # Аннотация знает, вручную ли действие; без неё — не наше решение,
            # а обычная отправка (кнопка меню Leo и т.п.).
            item["manual"] = None if mark is None else (mark["kind"] == "sent")
            if mark is not None and mark.get("text"):
                item["text"] = mark["text"]
            item["decision"] = (mark or {}).get("decision") or ""
            return item

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
            # Действие по анкете уже отработано отдельной строкой ниже по
            # ленте — второй 👎 в карточке не рисуем (data-decision для
            # фильтра при этом остаётся всегда).
            prof["acted"] = self._profile_acted(
                prof["id"], item, acted_profile_ids, action_marks
            )
            item["profile"] = prof
            item["kind"] = "profile"
        return item

    @staticmethod
    def _profile_acted(
        profile_id: Any,
        item: dict[str, Any],
        acted_profile_ids: set[Any] | None,
        action_marks: list[tuple[Any, Any]] | None,
    ) -> bool:
        """Есть ли по этой анкете действие (тогда AI-бейдж в карточке лишний).

        Два признака, потому что ``profile_id`` в аннотации есть не всегда:
        часть ручных отправок записана с ``profile_id = NULL`` (на реальной
        базе 165 из 597 — профиль не удалось резолвить в момент записи).

        Второй признак — ВРЕМЯ, а не номера сообщений: у каждого аккаунта
        Telegram свой диапазон message id в одном чате, поэтому «реакция в
        пределах N id после карточки» смысла не имеет. Leo держит одну
        активную карточку, и мы реагируем на неё в течение секунд — этого
        достаточно.
        """
        if acted_profile_ids and profile_id in acted_profile_ids:
            return True
        card_ts = SyncDB._parse_ts(item.get("ts"))
        chat_id = item.get("chat_id")
        if card_ts is None or not chat_id:
            return False
        window = timedelta(seconds=ACTION_TS_WINDOW_SEC)
        for mark_chat, mark_ts in action_marks or ():
            if mark_chat != chat_id:
                continue
            when = SyncDB._parse_ts(mark_ts)
            if when is not None and timedelta(0) < when - card_ts <= window:
                return True
        return False

    @staticmethod
    def _parse_ts(value: Any) -> datetime | None:
        """ISO-строка времени → datetime с приведением к UTC.

        ``raw_messages.message_date`` без зоны, а ``sent_messages.sent_at``
        с ``+00:00`` — сравнивать их наивно нельзя (TypeError либо разные
        часы), поэтому naive считаем UTC.
        """
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _action_label(action: str) -> str:
        """Код действия → подпись бейджа в ленте."""
        return {
            "LIKE": "❤️ LIKE",
            "DISLIKE": "👎 DISLIKE",
            "MESSAGE": "💬 Сообщение",
            "CAPTCHA": "🔑 Ответ на капчу",
        }.get(action, action)

    @classmethod
    def _enrich_outgoing(cls, r: dict[str, Any], manual: bool) -> dict[str, Any]:
        """Аннотация исходящего БЕЗ сырой записи → строка ленты."""
        action = (r.get("action") or "").upper()
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
            "action_label": cls._action_label(action),
            "manual": manual,
            "decision": r.get("decision") or "",
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

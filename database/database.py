# Менеджер подключения к SQLite через aiosqlite.
# Автоматически создаёт файл БД, таблицы, предоставляет async-интерфейс.

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
from loguru import logger

DB_DIR = Path("data")
DB_PATH = DB_DIR / "database.db"

# W4: ограниченный retry при транзитных ошибках БД во время сохранения RAW.
# Транзитные сбои (напр. "database is locked") повторяются с нарастающим
# backoff. Итоговая задержка при 3 попытках <= 0.05+0.1+0.2 = 0.35с, чтобы
# не блокировать Telegram-хендлер надолго. Бесконечного retry нет.
_RAW_SAVE_MAX_RETRIES = 3
_RAW_SAVE_BACKOFF_BASE = 0.05

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_message_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    sender_id INTEGER NOT NULL,
    sender_username TEXT DEFAULT '',
    sender_name TEXT DEFAULT '',
    message_date TEXT NOT NULL,
    text TEXT DEFAULT '',
    raw_entities TEXT DEFAULT '[]',
    reply_markup TEXT DEFAULT '[]',
    media_type TEXT DEFAULT '',
    reply_to_message_id INTEGER,
    received_at TEXT NOT NULL,
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    age INTEGER NOT NULL,
    raw_city TEXT DEFAULT '',
    normalized_city TEXT DEFAULT '',
    description TEXT DEFAULT '',
    fingerprint TEXT DEFAULT '',
    source_chat_id INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'NEW',
    UNIQUE(source_chat_id, source_message_id)
);

CREATE INDEX IF NOT EXISTS idx_profiles_fingerprint ON profiles(fingerprint);
CREATE INDEX IF NOT EXISTS idx_profiles_normalized_city ON profiles(normalized_city);
CREATE INDEX IF NOT EXISTS idx_profiles_age ON profiles(age);
CREATE INDEX IF NOT EXISTS idx_profiles_status ON profiles(status);

CREATE TABLE IF NOT EXISTS profile_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
    UNIQUE(profile_id, telegram_message_id)
);

CREATE INDEX IF NOT EXISTS idx_pm_profile_id ON profile_messages(profile_id);
CREATE INDEX IF NOT EXISTS idx_pm_telegram_message_id ON profile_messages(telegram_message_id);

CREATE TABLE IF NOT EXISTS chat_context (
    chat_id INTEGER PRIMARY KEY,
    profile_message_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS filter_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL,
    decision TEXT NOT NULL,
    reasons TEXT NOT NULL DEFAULT '[]',
    rules_checked INTEGER NOT NULL DEFAULT 0,
    evaluated_at TEXT NOT NULL,
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_fr_profile_id ON filter_results(profile_id);

CREATE TABLE IF NOT EXISTS ai_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL,
    decision TEXT NOT NULL,
    combined_score REAL NOT NULL DEFAULT 0.0,
    confidence REAL NOT NULL DEFAULT 0.0,
    reasons TEXT NOT NULL DEFAULT '[]',
    scoring_version TEXT DEFAULT 'v1',
    evaluated_at TEXT NOT NULL,
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ad_profile_id ON ai_decisions(profile_id);
CREATE INDEX IF NOT EXISTS idx_ad_evaluated_at ON ai_decisions(evaluated_at);

CREATE TABLE IF NOT EXISTS auto_actions_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    decision TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    sent_at TEXT NOT NULL,
    telegram_message_id INTEGER,
    message_text TEXT DEFAULT '',
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_aal_sent_at ON auto_actions_log(sent_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_aal_telegram
    ON auto_actions_log(chat_id, telegram_message_id, action)
    WHERE telegram_message_id IS NOT NULL;

-- Аналитика сообщений (Stage 8.5): какие тексты лайков пишутся и отвечают ли.
-- match_responses: событие взаимного лайка (девушка лайкнула в ответ) —
-- основной сигнал «ответила ли девушка» (путь Leo «Начинай общаться 👉 [Имя]»).
CREATE TABLE IF NOT EXISTS match_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER,
    name TEXT NOT NULL,
    telegram_username TEXT DEFAULT '',
    chat_id INTEGER NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    responded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_match_responses_profile_id ON match_responses(profile_id);
CREATE INDEX IF NOT EXISTS idx_match_responses_responded_at ON match_responses(responded_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_match_responses_unique
    ON match_responses(chat_id, telegram_message_id);

-- sent_messages: ручные действия владельца в чате Дайвинчика (Stage 8.5):
-- «❤️» → action LIKE (сам лайкнул), «👎» → DISLIKE, текст → MESSAGE (сообщение
-- при лайке). Авто-сообщения «Берем)» лежат в auto_actions_log.message_text.
CREATE TABLE IF NOT EXISTS sent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER,
    action TEXT NOT NULL DEFAULT 'MESSAGE',
    text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    chat_id INTEGER NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    sent_at TEXT NOT NULL,
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_sent_messages_profile_id ON sent_messages(profile_id);
CREATE INDEX IF NOT EXISTS idx_sent_messages_text ON sent_messages(text);
CREATE INDEX IF NOT EXISTS idx_sent_messages_sent_at ON sent_messages(sent_at);

-- like_outcomes (Stage 8.5.1): результат на КАЖДЫЙ лайк (авто/ручной) —
-- содержание сообщения при лайке (message_text, '' если без текста) и
-- responded (0/1) — лайкнула девушка в ответ или нет (ставится взаимным
-- лайком через match_responses). Бэкфилл старых лайков в _ensure_like_outcomes.
CREATE TABLE IF NOT EXISTS like_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER,
    source TEXT NOT NULL DEFAULT 'auto',
    message_text TEXT NOT NULL DEFAULT '',
    chat_id INTEGER NOT NULL,
    like_tm_id INTEGER NOT NULL,
    liked_at TEXT NOT NULL,
    responded INTEGER NOT NULL DEFAULT 0,
    responded_at TEXT,
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_like_outcomes_profile_id ON like_outcomes(profile_id);
CREATE INDEX IF NOT EXISTS idx_like_outcomes_responded ON like_outcomes(responded);
CREATE UNIQUE INDEX IF NOT EXISTS idx_like_outcomes_unique
    ON like_outcomes(source, chat_id, like_tm_id);

CREATE TABLE IF NOT EXISTS human_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL,
    ai_decision_id INTEGER NOT NULL,
    decision TEXT NOT NULL,
    agreement TEXT NOT NULL DEFAULT 'UNRESOLVED',
    created_at TEXT NOT NULL,
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
    FOREIGN KEY (ai_decision_id) REFERENCES ai_decisions(id) ON DELETE CASCADE,
    UNIQUE(ai_decision_id)
);

CREATE INDEX IF NOT EXISTS idx_human_decisions_profile_id ON human_decisions(profile_id);
CREATE INDEX IF NOT EXISTS idx_human_decisions_ai_decision_id ON human_decisions(ai_decision_id);
CREATE INDEX IF NOT EXISTS idx_human_decisions_decision ON human_decisions(decision);
CREATE INDEX IF NOT EXISTS idx_human_decisions_created_at ON human_decisions(created_at);
"""


class Database:
    """Асинхронный менеджер SQLite-iteDatabase."""

    def __init__(self, path: Path = DB_PATH) -> None:
        self._path = path
        self._connection: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Открывает соединение, создаёт таблицы."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(str(self._path))
        await self._connection.execute("PRAGMA journal_mode=WAL;")
        await self._connection.execute("PRAGMA foreign_keys=ON;")
        await self._connection.executescript(SCHEMA)
        await self._migrate()
        await self._connection.commit()
        logger.info(f"База данных подключена: {self._path}")

    async def close(self) -> None:
        """Закрывает соединение с БД."""
        if self._connection:
            await self._connection.close()
            self._connection = None
            logger.info("Соединение с БД закрыто")

    async def _migrate(self) -> None:
        """Идемпотентные миграции для уже существующих БД.

        CREATE TABLE IF NOT EXISTS не добавляет колонки в существующую
        таблицу, поэтому недостающие колонки добавляем здесь (backward
        compatible — существующие данные не трогаются).
        """
        # C1/C2: уникальность исходного Telegram-сообщения. Авторитетная
        # защита от повторной доставки (restart/reconnect/telethon catch_up).
        await self._ensure_raw_unique_index()
        # W3: флаг обработки RAW для startup-backlog recovery.
        await self._ensure_raw_processed_column()
        # Кнопки сообщения (reply_markup) — read-only разведка слоя действий.
        await self._ensure_raw_reply_markup_column()
        await self._ensure_auto_actions_log()
        # Аналитика лайков (Stage 8.5): таблицы ответов/исходящих и текст
        # действия MESSAGE в авто-журнале (пустая строка = текста не было).
        await self._ensure_auto_actions_message_text()
        # Ручные действия владельца в sent_messages: колонка action
        # (LIKE/DISLIKE для «сам лайкнул», MESSAGE для текста).
        await self._ensure_sent_messages_action()
        # Результат каждого лайка («ответила/нет») + содержание сообщения.
        await self._ensure_like_outcomes()

    async def _ensure_auto_actions_log(self) -> None:
        """Добавляет журнал успешных авто-действий для старых БД.

        Идемпотентность теперь по конкретной карточке (telegram_message_id):
        каждая показанная анкета получает реакцию ровно один раз, а повторная
        карточка той же личности (новый telegram_message_id) — снова. Поэтому
        UNIQUE(profile_id) убран; на один профиль может быть несколько записей
        (по разным карточкам ленты).
        """
        await self._connection.execute(
            """CREATE TABLE IF NOT EXISTS auto_actions_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                decision TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                sent_at TEXT NOT NULL,
                telegram_message_id INTEGER,
                FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
            )"""
        )
        # Миграция существующих БД (старая схема с UNIQUE(profile_id)
        # и без telegram_message_id): пересоздаём таблицу, сохраняя записи.
        cols = await self._connection.execute(
            "PRAGMA table_info(auto_actions_log)"
        )
        names = {row[1] for row in await cols.fetchall()}
        if "telegram_message_id" not in names:
            # Старая схема (UNIQUE(profile_id), без telegram_message_id):
            # пересоздаём с правильной схемой, перенося записи. SQLite не
            # поддерживает DROP CONSTRAINT, поэтому полная пересборка.
            await self._connection.execute(
                """ALTER TABLE auto_actions_log RENAME TO auto_actions_log_old"""
            )
            await self._connection.execute(
                """CREATE TABLE auto_actions_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    sent_at TEXT NOT NULL,
                    telegram_message_id INTEGER,
                    FOREIGN KEY (profile_id) REFERENCES profiles(id)
                    ON DELETE CASCADE
                )"""
            )
            await self._connection.execute(
                """INSERT INTO auto_actions_log
                (id, profile_id, action, decision, chat_id, sent_at,
                 telegram_message_id)
                SELECT id, profile_id, action, decision, chat_id, sent_at, NULL
                FROM auto_actions_log_old"""
            )
            await self._connection.execute(
                "DROP TABLE auto_actions_log_old"
            )
            await self._connection.commit()
            # Свежий индекс по времени после пересоздания.
            try:
                await self._connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_aal_sent_at "
                    "ON auto_actions_log(sent_at)"
                )
                await self._connection.commit()
            except Exception as e:
                logger.warning(f"index aal_sent_at: {e}")
        # Idempotency по конкретной карточке И виду действия (LIKE/DISLIKE/MESSAGE):
        # ❤️ и "Берем)" — НЕЗАВИСИМЫЕ действия (Decision != Action, §30): на одну
        # карточку может быть и LIKE, и MESSAGE, но каждый вид — не более одного
        # раза. Partial index (WHERE IS NOT NULL) не трогает старые записи (NULL).
        try:
            await self._connection.execute(
                "DROP INDEX IF EXISTS idx_aal_telegram"
            )
            await self._connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_aal_telegram "
                "ON auto_actions_log(chat_id, telegram_message_id, action) "
                "WHERE telegram_message_id IS NOT NULL"
            )
            await self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_aal_sent_at ON auto_actions_log(sent_at)"
            )
            await self._connection.commit()
        except Exception as e:
            logger.warning(f"Не удалось создать индекс авто-журнала: {e}")

    async def _ensure_auto_actions_message_text(self) -> None:
        """Добавляет колонку message_text в auto_actions_log (старые БД).

        Stage 8.5: для аналитики тестов сообщений при лайке сохраняем сам
        отправленный текст («Берем)» из like_message.text). У старых БД
        колонки нет — добавляем ALTER TABLE (существующие данные не трогаем).
        """
        cols = await self._connection.execute(
            "PRAGMA table_info(auto_actions_log)"
        )
        names = {row[1] for row in await cols.fetchall()}
        if "message_text" not in names:
            try:
                await self._connection.execute(
                    "ALTER TABLE auto_actions_log "
                    "ADD COLUMN message_text TEXT DEFAULT ''"
                )
                await self._connection.commit()
            except Exception as e:
                logger.warning(f"message_text column: {e}")
        await self._connection.execute(
            """CREATE TABLE IF NOT EXISTS match_responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER,
                name TEXT NOT NULL,
                telegram_username TEXT DEFAULT '',
                chat_id INTEGER NOT NULL,
                telegram_message_id INTEGER NOT NULL,
                responded_at TEXT NOT NULL
            )"""
        )
        await self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_match_responses_profile_id "
            "ON match_responses(profile_id)"
        )
        await self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_match_responses_responded_at "
            "ON match_responses(responded_at)"
        )
        await self._connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_match_responses_unique "
            "ON match_responses(chat_id, telegram_message_id)"
        )
        await self._connection.execute(
            """CREATE TABLE IF NOT EXISTS sent_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER,
                action TEXT NOT NULL DEFAULT 'MESSAGE',
                text TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual',
                chat_id INTEGER NOT NULL,
                telegram_message_id INTEGER NOT NULL,
                sent_at TEXT NOT NULL,
                FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE SET NULL
            )"""
        )
        await self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_sent_messages_profile_id "
            "ON sent_messages(profile_id)"
        )
        await self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_sent_messages_text "
            "ON sent_messages(text)"
        )
        await self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_sent_messages_sent_at "
            "ON sent_messages(sent_at)"
        )
        await self._connection.commit()

    async def _ensure_sent_messages_action(self) -> None:
        """Добавляет колонку action в sent_messages (старые БД).

        Ручные действия владельца: реакция «❤️»→LIKE, «👎»→DISLIKE,
        текст→MESSAGE. Для аналитики «сам лайкнул» (ручные лайки, которые
        тоже считаются в топе и влияют на привязку ответа).
        """
        cols = await self._connection.execute(
            "PRAGMA table_info(sent_messages)"
        )
        names = {row[1] for row in await cols.fetchall()}
        if "action" not in names:
            try:
                await self._connection.execute(
                    "ALTER TABLE sent_messages "
                    "ADD COLUMN action TEXT NOT NULL DEFAULT 'MESSAGE'"
                )
                await self._connection.commit()
            except Exception as e:
                logger.warning(f"sent_messages.action column: {e}")

    async def _ensure_like_outcomes(self) -> None:
        """Таблица результатов лайков (Stage 8.5.1).

        На каждый лайк (авто/ручной) — строка: содержание сообщения при лайке
        (``message_text``, '' если сообщения не было) и результат
        ``responded`` (0/1) — лайкнула девушка в ответ или нет (ставится по
        match_responses при взаимном лайке). Заполняется в рантайме из
        record_auto_action/record_sent_message/record_match_response;
        существующие лайки добираем однократным бэкфиллом (INSERT OR IGNORE —
        идемпотентно).
        """
        await self._connection.execute(
            """CREATE TABLE IF NOT EXISTS like_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER,
                source TEXT NOT NULL DEFAULT 'auto',
                message_text TEXT NOT NULL DEFAULT '',
                chat_id INTEGER NOT NULL,
                like_tm_id INTEGER NOT NULL,
                liked_at TEXT NOT NULL,
                responded INTEGER NOT NULL DEFAULT 0,
                responded_at TEXT,
                FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE SET NULL
            )"""
        )
        await self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_like_outcomes_profile_id "
            "ON like_outcomes(profile_id)"
        )
        await self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_like_outcomes_responded "
            "ON like_outcomes(responded)"
        )
        await self._connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_like_outcomes_unique "
            "ON like_outcomes(source, chat_id, like_tm_id)"
        )
        # Бэкфилл: авто-лайки (текст — из MESSAGE на ту же карточку).
        try:
            await self._connection.execute(
                """INSERT OR IGNORE INTO like_outcomes
                (profile_id, source, message_text, chat_id, like_tm_id, liked_at, responded)
                SELECT a.profile_id, 'auto',
                       COALESCE((
                           SELECT m.message_text FROM auto_actions_log m
                           WHERE m.profile_id = a.profile_id
                             AND m.chat_id = a.chat_id
                             AND m.telegram_message_id = a.telegram_message_id
                             AND m.action = 'MESSAGE'
                             AND m.message_text != ''
                           ORDER BY m.sent_at DESC LIMIT 1
                       ), ''),
                       a.chat_id, a.telegram_message_id, a.sent_at,
                       CASE WHEN EXISTS (
                           SELECT 1 FROM match_responses r
                           WHERE r.profile_id = a.profile_id
                       ) THEN 1 ELSE 0 END
                FROM auto_actions_log a
                WHERE a.action = 'LIKE'
                  AND a.profile_id IS NOT NULL
                  AND a.telegram_message_id IS NOT NULL"""
            )
        except Exception as e:
            logger.warning(f"like_outcomes backfill (auto): {e}")
        # Бэкфилл: ручные лайки (текст — первый MESSAGE после ❤️ на тот же чат).
        try:
            await self._connection.execute(
                """INSERT OR IGNORE INTO like_outcomes
                (profile_id, source, message_text, chat_id, like_tm_id, liked_at, responded)
                SELECT s.profile_id, 'manual',
                       COALESCE((
                           SELECT m.text FROM sent_messages m
                           WHERE m.profile_id = s.profile_id
                             AND m.chat_id = s.chat_id
                             AND m.action = 'MESSAGE'
                             AND m.text != ''
                             AND m.telegram_message_id > s.telegram_message_id
                           ORDER BY m.sent_at ASC LIMIT 1
                       ), ''),
                       s.chat_id, s.telegram_message_id, s.sent_at,
                       CASE WHEN EXISTS (
                           SELECT 1 FROM match_responses r
                           WHERE r.profile_id = s.profile_id
                       ) THEN 1 ELSE 0 END
                FROM sent_messages s
                WHERE s.action = 'LIKE'
                  AND s.profile_id IS NOT NULL"""
            )
        except Exception as e:
            logger.warning(f"like_outcomes backfill (manual): {e}")
        await self._connection.commit()

    async def _ensure_raw_unique_index(self) -> None:
        """Создаёт UNIQUE-индекс на (chat_id, telegram_message_id).

        Если в таблице уже есть дубликаты (из старой версии без защиты),
        они сворачиваются — оставляем самую раннюю запись для каждой пары.
        Без индекса INSERT OR IGNORE не сработает как защита.
        """
        try:
            await self._connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_unique "
                "ON raw_messages(chat_id, telegram_message_id)"
            )
            await self._connection.commit()
        except aiosqlite.IntegrityError:
            logger.warning(
                "raw_messages содержит дубликаты (chat_id, telegram_message_id); "
                "оставляем только первую запись для каждой пары."
            )
            await self._connection.execute(
                "DELETE FROM raw_messages "
                "WHERE id NOT IN ("
                "  SELECT MIN(id) FROM raw_messages "
                "  GROUP BY chat_id, telegram_message_id"
                ")"
            )
            await self._connection.commit()
            await self._connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_unique "
                "ON raw_messages(chat_id, telegram_message_id)"
            )
            await self._connection.commit()

    async def _ensure_raw_processed_column(self) -> None:
        """Добавляет processed_at и помечает исторические RAW как обработанные.

        Новые сообщения стартуют с processed_at IS NULL и помечаются после
        прохождения pipeline. Существующие (до миграции) RAW считаем
        обработанными, чтобы не переобрабатывать всю историю при первом
        запуске после миграции.
        """
        cursor = await self._connection.execute("PRAGMA table_info(raw_messages)")
        rows = await cursor.fetchall()
        columns = {row[1] for row in rows}
        if "processed_at" not in columns:
            await self._connection.execute(
                "ALTER TABLE raw_messages ADD COLUMN processed_at TEXT"
            )
            await self._connection.commit()
            logger.info("Миграция: raw_messages.processed_at добавлен")
        now = datetime.now(timezone.utc).isoformat()
        await self._connection.execute(
            "UPDATE raw_messages SET processed_at = ? WHERE processed_at IS NULL",
            (now,),
        )
        await self._connection.commit()

    async def _ensure_raw_reply_markup_column(self) -> None:
        """Добавляет reply_markup (кнопки сообщения) для существующих БД.

        Read-only сбор данных для разведки слоя действий (кнопка LIKE/далее):
        сериализованный JSON кнопок (текст + callback_data/url) сохраняется в
        RAW ПЕРВЫМ, до любого разбора. Обратная совместимость — DEFAULT '[]'.
        """
        cursor = await self._connection.execute("PRAGMA table_info(raw_messages)")
        rows = await cursor.fetchall()
        columns = {row[1] for row in rows}
        if "reply_markup" not in columns:
            await self._connection.execute(
                "ALTER TABLE raw_messages ADD COLUMN reply_markup TEXT DEFAULT '[]'"
            )
            await self._connection.commit()
            logger.info("Миграция: raw_messages.reply_markup добавлен")

    async def mark_raw_processed(self, raw_id: int) -> None:
        """Помечает RAW-сообщение обработанным (pipeline завершён)."""
        now = datetime.now(timezone.utc).isoformat()
        await self._connection.execute(
            "UPDATE raw_messages SET processed_at = ? WHERE id = ?",
            (now, raw_id),
        )
        await self._connection.commit()

    async def get_max_raw_id(self) -> int:
        """Максимальный id в raw_messages (для cutoff backlog-рекавери)."""
        cursor = await self._connection.execute("SELECT MAX(id) FROM raw_messages")
        row = await cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    async def get_unprocessed_raw_messages_before(
        self, cutoff_id: int, limit: int, after_id: int = 0,
    ) -> list[dict]:
        """Необработанные RAW с id <= cutoff_id и id > after_id (батч для рекавери).

        after_id продвигает курсор линейно по id, чтобы один и тот же RAW не
        попадал в очередь дважды (worker может ещё не пометить его processed
        к моменту следующего батча).
        """
        cursor = await self._connection.execute(
            """SELECT * FROM raw_messages
            WHERE processed_at IS NULL AND id <= ? AND id > ?
            ORDER BY id
            LIMIT ?""",
            (cutoff_id, after_id, limit),
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(cursor, row) for row in rows]

    async def check(self) -> bool:
        """Проверяет доступность БД."""
        if not self._connection:
            return False
        try:
            await self._connection.execute("SELECT 1;")
            return True
        except Exception:
            return False

    async def save_raw_message(
        self,
        telegram_message_id: int,
        chat_id: int,
        sender_id: int,
        sender_username: str,
        sender_name: str,
        message_date: str,
        text: str,
        raw_entities: str,
        reply_markup: str = "[]",
        media_type: str = "",
        reply_to_message_id: int | None = None,
        received_at: str = "",
        retry_attempts: int = _RAW_SAVE_MAX_RETRIES,
    ) -> int | None:
        """Сохраняет сырое сообщение в БД с ограниченным retry при транзитных сбоях.

        Дубликат (нарушение UNIQUE) НЕ является ошибкой: INSERT OR IGNORE
        просто не вставляет строку и возвращает None — это штатный C1/C2-путь
        (повторная доставка, restart/reconnect). Транзитные сбои БД
        (``sqlite3.OperationalError``, напр. "database is locked") повторяются
        с нарастающим backoff. Постоянные/неизвестные ошибки пробрасываются
        после исчерпания попыток — коллектор их ловит и НЕ пускает сообщение
        дальше в pipeline (RAW-first: сырьё либо сохранено, либо сообщение
        отброшено до парсинга).

        Returns:
            ID записи или None, если сообщение уже было сохранено (дубликат).
        """
        last_error: Exception | None = None
        for attempt in range(retry_attempts):
            try:
                cursor = await self._connection.execute(
                    """INSERT OR IGNORE INTO raw_messages
                    (telegram_message_id, chat_id, sender_id, sender_username,
                     sender_name, message_date, text, raw_entities, reply_markup,
                     media_type, reply_to_message_id, received_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        telegram_message_id, chat_id, sender_id, sender_username,
                        sender_name, message_date, text, raw_entities,
                        reply_markup, media_type, reply_to_message_id, received_at,
                    ),
                )
                await self._connection.commit()
                if cursor.rowcount == 0:
                    return None
                return cursor.lastrowid  # type: ignore[return-value]
            except sqlite3.OperationalError as e:
                # Транзитный сбой БД (locked/busy): повторяем с backoff.
                last_error = e
                if attempt < retry_attempts - 1:
                    logger.warning(
                        f"RAW save: транзитная ошибка БД (попытка "
                        f"{attempt + 1}/{retry_attempts}): {e}; повтор..."
                    )
                    await asyncio.sleep(_RAW_SAVE_BACKOFF_BASE * (2 ** attempt))
                    continue
                logger.error(
                    f"RAW save: не удалось после {retry_attempts} попыток: {e}"
                )
                raise
        # Достижимо только при исчерпании retry_attempts без успеха.
        assert last_error is not None
        raise last_error

    # ── Profile operations ────────────────────────────────────────────

    async def insert_profile(
        self,
        name: str,
        age: int,
        raw_city: str,
        normalized_city: str,
        description: str,
        fingerprint: str,
        source_chat_id: int,
        source_message_id: int,
        first_seen_at: str,
        last_seen_at: str,
        status: str,
    ) -> int:
        """Вставляет новый профиль.

        Returns:
            ID записи.
        """
        cursor = await self._connection.execute(
            """INSERT INTO profiles
            (name, age, raw_city, normalized_city, description,
             fingerprint, source_chat_id, source_message_id,
             first_seen_at, last_seen_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                name, age, raw_city, normalized_city, description,
                fingerprint, source_chat_id, source_message_id,
                first_seen_at, last_seen_at, status,
            ),
        )
        await self._connection.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_profile_by_id(self, profile_id: int) -> dict | None:
        """Получает профиль по ID."""
        cursor = await self._connection.execute(
            "SELECT * FROM profiles WHERE id = ?",
            (profile_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(cursor, row)

    async def find_profile_by_message(
        self, chat_id: int, telegram_message_id: int,
    ) -> dict | None:
        """Ищет профиль по telegram_message_id."""
        cursor = await self._connection.execute(
            """SELECT p.* FROM profiles p
            JOIN profile_messages pm ON pm.profile_id = p.id
            WHERE pm.chat_id = ? AND pm.telegram_message_id = ?""",
            (chat_id, telegram_message_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(cursor, row)

    async def set_chat_profile_context(
        self, chat_id: int, profile_message_id: int,
    ) -> None:
        """Сохраняет контекст чата: какое PROFILE-сообщение «текущее».

        Позволяет MEDIA_ONLY восстановить привязку к профилю после
        restart/reconnect, когда in-memory кэш пуст. ON CONFLICT обновляет
        «текущий» профиль для чата (всегда только один).
        """
        now = datetime.now(timezone.utc).isoformat()
        await self._connection.execute(
            """INSERT INTO chat_context (chat_id, profile_message_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                profile_message_id = excluded.profile_message_id,
                updated_at = excluded.updated_at""",
            (chat_id, profile_message_id, now),
        )
        await self._connection.commit()

    async def get_chat_profile_context(self, chat_id: int) -> int | None:
        """Возвращает profile_message_id «текущего» профиля чата или None."""
        cursor = await self._connection.execute(
            "SELECT profile_message_id FROM chat_context WHERE chat_id = ?",
            (chat_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return int(row[0])

    async def find_profile_by_fingerprint(self, fingerprint: str) -> dict | None:
        """Ищет профиль по fingerprint."""
        cursor = await self._connection.execute(
            "SELECT * FROM profiles WHERE fingerprint = ?",
            (fingerprint,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(cursor, row)

    async def update_profile_last_seen(self, profile_id: int, last_seen_at: str) -> None:
        """Обновляет last_seen_at и переводит статус в SEEN."""
        await self._connection.execute(
            """UPDATE profiles
            SET last_seen_at = ?, status = CASE
                WHEN status IN ('LIKED', 'DISLIKED') THEN status
                ELSE 'SEEN'
            END
            WHERE id = ?""",
            (last_seen_at, profile_id),
        )
        await self._connection.commit()

    async def update_profile_description(
        self, profile_id: int, description: str,
    ) -> None:
        """Обновляет описание профиля."""
        await self._connection.execute(
            "UPDATE profiles SET description = ? WHERE id = ?",
            (description, profile_id),
        )
        await self._connection.commit()

    async def has_auto_action(self, profile_id: int) -> bool:
        """Проверяет, было ли для анкеты успешно отправлено авто-действие."""
        cursor = await self._connection.execute(
            "SELECT 1 FROM auto_actions_log WHERE profile_id = ?", (profile_id,)
        )
        return await cursor.fetchone() is not None

    async def has_auto_action_for_message(
        self, chat_id: int, telegram_message_id: int,
        action: str = "LIKE",
    ) -> bool:
        """Идемпотентность по конкретной карточке (telegram_message_id) и виду
        действия (LIKE / DISLIKE / MESSAGE).

        Каждый вид действия (Decision != Action, §30): LIKE (❤️), DISLIKE (👎)
        и MESSAGE («Берем)») независимы — на одну карточку может быть и LIKE,
        и MESSAGE, но каждый вид — не более одного раза. Повторная карточка
        той же личности (новый telegram_message_id) считается новой — реакция
        отправляется снова.
        """
        cursor = await self._connection.execute(
            "SELECT 1 FROM auto_actions_log "
            "WHERE chat_id = ? AND telegram_message_id = ? AND action = ?",
            (chat_id, telegram_message_id, action),
        )
        return await cursor.fetchone() is not None

    async def record_auto_action(
        self,
        profile_id: int,
        action: str,
        decision: str,
        chat_id: int,
        telegram_message_id: int | None = None,
        message_text: str | None = None,
    ) -> None:
        """Атомарно фиксирует успешное действие и итоговый статус анкеты.

        ``action`` — вид действия: LIKE / DISLIKE / MESSAGE (decision != action,
        §30). Статус анкеты (LIKED / DISLIKED) обновляется только для LIKE и
        DISLIKE; MESSAGE (сообщение «Берем)») статус НЕ меняет. ``message_text``
        (Stage 8.5) сохраняется для MESSAGE — текст, отправленный при лайке
        (из like_message.text), нужен аналитике протестированных сообщений.
        """
        sent_at = datetime.now(timezone.utc).isoformat()
        status: str | None = None
        if action == "LIKE":
            status = "LIKED"
        elif action == "DISLIKE":
            status = "DISLIKED"
        try:
            await self._connection.execute(
                """INSERT INTO auto_actions_log
                (profile_id, action, decision, chat_id, sent_at, telegram_message_id, message_text)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    profile_id,
                    action,
                    decision,
                    chat_id,
                    sent_at,
                    telegram_message_id,
                    message_text or "",
                ),
            )
            if status is not None:
                await self._connection.execute(
                    "UPDATE profiles SET status = ? WHERE id = ?",
                    (status, profile_id),
                )
            await self._track_like_outcome_from_auto(
                action=action,
                profile_id=profile_id,
                chat_id=chat_id,
                telegram_message_id=telegram_message_id,
                message_text=message_text or "",
                sent_at=sent_at,
            )
            await self._connection.commit()
        except Exception:
            await self._connection.rollback()
            raise

    async def _track_like_outcome_from_auto(
        self,
        action: str,
        profile_id: int,
        chat_id: int,
        telegram_message_id: int | None,
        message_text: str,
        sent_at: str,
    ) -> None:
        """Заводит/обновляет like_outcome («каждый лайк + результат»).

        LIKE → запись о лайке (текст пока пустой, появится при MESSAGE).
        MESSAGE → прикрепляет содержание сообщения к лайку на ту же карточку.
        Идемпотентность — UNIQUE(source, chat_id, like_tm_id).
        """
        if telegram_message_id is None:
            return
        try:
            if action == "LIKE":
                await self._connection.execute(
                    """INSERT OR IGNORE INTO like_outcomes
                    (profile_id, source, message_text, chat_id, like_tm_id, liked_at, responded)
                    VALUES (?, 'auto', '', ?, ?, ?, 0)""",
                    (profile_id, chat_id, telegram_message_id, sent_at),
                )
            elif action == "MESSAGE" and message_text:
                await self._connection.execute(
                    """UPDATE like_outcomes
                    SET message_text = ?
                    WHERE source = 'auto' AND chat_id = ? AND like_tm_id = ?
                      AND message_text = ''""",
                    (message_text, chat_id, telegram_message_id),
                )
        except Exception as e:
            logger.warning(f"track_like_outcome(auto): {e}")

    # ── Аналитика лайков (Stage 8.5) ─────────────────────────────────

    async def _resolve_profile_by_name(self, name: str) -> int | None:
        """Находит id профиля по имени среди тех, кому отправлялось действие.

        Привязка «девушка ответила» к конкретной анкете. Имя сравнивается
        регистронезависимо по-настоящему (Python lower — SQLite Cyrillic
        NOCASE/lower() работают только с ASCII). Приоритет — свежие записи
        (авто-журнал ∪ ручные sent_messages). None если профиль
        с таким именем ещё не лайкался.
        """
        needle = (name or "").strip().lower()
        if not needle:
            return None
        try:
            cursor = await self._connection.execute(
                """SELECT p.id, p.name, MAX(u.sent_at) AS last_sent
                FROM (
                    SELECT profile_id AS pid, sent_at FROM auto_actions_log
                    UNION ALL
                    SELECT profile_id, sent_at FROM sent_messages
                ) u
                JOIN profiles p ON p.id = u.pid
                GROUP BY p.id, p.name
                ORDER BY last_sent DESC"""
            )
            rows = await cursor.fetchall()
        except Exception as e:
            logger.warning(f"resolve_profile_by_name: {e}")
            return None
        for rid, rname, _last in rows:
            if (rname or "").strip().lower() == needle:
                return rid
        return None

    async def resolve_profile_by_message(
        self, profile_message_id: int,
    ) -> int | None:
        """Находит профиль по telegram_message_id его карточки (chat_context)."""
        try:
            cursor = await self._connection.execute(
                "SELECT id FROM profiles "
                "WHERE source_message_id = ? ORDER BY id DESC LIMIT 1",
                (profile_message_id,),
            )
            row = await cursor.fetchone()
        except Exception as e:
            logger.warning(f"resolve_profile_by_message: {e}")
            return None
        return row[0] if row else None

    async def record_match_response(
        self,
        name: str,
        chat_id: int,
        telegram_message_id: int,
        telegram_username: str = "",
    ) -> None:
        """Фиксирует взаимный лайк («девушка ответила») — сигнал конверсии.

        Привязывается к профилю по имени (после лайков с этого же чата).
        Дубликаты на одну карточку (chat_id + telegram_message_id)
        отбрасываются (INSERT OR IGNORE).
        """
        profile_id = await self._resolve_profile_by_name(name)
        responded_at = datetime.now(timezone.utc).isoformat()
        try:
            await self._connection.execute(
                """INSERT OR IGNORE INTO match_responses
                (profile_id, name, telegram_username, chat_id, telegram_message_id, responded_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    profile_id,
                    (name or "").strip(),
                    telegram_username or "",
                    chat_id,
                    telegram_message_id,
                    responded_at,
                ),
            )
            if profile_id is not None:
                # результат каждого лайка этой девушки: «лайкнула в ответ».
                await self._connection.execute(
                    """UPDATE like_outcomes
                    SET responded = 1, responded_at = ?
                    WHERE profile_id = ? AND responded = 0""",
                    (responded_at, profile_id),
                )
            await self._connection.commit()
        except Exception as e:
            await self._connection.rollback()
            logger.warning(f"record_match_response: {e}")

    async def record_sent_message(
        self,
        text: str,
        chat_id: int,
        telegram_message_id: int,
        source: str = "manual",
        profile_id: int | None = None,
        action: str = "MESSAGE",
    ) -> None:
        """Фиксирует РУЧНОЕ действие владельца в чате Дайвинчика.

        ``action``: 'LIKE' (сам лайкнул, ❤️), 'DISLIKE' (👎), 'MESSAGE'
        (текст при лайке — «Беру)» и т.п.). Ручные действия — вторая половина
        аналитики лайков (авто-«Берем)» живут в auto_actions_log.message_text).
        LIKE заводит like_outcome (результат «ответила/нет»), MESSAGE
        прикрепляет содержание текста к последнему ручному лайку.
        """
        sent_at = datetime.now(timezone.utc).isoformat()
        try:
            await self._connection.execute(
                """INSERT INTO sent_messages
                (profile_id, action, text, source, chat_id, telegram_message_id, sent_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    profile_id,
                    action or "MESSAGE",
                    (text or "").strip(),
                    source,
                    chat_id,
                    telegram_message_id,
                    sent_at,
                ),
            )
            if action == "LIKE":
                await self._connection.execute(
                    """INSERT OR IGNORE INTO like_outcomes
                    (profile_id, source, message_text, chat_id, like_tm_id, liked_at, responded)
                    VALUES (?, 'manual', '', ?, ?, ?, 0)""",
                    (profile_id, chat_id, telegram_message_id, sent_at),
                )
            elif action == "MESSAGE":
                await self._connection.execute(
                    """UPDATE like_outcomes
                    SET message_text = ?
                    WHERE id IN (
                        SELECT id FROM like_outcomes
                        WHERE source = 'manual' AND chat_id = ? AND message_text = ''
                        ORDER BY liked_at DESC, id DESC LIMIT 1
                    )""",
                    ((text or "").strip(), chat_id),
                )
            await self._connection.commit()
        except Exception as e:
            await self._connection.rollback()
            logger.warning(f"record_sent_message: {e}")

    async def get_message_response_stats(self) -> list[dict]:
        """Конверсия по текстам сообщений при лайке.

        Объединяет авто-«Берем)» (auto_actions_log.action='MESSAGE') и ручные
        тексты (sent_messages). Для каждого текста: сколько лайков с этим
        текстом отправлено (по уникальным профилям) и сколько профилей
        ответило (взаимный лайк, match_responses).
        """
        cursor = await self._connection.execute(
            """WITH all_msgs AS (
                SELECT message_text AS text, profile_id AS pid
                FROM auto_actions_log
                WHERE action = 'MESSAGE'
                  AND message_text IS NOT NULL AND message_text != ''
                UNION ALL
                SELECT text, profile_id
                FROM sent_messages
                WHERE action = 'MESSAGE' AND text != ''
            )
            SELECT m.text AS message_text,
                   COUNT(DISTINCT m.pid) AS sent,
                   COUNT(DISTINCT r.profile_id) AS responded
            FROM all_msgs m
            LEFT JOIN match_responses r ON r.profile_id = m.pid
            GROUP BY m.text
            ORDER BY sent DESC, responded DESC"""
        )
        rows = await cursor.fetchall()
        return [
            {
                "message_text": row[0],
                "sent": row[1],
                "responded": row[2],
            }
            for row in rows
        ]

    async def get_top_liked_profiles(self, limit: int = 10) -> list[dict]:
        """Топ профилей по количеству лайков + ответили ли они.

        Лайки считаются по like_outcomes (авто ❤️ ∪ ручные «сам лайкнул»),
        на каждый лайк зафиксирован результат responded (0/1) — «лайкнула
        в ответ». ``message_text`` — содержание последнего сообщения при лайке.
        """
        cursor = await self._connection.execute(
            """SELECT p.id, p.name, p.age, p.normalized_city AS city,
                      COUNT(o.id) AS likes,
                      SUM(o.responded) AS responded,
                      (SELECT sub.message_text FROM like_outcomes sub
                       WHERE sub.profile_id = p.id AND sub.message_text != ''
                       ORDER BY sub.liked_at DESC, sub.id DESC LIMIT 1
                      ) AS last_text
            FROM like_outcomes o
            JOIN profiles p ON p.id = o.profile_id
            GROUP BY p.id
            ORDER BY likes DESC, responded DESC
            LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "name": row[1],
                "age": row[2],
                "city": row[3],
                "likes": row[4],
                "responded": row[5] or 0,
                "message_text": row[6] or "",
            }
            for row in rows
        ]

    async def update_profile_raw_city(
        self, profile_id: int, raw_city: str,
    ) -> None:
        """Обновляет raw_city (normalized_city не меняется)."""
        await self._connection.execute(
            "UPDATE profiles SET raw_city = ? WHERE id = ?",
            (raw_city, profile_id),
        )
        await self._connection.commit()

    # ── Profile messages ──────────────────────────────────────────────

    async def link_profile_message(
        self,
        profile_id: int,
        telegram_message_id: int,
        chat_id: int,
        created_at: str,
    ) -> None:
        """Связывает профиль с Telegram-сообщением."""
        await self._connection.execute(
            """INSERT OR IGNORE INTO profile_messages
            (profile_id, telegram_message_id, chat_id, created_at)
            VALUES (?, ?, ?, ?)""",
            (profile_id, telegram_message_id, chat_id, created_at),
        )
        await self._connection.commit()

    async def get_profile_messages(self, profile_id: int) -> list[dict]:
        """Получает все сообщения профиля."""
        cursor = await self._connection.execute(
            """SELECT pm.*, rm.text, rm.media_type
            FROM profile_messages pm
            LEFT JOIN raw_messages rm
                ON rm.telegram_message_id = pm.telegram_message_id
                AND rm.chat_id = pm.chat_id
            WHERE pm.profile_id = ?
            ORDER BY pm.created_at""",
            (profile_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(cursor, row) for row in rows]

    async def get_profile_message_count(self, profile_id: int) -> int:
        """Количество сообщений профиля."""
        cursor = await self._connection.execute(
            "SELECT COUNT(*) FROM profile_messages WHERE profile_id = ?",
            (profile_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    # ── Filter results ────────────────────────────────────────────────

    async def save_filter_result(
        self,
        profile_id: int,
        decision: str,
        reasons: str,
        rules_checked: int,
        evaluated_at: str,
    ) -> int:
        """Сохраняет результат фильтрации."""
        cursor = await self._connection.execute(
            """INSERT INTO filter_results
            (profile_id, decision, reasons, rules_checked, evaluated_at)
            VALUES (?, ?, ?, ?, ?)""",
            (profile_id, decision, reasons, rules_checked, evaluated_at),
        )
        await self._connection.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_latest_filter_result(self, profile_id: int) -> dict | None:
        """Получает последний результат фильтрации."""
        cursor = await self._connection.execute(
            """SELECT * FROM filter_results
            WHERE profile_id = ?
            ORDER BY evaluated_at DESC LIMIT 1""",
            (profile_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(cursor, row)

    async def get_filter_history(self, profile_id: int) -> list[dict]:
        """Получает историю фильтрации."""
        cursor = await self._connection.execute(
            """SELECT * FROM filter_results
            WHERE profile_id = ?
            ORDER BY evaluated_at DESC""",
            (profile_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(cursor, row) for row in rows]

    # ── AI decisions ─────────────────────────────────────────────────

    async def save_ai_decision(
        self,
        profile_id: int,
        decision: str,
        combined_score: float,
        confidence: float,
        reasons: str,
        scoring_version: str,
        evaluated_at: str,
    ) -> int:
        """Сохраняет результат Decision Engine."""
        cursor = await self._connection.execute(
            """INSERT INTO ai_decisions
            (profile_id, decision, combined_score,
             confidence, reasons, scoring_version, evaluated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                profile_id, decision, combined_score,
                confidence, reasons, scoring_version, evaluated_at,
            ),
        )
        await self._connection.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_latest_ai_decision(self, profile_id: int) -> dict | None:
        """Получает последнее AI-решение для профиля."""
        cursor = await self._connection.execute(
            """SELECT * FROM ai_decisions
            WHERE profile_id = ?
            ORDER BY evaluated_at DESC, id DESC LIMIT 1""",
            (profile_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(cursor, row)

    async def get_ai_decision_history(self, profile_id: int) -> list[dict]:
        """Получает историю AI-решений для профиля."""
        cursor = await self._connection.execute(
            """SELECT * FROM ai_decisions
            WHERE profile_id = ?
            ORDER BY evaluated_at DESC, id DESC""",
            (profile_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(cursor, row) for row in rows]

    # ── Human decisions (Stage 6) ───────────────────────────────────

    async def save_human_decision(
        self,
        profile_id: int,
        ai_decision_id: int,
        decision: str,
        agreement: str,
        created_at: str,
    ) -> int:
        """Сохраняет рецензию человека.

        Повторная рецензия той же комбинации (profile_id + ai_decision_id)
        невозможна из-за UNIQUE(ai_decision_id): новая AI-оценка = новый
        ai_decision_id, поэтому каждая запись сохраняется отдельно.
        """
        cursor = await self._connection.execute(
            """INSERT INTO human_decisions
            (profile_id, ai_decision_id, decision, agreement, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            (profile_id, ai_decision_id, decision, agreement, created_at),
        )
        await self._connection.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_human_decision_history(self, profile_id: int) -> list[dict]:
        """Получает историю рецензий для профиля."""
        cursor = await self._connection.execute(
            """SELECT * FROM human_decisions
            WHERE profile_id = ?
            ORDER BY created_at DESC, id DESC""",
            (profile_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(cursor, row) for row in rows]

    async def get_latest_human_decision(self, profile_id: int) -> dict | None:
        """Получает последнюю рецензию для профиля."""
        cursor = await self._connection.execute(
            """SELECT * FROM human_decisions
            WHERE profile_id = ?
            ORDER BY created_at DESC, id DESC LIMIT 1""",
            (profile_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(cursor, row)

    async def is_human_reviewed(
        self, profile_id: int, ai_decision_id: int,
    ) -> bool:
        """Проверяет, рассмотрена ли конкретная AI-оценка."""
        cursor = await self._connection.execute(
            "SELECT 1 FROM human_decisions WHERE profile_id = ? AND ai_decision_id = ?",
            (profile_id, ai_decision_id),
        )
        row = await cursor.fetchone()
        return row is not None

    async def get_all_human_history(self) -> list[dict]:
        """Получает всю историю рецензий (для analytics/экспорта)."""
        cursor = await self._connection.execute(
            """SELECT * FROM human_decisions
            ORDER BY created_at ASC, id ASC"""
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(cursor, row) for row in rows]

    async def get_pending_review(self) -> dict | None:
        """Возвращает самую старую неразобранную AI-оценку.

        В очередь попадают только AI-решения, ещё не рассмотренные
        человеком. Сортировка: oldest first (по evaluated_at).
        """
        cursor = await self._connection.execute(
            """SELECT ad.* FROM ai_decisions ad
            LEFT JOIN human_decisions hd
                ON hd.ai_decision_id = ad.id
            WHERE hd.id IS NULL
            ORDER BY ad.evaluated_at ASC, ad.id ASC
            LIMIT 1"""
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(cursor, row)

    async def get_pending_reviews(self) -> list[dict]:
        """Возвращает все неразобранные AI-оценки (oldest first).

        В очередь попадают только AI-решения, ещё не рассмотренные
        человеком. Сортировка: oldest first (по evaluated_at).
        """
        cursor = await self._connection.execute(
            """SELECT ad.* FROM ai_decisions ad
            LEFT JOIN human_decisions hd
                ON hd.ai_decision_id = ad.id
            WHERE hd.id IS NULL
            ORDER BY ad.evaluated_at ASC, ad.id ASC"""
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(cursor, row) for row in rows]

    async def get_pending_count(self) -> int:
        """Считает количество неразобранных AI-решений."""
        cursor = await self._connection.execute(
            """SELECT COUNT(*) FROM ai_decisions ad
            LEFT JOIN human_decisions hd
                ON hd.ai_decision_id = ad.id
            WHERE hd.id IS NULL"""
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    # ── Analytics queries (Stage 6, READ ONLY) ───────────────────────

    async def count_profiles(self) -> int:
        """Общее количество профилей."""
        cursor = await self._connection.execute("SELECT COUNT(*) FROM profiles")
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def count_filter_results(self, decision: str) -> int:
        """Количество результатов фильтрации по решению."""
        cursor = await self._connection.execute(
            "SELECT COUNT(*) FROM filter_results WHERE decision = ?",
            (decision,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_all_ai_decisions(self) -> list[dict]:
        """Получает все AI-решения (для analytics/экспорта)."""
        cursor = await self._connection.execute(
            """SELECT * FROM ai_decisions ORDER BY evaluated_at ASC, id ASC"""
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(cursor, row) for row in rows]

    async def get_ai_decision(self, ai_decision_id: int) -> dict | None:
        """Получает одно AI-решение по ID."""
        cursor = await self._connection.execute(
            "SELECT * FROM ai_decisions WHERE id = ?", (ai_decision_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(cursor, row)

    async def get_human_reviews_with_ai(self) -> list[dict]:
        """Объединяет human_decisions + ai_decisions (для analytics/экспорта).

        Возвращает строки с полями обеих таблиц (префикс h_ для human).
        """
        cursor = await self._connection.execute(
            """SELECT
                hd.id AS h_id,
                hd.profile_id,
                hd.ai_decision_id,
                hd.decision AS human_decision,
                hd.agreement,
                hd.created_at AS reviewed_at,
                ad.decision AS ai_decision,
                ad.combined_score,
                ad.confidence,
                ad.scoring_version,
                ad.evaluated_at
            FROM human_decisions hd
            JOIN ai_decisions ad ON ad.id = hd.ai_decision_id
            ORDER BY hd.created_at ASC, hd.id ASC"""
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(cursor, row) for row in rows]

    async def get_ai_decision_for_profile_prompt(
        self, profile_id: int, ai_decision_id: int,
    ) -> dict | None:
        """Получает AI-решение профиля по ID (для re-review истории)."""
        cursor = await self._connection.execute(
            "SELECT * FROM ai_decisions WHERE id = ? AND profile_id = ?",
            (ai_decision_id, profile_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(cursor, row)

    def _row_to_dict(self, cursor, row) -> dict:
        return {col[0]: val for col, val in zip(cursor.description, row)}
    @property
    def connection(self):
        return self._connection

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
    processed_at TEXT,
    is_outgoing INTEGER DEFAULT 0,
    account_session TEXT DEFAULT ''
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
    account_session TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
    UNIQUE(profile_id, telegram_message_id)
);

CREATE INDEX IF NOT EXISTS idx_pm_profile_id ON profile_messages(profile_id);
CREATE INDEX IF NOT EXISTS idx_pm_telegram_message_id ON profile_messages(telegram_message_id);

CREATE TABLE IF NOT EXISTS chat_context (
    account_session TEXT NOT NULL DEFAULT '',
    chat_id INTEGER NOT NULL,
    profile_message_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_session, chat_id)
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
-- лайком через match_responses). rejected=1 — Leo отклонил лайк (дневной
-- лимит, «Слишком много ❤️ за сегодня») — такие из аналитики исключаются.
-- Бэкфилл старых лайков в _ensure_like_outcomes.
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
    rejected INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_like_outcomes_profile_id ON like_outcomes(profile_id);
CREATE INDEX IF NOT EXISTS idx_like_outcomes_responded ON like_outcomes(responded);
-- idx_like_outcomes_rejected создаётся в _ensure_like_outcomes ПОСЛЕ миграции
-- колонки rejected (в top-SCHEMA его нет — на старых базах без колонки он бы
-- падал при executescript ещё до ALTER).
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

-- Память капч Leo (Stage 7.6): тексты капч/сделок, на которые бот когда-то
-- затруднился ответить, и выученные ответы владельца. signal signature =
-- нормализованный текст сообщения капчи (нижний регистр + схлопнутые
-- пробелы). answer NULL — капча НЕизвестная и ждёт ответа владельца в блоке
-- капч на главном экране; запись с ответом — бот отвечает автоматически.
CREATE TABLE IF NOT EXISTS captcha_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signature TEXT NOT NULL UNIQUE,
    message_text TEXT NOT NULL,
    buttons_json TEXT NOT NULL DEFAULT '[]',
    answer TEXT,
    answered_at TEXT,
    used_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_captcha_memory_pending
    ON captcha_memory(answer) WHERE answer IS NULL;
CREATE INDEX IF NOT EXISTS idx_captcha_memory_updated
    ON captcha_memory(updated_at);

-- app_state: простой key/value для рантайм-состояния, которое должно переживать
-- рестарт (напр. likes_exhausted_until — дневной лимит ❤️ исчерпан до 07:00).
CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- match_dialogs: автопереписка с девушкой после взаимного лайка (режим AUTO).
-- Строка заводится, когда бот отправил ей стикер-привет. Её первый ответ →
-- status SCHEDULED + due_at (через delay_min); в срок бот пересылает ей её же
-- анкету из чата Leo и закрепляет у обоих → INTRO_SENT. Ключ — аккаунт +
-- user_id девушки (у каждого аккаунта своя переписка).
-- status: WAITING_REPLY | SCHEDULED | INTRO_SENT | NO_PROFILE | ERROR
CREATE TABLE IF NOT EXISTS match_dialogs (
    account_session TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    profile_id INTEGER,
    status TEXT NOT NULL,
    greeted_at TEXT NOT NULL,
    replied_at TEXT,
    due_at TEXT,
    sent_at TEXT,
    pinned INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (account_session, user_id)
);

CREATE INDEX IF NOT EXISTS idx_match_dialogs_status
    ON match_dialogs(status);

-- dialog_contacts: девушки со взаимным лайком, чьи переписки логируются
-- (сбор данных для анализа и алгоритма общения). Заводится на КАЖДЫЙ матч
-- (независимо от привета/автопереписки). Ключ — аккаунт + её user_id.
-- synced_at — последняя сверка её чата с историей Telegram.
CREATE TABLE IF NOT EXISTS dialog_contacts (
    account_session TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    profile_id INTEGER,
    matched_at TEXT NOT NULL,
    last_message_at TEXT,
    synced_at TEXT,
    PRIMARY KEY (account_session, user_id)
);

-- dialog_messages: все сообщения в личке с девушкой из dialog_contacts —
-- её, владельца и бота (append-only). sent_by: her | owner | bot.
-- media_type: '' (текст) | sticker | photo | video | voice | video_note | gif
-- | document | other; для стикера в text — его эмодзи. is_forward — пересланное
-- (её анкета от автопереписки).
CREATE TABLE IF NOT EXISTS dialog_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_session TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    is_outgoing INTEGER NOT NULL DEFAULT 0,
    sent_by TEXT NOT NULL DEFAULT '',
    message_date TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    media_type TEXT NOT NULL DEFAULT '',
    is_forward INTEGER NOT NULL DEFAULT 0,
    reply_to_message_id INTEGER,
    received_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_dialog_messages_unique
    ON dialog_messages(account_session, user_id, telegram_message_id);
CREATE INDEX IF NOT EXISTS idx_dialog_messages_chat
    ON dialog_messages(account_session, user_id, message_date);

-- photo_ratings: быстрая разметка внешности на сайте (/rate). Владелец
-- листает фото анкет (без текста) и отмечает «нравится / нет / пропустить».
-- Это обучающие метки для модели «твой вкус по фото», на действия бота не
-- влияют. Одна оценка на анкету (повторная перезаписывает, «отменить» удаляет).
-- rating: LIKE | DISLIKE | SKIP (SKIP — фото не годится для оценки) |
-- VICE (на фото сигарета/вейп/алкоголь — для владельца это 👎; метки детектора).
CREATE TABLE IF NOT EXISTS photo_ratings (
    profile_id INTEGER PRIMARY KEY,
    rating TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_photo_ratings_rating ON photo_ratings(rating);

-- photo_scores: ТЕНЕВОЙ вердикт модели вкуса по фото (services/photo_taste.py)
-- на каждую карточку анкеты: score 0..1, зона LIKE/REVIEW/DISLIKE и максимум
-- вероятностей «сигарета»/«алкоголь» по фото. На действия бота не влияет —
-- нужен, чтобы сравнить модель с решениями владельца перед включением.
CREATE TABLE IF NOT EXISTS photo_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL,
    account_session TEXT NOT NULL DEFAULT '',
    chat_id INTEGER NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    score REAL NOT NULL,
    verdict TEXT NOT NULL,
    smoke_prob REAL,
    alcohol_prob REAL,
    n_photos INTEGER NOT NULL DEFAULT 0,
    model_version TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
    UNIQUE(account_session, chat_id, telegram_message_id)
);

CREATE INDEX IF NOT EXISTS idx_photo_scores_profile ON photo_scores(profile_id);

-- photo_embeddings: «отпечаток» фото анкеты для обучения модели вкуса —
-- среднее L2-нормированных CLIP-эмбеддингов фото последнего показа
-- (float32 little-endian, BLOB), тех же фото, что видны на /rate. Пишет бот
-- (фоновый досчёт и теневой расчёт), читает scripts/train_photo_taste.py —
-- переобучение без повторного прогона CLIP. emb NULL + n_photos 0 — фото
-- получить не удалось (чтобы досчёт не крутился на ней вечно).
CREATE TABLE IF NOT EXISTS photo_embeddings (
    profile_id INTEGER PRIMARY KEY,
    emb BLOB,
    n_photos INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
);

-- photo_item_ratings: разметка /rate ПО ОДНОМУ ФОТО (с 2026-10-29). Фото =
-- сообщение анкеты (telegram_message_id аккаунта account_session). Те же
-- оценки, что в photo_ratings; повтор перезаписывает, «назад» удаляет.
-- Старые оценки анкеты целиком остаются в photo_ratings (тоже идут в обучение).
CREATE TABLE IF NOT EXISTS photo_item_ratings (
    profile_id INTEGER NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    account_session TEXT NOT NULL DEFAULT '',
    rating TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (profile_id, telegram_message_id),
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
);

-- photo_item_embeddings: CLIP-отпечаток КАЖДОГО фото (L2-нормированный,
-- float32 LE). Нужен для обучения на оценках фото и для оценки анкеты как
-- среднего вероятностей по фото. emb NULL — фото не получилось скачать/прочитать.
CREATE TABLE IF NOT EXISTS photo_item_embeddings (
    profile_id INTEGER NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    account_session TEXT NOT NULL DEFAULT '',
    emb BLOB,
    model TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (profile_id, telegram_message_id),
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
);
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
        # C1/C2: уникальность исходного Telegram-сообщения считается ЗДЕСЬ
        # (после is_outgoing) — индекс включает направление, см. ниже.
        # W3: флаг обработки RAW для startup-backlog recovery.
        await self._ensure_raw_processed_column()
        # Кнопки сообщения (reply_markup) — read-only разведка слоя действий.
        await self._ensure_raw_reply_markup_column()

        # is_outgoing ДОЛЖЕН существовать до уникального индекса: индекс
        # теперь (chat_id, telegram_message_id, is_outgoing) — входящее Leo и
        # наше исходящее могут иметь один и тот же номер (у аккаунтов свои
        # диапазоны message id в одном чате), и без направления наши 👎/❤️
        # молча терялись на INSERT OR IGNORE.
        await self._ensure_raw_is_outgoing_column()
        await self._ensure_raw_account_session_column()
        await self._ensure_raw_unique_index()
        await self._ensure_auto_actions_log()
        # Аналитика лайков (Stage 8.5): таблицы ответов/исходящих и текст
        # действия MESSAGE в авто-журнале (пустая строка = текста не было).
        await self._ensure_auto_actions_message_text()
        # Ручные действия владельца в sent_messages: колонка action
        # (LIKE/DISLIKE для «сам лайкнул», MESSAGE для текста).
        await self._ensure_sent_messages_action()
        # Результат каждого лайка («ответила/нет») + содержание сообщения.
        await self._ensure_like_outcomes()
        # Аккаунт-получатель сообщений анкеты (profile_messages): фото в вебе
        # качаются ТЕМ аккаунтом, который реально видел сообщение (message_id в
        # диалоге с Leo у каждого аккаунта своя нумерация).
        await self._ensure_profile_messages_account_session()
        # Контекст чата — СТРОГО на аккаунт: у аккаунтов свой чат с Leo в ОДНОМ
        # chat_id (свои диапазоны message id), общий контекст связывал MEDIA_ONLY
        # с карточкой другого аккаунта.
        await self._ensure_chat_context_account_session()
        # Логирование переписок: девушки из автопереписки (до появления
        # dialog_contacts) тоже попадают в реестр.
        await self._backfill_dialog_contacts()

    async def _backfill_dialog_contacts(self) -> None:
        """Переносит девушек из match_dialogs в dialog_contacts (идемпотентно)."""
        try:
            await self._connection.execute(
                """INSERT OR IGNORE INTO dialog_contacts
                (account_session, user_id, username, name, profile_id, matched_at)
                SELECT account_session, user_id, username, name, profile_id,
                       greeted_at
                FROM match_dialogs"""
            )
        except Exception as e:
            logger.warning(f"_backfill_dialog_contacts: {e}")

    async def _ensure_chat_context_account_session(self) -> None:
        """Пересоздаёт chat_context с составным ключом (account_session, chat_id).

        Старая схема (``chat_id PRIMARY KEY``) хранила ОДИН «текущий» профиль на
        чат — при нескольких аккаунтах это путало анкеты разных аккаунтов.
        SQLite не умеет менять PRIMARY KEY, поэтому пересобираем таблицу.
        Историю не переносим ('' не совпадёт ни с одним аккаунтом; контекст
        восстанавливается со следующей карточки).
        """
        cols = await self._connection.execute("PRAGMA table_info(chat_context)")
        names = {row[1] for row in await cols.fetchall()}
        if "account_session" in names:
            return
        try:
            await self._connection.execute(
                "ALTER TABLE chat_context RENAME TO chat_context_old"
            )
            await self._connection.execute(
                """CREATE TABLE chat_context (
                    account_session TEXT NOT NULL DEFAULT '',
                    chat_id INTEGER NOT NULL,
                    profile_message_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (account_session, chat_id)
                )"""
            )
            await self._connection.execute("DROP TABLE chat_context_old")
            await self._connection.commit()
            logger.info(
                "Миграция: chat_context пересоздан с ключом "
                "(account_session, chat_id)"
            )
        except Exception as e:
            logger.warning(f"chat_context migration: {e}")

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
                rejected INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE SET NULL
            )"""
        )
        # Миграция: колонка rejected (лимит Leo) для старых баз — ДО индексов,
        # чтобы idx_like_outcomes_rejected не падал «no such column» на старых
        # таблицах. CREATE TABLE IF NOT EXISTS не трогает существующую таблицу.
        cols = await self._connection.execute("PRAGMA table_info(like_outcomes)")
        names = {row[1] for row in await cols.fetchall()}
        if "rejected" not in names:
            await self._connection.execute(
                "ALTER TABLE like_outcomes "
                "ADD COLUMN rejected INTEGER NOT NULL DEFAULT 0"
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
            "CREATE INDEX IF NOT EXISTS idx_like_outcomes_rejected "
            "ON like_outcomes(rejected)"
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

    async def _ensure_profile_messages_account_session(self) -> None:
        """Добавляет колонку account_session в profile_messages (старые БД).

        Аккаунт-получатель нужен веб-панели, чтобы скачивать фото ТЕМ
        аккаунтом, который реально видел сообщение: message_id в диалоге с Leo
        у каждого аккаунта своя нумерация, и запрос того же id через другой
        аккаунт может вернуть ФОТО ДРУГОЙ анкеты. Существующие записи получают
        '' — для них остаётся прежнее поведение (проба всех клиентов).
        """
        cols = await self._connection.execute(
            "PRAGMA table_info(profile_messages)"
        )
        names = {row[1] for row in await cols.fetchall()}
        if "account_session" not in names:
            await self._connection.execute(
                "ALTER TABLE profile_messages "
                "ADD COLUMN account_session TEXT DEFAULT ''"
            )
            await self._connection.commit()
            logger.info("Миграция: profile_messages.account_session добавлен")

    async def _ensure_raw_unique_index(self) -> None:
        """Уникальный индекс RAW по ``(chat_id, telegram_message_id, is_outgoing)``.

        Направление в ключе обязательно: у каждого аккаунта СВОЙ диапазон
        message id в одном чате Leo, поэтому номер нашего исходящего может
        совпасть с номером входящего. Старый индекс без направления молча
        отбрасывал наши исходящие (``INSERT OR IGNORE``) — сырые записи всех
        реакций терялись. Индекс пересоздаём при старте: ``CREATE ... IF NOT
        EXISTS`` не обновляет уже существующее определение, поэтому сначала
        ``DROP``. Дубликаты по ключу схлопываются (оставляем первую запись).
        """
        await self._connection.execute("DROP INDEX IF EXISTS idx_raw_unique")
        await self._connection.commit()
        try:
            await self._connection.execute(
                "CREATE UNIQUE INDEX idx_raw_unique "
                "ON raw_messages(account_session, chat_id, "
                "telegram_message_id, is_outgoing)"
            )
            await self._connection.commit()
        except aiosqlite.IntegrityError:
            logger.warning(
                "raw_messages содержит дубликаты "
                "(account_session, chat_id, telegram_message_id, is_outgoing); "
                "оставляем только первую запись для каждой четвёрки."
            )
            await self._connection.execute(
                "DELETE FROM raw_messages "
                "WHERE id NOT IN ("
                "  SELECT MIN(id) FROM raw_messages "
                "  GROUP BY account_session, chat_id, "
                "telegram_message_id, is_outgoing"
                ")"
            )
            await self._connection.commit()
            await self._connection.execute(
                "CREATE UNIQUE INDEX idx_raw_unique "
                "ON raw_messages(account_session, chat_id, "
                "telegram_message_id, is_outgoing)"
            )
            await self._connection.commit()

    async def _ensure_raw_account_session_column(self) -> None:
        """Добавляет raw_messages.account_session (аккаунт-получатель чата).

        Лента чата фильтруется по выбранному аккаунту-исполнителю: сообщения
        обоих аккаунтов лежат в одном chat_id и без сессии перемешиваются по
        времени. Для истории колонка пуста ('' = неизвестно).
        """
        cursor = await self._connection.execute("PRAGMA table_info(raw_messages)")
        rows = await cursor.fetchall()
        columns = {row[1] for row in rows}
        if "account_session" not in columns:
            await self._connection.execute(
                "ALTER TABLE raw_messages ADD COLUMN account_session TEXT DEFAULT ''"
            )
            await self._connection.commit()
            logger.info("Миграция: raw_messages.account_session добавлен")

    async def backfill_raw_account_session(
        self, sender_to_session: dict[int, str],
    ) -> int:
        """Размечает исторические RAW (``account_session=''``) по аккаунтам.

        Каждый аккаунт слушает ОДИН и тот же чат Лео, но пространства
        ``telegram_message_id`` у аккаунтов свои (у нас ``dvai`` ~708k, ``dvai_2``
        ~22k). До появления колонки лента показывала сообщения обоих аккаунтов
        сразу — «супер-чат», не совпадающий ни с одним Telegram-чатом.

        Разметка legacy:
          * исходящие — по ``sender_id`` (это user id аккаунта);
          * входящие — по ближайшему диапазону id исходящих аккаунта
            (диапазоны разнесены, разрыв огромный — привязка однозначна).

        ``sender_to_session`` строится в ``main.py`` из ``client.get_me()``.
        No-op для однопользовательских конфигов / когда размечено нечего.
        """
        if not sender_to_session:
            return 0

        cursor = await self._connection.execute(
            """SELECT sender_id, MIN(telegram_message_id), MAX(telegram_message_id)
               FROM raw_messages WHERE is_outgoing = 1 GROUP BY sender_id"""
        )
        ranges: list[tuple[str, int, int]] = []
        for sender_id, mn, mx in await cursor.fetchall():
            session = sender_to_session.get(int(sender_id or 0))
            if session:
                ranges.append((session, int(mn), int(mx)))
        if not ranges:
            return 0

        def _nearest(tm: int) -> str | None:
            best: str | None = None
            best_dist: int | None = None
            for session, mn, mx in ranges:
                dist = 0 if mn <= tm <= mx else min(abs(tm - mn), abs(tm - mx))
                if best_dist is None or dist < best_dist:
                    best, best_dist = session, dist
            return best

        cursor = await self._connection.execute(
            """SELECT id, telegram_message_id, is_outgoing, sender_id
               FROM raw_messages WHERE account_session IS NULL OR account_session = ''"""
        )
        updates: list[tuple[str, int]] = []
        for row_id, tm, is_out, sender_id in await cursor.fetchall():
            session = None
            if is_out:
                session = sender_to_session.get(int(sender_id or 0))
            if session is None:
                session = _nearest(int(tm or 0))
            if session:
                updates.append((session, int(row_id)))
        if not updates:
            return 0

        await self._connection.executemany(
            "UPDATE raw_messages SET account_session = ? WHERE id = ?", updates
        )
        await self._connection.commit()
        logger.info(
            f"Миграция: raw_messages.account_session размечен "
            f"({len(updates)} исторических строк)"
        )
        return len(updates)


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

    async def _ensure_raw_is_outgoing_column(self) -> None:
        """Добавляет raw_messages.is_outgoing и проставляет его истории.

        Коллектор логирует ВЕСЬ чат: и входящие от Leo, и исходящие от нас
        (``_handle_outgoing_message``). Без явного флага лента не может
        отличить «Leo написал» от «мы отправили 👎» и рисует наше собственное
        действие как входящее сообщение от бота.

        Для существующих баз флаг восстанавливается по инварианту личных
        чатов с ботом: входящие сообщения приходят от ``sender_id == chat_id``
        (id бота = id чата), исходящие — от нашего пользователя. Групповые
        чаты (sender_id != chat_id) тоже считаются исходящими, но их в ленте
        Дайвинчика нет, поэтому точность тут не критична.
        """
        cursor = await self._connection.execute("PRAGMA table_info(raw_messages)")
        rows = await cursor.fetchall()
        columns = {row[1] for row in rows}
        if "is_outgoing" not in columns:
            await self._connection.execute(
                "ALTER TABLE raw_messages ADD COLUMN is_outgoing INTEGER DEFAULT 0"
            )
            await self._connection.execute(
                "UPDATE raw_messages SET is_outgoing = 1 WHERE sender_id <> chat_id"
            )
            await self._connection.commit()
            logger.info("Миграция: raw_messages.is_outgoing добавлен (история помечена)")

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
        is_outgoing: bool = False,
        account_session: str = "",
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

        ``is_outgoing=True`` ставится перехватчиком исходящих: коллектор
        логирует весь чат, и без флага лента рисует наш собственный 👎
        как входящее сообщение от Leo.

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
                     media_type, reply_to_message_id, received_at, is_outgoing,
                     account_session)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        telegram_message_id, chat_id, sender_id, sender_username,
                        sender_name, message_date, text, raw_entities,
                        reply_markup, media_type, reply_to_message_id, received_at,
                        1 if is_outgoing else 0, account_session,
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
        self,
        chat_id: int,
        telegram_message_id: int,
        account_session: str | None = None,
    ) -> dict | None:
        """Ищет профиль по telegram_message_id.

        ``account_session`` (когда задан) ограничивает поиск сообщениями ТОГО
        же аккаунта: у аккаунтов своё пространство message id в одном чате,
        и без фильтра один id мог резолвиться в анкету другого аккаунта.
        """
        sql = (
            """SELECT p.* FROM profiles p
            JOIN profile_messages pm ON pm.profile_id = p.id
            WHERE pm.chat_id = ? AND pm.telegram_message_id = ?"""
        )
        params: list[object] = [chat_id, telegram_message_id]
        if account_session:
            sql += " AND (pm.account_session = ? OR pm.account_session = '')"
            params.append(account_session)
        cursor = await self._connection.execute(sql, tuple(params))
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(cursor, row)

    async def set_chat_profile_context(
        self,
        chat_id: int,
        profile_message_id: int,
        account_session: str = "",
    ) -> None:
        """Сохраняет контекст чата: какое PROFILE-сообщение «текущее».

        Контекст строго на аккаунт (``account_session``): у аккаунтов свой чат с
        Leo в одном ``chat_id``, общий контекст связывал MEDIA_ONLY с карточкой
        другого аккаунта. ON CONFLICT обновляет «текущий» профиль аккаунта.
        """
        now = datetime.now(timezone.utc).isoformat()
        await self._connection.execute(
            """INSERT INTO chat_context
            (account_session, chat_id, profile_message_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(account_session, chat_id) DO UPDATE SET
                profile_message_id = excluded.profile_message_id,
                updated_at = excluded.updated_at""",
            (account_session or "", chat_id, profile_message_id, now),
        )
        await self._connection.commit()

    async def get_chat_profile_context(
        self, chat_id: int, account_session: str = "",
    ) -> int | None:
        """Возвращает profile_message_id «текущего» профиля аккаунта или None."""
        cursor = await self._connection.execute(
            "SELECT profile_message_id FROM chat_context "
            "WHERE account_session = ? AND chat_id = ?",
            (account_session or "", chat_id),
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
        self,
        profile_message_id: int,
        account_session: str | None = None,
    ) -> int | None:
        """Находит профиль по telegram_message_id его карточки (chat_context)."""
        try:
            sql = (
                "SELECT id FROM profiles "
                "WHERE source_message_id = ? ORDER BY id DESC LIMIT 1"
            )
            params: list[object] = [profile_message_id]
            if account_session:
                sql = (
                    "SELECT p.id FROM profiles p "
                    "JOIN profile_messages pm ON pm.profile_id = p.id "
                    "WHERE p.source_message_id = ? "
                    "AND (pm.account_session = ? OR pm.account_session = '') "
                    "ORDER BY p.id DESC LIMIT 1"
                )
                params.append(account_session)
            cursor = await self._connection.execute(sql, tuple(params))
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
                # Отклонённые Leo лайки (rejected=1, дневной лимит) не флипаем —
                # они не дошли и «ответить» на них нельзя.
                await self._connection.execute(
                    """UPDATE like_outcomes
                    SET responded = 1, responded_at = ?
                    WHERE profile_id = ? AND responded = 0 AND rejected = 0""",
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

    async def mark_like_rejected(self, chat_id: int, window_sec: int = 120) -> None:
        """Помечает сбойный лайк как rejected (Leo отклонил из-за лимита).

        Leo отвечает на отклонённый ❤️ сообщением «Слишком много ❤️ за сегодня» —
        такой лайк не дошёл и не должен считаться в аналитике. Помечаем rejected=1
        у самого свежего неотклонённого лайка этого чата в пределах окна
        ``window_sec`` (по default 120 c — Leo отвечает сразу). Работает для
        обоих источников (авто/ручные), т.к. лайк уже записан в like_outcomes.
        Повторные отказы подряд помечают предыдущие лайки по очереди.
        """
        cutoff = (
            datetime.now(timezone.utc).timestamp() - int(window_sec)
        )
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        try:
            cursor = await self._connection.execute(
                """SELECT id FROM like_outcomes
                WHERE chat_id = ? AND rejected = 0 AND liked_at >= ?
                ORDER BY liked_at DESC, id DESC LIMIT 1""",
                (chat_id, cutoff_iso),
            )
            row = await cursor.fetchone()
            if row is None:
                return
            await self._connection.execute(
                "UPDATE like_outcomes SET rejected = 1 WHERE id = ?",
                (row[0],),
            )
            await self._connection.commit()
        except Exception as e:
            await self._connection.rollback()
            logger.warning(f"mark_like_rejected: {e}")

    async def save_photo_score(
        self,
        *,
        profile_id: int,
        account_session: str,
        chat_id: int,
        telegram_message_id: int,
        score: float,
        verdict: str,
        smoke_prob: float | None,
        alcohol_prob: float | None,
        n_photos: int,
        model_version: str,
    ) -> None:
        """Теневой вердикт модели вкуса на карточку (повтор карточки — no-op)."""
        try:
            await self._connection.execute(
                """INSERT OR IGNORE INTO photo_scores
                (profile_id, account_session, chat_id, telegram_message_id, score,
                 verdict, smoke_prob, alcohol_prob, n_photos, model_version, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    profile_id, account_session or "", chat_id, telegram_message_id,
                    score, verdict, smoke_prob, alcohol_prob, n_photos, model_version,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            await self._connection.commit()
        except Exception as e:
            await self._connection.rollback()
            logger.warning(f"save_photo_score(profile={profile_id}): {e}")

    #: Альбом одного показа анкеты — сообщения с номерами подряд (как в
    #: web/db.SyncDB.RATE_ALBUM_GAP): больший разрыв — предыдущий показ.
    PHOTO_ALBUM_GAP = 2

    async def save_photo_embedding(
        self, profile_id: int, emb: bytes | None, n_photos: int, model: str,
    ) -> None:
        """Отпечаток фото анкеты (перезаписывается свежим показом)."""
        try:
            await self._connection.execute(
                """INSERT OR REPLACE INTO photo_embeddings
                (profile_id, emb, n_photos, model, created_at) VALUES (?, ?, ?, ?, ?)""",
                (profile_id, emb, n_photos, model, datetime.now(timezone.utc).isoformat()),
            )
            await self._connection.commit()
        except Exception as e:
            await self._connection.rollback()
            logger.warning(f"save_photo_embedding(profile={profile_id}): {e}")

    async def get_photo_embedding(self, profile_id: int) -> bytes | None:
        """Отпечаток фото анкеты (float32 LE) или None."""
        try:
            cur = await self._connection.execute(
                "SELECT emb FROM photo_embeddings WHERE profile_id = ?", (profile_id,)
            )
            row = await cur.fetchone()
            return row[0] if row and row[0] else None
        except Exception as e:
            logger.warning(f"get_photo_embedding(profile={profile_id}): {e}")
            return None

    async def save_photo_item_embeddings(
        self,
        profile_id: int,
        account_session: str,
        items: list[tuple[int, bytes | None]],
        model: str,
    ) -> None:
        """Отпечатки отдельных фото анкеты: ``[(telegram_message_id, emb|None)]``."""
        if not items:
            return
        now = datetime.now(timezone.utc).isoformat()
        try:
            await self._connection.executemany(
                """INSERT OR REPLACE INTO photo_item_embeddings
                (profile_id, telegram_message_id, account_session, emb, model, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (profile_id, int(mid), account_session or "", emb, model, now)
                    for mid, emb in items
                ],
            )
            await self._connection.commit()
        except Exception as e:
            await self._connection.rollback()
            logger.warning(f"save_photo_item_embeddings(profile={profile_id}): {e}")

    async def get_photo_item_embeddings(
        self, profile_id: int, limit: int = 3,
    ) -> list[bytes]:
        """Отпечатки фото анкеты (первые ``limit`` фото последнего показа)."""
        try:
            cur = await self._connection.execute(
                """SELECT telegram_message_id, emb FROM photo_item_embeddings
                WHERE profile_id = ? AND emb IS NOT NULL
                ORDER BY telegram_message_id""",
                (profile_id,),
            )
            rows = await cur.fetchall()
        except Exception as e:
            logger.warning(f"get_photo_item_embeddings(profile={profile_id}): {e}")
            return []
        if not rows:
            return []
        # Последний показ — хвост с номерами подряд (как альбом на /rate).
        start = len(rows) - 1
        while start > 0 and rows[start][0] - rows[start - 1][0] <= self.PHOTO_ALBUM_GAP:
            start -= 1
        return [r[1] for r in rows[start:]][: max(1, int(limit))]

    #: Сколько фото анкеты брать в досчёт (= SyncDB.RATE_MAX_PHOTOS: все фото,
    #: что видны на /rate, должны иметь отпечаток для обучения).
    PHOTO_ITEMS_MAX = 4

    async def get_photo_backfill_batch(
        self, limit: int = 10, max_photos: int = 3,
    ) -> list[dict]:
        """Анкеты, которым нужен досчёт отпечатков фото.

        Порядок: (1) есть оценённые на /rate фото без отпечатка — нужны для
        обучения; (2) нет отпечатка анкеты — сначала НЕоценённые (очередь
        «модель сомневается»), затем оценённые; (3) нет отпечатков отдельных
        фото (пересчёт старых анкет под оценку по фото).

        Фото — первые ``PHOTO_ITEMS_MAX`` фото последнего показа с одного
        аккаунта (та же логика, что у /rate); вердикт бот считает по первым
        ``max_photos`` (как живой расчёт):
        ``[{"profile_id", "photos": [{"message_id", "chat_id", "account_session"}]}]``.
        """
        photo_sql = """
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
        rated_missing = """(SELECT r.profile_id FROM photo_item_ratings r
            LEFT JOIN photo_item_embeddings e
              ON e.profile_id = r.profile_id
             AND e.telegram_message_id = r.telegram_message_id
            WHERE e.profile_id IS NULL)"""
        cur = await self._connection.execute(
            "SELECT DISTINCT pm.profile_id " + photo_sql
            + f""" AND (pm.profile_id NOT IN (SELECT profile_id FROM photo_embeddings)
                 OR pm.profile_id NOT IN (SELECT profile_id FROM photo_item_embeddings)
                 OR pm.profile_id IN {rated_missing})
            ORDER BY (pm.profile_id IN {rated_missing}) DESC,
                     (pm.profile_id IN (SELECT profile_id FROM photo_embeddings)),
                     (pm.profile_id IN (SELECT profile_id FROM photo_ratings)),
                     pm.profile_id DESC
            LIMIT ?""",
            (int(limit),),
        )
        pids = [r[0] for r in await cur.fetchall()]
        out: list[dict] = []
        for pid in pids:
            cur = await self._connection.execute(
                "SELECT pm.telegram_message_id, pm.chat_id, pm.account_session "
                + photo_sql + " AND pm.profile_id = ? ORDER BY pm.telegram_message_id",
                (pid,),
            )
            rows = await cur.fetchall()
            if not rows:
                continue
            session = rows[-1][2]
            same = [r for r in rows if r[2] == session]
            start = len(same) - 1
            while start > 0 and same[start][0] - same[start - 1][0] <= self.PHOTO_ALBUM_GAP:
                start -= 1
            photos = [
                {"message_id": r[0], "chat_id": r[1], "account_session": r[2]}
                # Первые фото альбома — как у живого теневого расчёта
                # (DvinchikCollector._card_photos) и как на /rate.
                for r in same[start:][: max(self.PHOTO_ITEMS_MAX, int(max_photos))]
            ]
            out.append({"profile_id": pid, "photos": photos})
        return out

    async def set_state(self, key: str, value: str) -> None:
        """Сохраняет рантайм-значение (переживает рестарт)."""
        try:
            await self._connection.execute(
                """INSERT INTO app_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at""",
                (key, value, datetime.now(timezone.utc).isoformat()),
            )
            await self._connection.commit()
        except Exception as e:
            await self._connection.rollback()
            logger.warning(f"set_state({key}): {e}")

    async def get_state(self, key: str) -> str | None:
        """Читает рантайм-значение (None, если не задано)."""
        try:
            cursor = await self._connection.execute(
                "SELECT value FROM app_state WHERE key = ?", (key,)
            )
            row = await cursor.fetchone()
            return row[0] if row else None
        except Exception as e:
            logger.warning(f"get_state({key}): {e}")
            return None

    # ── Автопереписка с матчами (match_dialogs) ──────────────────────

    #: Поля match_dialogs, которые можно менять через update_match_dialog.
    _MATCH_DIALOG_FIELDS: frozenset[str] = frozenset({
        "username", "name", "profile_id", "status", "replied_at", "due_at",
        "sent_at", "pinned", "error",
    })

    async def add_match_dialog(
        self,
        account_session: str,
        user_id: int,
        *,
        username: str = "",
        name: str = "",
        profile_id: int | None = None,
        greeted_at: str | None = None,
    ) -> bool:
        """Заводит переписку (WAITING_REPLY). False — уже была (не трогаем)."""
        try:
            cursor = await self._connection.execute(
                """INSERT OR IGNORE INTO match_dialogs
                (account_session, user_id, username, name, profile_id,
                 status, greeted_at)
                VALUES (?, ?, ?, ?, ?, 'WAITING_REPLY', ?)""",
                (
                    account_session or "", int(user_id), username or "",
                    name or "", profile_id,
                    greeted_at or datetime.now(timezone.utc).isoformat(),
                ),
            )
            await self._connection.commit()
            return (cursor.rowcount or 0) > 0
        except Exception as e:
            await self._connection.rollback()
            logger.warning(f"add_match_dialog: {e}")
            return False

    async def get_match_dialog(
        self, account_session: str, user_id: int,
    ) -> dict | None:
        """Переписка с девушкой на аккаунте или None."""
        cursor = await self._connection.execute(
            "SELECT * FROM match_dialogs WHERE account_session = ? AND user_id = ?",
            (account_session or "", int(user_id)),
        )
        row = await cursor.fetchone()
        return self._row_to_dict(cursor, row) if row else None

    async def update_match_dialog(
        self, account_session: str, user_id: int, **fields: object,
    ) -> None:
        """Обновляет поля переписки (только из _MATCH_DIALOG_FIELDS)."""
        bad = set(fields) - self._MATCH_DIALOG_FIELDS
        if bad:
            msg = f"update_match_dialog: неизвестные поля {sorted(bad)}"
            raise ValueError(msg)
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        try:
            await self._connection.execute(
                f"UPDATE match_dialogs SET {cols} "
                "WHERE account_session = ? AND user_id = ?",
                (*fields.values(), account_session or "", int(user_id)),
            )
            await self._connection.commit()
        except Exception as e:
            await self._connection.rollback()
            logger.warning(f"update_match_dialog: {e}")

    async def list_match_dialogs(
        self, account_session: str, status: str, limit: int = 50,
    ) -> list[dict]:
        """Переписки аккаунта в статусе (старые первыми)."""
        cursor = await self._connection.execute(
            """SELECT * FROM match_dialogs
            WHERE account_session = ? AND status = ?
            ORDER BY greeted_at LIMIT ?""",
            (account_session or "", status, int(limit)),
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(cursor, row) for row in rows]

    # ── Логирование переписок с матчами (dialog_contacts/messages) ───

    async def upsert_dialog_contact(
        self,
        account_session: str,
        user_id: int,
        *,
        username: str = "",
        name: str = "",
        profile_id: int | None = None,
        matched_at: str | None = None,
    ) -> bool:
        """Заводит девушку в реестр переписок. True — новая.

        Повторный матч дополняет пустые поля (username/имя/анкета), дату
        первого матча не трогает.
        """
        try:
            cursor = await self._connection.execute(
                """INSERT OR IGNORE INTO dialog_contacts
                (account_session, user_id, username, name, profile_id, matched_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    account_session or "", int(user_id), username or "",
                    name or "", profile_id,
                    matched_at or datetime.now(timezone.utc).isoformat(),
                ),
            )
            added = (cursor.rowcount or 0) > 0
            if not added:
                await self._connection.execute(
                    """UPDATE dialog_contacts SET
                        username = CASE WHEN ? <> '' THEN ? ELSE username END,
                        name = CASE WHEN name = '' THEN ? ELSE name END,
                        profile_id = COALESCE(profile_id, ?)
                    WHERE account_session = ? AND user_id = ?""",
                    (
                        username or "", username or "", name or "", profile_id,
                        account_session or "", int(user_id),
                    ),
                )
            await self._connection.commit()
            return added
        except Exception as e:
            await self._connection.rollback()
            logger.warning(f"upsert_dialog_contact: {e}")
            return False

    async def get_dialog_contact_ids(self, account_session: str) -> set[int]:
        """user_id всех девушек аккаунта, чьи переписки логируются."""
        cursor = await self._connection.execute(
            "SELECT user_id FROM dialog_contacts WHERE account_session = ?",
            (account_session or "",),
        )
        return {int(r[0]) for r in await cursor.fetchall()}

    async def get_dialog_contact_usernames(self, account_session: str) -> set[str]:
        """@username (нижний регистр) девушек аккаунта в реестре."""
        cursor = await self._connection.execute(
            "SELECT username FROM dialog_contacts "
            "WHERE account_session = ? AND username <> ''",
            (account_session or "",),
        )
        return {str(r[0]).lower() for r in await cursor.fetchall()}

    async def save_dialog_message(
        self,
        account_session: str,
        user_id: int,
        telegram_message_id: int,
        *,
        is_outgoing: bool,
        sent_by: str,
        message_date: str,
        text: str = "",
        media_type: str = "",
        is_forward: bool = False,
        reply_to_message_id: int | None = None,
    ) -> bool:
        """Сохраняет сообщение переписки. True — новое.

        Дубликат (событие + сверка истории) схлопывается по
        (аккаунт, девушка, message id). Метка ``bot`` побеждает: бот пишет свои
        отправки сам, а эхо-событие могло успеть записать их как ``owner``.
        """
        try:
            cursor = await self._connection.execute(
                """INSERT OR IGNORE INTO dialog_messages
                (account_session, user_id, telegram_message_id, is_outgoing,
                 sent_by, message_date, text, media_type, is_forward,
                 reply_to_message_id, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    account_session or "", int(user_id),
                    int(telegram_message_id), int(bool(is_outgoing)),
                    sent_by or "", message_date, text or "", media_type or "",
                    int(bool(is_forward)), reply_to_message_id,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            added = (cursor.rowcount or 0) > 0
            if not added and sent_by == "bot":
                await self._connection.execute(
                    """UPDATE dialog_messages SET sent_by = 'bot'
                    WHERE account_session = ? AND user_id = ?
                      AND telegram_message_id = ?""",
                    (account_session or "", int(user_id), int(telegram_message_id)),
                )
            if added:
                await self._connection.execute(
                    """UPDATE dialog_contacts SET last_message_at = ?
                    WHERE account_session = ? AND user_id = ?
                      AND (last_message_at IS NULL OR last_message_at < ?)""",
                    (
                        message_date, account_session or "", int(user_id),
                        message_date,
                    ),
                )
            await self._connection.commit()
            return added
        except Exception as e:
            await self._connection.rollback()
            logger.warning(f"save_dialog_message: {e}")
            return False

    async def get_dialog_sync_queue(
        self, account_session: str, *, older_than: str, limit: int = 15,
    ) -> list[dict]:
        """Девушки на сверку истории: давно не сверялись (никогда — первыми).

        ``last_tm_id`` — последний сохранённый message id её чата (сверка
        качает только новее).
        """
        cursor = await self._connection.execute(
            """SELECT c.*, (
                    SELECT MAX(m.telegram_message_id) FROM dialog_messages m
                    WHERE m.account_session = c.account_session
                      AND m.user_id = c.user_id
                ) AS last_tm_id
            FROM dialog_contacts c
            WHERE c.account_session = ?
              AND (c.synced_at IS NULL OR c.synced_at < ?)
            ORDER BY c.synced_at IS NOT NULL, c.synced_at, c.matched_at DESC
            LIMIT ?""",
            (account_session or "", older_than, int(limit)),
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(cursor, row) for row in rows]

    async def mark_dialog_synced(
        self, account_session: str, user_id: int, at: str | None = None,
    ) -> None:
        """Отметка «её чат сверен с историей Telegram»."""
        try:
            await self._connection.execute(
                "UPDATE dialog_contacts SET synced_at = ? "
                "WHERE account_session = ? AND user_id = ?",
                (
                    at or datetime.now(timezone.utc).isoformat(),
                    account_session or "", int(user_id),
                ),
            )
            await self._connection.commit()
        except Exception as e:
            await self._connection.rollback()
            logger.warning(f"mark_dialog_synced: {e}")

    async def list_untracked_matches(
        self, account_session: str, limit: int = 20,
    ) -> list[dict]:
        """Старые взаимные лайки аккаунта с @username, которых нет в реестре.

        Аккаунт матча — по входящему сообщению Leo с тем же id (у аккаунтов
        свои пространства id). Только с @username: без него user_id не узнать.
        """
        known = await self.get_dialog_contact_usernames(account_session)
        cursor = await self._connection.execute(
            """SELECT mr.name, mr.telegram_username, mr.profile_id,
                      MIN(mr.responded_at) AS matched_at
            FROM match_responses mr
            JOIN raw_messages rm
              ON rm.chat_id = mr.chat_id
             AND rm.telegram_message_id = mr.telegram_message_id
             AND rm.is_outgoing = 0
            WHERE rm.account_session = ? AND mr.telegram_username <> ''
            GROUP BY lower(mr.telegram_username)
            ORDER BY matched_at DESC""",
            (account_session or "",),
        )
        rows = [self._row_to_dict(cursor, r) for r in await cursor.fetchall()]
        fresh = [
            r for r in rows
            if str(r["telegram_username"]).lower() not in known
        ]
        return fresh[: int(limit)]

    async def find_match_profile(
        self,
        name: str,
        chat_id: int,
        account_session: str = "",
        *,
        use_context: bool = True,
    ) -> int | None:
        """Анкета девушки, с которой случился взаимный лайк.

        Сначала «текущая» карточка чата аккаунта (MATCH приходит сразу после
        её анкеты), если имя совпадает; иначе — по имени среди лайкнутых.
        """
        needle = (name or "").strip().lower()
        if use_context:
            try:
                ctx = await self.get_chat_profile_context(chat_id, account_session)
                pid = (
                    await self.resolve_profile_by_message(ctx, account_session)
                    if ctx is not None else None
                )
                if pid is not None:
                    cursor = await self._connection.execute(
                        "SELECT name FROM profiles WHERE id = ?", (pid,)
                    )
                    row = await cursor.fetchone()
                    if row and (row[0] or "").strip().lower() == needle:
                        return pid
            except Exception as e:
                logger.warning(f"find_match_profile(context): {e}")
        return await self._resolve_profile_by_name(name)

    async def get_profile_card_message_ids(
        self, profile_id: int, account_session: str, window: int = 10,
    ) -> list[int]:
        """telegram_message_id последнего показа анкеты на аккаунте.

        Альбом анкеты — подряд идущие id: берём самую свежую карточку с
        текстом (подпись анкеты) и её фото в пределах ``window`` id. Только
        id ЭТОГО аккаунта (у аккаунтов свои пространства id в чате Leo).
        """
        cursor = await self._connection.execute(
            """SELECT pm.telegram_message_id,
                COALESCE((SELECT rm.text FROM raw_messages rm
                    WHERE rm.chat_id = pm.chat_id
                      AND rm.telegram_message_id = pm.telegram_message_id
                      AND rm.account_session = pm.account_session
                      AND rm.is_outgoing = 0 LIMIT 1), '') AS text
            FROM profile_messages pm
            WHERE pm.profile_id = ? AND pm.account_session = ?
            ORDER BY pm.telegram_message_id""",
            (profile_id, account_session or ""),
        )
        rows = [(int(r[0]), r[1] or "") for r in await cursor.fetchall()]
        if not rows:
            return []
        with_text = [mid for mid, text in rows if text.strip()]
        anchor = with_text[-1] if with_text else rows[-1][0]
        ids = sorted({mid for mid, _ in rows if abs(mid - anchor) <= window})
        return ids[:window]

    async def delete_state(self, key: str) -> None:
        """Удаляет рантайм-значение."""
        try:
            await self._connection.execute(
                "DELETE FROM app_state WHERE key = ?", (key,)
            )
            await self._connection.commit()
        except Exception as e:
            await self._connection.rollback()
            logger.warning(f"delete_state({key}): {e}")

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
                       WHERE sub.profile_id = p.id AND sub.rejected = 0
                         AND sub.message_text != ''
                       ORDER BY sub.liked_at DESC, sub.id DESC LIMIT 1
                      ) AS last_text
            FROM like_outcomes o
            JOIN profiles p ON p.id = o.profile_id
            WHERE o.rejected = 0
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
        account_session: str = '',
    ) -> None:
        """Связывает профиль с Telegram-сообщением.

        ``account_session`` — сессия аккаунта, который реально ПОЛУЧИЛ это
        сообщение (message_id в диалоге с Leo у каждого аккаунта своя
        нумерация). Нужен вебу, чтобы качать фото тем же аккаунтом.
        """
        await self._connection.execute(
            """INSERT OR IGNORE INTO profile_messages
            (profile_id, telegram_message_id, chat_id, account_session, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            (profile_id, telegram_message_id, chat_id, account_session, created_at),
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

    # ── Analytics queries (Stage 6, READ ONLY) ───────────────────────

    async def get_all_ai_decisions(self) -> list[dict]:
        """Получает все AI-решения (для analytics/экспорта)."""
        cursor = await self._connection.execute(
            """SELECT * FROM ai_decisions ORDER BY evaluated_at ASC, id ASC"""
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(cursor, row) for row in rows]

    # ── Экспорт анализа всех анкет (Stage 11) ─────────────────────────

    async def get_profiles_for_analysis(self) -> list[dict]:
        """Возвращает все анкеты с данными для экспорта/анализа.

        Каждая строка: профиль + последний AI-вердикт (decision/score/reasons),
        последний фильтр, ручное решение, агрегированные авто-действия
        (LIKE/DISLIKE/MESSAGE по профилю), последнее ручное сообщение,
        метка взаимного лайка (matched) и ответ на лайк (responded).
        """
        cursor = await self._connection.execute(
            """SELECT
                p.id AS profile_id,
                p.name,
                p.age,
                p.raw_city AS city,
                p.description,
                p.status,
                p.first_seen_at,
                p.last_seen_at,

                ad.decision AS ai_decision,
                ad.combined_score AS ai_score,
                ad.confidence AS ai_confidence,
                ad.reasons AS ai_reasons,
                ad.evaluated_at AS ai_evaluated_at,

                fr.decision AS filter_decision,

                hd.decision AS human_decision,
                hd.agreement AS human_agreement,
                hd.created_at AS human_decided_at,

                aal.liked AS auto_liked,
                aal.disliked AS auto_disliked,
                aal.message_text AS auto_message_text,
                aal.first_action AS auto_first_action,
                aal.first_action_at AS auto_first_action_at,

                sm.liked AS manual_liked,
                sm.disliked AS manual_disliked,
                sm.action AS manual_action,
                sm.text AS manual_text,
                sm.sent_at AS manual_at,

                CASE WHEN mr.profile_id IS NOT NULL THEN 1 ELSE 0 END AS matched,

                lo.responded AS responded

            FROM profiles p
            LEFT JOIN ai_decisions ad ON ad.id = (
                SELECT id FROM ai_decisions WHERE profile_id = p.id
                ORDER BY evaluated_at DESC LIMIT 1
            )
            LEFT JOIN filter_results fr ON fr.id = (
                SELECT id FROM filter_results WHERE profile_id = p.id
                ORDER BY evaluated_at DESC LIMIT 1
            )
            LEFT JOIN human_decisions hd ON hd.ai_decision_id = ad.id

            -- Авто-действия (агрегация по профилю): флаги LIKE/DISLIKE,
            -- текст последнего сообщения и первое действие.
            LEFT JOIN (
                SELECT a.profile_id,
                       MAX(CASE WHEN a.action = 'LIKE' THEN 1 ELSE 0 END) AS liked,
                       MAX(CASE WHEN a.action = 'DISLIKE' THEN 1 ELSE 0 END) AS disliked,
                       (SELECT a2.message_text FROM auto_actions_log a2
                        WHERE a2.profile_id = a.profile_id
                          AND a2.action = 'MESSAGE'
                          AND a2.message_text != ''
                        ORDER BY a2.sent_at DESC, a2.id DESC LIMIT 1) AS message_text,
                       (SELECT a3.action FROM auto_actions_log a3
                        WHERE a3.profile_id = a.profile_id
                        ORDER BY a3.sent_at ASC, a3.id ASC LIMIT 1) AS first_action,
                       (SELECT a3.sent_at FROM auto_actions_log a3
                        WHERE a3.profile_id = a.profile_id
                        ORDER BY a3.sent_at ASC, a3.id ASC LIMIT 1) AS first_action_at
                FROM auto_actions_log a
                GROUP BY a.profile_id
            ) aal ON aal.profile_id = p.id

            -- Последнее ручное сообщение (sent_messages) + флаги LIKE/DISLIKE.
            LEFT JOIN (
                SELECT s.profile_id, s.action, s.text, s.sent_at,
                       mx.liked, mx.disliked
                FROM sent_messages s
                JOIN (
                    SELECT profile_id,
                           MAX(CASE WHEN action = 'LIKE' THEN 1 ELSE 0 END) AS liked,
                           MAX(CASE WHEN action = 'DISLIKE' THEN 1 ELSE 0 END) AS disliked,
                           MAX(sent_at) AS max_at
                    FROM sent_messages GROUP BY profile_id
                ) mx ON mx.profile_id = s.profile_id AND mx.max_at = s.sent_at
            ) sm ON sm.profile_id = p.id

            -- Взаимный лайк (матч)
            LEFT JOIN (
                SELECT DISTINCT profile_id FROM match_responses
            ) mr ON mr.profile_id = p.id

            -- Ответ на лайк (like_outcomes)
            LEFT JOIN (
                SELECT o.profile_id, MAX(o.responded) AS responded
                FROM like_outcomes o
                WHERE o.rejected = 0
                GROUP BY o.profile_id
            ) lo ON lo.profile_id = p.id

            ORDER BY p.last_seen_at DESC, p.id DESC"""
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(cursor, row) for row in rows]

    # ========================= Память капч (Stage 7.6) =========================

    async def record_pending_captcha(
        self, signature: str, message_text: str, buttons_json: str = "[]"
    ) -> bool:
        """Записывает (или освежает) капчу с неизвестным ответом.

        Неизвестная капча выводится в блок «Капчи ждут ответа» на главном
        экране сайта (/). Повторное появление
        той же капчи обновляет текст/кнопки, но НЕ затирает уже выученный
        ответ (UPSERT только для строк с answer IS NULL).
        """
        now = datetime.now(timezone.utc).isoformat()
        try:
            cursor = await self._connection.execute(
                """INSERT INTO captcha_memory
                       (signature, message_text, buttons_json, answer, created_at, updated_at)
                   VALUES (?, ?, ?, NULL, ?, ?)
                   ON CONFLICT(signature) DO UPDATE SET
                       message_text = excluded.message_text,
                       buttons_json = excluded.buttons_json,
                       updated_at = excluded.updated_at
                   WHERE answer IS NULL""",
                (signature, message_text, buttons_json, now, now),
            )
            await self._connection.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"record_pending_captcha: {e}")
            return False

    async def get_captcha_answer(self, signature: str) -> str | None:
        """Возвращает выученный ответ для капчи с указанной сигнатурой."""
        try:
            cursor = await self._connection.execute(
                "SELECT answer FROM captcha_memory WHERE signature = ?",
                (signature,),
            )
            row = await cursor.fetchone()
            if row is None or row[0] is None:
                return None
            return str(row[0])
        except Exception as e:
            logger.error(f"get_captcha_answer: {e}")
            return None

    async def get_pending_captchas(self, limit: int = 50) -> list[dict]:
        """Неизвестные капчи, ждущие ответа владельца (свежие сначала)."""
        try:
            cursor = await self._connection.execute(
                """SELECT * FROM captcha_memory
                   WHERE answer IS NULL
                   ORDER BY updated_at DESC, id DESC
                   LIMIT ?""",
                (int(limit),),
            )
            rows = await cursor.fetchall()
            return [self._row_to_dict(cursor, row) for row in rows]
        except Exception as e:
            logger.error(f"get_pending_captchas: {e}")
            return []

    async def get_known_captchas(self, limit: int = 50) -> list[dict]:
        """Капчи с выученными ответами (свежие сначала)."""
        try:
            cursor = await self._connection.execute(
                """SELECT * FROM captcha_memory
                   WHERE answer IS NOT NULL
                   ORDER BY updated_at DESC, id DESC
                   LIMIT ?""",
                (int(limit),),
            )
            rows = await cursor.fetchall()
            return [self._row_to_dict(cursor, row) for row in rows]
        except Exception as e:
            logger.error(f"get_known_captchas: {e}")
            return []

    async def get_captcha_by_id(self, row_id: int) -> dict | None:
        """Одна запись captcha_memory по id (для веб-ответа на капчу)."""
        try:
            cursor = await self._connection.execute(
                "SELECT * FROM captcha_memory WHERE id = ?",
                (int(row_id),),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return self._row_to_dict(cursor, row)
        except Exception as e:
            logger.error(f"get_captcha_by_id: {e}")
            return None

    async def set_captcha_answer(self, row_id: int, answer: str) -> bool:
        """Сохраняет ответ владельца на капчу (в этот раз уже известна)."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            cursor = await self._connection.execute(
                """UPDATE captcha_memory
                   SET answer = ?, answered_at = ?, updated_at = ?
                   WHERE id = ?""",
                (answer.strip(), now, now, int(row_id)),
            )
            await self._connection.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"set_captcha_answer: {e}")
            return False

    async def mark_captcha_used(self, signature: str) -> bool:
        """Инкрементирует счётчик применений выученного ответа."""
        try:
            cursor = await self._connection.execute(
                """UPDATE captcha_memory
                   SET used_count = used_count + 1, updated_at = ?
                   WHERE signature = ?""",
                (datetime.now(timezone.utc).isoformat(), signature),
            )
            await self._connection.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"mark_captcha_used: {e}")
            return False

    async def delete_captcha(self, row_id: int) -> bool:
        """Удаляет запись captcha_memory из памяти капч."""
        try:
            cursor = await self._connection.execute(
                "DELETE FROM captcha_memory WHERE id = ?",
                (int(row_id),),
            )
            await self._connection.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"delete_captcha: {e}")
            return False

    async def clear_all_data(self) -> list[str]:
        """Полностью очищает таблицы (после экспорта/бэкапа).

        Порядок удаления — по зависимостям (FK). Возвращает список
        очищенных таблиц. Используется CLI-флагом ``--clear-db``
        (см. services/analysis_export.py).
        """
        tables = [
            "human_decisions",
            "auto_actions_log",
            "filter_results",
            "ai_decisions",
            "profile_messages",
            "sent_messages",
            "match_responses",
            "like_outcomes",
            "chat_context",
            "profiles",
            "raw_messages",
            "captcha_memory",
        ]
        for table in tables:
            try:
                await self._connection.execute(f"DELETE FROM {table}")
            except Exception as e:
                logger.warning(f"Очистка {table} пропущена: {e}")
        await self._connection.commit()
        return tables

    def _row_to_dict(self, cursor, row) -> dict:
        return {col[0]: val for col, val in zip(cursor.description, row)}
    @property
    def path(self) -> Path:
        """Путь к файлу БД (для бэкапов)."""
        return self._path
    @property
    def connection(self):
        return self._connection

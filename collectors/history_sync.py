# Сверка ленты с Telegram: досохранение пропущенных сообщений чата Leo и восстановление backlog.
# Миксин DvinchikCollector (вынесен из collectors/dvinchik_collector.py без изменения логики).

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from loguru import logger

from collectors.leo_markers import (
    HISTORY_SYNC_LIMIT,
    HISTORY_SYNC_INTERVAL_SEC,
    HISTORY_SYNC_MIN_AGE_SEC,
    _detect_media_type,
    _CURRENT_SESSION,
)
from collectors.raw_worker import RawTask


class HistorySyncMixin:
    """Сверка истории чата Leo и рекавери backlog (миксин DvinchikCollector)."""

    async def sync_chat_history(
        self,
        limit: int = HISTORY_SYNC_LIMIT,
        min_age_sec: float = HISTORY_SYNC_MIN_AGE_SEC,
    ) -> int:
        """Досохраняет в RAW сообщения чата Leo, которые прошли мимо событий.

        Лента на сайте — зеркало Telegram-чата, но строится только из того, что
        Telethon доставил событием. При обрывах прокси/рестартах события теряются
        (входящие карточки, наши ❤️/👎, ручные ответы с телефона) — в ленте
        появлялись дыры и «анархия». Здесь для КАЖДОГО аккаунта читаем последние
        ``limit`` сообщений чата и INSERT OR IGNORE'им недостающие (ключ
        ``(account_session, chat_id, tm_id, is_outgoing)`` — уже сохранённые
        не трогаются).

        Досохранённое помечается обработанным: это прошлое, реагировать на него
        поздно (карточка уже ушла). Сообщения моложе ``min_age_sec`` не трогаем:
        их вот-вот сохранит живой хендлер, а наш INSERT отнял бы у него
        сообщение (дубликат → pipeline пропущен). Возвращает число новых строк.
        """
        added = 0
        now = datetime.now(timezone.utc)
        for client in self._clients:
            account_session = self._session_for_client(client)
            token = _CURRENT_SESSION.set(account_session or None)
            # Карточка «Ты понравился…», пропущенная живым хендлером, тоже
            # должна остановить поиск — но только самая свежая и только если
            # после неё мы ещё ничего не отправляли (иначе уже разобрана).
            watch_match = client is self._auto_engine.client
            try:
                async for msg in client.iter_messages(
                    self._dvinchik_chat_id, limit=limit
                ):
                    if getattr(msg, "out", False):
                        watch_match = False
                    msg_dt = getattr(msg, "date", None)
                    if (
                        isinstance(msg_dt, datetime)
                        and (now - msg_dt).total_seconds() < min_age_sec
                    ):
                        continue
                    text = getattr(msg, "text", None) or ""
                    media_type = _detect_media_type(msg)
                    if not text.strip() and not media_type:
                        continue
                    outgoing = bool(getattr(msg, "out", False))
                    reply_to = getattr(msg, "reply_to_msg_id", None)
                    raw_id = await self._db.save_raw_message(
                        telegram_message_id=msg.id,
                        chat_id=self._dvinchik_chat_id,
                        sender_id=getattr(msg, "sender_id", 0) or 0,
                        sender_username="",
                        sender_name="",
                        message_date=(
                            msg_dt.isoformat()
                            if isinstance(msg_dt, datetime)
                            else now.isoformat()
                        ),
                        text=text,
                        raw_entities=(
                            "[]" if outgoing else self._serialize_entities(msg)
                        ),
                        reply_markup=(
                            "[]" if outgoing
                            else self._serialize_reply_markup(msg)
                        ),
                        media_type=media_type,
                        reply_to_message_id=(
                            reply_to if isinstance(reply_to, int) else None
                        ),
                        received_at=now.isoformat(),
                        is_outgoing=outgoing,
                        account_session=account_session,
                    )
                    is_card = watch_match and self._is_match_card(msg)
                    if is_card:
                        watch_match = False
                    if raw_id is None:
                        continue
                    await self._db.mark_raw_processed(raw_id)
                    added += 1
                    if is_card:
                        await self._stop_on_match_card(msg)
            except Exception as e:
                logger.error(
                    f"Сверка истории чата ({account_session or '?'}) не удалась: {e}"
                )
            finally:
                _CURRENT_SESSION.reset(token)
        if added:
            logger.info(f"Сверка истории чата: досохранено {added} сообщений")
        return added

    async def run_history_sync(
        self, interval_sec: float = HISTORY_SYNC_INTERVAL_SEC,
    ) -> None:
        """Фоновая сверка ленты с Telegram (раз в ``interval_sec``)."""
        while True:
            await asyncio.sleep(interval_sec)
            try:
                await self.sync_chat_history()
            except Exception as e:
                logger.error(f"Сверка истории чата: {e}")
            await self.retry_match_greetings()
            await self.run_match_intros()
            await self.sync_dialogs()

    async def recover_backlog(self, batch_size: int = 200) -> int:
        """Восстанавливает необработанные RAW из БД в очередь worker'а (W3).

        Обрабатывает батчами по ``batch_size``, чтобы не держать весь backlog
        в памяти. Очередь ограничена (maxsize), поэтому ``enqueue`` блокируется
        при заполнении — worker успевает обрабатывать (естественная backpressure,
        память ограничена). Захватывает cutoff (MAX id) на момент старта, чтобы
        не дублировать живой трафик, который хендлер сам ставит в очередь.
        Возвращает число поставленных в очередь заданий.
        """
        if self._worker is None:
            return 0
        try:
            cutoff_id = await self._db.get_max_raw_id()
        except Exception as e:
            logger.error(f"Backlog recovery: ошибка БД: {e}")
            return 0

        # Recovery активна: live handler помечает свои raw_id в set (см.
        # _handle_new_message). После завершения recovery флаг снимается и
        # set очищается — steady-state live-сообщения (id > cutoff) не могут
        # быть продублированы recovery, поэтому отслеживать их не нужно.
        self._recovery_armed = True

        total = 0
        after_id = 0
        while True:
            try:
                rows = await self._db.get_unprocessed_raw_messages_before(
                    cutoff_id, batch_size, after_id
                )
            except Exception as e:
                logger.error(f"Backlog recovery: ошибка чтения БД: {e}")
                break
            if not rows:
                break
            for row in rows:
                raw_id = row["id"]
                # D: не дублируем raw_id, уже поставленный live handler'ом
                # в эту сессию (иначе один RAW попадёт и в live worker, и в
                # recover_backlog). Recovery сама не добавляет в set — её курсор
                # (after_id) гарантирует, что каждая строка читается ровно один
                # раз, поэтому set нужен только для пропуска live-энqueued.
                if raw_id in self._enqueued_raw_ids:
                    continue
                task = RawTask(
                    chat_id=row["chat_id"],
                    message_id=row["telegram_message_id"],
                    sender_id=row["sender_id"],
                    sender_username=row.get("sender_username") or "",
                    sender_name=row.get("sender_name") or "",
                    text=row.get("text") or "",
                    media_type=row.get("media_type") or "",
                    entities_json=row.get("raw_entities") or "[]",
                    reply_markup_json=row.get("reply_markup") or "[]",
                    reply_to=row.get("reply_to_message_id"),
                    received_at=row["received_at"],
                    msg_date=row["message_date"],
                    msg=None,
                    raw_id=raw_id,
                    account_session=row.get("account_session") or "",
                )
                try:
                    await self._worker.enqueue(task)
                    total += 1
                except Exception as e:
                    logger.error(f"Backlog recovery: ошибка enqueue: {e}")
                    break
            # Продвигаем курсор, чтобы не переобрабатывать те же строки
            # (worker может ещё не пометить их processed_at).
            after_id = max(r["id"] for r in rows)
        # После recovery live-сообщения имеют id > cutoff и с recovery не
        # пересекаются, поэтому set больше не нужен: снимаем защиту и очищаем.
        # В steady-state (recovery не активна) live handler НЕ добавляет в set,
        # поэтому память не растёт бесконечно (MEDIUM-1).
        self._recovery_armed = False
        self._enqueued_raw_ids.clear()
        if total:
            logger.info(f"Backlog recovery: поставлено заданий: {total}")
        return total

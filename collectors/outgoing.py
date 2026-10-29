# Наши исходящие сообщения: RAW-сохранение, ручные действия владельца (sent_messages), ручные решения по REVIEW.
# Миксин DvinchikCollector (вынесен из collectors/dvinchik_collector.py без изменения логики).

from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger
from rich.panel import Panel
from rich.table import Table
from telethon import events

from collectors.leo_markers import console, is_manual_message_excluded


class OutgoingMixin:
    """Перехват наших исходящих сообщений (миксин DvinchikCollector)."""

    async def _handle_outgoing_message(self, event: events.NewMessage.Event) -> None:
        """Перехват исходящих сообщений (actions пользователя) в чате бота.

        Сохраняет RAW в raw_messages с ``is_outgoing=True`` и помечает
        processed_at — pipeline (парсинг/фильтр/AI) НЕ запускается. Единственная
        цель — ground truth для реверса механики LIKE: что именно отправляет
        пользователь.

        Исходящие — полноценные события ленты: веб показывает их отдельной
        строкой (``is_outgoing``), а не как сообщение от Leo. ``sent_messages`` /
        ``auto_actions_log`` в ленту НЕ подмешиваются — это журналы действий.
        """
        self._bind_client(getattr(event, "client", None))
        chat_id = event.chat_id
        if chat_id != self._dvinchik_chat_id:
            # Личка с девушкой-матчем: сообщение владельца → журнал переписки.
            if getattr(event, "is_private", False) is True:
                try:
                    await self._dialog_log.log_event(
                        self._session_for_client(getattr(event, "client", None)),
                        getattr(event, "message", None), outgoing=True,
                        user_id=chat_id,
                    )
                except Exception as e:
                    logger.error(f"DialogLog: исходящее: {e}")
            return

        msg = event.message
        text = msg.text or ""
        if not text.strip():
            return

        sender_id = msg.sender_id if getattr(msg, "sender_id", None) else 0
        msg_date = msg.date.isoformat() if msg.date else datetime.now(timezone.utc).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        account_session = self._session_for_client(
            getattr(event, "client", None)
        )

        # === RAW сохраняется ПЕРВЫМ ===
        try:
            raw_id = await self._db.save_raw_message(
                telegram_message_id=msg.id,
                chat_id=chat_id,
                sender_id=sender_id,
                sender_username="",
                sender_name="",
                message_date=msg_date,
                text=text,
                raw_entities="[]",
                reply_markup="[]",
                media_type="",
                reply_to_message_id=None,
                received_at=now,
                is_outgoing=True,
                account_session=account_session,
            )
        except Exception as e:
            logger.error(f"Outgoing RAW save failed: {e}")
            return
        if raw_id is None:
            return

        # Помечаем обработанным — pipeline пропускается.
        try:
            await self._db.mark_raw_processed(raw_id)
        except Exception as e:
            logger.error(f"Outgoing mark processed failed: {e}")

        self._print_outgoing_message(chat_id, msg.id, text, msg_date)

        await self._maybe_record_manual_review(
            chat_id, msg.id, text, account_session,
        )
        await self._maybe_record_sent_message(
            chat_id, text, msg, account_session,
        )

    async def _maybe_record_sent_message(
        self, chat_id: int, text: str, msg: object,
        account_session: str = "",
    ) -> None:
        """Stage 8.5: фиксирует РУЧНОЕ действие владельца при лайке.

        Авто-сообщения «Берем)» не попадают сюда: отправки с авто-аккаунта
        уже отслеживаются в auto_actions_log(message_text). Здесь — действия
        владельца (клиент НЕ авто-аккаунт):
          - «❤️»  → ручной LIKE (сам лайкнул),
          - «👎»  → ручной DISLIKE,
          - текст → ручное сообщение (MESSAGE), кроме кнопок/реакций из
            MANUAL_MESSAGE_EXCLUDE и коротких (len < 2).
        Профиль для привязки ответа берём из «текущей» анкеты чата
        (chat_context / pending). Ошибки БД не ломают перехват исходящих
        (RAW уже сохранён).
        """
        t = (text or "").strip()
        if not t:
            return
        # Исходящие с авто-аккаунта пропускаем ТОЛЬКО когда авто-действия
        # реально включены (SEMI_AUTO/AUTO): тогда их шлёт AutoActionEngine,
        # и они уже записаны в auto_actions_log. В OBSERVE авто ничего не
        # шлёт — любой исходящий (в т.ч. с авто-аккаунта) = ручное действие
        # владельца, его записываем в sent_messages.
        if (
            self._auto_engine.enabled
            and self._auto_engine.client is not None
            and getattr(msg, "client", None) is self._auto_engine.client
        ):
            return
        # Реакции владельца = ручной лайк/дизлайк (сам лайкнул и т.п.).
        if t == "❤️":
            action = "LIKE"
        elif t == "👎":
            action = "DISLIKE"
        else:
            if len(t) < 2 or is_manual_message_excluded(t):
                return
            action = "MESSAGE"
        profile_id: int | None = None
        try:
            context = await self._db.get_chat_profile_context(
                chat_id, account_session,
            )
            if context is None:
                context = self._pending_profiles.get((account_session, chat_id))
            if context is not None:
                profile_id = await self._db.resolve_profile_by_message(
                    context, account_session,
                )
        except Exception as e:
            logger.warning(f"sent_message: не смогли привязать профиль: {e}")
        try:
            await self._db.record_sent_message(
                t, chat_id, getattr(msg, "id", 0), source="manual",
                action=action, profile_id=profile_id,
            )
        except Exception as e:
            logger.warning(f"record_sent_message error: {e}")

    async def _maybe_record_manual_review(
        self, chat_id: int, outgoing_tm_id: int, text: str,
        account_session: str = "",
    ) -> None:
        """Фиксирует ручное решение владельца по активной REVIEW-анкете.

        Исходящее действие (❤️/👎/сообщение) в чате Дайвинчика привязывается к
        «текущей» анкете чата (chat_context / _pending_profiles). Если последнее
        AI-решение этой анкеты — REVIEW, действие записывается в журнал ручных
        решений. Ошибки не ломают перехват исходящих (RAW уже сохранён).
        """
        if not self._manual_review or not getattr(
            self._manual_review, "enabled", False
        ):
            return
        if chat_id != self._dvinchik_chat_id:
            return
        context = await self._db.get_chat_profile_context(
            chat_id, account_session,
        )
        if context is None:
            context = self._pending_profiles.get((account_session, chat_id))
        if context is None:
            return
        try:
            await self._manual_review.handle_outgoing(
                chat_id, context, outgoing_tm_id, text,
            )
        except Exception as e:
            logger.error(f"ManualReview: ошибка обработки исходящего: {e}")

    def _print_outgoing_message(
        self, chat_id: int, message_id: int, text: str, msg_date: str,
    ) -> None:
        """Красивый вывод исходящего сообщения (action пользователя)."""
        table = Table(show_header=False, border_style="dim", padding=(0, 1))
        table.add_column("Key", style="bold magenta", width=14)
        table.add_column("Value")
        table.add_row("Source", "[bold magenta]USER ACTION[/bold magenta]")
        table.add_row("Chat ID", str(chat_id))
        table.add_row("Message ID", str(message_id))
        table.add_row("Date", msg_date[:19])
        preview = text[:200] + ("..." if len(text) > 200 else "")
        table.add_row("Text", preview)
        console.print(Panel(
            table,
            title="[bold magenta]OUTGOING[/bold magenta]",
            border_style="magenta",
        ))

# Личка и взаимные лайки: стикер-привет, автопереписка (анкета + закреп), журнал переписок.
# Миксин DvinchikCollector (вынесен из collectors/dvinchik_collector.py без изменения логики).

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime

from loguru import logger
from telethon import events

from collectors.match_intro import INTRO_SENT, NO_PROFILE
from services.match_script_rules import (
    ASKS_BOT,
    DELETED,
    HANDOFF,
    STOPPED,
)
from collectors.raw_worker import RawTask
from models.raw import ParsedMatch


class MatchDialogsMixin:
    """Привет матчу, автопереписка и журнал лички (миксин DvinchikCollector)."""

    async def _greet_match(
        self, task: RawTask, msg: object | None, match: ParsedMatch,
    ) -> None:
        """Взаимный лайк → стикер девушке (вместо текста Leo).

        Всем матчам: и после лайка бота, и после ручного лайка владельца
        (в т.ч. в OBSERVE). Пишем с того аккаунта, на котором случился матч
        (у неё матч именно с ним). Режимы — ``match_greeting.modes``.

        Перенаправление (стрелка в шапке, ``auto_actions.account_redirects``):
        аккаунт в бане → пишем девушке с другого аккаунта, и вся дальнейшая
        переписка (журнал, автопереписка) живёт на нём.
        """
        client = getattr(msg, "client", None) if msg is not None else None
        if client is None or task.chat_id != self._dvinchik_chat_id:
            return
        session = task.account_session or self._session_for_client(client)
        source = ""
        target = self.redirect_for(session)
        if target:
            target_client, _ = self._clients_for_session(target)
            if target_client is not None:
                source, session, client = session, target, target_client
        scope = self._session_scope(session) if source else nullcontext()
        with scope:
            # Реестр переписок — ДО привета: стикер бота сразу ляжет в журнал.
            await self._track_dialog(client, session, match, source=source)
            try:
                status = await self._match_greeter.greet(
                    client, session, match, self._dialog_mode(session),
                )
            except Exception as e:
                logger.error(f"MatchGreeting error: {e}")
                return
            await self._notify_greeting(session, match, status, source=source)
            if status == "SENT":
                await self._enroll_match_intro(
                    client, session, match, source=source,
                )

    async def _track_dialog(
        self, client: object, session: str, match: ParsedMatch, *,
        source: str = "",
    ) -> None:
        """Матч → девушка в журнале переписок (каждый матч, любой режим).

        ``source`` — аккаунт, на котором был матч, если личка перенаправлена:
        её анкету ищем в его ленте.
        """
        if not self._dialog_log.enabled:
            return
        try:
            profile_id = await self._db.find_match_profile(
                match.name, self._dvinchik_chat_id, source or session,
            )
            await self._dialog_log.track(client, session, match, profile_id)
        except Exception as e:
            logger.error(f"DialogLog: не завёл переписку {match.name}: {e}")

    async def sync_dialogs(self) -> None:
        """Сверка журнала переписок с историей Telegram (по аккаунтам)."""
        if not (self._dialog_log.enabled and self._dialog_sync):
            return
        for client, session in zip(self._clients, self._client_sessions):
            with self._session_scope(session):
                try:
                    await self._dialog_log.sync(client, session)
                except Exception as e:
                    logger.error(f"DialogLog: сверка {session}: {e}")

    async def _enroll_match_intro(
        self, client: object, session: str, match: ParsedMatch, *,
        use_context: bool = True, source: str = "",
    ) -> None:
        """Привет ушёл → ждём её ответа (автопереписка, ``match_intro``).

        ``source`` — аккаунт матча при перенаправлении (анкета — в его ленте).
        """
        try:
            # Карточка — из ленты аккаунта матча: под одним profile_id бывают
            # разные девушки с тем же «имя, возраст» (прод 2026-10-30).
            card_session = source or session
            profile_id, card_id = await self._db.find_match_card(
                match.name, self._dvinchik_chat_id, card_session,
                use_context=use_context,
            )
            await self._match_intro.enroll(
                client, session, match, profile_id,
                card_session=card_session if card_id else "",
                card_message_id=card_id,
            )
        except Exception as e:
            logger.error(f"MatchIntro: не завёл переписку {match.name}: {e}")

    async def _handle_private_message(
        self, event: events.NewMessage.Event,
    ) -> None:
        """Входящее в личке: девушка-матч → журнал переписки; после привета —
        планируем анкету.

        Чат Leo (и прочие источники ленты) сюда не относятся. Текст в лог
        (loguru) не пишется — только сообщения девушек-матчей в БД
        (``dialog_messages``).
        """
        self._bind_client(getattr(event, "client", None))
        try:
            chat_id = event.chat_id
            if chat_id in self._allowed_chat_ids:
                return
            sender_id = int(getattr(event, "sender_id", 0) or chat_id or 0)
            session = self._session_for_client(getattr(event, "client", None))
            await self._dialog_log.log_event(
                session, getattr(event, "message", None), outgoing=False,
                user_id=chat_id,
            )
            message = getattr(event, "message", None)
            date = getattr(message, "date", None)
            at = date if isinstance(date, datetime) else None
            mode = self._dialog_mode(session)
            await self._match_intro.note_reply(session, sender_id, mode, at=at)
            # Сценарий знакомства (после анкеты): текст только разбирается,
            # в лог не пишется.
            text = getattr(message, "message", "")
            await self._match_script.note_message(
                session, sender_id, text if isinstance(text, str) else "",
                mode, at=at,
            )
        except Exception as e:
            logger.error(f"MatchIntro: ошибка обработки ответа: {e}")

    async def run_match_intros(self) -> None:
        """Отправка созревших анкет девушкам (раз в цикл сверки истории)."""
        for client, session in zip(self._clients, self._client_sessions):
            with self._session_scope(session):
                await self._run_match_intros_for(client, session)

    async def _run_match_intros_for(self, client: object, session: str) -> None:
        """Проход автопереписки по одному аккаунту."""
        if True:
            try:
                results = await self._match_intro.process(
                    client, session, self._dialog_mode(session),
                )
            except Exception as e:
                logger.error(f"MatchIntro: проход {session}: {e}")
                return
            for row, status in results:
                who = (
                    f"@{row['username']}" if row.get("username")
                    else f"id {row.get('user_id')}"
                )
                if status == NO_PROFILE:
                    # Её карточку матча не нашли — чужую не шлём, пишет владелец.
                    await self._auto_engine.notify_owner(
                        f"💬 {row.get('name') or who} ({who}) ответила на привет "
                        f"на {session or 'аккаунте'} — её анкету не нашёл, "
                        "не слал. Напиши ей сам."
                    )
                    continue
                if status != INTRO_SENT:
                    continue
                await self._match_script.start(
                    session, int(row["user_id"]), row.get("sent_msg_id"),
                )
                pin = " и закрепил 📌" if row.get("pinned") else ""
                await self._auto_engine.notify_owner(
                    f"💬 {row.get('name') or who} ({who}) ответила на привет — "
                    f"отправил ей её анкету{pin} с {session or 'аккаунта'}"
                )
            await self._run_match_script_for(client, session)

    async def _run_match_script_for(self, client: object, session: str) -> None:
        """Проход сценария знакомства по аккаунту + уведомления владельцу."""
        try:
            results = await self._match_script.process(
                client, session, self._dialog_mode(session),
            )
        except Exception as e:
            logger.error(f"MatchScript: проход {session}: {e}")
            return
        for row, step, kind in results:
            text = script_notice(row, step, kind, session)
            if text:
                await self._auto_engine.notify_owner(text)

    async def _notify_greeting(
        self, session: str, match: ParsedMatch, status: str, *,
        delayed: bool = False, source: str = "",
    ) -> None:
        """Уведомление владельцу: стикер ушёл / аккаунт в спам-блоке.

        ``source`` — матч был на другом аккаунте, личка перенаправлена.
        """
        who = (
            f"@{match.telegram_username}" if match.telegram_username
            else f"id {match.telegram_user_id}"
        )
        via = f" (перенаправлено с {source})" if source else ""
        if status == "SENT":
            tail = " (из очереди после спам-блока)" if delayed else ""
            await self._auto_engine.notify_owner(
                f"🔥 Взаимный лайк: {match.name} ({who}) — отправил ей "
                f"стикер-привет с {session or 'аккаунта'}{via}{tail}"
            )
        elif source and status in ("ERROR", "NO_TARGET"):
            # С чужого аккаунта без @username её часто не найти (нет
            # access_hash) — владелец должен знать, что привет не ушёл.
            await self._auto_engine.notify_owner(
                f"⚠️ Взаимный лайк на {source}: {match.name} ({who}) — не смог "
                f"написать с {session}{via}. Напиши ей сам."
            )
        elif status == "SPAM_BLOCK":
            until = await self._match_greeter.paused_until(session)
            when = (
                until.astimezone().strftime("%d.%m %H:%M") if until else "—"
            )
            await self._auto_engine.notify_owner(
                f"⛔ {session or 'Аккаунт'} в спам-блоке Telegram (нельзя "
                f"писать незнакомым): приветствия на паузе до {when}. "
                f"{match.name} ({who}) — в очереди, допишу после паузы. "
                "Проверь @SpamBot."
            )

    async def retry_match_greetings(self) -> None:
        """Дописывает девушкам, отложенным из-за спам-блока (после паузы)."""
        for client, session in zip(self._clients, self._client_sessions):
            with self._session_scope(session):
                await self._retry_match_greetings_for(client, session)

    async def _retry_match_greetings_for(self, client: object, session: str) -> None:
        """Дозапись очереди приветствий одного аккаунта."""
        if True:
            try:
                results = await self._match_greeter.retry_queued(
                    client, session, self._dialog_mode(session),
                )
            except Exception as e:
                logger.error(f"MatchGreeting: дозапись очереди {session}: {e}")
                return
            for match, status in results:
                await self._notify_greeting(
                    session, match, status, delayed=True,
                )
                if status == "SENT":
                    await self._enroll_match_intro(
                        client, session, match, use_context=False,
                    )


def script_notice(row: dict, step: str, kind: str, session: str) -> str:
    """Уведомление владельцу о конце сценария (без текста её сообщений)."""
    who = (
        f"@{row['username']}" if row.get("username")
        else f"id {row.get('user_id')}"
    )
    name = f"{row.get('name') or who} ({who})"
    acct = session or "аккаунта"
    if step == DELETED:
        return f"🗑 {name} попросила не писать или грубит — чат удалён на {acct}"
    if step == STOPPED:
        return (
            f"🔕 {name} молчит сутки после «Рассказывайте» — бот больше не "
            f"пишет ({acct})"
        )
    if step != HANDOFF:
        return ""
    if kind == ASKS_BOT:
        why = "спросила, не бот ли ты — ответь сам"
    elif row.get("script_text"):
        why = f"бот ответил «{row['script_text']}», дальше ты"
    else:
        why = "ответила не по сценарию — дальше ты"
    return f"💬 {name}: переписка твоя на {acct} — {why}"

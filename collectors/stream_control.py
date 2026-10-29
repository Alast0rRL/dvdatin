# Автопоток ленты Leo: старт ленты, стоп на взаимном лайке и «Продолжить», сбой автопотока.
# Миксин DvinchikCollector (вынесен из collectors/dvinchik_collector.py без изменения логики).

from __future__ import annotations

import json
from datetime import datetime, timezone

from loguru import logger

from collectors.auto_action import DISLIKE_TEXT, LIKE_TEXT
from collectors.leo_markers import (
    STREAM_ERROR_STATE_KEY,
    STREAM_ERROR_NOTIFY_COOLDOWN_SEC,
    STREAM_ERROR_SNAPSHOT_LIMIT,
    MATCH_STOP_STATE_KEY,
    MATCH_STOP_PREV_MODE_KEY,
    MATCH_CARD_FRAGMENT,
    MATCH_SHOW_FRAGMENT,
    MATCH_NOTICE_FRAGMENTS,
    MATCH_SHOW_SKIP_FRAGMENT,
    MATCH_SHOW_ALT_FRAGMENT,
    MATCH_SHOW_SKIP_ALT_FRAGMENT,
)
from collectors.raw_worker import RawTask
from core.types import Mode


class StreamControlMixin:
    """Старт ленты, стоп на карточке взаимного лайка, баннер сбоя (миксин DvinchikCollector)."""

    async def start_all_auto_streams(self) -> None:
        """Запускает ленту на всех аккаунтах с включёнными авто-действиями."""
        for session in self.account_sessions():
            if self._engines[session].enabled:
                await self.start_auto_stream(session)

    async def start_auto_stream(self, session: str | None = None) -> bool:
        """Лента Leo на аккаунте ``session`` (None — текущий/основной)."""
        if session is None:
            return await self._start_auto_stream_impl()
        with self._session_scope(session):
            return await self._start_auto_stream_impl()

    async def _start_auto_stream_impl(self) -> bool:
        """Обрабатывает активную анкету или продолжает ленту Leo.

        ✨🔍 НЕ отправляется: команда не приводит новые анкеты, а показанная
        (обычный текст "Имя, возраст, город") уже ждёт только лайк/дизлайк.
        1) Есть активная анкета → обрабатываем (лайк/дизлайк).
        2) Нет активной анкеты, но Leo показал «Смотреть анкеты» → нажимаем
           кнопку, чтобы получить следующую партию.
        3) Ничего из этого → False.
        """
        if not self._auto_engine.enabled:
            return False
        try:
            await self._auto_engine.ensure_peer()
            # Одна выборка истории на все проверки кнопок (см. _recent_history).
            history = await self._recent_history()
            # Карточка «Ты понравился…» — первым делом: поиск останавливается,
            # пока владелец не разберёт лайки сам (никаких кнопок не жмём).
            if await self._stop_on_match_card_if_needed(history):
                return False
            if await self._process_active_profile_if_any():
                await self._clear_stream_error()
                return True
            if await self._continue_stream_if_needed(history):
                await self._clear_stream_error()
                return True
            if await self._handle_captcha():
                await self._clear_stream_error()
                return True
            # Ни активной анкеты, ни кнопки продолжения, ни капчи. Последняя
            # надежда — «зависший» композер Leo: после рестарта цепочка
            # «Берем)» теряется, а Leo всё ещё ждёт текст, и лента стоит.
            last_text = await self._last_incoming_text()
            if await self._auto_engine.recover_open_composer(last_text):
                await self._clear_stream_error()
                return True
            if self._auto_engine.is_composer_prompt(last_text):
                # Leo ждёт текст, а закрыть композер не можем (like_message
                # выключен) — это реальный сбой, владельца надо предупредить.
                await self._report_stream_stuck(
                    f"Leo ждёт текст в композере: «{last_text[:80]}»"
                )
                return False
            # Ничего продолжать не нужно (нормальное состояние, не сбой).
            return False
        except Exception as e:
            logger.error(f"AutoAction: ошибка обработки активной анкеты: {e}")
            await self._report_stream_stuck(f"ошибка автопотока: {e}")
            return False

    async def _stream_snapshot(
        self, limit: int = STREAM_ERROR_SNAPSHOT_LIMIT
    ) -> list[str]:
        """Снимок последних сообщений чата Leo — для диагностики сбоя потока.

        Идём от свежих к старым, затем разворачиваем: в конце списка — самое
        свежее сообщение (его и показываем как «последнее в чате»).
        """
        client = self._auto_engine.client
        if client is None:
            return []
        lines: list[str] = []
        try:
            async for msg in client.iter_messages(
                self._dvinchik_chat_id, limit=limit
            ):
                text = (msg.text or "").strip()
                if not text:
                    continue
                who = "мы" if getattr(msg, "out", False) else "Leo"
                lines.append(f"{who}: {text[:120]}")
        except Exception as e:
            logger.error(f"AutoAction: не удалось снять снимок чата: {e}")
            return []
        lines.reverse()
        return lines

    async def _last_incoming_text(self) -> str:
        """Текст последнего сообщения в чате Leo ('' — нет/ошибка).

        Используется, чтобы понять, не ждёт ли Leo текст в «зависшем»
        композере (после рестарта цепочка «Берем)» теряется).
        """
        client = self._auto_engine.client
        if client is None:
            return ""
        try:
            async for msg in client.iter_messages(self._dvinchik_chat_id, limit=1):
                return (msg.text or "").strip()
        except Exception as e:
            logger.error(
                f"AutoAction: не удалось прочитать последнее сообщение: {e}"
            )
        return ""

    async def _report_stream_stuck(self, reason: str = "") -> None:
        """Сообщает владельцу и на сайт, что бот не может продолжить ленту.

        Пишет диагностический снимок в ``app_state`` (его показывает баннер на
        сайте) и один раз в ``STREAM_ERROR_NOTIFY_COOLDOWN_SEC`` шлёт
        уведомление владельцу в Telegram. Если в чате нет ни одного внятного
        сообщения — ничего не сообщаем (ложное срабатывание на пустом старте).
        """
        if self._db is None:
            return
        snapshot = await self._stream_snapshot()
        if not snapshot and not reason:
            return
        if not reason and snapshot:
            reason = snapshot[-1]
        now = datetime.now(timezone.utc)
        session = self._current_session()
        payload = {
            "session": session,
            "message": (
                f"Бот не может продолжить ленту анкет Дайвинчика ({session})"
                if len(self._engines) > 1
                else "Бот не может продолжить ленту анкет Дайвинчика"
            ),
            "reason": reason,
            "snapshot": snapshot,
            "at": now.isoformat(),
        }
        notify_needed = True
        try:
            prev_raw = await self._db.get_state(STREAM_ERROR_STATE_KEY)
            if prev_raw:
                try:
                    prev_at = datetime.fromisoformat(
                        str(json.loads(prev_raw).get("at") or "")
                    )
                    age = (now - prev_at).total_seconds()
                    if 0 <= age < STREAM_ERROR_NOTIFY_COOLDOWN_SEC:
                        notify_needed = False
                except Exception:
                    pass
            await self._db.set_state(
                STREAM_ERROR_STATE_KEY,
                json.dumps(payload, ensure_ascii=False),
            )
        except Exception as e:
            logger.error(f"AutoAction: не удалось сохранить сбой потока: {e}")

        logger.warning(
            f"AutoAction: лента встала — {reason or 'причина неизвестна'}"
        )
        if notify_needed and self._auto_engine is not None:
            await self._auto_engine.notify_owner(
                self._format_stream_stuck_text(reason, snapshot)
            )

    async def _clear_stream_error(self) -> None:
        """Сбрасывает отметку о сбое потока, когда лента снова пошла."""
        if self._db is None:
            return
        try:
            raw = await self._db.get_state(STREAM_ERROR_STATE_KEY)
            if raw:
                # Баннер другого аккаунта не трогаем: его лента всё ещё стоит.
                owner = ""
                try:
                    owner = (json.loads(raw) or {}).get("session") or ""
                except Exception:
                    owner = ""
                if owner and owner != self._current_session():
                    return
                await self._db.delete_state(STREAM_ERROR_STATE_KEY)
        except Exception as e:
            logger.error(f"AutoAction: не удалось сбросить сбой потока: {e}")

    @staticmethod
    def _format_stream_stuck_text(reason: str, snapshot: list[str]) -> str:
        """Текст уведомления владельцу о сбое потока."""
        lines = ["⚠️ Бот не может продолжить ленту анкет (Дайвинчик)."]
        if reason:
            lines.append(f"Последнее в чате — {reason}")
        if snapshot:
            lines.append("")
            lines.append("Последние сообщения:")
            lines.extend(snapshot[-5:])
        lines.append("")
        lines.append(
            "Проверь чат с Leo — возможно, нужно нажать кнопку или ответить вручную."
        )
        return "\n".join(lines)

    async def _process_active_profile_if_any(self) -> bool:
        """Обрабатывает уже показанную активную анкету, если она есть.

        Механика Дайвинчика: анкета приходит обычным текстом
        ("Имя, возраст, город – …") и ждёт реакции простым текстом ❤️/👎
        (кнопок на анкете может не быть). Сканируем последние сообщения чата
        на авто-аккаунте и берём самую свежую анкету (PROFILE), на которую
        ещё не отправлена реакция (нет исходящего ❤️/👎 после неё). Прогоняем
        её через штатный pipeline (_process_message: parse → filter → AI →
        авто-действие). Если активной анкеты нет — возвращаем False.

        Важно: анкета считается активной, только если после неё нет СВЕЖЕГО
        содержательного входящего сообщения Leo (меню, Premium-промо, MATCH,
        «Лайк отправлен…», капча, «Нет такого варианта ответа») — такое
        сообщение означает, что фаза реакции на анкету завершена/лента ушла
        вперёд (после переключения аккаунта в истории может остаться старая
        анкета, на которую больше нельзя отвечать — отправка ❤️/👎 даст
        «Нет такого варианта ответа»). Медиа/фото без текста игнорируются
        (они относятся к текущей карточке анкеты).
        """
        client = self._auto_engine.client
        if client is None:
            return False
        try:
            latest_action_id: int | None = None
            active = None
            async for msg in client.iter_messages(self._dvinchik_chat_id, limit=15):
                text = (msg.text or "").strip()
                if getattr(msg, "out", False):
                    if text in (LIKE_TEXT, DISLIKE_TEXT):
                        if latest_action_id is None:
                            latest_action_id = msg.id
                    continue
                if not text:
                    # Медиа/стикер без текста — часть карточки анкеты,
                    # состояние чата не меняет.
                    continue
                if self._is_profile_text(msg):
                    if (
                        latest_action_id is not None
                        and msg.id < latest_action_id
                    ):
                        # на эту анкету уже отправлена реакция — не активна
                        continue
                    active = msg
                    break
                # Самое свежее входящее — НЕ анкета (меню/промо/MATCH/лайк-эк/
                # капча/отказ Leo): активной анкеты нет.
                logger.info(
                    f"AutoAction: нет активной анкеты — самое свежее сообщение "
                    f"Leo (msg={msg.id}) не анкета: {text[:60]!r}"
                )
                active = None
                break

            if active is None:
                logger.info("AutoAction: активная анкета в чате не найдена")
                return False

            text = (active.text or "").strip()
            if not text:
                return False

            logger.info(
                f"AutoAction: найдена активная анкета (msg={active.id}) — "
                "обрабатываю без повторного ✨🔍"
            )
            task = RawTask(
                chat_id=self._dvinchik_chat_id,
                message_id=active.id,
                sender_id=active.sender_id or 0,
                sender_username="",
                sender_name="",
                text=text,
                media_type="",
                entities_json="[]",
                reply_to=None,
                received_at=datetime.now(timezone.utc).isoformat(),
                msg_date=active.date.isoformat() if active.date else "",
                msg=active,
                raw_id=0,
                account_session=self._auto_engine.account_session,
            )
            await self._process_message(task)
            return True
        except Exception as e:
            logger.error(f"AutoAction: ошибка обработки активной анкеты: {e}")
            return False

    def _is_match_card(self, msg: object) -> bool:
        """Карточка «Ты понравился N девушке, показать её?» (N — любое).

        Текст содержит MATCH_CARD_FRAGMENT и либо «показать» в самом тексте,
        либо кнопку показа (обе клавиатуры Leo: «1. Показать.» / «1 👍»).
        """
        low = (getattr(msg, "text", "") or "").lower()
        if MATCH_CARD_FRAGMENT not in low:
            return False
        if (self._current_session(), getattr(msg, "id", None)) in self._match_ack:
            # Владелец нажал «Продолжить» на этой карточке — больше не стоп.
            return False
        if MATCH_SHOW_FRAGMENT in low:
            return True
        if any(f in low for f in MATCH_NOTICE_FRAGMENTS):
            return True
        return any(
            self._is_match_show_button(t) for t in self._extract_button_texts(msg)
        )

    async def _stop_on_match_card_if_needed(
        self, history: list | None = None,
    ) -> bool:
        """Останавливает поиск на карточке «Ты понравился N девушке…».

        Владелец хочет сам решать, смотреть ли тех, кто его лайкнул, поэтому
        бот НЕ жмёт ни «Показать», ни «Не хочу…»: переводит режим в OBSERVE
        (сохраняется в config.yaml — переживает restart), снимает незакрытые
        цепочки «Берем)», ставит баннер на сайте и один раз на карточку
        уведомляет владельца. Продолжить — ``/mode on`` или режим на сайте.

        Карточка «актуальна», только если после неё мы ещё ничего не
        отправляли (иначе владелец уже ответил Leo). True — поиск остановлен.
        """
        client = self._auto_engine.client
        if client is None or not self._auto_engine.enabled:
            return False
        try:
            if history is None:
                history = await self._recent_history()
            card_msg = None
            for msg in history:
                if getattr(msg, "out", False):
                    # Наш ответ свежее любой карточки ниже — она уже разобрана.
                    return False
                if self._is_match_card(msg):
                    card_msg = msg
                    break
            if card_msg is None:
                return False

            return await self._stop_on_match_card(card_msg)
        except Exception as e:
            logger.error(f"AutoAction: ошибка остановки на карточке лайка: {e}")
            return False

    def _halt_on_match_card(self, card_msg: object) -> bool:
        """Синхронная часть остановки: режим OBSERVE + снятие цепочек.

        Без сети и БД (кроме записи режима в config.yaml) — поэтому её можно
        звать прямо из Telegram-хендлера под локом чата, ДО постановки
        сообщения в очередь pipeline. Карточка помечается активной
        (``note_card``): реакции на анкеты, ещё стоящие в очереди, становятся
        «устаревшими» и не уходят — иначе queued 👎 уходил ПОСЛЕ карточки,
        и проверка «после карточки уже есть наш ответ» считала её разобранной
        (поиск не останавливался). True — поиск остановлен сейчас.
        """
        if not self._auto_engine.enabled:
            return False
        card_text = (getattr(card_msg, "text", "") or "").strip()
        logger.warning(
            f"AutoAction: карточка взаимного лайка (msg={card_msg.id}) — "
            f"«{card_text[:80]}»: останавливаю поиск (скроллинг выключен)"
        )
        self._auto_engine.note_card(self._dvinchik_chat_id, card_msg.id)
        self._auto_engine.abort_like_chains(self._dvinchik_chat_id)
        session = self._current_session()
        self._mode_before_match_stop[session] = self.mode_for(session)
        self.set_mode(Mode.OBSERVE, session)
        return True

    async def _stop_on_match_card(self, card_msg: object) -> bool:
        """Полная остановка на конкретной карточке «Ты понравился…»."""
        if not self._halt_on_match_card(card_msg):
            return False
        await self._mark_match_stop(
            card_msg.id, (getattr(card_msg, "text", "") or "").strip()
        )
        return True

    async def _mark_match_stop(self, card_id: int, card_text: str) -> None:
        """Баннер на сайте + уведомление владельцу (одно на карточку)."""
        if self._db is None:
            return
        card_key = f"{self._auto_engine.account_session}:{card_id}"
        notify = True
        try:
            if await self._db.get_state(MATCH_STOP_STATE_KEY) == card_key:
                notify = False
            await self._db.set_state(MATCH_STOP_STATE_KEY, card_key)
            prev_mode = self._mode_before_match_stop.get(self._current_session())
            if prev_mode is not None and prev_mode != Mode.OBSERVE:
                await self._db.set_state(MATCH_STOP_PREV_MODE_KEY, prev_mode.value)
            await self._db.set_state(
                STREAM_ERROR_STATE_KEY,
                json.dumps(
                    {
                        "kind": "match_stop",
                        "session": self._current_session(),
                        "message": (
                            f"Поиск остановлен ({self._current_session()}): "
                            "тебя лайкнули"
                            if len(self._engines) > 1
                            else "Поиск остановлен: тебя лайкнули"
                        ),
                        "reason": card_text,
                        "snapshot": [f"Leo: {card_text[:120]}"],
                        "at": datetime.now(timezone.utc).isoformat(),
                    },
                    ensure_ascii=False,
                ),
            )
        except Exception as e:
            logger.error(f"AutoAction: не удалось сохранить остановку поиска: {e}")
        if notify:
            await self._auto_engine.notify_owner(
                f"💘 {card_text}\n\nПоиск остановлен (скроллинг "
                f"{self._current_session() or 'аккаунта'} выключен) — посмотри "
                "сам в Дайвинчике. Продолжить: кнопка «Продолжить» на сайте "
                "или /mode on."
            )

    async def _match_stop_prev_mode(self) -> Mode:
        """Режим, который был до остановки на карточке лайка (иначе SEMI_AUTO)."""
        prev = self._mode_before_match_stop.get(self._current_session())
        if prev is None and self._db is not None:
            try:
                raw = await self._db.get_state(MATCH_STOP_PREV_MODE_KEY)
                if raw:
                    prev = Mode(raw)
            except Exception as e:
                logger.error(f"AutoAction: не удалось прочитать режим до стопа: {e}")
        if prev is None or prev == Mode.OBSERVE:
            prev = Mode.SEMI_AUTO
        return prev

    async def resume_after_match_stop(self, session: str | None = None) -> bool:
        """«Продолжить» на аккаунте ``session`` (None — из баннера, иначе основной)."""
        if session is None and self._db is not None:
            try:
                raw = await self._db.get_state(STREAM_ERROR_STATE_KEY)
                if raw:
                    session = (json.loads(raw) or {}).get("session") or None
            except Exception as e:
                logger.error(f"AutoAction: не удалось прочитать баннер стопа: {e}")
        with self._session_scope(self._resolve_session(session)):
            return await self._resume_after_match_stop_impl()

    async def _resume_after_match_stop_impl(self) -> bool:
        """Кнопка «Продолжить» после остановки на карточке «Ты понравился…».

        Возвращает режим, который был до остановки (по умолчанию SEMI_AUTO),
        снимает баннер и продолжает ленту. Если карточка всё ещё активна
        (после неё нет нашего ответа), жмёт «Показать» — иначе
        ``start_auto_stream`` снова увидел бы её и остановил поиск. Анкету
        лайкнувшей Leo пришлёт обычной карточкой, её оценит pipeline.
        True — лента продолжена.
        """
        mode = await self._match_stop_prev_mode()
        session = self._current_session()
        reason = await self.mode_block_reason(mode, session)
        if reason is not None:
            # Лимит ❤️ исчерпан — остаёмся в OBSERVE, баннер не снимаем.
            logger.warning(f"AutoAction: «Продолжить» отклонено: {reason}")
            return False
        self._mode_before_match_stop.pop(session, None)
        self.set_mode(mode, session)
        if self._db is not None:
            try:
                await self._db.delete_state(MATCH_STOP_PREV_MODE_KEY)
            except Exception as e:
                logger.error(f"AutoAction: не удалось сбросить режим до стопа: {e}")
        await self._clear_stream_error()
        if self._auto_engine is None or not self._auto_engine.enabled:
            return False
        logger.info(f"AutoAction: «Продолжить» — режим {mode.value}, продолжаю поиск")

        for msg in await self._recent_history():  # новые → старые
            if getattr(msg, "out", False):
                break  # после последней карточки уже ответили
            if not self._is_match_card(msg):
                continue
            button = next(
                (t for t in self._extract_button_texts(msg)
                 if self._is_match_show_button(t)),
                None,
            )
            if button is None:
                # Уведомление без кнопок («Кому-то понравилась твоя анкета…»):
                # владелец разобрался сам — продолжаем ленту мимо него.
                self._match_ack.add((self._current_session(), msg.id))
                break
            logger.info(
                f"AutoAction: карточка лайка (msg={msg.id}) — жму «{button}»"
            )
            return await self._auto_engine.send_text(button)
        return await self.start_auto_stream()

    @staticmethod
    def _is_match_show_button(button_text: str) -> bool:
        """Кнопка «Показать» на карточке взаимного лайка?

        Leo шлёт разные клавиатуры: [«1. Показать.», «2. Не хочу…»] (старый
        вид) и [«1 👍», «2 💤»] (нумерованная, прод-2026). Отказную («не хочу»
        / «💤») не считаем — её жать нельзя.
        """
        t = button_text or ""
        low = t.lower()
        if MATCH_SHOW_SKIP_FRAGMENT in low or MATCH_SHOW_SKIP_ALT_FRAGMENT in t:
            return False
        return MATCH_SHOW_FRAGMENT in low or MATCH_SHOW_ALT_FRAGMENT in t

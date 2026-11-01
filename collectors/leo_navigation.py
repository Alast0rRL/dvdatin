# Кнопки и навигация Leo: продолжение ленты, Premium, главное меню, проверка города, капчи.
# Миксин DvinchikCollector (вынесен из collectors/dvinchik_collector.py без изменения логики).

from __future__ import annotations

import json

from loguru import logger

from collectors.auto_action import AutoActionEngine
from collectors.leo_markers import (
    VIEW_BUTTON_FRAGMENT,
    MENU_VIEW_FRAGMENT,
    ROUTINE_LEO_TEXTS,
    PREMIUM_DECLINE_FRAGMENT,
    CITY_CHECK_FRAGMENTS,
    CITY_CHECK_ANSWER,
    CAPTCHA_MIN_BUTTONS,
    CAPTCHA_MARKERS,
    CAPTCHA_NOTIFIED_STATE_PREFIX,
    captcha_signature,
    is_manual_message_excluded,
)
from models.raw import MessageType


class LeoNavigationMixin:
    """Нажатия кнопок Leo и ответы на капчи (миксин DvinchikCollector)."""

    async def _recent_history(self, limit: int = 15) -> list:
        """Последние ``limit`` сообщений чата Leo на авто-аккаунте (новые первыми).

        Один запрос истории на всю проверку состояния чата. Раньше каждый
        обработчик кнопок (меню, Premium, «Смотреть анкеты», карточка лайка)
        сам дважды скачивал историю — на каждое служебное сообщение Leo
        выходило до ~10 запросов getHistory, то есть несколько в секунду
        при живой ленте: прямой путь к FloodWait.
        """
        client = self._auto_engine.client
        if client is None:
            return []
        try:
            return [
                msg async for msg in client.iter_messages(
                    self._dvinchik_chat_id, limit=limit
                )
            ]
        except Exception as e:
            # Как и раньше (каждый обработчик глотал свою ошибку): сбой
            # чтения истории не роняет автопоток — кнопки просто не жмём.
            logger.error(f"AutoAction: не удалось прочитать историю чата Leo: {e}")
            return []

    async def _continue_stream_if_needed(self, history: list) -> bool:
        """Продолжает ленту Leo одной кнопкой: «Смотреть анкеты» → отказ от
        Premium → «Нет» на «ты живешь в …?» → «1 🚀» в меню. Останавливается
        на первой нажатой — после
        нажатия ``history`` устарела, следующий шаг сделает уже новое
        сообщение Leo. Все проверки — по одной выборке истории.
        """
        for press in (
            self._press_view_button_if_needed,
            self._press_premium_decline_if_needed,
            self._answer_city_check_if_needed,
            self._press_menu_view_if_needed,
        ):
            if await press(history):
                return True
        return False

    async def _press_view_button_if_needed(
        self, history: list | None = None,
    ) -> bool:
        """Нажимает кнопку «Смотреть анкеты», если Leo её показал.

        При исчерпании ленты Leo присылает промо-сообщение с reply-кнопкой
        «🚀 Смотреть анкеты» — нажатие (отправка текста кнопки) продолжает
        поток. Работает только если активной анкеты нет (иначе кнопка не
        относится к продолжению). Сканируем последние сообщения чата на
        авто-аккаунте; если найдено сообщение с такой кнопкой и после него
        ещё не отправлен текст кнопки (идемпотентность) — отправляем.
        """
        client = self._auto_engine.client
        if client is None or not self._auto_engine.enabled:
            return False
        try:
            if history is None:
                history = await self._recent_history()
            card_msg = None
            button_text = ""
            for msg in history:
                texts = self._extract_button_texts(msg)
                hit = next(
                    (t for t in texts if VIEW_BUTTON_FRAGMENT in t), None
                )
                if hit:
                    card_msg = msg
                    button_text = hit
                    break

            if card_msg is None or not button_text:
                return False

            # Идемпотентность: если после карточки уже отправлен текст кнопки,
            # не нажимаем повторно (тот же поток уже продолжен).
            sent_at = card_msg.id
            already_sent = False
            for msg in history:
                if msg.id <= sent_at:
                    break
                if (
                    getattr(msg, "out", False)
                    and (msg.text or "").strip() == button_text
                ):
                    already_sent = True
                    break

            if already_sent:
                logger.info("AutoAction: кнопка «Смотреть анкеты» уже нажата")
                return False

            logger.info(
                f"AutoAction: найдена кнопка «Смотреть анкеты» (msg={card_msg.id}) — "
                "продолжаю ленту"
            )
            await self._auto_engine.send_text(button_text)
            return True
        except Exception as e:
            logger.error(f"AutoAction: ошибка нажатия кнопки «Смотреть анкеты»: {e}")
            return False

    async def _press_menu_view_if_needed(
        self, history: list | None = None,
    ) -> bool:
        """Нажимает «1 🚀» в ГЛАВНОМ МЕНЮ Leo, если чат стоит на нём.

        Когда лента исчерпана, Leo шлёт текстовое меню «1. Смотреть анкеты. …
        4. Активируй Premium» с reply-клавиатурой [«1 🚀», «2», «3», «4»].
        Кнопки-промо «🚀 Смотреть анкеты» здесь нет, поэтому
        ``_press_view_button_if_needed`` молча выходит, и без ручного нажатия
        лента стоит навсегда (типично после переключения авто-аккаунта, у
        которого чат уже был в этом состоянии).

        Жмём «1 🚀» ТОЛЬКО если самая свежая карточка с кнопками — это меню:
        тогда карточка взаимного лайка / Premium-промо / капча (у них свои
        обработчики, они проверяются раньше) не будет перебита нажатием.
        Идемпотентность — как у view-кнопки: текст кнопки не отправлялся после
        этой карточки. Сравнение id корректно, потому что оба прохода идут
        через клиент авто-аккаунта (у каждого аккаунта своё пространство id).
        """
        client = self._auto_engine.client
        if client is None or not self._auto_engine.enabled:
            return False
        try:
            if history is None:
                history = await self._recent_history()
            card_msg = None
            button_text = ""
            for msg in history:
                if getattr(msg, "out", False) or not (msg.text or "").strip():
                    continue
                texts = self._extract_button_texts(msg)
                if not texts:
                    # фото/текст без кнопок (карточка анкеты, сервис) —
                    # состояние чата не меняет, смотрим дальше.
                    continue
                # Самая свежая карточка с кнопками определяет состояние чата.
                card_msg = msg
                hit = next((t for t in texts if MENU_VIEW_FRAGMENT in t), None)
                button_text = hit or ""
                break

            if card_msg is None or not button_text:
                return False

            sent_at = card_msg.id
            already_sent = False
            for msg in history:
                if msg.id <= sent_at:
                    break
                if (
                    getattr(msg, "out", False)
                    and (msg.text or "").strip() == button_text
                ):
                    already_sent = True
                    break

            if already_sent:
                logger.info("AutoAction: кнопка меню «1 🚀» уже нажата")
                return False

            logger.info(
                f"AutoAction: чат стоит на главном меню Leo (msg={card_msg.id}) — "
                "жму «1 🚀», продолжаю ленту"
            )
            await self._auto_engine.send_text(button_text)
            return True
        except Exception as e:
            logger.error(f"AutoAction: ошибка нажатия кнопки меню «1 🚀»: {e}")
            return False

    async def _press_premium_decline_if_needed(
        self, history: list | None = None,
    ) -> bool:
        """Нажимает кнопку-отказ у Premium-промо Leo («Пока без Premium»).

        После ❤️/👎 Leo иногда присылает премиум-реклму («Борюсь с
        эгоизмом…», кнопки [⭐️Активировать, Пока без Premium]) — без
        нажатия отказной кнопки лента встаёт. Ищем самую свежую карточку
        с кнопкой, содержащей PREMIUM_DECLINE_FRAGMENT, и нажимаем её
        (идемпотентно: только если текст кнопки ещё не отправлялся после
        карточки). Кнопку «Активировать» никогда не нажимаем.
        """
        client = self._auto_engine.client
        if client is None or not self._auto_engine.enabled:
            return False
        try:
            if history is None:
                history = await self._recent_history()
            card_msg = None
            button_text = ""
            for msg in history:
                texts = self._extract_button_texts(msg)
                hit = next(
                    (t for t in texts if PREMIUM_DECLINE_FRAGMENT in t.lower()), None
                )
                if hit:
                    card_msg = msg
                    button_text = hit
                    break

            if card_msg is None or not button_text:
                return False

            # Идемпотентность: если после карточки уже отправлен текст кнопки,
            # не нажимаем повторно (тот же поток уже продолжен).
            sent_at = card_msg.id
            already_sent = False
            for msg in history:
                if msg.id <= sent_at:
                    break
                if (
                    getattr(msg, "out", False)
                    and (msg.text or "").strip() == button_text
                ):
                    already_sent = True
                    break

            if already_sent:
                logger.info("AutoAction: кнопка «Пока без Premium» уже нажата")
                return False

            logger.info(
                f"AutoAction: Premium-промо (msg={card_msg.id}) — нажимаю "
                f"«{button_text}», продолжаю ленту"
            )
            await self._auto_engine.send_text(button_text)
            return True
        except Exception as e:
            logger.error(f"AutoAction: ошибка нажатия кнопки Premium-промо: {e}")
            return False

    async def _answer_city_check_if_needed(
        self, history: list | None = None,
    ) -> bool:
        """Отвечает «Нет» на проверку Leo «<имя>, ты живешь в Питере?».

        Только если это САМАЯ СВЕЖАЯ входящая карточка с кнопками (активная
        клавиатура) и после неё нет нашего исходящего (уже ответили — сами
        или владелец вручную).
        """
        client = self._auto_engine.client
        if client is None or not self._auto_engine.enabled:
            return False
        try:
            if history is None:
                history = await self._recent_history()
            for msg in history:  # новые → старые
                if getattr(msg, "out", False):
                    return False  # после последней клавиатуры уже ответили
                texts = self._extract_button_texts(msg)
                if not texts:
                    continue
                text = (getattr(msg, "text", "") or "").lower()
                if not any(f in text for f in CITY_CHECK_FRAGMENTS):
                    return False  # активная клавиатура — не эта проверка
                answer = next(
                    (t for t in texts if t.strip().lower() == CITY_CHECK_ANSWER),
                    None,
                )
                if answer is None:
                    return False
                logger.info(
                    f"AutoAction: проверка «ты живешь в …?» (msg={msg.id}) — "
                    f"отвечаю «{answer}», продолжаю ленту"
                )
                await self._auto_engine.send_text(answer)
                return True
            return False
        except Exception as e:
            logger.error(f"AutoAction: ошибка ответа на проверку города: {e}")
            return False

    @staticmethod
    def _is_routine_leo_text(text: str) -> bool:
        """Штатное служебное сообщение Leo, не требующее внимания в логе."""
        stripped = (text or "").strip()
        return (
            stripped in ROUTINE_LEO_TEXTS
            or AutoActionEngine.is_composer_prompt(stripped)
        )

    async def _handle_captcha(
        self, msg: object | None = None, *, any_keyboard: bool = False,
    ) -> bool:
        """Разруливает капчу/проверку Leo (Stage 7.6 «память капч»).

        Капча = текст с одним из CAPTCHA_MARKERS и >= CAPTCHA_MIN_BUTTONS
        reply-кнопок (иначе легко зациклиться в меню/Premium).

        Поведение:
        1. Если для этой капчи (сигнатура = нормализованный текст) уже
           выучен ответ владельца — отправляем его (идемпотентно: только
           если текст ещё не отправлялся после карточки капчи).
        2. Если ответа нет и включён fallback_press_last (по умолчанию,
           владелец 2026-11-01: «он должен нажать правую (последнюю)
           кнопку») — жмём последнюю ОБЫЧНУЮ кнопку и запоминаем её как
           ответ (``learn_captcha_answer``): на сайте строка «🧩 Капча: бот
           нажал …». При fallback_press_last=false капча записывается в
           captcha_memory (pending) и ждёт ответа в блоке «Капчи ждут ответа».
        3. Если капча-память выключена (captcha.enabled=false) — полностью
           старое поведение: нажимаем последнюю обычную кнопку.

        ``msg`` — конкретное сообщение капчи из live-обработки (уже есть);
        если None — сканируем последние сообщения чата (start_auto_stream).
        ``any_keyboard`` — капчей считается и сообщение без маркеров, но с
        незнакомой клавиатурой (``_is_unknown_keyboard``).
        Возвращает True, если на капчу отправлен текст.
        """
        client = self._auto_engine.client
        if client is None or not self._auto_engine.enabled:
            return False
        try:
            if msg is not None:
                card = (
                    msg if self._is_captcha_message(msg)
                    or (any_keyboard and self._is_unknown_keyboard(msg))
                    else None
                )
            else:
                card = await self._find_captcha_message()
            if card is None:
                return False

            # Сигнатура капчи: одинаковый текст — одинаковая капча, один ответ.
            text_full = (getattr(card, "text", None) or "").strip()
            signature = captcha_signature(text_full)
            buttons = self._extract_button_texts(card)
            cc = self._config.auto_actions.captcha

            if cc.enabled:
                # 1) Выученный ответ — отправляем авто-ответ.
                if cc.auto_answer and self._db is not None:
                    try:
                        answer = await self._db.get_captcha_answer(signature)
                    except Exception as e:
                        logger.error(f"AutoAction: ошибка чтения ответа капчи: {e}")
                        answer = None
                    if answer:
                        return await self._send_captcha_text(
                            card, answer, used_signature=signature
                        )

                # 2) Неизвестная капча: жмём последнюю кнопку и запоминаем.
                if cc.fallback_press_last:
                    return await self._press_last_captcha_button(
                        card, signature, text_full, buttons,
                    )
                # Без fallback — выводим на сайт, наугад не нажимаем.
                if self._db is not None:
                    try:
                        await self._db.record_pending_captcha(
                            signature,
                            text_full,
                            json.dumps(buttons, ensure_ascii=False),
                        )
                    except Exception as e:
                        logger.error(f"AutoAction: ошибка записи pending-капчи: {e}")
                logger.warning(
                    f"AutoAction: неизвестная капча (msg={card.id}) — жду ответа "
                    f"владельца в блоке «Капчи ждут ответа» на главном экране"
                )
                await self._notify_unknown_captcha(card, text_full, buttons)
                return False

            # 3) Обычная кнопка: последняя ОБЫЧНАЯ reply-кнопка (не спец-запрос).
            # Спец-кнопки (гео/телефон) текстом не нажимаются — нужен реальный
            # маркап, поэтому в гео-капче берём «Продолжить смотреть анкеты».
            plain = self._extract_plain_button_texts(card)
            if not plain:
                return False
            return await self._send_captcha_text(card, plain[-1])
        except Exception as e:
            logger.error(f"AutoAction: ошибка обработки капчи: {e}")
            return False

    async def _press_last_captcha_button(
        self, card: object, signature: str, text_full: str, buttons: list[str],
    ) -> bool:
        """Жмёт последнюю (правую) обычную кнопку неизвестной капчи.

        Нажатая кнопка запоминается как ответ (в следующий раз — сразу
        ею), на сайте над лентой — строка «🧩 Капча: бот нажал …».
        """
        plain = self._extract_plain_button_texts(card)
        if not plain:
            return False
        answer = plain[-1]
        sent = await self._send_captcha_text(card, answer)
        if not sent:
            return False
        logger.warning(
            f"AutoAction: неизвестная капча (msg={card.id}) — нажал последнюю "
            f"кнопку «{answer}» и запомнил ответ"
        )
        if self._db is not None:
            try:
                await self._db.learn_captcha_answer(
                    signature, text_full,
                    json.dumps(buttons, ensure_ascii=False), answer,
                )
            except Exception as e:
                logger.error(f"AutoAction: не запомнил ответ капчи: {e}")
        try:
            await self._auto_engine.notify_owner(
                f"🧩 Капча Leo: нажал «{answer}»\n\n{text_full[:300]}"
            )
        except Exception as e:
            logger.error(f"AutoAction: уведомление о капче: {e}")
        return True

    async def _send_captcha_text(
        self, card: object, text: str, used_signature: str | None = None
    ) -> bool:
        """Отправляет текст (выученный ответ или кнопку) на капчу. Идемпотентно."""
        try:
            if await self._captcha_text_sent_after(card.id, text):
                logger.info(
                    f"AutoAction: ответ на капчу «{text}» уже отправлен "
                    f"(msg={card.id})"
                )
                return False
            logger.info(
                f"AutoAction: капча/проверка (msg={card.id}) — отвечаю «{text}»"
            )
            await self._auto_engine.send_text(text)
            if used_signature and self._db is not None:
                try:
                    await self._db.mark_captcha_used(used_signature)
                except Exception as e:
                    logger.error(f"AutoAction: mark_captcha_used error: {e}")
            return True
        except Exception as e:
            logger.error(f"AutoAction: ошибка отправки текста на капчу: {e}")
            return False

    async def _captcha_text_sent_after(self, msg_id: int, text: str) -> bool:
        """Отправлялся ли уже ``text`` после сообщения капчи (идемпотентность)."""
        client = self._auto_engine.client
        if client is None:
            return False
        try:
            async for msg in client.iter_messages(self._dvinchik_chat_id, limit=15):
                if msg.id <= msg_id:
                    break
                if (
                    getattr(msg, "out", False)
                    and (msg.text or "").strip() == text
                ):
                    return True
        except Exception as e:
            logger.error(f"AutoAction: ошибка проверки уже отправленного ответа: {e}")
        return False

    async def _notify_unknown_captcha(
        self, card: object, text: str, buttons: list[str],
    ) -> None:
        """Один раз на сообщение капчи спрашивает владельца в Telegram.

        Раньше неизвестная капча была видна только на сайте: лента стояла
        молча (прод 2026-11-01, «131 бомбоубежище…» на обоих аккаунтах).
        """
        session = self._current_session()
        key = f"{CAPTCHA_NOTIFIED_STATE_PREFIX}:{session}"
        card_id = str(getattr(card, "id", ""))
        try:
            if self._db is not None:
                if await self._db.get_state(key) == card_id:
                    return
                await self._db.set_state(key, card_id)
        except Exception as e:
            logger.error(f"AutoAction: метка уведомления о капче: {e}")
        variants = " | ".join(buttons) if buttons else "—"
        await self._auto_engine.notify_owner(
            f"🧩 Leo ждёт ответа на {session or 'аккаунте'} — лента стоит:\n\n"
            f"{text[:500]}\n\nКнопки: {variants}\n\nВыбери ответ на сайте в "
            "блоке «Капчи ждут ответа» (бот запомнит его) или нажми сам в Leo."
        )

    def _is_unknown_keyboard(self, msg: object) -> bool:
        """Незнакомое сообщение Leo с кнопками — ждёт ответа, ленту держит.

        ≥ ``CAPTCHA_MIN_BUTTONS`` кнопок, и ни одна не из известных
        (реакции, меню, Premium, «Да/Нет», «Вернуться назад», «Показать»…,
        ``is_manual_message_excluded``). Прод 2026-11-01: «Сохрани… 131
        бомбоубежище… Отправить вам адреса?» [Получить список | Поделюсь с
        другом] — маркеров капчи нет, бот молча встал. Карточка «Ты
        понравился…» и вопрос о городе сюда не относятся.
        """
        if getattr(msg, "out", False):
            return False
        buttons = self._extract_button_texts(msg)
        if len(buttons) < CAPTCHA_MIN_BUTTONS:
            return False
        if any(is_manual_message_excluded(b.strip()) for b in buttons):
            return False
        # Цифровые кнопки — главное меню Leo («2», «3», «4»), не вопрос.
        if any(b.strip().isdigit() for b in buttons):
            return False
        low = (getattr(msg, "text", None) or "").lower()
        if any(f in low for f in CITY_CHECK_FRAGMENTS):
            return False
        return not self._is_match_card(msg)

    def _is_captcha_message(self, msg: object) -> bool:
        """Является ли сообщение капчей/сделкой/проверкой Leo.

        Текст содержит один из CAPTCHA_MARKERS, а reply-кнопок — не меньше
        CAPTCHA_MIN_BUTTONS. Меню/Premium-промо без маркеров НЕ капча.
        """
        text = (getattr(msg, "text", None) or "").lower()
        if not any(m in text for m in CAPTCHA_MARKERS):
            return False
        return len(self._extract_button_texts(msg)) >= CAPTCHA_MIN_BUTTONS

    async def _find_captcha_message(self) -> object | None:
        """Ищет самую свежую капчу среди последних сообщений чата."""
        client = self._auto_engine.client
        if client is None:
            return None
        try:
            newest = True
            async for msg in client.iter_messages(self._dvinchik_chat_id, limit=15):
                if self._is_captcha_message(msg):
                    return msg
                # Незнакомая клавиатура — только если это последнее сообщение
                # (старые давно разобраны).
                if newest and self._is_unknown_keyboard(msg):
                    return msg
                newest = False
        except Exception as e:
            logger.error(f"AutoAction: ошибка поиска капчи: {e}")
        return None

    def _extract_button_texts(self, msg: object) -> list[str]:
        """Извлекает тексты reply-кнопок сообщения (пусто, если их нет).

        Возвращает ВСЕ кнопки (включая специальные запросы геолокации/телефона) —
        используются только для подсчёта количества кнопок капчи.
        """
        rm = getattr(msg, "reply_markup", None)
        rows = getattr(rm, "rows", None)
        if not rows:
            return []
        out: list[str] = []
        for row in rows:
            for b in getattr(row, "buttons", []):
                t = getattr(b, "text", "")
                if t:
                    out.append(t)
        return out

    def _extract_plain_button_texts(self, msg: object) -> list[str]:
        """Как _extract_button_texts, но только ОБЫЧНЫЕ KeyboardButton.

        Специальные кнопки (запрос геолокации/телефона и т.п.) нельзя «нажать»
        простой отправкой их текста — Leo ответит «Нет такого варианта ответа».
        Для нажатия выбираем только plain-кнопки (все, кроме special-исключений;
        класс обычной кнопки = KeyboardButton).
        """
        special: tuple[str, ...] = (
            "KeyboardButtonRequestGeoLocation",
            "KeyboardButtonRequestPhone",
            "KeyboardButtonRequestPeer",
            "KeyboardButtonUrl",
            "KeyboardButtonCallback",
            "KeyboardButtonInline",
            "KeyboardButtonSwitchInline",
            "KeyboardButtonGame",
            "KeyboardButtonPay",
        )
        rm = getattr(msg, "reply_markup", None)
        rows = getattr(rm, "rows", None)
        if not rows:
            return []
        out: list[str] = []
        for row in rows:
            for b in getattr(row, "buttons", []):
                t = getattr(b, "text", "")
                if t and type(b).__name__ not in special:
                    out.append(t)
        return out

    def _is_profile_text(self, msg: object) -> bool:
        """Является ли текст сообщения анкетой (PROFILE)."""
        text = (getattr(msg, "text", None) or "").strip()
        if not text:
            return False
        return self._parser.classify(text) == MessageType.PROFILE

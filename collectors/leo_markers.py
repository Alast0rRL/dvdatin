# Маркеры и тексты Leo (Дайвинчик), общие для коллектора и его миксинов:
# фрагменты кнопок, ключи app_state, маркеры капч, разбор медиа.
# Вынесено из collectors/dvinchik_collector.py без изменений.

from __future__ import annotations

import re
from contextvars import ContextVar

from rich.console import Console
from telethon.tl.types import (
    MessageMediaDocument,
    MessageMediaGeo,
    MessageMediaPhoto,
    MessageMediaPoll,
    MessageMediaWebPage,
)

console = Console(force_terminal=True)

_MEDIA_TYPE_MAP: dict[type, str] = {
    MessageMediaPhoto: "photo",
    MessageMediaDocument: "document",
    MessageMediaGeo: "geo",
    MessageMediaWebPage: "webpage",
    MessageMediaPoll: "poll",
}

#: Кнопка Дайвинчика, которой продолжают показ анкет после исчерпания ленты:
#: Leo присылает промо «🚀 Смотреть анкеты», нажатие = отправка текста кнопки.
#: Парсим по частичному вхождению, т.к. эмодзи могут различаться.
VIEW_BUTTON_FRAGMENT: str = "Смотреть анкеты"

#: Кнопка «Смотреть анкеты» в ГЛАВНОМ МЕНЮ Leo. Когда лента исчерпана (или
#: после переключения аккаунта) бот шлёт текстовое меню «1. Смотреть анкеты.
#: 2. Моя анкета. …» с reply-клавиатурой [«1 🚀», «2», «3», «4»] — вот тут
#: VIEW_BUTTON_FRAGMENT не встречается (текст кнопки «1 🚀»), и лента ВСТАЁТ
#: до ручного нажатия. Жмём кнопку с этим фрагментом; остальные («2»/«3»/«4» —
#: «Моя анкета», «Я больше не хочу никого искать», Premium) НИКОГДА не жмём.
MENU_VIEW_FRAGMENT: str = "1 🚀"

#: Ключ app_state: последний сбой автопотока — бот не смог продолжить ленту
#: анкет (ни активной анкеты, ни кнопки продолжения, ни капчи). Хранит JSON
#: для баннера на сайте; владельцу шлётся уведомление в Telegram.
STREAM_ERROR_STATE_KEY: str = "stream_error"
#: Как часто повторно уведомлять владельцу о сбое (сек) — чтобы не спамить,
#: если лента стоит долго.
STREAM_ERROR_NOTIFY_COOLDOWN_SEC: float = 600.0
#: Сколько последних сообщений чата Leo попадает в диагностический снимок.
STREAM_ERROR_SNAPSHOT_LIMIT: int = 12
#: Ключ app_state: карточка «Ты понравился…», на которой остановлен поиск
#: (``<account_session>:<message_id>``) — чтобы уведомлять владельца один раз.
MATCH_STOP_STATE_KEY: str = "match_stop_card"
#: Режим до остановки на карточке взаимного лайка — его возвращает кнопка
#: «Продолжить» на сайте (``resume_after_match_stop``).
MATCH_STOP_PREV_MODE_KEY: str = "match_stop_prev_mode"

#: Штатные сообщения Leo, которые парсер не относит ни к одному типу, но
#: которые приходят на КАЖДОЙ анкете: в лог — DEBUG, а не WARNING.
ROUTINE_LEO_TEXTS: frozenset[str] = frozenset({"✨\U0001F50D"})  # ✨🔍

#: Сверка ленты с Telegram (``sync_chat_history``): сколько последних сообщений
#: чата Leo читать у каждого аккаунта, как часто и какой «возраст» сообщения
#: оставлять живому хендлеру (моложе — не трогаем, иначе отнимем его у pipeline).
HISTORY_SYNC_LIMIT: int = 100
HISTORY_SYNC_INTERVAL_SEC: float = 60.0
HISTORY_SYNC_MIN_AGE_SEC: float = 120.0

#: Кнопка-отказ от Premium Leo («Пока без Premium»): после ❤️/👎 бот иногда
#: присылает премиум-реклму с кнопками [⭐️Активировать, Пока без Premium].
#: Без нажатия отказной кнопки лента встаёт (новые анкеты/капчи не идут).
#: Нажимаем ТОЛЬКО кнопку с этим фрагментом («Активировать» — никогда).
PREMIUM_DECLINE_FRAGMENT: str = "без premium"

#: Проверка Leo «<имя>, ты живешь в Питере?» с кнопками [Да, Нет] (прод
#: 2026-10-27/29: лента вставала до ручного ответа). По просьбе владельца
#: всегда отвечаем «Нет». Узнаём по фрагменту текста + кнопке «Нет».
CITY_CHECK_FRAGMENTS: tuple[str, ...] = ("живешь в", "живёшь в")
CITY_CHECK_ANSWER: str = "нет"

#: Карточка взаимного лайка Leo: «Ты понравился 3 девушке, показать её?» с
#: кнопками [«1. Показать.», «2. Не хочу больше никого смотреть.»]. Бот НЕ
#: жмёт ни одну кнопку: такая карточка ОСТАНАВЛИВАЕТ поиск (режим → OBSERVE,
#: см. ``_stop_on_match_card_if_needed``) — владелец разбирает лайки сам.
MATCH_CARD_FRAGMENT: str = "понравил"
MATCH_SHOW_FRAGMENT: str = "показать"
#: Другой вариант того же события — без кнопок, посреди ленты (прод
#: 2026-10-29, dvai msg=744851): «Кому-то понравилась твоя анкета!
#: Заканчивай с вопросом выше и посмотрим кто это». Leo покажет её после
#: ответа на текущую анкету — поиск тоже останавливаем.
MATCH_NOTICE_FRAGMENTS: tuple[str, ...] = ("понравилась твоя анкета", "посмотрим кто")
MATCH_SHOW_SKIP_FRAGMENT: str = "не хочу"
#: На реальных карточках Leo клавиатура нумерованная: [«1 👍», «2 💤»],
#: т.е. слова «показать» в тексте кнопки НЕТ (он только в тексте карточки).
#: Поэтому «показать» ловим ещё и по 👍; отказной «2 💤» отсекается по MATCH_SHOW_SKIP_FRAGMENT.
MATCH_SHOW_ALT_FRAGMENT: str = "👍"
MATCH_SHOW_SKIP_ALT_FRAGMENT: str = "💤"

#: Stage 8.5: тексты, которые НЕ считаются ручным «сообщением при лайке»
#: (кнопки Leo/эмодзи-реакции/навигация). Они не попадают в sent_messages,
#: чтобы навигация и реакции не засоряли аналитику текстов.
MANUAL_MESSAGE_EXCLUDE: frozenset[str] = frozenset(
    {
        "❤️",
        "👎",
        "💤",
        "💌",
        "💌 📹 🎤",
        "🌸",
        "🚀 Смотреть анкеты",
        "Смотреть анкеты",
        "Возможно позже",
        "Продолжить смотреть анкеты",
        "Продолжить",
        "Начать",
        "← Назад",
        "Вернуться назад",
        "👌 Готово",
        "Готово",
        "Активировать Premium",
        "Пока без Premium",
        "Показать девушку",
        "Да",
        "Нет",
    }
)

#: Stage 8.5: фрагменты навигационных кнопок Leo. Точного совпадения не
#: хватает: кнопки главного меню приходят с номерами («1 🚀»,
#: «1 👍», «Главное меню»). Ловим по подстроке + шаблону «номер+не-лента».
MANUAL_MESSAGE_EXCLUDE_FRAGMENTS: tuple[str, ...] = (
    "смотреть анкеты",
    "главное меню",
    "возможно позже",
    "продолжить",
    "вернуться назад",
    "активировать premium",
    "пока без premium",
    "без premium",
    "показать девушку",
)

#: Кнопка меню с префиксом-номером: «1 🚀», «2 👍» и т.п.
#: (после цифры — НЕ буква и НЕ пробел; «2 часа назад…» не отсекается).
MANUAL_MESSAGE_NUMBERED_RE: re.Pattern[str] = re.compile(r"^\s*\d+\s*[^\wа-яё\s-]", re.IGNORECASE)


def is_manual_message_excluded(text: str) -> bool:
    """Кнопка/навигация Leo (не ручное сообщение при лайке).

    Точное совпадение не срабатывает на нумерованные кнопки меню
    («1 🚀», «Главное меню», «1 👍») — используем подстроки и шаблон
    «цифра + не-буква» (Анкета с цифры не начинается).
    """
    if text in MANUAL_MESSAGE_EXCLUDE:
        return True
    low = text.lower()
    if any(f in low for f in MANUAL_MESSAGE_EXCLUDE_FRAGMENTS):
        return True
    if MANUAL_MESSAGE_NUMBERED_RE.match(text):
        return True
    return False

#: На любые "проверки"/капчи Leo (сделка, подписка, подтверждение и т.п.)
#: авто-аккаунт всегда нажимает ПОСЛЕДНЮЮ reply-кнопку — это сбрасывает
#: диалог и продолжает ленту (в конкретном случае «Возможно позже»).
#: Срабатывает ТОЛЬКО на явные капчи/сделки: текст сообщения должен содержать
#: один из маркеров CAPTCHA_MARKERS (иначе легко зациклиться в меню/Premium).
#: Кнопок при этом должно быть >= CAPTCHA_MIN_BUTTONS.
CAPTCHA_MIN_BUTTONS: int = 2

#: Маркеры текста, по которым сообщение считается капчей/сделкой/проверкой
#: (а не меню/Premium-промо). Любой из них (без учёта регистра) включает
#: авто-ответ кнопкой. «расположени/геолокаци/координат» ловят гео-капчу
#: Leo («Пришли свое расположение…»), где обычной кнопкой отказываются от
#: передачи координат и продолжают ленту.
CAPTCHA_MARKERS: tuple[str, ...] = (
    "сделк",
    "подписываешься",
    "подпишись",
    "подтверд",
    "верификац",
    "капч",
    "проверк",
    "ты подписываешься",
    "@leoday",
    "расположени",
    "геолокаци",
    "координат",
)


def captcha_signature(text: str) -> str:
    """Нормализует текст капчи в сигнатуру для сопоставления (Stage 7.6).

    Нижний регистр + схлопывание всех пробельных символов: одинаковое
    сообщение с разными переносами строк/отступами даёт одну сигнатуру.
    Используется как ключ в captcha_memory (выученные ответы владельца).
    """
    return " ".join((text or "").lower().split())


def _detect_media_type(msg: object) -> str:
    """Определяет тип media через MessageMedia объект."""
    if not hasattr(msg, "media") or msg.media is None:
        return ""

    media = msg.media
    media_type = type(media)

    if media_type in _MEDIA_TYPE_MAP:
        return _MEDIA_TYPE_MAP[media_type]

    if media_type == MessageMediaDocument and hasattr(media, "document"):
        doc = media.document
        if hasattr(doc, "mime_type"):
            mime = doc.mime_type
            if "video" in mime:
                return "video"
            if "gif" in mime:
                return "gif"
            if "sticker" in getattr(doc, "attributes", [None])[0] if doc.attributes else False:
                return "sticker"
            if "voice" in mime or "audio" in mime:
                return "voice"
        return "document"

    return "unknown"


#: Аккаунт (session), от имени которого сейчас идёт обработка. Ставится на
#: входе хендлеров Telegram, в worker'е (по ``RawTask.account_session``) и в
#: циклах по аккаунтам; ``DvinchikCollector._auto_engine`` по нему выбирает
#: движок авто-действий ЭТОГО аккаунта (у каждого аккаунта свой режим и
#: своя лента Leo). None — основной аккаунт (``auto_actions.account_session``).
_CURRENT_SESSION: ContextVar[str | None] = ContextVar(
    "dvai_current_session", default=None,
)

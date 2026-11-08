"""Разбор ответа девушки для сценария знакомства (Telegram-free).

Чистые правила по словам: один текст → один вид ответа. Что правила не
узнали — ``OTHER`` (по сценарию это обычно передача переписки владельцу).
Шаги сценария и переходы — тоже здесь (``decide``), чтобы их можно было
проверить без Telegram.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

#: Виды её ответа.
GO_AWAY: str = "go_away"          # «не пиши», «есть парень» → удалить чат
RUDE: str = "rude"                # прямое оскорбление → удалить чат
ASKS_BOT: str = "asks_bot"        # «ты бот?» → владельцу (бот не врёт)
NOT_DAMOCHKA: str = "not_damochka"
ASKS_TOO_MANY: str = "asks_too_many"
ASKS_PIN: str = "asks_pin"        # «а зачем закрепил?» / «почему скинул?»
ASKS_WHAT: str = "asks_what"      # «что это / зачем» про анкету
ABOUT_WHAT: str = "about_what"    # «о чём? / что?»
#: Её собственный вопрос, правилам незнакомый («ну вы любите розы?») — нужен
#: живой ответ: бот молчит, переписка владельцу (владелец, 2026-11-04).
HER_QUESTION: str = "her_question"
OTHER: str = "other"
#: Стикер / эмодзи / короткое «привет» — единственное, на что на шаге after_pin
#: отвечает «Рассказывайте»; любой другой текст — владельцу (2026-11-07).
GREETING: str = "greeting"
#: «и?» / «и что?» / «ну и?» — ждёт продолжения от владельца: без ответа бота.
AND_SO: str = "and_so"
#: «мы знакомы?» / «кто ты?» / «откуда ты?» (владелец, 2026-11-07).
ASKS_KNOW: str = "asks_know"
#: «а твою анкету можно?» / «скинь свою» / «как ты выглядишь?» → бот пересылает
#: анкету владельца из чата Leo («Моя анкета»; владелец, 2026-11-07).
ASKS_YOUR_CARD: str = "asks_your_card"
#: Ключ «фразы» — не текст, а пересылка анкеты владельца.
OWN_CARD: str = "own_card"

#: Шаги сценария (``match_dialogs.script_step``).
AFTER_PIN: str = "after_pin"      # анкета закреплена, ждём её
WAIT_MORE: str = "wait_more"      # ответили «Это твоя анкета…» / «удобно»
TELL_ME: str = "tell_me"          # написали «Рассказывайте, дамочка»
WAKE_UP: str = "wake_up"          # молчала — написали «АЛООО ПРОСЫПАЕМСЯ»
#: Конечные шаги.
HANDOFF: str = "handoff"          # дальше пишет владелец
OWNER: str = "owner"              # владелец сам вмешался
STOPPED: str = "stopped"          # молчит — бот больше не пишет
DELETED: str = "deleted"          # грубость / «не пиши» — чат удалён
ERROR: str = "error"

ACTIVE_STEPS: tuple[str, ...] = (AFTER_PIN, WAIT_MORE, TELL_ME, WAKE_UP)
#: Виды, после которых чат удаляется.
STOP_KINDS: frozenset[str] = frozenset({GO_AWAY, RUDE})

#: Важность вида: при нескольких её сообщениях подряд побеждает важнейший.
_PRIORITY: dict[str, int] = {
    GO_AWAY: 7, RUDE: 7, ASKS_BOT: 6,
    # Вопрос про закреп важнее общего «зачем»: «Ну да, я» + «А для чего
    # закрепил?» — ответ про закреп, не «Пусть лежит здесь» (владелец, 2026-11-03).
    ASKS_PIN: 5,
    # «Мы знакомы?» важнее её прочего вопроса, слабее закрепа.
    ASKS_KNOW: 4,
    # Просьба показать его анкету — её вопрос, на который у бота есть ответ.
    ASKS_YOUR_CARD: 4,
    # Её свой вопрос важнее переспроса: «а че» + «ну вы любите розы?» — не
    # «О себе конечно», а владельцу (прод 2026-11-04, @Pivo2007).
    HER_QUESTION: 3,
    NOT_DAMOCHKA: 2, ASKS_TOO_MANY: 2, ASKS_WHAT: 2, ABOUT_WHAT: 2,
    OTHER: 1,
    GREETING: 0,
    AND_SO: 3,
}

_RULES: list[tuple[str, list[str]]] = [
    (GO_AWAY, [
        r"не\s+(надо\s+)?(мне\s+)?пиши", r"не\s+надо\s+(мне\s+)?писать",
        r"не\s+пишите", r"отстан", r"отвал", r"отвянь", r"мне\s+не\s+интересн",
        r"(есть|у\s+меня)\s+(уже\s+)?(есть\s+)?(парень|муж|молодой\s+человек)",
        r"заблокир", r"не\s+хочу\s+(с\s+тобой\s+)?общат",
    ]),
    (RUDE, [
        r"иди\s+(на\s*х|нах|в\s+жоп|в\s+пизд|лесом)", r"пош[её]л\s+(на\s*х|нах|в\s+жоп)",
        r"нахуй\s+(иди|пош)", r"отъеб", r"съеб", r"\bеблан", r"\bдебил\b",
        r"\bмудак", r"долбо[её]б", r"\bпридур(ок|ки)\b", r"\bчмо\b", r"\bурод\b",
        r"\bуебок", r"\bуёбок",
    ]),
    (ASKS_BOT, [
        r"\bбот(ом|ы|а)?\b", r"нейросет", r"автоответ", r"chat\s*gpt", r"\bгпт\b",
        r"\bробот",
    ]),
    (ASKS_YOUR_CARD, [
        r"\b(тво|сво|ваш)\w*\s+(\w+\s+)?анкет", r"у\s+(тебя|вас)\s+(\w+\s+)?анкет",
        r"\b(тво|сво|ваш)\w*\s+(\w+\s+)?(фот|фоч|селф)",
        r"как\s+(ты|вы)\s+(\w+\s+)?выгляд",
        r"(покажи|покажешь|покажись|скинь|скинешь|кинь|кинешь|отправь|пришли|"
        r"пришлешь|пришлёшь)\s+(\w+\s+)?(себя|фот|фоч|селф|анкет)",
        r"(посмотреть|глянуть|увидеть)\s+(на\s+)?(тебя|вас)\b",
        r"(тебя|вас)\s+(\w+\s+)?(посмотреть|глянуть|увидеть)",
    ]),
    (NOT_DAMOCHKA, [
        r"не\s+дамочк", r"какая\s+(я\s+)?(тебе\s+)?дамочк", r"\bя\s+не\s+дама\b",
        r"дамочк\w*\s*\?", r"сам\s+ты\s+дам",
    ]),
    (ASKS_TOO_MANY, [
        r"много\s+(с\s+кем|кем|девуш|девоч|анкет|общ|переписк|таких)",
        r"не\s+перепутать", r"не\s+путать", r"чтоб(ы)?\s+не\s+(за|пере)?пута",
        r"столько\s+(девуш|девоч|анкет)", r"конвейер", r"коллекци",
        # «Помечаешь чтоб не забыть или че» — то же «чтобы не перепутать»
        # (прод 2026-11-04: бот ответил «Рассказывайте, дамочка»)
        r"не\s+забы(ть|л|ва)",
    ]),
    (ASKS_PIN, [
        r"закреп", r"запин",
        # «почему ты это скинул мне» — тот же ответ, что на закреп (владелец, 2026-11-04)
        r"\bс?кинул\b", r"\bс?кидываешь\b", r"\bкидаешь\b", r"\bотправил\b",
        r"\bприслал\b", r"\bпереслал\b",
        # «зачем пометил?» / «помечаешь?» — тот же закреп (2026-11-04)
        r"\bпомеч", r"\bпометил",
    ]),
    (ASKS_KNOW, [
        r"\bзнаком(ы|а|ство)\b", r"\bкто\s+(ты|это|вы|таков)\b",
        r"откуда\s+(ты|вы)\b", r"(где[- ]то|когда[- ]то)\s+виделись",
        r"\bмы\s+(с\s+тобой\s+)?виделись",
    ]),
    (ASKS_WHAT, [
        r"что\s+это", r"это\s+что", r"зачем", r"нафига", r"нахрена", r"нафиг",
        r"к\s+ч[её]му", r"для\s+чего", r"почему\s+(ты\s+)?(мою|моя|это)",
        r"\bмо(я|ю)\s+анкет", r"что\s+за\b", r"\bэто\s+я\s*\?",  # «это я?» — вопрос; «Ага это я» — подтверждение, не вопрос
        r"в\s+ч[её]м\s+(прикол|смысл)", r"смысл",
    ]),
    (ABOUT_WHAT, [
        r"о\s+ч[её]м", r"про\s+что", r"(что|чего|че|чё)\s+рассказ",
        r"что\s+(именно|конкретно)", r"\bнапример\b",
    ]),
]

_COMPILED: list[tuple[str, list[re.Pattern[str]]]] = [
    (kind, [re.compile(p) for p in patterns]) for kind, patterns in _RULES
]

#: Короткое приветствие без содержания.
_GREETING = re.compile(
    r"^(при+в(е+т\w*|ик\w*)?|хай|хей|хэй|ку+|здравствуй\w*|здаров\w*|здорово|хелло|hi|hello|hey|"
    r"салют|доброе\s+утро|добрый\s+(день|вечер)|добрый)[\s!.,)(\-~]*$"
)

#: «и?», «и что?», «ну и?», «Моя анкета в дв, и?» — вопрос-подталкивание.
_AND_SO = re.compile(r"(^|[\s,.!)(-])и(\s+(что|чё|че|чо|дальше|потом))?\s*\?+[)!.]*$")

#: Короткое «что?» / «?» без слов — переспрос.
_BARE_QUESTION = re.compile(r"^(а\s+)?(что|чего|че|чё|шо|эм+|э+|\?)?\s*\?*[)!.]*$")


def normalize(text: str) -> str:
    """Нижний регистр и схлопнутые пробелы (правила знают и «е», и «ё»)."""
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


#: «это твоя анкета?» — она спрашивает про присланную ей анкету, а не просит
#: его: не ``ASKS_YOUR_CARD``.
_THIS_IS_YOUR_CARD = re.compile(r"\bэто\s+(\w+\s+)?тво\w*\s+анкет")


def classify(text: str) -> str:
    """Вид её ответа. Пустой текст (стикер, фото) и эмодзи — ``GREETING``."""
    norm = normalize(text)
    if not norm:
        return GREETING
    for kind, patterns in _COMPILED:
        if kind == ASKS_YOUR_CARD and _THIS_IS_YOUR_CARD.search(norm):
            continue
        if any(p.search(norm) for p in patterns):
            if kind in (ASKS_WHAT, ABOUT_WHAT) and _AND_SO.search(norm):
                return AND_SO  # «Моя анкета в дв, и?» — не «Это твоя анкета…»
            return kind
    if _AND_SO.search(norm):
        return AND_SO
    if _BARE_QUESTION.match(norm):
        return ABOUT_WHAT
    if "?" in norm:
        return HER_QUESTION
    if _GREETING.match(norm) or not any(c.isalnum() for c in norm):
        return GREETING  # «привет», «хай», только эмодзи
    return OTHER


_BARE_MARK = re.compile(r"^\?+$")


def classify_before_card(text: str) -> str:
    """Вид её сообщения до анкеты (она ответила на стикер): голый «?» —
    как «мы знакомы?»."""
    if _BARE_MARK.match(normalize(text)):
        return ASKS_KNOW
    kind = classify(text)
    # До анкеты просьба показать его анкету — просто её вопрос (слабее
    # «мы знакомы?»): анкета не уходит, владельцу.
    return HER_QUESTION if kind == ASKS_YOUR_CARD else kind


def decide_before_card(texts: list[str]) -> str:
    """Что делать с анкетой по её сообщениям на стикер (сильнейший вид):
    ``know`` — сначала «Лайкнули друг друга…», потом анкета; ``handoff`` —
    её вопрос не про знакомство: анкету не шлём, владельцу; ``card`` — анкета."""
    if not texts:
        return "card"
    kind = strongest([classify_before_card(t) for t in texts])
    if kind == ASKS_KNOW:
        return "know"
    if kind == HER_QUESTION:
        return "handoff"
    return "card"


def priority(kind: str) -> int:
    return _PRIORITY.get(kind, 0)


def strongest(kinds: list[str]) -> str:
    """Важнейший из видов (при равенстве — последний)."""
    best = GREETING
    for kind in kinds:
        if priority(kind) >= priority(best):
            best = kind
    return best


def decide(step: str, kind: str) -> tuple[str | None, str]:
    """(ключ фразы из ``MatchScriptTexts`` или None, следующий шаг).

    Схема владельца (канвас «Знакомство для бота», 2026-11-02):
    анкета → «что это/зачем» и «зачем закрепил?» → «Это твоя анкета из
    Дайвинчика) Закрепил, чтобы не потерять тебя среди чатов» (владелец,
    2026-11-04: «Пусть лежит здесь» смущал девушек); «много с кем общаешься,
    чтобы не перепутать?» → «Это так-то удобно»; пишет о другом →
    «Рассказывайте, дамочка» → «я не дамочка» → «А кто? Парнишка?»;
    «о чём?/что?» → «О себе конечно»; «а твою анкету можно?» (на любом
    шаге) → пересылка анкеты владельца из Leo (``OWN_CARD``). После «Это
    твоя анкета…» / «удобно» и на концах веток любой её ответ — владельцу.
    """
    if kind in STOP_KINDS:
        return None, DELETED
    if kind in (ASKS_BOT, HER_QUESTION, AND_SO):
        return None, HANDOFF
    if kind == ASKS_YOUR_CARD:
        # Пересылка анкеты владельца на любом шаге; дальше — как после
        # «Это твоя анкета…» (её следующий ответ — владельцу).
        return OWN_CARD, WAIT_MORE
    if step in (AFTER_PIN, WAIT_MORE) and kind == ASKS_PIN:
        return "pin_why", WAIT_MORE
    if step in (AFTER_PIN, WAIT_MORE) and kind == ASKS_KNOW:
        return "know_after", WAIT_MORE
    if step == AFTER_PIN:
        if kind in (ASKS_WHAT, ABOUT_WHAT):
            return "card_here", WAIT_MORE
        if kind == ASKS_TOO_MANY:
            return "convenient", WAIT_MORE
        if kind == GREETING:
            return "tell_me", TELL_ME
        # Не знаем, что ответить, — передаём владельцу (2026-11-07).
        return None, HANDOFF
    if step == WAIT_MORE:
        if kind == ASKS_TOO_MANY:
            return "convenient", WAIT_MORE
        return None, HANDOFF
    if step == TELL_ME:
        if kind == NOT_DAMOCHKA:
            return "who_then", HANDOFF
        if kind == ABOUT_WHAT:
            return "about_what", HANDOFF
        return None, HANDOFF
    return None, HANDOFF


def decide_silence(step: str) -> tuple[str | None, str]:
    """Она молчит дольше срока: что делать на шаге.

    После анкеты / «Это твоя анкета…» — «Рассказывайте, дамочка»; после
    «Рассказывайте» — «АЛООО ПРОСЫПАЕМСЯ» (в вечернее окно, см.
    ``in_hour_window``); после него — бот больше не пишет.
    """
    if step in (AFTER_PIN, WAIT_MORE):
        return "tell_me", TELL_ME
    if step == TELL_ME:
        return "wake_up", WAKE_UP
    return None, STOPPED


def in_hour_window(
    now: datetime, start_hour: int, width_hours: int, utc_offset_hours: float,
) -> bool:
    """Местное время ``now`` (UTC + смещение) в окне [start, start+width) часов.

    «АЛООО ПРОСЫПАЕМСЯ» уходит в ближайшие 22:00 после суток тишины: окно
    22:00–24:00; бот лежал в это время — ждёт следующего вечера.
    """
    width = int(width_hours)
    if width <= 0:
        return False
    if width >= 24:
        return True
    local = now + timedelta(hours=float(utc_offset_hours))
    start = int(start_hour) % 24
    end = (start + width) % 24
    h = local.hour
    return start <= h < end if start < end else (h >= start or h < end)

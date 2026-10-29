"""Разбор ответа девушки для сценария знакомства (Telegram-free).

Чистые правила по словам: один текст → один вид ответа. Что правила не
узнали — ``OTHER`` (по сценарию это обычно передача переписки владельцу).
Шаги сценария и переходы — тоже здесь (``decide``), чтобы их можно было
проверить без Telegram.
"""

from __future__ import annotations

import re

#: Виды её ответа.
GO_AWAY: str = "go_away"          # «не пиши», «есть парень» → удалить чат
RUDE: str = "rude"                # прямое оскорбление → удалить чат
ASKS_BOT: str = "asks_bot"        # «ты бот?» → владельцу (бот не врёт)
NOT_DAMOCHKA: str = "not_damochka"
ASKS_TOO_MANY: str = "asks_too_many"
ASKS_WHAT: str = "asks_what"      # «что это / зачем» про анкету
ABOUT_WHAT: str = "about_what"    # «о чём? / что?»
OTHER: str = "other"

#: Шаги сценария (``match_dialogs.script_step``).
AFTER_PIN: str = "after_pin"      # анкета закреплена, ждём её
WAIT_MORE: str = "wait_more"      # ответили «Пусть лежит здесь» / «удобно»
TELL_ME: str = "tell_me"          # написали «Рассказывайте, дамочка»
#: Конечные шаги.
HANDOFF: str = "handoff"          # дальше пишет владелец
OWNER: str = "owner"              # владелец сам вмешался
STOPPED: str = "stopped"          # молчит — бот больше не пишет
DELETED: str = "deleted"          # грубость / «не пиши» — чат удалён
ERROR: str = "error"

ACTIVE_STEPS: tuple[str, ...] = (AFTER_PIN, WAIT_MORE, TELL_ME)
#: Виды, после которых чат удаляется.
STOP_KINDS: frozenset[str] = frozenset({GO_AWAY, RUDE})

#: Важность вида: при нескольких её сообщениях подряд побеждает важнейший.
_PRIORITY: dict[str, int] = {
    GO_AWAY: 4, RUDE: 4, ASKS_BOT: 3,
    NOT_DAMOCHKA: 2, ASKS_TOO_MANY: 2, ASKS_WHAT: 2, ABOUT_WHAT: 2,
    OTHER: 1,
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
    (NOT_DAMOCHKA, [
        r"не\s+дамочк", r"какая\s+(я\s+)?(тебе\s+)?дамочк", r"\bя\s+не\s+дама\b",
        r"дамочк\w*\s*\?", r"сам\s+ты\s+дам",
    ]),
    (ASKS_TOO_MANY, [
        r"много\s+(с\s+кем|кем|девуш|девоч|анкет|общ|переписк|таких)",
        r"не\s+перепутать", r"не\s+путать", r"чтоб(ы)?\s+не\s+(за|пере)?пута",
        r"столько\s+(девуш|девоч|анкет)", r"конвейер", r"коллекци",
    ]),
    (ASKS_WHAT, [
        r"что\s+это", r"это\s+что", r"зачем", r"нафига", r"нахрена", r"нафиг",
        r"к\s+ч[её]му", r"для\s+чего", r"почему\s+(ты\s+)?(мою|моя|это)",
        r"\bмо(я|ю)\s+анкет", r"что\s+за\b", r"\bэто\s+я\b",
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

#: Короткое «что?» / «?» без слов — переспрос.
_BARE_QUESTION = re.compile(r"^(а\s+)?(что|чего|че|чё|шо|эм+|э+|\?)?\s*\?*[)!.]*$")


def normalize(text: str) -> str:
    """Нижний регистр и схлопнутые пробелы (правила знают и «е», и «ё»)."""
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def classify(text: str) -> str:
    """Вид её ответа. Пустой текст (стикер, фото) — ``OTHER``."""
    norm = normalize(text)
    if not norm:
        return OTHER
    for kind, patterns in _COMPILED:
        if any(p.search(norm) for p in patterns):
            return kind
    if _BARE_QUESTION.match(norm):
        return ABOUT_WHAT
    return OTHER


def priority(kind: str) -> int:
    return _PRIORITY.get(kind, 0)


def strongest(kinds: list[str]) -> str:
    """Важнейший из видов (при равенстве — последний)."""
    best = OTHER
    for kind in kinds:
        if priority(kind) >= priority(best):
            best = kind
    return best


def decide(step: str, kind: str) -> tuple[str | None, str]:
    """(ключ фразы из ``MatchScriptTexts`` или None, следующий шаг).

    Схема владельца (2026-10-29):
    анкета → «что это/зачем» → «Пусть лежит здесь»; «много с кем общаешься,
    чтобы не перепутать?» → «Это так-то удобно»; молчит или не спрашивает →
    «Рассказывайте, дамочка» → «я не дамочка» → «А кто? Парнишка?»;
    «о чём?/что?» → «О себе конечно» / «А о чём обычно рассказывают…».
    Концы веток и ответы не по сценарию — владельцу.
    """
    if kind in STOP_KINDS:
        return None, DELETED
    if kind == ASKS_BOT:
        return None, HANDOFF
    if step == AFTER_PIN:
        if kind in (ASKS_WHAT, ABOUT_WHAT):
            return "card_here", WAIT_MORE
        if kind == ASKS_TOO_MANY:
            return "convenient", WAIT_MORE
        return "tell_me", TELL_ME
    if step == WAIT_MORE:
        if kind == ASKS_TOO_MANY:
            return "convenient", WAIT_MORE
        if kind in (ASKS_WHAT, ABOUT_WHAT):
            return None, HANDOFF
        return "tell_me", TELL_ME
    if step == TELL_ME:
        if kind == NOT_DAMOCHKA:
            return "who_then", HANDOFF
        if kind == ABOUT_WHAT:
            return "about_what", HANDOFF
        return None, HANDOFF
    return None, HANDOFF


def decide_silence(step: str) -> tuple[str | None, str]:
    """Она молчит дольше срока: что делать на шаге."""
    if step in (AFTER_PIN, WAIT_MORE):
        return "tell_me", TELL_ME
    return None, STOPPED

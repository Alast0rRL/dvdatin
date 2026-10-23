"""Человеко-читаемые причины решений LIKE/REVIEW/DISLIKE.

Telegram-free: один перевод кодов причин (``ai_decisions.reasons``,
``filter_results.reasons``) для уведомлений владельцу в Telegram
(``AutoActionEngine._format_reason``) и для улучшенной ленты сайта.
"""

from __future__ import annotations

import re

#: Причины фильтра (возраст/город).
FILTER_LABELS: dict[str, str] = {
    "AGE_OUT_OF_RANGE": "Возраст не подходит",
    "CITY_OUT_OF_RANGE": "Город не в списке",
    "AGE_UNKNOWN": "Возраст неизвестен",
    "CITY_UNKNOWN": "Город неизвестен",
    "INSUFFICIENT_DATA": "Недостаточно данных",
}

#: Служебные коды решения — смысла не несут, не показываем.
DECISION_CODES: frozenset[str] = frozenset({
    "LIKE_THRESHOLD", "LOW_CONFIDENCE", "REVIEW_THRESHOLD",
    "BELOW_THRESHOLDS", "FILTER_REJECTED", "FILTER_REVIEW",
    "USER_SKIP", "USER_LIKE", "AI_UNAVAILABLE", "AGE_OK", "CITY_OK",
})

#: Коды скоринга без параметров.
SCORING_LABELS: dict[str, str] = {
    "NO_FEATURES_FOUND": "Мало информации в анкете",
    "INSUFFICIENT_DATA": "Недостаточно данных",
    "INFORMATIVE_CLEAN": "Анкета содержательная, стоп-признаков нет",
}

NEGATIVE_LABELS: dict[str, str] = {
    "not_relationships": "Не ищет отношения",
    "has_boyfriend": "Есть парень",
    "smoking": "Курит",
    "alcohol": "Пьёт",
    "bad_habits": "Вредные привычки",
    "pokatayte": "Покатайте/прокат",
    "short_hair": "Волосы короче каре",
    "instagram": "Instagram",
    "plus_size": "+size",
    "age_mismatch": "Возраст в анкете не совпадает",
}

POSITIVE_LABELS: dict[str, str] = {
    "spbpu": "СПбПУ",
    "anime": "Аниме",
    "games": "Игры",
    "relocated_to_spb": "Переехала в СПб",
}


def evidence_in_card(evidence: str, card_text: str | None) -> bool:
    """Есть ли текст-свидетель в показанной карточке.

    ``card_text is None`` — цитировать можно (обратная совместимость).
    Иначе сравниваем с описанием карточки (всё после «Имя, возраст, город –»):
    повторный показ усечённой карточки не должен цитировать старое описание.
    """
    if card_text is None:
        return True
    if not evidence:
        return False
    header = re.split(r"\s*[–—]\s*", card_text, maxsplit=1)
    card_desc = header[1] if len(header) > 1 else ""
    if not card_desc:
        return False
    norm_ev = "".join(evidence.lower().split())
    norm_card = "".join(card_desc.lower().split())
    return norm_ev in norm_card or (
        len(norm_ev) >= 6 and
        any(
            norm_ev[i:i + 6] in norm_card
            for i in range(0, max(len(norm_ev) - 5, 1), 3)
        )
    )


def _feature(reason: str, labels: dict[str, str], card_text: str | None) -> str:
    """``HARD_NEGATIVE:name:evidence`` / ``POSITIVE:…`` → «Ярлык: цитата»."""
    parts = reason.split(":", 2)
    name = parts[1] if len(parts) > 1 else ""
    evidence = parts[2].strip("«»") if len(parts) > 2 else ""
    label = labels.get(name, name)
    if evidence and evidence_in_card(evidence, card_text):
        return f"{label}: {evidence}"
    return label


def describe_reason(reason: str, card_text: str | None = None) -> str | None:
    """Одна причина → строка для человека (None — служебный код, скрыть)."""
    if reason in DECISION_CODES:
        return None
    if reason.startswith("USER_SKIP:"):
        return f"Ваша стоп-метка: {reason.split(':', 1)[1]}"
    if reason.startswith("USER_LIKE_HARD:"):
        return f"Ваша сильная метка интереса: {reason.split(':', 1)[1]}"
    if reason.startswith("USER_LIKE:"):
        return f"Ваша метка интереса: {reason.split(':', 1)[1]}"
    if reason.startswith("HARD_NEGATIVE:"):
        return _feature(reason, NEGATIVE_LABELS, card_text)
    if reason.startswith("POSITIVE:"):
        return _feature(reason, POSITIVE_LABELS, card_text)
    if reason.startswith("PHOTO_TASTE:"):
        # PHOTO_TASTE:<like|dislike>:<score> — services/photo_taste.py (decide)
        _, tag, score = (reason.split(":") + ["", ""])[:3]
        try:
            pct = f" ({float(score) * 100:.0f}%)"
        except ValueError:
            pct = ""
        return f"Фото во вкусе{pct}" if tag == "like" else f"Фото не во вкусе{pct}"
    if reason.startswith("PHOTO_VICE:"):
        # PHOTO_VICE:<smoke|alcohol>:<вероятность> — services/photo_vice.py
        _, kind, prob = (reason.split(":") + ["", ""])[:3]
        what = {"smoke": "сигарета/вейп", "alcohol": "алкоголь"}.get(kind, kind)
        try:
            pct = f" ({float(prob) * 100:.0f}%)"
        except ValueError:
            pct = ""
        return f"На фото {what}{pct}"
    if reason.startswith("LOW_INFO_SKIP"):
        words = reason.partition("words=")[2]
        return (
            f"Мало текста в анкете: {words} значимых слов"
            if words else "Мало текста в анкете"
        )
    if reason in SCORING_LABELS:
        return SCORING_LABELS[reason]
    if reason in FILTER_LABELS:
        return FILTER_LABELS[reason]
    return reason


def describe_reasons(
    reasons: list[str] | None, card_text: str | None = None,
) -> list[str]:
    """Список причин → строки для человека (служебные коды отброшены)."""
    out: list[str] = []
    for reason in reasons or []:
        line = describe_reason(str(reason), card_text)
        if line and line not in out:
            out.append(line)
    return out

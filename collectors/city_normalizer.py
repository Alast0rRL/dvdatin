# Нормализация городов: приведение вариантов написания к единому формату.

from __future__ import annotations

import re

_CITY_MAP: dict[str, str] = {
    "спб": "Санкт-Петербург",
    "спб.": "Санкт-Петербург",
    "питер": "Санкт-Петербург",
    "питер(": "Санкт-Петербург",
    "санкт петербург": "Санкт-Петербург",
    "санкт-петербург": "Санкт-Петербург",
    "saint petersburg": "Санкт-Петербург",
    "st. petersburg": "Санкт-Петербург",
    "st petersburg": "Санкт-Петербург",
}

# Unicode-варианты дефиса/тире -> ASCII "-"
_DASH_TRANS = str.maketrans(
    {
        "\u2010": "-",  # hyphen
        "\u2011": "-",  # non-breaking hyphen
        "\u2012": "-",  # figure dash
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
        "\u2015": "-",  # horizontal bar
        "\u2212": "-",  # minus sign
    }
)

# Пробелы вокруг дефиса убираем: "санкт - петербург" -> "санкт-петербург"
_SPACE_AROUND_DASH = re.compile(r"\s*-\s*")


def normalize_city(raw: str) -> str:
    """Нормализует название города.

    Устойчив к регистру, пробелам, Unicode-дефисам/тире и вариантам
    написания "Санкт-Петербург". Неизвестные названия (в т.ч. районы/улицы)
    возвращаются без изменений — они не приравниваются к городу.

    Args:
        raw: Исходное название из анкеты.

    Returns:
        Нормализованное название или исходное, если не распознано.
    """
    normalized = raw.strip().lower()
    normalized = normalized.translate(_DASH_TRANS)
    normalized = _SPACE_AROUND_DASH.sub("-", normalized)
    normalized = normalized.rstrip(".")
    normalized = " ".join(normalized.split())

    if normalized in _CITY_MAP:
        return _CITY_MAP[normalized]

    return raw.strip()

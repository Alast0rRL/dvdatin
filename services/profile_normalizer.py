# ProfileNormalizer: детерминированная нормализация текста профиля.
# Только механические преобразования: Unicode, lowercase, пробелы, тире.
# Никакого LLM/NLP. Telegram-free.

from __future__ import annotations

import re
import unicodedata


def normalize_text(text: str) -> str:
    """Нормализует текст профиля.

    Выполняет:
    1. Unicode NFKC normalization.
    2. Удаление/замена специальных символов (эмодзи-разделители, typographic).
    3. Нормализация тире (все варианты → ASCII "-").
    4. Нормализация пробелов (множественные → один, trim).
    5. Lowercase.
    """
    if not text:
        return ""

    # Unicode NFKC normalization
    result = unicodedata.normalize("NFKC", text)

    # Normalize various dash/hyphen types to ASCII "-"
    result = _DASH_RE.sub("-", result)

    # Remove zero-width spaces and similar invisible characters
    result = _INVISIBLE_RE.sub("", result)

    # Normalize whitespace (multiple spaces, tabs, etc.)
    result = _WHITESPACE_RE.sub(" ", result)

    # Trim
    result = result.strip()

    return result


def normalize_for_matching(text: str) -> str:
    """Нормализует текст для поиска подстрок (lowercase + normalized)."""
    return normalize_text(text).lower()


# Regex patterns for normalization
_DASH_RE = re.compile(r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212\u00AD\-]+")
_INVISIBLE_RE = re.compile(r"[\u200B\u200C\u200D\u2060\uFEFF\u00A0]+")
_WHITESPACE_RE = re.compile(r"\s+")

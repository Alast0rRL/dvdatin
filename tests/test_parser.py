# Unit-тесты DvinchikParser + CityNormalizer.
# Синтетические данные на основе реальных форматов Дайвинчика.

from __future__ import annotations

import pytest

from collectors.city_normalizer import normalize_city
from collectors.dvinchik_parser import DvinchikParser
from app.config import FiltersConfig
from models.raw import FilterResult, MessageType


@pytest.fixture
def parser() -> DvinchikParser:
    filters = FiltersConfig(
        age={"min": 18, "max": 19},
        city={"allowed": ["Санкт-Петербург"]},
    )
    return DvinchikParser(filters)


# ==================== CLASSIFY ====================

class TestClassify:
    """Тесты классификации сообщений."""

    @pytest.mark.parametrize("text", [
        "сашк, 19, спб – знаю любви не бывает",
        "Майя, 18, санкт петербург",
        "*)))^, 18, Санкт Петербург – В планах",
        "wimx, 18, Санкт-Петербург",
        "Анна, 19, Москва – Привет всем",
    ])
    def test_profiles(self, parser: DvinchikParser, text: str) -> None:
        assert parser.classify(text) == MessageType.PROFILE

    def test_match(self, parser: DvinchikParser) -> None:
        text = "Начинай общаться 👉 [Anna](https://t.me/anna123?ref=abc)"
        assert parser.classify(text) == MessageType.MATCH

    def test_service(self, parser: DvinchikParser) -> None:
        assert parser.classify("Вам поставили лайк!") == MessageType.SERVICE

    def test_empty_is_service(self, parser: DvinchikParser) -> None:
        assert parser.classify("") == MessageType.SERVICE

    def test_media_only(self, parser: DvinchikParser) -> None:
        assert parser.classify("", has_media=True) == MessageType.MEDIA_ONLY

    def test_unknown(self, parser: DvinchikParser) -> None:
        assert parser.classify("Просто текст без формата") == MessageType.UNKNOWN

    def test_profile_without_description(self, parser: DvinchikParser) -> None:
        assert parser.classify("Майя, 18, санкт петербург") == MessageType.PROFILE


# ==================== PARSE PROFILE ====================

class TestParseProfile:
    """Тесты парсинга анкет на реальных данных."""

    def test_real_spb_short(self, parser: DvinchikParser) -> None:
        text = "сашк, 19, спб – знаю любви не бывает ты может заметил уже"
        p = parser.parse_profile(text)
        assert p.name == "сашк"
        assert p.age == 19
        assert p.raw_city == "спб"
        assert p.normalized_city == "Санкт-Петербург"
        assert "знаю любви" in p.description
        assert p.filter_result == FilterResult.FILTER_MATCH

    def test_real_no_description(self, parser: DvinchikParser) -> None:
        text = "Майя, 18, санкт петербург"
        p = parser.parse_profile(text)
        assert p.name == "Майя"
        assert p.age == 18
        assert p.raw_city == "санкт петербург"
        assert p.normalized_city == "Санкт-Петербург"
        assert p.description == ""
        assert p.filter_result == FilterResult.FILTER_MATCH

    def test_real_special_chars_name(self, parser: DvinchikParser) -> None:
        text = "*)))^, 18, Санкт Петербург – В планах через год ебнуть в спб"
        p = parser.parse_profile(text)
        assert p.name == "*)))^"
        assert p.age == 18
        assert p.raw_city == "Санкт Петербург"
        assert p.normalized_city == "Санкт-Петербург"

    def test_real_simple_name(self, parser: DvinchikParser) -> None:
        text = "wimx, 18, Санкт-Петербург"
        p = parser.parse_profile(text)
        assert p.name == "wimx"
        assert p.age == 18
        assert p.normalized_city == "Санкт-Петербург"
        assert p.description == ""

    def test_cyrillic_name(self, parser: DvinchikParser) -> None:
        text = "Анна, 19, Москва – Привет всем"
        p = parser.parse_profile(text)
        assert p.name == "Анна"
        assert p.normalized_city == "Москва"
        assert p.filter_result == FilterResult.FILTER_NOT_MATCH

    def test_wrong_age(self, parser: DvinchikParser) -> None:
        text = "Anna, 25, Санкт-Петербург – Привет"
        p = parser.parse_profile(text)
        assert p.filter_result == FilterResult.FILTER_NOT_MATCH

    def test_source_ids(self, parser: DvinchikParser) -> None:
        text = "wimx, 18, Санкт-Петербург"
        p = parser.parse_profile(text, source_message_id=123, source_chat_id=456)
        assert p.source_message_id == 123
        assert p.source_chat_id == 456

    def test_no_match_returns_description(self, parser: DvinchikParser) -> None:
        text = "Просто сообщение"
        p = parser.parse_profile(text)
        assert p.name == ""
        assert p.description == text


# ==================== PARSE MATCH ====================

class TestParseMatch:
    """Тесты парсинга матча."""

    def test_basic_match(self, parser: DvinchikParser) -> None:
        text = "Начинай общаться 👉 [Anna](https://t.me/anna123?ref=abc)"
        m = parser.parse_match(text)
        assert m is not None
        assert m.name == "Anna"
        assert m.telegram_username == "anna123"
        assert "t.me/anna123" in m.telegram_url

    def test_match_no_link(self, parser: DvinchikParser) -> None:
        assert parser.parse_match("Начинай общаться") is None

    def test_match_multiline(self, parser: DvinchikParser) -> None:
        text = (
            "Отлично! Надеюсь хорошо проведете время\n"
            "Начинай общаться 👉 [Anna](https://t.me/anna123?ref=x)"
        )
        m = parser.parse_match(text)
        assert m is not None
        assert m.name == "Anna"


# ==================== SERVICE ====================

class TestServiceMessages:
    """Тесты сервисных сообщений."""

    @pytest.mark.parametrize("text", [
        "Вам поставили лайк!",
        "У вас обоюдное совпадение!",
        "Напишите ей первым!",
    ])
    def test_service(self, parser: DvinchikParser, text: str) -> None:
        assert parser.classify(text) == MessageType.SERVICE


# ==================== CITY NORMALIZATION ====================

class TestCityNormalization:
    """Тесты нормализации городов."""

    @pytest.mark.parametrize("raw,expected", [
        ("спб", "Санкт-Петербург"),
        ("СПб", "Санкт-Петербург"),
        ("спб.", "Санкт-Петербург"),
        ("санкт петербург", "Санкт-Петербург"),
        ("Санкт Петербург", "Санкт-Петербург"),
        ("Санкт-Петербург", "Санкт-Петербург"),
        ("Питер", "Санкт-Петербург"),
        ("питер", "Санкт-Петербург"),
        # пробелы вокруг дефиса
        ("санкт - петербург", "Санкт-Петербург"),
        ("Санкт – Петербург", "Санкт-Петербург"),
        # Unicode-дефисы/тире
        ("санкт–петербург", "Санкт-Петербург"),
        ("санкт—петербург", "Санкт-Петербург"),
    ])
    def test_spb_variants(self, raw: str, expected: str) -> None:
        assert normalize_city(raw) == expected

    def test_unknown_city(self) -> None:
        assert normalize_city("Москва") == "Москва"

    def test_unknown_district_not_mapped_to_city(self) -> None:
        # Район/улица не должен превращаться в город без явного правила.
        assert normalize_city("Проспект просвещения") == "Проспект просвещения"

    def test_whitespace(self) -> None:
        assert normalize_city("  спб  ") == "Санкт-Петербург"


# ==================== MEDIA-ONLY ====================

class TestMediaOnly:
    """Тесты media-only сообщений."""

    def test_photo_no_text(self, parser: DvinchikParser) -> None:
        assert parser.classify("", has_media=True) == MessageType.MEDIA_ONLY

    def test_text_with_media(self, parser: DvinchikParser) -> None:
        assert parser.classify("Привет", has_media=True) == MessageType.PROFILE or \
               parser.classify("Привет", has_media=True) == MessageType.UNKNOWN

    def test_photo_with_text_profile(self, parser: DvinchikParser) -> None:
        text = "wimx, 18, Санкт-Петербург"
        assert parser.classify(text, has_media=True) == MessageType.PROFILE


# ==================== REGRESSION: msg.animation ====================

class TestMediaDetection:
    """Regression: _detect_media_type не должен падать."""

    def test_detect_media_type_photo(self) -> None:
        from collectors.dvinchik_collector import _detect_media_type
        from unittest.mock import MagicMock

        msg = MagicMock()
        msg.media = MagicMock(spec=type("MediaPhoto", (), {}))
        msg.media.__class__ = type("MediaPhoto", (), {})
        # Не должен упасть
        result = _detect_media_type(msg)
        assert isinstance(result, str)

    def test_detect_media_type_none(self) -> None:
        from collectors.dvinchik_collector import _detect_media_type
        from unittest.mock import MagicMock

        msg = MagicMock()
        msg.media = None
        assert _detect_media_type(msg) == ""

    def test_detect_media_type_no_media_attr(self) -> None:
        from collectors.dvinchik_collector import _detect_media_type

        class NoMedia:
            pass
        assert _detect_media_type(NoMedia()) == ""

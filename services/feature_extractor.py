# FeatureExtractor: детерминированное извлечение признаков из текста анкеты.
# Никакого LLM/NLP. Только regex, tokenization, словари, контекстные окна.
# Telegram-free. Полностью воспроизводим.
#
# КРИТИЧЕСКИЙ ИНВАРИАНТ: отсутствие информации НИКОГДА не является негативным
# признаком. UNKNOWN → REVIEW, а не DISLIKE.

from __future__ import annotations

import re
from dataclasses import dataclass, field

from models.features import Feature, FeatureType
from services.profile_normalizer import normalize_for_matching


@dataclass
class ExtractionResult:
    """Результат извлечения признаков."""

    hard_negatives: list[Feature] = field(default_factory=list)
    positive_factors: list[Feature] = field(default_factory=list)
    neutral_features: list[Feature] = field(default_factory=list)


class FeatureExtractor:
    """Детерминированный экстрактор признаков из текста анкеты.

    Использует whitelist-подход: программа заранее знает все допустимые
    признаки и ищет их по конкретным паттернам. Ничего «не додумывает».

    Принимает:
        name: имя анкеты
        age: возраст
        city: нормализованный город
        description: текст описания
    """

    def extract(
        self,
        name: str = "",
        age: int | None = None,
        city: str = "",
        description: str = "",
    ) -> ExtractionResult:
        """Извлекает все признаки из данных анкеты."""
        result = ExtractionResult()

        # Объединяем всё текстовое содержимое для поиска
        full_text = " ".join(filter(None, [name, city, description])).lower()
        full_text_norm = normalize_for_matching(full_text)

        # Извлекаем жёсткие негативы
        self._extract_hard_negatives(full_text_norm, full_text, result)

        # Извлекаем подмену/противоречие возраста (H10)
        self._extract_age_mismatch(full_text_norm, full_text, age, result)

        # Извлекаем положительные факторы
        self._extract_positive_factors(full_text_norm, full_text, result)

        return result

    # ── Hard Negatives ────────────────────────────────────────────────

    def _extract_hard_negatives(
        self, text_norm: str, raw_text: str, result: ExtractionResult
    ) -> None:
        """Извлекает жёсткие негативные признаки."""
        for rule in HARD_NEGATIVE_RULES:
            feature = self._apply_negative_rule(rule, text_norm, raw_text)
            if feature is not None:
                result.hard_negatives.append(feature)

    def _apply_negative_rule(
        self, rule: NegativeRule, text_norm: str, raw_text: str
    ) -> Feature | None:
        """Применяет одно правило негатива.

        Учитывает отрицания: «не курю», «парень курит», «бросила курить».
        """
        for pattern in rule.patterns:
            match = _find_pattern_with_context(text_norm, pattern)
            if match is not None:
                # Проверяем контекст отрицания
                if _is_negated(text_norm, match.start(), rule.allow_negation_check):
                    continue
                # Проверяем контекст归属 (parьer курит ≠ я курю)
                if _is_third_person(text_norm, match.start(), rule.check_third_person):
                    continue
                evidence = _extract_evidence(raw_text, match, text_norm)
                return Feature(
                    code=rule.code,
                    type=FeatureType.HARD_NEGATIVE,
                    name=rule.name,
                    value=True,
                    evidence=evidence,
                    source="description",
                )
        return None

    # ── H10: Подмена / противоречие возраста ─────────────────────────

    def _extract_age_mismatch(
        self,
        text_norm: str,
        raw_text: str,
        age: int | None,
        result: ExtractionResult,
    ) -> None:
        """Ловит «подмену» возраста: реальный возраст в тексте противоречит
        заявленному в карточке, либо текст явно говорит о фейковом/заблокированном
        возрасте («не даёт поставить возраст»).

        Пример: карточка «алиночка, 18, СПб – мне 16,дв не даёт поставить этот
        возраст» → заявленный 18, но в описании «мне 16» → H10 (hard negative).

        Это ЯВНЫЙ сигнал (противоречие/самопризнание), а не отсутствие данных —
        поэтому не нарушает инвариант NO_HARD_NEGATIVE_MUST_NOT_BECOME_DISLIKE.
        """
        # 1) Фейковый/заблокированный возраст (без числа требуется только фраза).
        for pattern in _FAKE_AGE_PATTERNS:
            match = re.search(pattern, text_norm)
            if match is not None:
                evidence = _extract_evidence(raw_text, match, text_norm)
                result.hard_negatives.append(
                    Feature(
                        code="H10",
                        type=FeatureType.HARD_NEGATIVE,
                        name="age_mismatch",
                        value=True,
                        evidence=evidence,
                        source="description",
                    )
                )
                return

        # 2) Самообъявленный возраст в тексте, который НЕ совпадает с заявленным.
        if age is None:
            return
        for pattern in _AGE_CLAIM_PATTERNS:
            match = re.search(pattern, text_norm)
            if match is None:
                continue
            claimed = match.group(1)
            try:
                claimed_age = int(claimed)
            except ValueError:
                continue
            if claimed_age not in _AGE_CLAIM_RANGE:
                continue
            if claimed_age != age:
                evidence = _extract_evidence(raw_text, match, text_norm)
                result.hard_negatives.append(
                    Feature(
                        code="H10",
                        type=FeatureType.HARD_NEGATIVE,
                        name="age_mismatch",
                        value=True,
                        evidence=evidence,
                        source="description",
                    )
                )
                return

    # ── Positive Factors ──────────────────────────────────────────────

    def _extract_positive_factors(
        self, text_norm: str, raw_text: str, result: ExtractionResult
    ) -> None:
        """Извлекает положительные факторы."""
        for rule in POSITIVE_RULES:
            feature = self._apply_positive_rule(rule, text_norm, raw_text)
            if feature is not None:
                result.positive_factors.append(feature)

    def _apply_positive_rule(
        self, rule: PositiveRule, text_norm: str, raw_text: str
    ) -> Feature | None:
        """Применяет одно правило позитива."""
        for pattern in rule.patterns:
            match = _find_pattern_with_context(text_norm, pattern)
            if match is not None:
                evidence = _extract_evidence(raw_text, match, text_norm)
                return Feature(
                    code=rule.code,
                    type=FeatureType.POSITIVE,
                    name=rule.name,
                    value=True,
                    evidence=evidence,
                    source="description",
                )
        return None


# ── Negative Rules ──────────────────────────────────────────────────

@dataclass
class NegativeRule:
    """Правило жёсткого негатива."""

    code: str
    name: str
    patterns: list[str]
    # Разрешить проверку на отрицание ("не курю" → не негатив)
    allow_negation_check: bool = True
    # Проверять归属 на третье лицо ("парень курит" → не негатив)
    check_third_person: bool = True


# Whitelist жёстких негативов (H01-H12).
# Только явные, подтверждённые признаки. Ничего «не додумывается».
HARD_NEGATIVE_RULES: list[NegativeRule] = [
    # H01: Не ищет отношения
    NegativeRule(
        code="H01",
        name="not_relationships",
        patterns=[
            "ищу друга", "ищу подругу", "ищу друзей",
            "без отношений", "не ищу отношения", "не ищу отношений",
            "просто ищу общение", "ищу общение", "только общение",
            "пообщаться", "ищу человечка", "ищу кого-нибудь",
            "ищу когонибудь", "без обязательств", "для общения",
            "хочу общаться", "хочу пообщаться",
        ],
        allow_negation_check=True,
        check_third_person=False,
    ),
    # H02: Есть парень / в отношениях
    NegativeRule(
        code="H02",
        name="has_boyfriend",
        patterns=[
            "есть парень", "у меня парень", "мой парень",
            "с парнем", "в отношениях", "состояю в отношениях",
            "есть паренек", "у меня есть парень",
            "моему парню", "моем парне",
        ],
        allow_negation_check=True,
        check_third_person=True,
    ),
    # H03: Курит
    NegativeRule(
        code="H03",
        name="smoking",
        patterns=["курю", "курит", "курение", "сигарет", "сигареты", "сигарку"],
        allow_negation_check=True,
        check_third_person=True,
    ),
    # H04: Пьёт
    NegativeRule(
        code="H04",
        name="alcohol",
        patterns=[
            "пью", "пьёт", "алкогол", "вино", "пиво", "пива", "пивка",
            "пивко", "пивас", "пивасик", "пивной", "водк", "выпива",
            "бахаю", "выпивка", "алкоголь", "напиться", "нажраться",
        ],
        allow_negation_check=True,
        check_third_person=True,
    ),
    # H05: Вредные привычки (общий)
    NegativeRule(
        code="H05",
        name="bad_habits",
        patterns=["вредные привычки", "вредн"],
        allow_negation_check=True,
        check_third_person=False,
    ),
    # H06: Покатайте / прокат
    NegativeRule(
        code="H06",
        name="pokatayte",
        patterns=["покатайте", "покатать", "покатуш", "прокат", "подвезти"],
        allow_negation_check=True,
        check_third_person=False,
    ),
    # H07: Волосы короче каре
    NegativeRule(
        code="H07",
        name="short_hair",
        patterns=["под каре", "короткая стрижка", "короче каре"],
        allow_negation_check=True,
        check_third_person=False,
    ),
    # H08: Instagram в анкете
    NegativeRule(
        code="H08",
        name="instagram",
        patterns=["instagram", "инстаграм"],
        allow_negation_check=False,
        check_third_person=False,
    ),
    # H09: Девушка +size (если это нежелательный критерий)
    NegativeRule(
        code="H09",
        name="plus_size",
        patterns=["+size", "плюс сайз", "полненькая", "пышная фигура"],
        allow_negation_check=True,
        check_third_person=False,
    ),
]


# ── Age mismatch (H10) ──────────────────────────────────────────────

# Диапазон «реального» возраста, который имеет смысл сравнивать с карточным.
_AGE_CLAIM_RANGE = range(13, 31)

# Паттерны самообъявления возраста в тексте (для сравнения с заявленным).
_AGE_CLAIM_PATTERNS: list[str] = [
    r"мне\s+(?:уже\s+|всего\s+|сейчас\s+)?(\d{1,2})",
    r"мне\s+было\s+(\d{1,2})",
    r"возраст\s+(?:у\s+меня\s+)?(\d{1,2})",
    r"исполнилось\s+(\d{1,2})",
    r"стукнуло\s+(\d{1,2})",
]

# Паттерны «фейкового»/заблокированного возраста (срабатывает без числа).
_FAKE_AGE_PATTERNS: list[str] = [
    r"не\s+(?:дает|даёт|могу)\s+поставить\s+(?:этот\s+)?(?:настоящий\s+)?возраст",
    r"не\s+(?:дает|даёт)\s+поставить\s+(?:этот\s+)?возраст",
    r"не\s+позволяет\s+поставить\s+(?:этот\s+)?возраст",
    r"нельзя\s+поставить\s+возраст",
    r"(?:поставила|написала|указала)\s+другой\s+возраст",
    r"возраст\s+не\s+настоящий",
    r"возраст\s+не\s+тот",
]


# ── Positive Rules ──────────────────────────────────────────────────

@dataclass
class PositiveRule:
    """Правило положительного фактора."""

    code: str
    name: str
    patterns: list[str]


# Whitelist положительных факторов (P01-P04).
POSITIVE_RULES: list[PositiveRule] = [
    # P01: СПбПУ / Политех
    PositiveRule(
        code="P01",
        name="spbpu",
        patterns=["спбпу", "политех", "спбау", "лэти", "итимо", "питерский политех"],
    ),
    # P02: Аниме / манга
    PositiveRule(
        code="P02",
        name="anime",
        patterns=["аниме", "анимеш", "манга"],
    ),
    # P03: Игры
    PositiveRule(
        code="P03",
        name="games",
        patterns=[
            "играю в", "играть в", "игры", "гейм",
            "дота", "майнкрафт", "пабг", "стрим",
            "компьютерные игры", "кооп", "gaming",
        ],
    ),
    # P04: Переехала / живёт в СПб (именно переехала, а не просто «в СПб»)
    PositiveRule(
        code="P04",
        name="relocated_to_spb",
        patterns=["переехала", "перееха", "недавно в питер", "недавно переехала"],
    ),
]


# ── Pattern Matching Helpers ────────────────────────────────────────

def _find_pattern_with_context(
    text: str, pattern: str
) -> re.Match[str] | None:
    """Ищет паттерн в тексте с учётом границ слов."""
    # Используем word boundary для корректного поиска
    regex = re.compile(r"(?<!\w)" + re.escape(pattern) + r"(?!\w)", re.IGNORECASE)
    return regex.search(text)


# Паттерны, указывающие на отрицание
_NEGATION_PREFIXES = [
    r"(?:не|ни|без|никогда\s+не|вообще\s+не|вовсе\s+не|отнюдь\s+не|совсем\s+не|yet)\s*",
    r"(?:не\s+хочу|не\s+люблю|не\s+ඞаю|не\s+буду|не\s+стала|не\s+старал|не\s+пытал)",
]

# Паттерны归属 на третье лицо.
# ВАЖНО: требуем границу слова (\b) перед маркером, иначе короткие обороты
# вроде «он/она/оно» матчатся как ПОДСТРОКА внутри чужих слов («галон»,
# «телефон», «закон») + пробел → ложное «третье лицо», из-за которого
# негатив (например «пива» в «галон пива») ошибочно сбрасывается.
_THIRD_PERSON_PREFIXES = [
    r"(?<!\w)(?:парень|муж|бойфренд|boyfriend)\s+(?:мой|мой|твой|её|их)",
    r"(?<!\w)(?:парень|муж)\s+",
    r"(?<!\w)(?:он|она|оно)\s+",
    r"(?:все\s+мои|все\s+твои|все\s+её)\s+",
    r"(?:мои\s+друзья|твои\s+друзья|её\s+друзья)\s+",
]


def _is_negated(text: str, position: int, allow: bool) -> bool:
    """Проверяет, стоит ли перед паттерном отрицание.

    Args:
        text: нормализованный текст (lowercase).
        position: позиция начала совпадения паттерна.
        allow: разрешена ли проверка на отрицание для данного правила.

    Returns:
        True, если паттерн в контексте отрицания.
    """
    if not allow:
        return False

    # Берём контекст перед совпадением (до 40 символов).
    prefix = text[max(0, position - 40) : position].strip()
    if not prefix:
        return False

    # Отрицание — слово/оборот, стоящий НЕПОСРЕДСТВЕННО перед паттерном
    # («не курю», «не пью»). Ищем его в конце префикса (прилегает к позиции),
    # а не привязываясь к началу строки: в анкетах перед «не курю» почти всегда
    # стоят имя/город («Полина, Санкт-Петербург, не курю, не пью»).
    for neg_pattern in _NEGATION_PREFIXES:
        regex = re.compile(
            r"(?<!\w)" + neg_pattern + r"(?=[\s,.;:!?…\-–—]*$)",
            re.IGNORECASE,
        )
        m = regex.search(prefix)
        if m:
            # «парень не курит» — «курит» относится к парню, а не к анкете,
            # и не является отрицанием в нашем смысле.
            remaining = text[max(0, position - 40) : m.start()]
            if not _has_third_person_before_negative(remaining):
                return True
    return False


def _has_third_person_before_negative(text: str) -> bool:
    """Проверяет, есть ли перед negate-словом归属 на третье лицо.

    «парень не курит» → True (не считаем отрицанием для девушки)
    «не курю» → False (считаем отрицанием)
    """
    third_person_markers = [
        "парень", "муж", "бойфренд", "boyfriend",
        "он ", "она ", "они ", "все ", "все мои",
        "друг ", "подруга ", "друзья ",
    ]
    low = text.lower()
    for marker in third_person_markers:
        if marker in low:
            return True
    return False


def _is_third_person(text: str, position: int, check: bool) -> bool:
    """Проверяет, относится ли совпадение к третьему лицу.

    «парень курит» → True (не негатив для анкеты девушки)
    «курю» → False
    """
    if not check:
        return False

    # Берём контекст перед совпадением (до 40 символов) — НЕ strip(),
    # т.к. regex-паттерны ожидают пробел после маркера归属.
    prefix = text[max(0, position - 40) : position]
    if not prefix.strip():
        return False

    for marker in _THIRD_PERSON_PREFIXES:
        if re.search(marker + r"$", prefix, re.IGNORECASE):
            return True
    return False


def _extract_evidence(raw_text: str, match: re.Match[str], norm_text: str) -> str:
    """Извлекает evidence (точную цитату) из исходного текста.

    Находит соответствующий фрагмент в raw_text по нормализованному совпадению.
    """
    if not raw_text:
        return match.group()

    # Ищем совпадение в raw_text (case-insensitive)
    pattern = re.compile(re.escape(match.group()), re.IGNORECASE)
    raw_match = pattern.search(raw_text)
    if raw_match:
        # Возвращаем цитату с контекстом (±30 символов)
        start = max(0, raw_match.start() - 30)
        end = min(len(raw_text), raw_match.end() + 30)
        snippet = raw_text[start:end].strip()
        # Добавляем многоточие если обрезали
        if start > 0:
            snippet = "..." + snippet
        if end < len(raw_text):
            snippet = snippet + "..."
        return snippet

    return match.group()

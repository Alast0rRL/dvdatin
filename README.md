# DvAI — Система автоматизации знакомств в Telegram

Проект для автоматизации работы с сервисом знакомств "Дайвинчик" в Telegram.
На текущем этапе (v0.6 Stage 6) реализован OBSERVE-режим с фильтрацией, AI-скорингом,
AI Decision Engine и Human Review & Analytics:
перехват сообщений, сохранение RAW-данных, классификация, парсинг,
upsert профилей с fingerprint-дедупликацией, конфигурируемая фильтрация,
CLIP + LLM анализ анкет, AI-решение LIKE/REVIEW/DISLIKE, ручная рецензия (APPROVE/REJECT/SKIP)
и аналитика согласия AI/человека (OBSERVE/REVIEW-only, без Telegram-действий).
Windows — Remote AI Client, Ubuntu — AI Inference Server.

## Структура проекта

```
dvdatin/
├── main.py                          # Точка входа
├── run.bat                          # Запуск (Windows)
├── requirements.txt                 # Зависимости
├── config/
│   ├── config.example.yaml          # Пример конфигурации
│   └── config.yaml                  # Ваш конфиг (в .gitignore)
├── app/
│   ├── config.py                    # Pydantic-загрузчик YAML
│   ├── logging.py                   # Loguru + Rich
│   └── banner.py                    # ASCII-баннер
├── core/
│   └── types.py                     # Enum-ы
├── database/
│   └── database.py                  # SQLite (raw_messages, profiles, profile_messages, filter_results, ai_scores, ai_decisions, human_decisions)
├── telegram/
│   ├── client.py                    # Telethon клиент
│   └── review_bot.py                # Telegram UI для Human Review (клавиатуры, callback)
├── models/
│   ├── raw.py                       # RawMessage, ParsedProfile, ParsedMatch, MessageType
│   ├── profile.py                   # Profile, ProfileStatus, compute_fingerprint
│   ├── filter.py                    # FilterDecision, FilterReason, FilterResult
│   ├── ai.py                        # AIScore, CLIPScore, LLMScore, AIRecommendation
│   ├── decision.py                  # AIDecision, AIDecisionResult
│   └── human_decision.py            # HumanDecision, AgreementStatus, HumanDecisionResult, HumanReview
├── services/
│   ├── profile_service.py           # CRUD + upsert + fingerprint
│   ├── filter_engine.py             # Движок фильтрации (AgeRule, CityRule, DataCompletenessRule)
│   ├── filter_service.py            # Оценка + история в БД
│   ├── clip_service.py              # CLIP анализ фото (ABC + заглушка)
│   ├── llm_service.py               # LLM оценка анкет (ABC + заглушка)
│   ├── ai_scoring_service.py        # Объединённый CLIP + LLM скоринг
│   ├── decision_service.py          # AI Decision Engine (LIKE/REVIEW/DISLIKE)
│   ├── remote_llm_client.py         # Remote LLM client (httpx → Ubuntu)
│   ├── remote_clip_client.py        # Remote CLIP client (httpx → Ubuntu)
│   ├── review_service.py            # Human Review (очередь, сохранение решений)
│   ├── analytics_service.py         # Аналитика (согласие AI/человека, breakdowns)
│   └── review_export.py             # CSV-экспорт рецензий
├── collectors/
│   ├── __init__.py                  # Экспорт коллекторов
│   ├── dvinchik_collector.py        # Перехват сообщений Telegram
│   ├── dvinchik_parser.py           # Классификация и парсинг
│   ├── city_normalizer.py           # Нормализация городов
│   ├── dedup.py                     # Дедупликация сообщений
│   ├── anti_block.py                # Rate-limiter
│   ├── raw_queue.py                 # Async буфер
│   ├── stats.py                     # Статистика коллектора
│   └── media_analyzer.py           # Анализ фото
├── tests/
│   ├── test_parser.py               # Unit-тесты парсера
│   ├── test_collector.py            # Integration-тесты коллектора
│   ├── test_profile.py              # Unit-тесты профилей
│   ├── test_audit.py                # Database audit tests
│   ├── test_filter.py               # Filter engine tests (18 cases)
│   ├── test_ai.py                   # AI scoring tests (54 cases)
│   ├── test_ai_scoring.py           # Stage 5 scoring tests (offline)
│   ├── test_decision.py             # Stage 5 decision tests (20 cases)
│   ├── test_human_review.py         # Stage 6 Human Review tests (29 cases)
│   ├── test_analytics.py            # Stage 6 Analytics + CSV export tests (20 cases)
│   ├── test_review_ui.py            # Stage 6 Telegram review UI tests (5 cases, mocks)
│   └── e2e_ai.py                    # REAL_E2E против живого шлюза (не в pytest)
└── data/
    ├── logs/                        # runtime.log, analytics.log
    ├── exports/                     # CSV-экспорт рецензий (в .gitignore)
    └── sessions/                    # Telegram session
```

## Установка

### 1. Клонируйте репозиторий

```bash
git clone <url>
cd dvdatin
```

### 2. Установите зависимости

```bash
pip install -r requirements.txt
```

### 3. Настройте конфигурацию

```bash
cp config/config.example.yaml config/config.yaml
```

Заполните `config/config.yaml`:
- `telegram.api_id` / `api_hash` — с https://my.telegram.org
- `telegram.phone` — ваш номер телефона
- `telegram.proxy` — настройки прокси (для России)
- `dvinchik.chat_id` — ID чата Дайвинчика (по умолчанию 1234060895)
- `filters.age.min/max` — диапазон возраста для фильтрации
- `filters.city.allowed` — список разрешённых городов

## Режимы работы

### OBSERVE (Наблюдение) — текущий
- Перехватывает все входящие сообщения Telegram
- Сохраняет RAW-сообщения в SQLite (всегда ПЕРВЫМ)
- Классифицирует: PROFILE / MEDIA_ONLY / MATCH / SERVICE / UNKNOWN
- Извлекает name/age/city из анкет
- Создаёт и обновляет Profile с fingerprint-дедупликацией
- Фильтрует профили по возрасту и городу (конфигурируемо)
- Сохраняет результаты фильтрации в историю
- При PASS: AI-анализ фото (CLIP) и анкеты (LLM), объединённый скор
- При PASS: AI Decision Engine формирует решение LIKE / REVIEW / DISLIKE (OBSERVE)
- Ничего не отправляет и не нажимает

### SEMI_AUTO (Полуавтоматический) — будущее
Действия с подтверждением пользователя.

### AUTO (Автоматический) — будущее
Полная автоматизация.

## Схема SQLite

### raw_messages

```sql
CREATE TABLE raw_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_message_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    sender_id INTEGER NOT NULL,
    sender_username TEXT DEFAULT '',
    sender_name TEXT DEFAULT '',
    message_date TEXT NOT NULL,
    text TEXT DEFAULT '',
    raw_entities TEXT DEFAULT '[]',
    media_type TEXT DEFAULT '',
    reply_to_message_id INTEGER,
    received_at TEXT NOT NULL
);
```

### profiles

```sql
CREATE TABLE profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    age INTEGER NOT NULL,
    raw_city TEXT DEFAULT '',
    normalized_city TEXT DEFAULT '',
    description TEXT DEFAULT '',
    fingerprint TEXT DEFAULT '',
    source_chat_id INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'NEW',
    UNIQUE(source_chat_id, source_message_id)
);
```

### profile_messages

```sql
CREATE TABLE profile_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
    UNIQUE(profile_id, telegram_message_id)
);
```

### filter_results

```sql
CREATE TABLE filter_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL,
    decision TEXT NOT NULL,          -- PASS / REJECT / REVIEW
    reasons TEXT NOT NULL DEFAULT '[]',
    rules_checked INTEGER NOT NULL DEFAULT 0,
    evaluated_at TEXT NOT NULL,
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
);
```

### ai_scores

```sql
CREATE TABLE ai_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL,
    clip_score REAL,
    llm_score REAL,
    combined_score REAL NOT NULL DEFAULT 0.0,
    recommendation TEXT NOT NULL DEFAULT 'REVIEW',  -- LIKE / DISLIKE / REVIEW
    confidence TEXT NOT NULL DEFAULT 'LOW',          -- HIGH / MEDIUM / LOW
    confidence_score REAL NOT NULL DEFAULT 0.0,
    reasons TEXT NOT NULL DEFAULT '[]',
    model_version TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
);
```

## Profile Lifecycle

```
Telegram message
    ↓
RAW saved (always first)
    ↓
classify → PROFILE
    ↓
parse → ParsedProfile
    ↓
upsert_profile()
    ↓
evaluate() → FilterResult
    ↓
save to filter_results
    ↓
IF PASS:
    download images (remote_clip_client)
    AI scoring → AIScore
    save to ai_scores
    Decision Engine → AI DECISION
    save to ai_decisions
    ↓
console output
    ↓
[NEW] → [SEEN] → [MATCHED] → [ARCHIVED]
```

## Дедупликация

Профиль ищется по **fingerprint** (SHA-256):
```
normalized_name + age + normalized_city
```

Это НЕ гарантированный идентификатор человека — только механизм
дедупликации анкет. Архитектура позволяет позже заменить стратегию.

## Фильтрация

Движок фильтрации конфигурируется через `config.yaml`:

```yaml
filters:
  age:
    min: 18
    max: 19
  city:
    allowed:
      - "Санкт-Петербург"
```

**Решения:**
- **PASS** — профиль прошёл все правила
- **REJECT** — age/city вне диапазона
- **REVIEW** — возраст или город неизвестны

**Правила (все выполняются, останавливаются на REJECT):**
- AgeRule — проверка возраста
- CityRule — проверка города (нормализованного)
- DataCompletenessRule — проверка полноты данных

## AI Scoring

Конфигурируется через `config.yaml`:

```yaml
ai:
  enabled: true
  backend: "remote"           # "local" (stubs) или "remote" (Ubuntu AI Server через прокладку)
  remote:
    base_url: "http://144.31.139.206:8000"   # внешний порт прокладки → Ubuntu AI Server
    timeout: 90
    max_retries: 1
  images:
    enabled: true             # скачивать изображения при PASS
    max_images: 5
    max_size_mb: 10
  clip:
    enabled: true
    model: "openai/clip-vit-base-patch32"
  llm:
    enabled: true
    provider: "ollama"
    model: "qwen3:8b"
    api_key: ""
  scoring:
    clip_weight: 0.5
    llm_weight: 0.5
    like_threshold: 0.75
    dislike_threshold: 0.35  # validated: must be < like_threshold
```

**Рекомендации:**
- **LIKE** — combined_score >= like_threshold
- **DISLIKE** — combined_score <= dislike_threshold
- **REVIEW** — между порогами

**Компоненты (backend=remote — реальный инференс на Ubuntu):**
- CLIP — анализ фото на GPU через `/v1/clip/analyze` (multipart поле `files`, ответ `clip_score`)
- LLM — оценка анкеты через Ollama `/v1/llm/evaluate` (qwen3:8b), ответ `score/confidence/reasons`
- AIScoringService — объединяет веса CLIP + LLM на **Windows**, определяет рекомендацию по порогам

**Ограничения:**
- AI scoring запускается ТОЛЬКО при FilterDecision == PASS
- Изображения скачиваются ТОЛЬКО при PASS (перед AI scoring)
- Ошибки AI не ломают коллектор (сервер недоступен → безопасный скор, данные не теряются)
- AI сервисы опциональны — отключаются через `ai.enabled: false`
- Режим OBSERVE сохранён — ничего не отправляет и не нажимает
- Финальный LIKE/DISLIKE считается ТОЛЬКО на Windows, не на сервере

## AI Decision Engine

Самостоятельный слой принятия решения (Stage 5). Берёт сигналы фильтра (PASS), скор (AIScore) и конфигурацию, формирует окончательное решение `LIKE / REVIEW / DISLIKE` — **только OBSERVE, никаких Telegram-действий**.

```yaml
ai:
  decision:
    like_threshold: 0.75
    review_threshold: 0.50
    min_confidence: 0.60
    scoring_version: "v1"
    weights:
      llm: 0.7
      clip: 0.3
```

**Решения:**
- **LIKE** — combined >= like_threshold и confidence >= min_confidence
- **REVIEW** — combined >= review_threshold, но ниже LIKE (либо низкая уверенность / нет сигналов)
- **DISLIKE** — ниже порогов, либо FilterDecision == REJECT
- Фильтр **REVIEW** никогда не превращается в LIKE; при отсутствии сигналов (все AI недоступны) → REVIEW `AI_UNAVAILABLE`

**Два слоя (не путать):**
- `AIScoringService` (models/ai.py `AIRecommendation`) — рекомендация скоринга, сохраняется в `ai_scores`
- `DecisionService` (models/decision.py `AIDecision`) — итоговое решение, сохраняется в `ai_decisions`

**Компоненты (`services/decision_service.py`):**
- `DecisionService.evaluate(profile_id, image_data_list)` — полный пайплайн (фильтр + скор + решение), сохраняет в БД
- Веса `ai.decision.weights` независимы от `ai.scoring` (решение = llm*0.7 + clip*0.3)
- Один вызов шлюза: `evaluate` внутри сохраняет и `ai_scores`, и `ai_decisions`
- Ошибки/недоступность шлюза → безопасное REVIEW `AI_UNAVAILABLE`, данные не теряются

## Human Review & Analytics

Слой ручной рецензии (Stage 6) поверх AI Decision Engine. Человек просматривает анкеты с уже вынесенным AI-решением и либо одобряет выбор AI, либо отклоняет его — **REVIEW-only, без Telegram-действий** (`telegram/review_bot.py` — единственный Stage 6 слой с Telethon).

**Решение человека (`models/human_decision.py`):**
- **APPROVE** — одобряет выбор AI → AGREEMENT
- **REJECT** — отвергает выбор AI → DISAGREEMENT
- **SKIP** — без решения → UNRESOLVED (исключается из знаменателя)

**Очередь:** только профили с существующим AI-решением; oldest-first; рассмотренное комбо (profile_id + ai_decision_id) исключено; новое ai_decision_id снова попадает в очередь. Одна рецензия на AI-оценку (`UNIQUE(ai_decision_id)`), история рецензий append-only (некогда UPDATE/DELETE).

**Команды бота (`/review`, `/profile <id>`, `/stats`, `/ai_stats`, `/disagreements [sort]`)** + inline-кнопки `review:approve/reject/skip:<ai_decision_id>`, `review:next`, `disagreement:sort:<newest|score|confidence>` (в callback только id, без PII).

**AnalyticsService** (read-only): `get_overview`, `get_ai_stats`, `get_human_stats`, `get_agreement_stats`,
`get_disagreements`, `get_score_distribution`, `get_ai_breakdown`, `get_filter_breakdown`, `get_scoring_version_breakdown`, `get_prompt_version_breakdown`.

Метрика согласия — **AI/Human Agreement Rate** = AGREEMENT/(AGREEMENT+DISAGREEMENT); SKIP исключён; `null` (не 0%) при знаменателе 0. Никогда не называть "AI accuracy".

**Экспорт CSV:** `python main.py --export-review` → `data/exports/review_YYYYMMDD_HHMMSS.csv` (12 полей, включая human_decision, agreement, prompt_version).

## Связь данных

```
Profile #42
├── profile_messages
│   ├── raw_message #100
│   ├── raw_message #101
│   └── raw_message #102
├── filter_results
│   ├── result #1 (PASS)
│   ├── result #2 (REJECT)
│   └── result #3 (REVIEW)
├── ai_scores
│   ├── score #1 (LIKE, combined=0.85)
│   └── score #2 (REVIEW, combined=0.55)
├── ai_decisions
│   ├── decision #1 (LIKE, combined=0.78, v1)
│   └── decision #2 (REVIEW, AI_UNAVAILABLE)
└── human_decisions
    ├── review #1 (decision #1 → REJECT → DISAGREEMENT)
    └── review #2 (decision #2 → SKIP → UNRESOLVED)
```

RAW-сообщения никогда не удаляются.

## Тесты

```bash
# Все тесты (315)
python -m pytest tests/ -v

# Только AI scoring (54)
python -m pytest tests/test_ai.py -v

# Только Decision Engine (20)
python -m pytest tests/test_decision.py -v

# Только Stage 6: Human Review (29) + Analytics (20) + Review UI (5)
python -m pytest tests/test_human_review.py tests/test_analytics.py tests/test_review_ui.py -v

# REAL_E2E против живого шлюза (не входит в обычный pytest, без test_ префикса)
python tests/e2e_ai.py
```

## Технологии

- **Python 3.12+**
- **Telethon** — Telegram API клиент
- **SQLite + aiosqlite** — асинхронная БД
- **PyYAML** — конфигурация
- **Pydantic** — валидация данных
- **Loguru** — логирование
- **Rich** — красивый вывод в консоль
- **httpx** — async HTTP клиент (Remote AI Client → Ubuntu AI Server)

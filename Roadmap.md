# DvAI — План проекта

## Архитектура

```
┌─────────────────────────────────────────────────┐
│                  Telegram                        │
│              (Telethon client)                   │
└──────────────────────┬──────────────────────────┘
                       │ NewMessage event
                       ▼
┌─────────────────────────────────────────────────┐
│            DvinchikCollector                     │
│  ┌──────────────────────────────────────────┐   │
│  │ 1. save RAW message     (ALWAYS FIRST)   │   │
│  │ 2. classify                              │   │
│  │ 3. parse                                  │   │
│  │ 4. upsert profile (if PROFILE)           │   │
│  │ 5. link message                          │   │
│  │ 6. evaluate profile (FilterEngine)       │   │
│  │ 7. save result (FilterService)           │   │
│  │ 8. IF PASS: download images              │   │
│  │ 9. IF PASS: Remote AI Scoring (httpx)    │   │
│  │10. console output                         │   │
│  └──────────────────────────────────────────┘   │
└───┬──────────┬──────────┬──────────┬────────────┘
    │          │          │          │
    ▼          ▼          ▼          ▼
┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────────┐
│raw_msgs  │ │ profiles │ │filter_res│ │ profile_messages  │
│(SQLite)  │ │(SQLite)  │ │(SQLite)  │ │ (SQLite)          │
└──────────┘ └──────────┘ └──────────┘ └──────────────────┘
         │
         ▼ (httpx, on PASS only)
┌─────────────────────────────────────────────────┐
│         Remote AI Client (Windows)               │
│  remote_llm_client.py / remote_clip_client.py    │
└──────────────────────┬──────────────────────────┘
                       │ HTTP (httpx)
                       ▼
┌─────────────────────────────────────────────────┐
│         Ubuntu AI Inference Server               │
│         (CLIP + LLM backend)                    │
└─────────────────────────────────────────────────┘
```

## Текущий этап: Stage 7 — Semi-Auto (авто-действия)

### Завершено

- [x] Stage 0: Config, logging, DB, Telethon client, banner, main.py
- [x] Stage 1: Parser, classifier, RAW save, 18 unit tests
- [x] Stage 1.5: Fix msg.animation, city normalization, MEDIA_ONLY, dedup, rate limiter, stats, 59 tests
- [x] Stage 2: Profile model, profiles + profile_messages tables, fingerprint, ProfileService, upsert, 78 tests
- [x] Stage 2.5: Database Audit (29 tests), 107/107 passed
- [x] Stage 3: Filter Engine — config-driven rules, history in DB, collector integration, 132 tests
- [x] Stage 4: AI Scoring — CLIP + LLM stubs, combined scoring, DB persistence, collector integration, 186 tests
- [x] Stage 4.1: Remote AI Client — Ubuntu AI Inference Server, httpx clients, image downloads on PASS, 231 tests
- [x] Stage 4.2: Ubuntu AI Inference Server — FastAPI (LLM Ollama qwen3:8b + CLIP) на GPU, 2 integration tests
- [x] Stage 4.3: Windows ↔ Ubuntu AI E2E — реальный проброс через прокладку, фикс CLIP-контракта, live-проверка
- [x] Stage 5: AI Decision Engine — models/decision.py, DecisionService, ai_decisions, пороги/веса, offline-тесты + REAL_E2E, 266 tests
- [x] Stage 6: Human Review & Analytics — HumanDecision, human_decisions, ReviewService, AnalyticsService, Telegram review UI, CSV export, 315 tests

### Stage 4 — Что реализовано

**AI Models (`models/ai.py`):**
- AIRecommendation (LIKE / DISLIKE / REVIEW)
- ConfidenceLevel (HIGH / MEDIUM / LOW)
- CLIPScore, LLMScore, AIScore — Pydantic v2 модели с валидацией

**CLIP Service (`services/clip_service.py`):**
- BaseCLIPService (ABC) — абстрактный интерфейс
- CLIPService — заглушка, анализирует photo_count, возвращает aesthetic_score=0.5
- Включается/выключается через `ai.clip.enabled`
- Ошибки не кидаются — возвращается score=0.0

**LLM Service (`services/llm_service.py`):**
- BaseLLMService (ABC) — абстрактный интерфейс
- LLMService — заглушка; парсит JSON-ответ, извлекает признаки, возвращает детерминированный нейтральный score=0.6 (`NEUTRAL_SCORE`)
- Включается/выключается через `ai.llm.enabled`
- Валидация JSON + Pydantic-модель, ошибки → REVIEW с confidence=0.0
- **Фичевый контракт (llm-v3)**: LLM извлекает только разрешённые признаки (hard_negatives H1–H8, positive_factors P1–P4, unknown), score вычисляется детерминированно (`detect_score_status`): негатив → 0.1, positive → 0.9, иначе нейтральный 0.6. Ответ модели-«мнение» (score/reasoning) игнорируется.

**AI Scoring Service (`services/ai_scoring_service.py`):**
- AIScoringService — объединяет CLIP + LLM в единый AIScore
- Веса: `clip_weight`, `llm_weight` (нормализуются если один компонент отсутствует)
- Порог: `like_threshold`; DISLIKE-рекомендация `_determine_recommendation` возможна ТОЛЬКО при подтверждённом hard negative (инвариант `NO_HARD_NEGATIVE_MUST_NOT_BECOME_DISLIKE`)
- Уверенность: HIGH (оба), MEDIUM (один), LOW (нет данных)
- Сохраняет результат в `ai_scores` таблицу

**Collector Integration:**
```
RAW → classify → parse → upsert → FilterResult == PASS → download images → Remote AI Scoring → console output
```
- AI scoring запускается ТОЛЬКО при PASS
- REJECT/REVIEW → AI не вызывается, изображения не скачиваются
- Изображения скачиваются ТОЛЬКО при PASS (перед AI scoring)
- Ошибки AI не ломают коллектор и не теряют RAW
- Remote clients (httpx) отправляют запросы на Ubuntu AI Inference Server

**Database:**
- `ai_scores` таблица (profile_id FK, clip_score, llm_score, combined_score, recommendation, confidence, confidence_score, reasons JSON, model_version, created_at)
- INDEX на profile_id и created_at, FK ON DELETE CASCADE

**Config Format:**
```yaml
ai:
  enabled: true
  backend: "remote"            # "local" (stubs) or "remote" (Ubuntu AI Server через прокладку)
  remote:
    base_url: "http://144.31.139.206:8000"  # внешний порт прокладки → Ubuntu AI Server
    timeout: 90
    max_retries: 1
  images:
    enabled: true
    max_images: 5
    max_size_mb: 10
    timeout: 60
  clip:
    enabled: true
    model: "openai/clip-vit-base-patch32"
  llm:
    enabled: true
    provider: "ollama"
    model: "qwen3:8b"
    api_key: ""
    timeout: 90
    max_retries: 1
  scoring:
    clip_weight: 0.5
    llm_weight: 0.5
    like_threshold: 0.75
```

**Stage 4.3 — что сделано (реальный E2E):**
- Проброс Ubuntu AI Server наружу через прокладку `144.31.139.206:8000` (SSH reverse tunnel + autossh systemd `ai-tunnel.service`)
- Проверен с Windows: `/health` (gpu/llm/clip=true), `/v1/models` (qwen3:8b, clip-vit-base-patch32)
- Реальный `/v1/llm/evaluate` → 200: score/confidence/reasons (latency ~3 сек)
- Реальный `/v1/clip/analyze` (multipart) → 200: clip_score/images_analyzed/status (latency ~0.5 сек)
- **Исправлен контракт CLIP-клиента** (`services/remote_clip_client.py`):
  - multipart-поле `images` → `files` (сервер возвращал 422)
  - ответ `aesthetic_score` → `clip_score` (иначе скор был 0.0)
- Live E2E: Profile(PASS) → LLM 0.7 + CLIP 0.9728 → combined 0.836 → LIKE → ai_scores в SQLite
- Сценарии устойчивости: сервер недоступен → REVIEW/DISLIKE, RAW/DATA не теряются, коллектор не падает

**Bug fixes:**
- Rich markup: `[/green]` → `[/]` для `[bold green]...` тегов (pre-existing)
- `confidence` tuple unpacking в AIScoringService.evaluate()
- CLIP remote-контракт: поле `files` + ответ `clip_score`

### Stage 5 — Что реализовано (AI Decision Engine)

**Модели (`models/decision.py`):**
- `AIDecision` (StrEnum): LIKE / REVIEW / DISLIKE
- `AIDecisionResult` (Pydantic v2): id, profile_id, decision, combined_score, llm_score, clip_score, confidence, reasons, evaluated_at, scoring_version; `reasons_json()`

**Config (`app/config.py`):**
- `DecisionWeightsConfig` (llm/clip, не оба 0)
- `DecisionConfig`: like_threshold 0.75, review_threshold 0.50, min_confidence 0.60, scoring_version "v1", weights
- `AIConfig.decision = DecisionConfig()`; `RemoteAIConfig.api_key` + `api_key_or_none()`

**AIScoringService (`services/ai_scoring_service.py`) — Stage 5 API:**
- `score_text(profile)` → LLMScore|None; `score_images(profile, image_data_list)` → CLIPScore|None
- `score_profile(profile, image_data_list)` → AIScore (без сохранения)
- `evaluate(profile, image_data_list)` → AIScore (сохраняет в ai_scores)
- Сигналы помечаются недоступными (None) при недоступности шлюза (`_llm_failed`/`_clip_failed`)
- `LLMScore.prompt_version` = "llm-v3" (клиент помечает версию промпта)

**DecisionService (`services/decision_service.py`):**
- `evaluate(profile_id, image_data_list)` / `evaluate_profile(profile, image_data_list)` / `get_latest` / `get_history`
- Алгоритм `_combine` (веса `ai.decision.weights`, отсутствующий сигнал не 0) + `_decide`:
  - Инвариант `NO_HARD_NEGATIVE_MUST_NOT_BECOME_DISLIKE`: семантический DISLIKE только при подтверждённом hard negative (пользовательский SKIP/`USER_SKIP`, признак LLM/`LLM_SKIP`, REJECT-фильтр)
  - REJECT → DISLIKE/FILTER_REJECTED; REVIEW → REVIEW (никогда LIKE, никогда не-DISLIKE без негатива)
  - PASS: USER_SKIP → DISLIKE; LLM hard negative → DISLIKE; нет сигналов → REVIEW/AI_UNAVAILABLE; combined ≥ like ∧ conf ≥ min_conf → LIKE; иначе low-conf → REVIEW/LOW_CONFIDENCE; combined ≥ review → REVIEW; иначе REVIEW/INSUFFICIENT_DATA (НЕ DISLIKE)
- Один вызов шлюза: внутри использует `AIScoringService.evaluate` (сохраняет и `ai_scores`, и `ai_decisions`)
- Telegram-free; сохраняет решение в БД, лог INFO

**Database (`database/database.py`):**
- Таблица `ai_decisions` (+ индексы по profile_id/evaluated_at, FK → profiles ON DELETE CASCADE)
- Методы `save_ai_decision`, `get_latest_ai_decision`, `get_ai_decision_history`

**Интеграция (OBSERVE):**
- `main.py`: сборка `DecisionService`; `collectors/dvinchik_collector.py`: при PASS вызывает `decision_service.evaluate` + вывод панели `AI DECISION` (Mode: OBSERVE, действий нет)
- Если decision_service отсутствует — прежний путь `AIScoringService.evaluate` + `AI SCORE`

**Тесты:**
- `tests/test_ai_scoring.py` (10, offline MockLLM/MockCLIP)
- `tests/test_decision.py` (20 сценариев: пороги/веса, PASS/REJECT/REVIEW, low-confidence, LLM-only, CLIP-only, AI_UNAVAILABLE, история, FK cascade, prompt_version)
- `tests/e2e_ai.py` — REAL_E2E против живого шлюза (не в обычном pytest; `python tests/e2e_ai.py`)
- Итог: **266 passed**

**Live REAL_E2E подтверждён:** health ok → LLM 0.70 + CLIP 0.97 → combined 0.78 → confidence 0.88 → **LIKE** → `ai_decisions` в SQLite (Windows → httpx → FastAPI → Qwen3/CLIP → решение).

### Stage 6 — Что реализовано (Human Review & Analytics)

**Модель (`models/human_decision.py`):**
- `HumanDecision` (StrEnum): APPROVE (одобряет выбор AI) / REJECT (отвергает выбор AI) / SKIP (без решения)
- `AgreementStatus`: AGREEMENT (APPROVE) / DISAGREEMENT (REJECT) / UNRESOLVED (SKIP) — через `from_human()`
- `HumanDecisionResult`, `HumanReview` — сохранение + joined-представление

**Database (`database/database.py`):**
- Таблица `human_decisions` (FK profile_id→profiles.id, ai_decision_id→ai_decisions.id ON DELETE CASCADE, `UNIQUE(ai_decision_id)` — одна рецензия на AI-оценку) + 4 индекса
- Миграция (`_migrate` через PRAGMA table_info): добавлена колонка `prompt_version` в `ai_decisions` (обратно-совместимо для существующих БД)
- Методы: `save_human_decision`, `get_human_decision_history`, `get_latest_human_decision`, `is_human_reviewed`, `get_all_human_history`, `get_pending_review` (oldest-first LEFT JOIN), `get_pending_count`, `get_human_reviews_with_ai` (joined), + аналитика (counts/breakdowns)

**ReviewService (`services/review_service.py`)** — Telegram-free:
- `get_next` / `get_pending` / `get_pending_count` — очередь: только существующие AI-решения, oldest-first, reviewed-комбо исключено, новый ai_decision_id возвращается в очередь
- `save_decision(profile_id, ai_decision_id, HumanDecision)` → сохраняет; блокирует повторную рецензию
- `get_history` / `is_reviewed` / `latest_for_profile`

**AnalyticsService (`services/analytics_service.py`)** — Telegram-free, READ ONLY:
- `get_overview` / `get_ai_stats` / `get_human_stats` / `get_agreement_stats` — Agreement rate = AGREEMENT/(AGREEMENT+DISAGREEMENT); SKIP исключён из знаменателя; `null` (не 0%) при знаменателе 0
- `get_disagreements(sort)` — sort: newest | score | confidence
- `get_score_distribution` (по порогам конфига), `get_ai_breakdown`, `get_filter_breakdown`, `get_scoring_version_breakdown`, `get_prompt_version_breakdown`

**CSV export (`services/review_export.py`) — Telegram-free:**
- `export_review_csv(db, directory)` → CSV с 12 полями (profile_id, ai_decision, llm_score, clip_score, combined_score, confidence, human_decision, agreement, scoring_version, prompt_version, created_at, reviewed_at); RuntimeError при пустой БД

**Telegram UI (`telegram/review_bot.py`) — единственный Stage 6 слой с Telethon:**
- Команды: `/review`, `/profile <id>`, `/stats`, `/ai_stats`, `/disagreements [sort]`
- Inline-кнопки: `review:approve:<ai_decision_id>` / `review:reject:<...>` / `review:skip:<...>`, + `review:next`, `disagreement:sort:<newest|score|confidence>` (в callback только id, без PII)
- Панель анкеты с рамкой (box-drawing), ошибки изолированы try/except

**Интеграция (OBSERVE/REVIEW):**
- `main.py`: wiring ReviewService/AnalyticsService/ReviewBot + CLI `--export-review` (диспатч через `if __name__ == "__main__"`)
- `prompt_version` прокинут сквозь `AIScore`/`AIDecisionResult`/`DecisionService`/`AIScoringService`/`save_ai_decision`

**Тесты:**
- `tests/test_human_review.py` (29: модель, APPROVE/REJECT/SKIP, agreement, повтор → raise, история, очередь oldest-first, reviewed исключён, re-enter, FK cascade, privacy)
- `tests/test_analytics.py` (20: итоги, agreement null-не-0%, breakdowns, кастомные пороги, сортировка disagreement, CSV export)
- `tests/test_review_ui.py` (5: регистрация 6 handler'ов, callback-парсинг, панель, полный поток через mocked event, все renderer'ы, отсутствие PII в кнопках)
- Итог: **315 passed**

### Следующие этапы

**Stage 7: Dialog Manager**
- Автоматическая отправка сообщений
- Генерация реплик
- Управление диалогами

**Stage 8: Production**
- Мониторинг
- Алертинг
- Адаптация под изменения API

## Ограничения

### Категорически НЕ реализовывать (Telegram-действия) — до Stage 7:
- Выполнение LIKE / DISLIKE в Telegram (Decision Engine лишь считает решение в OBSERVE)
- Автоматический swipe
- Автоматический переход к следующей анкете
- Автоматические сообщения
- Dialog Manager
- Message Generator

## Тесты

```bash
# Все тесты
python -m pytest tests/ -v

# Только Stage 4/5 (AI)
python -m pytest tests/test_ai.py tests/test_ai_scoring.py tests/test_decision.py -v

# Только Stage 6 (Human Review & Analytics)
python -m pytest tests/test_human_review.py tests/test_analytics.py tests/test_review_ui.py -v

# REAL_E2E против живого шлюза
python tests/e2e_ai.py

# Только Stage 3 (Filter)
python -m pytest tests/test_filter.py -v

# Только парсер
python -m pytest tests/test_parser.py -v
```

Текущий результат: **472/472 passed** (после фичевого LLM-контракта и инварианта `NO_HARD_NEGATIVE_MUST_NOT_BECOME_DISLIKE`)

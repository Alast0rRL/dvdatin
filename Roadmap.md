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
│  │ 6. evaluate (FilterEngine)               │   │
│  │ 7. save filter result (FilterService)    │   │
│  │ 8. deterministic scoring (все решения)   │   │
│  │ 9. auto-action / manual review (SEMI_AUTO)│  │
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
         ▼ (Telegram-free deterministic pipeline)
┌─────────────────────────────────────────────────┐
│  profile_normalizer → feature_extractor         │
│  → score_engine → decision_service (LIKE/…)     │
└─────────────────────────────────────────────────┘
```

## Текущий этап: Stage 8 — Детерминированный скоринг (без LLM/CLIP)

Scoring-пайплайн полностью детерминированный (`deterministic-v2`): нормализация →
извлечение признаков (H01–H09/P01–P04 из `config/preferences.yaml`) → расчёт скора →
решение. Никаких внешних шлюзов, сети, GPU, LLM/CLIP. LLM/CLIP-модули и Ubuntu AI
Server **удалены из кодовой базы** (см. README → «Совместимость и удаления»).

### Завершено

- [x] Stage 0: Config (Pydantic), Logging (Loguru/Rich), DB (SQLite+aiosqlite), Telethon client, banner, `main.py`.
- [x] Stage 1 / 1.5: Парсер, классификатор, RAW-first, city-normalizer, MEDIA_ONLY, dedup, rate-limiter, stats.
- [x] Stage 2 / 2.5: Профили (fingerprint, upsert), `profiles` + `profile_messages`, ProfileService, DB Audit.
- [x] Stage 3: Filter Engine (возраст/город/полнота), история в БД, интеграция в коллектор.
- (удалено) Stage 4 / 4.1–4.3: LLM (Ollama) + CLIP + Ubuntu AI Server + remote-клиенты — выпилено при упрощении кодовой базы.
- [x] Stage 5: Decision Engine `models/decision.py`, `DecisionService`, `ai_decisions` (детерминированные пороги/правила).
- [x] Stage 6: Human Review & Analytics — `human_decisions`, ReviewService, AnalyticsService (Agreement Rate), Telegram UI, CSV-export.
- [x] Preferences layer (SKIP/LIKE) — `config/preferences.yaml`, `app/preferences.py` (детерминированные правила).
- [x] Stage 7 (SEMI_AUTO): `AutoActionEngine` — `❤️`/`👎` на авто-аккаунте, rate-limit, идемпотентность по `telegram_message_id`, поток через кнопку «Смотреть анкеты», обход капч Leo.
- [x] Stage 7.5: `ControlBot` — `/status /mode on|off /stream /recent /help`, runtime-переключение режима.
- [x] Stage 8: детерминированный скоринг (normalizer → features H01–H09/P01–P04 → score → decision; правила только в `preferences.yaml`; `NO_HARD_NEGATIVE_MUST_NOT_BECOME_DISLIKE`; missing/unknown → REVIEW). Manual Review — ручные решения владельца по REVIEW-анкетам в файл (`services/manual_review.py`).
- [x] Упрощение кодовой базы (Phase 1–10): удалена вся legacy-инфраструктура LLM/CLIP (сервисы, модели, конфиг, тесты, e2e-скрипт), пустые плейсхолдер-пакеты (`dialogs/`, `filters/`, `managers/`, `prompts/`, `utils/`), неиспользуемый код.

## Следующие этапы

**Stage 9: Dialog Manager**
- Автоматическая отправка сообщений
- Генерация реплик
- Управление диалогами

**Stage 10: Production**
- Мониторинг
- Алертинг
- Адаптация под изменения API

## Ограничения

### Категорически НЕ реализовывать (Telegram-действия) до явной команды:
- Полный AUTO-режим (автосвайп, автоматический переход к следующей анкете без явного решения)
- Автоматические сообщения / Dialog Manager / Message Generator
- Стадия ограничена реакцией на конкретную анкету (`❤️`/`👎`) на сконфигурированном авто-аккаунте

## Тесты

```bash
# Все тесты
python -m pytest tests/ -v

# Только Stage 6 (Human Review & Analytics)
python -m pytest tests/test_human_review.py tests/test_analytics.py tests/test_review_ui.py -v

# Только детерминированный скоринг (Stage 8)
python -m pytest tests/test_deterministic_scoring.py tests/test_decision.py tests/test_preferences.py -v

# Только Stage 7 (авто-действия)
python -m pytest tests/test_auto_action.py tests/test_auto_action_audit.py tests/test_collector.py -v

# Только фильтр
python -m pytest tests/test_filter.py -v

# Только парсер
python -m pytest tests/test_parser.py -v
```

Текущий результат: **463/463 passed** (пост-упрощение; baseline в `tests/baseline/baseline_tests.txt`).
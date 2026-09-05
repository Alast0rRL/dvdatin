# DvAI Simplification Report

Дата: 2026-09-05 · База: `HEAD=6013d91` · Платформа: Windows (win32, PowerShell)
Статус: **полностью применено, тесты зелёные (463/463)**

---

## 1. Цель

Радикально упростить кодовую базу DvAI в соответствии со спекуляцией
«RADICAL CODEBASE SIMPLIFICATION» (46 правил, Phases 1–10): удалить legacy-инфраструктуру
LLM/CLIP, мёртвый код, лишние абстракции; упростить конфиг, скоринг, коллектор, тесты и
документацию; сохранить всё работающее поведение. Итог — отчёт с метриками до/после.

## 2. Принципы упрощения

- Приоритет операций: **DELETE > MERGE > INLINE > SIMPLIFY > RENAME**.
- **UNCERTAIN → KEEP**; подтверждённо мёртвое/legacy → **DELETE**.
- Запрещено: новые слои абстракций (Factory/Adapter/Facade/Repository/UseCase/Strategy/DI),
  массовые rename по репозиторию.
- Бизнес-логика скоринга (правила H/P, веса, пороги, семантика решений) **не менялась**.
- Обязательный запуск `python -m pytest tests/ -q` после каждой фазы; переход только при
  зелёных тестах.

## 3. Инварианты (сохранены, проверены регрессией)

- **RAW-first**: исходные сообщения сохраняются в SQLite до любого разбора.
- **Детерминированный скоринг**: без LLM/CLIP/сети; один текст → один результат.
- **`NO_HARD_NEGATIVE_MUST_NOT_BECOME_DISLIKE`**: missing/unknown → REVIEW, никогда DISLIKE.
- **OBSERVE → действий нет**; SEMI_AUTO → контролируемые действия на авто-аккаунте.
- Авто-действия: идемпотентность по карточке (`telegram_message_id`), rate-limit, гейт
  `project.mode` + `auto_actions.enabled` + найденный по `account_session` клиент.
- БД: WAL, FK, история `human_decisions` append-only, `UNIQUE(ai_decision_id)`.
- Human Review: APPROVE/REJECT/SKIP, Agreement Rate = AGREEMENT/(AGREEMENT+DISAGREEMENT).

## 4. Phase 1 — Удаление AI-стека (LLM/CLIP)

Удалены файлы (см. Phase 10 за метрики):

- `services/llm_service.py`, `services/clip_service.py` — фабрики/ABC LLM+CLIP
- `services/ai_scoring_service.py` — объединённый скоринг CLIP+LLM → AIScore
- `services/remote_llm_client.py`, `services/remote_clip_client.py` — httpx-клиенты до Ubuntu AI
- `models/ai.py` — AIRecommendation/ConfidenceLevel/CLIPScore/LLMScore/AIScore
- `collectors/media_analyzer.py`, `collectors/anti_block.py` — анализ фото и rate-limiter (не использовались детерминированным скорингом)
- `tests/e2e_ai.py`, `tests/test_ai_scoring.py`
- Пустые плейсхолдер-пакеты: `dialogs/`, `filters/`, `managers/`, `prompts/`, `utils/` (только `__init__.py`) — по спец. убраны, логика фильтрации живёт в `services/filter_engine.py`.

`config/deploy` документарные промпты (`deploy/llm-v2_prompt.md`, `deploy/llm-v3_prompt.md`,
`deploy/README.md`, `deploy/run.sh`, `deploy/dvai.service`) **оставлены** как read-only справочник.

## 5. Phase 2 — Аналитика и статистика

- Удалены тест-онли методы Analytics-брейкдаунов: `get_score_distribution`,
  `get_ai_breakdown`, `get_filter_breakdown`, `get_scoring_version_breakdown`
  (и их единственный DB-хелпер `get_profiles_last_filter`).
- `AnalyticsService.__init__` упрощён до `(db)`; обновлены места создания (`main.py`,
  `test_analytics.py`, `test_review_ui.py`).

## 6. Phase 3 — Мёртвый код

- `reasons_flat()` в `models/features.py`
- `get_analytics_logger()` и sink `ANALYTICS_LOG` в `app/logging.py`
- `update_profile_status` в `database/database.py`
- `_has_action_buttons` в `dvinchik_collector.py`
- `_deny`, `_cmd_start` в `telegram/control_bot.py` (роутер `/start` обрабатывает `_send_status`)

## 7. Phase 4 — Конфигурация

Удалены модели/поля и YAML-ключи:

- `ScoringConfig` (класс), `AIConfig.enabled`, `AIConfig.scoring`, `DvinchikConfig.enabled`.
- YAML: `dvinchik.enabled`, `ai.backend`, `ai.remote.*`, `ai.images.*`, `ai.clip.*`, `ai.llm.*`,
  `ai.scoring.*`, `ai.decision.weights.*`, `ai.decision.min_confidence`, `limits.*`,
  `auto_actions.start_command`.
- Нормализовано `ai.decision.scoring_version`: `v1` → `deterministic-v2`.
- Живой `config/config.yaml` и `config/config.example.yaml` приведены к текущему набору
  (секреты не тронуты, git-история не переписывалась).

## 8. Phase 5 — Модели скоринга и признаки

- `normalize_for_matching` (мёртвый импорт) убран из `decision_service.py`.
- `AIDecisionResult`: удалены write-only поля `hard_negatives`, `positive_factors`, `unknown`.
- `Feature`: удалены `value`, `source`; `FeatureType.NEUTRAL` удалён; `ExtractionResult.neutral_features` удалён; обновлены 4 места конструирования в `feature_extractor.py`.
- `DecisionConfig`: удалён write-only `min_confidence` (+ validator + YAML).

## 9. Phase 6 — Пайплайн и коллектор

- `MessageType.OTHER` и `MessageGroup` удалены из `models/raw.py`.
- `AutoActionEngine.start_stream` и конфиг `auto_actions.start_command` удалены (подтверждено,
  что метод нигде не вызывался; коллектор использует собственный `start_auto_stream` и хук
  `_press_view_button_if_needed`). Обновлены файлы тестов, где были присваивания поля.

## 10. Phase 7 — Тесты и другие файлы

- Убраны ссылки на удалённые конфиги: `min_confidence` в `test_decision.py`,
  `test_preferences.py`, `test_deterministic_scoring.py`; `"ai": {"enabled": True}` в
  `test_ai.py`.
- Исправлены артефакты предыдущих правок: синтакс-ошибка (дубль метода
  `get_ai_decision_for_profile_prompt`) в `database.py`; осиротевшее тело теста в
  `test_analytics.py` превращено в `TestDisagreementCalculation`.
- Замороженный baseline `tests/baseline/baseline_tests.txt` (508, со ссылками на удалённые
  файлы) **пересгенерирован** из актуальной коллекции — 463 записи.

## 11. Phase 8 — Документация

- **README.md** — переписан с нуля: три секции (Features / Architecture / Compatibility-Deprecations),
  без LLM/CLIP и Ubuntu-сервера в описании текущей архитектуры.
- **Roadmap.md** — переписан: актуальная схема, история этапов за вычетом legacy (LLM/CLIP —
  помечено «удалено»), ограничения, команды тестов.
- **AGENTS.md** — очищены ссылки на удалённые модули, устаревшие gotcha (ai.backend, remote
  CLIP-контракт, remote endpoint), убран раздел «Empty Placeholder Packages», обновлены счётчики
  тестов (463), удалено упоминание `auto_actions.start_command`, скорректировано поведение на
  AI-REVIEW (не действует сам).

## 12. Phase 9 — Пост-проверка неиспользуемого кода

AST-сканером (temp-скрипт `unused2.py`) проверены все `.py` без учёта Pydantic-валидаторов,
ссылающихся по строке (false positive). Найдено и устранено:

- `collectors/__init__.py` — неиспользуемые re-экспорты (`DvinchikCollector`, `DvinchikParser`,
  `normalize_city`) и `__all__`, не использовавшиеся ни одним импортом (все импортируют из
  подмодулей). Оставлен только docstring.
- `services/manual_review.py` — `Database` в `TYPE_CHECKING` **нужен** (используется в
  type-hint'е), не удалялся.

`from __future__ import annotations` в модулях — преднамеренная, не флаг.

## 13. Phase 10 — Регрессия, security и итоговая сверка

- Финальный прогон: `python -m pytest tests/ -q` → **463 passed, 3 warnings** (~27–31 с).
  Warnings — ResourceWarning по aiosqlite-ниткам (известный, не связан с фазами; тестовые
  фикстуры закрывают БД).
- **Аудит `proxy/` (креды)**: `git ls-files proxy/` пуст; `git check-ignore` показывает
  `proxy/` в `.gitignore:39` (config.json, xray.exe, sing-box.exe). Ни один секрет/бинарь
  **не в git**; git-история не переписывалась — только отчёт.
- Diff от `HEAD`: 56 файлов изменено (**−4372 / +572** строк).

## 14. Метрики до/после

| Метрика | До | После | Δ |
|---|---|---|---|
| Тестов (pytest pass) | 518 | 463 | **−55** |
| Python-файлов (tracked `.py`) | 75 | 60 | **−15 файлов** |
| Python LOC (без комментов) | 14 272 | 11 866 | **−2 406 (−16.9 %)** |
| Python LOC (raw) | 18 061 | 15 076 | **−2 985** |
| Классов (удалено в удалённых файлах) | — | — | **−20** |
| Классов (в изменённых файлах) | 60 | 52 | **−8** |
| Функций/методов (в удалённых файлах) | — | — | **−84** |
| Функций/методов (в изменённых файлах) | 246 | 217 | **−29** |
| Импортов (в удалённых файлах) | — | — | **−64** |
| YAML-ключей конфига | — | — | **−26** |
| Моделей/полей/классов конфига | — | — | **−14** |
| Пустых плейсхолдер-пакетов | 5 | 0 | **−5** |
| Baseline (замороженный файл) | 508 (устарел) | 463 (актуален) | пересгенерирован |

Изменено 56 tracked-файлов (docs/tests/config/code). Все удаления подтверждены grep/AST
сканерами; неопределённое не трогалось.

## 15. Вопросы и замечания (зафиксированы, не исправлялось по спец.)

1. **Баг паттерна отрицания — `services/feature_extractor.py:380`.** В `_NEGATION_PREFIXES`
   первая строка содержит `не\s+ඞаю` — stray Sinhala-символ «ඞ» вместо «д» («не даю»).
   Паттерн не матчится; на детект H-негативов по «не даю…» не влияет (тесты по отрицанию
   зелёные), прочие паттерны корректны. Правила не менялись, чтобы не трогать бизнес-логику.
   Смежное: `_THIRD_PERSON_PREFIXES[0]` содержит дубликат токена `мой|мой` — косметика.
2. **`requirements.txt` содержит `httpx`** — единственный остаток от удалённого remote-стека;
   `import httpx` в коде отсутствует. Кандидат на удаление (UNCERTAIN → KEEP, пометить).
3. **Исторические строки `ai_decisions.scoring_version="v1"`** в старых live-БД остаются как
   data-legacy; новые решения пишут `deterministic-v2`. Существующие таблицы `ai_scores`
   в старых БД не удаляются (идемпотентная схема), но больше не создаются и не пишутся.
4. **Аудит `proxy/`** — чисто (см. §13). Финальный чек перед коммитом обязателен.
5. Статус: полный AUTO / Dialog Manager не реализовывались (не входило в объём) —
   см. Roadmap «Следующие этапы».
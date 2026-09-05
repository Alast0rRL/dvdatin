# DvAI — Система автоматизации знакомств в Telegram

> **D**ayvinchik **AI** — коллектор + детерминированный скоринг + Human Review + авто-действия для сервиса знакомств «Дайвинчик» (Telegram).
> Текущий этап: **v0.7 / Stage 8 (SEMI_AUTO)** — детерминированный скоринг; на анкеты с авто-аккаунта отправляются `❤️`/`👎`, на AI-REVIEW бот ждёт ручного решения владельца. Полный AUTO не реализован.

---

## 1. Features

- **RAW-first коллектор** (`collectors/dvinchik_collector.py`): каждое сообщение Telegram сохраняется в SQLite **до любого разбора**. Потеря RAW считается ошибкой. Фоновая обработка (`RawQueue`/`RawWorker`) не блокирует приём событий; после рестарта необработанные RAW доставляются повторно (at-least-once).
- **Классификация и парсинг анкет** (`dvinchik_parser.py`): PROFILE / MEDIA_ONLY / MATCH / SERVICE / UNKNOWN; выделение имени/возраста/города/описания; нормализация городов (`city_normalizer.py`); дедупликация по fingerprint (in-memory + `UNIQUE` в БД).
- **Multi-account**: несколько Telegram-аккаунтов в `telegram.accounts`, общий pipeline, одно сообщение обрабатывается один раз.
- **Фильтрация** (`services/filter_engine.py`, `services/filter_service.py`): возраст/город/полнота данных → PASS / REJECT / REVIEW; правила из `config.yaml` (`filters:`), история в `filter_results`.
- **Детерминированный скоринг (Stage 8)**: `Profile.text` → `profile_normalizer` → `feature_extractor` (правила H01–H09 / P01–P04 из `config/preferences.yaml`) → `score_engine` → `decision_service` → **LIKE / REVIEW / DISLIKE**. Без LLM/CLIP/сети: один и тот же текст всегда даёт один и тот же результат. `DecisionService.evaluate()` считается для **всех** результатов фильтра.
- **Слой предпочтений (SKIP/LIKE)** (`app/preferences.py`): персональные правила в `config/preferences.yaml` (gitignored). SKIP → жёсткий DISLIKE; LIKE-фактор поднимает DISLIKE → REVIEW (анкета не теряется). Инвариант `NO_HARD_NEGATIVE_MUST_NOT_BECOME_DISLIKE`: missing/unknown информация → REVIEW, никогда DISLIKE.
- **Human Review (Stage 6)**: очередь профилей с AI-решением, ручная оценка APPROVE / REJECT / SKIP, метрика **AI/Human Agreement Rate** = AGREEMENT/(AGREEMENT+DISAGREEMENT) (SKIP исключён; `null` при нулевом знаменателе). Telegram-UI (`telegram/review_bot.py`): `/review`, `/profile`, `/stats`, `/ai_stats`, `/disagreements`. CSV-экспорт: `python main.py --export-review`.
- **Авто-действия (Stage 7, SEMI_AUTO)** (`collectors/auto_action.py`): `❤️`/`👎` на авто-аккаунте по решениям LIKE/DISLIKE; rate-limit `interval_sec`; идемпотентность **по карточке** (`telegram_message_id`); фильтровые не-PASS тоже получают `👎` (лента Leo не замирает); автопродолжение ленты кнопкой «🚀 Смотреть анкеты»; обход капч/проверок Leo (нажимается последняя reply-кнопка, только на явные маркеры капчи). Гейт: режим `project.mode ∈ {SEMI_AUTO, AUTO}` + `auto_actions.enabled` + найден клиент по `account_session`. OBSERVE → действий нет.
- **Manual Review (Stage 8)** (`services/manual_review.py`): когда скоринг выдаёт REVIEW, бот **не действует сам** — пересылает карточку владельцу и ждёт его ручного решения. Исходящее `❤️`/`👎` владельца перехватывается и записывается в файл `data/reviews/review_log.json`/`.md` (только для активных REVIEW-анкет).
- **Control Panel (Stage 7.5)** (`telegram/control_bot.py`): `/status /mode on|off /stream /recent /help` (+ inline-кнопки) только от `control.allowed_user_ids`; режим меняется на лету и персистится в `config.yaml`.
- **SAFE по умолчанию**: режимы `project.mode` (OBSERVE / SEMI_AUTO / AUTO). `OBSERVE` только наблюдает и рекомендует; авто-действия включаются только явно.

---

## 2. Architecture

### Поток данных

```
Telegram (RAW)
   → save RAW в SQLite (ВСЕГДА первым, UNIQUE(chat_id, telegram_message_id))
   → classify: PROFILE / MEDIA_ONLY / MATCH / SERVICE / UNKNOWN
   → PROFILE → upsert_profile (fingerprint-дедуп)
   → FilterService.evaluate(profile) → PASS / REJECT / REVIEW
   → DecisionService.evaluate(profile, filter_result)   # ДЛЯ ВСЕХ результатов
        профиль → профиль                 normalizer → feature_extractor
        (H01–H09/P01–P04 из preferences.yaml) → score_engine → решение
   → AutoActionEngine.maybe_act(decision) # только SEMI_AUTO/AUTO (❤️/👎/REVIEW-уведомление)
   → ReviewBot                            # человеческая рецензия (APPROVE/REJECT/SKIP)
```

### Компоненты

| Зона | Процесс | Компоненты | Контакт с Telegram |
|---|---|---|---|
| **Collector** | сбор и сохранение | `dvinchik_collector`, `raw_worker`, `raw_queue`, `dedup`, `city_normalizer`, `stats`, `auto_action` | ✅ Telethon |
| **Scoring (детерминированный)** | решение | `filter_engine`, `filter_service`, `profile_normalizer`, `feature_extractor`, `score_engine`, `decision_service`, `app/preferences` | ❌ Telegram-free |
| **Review + Analytics** | ручная рецензия и аналитика | `review_service`, `analytics_service`, `review_export`, `manual_review` | ❌ Telegram-free |
| **Telegram UI** | вывод и управление | `review_bot`, `control_bot` | ✅ Telethon |

> Единственные слои с Telethon: `collectors/dvinchik_collector.py`, `collectors/auto_action.py`, `telegram/`. Всё остальное — чистая логика (Profile/str/Config), тестируется без живого Telegram.

### Структура проекта

```
dvdatin/
├── main.py                      # Точка входа: конфиг, сборка стека, цикл, --export-review
├── run.bat                      # Запуск на Windows (chcp 65001, UTF-8)
├── requirements.txt / requirements-dev.txt
├── AGENTS.md / Roadmap.md / README.md / DvAI_SIMPLIFICATION_REPORT.md
├── config/
│   ├── config.example.yaml      # Шаблон (коммитится)
│   ├── config.yaml              # Живой конфиг (gitignored, секреты)
│   ├── preferences.example.yaml # Шаблон правил SKIP/LIKE (коммитится)
│   └── preferences.yaml         # Живые правила пользователя (gitignored)
├── app/                         # config.py (Pydantic), preferences.py, logging.py, banner.py
├── core/types.py                # Mode (OBSERVE/SEMI_AUTO/AUTO), LogLevel
├── database/database.py         # SQLite + aiosqlite (все таблицы, PRAGMA, миграции)
├── telegram/                    # client.py (Telethon), review_bot.py, control_bot.py
├── models/                      # raw.py, profile.py, filter.py, features.py, decision.py, human_decision.py
├── services/                    # Telegram-free бизнес-логика (см. таблицу выше)
├── collectors/                  # см. таблицу выше
├── tests/                       # 463 теста (16 файлов), baseline в tests/baseline/
├── deploy/                      # systemd unit + runbook
├── proxy/                       # vendored xray-core + VLESS (НЕ коммитить)
└── data/                        # БД, сессии, логи, экспорт (gitignored)
```

### База данных (SQLite, WAL)

Схема идемпотентна (`CREATE TABLE IF NOT EXISTS`), внешние ключи через PRAGMA.

| Таблица | Назначение |
|---|---|
| `raw_messages` | Сырые сообщения (append-only; включает `raw_entities`, `reply_markup`) |
| `profiles` | Профили (name/age/city/description/fingerprint/status…) |
| `profile_messages` | Связь профиль ↔ сообщения (в т.ч. MEDIA_ONLY) |
| `chat_context` | Контекст «последняя анкета чата» |
| `filter_results` | История фильтрации (PASS/REJECT/REVIEW) |
| `ai_decisions` | Решения DecisionService (LIKE/REVIEW/DISLIKE; `scoring_version=deterministic-v2`) |
| `auto_actions_log` | Отправленные `❤️`/`👎` (per-card, `telegram_message_id`) |
| `human_decisions` | Решения человека (APPROVE/REJECT/SKIP, append-only, `UNIQUE(ai_decision_id)`) |

### Конфиг (config.yaml)

```yaml
telegram:
  accounts:
    - api_id: 0
      api_hash: ""            # секрет
      phone: "+7..."
      session: dvai           # имя session-файла
      proxy: { enabled: false, type: socks5, host: "", port: 0, username: "", password: "" }

project:
  mode: OBSERVE               # OBSERVE | SEMI_AUTO | AUTO

dvinchik:
  chat_id: 1234060895         # Дайвинчик (Leo)

sources:
  allowed_chat_ids: [1234060895]

filters:
  age:   { min: 18, max: 19 }
  city:  { allowed: ["Санкт-Петербург"] }

ai:
  decision:
    like_threshold: 0.75
    review_threshold: 0.50
    scoring_version: "deterministic-v2"

auto_actions:
  enabled: false
  account_session: "dvai_2"   # сессия авто-аккаунта
  interval_sec: 10.0          # rate-limit между действиями
  notify_chat_id: 0           # уведомления владельцу (0 = выкл)

control:
  enabled: false
  allowed_user_ids: [8525808108]

manual_review:
  enabled: false
  file: "data/reviews/review_log.json"
  format: json                # json | md

logging:
  level: INFO
```

### Запуск и тесты

```bash
python -m venv venv
# Windows: venv\Scripts\activate ; Linux: source venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt
cp config/config.example.yaml config/config.yaml
cp config/preferences.example.yaml config/preferences.yaml   # по желанию

python main.py                 # или run.bat на Windows
python main.py --export-review # CSV-экспорт рецензий

python -m pytest tests/ -v     # 463 теста
```

### Ключевые константы

- Дайвинчик (Leo) chat_id по умолчанию: **`1234060895`**
- Пороги решения: LIKE **0.75**, REVIEW **0.50**
- Версия скоринга: **`deterministic-v2`**
- Версия баннера (`app/banner.py`): **0.7**
- Авто-интервал: `interval_sec` (default **10 s**)

---

## 3. Compatibility / Deprecations

### Удалено (упрощение кодовой базы)

- **Весь LLM/CLIP-стек**: `services/llm_service.py`, `services/clip_service.py`, `services/ai_scoring_service.py`, `services/remote_llm_client.py`, `services/remote_clip_client.py`, `models/ai.py`, `collectors/media_analyzer.py`, `collectors/anti_block.py`, `tests/e2e_ai.py`, `tests/test_ai_scoring.py`. Ubuntu AI Server больше не используется. Документарные артефакты деплоя (`deploy/llm-v2_prompt.md`, `deploy/llm-v3_prompt.md`, `deploy/README.md`, `deploy/run.sh`, `deploy/dvai.service`) оставлены в репозитории как read-only справочный материал.
- **Пустые плейсхолдер-пакеты**: `dialogs/`, `filters/`, `managers/`, `prompts/`, `utils/`.
- **Тест-онли аналитика**: `get_score_distribution`, `get_ai_breakdown`, `get_filter_breakdown`, `get_scoring_version_breakdown` (AnalyticsService теперь `AnalyticsService(db)`).
- **Мёртвый код**: `update_profile_status`, `get_profiles_last_filter`, `reasons_flat()`, `get_analytics_logger()`/`ANALYTICS_LOG`, `_has_action_buttons`, `_deny`, `_cmd_start`, `AutoActionEngine.start_stream`.
- **Конфиг-ключи**: `ai.enabled`, `ai.backend`, `ai.remote.*`, `ai.scoring.*`, `ai.clip.*`, `ai.llm.*`, `ai.decision.weights`, `ai.decision.min_confidence`, `dvinchik.enabled`, `auto_actions.start_command`, `limits.*`, `ImagesConfig`.
- **Модели/поля**: `MessageType.OTHER`, `MessageGroup`, `FeatureType.NEUTRAL`, `Feature.value`, `Feature.source`, `ExtractionResult.neutral_features`, `AIDecisionResult.hard_negatives/positive_factors/unknown`, `ScoringConfig`, `DecisionConfig.min_confidence`.

### Устаревшие таблицы в существующих БД

- `ai_scores` (CLIP/LLM-скоры): больше не создаётся; существующая таблица в старых БД не удаляется (идемпотентная схема), но не пишется скорингом.
- `ai_decisions.scoring_version="v1"` в исторических записях старых БД — новые решения пишут `deterministic-v2`.

### Известные ограничения / замечания

- **Паттерн отрицания «не даю»** в `services/feature_extractor.py` (`_NEGATION_PREFIXES`, `не\s+ඞаю` — испорченный символ). На обнаружение жёсткого негатива H-признаков по фразам вида «не даю…» это не влияет (паттерн не матчится), поведение других признаков корректно. Зафиксировано в отчёте об упрощении; правила не менялись, чтобы не трогать бизнес-логику.
- `requirements.txt` ещё содержит `httpx` — единственная оставшаяся зависимость от удалённого remote-стека; фактически не используется кодом (кандидат на удаление).
- `proxy/` содержит vendored xray-core и реальный VLESS-конфиг — не коммитить изменения креденшиалов.
- Полный AUTO / Dialog Manager не реализуются до явной команды (см. `Roadmap.md`).

### Лицензия

Проект внутренний/личный. Прокси-настройки и API-ключи — секреты, в репозиторий не коммитятся.
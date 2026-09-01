# DvAI — Система автоматизации знакомств в Telegram

> **D**ayvinchik **AI** — умный коллектор + AI-скоринг + Human Review + авто-действия для сервиса знакомств «Дайвинчик» в Telegram.
> Текущий этап: **v0.7 / Stage 7 (SEMI_AUTO)** — AI отправляет ❤️/👎 на анкеты с авто-аккаунта, управление через Telegram-панель.

---

## 1. Цели и идеи

### Какую боль закрывает проект

Сервис знакомств «Дайвинчик» работает в Telegram: бот присылает анкеты (профили) и
медиа, человек вручную просматривает каждую. На больших объёмах это превращается в
рутину: нужно читать сотни анкет, оценивать фото, отсеивать неподходящих (по возрасту,
городу, «своим» критериям), не пропустить взаимные мэтчи. Ручная обработка:
- медленная и утомительная;
- не масштабируется от множества аккаунтов;
- непоследовательна (оценки «на глаз» плавают);
- легко пропустить важное (мэтч, перспективная анкета) или наоборот потерять время на неподходящих.

### Что решает проект (глобальная задача)

**DvAI автоматизирует весь цикл обработки анкет** — от перехвата сообщений до вынесения
решения «подходит / на просмотр / не подходит» — оставляя человеку только самую
ценную работу: ручную рецензию перспективных кандидатов. При этом система построена
**безопасно по умолчанию**: режим **OBSERVE** только **наблюдает (OBSERVE)** и
**рекомендует (REVIEW)** — *никогда не действует* в Telegram от вашего имени. Начиная
со **Stage 7 (SEMI_AUTO)** авто-действия (⚠️/👎) включаются только явно через
`project.mode: SEMI_AUTO` + `auto_actions.enabled: true`.

### Концепция (поток данных)

```
Telegram (RAW)
   → сохранить всё ✅ (инвариант RAW-first)
   → классифицировать / распарсить
   → дедуплицировать в Profile
   → отфильтровать (возраст/город)
   → AI-оценить (CLIP фото + LLM текст)
   → вынести решение LIKE / REVIEW / DISLIKE
   → человек подтверждает/отклоняет решение (Human Review)
   → аналитика согласия «AI ↔ человек» (калибровка AI)
```

### Философия

1. **RAW-first инвариант.** Каждое сообщение сохраняется в SQLite **до любого разбора**.
   Данные первичны — потеря RAW считается ошибкой, а не штатной ситуацией.
2. **Telegram-действия — только по явной команде.** Архитектурно OBSERVE/REVIEW
   не умеют нажимать/отправлять. Автоматика (Stage 7) — отдельный слой (`AutoActionEngine`),
   гейтируемый режимом `project.mode` и `auto_actions.enabled`, работает только на
   сконфигурированном авто-аккаунте. Полный AUTO/диалог-менеджер — не раньше Stage 8.
3. **AI — это советник, а не владелец.** Финальное решение остаётся за человеком
   через Human Review; AI калибруется по метрике согласия, а не «учится на угадывании».
4. **«Свои правила» живут в файле, а не в коде.** Персональная калибровка
   (SKIP/LIKE-факторы) — в `config/preferences.yaml`, а не зашита в сервисы.
5. **Устойчивость к падению ИИ.** Ошибки AI/шлюза не ломают сбор данных:
   деградация до `AI_UNAVAILABLE → REVIEW`, RAW не теряется.

---

## 2. Архитектура

### Обзор компонентов

Система состоит из трёх логических зон:

| Зона | Процесс | Компоненты | Контакт с Telegram |
|---|---|---|---|
| **Collector (Windows/Linux)** | сбор и сохранение данных | `dvinchik_collector`, `raw_worker`, `raw_queue`, `dedup`, `city_normalizer`, `anti_block`, `stats` | ✅ Telethon |
| **AI + Decision (Windows/Linux)** | анализ и решение | `filter_engine/service`, `clip_service`, `llm_service`, `ai_scoring_service`, `decision_service`, `preferences` | ❌ Telegram-free |
| **Review + Analytics (Windows)** | ручная рецензия и аналитика | `review_service`, `analytics_service`, `review_export`, `review_bot` | ✅ только `review_bot.py` |

Отдельно живёт **Ubuntu AI Inference Server** (FastAPI): LLM (Ollama `qwen3:8b`) + CLIP
на GPU. Windows/Linux обращается к нему по HTTP через **remote-клиенты** (`httpx`).

### Пайплайн обработки сообщения (OBSERVE)

```
Telegram NewMessage (входящее)
   │
   ▼ 0. SOURCE FILTER (allowlist чатов)
   ▼ 1. DEDUP (in-memory, атомарно)
   ▼ 2. RAW save в SQLite (ВСЕГДА ПЕРВЫМ, UNIQUE(chat_id, telegram_message_id))
   ▼ 3. enqueue в RawQueue (worker потребляет фоном — не блокирует Telegram-handler)
   │
   ▼ Постановка в worker: parse → filter → AI
   ├─ classify → PROFILE / MEDIA_ONLY / MATCH / SERVICE / UNKNOWN
   ├─ PROFILE → upsert_profile(Profile, fingerprint-дедуп)
   ├─ Фильтр FilterService.evaluate(profile) → PASS / REJECT / REVIEW
   │     └─ Только PASS:
   │         ├─ скачать изображения (msg.download_media → bytes)
   │         ├─ Remote CLIP (фото) + Remote LLM (текст)
   │         ├─ AIScoringService → AIScore (combined)
   │         └─ DecisionService → LIKE / REVIEW / DISLIKE  (+ предпочтения SKIP/LIKE)
   ├─ MEDIA_ONLY → привязка к последней анкете чата (profile_messages)
   └─ mark_raw_processed (processed_at=now → W3-backlog не повторяет)
```

**Асинхронная обработка.** Хендлер Telegram делает минимум (RAW save + enqueue);
дорогой пайплайн (сеть/AI) выполняется фоновым воркером `RawWorker`, что не блокирует
приём входящих событий даже при медленном AI-шлюзе.

**Устойчивость и видимость:** startup-backlog recovery (`recover_backlog`) доставляет
необработанные RAW после рестарта (at-least-once); `processed_at=NULL` на ошибке pipeline
гарантирует повтор. Per-chat блокировки сохраняют порядок PROFILE → MEDIA_ONLY.

### Multi-account

Поддерживается несколько Telegram-аккаунтов одновременно (`telegram.accounts: [...]`).
Каждый аккаунт — отдельная сессия (`data/sessions/<session>.session`); один общий
пайплайн (dedup / worker / БД); хендлеры регистрируются на каждом клиенте. Одно и то же
сообщение, увиденное разными аккаунтами, дедуплицируется (in-memory + `UNIQUE` в БД) и
обрабатывается ровно один раз.

### База данных (SQLite, WAL)

Схема идемпотентна (`CREATE TABLE IF NOT EXISTS`), внешние ключи включены через PRAGMA.

| Таблица | Назначение |
|---|---|
| `raw_messages` | Сырые сообщения Telegram (append-only, никогда не удаляются; включает `raw_entities` и `reply_markup` = кнопки) |
| `profiles` | Профили (name/age/city/description/fingerprint/status…) |
| `profile_messages` | Связь профиль ↔ сообщения (в т.ч. MEDIA_ONLY) |
| `chat_context` | Контекст «последняя анкета чата» (переживает restart) |
| `filter_results` | История фильтрации (PASS/REJECT/REVIEW) |
| `ai_scores` | Скоры CLIP/LLM/combined + рекомендация скоринга |
| `ai_decisions` | Итоговые решения Decision Engine (LIKE/REVIEW/DISLIKE) |
| `auto_actions_log` | Успешно отправленные Telegram LIKE/DISLIKE; один action на профиль |
| `human_decisions` | Решения человека (APPROVE/REJECT/SKIP, append-only, `UNIQUE(ai_decision_id)`) |

Связи (FK ON DELETE CASCADE): `profiles ← profile_messages / filter_results / ai_scores /
ai_decisions ← human_decisions`.

### Два слоя решений (важно не путать)

| Слой | Модуль | Модель | Что сохраняется | Смысл |
|---|---|---|---|---|
| AI Scoring | `ai_scoring_service.py` | `AIRecommendation` (LIKE/DISLIKE/REVIEW) | `ai_scores` | Скоринговая рекомендация по порогам `ai.scoring` |
| AI Decision | `decision_service.py` | `AIDecision` (LIKE/REVIEW/DISLIKE) | `ai_decisions` | Итоговое решение + предпочтения пользователя |

`DecisionService.evaluate()` — единый вызов шлюза: внутри сохраняет и `ai_scores`, и
`ai_decisions`. Решение считается **только на клиенте**, не на сервере.

### Слой предпочтений пользователя (SKIP/LIKE)

`config/preferences.yaml` (gitignored) содержит персональные правила. `PreferencesEngine`
применяет их **после** порогов, как высший приоритет:

- **SKIP** (напр. «ищу друга», «курит», «есть парень», «покатайте», «под каре», «instagram»)
  → **жёсткий DISLIKE**, CLIP не может перевернуть;
- **LIKE-фактор** (напр. «СПбПУ», «аниме», «игры», «переехала в СПб») → потенциальный
  DISLIKE поднимается до **REVIEW** (анкета не теряется) или до LIKE при высоком скоре.

Пороги `0.75 / 0.50` не меняются. Правила дублируются в серверном LLM-промпте (`llm-v2`),
который живёт на Ubuntu AI Server (не в репозитории).

### Human Review & Analytics (Stage 6)

- **Queue:** только профили с существующим AI-решением; oldest-first; уже рассмотренное
  комбо исключено; новая AI-оценка снова попадает в очередь.
- **Решение человека:** `APPROVE` → AGREEMENT; `REJECT` → DISAGREEMENT; `SKIP` → UNRESOLVED.
- **Метрика:** Agreement Rate = AGREEMENT/(AGREEMENT+DISAGREEMENT); SKIP исключён;
  `null` при знаменателе 0. Называется **«AI/Human Agreement Rate»**, никогда не «AI accuracy».
- **Telegram UI:** `ReviewBot` — команды `/review`, `/profile <id>`, `/stats`, `/ai_stats`,
  `/disagreements [sort]` + inline-кнопки. Единственный Stage 6 компонент с Telethon.
- **CSV:** `python main.py --export-review` → `data/exports/review_*.csv` (12 полей).

### Auto-Actions (Stage 7, SEMI_AUTO)

- **Механика:** лайк/дизлайк в «Дайвинчике» = текст `❤️`/`👎`, отправляемый при активной
  reply-клавиатуре анкеты (`KeyboardButton`, не inline). Без активной анкеты бот отвечает
  «Нет такого варианта ответа».
- **`AutoActionEngine`** (`collectors/auto_action.py`): `maybe_act(decision)` →
  LIKE→`❤️`, DISLIKE→`👎`, REVIEW/None→`SKIP`, выключено→`GATE`. Rate-limit
  `interval_sec` (default 10s).
- **Гейт:** авто-действия активны только при `project.mode ∈ {SEMI_AUTO, AUTO}` И
  `auto_actions.enabled: true` И найден клиент по `account_session`. Решение↔аккаунт:
  действие отправляется только если анкета пришла на авто-аккаунт
  (`task.msg.client is auto_engine.client`).
- **Журнал и идемпотентность:** действие пишется в `auto_actions_log` только после
  успешной отправки; запись и статус профиля `LIKED`/`DISLIKED` фиксируются атомарно.
  Уже записанный профиль, а также повтор после ошибки записи в рамках процесса, не
  отправляет кнопку повторно.
- **Автозапуск потока (безопасный):** `collector.start_auto_stream()` вызывается
  фоном при старте (`main.py`). Он сам гейтится по `enabled`.
- **Обработка уже показанной анкеты:** перед запуском коллектор сканирует
  последние сообщения чата Leo на авто-аккаунте. Если найдена активная анкета —
  самая свежая `PROFILE`-текст («Имя, возраст, город – …»), на которую ещё не
  отправлена реакция (нет исходящего `❤️`/`👎` после неё) — она обрабатывается
  сразу через штатный pipeline (parse → filter → AI → авто-действие). Команда
  `✨🔍` НЕ отправляется (она не приводит новые анкеты; показанная анкета уже
  ждёт только лайк/дизлайк — простым текстом `❤️`/`👎`). Если активной анкеты
  нет — ничего не отправляется.
   Идемпотентность (`auto_actions_log` + in-memory) не даёт продублировать ❤️/👎.
- **Конфиг (`config/auto_actions`):**
  ```yaml
  project:
    mode: SEMI_AUTO          # OBSERVE → действий нет (безопасно по умолчанию)
  auto_actions:
    enabled: true
    account_session: dvai_2  # сессия авто-аккаунта (acc2, Бармалей)
    interval_sec: 10.0       # rate-limit ~6 действий/мин
    start_command: ""         # не используется: ✨🔍 не нужен (анкета уже показана)
  ```
- Полный AUTO / диалог-менеджер не реализуются до явной команды (см. Roadmap).

### Контрольная панель (Stage 7.5)

Управление ботом через Telegram-бота `ControlBot` (`telegram/control_bot.py`):
- **Команды** (только от `control.allowed_user_ids`, default `8525808108`):
  `/status` — текущий режим/статус, `/mode on|off` — SEMI_AUTO/OBSERVE,
  `/stream` — запустить поток анкет сейчас, `/recent` — последние решения AI,
  `/help` — справка. Плюс inline-кнопки (🟢 ON / ⭕ OFF / 📊 Статус / ▶ Поток).
- **Переключение на лету:** `collector.set_mode(Mode)` меняет `AutoActionEngine.mode`
  live (гейт `enabled` пересчитывается) и через `AppConfig.persist_mode()` пишет
  `project.mode` в `config.yaml` — режим переживает restart.
- Гейт: `config.control.enabled: true` регистрирует панель в `main.py` (на `accounts[0]`).
- **Конфиг:**
  ```yaml
  control:
    enabled: true
    allowed_user_ids: [8525808108]   # ваш user_id — только от него принимаются команды
  ```

---

## 3. Структура проекта

```
dvdatin/
│
├── main.py                      # Точка входа: конфиг, сборка стека, цикл, --export-review
├── run.bat                      # Запуск на Windows (chcp 65001, UTF-8)
├── requirements.txt             # Prod-зависимости
├── requirements-dev.txt         # Dev-зависимости (pytest)
├── AGENTS.md                    # Правила/конвенции для агентов (и этот файл)
├── PROJECT.md                   # План проекта, история этапов (Stages), roadmap
│
├── config/                      # Конфигурация
│   ├── config.example.yaml      #   Шаблон (коммитится)
│   ├── config.yaml              #   Живой конфиг (gitignored, секреты)
│   ├── preferences.example.yaml #   Шаблон правил SKIP/LIKE (коммитится)
│   └── preferences.yaml         #   Живые правила пользователя (gitignored)
│
├── app/                         # Прикладной слой
│   ├── config.py                #   Pydantic-модели конфигурации + загрузчик YAML
│   ├── preferences.py           #   PreferencesEngine (SKIP/LIKE)
│   ├── logging.py               #   Loguru (+ Rich)
│   └── banner.py                #   ASCII-баннер (версия)
│
├── core/
│   └── types.py                 # Mode (OBSERVE/SEMI_AUTO/AUTO), LogLevel
│
├── database/
│   └── database.py              # SQLite + aiosqlite (все таблицы, PRAGMA, миграции)
│
├── telegram/                    # Telethon-слой (единственный, кому можно Telethon)
│   ├── client.py                #   create_client / authorize (multi-account)
│   ├── review_bot.py            #   Telegram UI: /review, /stats, inline-кнопки
│   └── control_bot.py           #   Панель управления: /status /mode /stream /recent (Stage 7.5)
│
├── models/                      # Pydantic/dataclass-модели домена
│   ├── raw.py                   #   RawMessage, ParsedProfile, MessageType, FilterResult(raw)
│   ├── profile.py               #   Profile, ProfileStatus, compute_fingerprint
│   ├── filter.py                #   FilterDecision, FilterReason, FilterResult(filter)
│   ├── ai.py                    #   AIScore, CLIPScore, LLMScore, AIRecommendation
│   ├── decision.py              #   AIDecision, AIDecisionResult
│   └── human_decision.py        #   HumanDecision, AgreementStatus, HumanReview
│
├── services/                    # Бизнес-логика (Telegram-free, кроме review_export/…)
│   ├── profile_service.py       #   CRUD + upsert + fingerprint
│   ├── filter_engine.py         #   AgeRule / CityRule / DataCompletenessRule
│   ├── filter_service.py        #   Оценка + история в БД
│   ├── clip_service.py          #   BaseCLIPService (ABC) + локальная заглушка
│   ├── llm_service.py           #   BaseLLMService (ABC) + локальная заглушка
│   ├── ai_scoring_service.py    #   Объединённый CLIP+LLM скоринг → AIScore
│   ├── decision_service.py      #   AI Decision Engine (+ предпочтения)
│   ├── remote_clip_client.py    #   httpx → Ubuntu AI (multipart field "files")
│   ├── remote_llm_client.py     #   httpx → Ubuntu AI (Ollama /v1/llm/evaluate)
│   ├── review_service.py        #   Human Review очередь/сохранение
│   ├── analytics_service.py     #   Read-only аналитика (согласие, breakdowns)
│   └── review_export.py         #   CSV-экспорт рецензий
│
├── collectors/                  # Сбор данных
│   ├── dvinchik_collector.py    #   Перехват, RAW-first, per-chat locks, W3-recovery, outgoing+callback
│   ├── dvinchik_parser.py       #   Классификация и парсинг анкет
│   ├── raw_worker.py            #   Фоновый worker (parse→filter→AI вне хендлера)
│   ├── raw_queue.py             #   Async-буфер (RawQueue)
│   ├── dedup.py                 #   In-memory дедупликация
│   ├── city_normalizer.py       #   Нормализация городов (map + ASCII нормализация)
│   ├── anti_block.py            #   Rate-limiter (защита от блокировки)
│   ├── media_analyzer.py        #   Анализ фото (photo_count → CLIP/NSFW)
│   ├── auto_action.py           #   Stage 7: AutoActionEngine (❤️/👎, rate-limit, start_stream)
│   └── stats.py                 #   Статистика коллектора
│
├── filters/  dialogs/  managers/  prompts/  utils/   # 🅡 ЗАРЕЗЕРВИРОВАНЫ (только __init__.py)
│
├── tests/                       # Тесты (434, без pytest-asyncio)
│   ├── test_*.py                #   Unit/integration для модулей
│   ├── e2e_ai.py                #   REAL_E2E против живого шлюза (не в обычном pytest)
│   └── baseline/                #   Frozen baseline тестов (diff-сверка)
│
├── deploy/                      # Deploy-артефакты (Ubuntu/systemd)
│   ├── dvai.service             #   systemd unit
│   ├── run.sh                   #   Ручной запуск (UTF-8)
│   ├── llm-v2_prompt.md         #   Серверный LLM-промпт (SKIP/LIKE) + инструкция применения
│   └── README.md                #   Runbook деплоя
│
├── proxy/                       # Vendored xray-core + VLESS (НЕ коммитится)
└── data/                        # Данные (gitignored)
    ├── logs/                    #   runtime.log, analytics.log
    ├── exports/                 #   CSV-экспорт рецензий
    └── sessions/                #   Telegram session-файлы (.session)
```

> **Замечание про `filters/`:** это пустой плейсхолдер, зарезервированный для будущего.
> Реальная логика фильтрации живёт в `services/filter_engine.py` и `services/filter_service.py`.

---

## 4. Принятые технические решения (ADR-lite)

### ADR-1: Python 3.12 + современный синтаксис
**Почему:** чистый и выразительный код (`StrEnum`, `X | Y`, `list[str]`), совместимость с
Telethon/aiosqlite. **Компромисс:** требует Python ≥ 3.12.

### ADR-2: Telethon как единственный клиент Telegram
**Почему:** асинхронный, удобные `events.NewMessage`, скачивание media через
`msg.download_media`. **Компромисс:** Telethon разрешён только в `telegram/*` и
`collectors/dvinchik_collector.py` — остальной стек тестируем без живого Telegram
(моки/ABC).

### ADR-3: Telegram-free ядро (инверсия зависимостей)
**Почему:** вся аналитика/скоринг/решения проверяются офлайн и без реального аккаунта.
**Как:** ABC (`BaseCLIPService`, `BaseLLMService`) + локальные заглушки для `backend: local`,
и `Remote*Client` для `remote`. Тесты не зависят от сети.

### ADR-4: SQLite + aiosqlite (WAL, FK, идемпотентная схема)
**Почему:** ноль серверной инфраструктуры, транзакционность, достаточно для однопроцессного
коллектора. **Миграции:** `_migrate` через `PRAGMA table_info` добавляет колонки обратно-совместимо.

### ADR-5: RAW-first инвариант + UNIQUE в БД
**Почему:** данные важнее обработки. Дубликаты переживают restart (в отличие от in-memory
dedup); потеря RAW недопустима. **Компромисс:** нужен фон воркер, чтобы не блокировать handler.

### ADR-6: Фоновая обработка (RawQueue + RawWorker) + backlog recovery (W3)
**Почему:** сетевой AI не должен блокировать приём Telegram-событий; при рестарте
необработанные RAW доставляются повторно (at-least-once).

### ADR-7: AI как опция, деградация вместо краша
**Почему:** шлюз (Ollama/CLIP) нестабилен/платный. **Как:** `ai.enabled`, все AI-вызовы в
try/except, недоступность → `AI_UNAVAILABLE → REVIEW`, данные не теряются.

### ADR-8: CLIP + LLM с раздельными весами, решение только на клиенте
**Почему:** эстетика фото (CLIP) и содержание текста (LLM) дополняют друг друга; финальное
решение и веса принадлежат клиенту, а не серверу. Веса `decision.weights` (llm 0.7 / clip 0.3)
независимы от `scoring` (clip 0.5 / llm 0.5).

### ADR-9: Многослойность решений (Scoring → Decision → Human)
**Почему:** каждый слой решает свою задачу и сохраняется отдельно, что позволяет калибровать
AI против человеческих решений (Agreement Rate) и менять политику на любом слое.

### ADR-10: Правила пользователя — вне кода (preferences.yaml + серверный промпт)
**Почему:** персональная калибровка не должна требовать правок кода/пересборки.
**Компромисс:** текстовый поиск подстрок (простой, но грубый); нужен серверный LLM-промпт
для семантики. Пороги не трогаются.

### ADR-11: OBSERVE/REVIEW по умолчанию — «сначала безопасность»
**Почему:** автолайки/сообщения необратимы и рискованны. Действия внедряются отдельным
слоем (Stage 7) с лимитами (`limits.*`), только по явной команде.

### ADR-12: Multi-account
**Почему:** больше источников/объёма и распределение нагрузки по аккаунтам. **Компромисс:**
один общий pipeline и жёсткая дедупликация, чтобы один chat не обрабатывался дважды.

### ADR-13: Скромные зависимости (нет ОРМ, нет веб-фреймворка на клиенте)
**Почему:** прямой SQL + Pydantic достаточно; меньше зависимостей — меньше атакующая
поверхность и проще деплой. **Компромисс:** ручная работа со схемой/миграциями.

### ADR-14: Двойной формат конфига с обратной совместимостью
**Почему:** поддержка и `telegram: {accounts:[...]}`, и старого `telegram: {api_id,...}`
через before-валидатор упрощает миграцию. `FiltersConfig` тоже поддерживает compat-поля.

---

## 5. Инструкция по развертыванию и запуску

### Локально (Windows / Linux)

```bash
# 1. Клонировать
git clone <url> dvdatin && cd dvdatin

# 2. Виртуальное окружение + зависимости
python -m venv venv
# Windows: venv\Scripts\activate ; Linux: source venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt            # для тестов

# 3. Конфигурация (секреты вне git)
cp config/config.example.yaml config/config.yaml
```

Заполнить `config/config.yaml` (NONE из секретов в git):

```yaml
telegram:
  accounts:
    - api_id: 0            # из https://my.telegram.org
      api_hash: ""
      phone: "+7..."
      session: dvai           # имя session-файла
      proxy: { enabled: true, type: socks5, host: "127.0.0.1", port: 10808 }
    # …можно несколько аккаунтов

dvinchik:
  chat_id: 1234060895         # Дайвинчик (Leo)

filters:
  age:   { min: 18, max: 19 }
  city:  { allowed: ["Санкт-Петербург"] }

ai:
  enabled: false              # false = без AI (только сбор/фильтр); true = включить scoring
  backend: "local"            # "local" (заглушки) | "remote" (Ubuntu AI)
  remote: { base_url: "http://144.31.139.206:8000", timeout: 90, max_retries: 1 }

logging:
  level: INFO
```

Затем по желанию скопировать персональные правила:

```bash
cp config/preferences.example.yaml config/preferences.yaml   # SKIP/LIKE правила (gitignored)
```

### Авторизация и запуск

```bash
# Windows (UTF-8) — run.bat, или напрямую:
python main.py
```

При первом запуске Telethon запросит код подтверждения (вход выполняется интерактивно,
сессия сохраняется в `data/sessions/`). После успешной авторизации коллектор начинает
работать в режиме **OBSERVE**: перехватывает сообщения Дайвинчика, сохраняет RAW,
фильтрует, при PASS скачивает фото и вызывает AI (если включён), выводит решение в консоль.

> `dvinchik.chat_id == 0` → все входящие сообщения просто отображаются в консоли,
> чтобы вы могли узнать реальный chat_id Дайвинчика.

### Экспорт review-датасета (без Telegram)

```bash
python main.py --export-review        # → data/exports/review_YYYYMMDD_HHMMSS.csv
```

### Режим с реальным AI (remote, Ubuntu AI Server)

Клиент обращается к AI-шлюзу `base_url` (прокладка SSH reverse tunnel → Ubuntu AI Server
с Ollama `qwen3:8b` + CLIP на GPU):

```bash
python main.py            # при ai.enabled=true, backend=remote
```

Проверка шлюза:

```bash
curl -s http://144.31.139.206:8000/health      # → HTTP 200
```

При падении шлюза сбор не останавливается — AI деградирует до `AI_UNAVAILABLE → REVIEW`.

### Docker

Официального Docker-образа пока нет (см. Roadmap). Для systemd-деплоя на Ubuntu
используйте `deploy/` (runbook подробно в `deploy/README.md`):

```bash
sudo cp deploy/dvai.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dvai
sudo journalctl -u dvai -f

# ручной запуск
./deploy/run.sh
```

### Тесты

```bash
python -m pytest tests/ -v                  # все 396
python -m pytest tests/test_ai.py -v        # AI (105)
python -m pytest tests/test_preferences.py -v   # предпочтения (10)
python -m pytest tests/test_decision.py -v  # Decision Engine (27)

# REAL_E2E против живого шлюза (не в обычном pytest)
python tests/e2e_ai.py

# Сверка с frozen baseline
diff <(grep '::' tests/baseline/baseline_tests.txt | sort) \
     <(python -m pytest tests/ --collect-only -q | grep '::' | sort)
```

---

## 6. Планы развития (Roadmap)

### Завершено (текущее состояние — Stage 7)

- [x] **Stage 0** — конфиг (Pydantic), логирование (Loguru/Rich), БД, Telethon client, banner, `main.py`.
- [x] **Stage 1 / 1.5** — парсер, классификатор, RAW-first, city-normalizer, MEDIA_ONLY, dedup, rate-limiter, stats.
- [x] **Stage 2 / 2.5** — модель Profile, `profiles` + `profile_messages`, fingerprint, ProfileService, upsert, DB Audit.
- [x] **Stage 3** — Filter Engine (возраст/город/полнота), история в БД, интеграция в коллектор.
- [x] **Stage 4** — AI Scoring: CLIP + LLM (заглушки и рemote), combined, сохранение в `ai_scores`.
- [x] **Stage 4.1–4.3** — Remote AI Client (httpx) + Ubuntu AI Server (FastAPI: Ollama qwen3:8b + CLIP), E2E через прокладку, фикс CLIP-контракта (`files` / `clip_score`).
- [x] **Stage 5** — AI Decision Engine: `models/decision.py`, DecisionService, `ai_decisions`, пороги/веса, решение только на клиенте.
- [x] **Stage 6** — Human Review & Analytics: `human_decisions`, ReviewService, AnalyticsService (Agreement Rate), Telegram review UI, CSV-export.
- [x] **Multi-account** — несколько Telegram-аккаунтов с общим pipeline и дедупликацией.
- [x] **Preferences layer (SKIP/LIKE)** — `config/preferences.yaml`, `app/preferences.py`, интеграция в DecisionService.
- [x] **Серверный LLM-промпт `llm-v2`** — `deploy/llm-v2_prompt.md` (SKIP/LIKE-правила для семантической оценки), клиент помечает `prompt_version=llm-v2`.
- [x] **Захват кнопок (reply_markup)** — read-only разведка слоя действий: `raw_messages.reply_markup`, сериализация в коллекторе, вывод в консоль. Кнопка LIKE ставится по inline-кнопке на анкете (callback_data).
- [x] **Захват исходящих (outgoing capture)** — read-only перехват действий пользователя: `events.NewMessage(outgoing=True)` в чате бота (1234060895). Исходящие эмодзи (лайки/дизлайки) сохраняются в `raw_messages` и помечаются `processed_at` (pipeline пропускается). ground truth для реверса механики LIKE.
- [x] **Callback-query логирование** — read-only разведка inline-кнопок: `events.CallbackQuery()` логирует `callback_data`/собеседника в консоль (без действий и без записи в БД). Дополняет outgoing-capture, если лайк ставится кнопкой.
- [x] **Stage 7 (SEMI_AUTO) — авто-действия** — `AutoActionEngine` (`collectors/auto_action.py`): на основе DecisionService на анкеты авто-аккаунта отправляются `❤️` (LIKE) / `👎` (DISLIKE), REVIEW/None пропускается; rate-limit `interval_sec`; гейт по `project.mode` + `auto_actions.enabled`. Автозапуск потока отключён, так как `✨🔍` невалидна вне определённого состояния Leo. Финальный реверс механики LIKE/👎 как plain-text reply-кнопок.
- [x] **Stage 7.5 — контрольная панель** — `ControlBot` (`telegram/control_bot.py`): /status /mode on|off /stream /recent /help + inline-кнопки; runtime-переключение режима (`collector.set_mode`) с персистентностью в `config.yaml`; авторизация по `control.allowed_user_ids`.

Проверено: **434 теста проходят** (baseline в `tests/baseline/`).

### В разработке / планируется

| | Этап / фича | Идея |
|---|---|---|
| 🔜 | **Stage 8: Dialog Manager** | автоматические сообщения, генерация реплик, управление диалогами (требует переключения `Mode` в AUTO) |
| 🔜 | **Stage 9: Production** | мониторинг, алертинг, адаптация под изменения API Дайвинчика |
| 🔜 | **Docker-образ** | контейнеризация коллектора/клиента |
| 🔜 | **Перенос `llm-v2` на сервер** | применить `deploy/llm-v2_prompt.md` в FastAPI `/v1/llm/evaluate` на Ubuntu AI Server и проверить live-запросами |
| 🔜 | **Advanced AI scoring** | семантика вместо грубого текстового поиска, калибровка по Agreement Rate |

### Категорически НЕ реализуется до Stage 8

Любые **автоматические Telegram-действия за пределами Stage 7**: полный AUTO-режим со
свайпом и переходом к следующей анкете без явного AI-решения, автоматические сообщения,
Dialog/Message Generator. Stage 7 ограничен реакцией на конкретную анкету (❤️/👎) на
сконфигурированном авто-аккаунте.

---

## Приложение

### Переменные окружения / пути данных

| Путь | Что это | В git? |
|---|---|---|
| `config/config.yaml` | живой конфиг (api_id/hash, phone, proxy) | ❌ (gitignored) |
| `config/preferences.yaml` | персональные правила SKIP/LIKE | ❌ (gitignored) |
| `data/database.db` | основная SQLite-БД | ❌ |
| `data/sessions/*.session` | Telegram-сессии | ❌ |
| `data/logs/runtime.log, analytics.log` | логи | ❌ |
| `data/exports/*.csv` | экспорт рецензий | ❌ |
| `proxy/` | vendored xray-core + реальный VLESS | ❌ (не коммитить) |

### Ключевые константы

- Дайвинчик (Leo) chat_id по умолчанию: **`1234060895`**
- Пороги решений: LIKE `0.75`, REVIEW `0.50`, min_confidence `0.60`
- Веса Decision Engine: llm `0.7` / clip `0.3`
- Версия баннера (`banner.py`): **`0.7`**

### Лицензия

Проект внутренний/личный. Прокси-настройки и API-ключи являются секретами и не
коммитятся в репозиторий.

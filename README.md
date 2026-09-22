# DvAI — Система автоматизации знакомств в Telegram

> **D**ayvinchik **AI** — коллектор + детерминированный скоринг + Human Review + авто-действия для сервиса знакомств «Дайвинчик» (Telegram).
> Текущий этап: **v0.7 / Stage 8 (SEMI_AUTO) + Stage 8.5** — детерминированный скоринг + аналитика лайков; на анкеты с авто-аккаунта отправляются `❤️`/`👎` (и цепочка «Берем)» для информативных), на AI-REVIEW бот ждёт ручного решения владельца. Полный AUTO не реализован.

---

## 1. Features

- **RAW-first коллектор** (`collectors/dvinchik_collector.py`): каждое сообщение Telegram сохраняется в SQLite **до любого разбора**. Потеря RAW считается ошибкой. Фоновая обработка (`RawQueue`/`RawWorker`) не блокирует приём событий; после рестарта необработанные RAW доставляются повторно (at-least-once).
- **Классификация и парсинг анкет** (`dvinchik_parser.py`): PROFILE / MEDIA_ONLY / MATCH / SERVICE / UNKNOWN; выделение имени/возраста/города/описания; нормализация городов (`city_normalizer.py`); дедупликация по fingerprint (in-memory + `UNIQUE` в БД).
- **Multi-account**: несколько Telegram-аккаунтов в `telegram.accounts`, общий pipeline, одно сообщение обрабатывается один раз.
- **Фильтрация** (`services/filter_engine.py`, `services/filter_service.py`): возраст/город/полнота данных → PASS / REJECT / REVIEW; правила из `config.yaml` (`filters:`), история в `filter_results`.
- **Детерминированный скоринг (Stage 8)**: `Profile.text` → `profile_normalizer` → `feature_extractor` (правила H01–H09 / P01–P04; персональная калибровка в `config/preferences.yaml`) → `score_engine` → `decision_service` → **LIKE / REVIEW / DISLIKE**. Без LLM/CLIP/сети: один и тот же текст всегда даёт один и тот же результат. `DecisionService.evaluate()` считается для **всех** результатов фильтра. Хард-негатив H01 («Не ищет отношения») срабатывает только на однозначные формулировки («ищу друга», «не ищу отношения», «просто ищу общение») — «можно пообщаться/погулять» это обычная открытость, а не отказ от отношений. Решение на основе **информативности** (`informative + clean → LIKE`): достаточно значимых слов (≥10) ИЛИ найден позитивный признак; черновые/короткие, но чистые анкеты → REVIEW. `like_threshold`/`review_threshold` остались в конфиге, но для решения инертны.
- **Слой предпочтений (SKIP/LIKE)** (`app/preferences.py`): персональные правила в `config/preferences.yaml` (gitignored). SKIP → жёсткий DISLIKE; LIKE-фактор поднимает DISLIKE → REVIEW (анкета не теряется). Инвариант `NO_HARD_NEGATIVE_MUST_NOT_BECOME_DISLIKE`: missing/unknown информация → REVIEW, никогда DISLIKE.
- **Human Review (Stage 6)**: очередь профилей с AI-решением, ручная оценка APPROVE / REJECT / SKIP, метрика **AI/Human Agreement Rate** = AGREEMENT/(AGREEMENT+DISAGREEMENT) (SKIP исключён; `null` при нулевом знаменателе). Telegram-UI (`telegram/review_bot.py`): `/review`, `/profile`, `/stats`, `/ai_stats`, `/disagreements`. CSV-экспорт: `python main.py --export-review`.
- **Авто-действия (Stage 7, SEMI_AUTO)** (`collectors/auto_action.py`): Decision != Action (§30): `DecisionService` возвращает только LIKE/REVIEW/DISLIKE, а что слать в Telegram решает `ActionPolicyResolver` (`models/action.py`). LIKE → `❤️` (LIKE_ONLY) или цепочку «Берем)» ❤️→💌→«Берем)» (LIKE_AND_MESSAGE, только для информативных анкет); DISLIKE → `👎`; rate-limit `interval_sec`; идемпотентность **по карточке + виду действия** (`chat_id`+`telegram_message_id`+`action` — LIKE и MESSAGE независимы); фильтровые не-PASS тоже получают `👎` (лента Leo не замирает); автопродолжение ленты кнопкой «🚀 Смотреть анкеты». **Память капч (Stage 7.6)**: капчи/проверки Leo (сделки/подписки/подтверждения/гео-запросы — маркеры `CAPTCHA_MARKERS` + ≥2 reply-кнопки) — `_handle_captcha`: выученный ответ (`captcha_memory` по сигнатуре `captcha_signature(text)`) отправляется автоматически; **неизвестная капча выводится на сайт `/captchas` и наугад не нажимается** (ждёт ответа владельца; опционально `fallback_press_last` = старое поведение с последней обычной кнопкой; `captcha.enabled=false` — вообще без памяти). **Уведомления владельцу не дублируются**: реакция шлётся на каждую карточку, но пересылка владельцу — только для первого авто-действия профиля (`db.has_auto_action(profile_id)`), иначе повтор анкеты заваливал бы его историей. Гейт: режим `project.mode ∈ {SEMI_AUTO, AUTO}` + `auto_actions.enabled` + найден клиент по `account_session`. OBSERVE → действий нет.
- **Manual Review (Stage 8)** (`services/manual_review.py`): когда скоринг выдаёт REVIEW, бот **не действует сам** — пересылает карточку владельцу и ждёт его ручного решения. Исходящее `❤️`/`👎` владельца перехватывается и записывается в файл `data/reviews/review_log.json`/`.md` (только для активных REVIEW-анкет).
- **Control Panel (Stage 7.5)** (`telegram/control_bot.py`): `/status /mode on|off /stream /recent /msgs /top /help` (+ inline-кнопки) только от `control.allowed_user_ids`; слушает **все** `telegram.accounts`; режим меняется на лету и персистится в `config.yaml`.
- **Аналитика лайков (Stage 8.5)**: собирается «что я написал при лайке и ответила ли девушка». Авто-текст «Берем)» — в `auto_actions_log.message_text`, ручные действия владельца («❤️»→LIKE, «👎»→DISLIKE, текст→MESSAGE) — в `sent_messages` (`_handle_outgoing_message`); сигнал ответа — MATCH «Начинай общаться 👉 Имя» → `match_responses` (привязка к профилю по имени, `UNIQUE(chat_id, tm_id)`). **Результат на каждый лайк** (авто/ручной) — таблица `like_outcomes`: содержание сообщения (`message_text`, '' если без текста) и `responded` (0/1 — лайкнула в ответ). Отклонённые из-за лимита Leo лайки («Слишком много ❤️ за сегодня», детектор `is_like_rejection`) помечаются `rejected=1` и исключаются из аналитики (`mark_like_rejected`). Отчёты: `/msgs` — конверсия текстов (`sent`/`responded`/`rate`), `/top` — топ девушек по лайкам (+ ответила ли, последнее сообщение).
- **Web UI (Stage 9)** (`web/`): веб-интерфейс поверх существующей системы — Flask + Jinja2 + HTML/CSS/JS, запускается в daemon-потоке `main.py`. НЕ содержит бизнес-логики (только читает существующую SQLite через `web/db.py` SyncDB и передаёт команды ручного ревью в `human_decisions`). Параллельно с Telegram ControlBot (бот не удаляется): `/login` (селф-пароль + CSRF), `/dashboard` (вертикальная лента карточек анкет со всеми фото, решением и причинами + переключатель режима OBSERVE/SEMI_AUTO/AUTO прямо на ленте — POST на `/settings/mode`), `/profiles/<id>` (детальная страница), `/settings` (фильтры, preferences, режим OBSERVE/SEMI_AUTO/AUTO из `config.yaml`), `/captchas` (память капч Stage 7.6: неизвестные капчи Leo ждут ответа владельца — POST-ответ сохраняется в `captcha_memory` и сразу отправляется Leo через `web/actions.py` → `send_captcha_answer_sync`, чтобы разблокировать ленту; кнопки-подсказки из reply-маркапа капчи). На ленте — переключатель режима OBSERVE/SEMI_AUTO/AUTO (POST на `/settings/mode`) и **выпадающий список аккаунта-исполнителя авто-действий** (POST на `/settings/account`, меняется на лету) — вместо прежнего фильтра по решениям (роут `?decision=` сохранён, просто убран из UI). Переключатель режима на сайте работает **на лету через `web/actions.py` → `set_mode_sync`**: режим персистится в `config.yaml` И меняется живой `AutoActionEngine` + сразу запускается авто-поток (без рестарта) — см. блок «Web UI». Быстрые действия ❤️/👎 на анкетах в ленте (не только REVIEW) — записывают ручное решение владельца по последней AI-оценке **и реально отправляют реакцию в чат Leo** через авто-аккаунт (`web/actions.py` → `AutoActionEngine.manual_reaction`), чтобы лента Дайвинчика продолжилась. Кнопки показываются ТОЛЬКО пока анкета «не сыграна»: скрываются, когда человек уже решил (APPROVE/REJECT), бот реально лайкнул/дизлайкнул (статус `LIKED`/`DISLIKED`) ИЛИ AI вынес терминальное решение `LIKE`/`DISLIKE` (бейдж-заглушка «👎/❤️ DISLIKE/LIKE (решение AI)» вместо кнопок, даже если реакция ещё не ушла или анкета была с не-авто-аккаунта). `REVIEW`-анкеты кнопки сохраняют — это точка входа ручного ревью. Фото скачиваются по запросу (кэш в `media/<profile_id>/<message_id>.<session>.jpg`, для старых записей — `media/<profile_id>/<message_id>.jpg`) **аккаунтом-получателем** (`web/photos.py` → `download_photo`): message_id в диалоге с Leo уникален для каждого аккаунта, поэтому фото качается тем клиентом, чья сессия записана в `profile_messages.account_session` (регистрируются все клиенты+сессии через `set_telegram_clients_with_sessions` в `main.py`); чужие клиенты — только fallback для записей без сессии, иначе один и тот же id через чужой аккаунт вернул бы фото ДРУГОЙ анкеты. Коллектор фиксирует аккаунт-получателя в `_session_for_client(msg.client)` для каждой привязки PROFILE/MEDIA_ONLY. Лента авто-обновляется без F5: `/dashboard/new-count` возвращает сигнатуру, учитывающую `total/pending`, `MAX(last_seen_at)` и `MAX(raw_messages.id)` (ловит и новые анкеты, и повторы); при изменении сигнатуры фрагменты HTML тянутся через `/dashboard/feed` и подменяются в DOM на лету (`static/js/app.js`, опрос каждые ~8с, пропуск при активном действии). Числа статистики обновляются атомарно по `data-stat` атрибутам. Пароль/логин: env-переменные `DVAI_WEB_PASSWORD_HASH`/`DVAI_WEB_SECRET`, дефолтный адрес `0.0.0.0:5000`. Без WebSocket/SSE/React, live-лента не планируется, новый AUTO не реализуется.
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
        профиль → profile_normalizer → feature_extractor
        (H01–H09/P01–P04 из preferences.yaml) → score_engine → решение
        (informative+clean → LIKE; короткая чистая → REVIEW; хард-негатив → DISLIKE)
   → ActionPolicyResolver(decision, informative) → LIKE_ONLY / LIKE_AND_MESSAGE / DISLIKE_ONLY
   → AutoActionEngine.maybe_act(policy)  # только SEMI_AUTO/AUTO (❤️/👎/«Берем)»/REVIEW-уведомление)
   → ReviewBot                            # человеческая рецензия (APPROVE/REJECT/SKIP)
```

### Компоненты

| Зона | Процесс | Компоненты | Контакт с Telegram |
|---|---|---|---|
| **Collector** | сбор и сохранение | `dvinchik_collector`, `raw_worker`, `raw_queue`, `dedup`, `city_normalizer`, `stats`, `auto_action` | ✅ Telethon |
| **Scoring (детерминированный)** | решение | `filter_engine`, `filter_service`, `profile_normalizer`, `feature_extractor`, `score_engine`, `decision_service`, `app/preferences` | ❌ Telegram-free |
| **Review + Analytics** | ручная рецензия и аналитика | `review_service`, `analytics_service`, `review_export`, `manual_review` | ❌ Telegram-free |
| **Telegram UI** | вывод и управление | `review_bot`, `control_bot` | ✅ Telethon |
| **Web UI (Stage 9)** | веб-интерфейс | `web/` (Flask, SyncDB, фото-кэш), `static/`, шаблоны | ⚠️ только фото по запросу |

> Единственные слои с Telethon: `collectors/dvinchik_collector.py`, `collectors/auto_action.py`, `telegram/`. Всё остальное — чистая логика (Profile/str/Config), тестируется без живого Telegram.

### Структура проекта

```
dvdatin/
├── main.py                      # Точка входа: конфиг, сборка стека, цикл, --export-review/--export-analysis/--clear-db
├── run.bat                      # Запуск на Windows (chcp 65001, UTF-8)
├── requirements.txt / requirements-dev.txt
├── AGENTS.md / Roadmap.md / README.md
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
├── web/                         # Stage 9 Web UI: Flask (create_app, blueprints, SyncDB, photos)
├── static/                      # CSS/JS для Web UI
├── tests/                       # 607 тестов (17 файлов), baseline в tests/baseline/
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
| `profile_messages` | Связь профиль ↔ сообщения (в т.ч. MEDIA_ONLY); `account_session` — аккаунт-получатель (для скачивания фото тем клиентом, кто реально видел сообщение) |
| `chat_context` | Контекст «последняя анкета чата» |
| `filter_results` | История фильтрации (PASS/REJECT/REVIEW) |
| `ai_decisions` | Решения DecisionService (LIKE/REVIEW/DISLIKE; `scoring_version=deterministic-v2`) |
| `auto_actions_log` | Отправленные действия per-card + kind (`chat_id`+`telegram_message_id`+`action`): LIKE/DISLIKE/MESSAGE; `message_text` — текст сообщения при лайке («Берем)») |
| `match_responses` | Взаимные лайки (Stage 8.5): девушка ответила — привязка к `profile_id` по имени; `UNIQUE(chat_id, telegram_message_id)` |
| `sent_messages` | Ручные действия владельца (Stage 8.5, `source='manual'`): `action` LIKE («❤️» — сам лайкнул) / DISLIKE («👎») / MESSAGE (текст) |
| `like_outcomes` | Результат на КАЖДЫЙ лайк (Stage 8.5): `message_text` (содержание сообщения, '' если без текста) + `responded` (0/1 — лайкнула в ответ); бэкфилл из `auto_actions_log`/`sent_messages`; `UNIQUE(source, chat_id, like_tm_id)` |
| `captcha_memory` | Память капч (Stage 7.6): текст капчи Leo по сигнатуре (нормализованный текст) с выученным ответом владельца (`answer`); `NULL` = ждёт ответа на сайте; `used_count` — сколько раз ответ применялся |
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
  like:
    enabled: true             # ❤️ (LIKE_ONLY)
  like_message:
    enabled: false            # ❤️ + «Берем)» (LIKE_AND_MESSAGE)
    text: "Берем)"
  captcha:                    # память капч (Stage 7.6)
    enabled: true             # выученные ответы → авто-ответ; неизвестные → /captchas
    auto_answer: true         # отвечать на известные капчи автоматически
    fallback_press_last: false  # true = на неизвестную капчу жать последнюю кнопку

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
python -m pytest tests/ -v     # 631 тест
```

### Экспорт анализа и очистка БД (Stage 11)

CLI-утилиты, работающие **без Telegram-авторизации** (только БД + конфиг):

```bash
python main.py --export-analysis   # CSV всех анкет для анализа
python main.py --clear-db          # бэкап + полная очистка БД
```

**`--export-analysis`** → `data/exports/analysis_<дата>.csv`. Одна строка на анкету:
`profile_id`, `name`, `age`, `city`, `description`, `action`, `ai_decision`,
`ai_score`, `ai_confidence`, `filter_decision`, `human_decision`,
`human_agreement`, `auto_liked`, `auto_disliked`, `auto_message_text`,
`auto_first_action`, `auto_first_action_at`, `manual_liked`, `manual_disliked`,
`manual_action`, `manual_text`, `manual_at`, `matched`, `responded`, `status`,
`first_seen_at`, `last_seen_at`, `action_reasons`.

Категория `action` (кого лайкнул / проскипал / дизлайкнул и т.п.):
- **liked** — лайк отправлен (авто ❤️ или ручной) 
- **disliked** — дизлайк (авто 👎, ручной, либо human REJECT)
- **skipped** — SKIP через ручное решение (преференсы)
- **approved** — APPROVE через ручное решение
- **reviewed** — AI вынес LIKE/DISLIKE, но реакция не отправлена
- **pending** — AI REVIEW, ожидает решения
- **seen** — без AI-решения

`matched` — был ли взаимный лайк («Начинай общаться»), `responded` — ответила ли
девушка на лайк (`like_outcomes.responded`), `ai_score`/`ai_confidence` — из
последнего `ai_decisions`, `action_reasons` — причины решения (из `reasons`).

**`--clear-db`** — создаёт бэкап `data/exports/backups/database_<дата>.db`
(копия фактического файла БД) и полностью очищает все таблицы
(`profiles`, `ai_decisions`, `filter_results`, `human_decisions`,
`auto_actions_log`, `sent_messages`, `match_responses`, `like_outcomes`,
`profile_messages`, `chat_context`, `raw_messages`). Схема остаётся
(таблицы снова создаются при старте). Очистка необратима — только с бэкапом.
```

### Web UI (Stage 9)

Запускается автоматически внутри `main.py` в daemon-потоке Flask (порт по умолчанию `5000`). Настройка — env-переменные (без реальных секретов в конфиге):

```bash
export DVAI_WEB_SECRET="случайный-секрет"        # secret_key сессий
export DVAI_WEB_PASSWORD_HASH="<werkzeug hash>"  # пароль для /login
export DVAI_WEB_PORT="5000"                      # порт (по умолчанию 5000)
export DVAI_WEB_COOKIE_SECURE="0"                # 1 — только за HTTPS-реверс-прокси
```

URL-ы: `/login` → `/dashboard` (лента анкет) → `/profiles/<id>` (детали) → `/settings` (фильтры/режим). Подробнее в `web/` и `AGENTS.md`.

**Дизайн**: светлая чистая тема (статика без внешних зависимостей — `static/css/style.css` + `static/js/app.js`). Секции по решениям окрашены в LIKE/REVIEW/DISLIKE, статусы и решения — pill-бейджи, быстрые действия LIKE/DISLIKE — SVG-иконки, уведомления о результате — toast-snackbar (без перезагрузки страницы). Навигация/лого/favicon — inline SVG (data-URI), responsive-адаптация под мобильные (640px).

**Режим на сайте меняется на лету** (`web/actions.py` → `set_mode_sync`): POST на `/settings/mode` не только пишет `project.mode` в `config.yaml`, но и вызывает `collector.set_mode()` — живой `AutoActionEngine` переключается сразу, а для SEMI_AUTO/AUTO запускается `start_auto_stream()` (обработка активной анкеты / продолжение ленты Leo). Перезапуск не нужен. Без привязанного коллектора (например в тестах) режим просто сохраняется в YAML.

**Аккаунт-исполнитель меняется на лету** (`web/actions.py` → `set_account_sync`): POST на `/settings/account` пишет `auto_actions.account_session` в `config.yaml` И вызывает `collector.switch_auto_account(session)` — живой `AutoActionEngine` получает клиент другого аккаунта (`AutoActionEngine.swap_client`), кэш peer сбрасывается (у каждого аккаунта свой access_hash чата Leo), rate-limiter обнуляется, затем запускается `start_auto_stream()` на новом аккаунте. UI: выпадающий список на **ленте** (главная) и в `/settings` (аккаунты читаются из `telegram.accounts` конфига); на ленте переключение происходит сразу при выборе (авто-сабмит формы).

### Ключевые константы

- Дайвинчик (Leo) chat_id по умолчанию: **`1234060895`**
- Пороги решения: LIKE **0.75**, REVIEW **0.50**
- Версия скоринга: **`deterministic-v2`**
- Версия баннера (`app/banner.py`): **0.7**
- Авто-интервал: `interval_sec` (default **10 s**)

---

## 3. Compatibility / Deprecations

### Удалено (упрощение кодовой базы)

- **Весь LLM/CLIP-стек**: `services/llm_service.py`, `services/clip_service.py`, `services/ai_scoring_service.py`, `services/remote_llm_client.py`, `services/remote_clip_client.py`, `models/ai.py`, `collectors/media_analyzer.py`, `collectors/anti_block.py`, `tests/e2e_ai.py`, `tests/test_ai_scoring.py`. Ubuntu AI Server больше не используется. Актуальный runbook — `deploy/DEPLOY.md` (сервер `144.31.118.3:2200`, ssh-ключ `homekey`). `deploy/README.md` и `deploy/dvai.service` — справочные артефакты старой инфраструктуры.
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
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
- **Правило «мало инфы»** (`config/preferences.yaml`, секция `low_info`): по умолчанию `action: review` — пустая анкета ждёт ручного решения владельца. Если задано `action: skip` — неинформативная анкета (мало значимых слов + нет признаков) даёт **DISLIKE** (авто-👎, лента Leo не замирает), причина `LOW_INFO_SKIP:words=<N>`. Это единственное место, где «мало информации» становится отрицательным решением (иначе действует инвариант «без hard-negative не DISLIKE»). На информативные анкеты правило не влияет (они по-прежнему → LIKE).
- **Слой предпочтений (SKIP/LIKE)** (`app/preferences.py`): персональные правила в `config/preferences.yaml` (gitignored). SKIP → жёсткий DISLIKE; LIKE-фактор поднимает DISLIKE → REVIEW (анкета не теряется). Инвариант `NO_HARD_NEGATIVE_MUST_NOT_BECOME_DISLIKE`: missing/unknown информация → REVIEW, никогда DISLIKE.
- **Human Review (Stage 6)**: очередь профилей с AI-решением, ручная оценка APPROVE / REJECT / SKIP, метрика **AI/Human Agreement Rate** = AGREEMENT/(AGREEMENT+DISAGREEMENT) (SKIP исключён; `null` при нулевом знаменателе). Telegram-UI (`telegram/review_bot.py`): `/review`, `/profile`, `/stats`, `/ai_stats`, `/disagreements`. CSV-экспорт: `python main.py --export-review`.
- **Авто-действия (Stage 7, SEMI_AUTO)** (`collectors/auto_action.py`): Decision != Action (§30): `DecisionService` возвращает только LIKE/REVIEW/DISLIKE, а что слать в Telegram решает `ActionPolicyResolver` (`models/action.py`). LIKE → `❤️` (LIKE_ONLY) или цепочку «Берем)» ❤️→💌→«Берем)» (LIKE_AND_MESSAGE, только для информативных анкет); DISLIKE → `👎`; rate-limit `interval_sec` (по умолчанию **2 с** — Leo принимает реакцию только пока карточка на экране); **гейт «реакция в свою карточку»** (`note_card`/`is_stale_card` → `"STALE"`): карточка помечается в хендлере по порядку прихода, и устаревшая карточка **не** получает реакцию вовсе (при `interval_sec = 10 с` промехали 54 из 60 действий); идемпотентность **по карточке + виду действия** (`chat_id`+`telegram_message_id`+`action` — LIKE и MESSAGE независимы); фильтровые не-PASS тоже получают `👎` (лента Leo не замирает); автопродолжение ленты **Карточка взаимного лайка** («Ты понравился N девушке, показать её?», кнопки [«1. Показать.», «2. Не хочу больше никого смотреть.»]): Leo ждёт выбора и лента ВСТАЁТ, пока владелец не нажмёт «Показать» руками — авто-аккаунт жмёт её сам и идемпотентно (`_press_match_show_if_needed()`, отказную кнопку никогда); кнопкой «🚀 Смотреть анкеты»; **Premium-промо после реакции** (Stage 7.7): после ❤️/👎 Leo присылает рекламу [⭐️Активировать, Пока без Premium] — бот идемпотентно нажимает отказную кнопку («Активировать» никогда), чтобы лента не встала. **Главное меню Leo (Stage 7.8)**: при исчерпании ленты бот шлёт меню «1. Смотреть анкеты. / 2. Моя анкета. …» с reply-клавиатурой `["1 🚀", "2", "3", "4"]` — кнопки-промо «🚀 Смотреть анкеты» там НЕТ, и лента вставала намертво (прод 2026-10-01, `msg=25309`, авто-аккаунт `dvai_2`); `_press_menu_view_if_needed()` жмёт «1 🚀» — только если самая свежая карточка с кнопками это меню, остальные кнопки («Моя анкета», «не искать», Premium) не жмём никогда. **Кнопки карточки лайка бывают двух видов** — [«1. Показать.», «2. Не хочу…»] и [«1 👍», «2 💤»]: `_is_match_show_button` матчит «показать» **и по 👍**, отказную отсекает по «не хочу»/💤 (без этого лента вставала и на карточке взаимного лайка). **Память капч (Stage 7.6)**: капчи/проверки Leo (сделки/подписки/подтверждения/гео-запросы — маркеры `CAPTCHA_MARKERS` + ≥2 reply-кнопки) — `_handle_captcha`: выученный ответ (`captcha_memory` по сигнатуре `captcha_signature(text)`) отправляется автоматически; **неизвестная капча выводится в блоке «⚠️ Капчи ждут ответ» над лентой главного экрана и наугад не нажимается** (ждёт ответа владельца; опционально `fallback_press_last` = старое поведение с последней обычной кнопкой; `captcha.enabled=false` — вообще без памяти). **Уведомления владельцу не дублируются**: реакция шлётся на каждую карточку, но пересылка владельцу — только для первого авто-действия профиля (`db.has_auto_action(profile_id)`), иначе повтор анкеты заваливал бы его историей. Гейт: режим `project.mode ∈ {SEMI_AUTO, AUTO}` + `auto_actions.enabled` + найден клиент по `account_session`. OBSERVE → действий нет.
- **Manual Review (Stage 8)** (`services/manual_review.py`): когда скоринг выдаёт REVIEW, бот **не действует сам** — пересылает карточку владельцу и ждёт его ручного решения. Исходящее `❤️`/`👎` владельца перехватывается и записывается в файл `data/reviews/review_log.json`/`.md` (только для активных REVIEW-анкет).
- **Control Panel (Stage 7.5)** (`telegram/control_bot.py`): `/status /mode on|off /stream /recent /msgs /top /help` (+ inline-кнопки) только от `control.allowed_user_ids`; слушает **все** `telegram.accounts`; режим меняется на лету и персистится в `config.yaml`.
- **Аналитика лайков (Stage 8.5)**: собирается «что я написал при лайке и ответила ли девушка». Авто-текст «Берем)» — в `auto_actions_log.message_text`, ручные действия владельца («❤️»→LIKE, «👎»→DISLIKE, текст→MESSAGE) — в `sent_messages` (`_handle_outgoing_message`); сигнал ответа — MATCH «Начинай общаться 👉 Имя» → `match_responses` (привязка к профилю по имени, `UNIQUE(chat_id, tm_id)`). **Результат на каждый лайк** (авто/ручной) — таблица `like_outcomes`: содержание сообщения (`message_text`, '' если без текста) и `responded` (0/1 — лайкнула в ответ). Отклонённые из-за лимита Leo лайки («Слишком много ❤️ за сегодня», детектор `is_like_rejection`) помечаются `rejected=1` и исключаются из аналитики (`mark_like_rejected`). Отчёты: `/msgs` — конверсия текстов (`sent`/`responded`/`rate`), `/top` — топ девушек по лайкам (+ ответила ли, последнее сообщение).
- **Web UI (Stage 9)** (`web/`): **один экран без вкладок** — Flask + Jinja2 + HTML/CSS/JS в daemon-потоке `main.py`. Бизнес-логики нет: читает существующую SQLite через `web/db.py` SyncDB и записывает ручные решения в `human_decisions`. Главная страница — `/` (синоним `/chat`): лента чата Leo (входящие пузыри слева, наши ❤️/👎/«Берем)» — справа), анкеты с фото/именем/городом inline; сверху — переключатель режима OBSERVE/SEMI_AUTO/AUTO (переключается на лету), кнопка ⚙ настройки — drawer режима, аккоунта-исполнителя, фильтры и `SKIP/LIKE`-предпочтения. Бтонки ❤️/👎 — **только для анкет с AI-решением `REVIEW`** (и пока владелец не решил): POST `/chat/profile/<id>/<LIKE|DISLIKE>` — записывает ручное решение (принадлец консервация не перезаписывается) и реально отправляет реакцию в чат Leo через авто-аккаунт (`web/actions.py` → `AutoActionEngine.manual_reaction`). Блок «⚠️ Капчи ждут ответ» над лентой: неизвестные капчи Leo — ответ владельца сохраняется в `captcha_memory` и сразу отправляется Leo (`send_captcha_answer_sync`), разблокировая ленту. Автоообножения без перезаписивов: `/chat`, `/dashboard`, `/settings`, `/profiles/<id>` — 302 на `/`; legacy API роут `/dashboard/action/<id>/<action>` сохранён. Переключение режима и аккоунта работают **на лету** через `web/actions.py` (`set_mode_sync` / `set_account_sync`): режим персистится в `config.yaml` и сразу меняет живой `AutoActionEngine` (без рестарта) — см. блок «Web UI».
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
   → AutoActionEngine.step_pending(text)  # цепочка «Берем)»: 💌 📹 🎤 → «Отправь текст…» → «Берем)»
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
├── tests/                       # 722 тестов (20 файлов), baseline в tests/baseline/
├── deploy/                      # systemd unit + runbook
├── proxy/                       # vendored xray-core + VLESS (НЕ коммитить)
└── data/                        # БД, сессии, логи, экспорт (gitignored)
```

### База данных (SQLite, WAL)

Схема идемпотентна (`CREATE TABLE IF NOT EXISTS`), внешние ключи через PRAGMA.

| Таблица | Назначение |
|---|---|
| `raw_messages` | Сырые сообщения чата (append-only; включает `raw_entities`, `reply_markup`, `is_outgoing`) |
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
  interval_sec: 2.0           # rate-limit между действиями (держим МАЛЫМ, см. ниже)
  notify_chat_id: 0           # уведомления владельцу (0 = выкл)
  like:
    enabled: true             # ❤️ (LIKE_ONLY)
  like_message:
    enabled: false            # ❤️ + «Берем)» (LIKE_AND_MESSAGE)
    text: "Берем)"            # или пул: ["Берем)", "беру)"] — рандом при лайке
  captcha:                    # память капч (Stage 7.6)
    enabled: true             # выученные ответы → авто-ответ; неизвестные → блок капч на главной экран
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

#### Цепочка «Берем)» (LIKE_AND_MESSAGE)

Для **информативных** анкет с решением LIKE бот шлёт не только сердечко, а полную цепочку:

```text
❤️  →  «💌 📹 🎤»  →  (Leo: «Отправь текст, видео или голосовое(до 15сек).»)  →  «Берем)» / «беру)»
```

- `💌 📹 🎤` отправляется **сразу после `❤️`** (`collectors/auto_action.py`, ветка `LIKE_AND_MESSAGE`). Раньше цепочка ждала подтверждение Leo «Лайк отправлен, ждем ответа.», но на аккаунте `dvai_2` Leo его больше не присылает — после `❤️` он сразу шлёт следующую карточку, поэтому цепочка стояла только на сердечке. Ack оставлен как ретрай: если проактивная отправка кнопки не удалась, стадия остаётся `AWAIT_LIKE_ACK` и кнопка отправляется по ack.
- Строка кнопок отправляется целиком: одиночный `💌` Leo не распознаёт (проверено по прод-логам, tm=722860/722895).
- Стадии: `AWAIT_LIKE_ACK` → `AWAIT_PROMPT` → цепочка завершена и удаляется из `_pending_chains`. Продвигает `AutoActionEngine.step_pending(text, chat_id)`, который коллектор зовёт на входящих НЕ-`PROFILE` сообщениях Leo на авто-аккаунте.
- Маркеры композера (`MESSAGE_PROMPT_MARKERS`, сравнение регистронезависимое): `отправь текст`, `отправьте текст`, `видео или голосовое`.
- Финальный текст берётся рандомно из `auto_actions.like_message.text`, записывается в `auto_actions_log` как действие `MESSAGE` с `message_text` и попадает в аналитику `like_outcomes`.
- Rate-limit `interval_sec` сам разносит `❤️` и `💌 📹 🎤` по времени; строка `💌 📹 🎤` входит в `MANUAL_MESSAGE_EXCLUDE`, поэтому авто-кнопка не попадает в `sent_messages` как ручное действие.
- **Текст в композер уходит вне общего rate-limit** (`_send_composer_text`, минимальный интервал `_COMPOSER_SEND_GAP_SEC` = 1.5 с): Leo ждёт текст недолго, и прежний общий `interval_sec` (10 с) успевал вставить между лайком и сообщением реакцию «👎» на следующую карточку — Leo отвечал «Сообщение слишком короткое. Введите сообщение заново».
- **Реакция уходит ТОЛЬКО в свою карточку** (гейт `AutoActionEngine.is_stale_card` + `note_card`): Leo держит **одну** активную карточку и принимает `❤️`/`👎` только пока она на экране, а ленту листает **сам** (~каждые 2–10 с). При прежнем `interval_sec = 10 с` реакция уезжала на 1–3 карточки вперёд — прод-замер: **54 промаха из 60 действий** (лайк/дизлайк уезжали на чужую анкету). Теперь коллектор помечает показанную карточку прямо в Telegram-хендлере (`_handle_new_message`, сразу после RAW-save, до worker'а — чтобы отставание pipeline было видно), а `maybe_act` для устаревшей карточки возвращает `"STALE"` и **ничего не отправляет** (в `auto_actions_log` тоже не пишется). Актуальная карточка реакцию получает как обычно. Точность страхует малый `interval_sec` (по умолчанию **2 с**).
- **Пока композер открыт, реакция на новую карточку откладывается** (`_defer_reaction` → `_flush_deferred`, `maybe_act` возвращает `"DEFERRED"`). Так «👎»/«❤️» не попадают в композер вместо текста. Leo показывает одну активную карточку, поэтому в слоте хранится последняя. Отсрочка сбрасывается, когда цепочка закрылась (шаг 3/3), либо по таймауту `_COMPOSER_WAIT_SEC` (20 с) — если Leo не ответил на кнопку, цепочка снимается (`_drop_stale_chains`) и лента идёт дальше. Отложенное действие записывается в `auto_actions_log` самим движком (collector при `"DEFERRED"` ничего не пишет); если к моменту сброса карточка уже устарела, отложенная реакция **отбрасывается** тем же гейтом (`"STALE"`) — отправлять её было бы промахом по чужой анкете.

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
python -m pytest tests/ -v     # 722 тестов
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

URL-ы: `/login` → **`/`** — единственный экран приложения (`web/blueprints/chat.py`, шаблон `web/templates/app.html`). Отдельных вкладок и страниц больше нет:

- **лента — плоский лог-список**: контейнер `.app` (`max-width: 860px; margin: 0 auto`) → `.feed` (`display: flex; flex-direction: column; align-items: stretch; gap: 8px`) → строки `.row` (`display: grid; grid-template-columns: 46px minmax(0, 1fr); width: 100%`). Каждое событие — одна строка одинаковой ширины: колонка времени + содержимое (имя/возраст/город, описание, фото, кнопки). Чат-вёрстки с пузырями больше нет — **никаких** выравниваний по краям, подгонки под содержимое и абсолютного позиционирования (из-за них короткие события вроде «👎 DISLIKE» вырождались в узкие плашки по краям с пустым центром);
- **панель управления лентой**: фильтр по решению (Все / ❤️ / 🤔 / 👎) и поиск по имени-городу работают на клиенте по `data-decision` / `data-name` строк, поэтому перерисовка ленты polling-ом не сбрасывает выбор; счётчик показывает «N из M»;
- **выбор режима** в топбаре — `OBSERVE` / `SEMI_AUTO` / `AUTO`, текущий подсвечен, POST на `/settings/mode` (переключение на лету);
- **кнопки ❤️/👎** у анкеты — **только если AI-решение `REVIEW`** и человек ещё не решил; POST на `/chat/profile/<id>/<LIKE|DISLIKE>` (CSRF + серверный гейт по `get_latest_ai_decision`, `is_already_reviewed` → `save_human_decision` + реакция в чат Leo);
- **аккаунт-исполнитель** — селект в топбаре рядом с режимом (`#account-form`, авто-сабмит при выборе, кнопка `ОК` как fallback без JS); в drawer настроек его больше нет;
- **переключатель темы** — кнопка в топбаре (тёмная ⇄ светлая), выбор в `localStorage`, применяется **до первой отрисовки** (`web/templates/partials/theme_boot.html`), работает и на `/login`;
- **⚙ настройки** — одна кнопка открывает drawer поверх ленты (только фильтры и `SKIP/LIKE`-предпочтения);
- **блок «⚠️ Капчи ждут ответ»** над лентой (`captcha_memory` с `answer IS NULL`) — виден всегда, независимо от пагинации: ответ (с подсказками из кнопок Leo, POST `/chat/captcha/<id>/answer`) и «забыть» (POST `/chat/captcha/<id>/delete`).

Лента обновляется без перезагрузки: polling `/chat/new-count` (сигнатура raw∪sent∪auto∪captcha) → подмена фрагмента `/chat/feed`. Всегда открывается на **свежих** сообщениях (пагинация «s конца»: `page=1` — новые, `page=2` — более ранние, «Показать более ранние» вставляет их сверху, `FEED_PER_PAGE = 40`).


### Лента = одно сообщение Telegram = одна строка

Коллектор пишет в `raw_messages` **весь** чат — и входящие от Leo, и наши исходящие (RAW-first). Поэтому строки ленты строятся как **хребет + аннотации**, а не как склейка трёх таблиц:

- **ключ строки** — `(chat_id, telegram_message_id, направление)`. Направление в ключе обязательно: у каждого аккаунта Telegram **свой диапазон message id** в одном чате, поэтому номер нашего исходящего совпадает с номером входящего от Leo. На реальной базе диапазоны пересекаются полностью (входящие 22757..723077, исходящие 22871..723078);
- **`raw_messages` — хребет**; `sent_messages` и `auto_actions_log` — **аннотации** исходящих, приклеиваются по ключу, приоритет авто > ручная (у авто есть `decision` и `message_text`);
- **`raw_messages.is_outgoing`** — новая колонка (`INTEGER DEFAULT 0`), ставится перехватчиком исходящих в `DvinchikCollector._handle_outgoing_message`; миграция `_ensure_raw_is_outgoing_column` + backfill `WHERE sender_id <> chat_id`. Без неё наш собственный `👎` рисовался в ленте как входящее сообщение от Leo;
- **пустые сообщения не показываются** — фото Leo без текста (`MEDIA_ONLY`) и кнопки меню; фото анкеты уже есть внутри карточки, иначе анкета рисовалась в ленте дважды;
- **пагинация — в SQL** (`SyncDB._feed_keys`): объединение ветвей группируется по ключу и режется `ORDER BY ts ... LIMIT/OFFSET`. Постранично брать «последние N» из каждой таблицы нельзя — окна не совпадают, и на соседних страницах сообщения повторялись (на реальной базе 578 повторов). Порядок — по времени (`message_date`/`sent_at`), а не по id: ветви лежат в разных пространствах id;
- **AI-бейдж скрывается, если действие уже показано отдельной строкой** (`profile.acted`). Признак — `profile_id` в аннотации, а при `profile_id IS NULL` (на реальной базе 165 из 597) — **время**: реакция пришла в течение `ACTION_TS_WINDOW_SEC` после карточки. Сравнение по времени, а не по номерам сообщений;

**Важное следствие уникального индекса**: у `raw_messages` есть `UNIQUE(chat_id, telegram_message_id)` (`idx_raw_unique`), а пространства id входящих и исходящих пересекаются. Поэтому `INSERT OR IGNORE` **молча отбрасывал** наши исходящие сообщения, номер которых совпал с входящим — на реальной базе так потеряны сырые записи всех `153` авто-действий. Лента это переживает (действие приходит из аннотации и рисуется отдельной строкой), но знать об этом нужно: думать, что в `raw_messages` лежит весь чат, нельзя.

Регресс-тесты: `TestChatFeedDedup` — одна строка на сообщение, исходящее не выглядит как сообщение от Leo, обход всех страниц без дублей, коллизия номера входящего и исходящего, скрытие бейджа при `profile_id IS NULL`, пустой `MEDIA_ONLY` не дублирует карточку, `total` считает сообщения, а не записи в таблицах.

Старые адреса сохранены как **редиректы** на главный экран (чтобы старые закладки не отдавали 404): `/chat` (синоним `/`), `/dashboard`, `/settings`, `/profiles/<id>`, `/profiles/<id>/action`; legacy API-роут быстрых действий `/dashboard/action/<id>/<action>` остался для обратной совместимости. Шаблоны `chat.html` / `dashboard.html` / `settings.html` / `profile.html` / `captchas.html`, blueprint `web/blueprints/captchas.py` и `static/css/style.css` удалены (Stage 9). Подробнее в `web/` и `AGENTS.md`.

**Дизайн** (Stage 9.2, переписан с нуля): **тёмная и светлая темы**, статика без внешних зависимостей — `static/css/app.css` + `static/js/app.js`. Плотный лог-список событий (без «пузырей»), фильтр по решению и поиск, pill-бейджи решений, миниатюры фото, SVG-иконки ⚙/выход, toast-уведомления, drawer настроек.

Тема: палитра задана CSS-переменными (`:root` — тёмная, `[data-theme="light"]` — светлая), переключатель `#theme-toggle` в топбаре пишет выбор в `localStorage`, а `web/templates/partials/theme_boot.html` применяет его инлайн-скриптом **до первой отрисовки** (иначе при перезагрузке мигает тёмная тема). За пределами палитр сырых цветов в `app.css` нет — это проверяет `TestTheme::test_no_raw_colors_outside_palettes`, иначе в светлой теме остались бы тёмные пятна.

Жёсткие правила вёрстки (проверяются тестами и числовым аудитом в headless-браузере, `_audit.py`):

- **топбар — CSS-grid** `minmax(0, 1fr) auto` (левая группа тянется, правая прижата к краю и не может выехать за экран); на узких экранах (<620px) переносится на две строки;
- **лента — одна колонка** `.feed { display: flex; flex-direction: column; align-items: stretch; gap: 8px }` внутри `.app { max-width: 860px; margin: 0 auto }`, строка `.row { display: grid; grid-template-columns: 46px minmax(0, 1fr); width: 100% }` — без выравнивания по краям и подгонки под содержимое; аудит проверяет, что все строки одной ширины и с одним `x`;
- **высота страницы**: реальные 40 событий занимают ~4600px вместо ~22000px в чат-вёрстке — строки плотные (фото 192×256, шрифт 13px, gap 8px);
- **`min-width: 0` во всей flex/grid-цепочке** + `minmax(0, 1fr)` в колонках + `overflow-wrap: anywhere` — длинное слово, имя или ссылка в анкете не растягивают ленту (плюс `overflow-x: hidden` как страховка);
- **фото анкеты — фиксированные миниатюры** `.thumbs__item { width: 192px; height: 256px }` + `object-fit: cover`: высота не зависит от загрузки файла и его исходного разрешения, вёрстка не «прыгает», три фото занимают одну строку; битые/удалённые фото убирает `app.js` (пустая `.thumbs` скрывается через `:empty`);
- **кликабельные элементы ≥ 30px** (`.btn`, `.icon-btn`, `.input` — 36px; кнопки REVIEW в строке — 30px, чтобы не раздувать плотную ленту);
- **панель настроек** `position: fixed; width: min(420px, 100vw)` — не шире экрана, `[hidden] { display: none !important }` перебивает `display: flex`.

Регресс-тесты: `test_feed_is_single_vertical_column` (одна колонка, строка = grid, без позиционирования), `test_feed_rows_carry_filter_attributes` (строки помечены решением и именем, фильтр/поиск в JS), `test_layout_cannot_shift_sideways` (структурные гарантии), `test_login_uses_defined_css_classes` и `test_every_component_class_is_styled` (каждый класс из шаблонов определён в `app.css` — ловит рассинхрон разметки и стилей, из-за которого `/login` рендерился без оформленной кнопки).

**Режим на сайте меняется на лету** (`web/actions.py` → `set_mode_sync`): POST на `/settings/mode` не только пишет `project.mode` в `config.yaml`, но и вызывает `collector.set_mode()` — живой `AutoActionEngine` переключается сразу, а для SEMI_AUTO/AUTO запускается `start_auto_stream()` (обработка активной анкеты / продолжение ленты Leo). Перезапуск не нужен. Без привязанного коллектора (например в тестах) режим просто сохраняется в YAML.

**Аккаунт-исполнитель меняется на лету** (`web/actions.py` → `set_account_sync`): POST на `/settings/account` пишет `auto_actions.account_session` в `config.yaml` И вызывает `collector.switch_auto_account(session)` — живой `AutoActionEngine` получает клиент другого аккаунта (`AutoActionEngine.swap_client`), кэш peer сбрасывается (у каждого аккаунта свой access_hash чата Leo), rate-limiter обнуляется, затем запускается `start_auto_stream()` на новом аккаунте. UI: выпадающий список аккаунтов в **топбаре** (`web/templates/app.html`, `#account-form`, рядом с выбором режима) — в drawer настроек его больше нет. Аккаунты читаются из `telegram.accounts` конфига; переключение происходит сразу при выборе (авто-сабмит формы, кнопка `ОК` — fallback без JS).

### Ключевые константы

- Дайвинчик (Leo) chat_id по умолчанию: **`1234060895`**
- Пороги решения: LIKE **0.75**, REVIEW **0.50**
- Версия скоринга: **`deterministic-v2`**
- Версия баннера (`app/banner.py`): **0.7**
- Авто-интервал: `interval_sec` (default **2 s** — реакция должна уйти, пока карточка на экране; точность страхует гейт `is_stale_card`)

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
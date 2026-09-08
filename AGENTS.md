# AGENTS.md

## Commands

```bash
# Run the app
python main.py

# Run all tests (508 total, no linter/formatter configured)
python -m pytest tests/ -v

# Run a single test file
python -m pytest tests/test_ai.py -v
```

## Critical Architecture Rules

- **RAW-first invariant**: raw Telegram messages are ALWAYS saved to SQLite BEFORE any parsing/classification. Never change the order in `collectors/dvinchik_collector.py`.
- **FilterEngine is Telegram-free**: `services/filter_engine.py` takes only `Profile + AppConfig`. Never import Telethon types there.
- **Deterministic scoring is Telegram-free**: `services/decision_service.py`, `services/profile_normalizer.py`, `services/feature_extractor.py`, `services/score_engine.py` never import Telethon. Work only with Profile/str and Config.
- **Deterministic scoring (Stage 8)**: Scoring is a pure rule engine, no LLM/CLIP/network. `DecisionService.evaluate()` runs for **ALL** filter results (PASS/REJECT/REVIEW); the decision is derived internally. Rules live ONLY in `config/preferences.yaml`; never hardcode in `decision_service.py`. Missing/unknown info → REVIEW, never DISLIKE (`NO_HARD_NEGATIVE_MUST_NOT_BECOME_DISLIKE`). Scoring version `deterministic-v2`. Decision на основе **информативности**: `informative + clean → LIKE` (достаточно значимых слов `>= 10` ИЛИ найден позитивный признак; `score_engine` считает `informative`/`meaningful_words`); черновые/короткие, но чистые → REVIEW. `like_threshold`/`review_threshold` в конфиге остались, но для решения инертны. **DecisionService НЕ выполняет действий** — только возвращает LIKE/REVIEW/DISLIKE (+`informative` для слоя действий).
- **AI scoring gated by PASS**: Legacy only. In the current collector, `DecisionService` evaluates every profile regardless of filter result. On the auto account, filter-level REJECT/REVIEW still auto-sends `👎` (via `auto_engine.maybe_act(AIDecision.DISLIKE)`) so Leo's stream never blocks; the profile + filter result remain in DB. This means: non-PASS filter results get `DISLIKE` recorded in `auto_actions_log` with the filter decision as reason.
- **AI errors must not break Collector**: All AI calls are wrapped in try/except. RAW messages are never lost.
- **Preferences layer (SKIP/LIKE)**: User's calibration rules (`app/preferences.py` → `PreferencesEngine`) live ONLY in `config/preferences.yaml` (live, gitignored) / `config/preferences.example.yaml` (committed). Never hardcode rules in `services/decision_service.py` — it only applies them. SKIP → hard DISLIKE; LIKE-factor → a DISLIKE is lifted to REVIEW (profile not lost). Thresholds 0.75/0.50 unchanged.
- **`filters/` was deleted during simplification** — the placeholder package is gone. Actual filter logic lives in `services/filter_engine.py` and `services/filter_service.py`. Do not recreate the placeholder.
- **`config/config.yaml` is gitignored** (contains API keys, phone, proxy creds). Only `config/config.example.yaml` is committed. Never commit real secrets.

## Config Format (nested)

```yaml
filters:
  age:
    min: 18
    max: 19
  city:
    allowed:
      - "Санкт-Петербург"
```

`FiltersConfig` exposes compat fields `age_min`, `age_max`, `city_allowed` via `model_post_init` — both forms work internally.

## Code Conventions

- **Python 3.12+** — `StrEnum`, `X | Y` union syntax, lowercase `list[str]` generics.
- **`from __future__ import annotations`** in every module.
- **Type hints on all function signatures** including return types.
- **Private attributes** use underscore prefix: `self._client`, `self._db`.
- **Docstrings and comments in Russian**, code identifiers in English.
- **`TYPE_CHECKING` guard** for heavy imports (avoids circular deps).
- **Pydantic v2** models — use `field_validator`, not `validator`.
- **Loguru** for logging (not stdlib logging). Rich for console output.

## Testing Patterns

- **No pytest-asyncio** — tests wrap async with `asyncio.get_event_loop().run_until_complete()`.
- **Class-based test organization** (not function-level).
- **Factory helpers** per test file: `make_config()`, `make_profile()`, `make_parsed()`, `make_event()`.
- **Temp DB fixtures**: `tmp_path` creates a fresh SQLite per test.
- **Mocks**: `unittest.mock.AsyncMock` / `MagicMock` for Telegram client and DB.
- Current counts: test_ai (10), test_collector (101), test_parser (47), test_decision (23), test_audit (27), test_filter (26), test_human_review (23), test_analytics (23), test_profile (18), test_preferences (12), test_review_ui (5), test_auto_action (54), test_auto_action_audit (4), test_control_bot (14), test_deterministic_scoring (84), test_manual_review (13) → **508 total**. Reset the exact counts from the real file (`tests/baseline/baseline_tests.txt`) when editing them; the summary here is indicative.

## Gotchas

- **Dual FilterResult types**: `models/raw.py` has `FilterResult` (FILTER_MATCH/FILTER_NOT_MATCH) used by the parser. `models/filter.py` has `FilterResult` (PASS/REJECT/REVIEW) used by the filter engine. They serve different layers.
- **`FilterResult` in `models/raw.py`** uses Pydantic `StrEnum` named `FilterResult` — do not confuse with `models/filter.py`'s `FilterResult` dataclass.
- **SQLite WAL mode** + foreign keys explicitly enabled via PRAGMA. Schema is idempotent (`CREATE TABLE IF NOT EXISTS`).
- **Windows-specific**: `run.bat` sets `chcp 65001`, `PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8`. `main.py` also forces UTF-8 on stdout/stderr.
- **Banner version** (`banner.py`) is synced to `"0.7"`.
- **Chat ID 1234060895** is the Dayvinchik bot default, hardcoded in `config/config.example.yaml`.
- **`proxy/`** contains vendored xray-core binaries and real VLESS config — do not commit credential changes.
- **Human decisions append-only**: `human_decisions` history is never UPDATE/DELETE. `UNIQUE(ai_decision_id)` — one review per AI evaluation. Latest human decision by `created_at`.
- **`get_profile_by_id` returns dict** (not `Profile` object) — in `_render_profile` use `profile['id']`, not `profile.id`.
- **DB thread leak in tests**: any test that opens a `Database` must close it (fixture teardown). A leaked aiosqlite thread prevents the pytest process from exiting (hang).
- **Agreement rate** = AGREEMENT/(AGREEMENT+DISAGREEMENT); SKIP excluded from denominator; `null` (not 0%) when denominator = 0. Call it "AI/Human Agreement Rate", never "AI accuracy".
- **Stage 6 Telegram-free layers**: `ReviewService`, `AnalyticsService`, `review_export.py` never import Telethon. Only `telegram/review_bot.py` consumes Telethon.
- **Decision != Action (§30)**: `DecisionService`/`AIDecision` — только LIKE/REVIEW/DISLIKE, Telegram-free. Telegram-действия — `models/action.py` (`ActionPolicy`: NO_ACTION/LIKE_ONLY/LIKE_AND_MESSAGE/DISLIKE_ONLY) + `services/action_policy.py` (`ActionPolicyResolver`), которые тоже Telegram-free; их потребляет `AutoActionEngine`. Не смешивать Decision и Action.
- **`AIDecisionResult.informative/meaningful_words`**: проставляются из `ScoringResult` в `DecisionService.evaluate_profile`; коллектор передаёт `decision.informative` в `maybe_act` для выбора LIKE_ONLY vs LIKE_AND_MESSAGE.

## Project Stage

Currently at **Stage 8 (deterministic scoring)** + **Stage 8.5 (like analytics)** on top of **Stage 7 (SEMI_AUTO)**. See `Roadmap.md` for roadmap. DecisionService computes LIKE/REVIEW/DISLIKE via a deterministic rule engine (`profile_normalizer` → `feature_extractor` H01–H09/P01–P04 → `score_engine`), rules from `config/preferences.yaml`; never LLM/CLIP. **Decision != Action (§30)**: `DecisionService` возвращает только LIKE/REVIEW/DISLIKE и НЕ знает о Telegram; что именно слать в чат (❤️ / 👎 / +«Берем)») решает слой действий (`models/action.py` `ActionPolicy` + `services/action_policy.py` `ActionPolicyResolver`). LIKE для **информативной** чистой анкеты → `LIKE_AND_MESSAGE` (❤️ + «Берем)»), для короткой → `LIKE_ONLY` (только ❤️). В SEMI_AUTO коллектор шлёт авто-действия через `AutoActionEngine` на авто-аккаунте: LIKE→`❤️` и, если нужно, цепочку «Берем)»; DISLIKE→`👎`; **AI REVIEW→ не действует сам** (уведомляет владельца — см. Manual Review), rate-limited (default 10s). `config.project.mode` гейтит авто-действия (OBSERVE → no actions). Active stream: `collector.start_auto_stream()` processes the already-displayed active profile or presses «Смотреть анкеты». ReviewBot still saves human decisions (APPROVE/REJECT/SKIP). Do not implement full AUTO / dialog manager until explicitly instructed.

## Auto-Actions (Stage 7)

- Live in `collectors/auto_action.py`: `AutoActionEngine(client, config, mode, chat_id, notify_client=None, db=None)`. `db` (Database) нужен, чтобы при уведомлении владельцу пересылать **все** фото анкеты (PROFILE + связанные MEDIA_ONLY из `profile_messages` через `get_profile_messages(profile_id)`), а не только одно PROFILE-сообщение. Без `db`/`profile_id` — прежнее поведение (один `message_id`); ошибки БД ловятся.
- Gate: `enabled` only when mode ∈ {SEMI_AUTO, AUTO} AND `auto_actions.enabled` AND a client exists (matched via `account_session` to `telegram.accounts`/`self._clients` by index).
- `maybe_act(decision, ..., informative=...)`: Decision != Action (§30) через `ActionPolicyResolver`. DISLIKE→`👎`, **AI REVIEW→ не действует сам** (возвращает `"REVIEW"`, уведомляет владельца — см. Manual Review ниже), disabled→`GATE`. LIKE → `LIKE_ONLY` (только ❤️) или `LIKE_AND_MESSAGE` (цепочка «Берем)»): ❤️ → (Leo: «Лайк отправлен, ждем ответа.») → `💌` → (Leo: «Отправь текст, видео или голосовое…») → «Берем)». Цепочка живёт в `_pending_chains` и прогрессирует из `step_pending(text, chat_id)` (вызывается коллектором на входящих НЕ-PROFILE сообщениях чата Дайвинчика на авто-аккаунте); финальный «Берем)» записывается в БД как действие `MESSAGE`. Фильтровые не-PASS (REJECT/REVIEW) на авто-аккаунте по-прежнему шлют `👎` через `maybe_act(AIDecision.DISLIKE)` — иначе Leo ждёт реакцию и лента замирает; профиль и фильтр-решение остаются в БД.
- Sent only when the profile arrived on the auto account (`task.msg.client is auto_engine.client`).
- `REPLY-markup` mechanic (KeyboardButton, not inline): the bot's profile card has buttons `❤️ 💌 📹 🎤 👎 💤`; the action is plain text `❤️`/`👎`, valid only while a profile is active.
- `auto_actions` config: `enabled`, `account_session`, `interval_sec` (rate limit, default 10s), `notify_chat_id` (default 0, user_id владельца для уведомлений о лайке/дизлайке; `0` = авто-режим «другой аккаунт»: уведомление уходит на аккаунт из `accounts`, чей user_id ≠ авто-аккаунта — в нашем случае с Бармалея (dvai_2) на Меланхолика (dvai)), плюс **ОТДЕЛЬНЫЕ** `like` и `like_message` (Decision != Action, §30): `like.enabled` — слать ❤️ (LIKE_ONLY); `like_message.enabled` + `like_message.text` («Берем)») — слать ❤️ И потом «Берем)» (LIKE_AND_MESSAGE, только для информативных анкет; требует и `like.enabled=true`, иначе деградирует к LIKE_ONLY/NO_ACTION).
- Active mode: `collector.start_auto_stream()` при старте (SEMI_AUTO) обрабатывает уже показанную активную анкету (без повторного `✨🔍`), а если активной нет — нажимает кнопку «🚀 Смотреть анкеты» (`VIEW_BUTTON_FRAGMENT`, идемпотентно через `AutoActionEngine.send_text`), продолжая ленту Leo.
- Капчи/проверки Leo (сделки, подписки, подтверждения, гео-запросы и т.п.): на `UNKNOWN`-сообщение в чате Дайвинчика на авто-аккаунте авто-аккаунт нажимает **последнюю ОБЫЧНУЮ** кнопку (`_press_captcha_button`, идемпотентно) — сбрасывает диалог и продолжает ленту. Спец-кнопки (запрос геолокации/телефона и т.п.) текстом не нажимаются — для них берётся последняя `KeyboardButton` (в гео-капче «Пришли свое расположение…» это «Продолжить смотреть анкеты» = отказ от координат). Реагирует ТОЛЬКО на явные капчи/сделки: текст должен содержать один из маркеров `CAPTCHA_MARKERS` (сделк/подписываешься/подтверд/верификац/расположени/геолокаци/координат и т.п.), а reply-кнопок должно быть `>= CAPTCHA_MIN_BUTTONS`. Это НЕ трогает главное меню Leo и Premium-промо (иначе бот зацикливается: жмёт «Активировать Premium»/«← Назад» каждые 1-2 сек). Работает и при старте (`start_auto_stream` fallback после view-кнопки), и в live-обработке (`UNKNOWN`-ветка).
- Кнопка «🚀 Смотреть анкеты» может прийти на **отдельном сообщении** после рекламного/промо-текста (а не на самом промо). В live-`UNKNOWN`-ветке, если у текущего сообщения нет ни view-кнопки, ни капчи, авто-аккаунт всё равно вызывает `_press_view_button_if_needed()` — он сам сканирует последние 15 сообщений и жмёт кнопку только если она реально есть и ещё не нажата (идемпотентность), поэтому меню/Premium без такой кнопки не зацикливаются.
- Идемпотентность авто-действий — по **конкретной карточке И виду действия** (`chat_id`+`telegram_message_id`+`action`), НЕ по `profile_id`/имени: `has_auto_action_for_message(chat_id, tm_id, action)` / `record_auto_action(..., tm_id)`. LIKE и MESSAGE — **независимые** действия (§30): на одну карточку может быть LIKE и MESSAGE, но каждый вид — не более одного раза. Повторная карточка той же личности (новый `telegram_message_id` в ленте) получает реакцию снова, чтобы лента не замирала при повторах Leo. `auto_actions_log` хранит `telegram_message_id` (partial unique index `chat_id`+`telegram_message_id`+`action` WHERE `telegram_message_id IS NOT NULL`), `UNIQUE(profile_id)` убран; `record_auto_action` обновляет статус профиля `LIKED`/`DISLIKED` только для LIKE/DISLIKE (MESSAGE статус НЕ меняет). Миграция старой схемы — полное пересоздание таблицы с переносом записей (SQLite не умеет DROP CONSTRAINT) / пересоздание unique-индекса.
- **Уведомления владельцу НЕ дублируются**: реакция (`❤️`/`👎`) шлётся Leo на каждую карточку (feed не замирает), но пересылка владельцу (`_notify`/`_notify_needs_action`) выполняется ТОЛЬКО для первого авто-действия профиля — гейт `_has_prior_action(profile_id)` через `db.has_auto_action(profile_id)`. Иначе повтор одной анкеты заваливает владельца пересылками всей её истории («несколько анкет разом»). Ошибка БД при проверке → fail-open (уведомление всё равно уходит).
- **Резолв ЛЕО-чата**: `AutoActionEngine.ensure_peer()` сканирует диалоги авто-аккаунта и кэширует сущность чата (ЛЕО-чат — `PeerUser`, у каждого аккаунта свой access_hash; без кэша `send_message` падает «Could not find the input entity»). Вызывается из `_send` и `start_auto_stream`. Если чата нет в диалогах — явный WARNING «открой Дайвинчика (Leo) с этого аккаунта» и действий нет.

## Manual Review (Stage 8)

- Логика в `services/manual_review.py` (Telegram-free): `ManualReviewRecorder(db, path, enabled, file_format)` — фиксирует РУЧНОЕ решение владельца по REVIEW-анкете в файл (`data/reviews/review_log.json` или `.md`). `classify_outgoing(text)`: `❤️`→LIKE, `👎`→DISLIKE, иначе MESSAGE.
- Сценарий: детерминированный scoring выдал REVIEW (бот «не справляется»). `AutoActionEngine.maybe_act(REVIEW)` НЕ шлёт авто-`👎` (`_notify_needs_action`: пересылает карточку владельцу и пишет «Нужно твоё решение»), ждёт ручного действия. Анкета остаётся активной в ленте.
- Владелец действует с того же аккаунта, под которым слушает collector (тот же, что `dvinchik.chat_id`). Исходящее действие перехватывается в `_handle_outgoing_message` → `_maybe_record_manual_review`: через `get_chat_profile_context(chat_id)`/`_pending_profiles` берёт «текущую» анкету чата и, если последнее AI-решение == REVIEW, вызывает `manual_review.handle_outgoing(...)` → запись в файл.
- Только активная REVIEW-анкета: бот и сам шлёт `❤️`/`👎` (LIKE/DISLIKE), но recorder записывает лишь когда последнее AI-решение профиля == REVIEW → ложных записей нет.
- Проводка: `main.py` создаёт `ManualReviewRecorder` (гейт `config.manual_review.enabled`) и передаёт в `DvinchikCollector(..., manual_review=...)`. Конфиг — `manual_review: enabled / file / format (json|md)` в `config.yaml`/`config.example.yaml`. Ошибки файла/БД не ломают перехват исходящих (RAW уже сохранён).

## Like Analytics (Stage 8.5)

- **Цель**: статистика «что написал при лайке и ответила ли девушка» — какие тексты сообщений работают лучше (конверсия в ответ) и каких девушек чаще лайкают.
- **Два источника сообщений при лайке**:
  1. Авто-«Берем)» (цепочка LIKE_AND_MESSAGE) — текст пишется в `auto_actions_log.message_text` при `action='MESSAGE'` (из `like_message.text`; `record_auto_action(..., message_text=...)`). Колонка добавлена миграцией `_ensure_auto_actions_message_text`.
  2. Ручные тексты владельца — в `_handle_outgoing_message` → `_maybe_record_sent_message` → таблица `sent_messages` (`source='manual'`). НЕ записываются: исходящие с авто-аккаунта (`msg.client is auto_engine.client` — уже в авто-журнале), кнопки/реакции из `MANUAL_MESSAGE_EXCLUDE`, тексты < 2 символов. Профиль привязывается по «текущей» анкете чата (`get_chat_profile_context`/`_pending_profiles` → `Database.resolve_profile_by_message`).
- **Сигнал ответа** — взаимный лайк: MATCH-сообщение «Начинай общаться 👉 [Имя]» → `Database.record_match_response(name, chat_id, tm_id, telegram_username=...)`. Привязка к профилю по имени (`_resolve_profile_by_name`): регистронезависимо по-настоящему (Python `lower`), приоритет — профили со свежими записями `auto_actions_log`. Дубликаты на карточку отсекаются UNIQUE-индексом `(chat_id, telegram_message_id)` (INSERT OR IGNORE).
- **Отчёты** (Telegram-free, `AnalyticsService` + `Database`): `get_message_response_stats()` — по каждому тексту `sent` (уникальных профилей) / `responded` / `rate`; `get_top_liked_profiles(limit)` — топ по лайкам + ответили ли. Команды ControlBot `/msgs` и `/top` (+ inline-кнопки «💬 Сообщения»/«💖 Топ лайков»).
- **Правила**: аналитика read-only и не влияет на действия (LIKE/MESSAGE всё равно шлются); ошибки БД в записи ответов/исходящих не ломают pipeline (RAW уже сохранён).

## Control Panel (Stage 7.5)

- Live in `telegram/control_bot.py`: `ControlBot(client, config, collector, db)`.
- Commands `/status /mode on|off /stream /recent /msgs /top /help` + inline-кнопки; принимаются ТОЛЬКО от `control.allowed_user_ids` (оба аккаунта-оператора: Бармалей `8525808108` + melancholic `1753676469`).
- Регистрируется на ВСЕХ `telegram.accounts` (в `main.py` — цикл по `clients`); `config.control.enabled` гейтит регистрацию. Ответ шлётся с того же аккаунта, что получил команду (`event.client`).
- Runtime-переключение: `collector.set_mode(Mode)` → обновляет `AutoActionEngine.mode` на лету (гатег `enabled` пересчитывается) + `AppConfig.persist_mode()` записывает `project.mode` в `config.yaml` (переживает restart). `AutoActionEngine.mode` — сеттер.
- `collector.auto_engine()` — доступ к движку для панели; `collector.mode` — текущий режим.

## Documentation Rule (IMPORTANT)

**Правило документирования:** Любые новые фичи, измененные архитектурные решения,
новые модули, эндпоинты или изменения в структуре базы данных/данных **обязательно**
должны сразу же документироваться и отражаться в актуальном состоянии в файле
`README.md`. Ни одно изменение не считается завершенным, пока документация не обновлена.

Практика применения:
- Каждый завершённый PR/коммит, добавляющий что-то из перечисленного, обновляет `README.md`.
- Сверяй `Roadmap.md` (roadmap/этапы) — при переходе на новый Stage обнови раздел «Roadmap».
- Если меняется схема БД — обнови блок «База данных» и «Связи» в README.
- Если меняются конфиг/эндпоинты — обнови соответствующие секции README.

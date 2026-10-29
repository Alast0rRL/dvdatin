# AGENTS.md

Правила для агентов. Подробности механик и история сбоев — в `README.md`, этапы — в `Roadmap.md`.

## Commands

```bash
# Run the app
python main.py

# Run all tests (~1030 total, no linter/formatter configured)
python -m pytest tests/ -v

# Run a single test file
python -m pytest tests/test_ai.py -v
```

## Архитектура

- Поток: Telegram → `raw_messages` (RAW-first) → классификация `dvinchik_parser` (PROFILE / MEDIA_ONLY / MATCH / SERVICE / UNKNOWN) → `FilterService` (PASS/REJECT/REVIEW) → `DecisionService` (LIKE/REVIEW/DISLIKE) → фото-шаги `_apply_photo_taste` / `_apply_photo_vice` → `ActionPolicyResolver` → `AutoActionEngine` аккаунта.
- Решение по тексту: `profile_normalizer` → `feature_extractor` (H01–H09 / P01–P04) → `score_engine` → `decision_service`, версия `deterministic-v2`; правила из `config/preferences.yaml`.
- Решение по фото: `services/photo_taste.py` (модель вкуса, CLIP на CPU, `photo_taste.mode: shadow|decide`, на сервере `decide`) и `services/photo_vice.py` (сигареты/алкоголь, только для LIKE). Общий CLIP — `services/clip_encoder.py`.
- MATCH → `dialog_logger` (журнал лички) → `match_greeter` (стикер-привет) → `match_intro` (её анкета + закреп, только при «Авто») → `match_script` (сценарий знакомства по схеме владельца, правила — `services/match_script_rules.py`) → передача владельцу.
- Фон (`main.py`): `run_history_sync` (60 с: сверка чата Leo, повтор приветствий, автопереписка, сверка переписок), `run_photo_backfill`.
- Управление: два аккаунта, у каждого свой `AutoActionEngine` и переключатели «Скроллинг» / «Авто» (`auto_actions.account_controls`). Внутри кода `Mode`: OBSERVE = скроллинг выкл, SEMI_AUTO = скроллинг, AUTO = скроллинг + «Авто».
- UI: Web (`web/`, Flask в daemon-потоке; `/`, `/rate`, `/dialogs`) и `telegram/control_bot.py` (команды только от `control.allowed_user_ids`).
- **Telegram-free** (не импортировать Telethon): `services/*` (фильтр, скоринг, фото-модель, `action_policy`, `manual_review`, `reason_labels`), `models/*`, `app/preferences.py`. Telethon — только в `collectors/`, `telegram/`, `web/photos.py`.
- **Коллектор разделён на миксины (2026-10-29, чистый рефакторинг)**: `DvinchikCollector` в `collectors/dvinchik_collector.py` — конструктор, `register`, `_handle_new_message` (RAW-first), `_process_message`, `_handle_media_only`, `_handle_callback_query`. Остальные методы — в миксинах (общий `self`; тесты по-прежнему патчат их на экземпляре, ссылки `DvinchikCollector.<метод>` ниже верны): `collectors/account_engines.py` `AccountEnginesMixin` (`_engines`/`_auto_engine`, `set_controls`, `set_mode`, `auto_for`, `mode_block_reason`, `_session_scope`, `switch_auto_account`); `collectors/stream_control.py` `StreamControlMixin` (`start_auto_stream`, `_process_active_profile_if_any`, `_is_match_card`, `_halt_on_match_card`, `_stop_on_match_card*`, `resume_after_match_stop`, `_report_stream_stuck`, `_clear_stream_error`); `collectors/leo_navigation.py` `LeoNavigationMixin` (`_recent_history`, `_continue_stream_if_needed`, `_press_*_if_needed`, `_answer_city_check_if_needed`, `_handle_captcha`, `_extract_button_texts`); `collectors/history_sync.py` `HistorySyncMixin` (`sync_chat_history`, `run_history_sync`, `recover_backlog`); `collectors/match_dialogs.py` `MatchDialogsMixin` (`_handle_private_message`, `_greet_match`, `_enroll_match_intro`, `run_match_intros`, `retry_match_greetings`, `_track_dialog`, `sync_dialogs`); `collectors/photo_steps.py` `PhotoStepsMixin` (`_schedule_photo_shadow`, `_apply_photo_taste`, `_apply_photo_vice`, `run_photo_backfill`); `collectors/outgoing.py` `OutgoingMixin` (`_handle_outgoing_message`, `_maybe_record_sent_message`, `_maybe_record_manual_review`); `collectors/console_output.py` `ConsoleOutputMixin` (`_print_*`, `_serialize_*`). Константы/маркеры Leo, `captcha_signature`, `is_manual_message_excluded`, `_detect_media_type`, contextvar `_CURRENT_SESSION` — `collectors/leo_markers.py` (реэкспорт из `collectors.dvinchik_collector` сохранён, `__all__`). Новый метод клади в миксин своей темы, а не в коллектор.

## Инварианты

Где указан тест — он защищает пункт; меняешь поведение — меняй тест осознанно.

### Сбор и данные
- **RAW-first**: сообщение сохраняется в `raw_messages` ДО разбора; порядок в `dvinchik_collector.py` не менять; ошибки разбора RAW не теряют. Тесты: `test_save_failure_stops_pipeline`, `test_get_sender_failure_still_saves_raw`.
- **Всё «за аккаунт»**: дедуп, лок чата, `_pending_profiles`, `chat_context`, `idx_raw_unique` ключуются аккаунтом; фото качает только аккаунт-получатель. Тесты: `test_same_message_from_two_accounts_enqueued_once`, `test_download_uses_receiver_account`.
- **Код «за аккаунт» ставит контекст** `_CURRENT_SESSION` (`_bind_client` / `_session_scope` / `task.account_session`), иначе подействует движок основного. Тесты: `test_message_handled_by_receiving_account_engine`, `test_each_account_has_own_engine_and_mode`.
- **Имя сессии клиента — из `main.py`** (`client_sessions`), не по позиции в `telegram.accounts`. Тест: `TestClientSessions`.
- **Миграции идемпотентны**; `account_session` и `is_outgoing` мигрируются ДО пересоздания `idx_raw_unique` в `Database.connect()`. Все вызовы `save_raw_message` — keyword.
- **`human_decisions` append-only** (`UNIQUE(ai_decision_id)`), не UPDATE/DELETE.
- **История сверки не трогает сообщения моложе 120 с** (`HISTORY_SYNC_MIN_AGE_SEC`) — их отдаёт pipeline живой хендлер. Тест: `TestHistorySync`.

### Решения
- **Правила только в `config/preferences.yaml`** (`preferences.example.yaml` в git); в `decision_service.py` не хардкодить. Тесты: `TestUserPreferences`, `test_preferences.py`.
- **Нет данных → REVIEW, никогда DISLIKE** (`NO_HARD_NEGATIVE_MUST_NOT_BECOME_DISLIKE`); исключение — явный `low_info.action: skip`. Тесты: `TestDecisionServiceNoUnknownToDislike`, `TestLowInfoSkipRule`.
- **Текстовый скоринг детерминирован**: без LLM и сети, один текст → одно решение. CLIP живёт только в фото-шагах и не влияет на `score_engine`. Тест: `TestDeterminism`.
- **Фото решает лайк только в `decide`** и только если нет `USER_SKIP` / `HARD_NEGATIVE` / `FILTER_REJECTED` / `USER_LIKE_HARD` / `PHOTO_VICE` (`photo_taste_applies`). Тесты: `test_photo_taste.py`.
- **Фото-шаги не бросают исключений в авто-действие**: любая ошибка CLIP/скачивания оставляет решение как есть. Тест: `test_error_reporter.py::TestPhotoChecksNeverBreakActions`.
- **`DecisionService.evaluate()` — для ВСЕХ результатов фильтра**; фильтр-REJECT становится причиной `FILTER_REJECTED` → DISLIKE → `👎`, лента не встаёт.

### Действия в Telegram
- **Decision != Action (§30)**: `DecisionService` только возвращает LIKE/REVIEW/DISLIKE; что слать — `ActionPolicyResolver` (`NO_ACTION` / `LIKE_AND_MESSAGE` / `DISLIKE_ONLY`). Не смешивать.
- **❤️ не шлётся**: LIKE = только цепочка `💌 📹 🎤` (вся строка) → «Берем)»; `LIKE_ONLY` удалён, без `like` + `like_message` — NO_ACTION. Тесты: `test_like_only_policy_is_removed`, `test_like_sends_composer_button_without_heart`.
- **Реакция только в свою карточку**: `note_card` — в хендлере сразу после RAW-save; устаревшая карточка → `"STALE"`, ничего не шлём и не пишем. `interval_sec` держать малым (2 с). Тесты: `test_reaction_skipped_for_stale_card`, `test_stale_skip_beats_composer_defer`.
- **REVIEW — бот сам не действует**: уведомляет владельца и ждёт ручного решения (`manual_review`). Тест: `test_review_notifies_owner_when_message_id_given`.
- **Лимит ❤️ исчерпан** → LIKE не шлётся (`"LIKE_LIMIT"`), `👎` уходит; лимит хранится на аккаунт (`likes_exhausted_until:<session>`); «Скроллинг» не включается до сброса. Тесты: `TestAutoActionLikeLimit`, `test_mode_blocked_while_like_limit_exhausted`.
- **Карточка взаимного лайка / «Кому-то понравилась…»** → стоп скроллинга своего аккаунта, кнопок не жмём; стоп в хендлере до очереди. Тесты: `test_match_card_stops_search_in_handler`, `test_match_notice_without_buttons_stops_search`.
- **Сами жмём только**: «Смотреть анкеты», «Пока без Premium», «1 🚀» в меню, «Нет» на «живешь в Питере?», выученный ответ капчи; «Показать» — только по «Продолжить» владельца. Тест: `test_start_stream_main_menu_never_presses_other_buttons`.
- **Неизвестную капчу наугад не жмём** — она ждёт ответа на сайте (кроме `captcha.fallback_press_last`). Тест: `test_start_stream_unknown_captcha_waits_for_owner`.
- **Идемпотентность — по карточке и виду действия** (`chat_id` + `telegram_message_id` + `action`), не по профилю; уведомление владельцу — только на первое действие профиля. Тесты: `test_same_profile_id_allows_same_action_twice`, `test_first_action_notifies`.
- **Пересылка владельцу — только id своего аккаунта** (`_profile_message_ids`), иначе Telethon перешлёт чужие сообщения. Тест: `test_filters_ids_from_other_account`.
- **Проверка истории — одна выборка** (`_recent_history`) на все проверки кнопок; сбой чтения → `[]`, поток не падает. Тесты: `test_start_stream_reads_history_once_for_buttons`, `test_history_read_error_does_not_stop_stream`.
- **Перенаправление лички** (`auto_actions.account_redirects`, стрелка в шапке): матч на аккаунте → привет/журнал/автопереписка с аккаунта-цели, только если цель — другой вошедший аккаунт; анкета ищется в ленте аккаунта матча; её анкету девушке аккаунт матча пересылает пишущему, тот — ей (`MatchIntro._relay_card`). Тесты: `test_redirected_account_greets_from_target`, `test_redirect_to_unknown_account_greets_from_own`, `test_card_relayed_from_account_that_saw_it`.
- **Владелец пишет сам — бот молчит**: перед отправкой анкеты проверка её чата; наше исходящее новее привета → `OWNER_CHAT`, анкету не шлём. Тест: `test_owner_chatting_cancels_intro`.
- **Автопереписка — только то, что попросил владелец** (стикер-привет, её анкета + закреп, сценарий `match_script` по его схеме). Новых шагов и фраз не добавлять без явной команды; концы веток и ответы не по сценарию — передача владельцу. Тест: `test_her_story_goes_to_owner`.
- **Сценарий: бот не отрицает, что он бот** — «ты бот?» → передача владельцу без ответа. Тест: `test_asks_bot_hands_off_without_lying`.
- **Сценарий: «не пиши» / прямое оскорбление → чат удаляется сразу**, бытовой мат не по адресу — не грубость. Тесты: `test_go_away_deletes_chat_at_once`, `TestClassify`.
- **Сценарий молчит, если пишет владелец**: исходящее новее последнего сообщения бота (кроме служебного «закрепил») → `owner`. Тесты: `test_owner_wrote_silences_bot`, `test_pin_service_message_is_not_owner`.

### Web UI
- **Лента = зеркало чата Leo**: читает только `raw_messages`, строго выбранного аккаунта (без legacy `''`) и только `dvinchik.chat_id`; `sent_messages` / `auto_actions_log` не подмешивать. Тесты: `TestChatFeedMirror`, `test_strict_account_filter`, `test_feed_shows_only_leo_chat`.
- **Вёрстка ленты — одна колонка** (`max-width: 860px; margin: 0 auto`, `minmax(0, 1fr)`, миниатюры 192×256); без выравнивания по краям, `align-self` и абсолютного позиционирования. Тест: `test_feed_is_single_vertical_column`.
- **Альбомы склеиваются в хронологическом порядке, потом лента разворачивается**. Тест: `test_album_whole_in_newest_first_feed`.
- **Сырых цветов вне палитр в `app.css` нет** (темы через CSS-переменные). Тест: `TestTheme::test_no_raw_colors_outside_palettes`.
- **Коды причин переводит только `services/reason_labels.py`** (сайт и TG); новые коды добавлять туда. Тест: `TestReasonLabels`.
- **Запрос фото `/rate` идёт по `idx_raw_unique`** (`rm.account_session IN (pm.account_session, '')`). Тест: `test_photo_query_uses_raw_index`.
- **Логи, ошибки и текст из БД в JS — только через `textContent`**; `innerHTML` — лишь для HTML-фрагментов, отрисованных сервером (Jinja-экранирование).

### Безопасность и приватность
- **Секреты не коммитить**: `config/config.yaml`, `config/preferences.yaml`, `proxy/`, `data/` — gitignored; в git только `*.example.yaml`.
- **Тесты пишут конфиг только во временные файлы**: `tests/conftest.py::_protect_real_config` роняет запись в настоящий `config/config.yaml`.
- **Текст переписки и чужих сообщений не пишется в лог** (loguru / debug-консоль): только в БД. ControlBot логирует лишь имя команды. Тесты: `TestControlBotPrivacy`, `test_dialog_logger.py`.
- **ControlBot — только входящие личные `/команды` от `control.allowed_user_ids`**. Бот рецензий (`ReviewBot`) удалён — не возвращать.
- **Любой ERROR доходит до владельца** (`collectors/error_reporter.py`: `app_state.bot_error` + Telegram, ≤ 1 раз в 10 мин). Тест: `test_error_reporter.py`.

## Gotchas

- `models/raw.py::FilterResult` (FILTER_MATCH/NOT_MATCH, парсер) ≠ `models/filter.py::FilterResult` (PASS/REJECT/REVIEW, фильтр).
- `FiltersConfig` — вложенный формат (`filters.age.min`); compat-поля `age_min`/`age_max`/`city_allowed` через `model_post_init`.
- `get_profile_by_id` возвращает dict, не `Profile`. SQLite — WAL + `PRAGMA foreign_keys`, схема идемпотентна (`CREATE TABLE IF NOT EXISTS`).
- Тест, открывший `Database`, обязан её закрыть — иначе aiosqlite-поток вешает pytest.
- `filters/` удалён — логика в `services/filter_engine.py` / `filter_service.py`; не восстанавливать.
- `like_threshold` / `review_threshold` в конфиге есть, но на решение не влияют.
- Windows: `run.bat` ставит `chcp 65001`, `PYTHONUTF8=1`; `main.py` форсит UTF-8. Чат Leo по умолчанию — `1234060895`. Версия баннера — `"0.7"`.

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
- **Mocks**: `unittest.mock.AsyncMock` / `MagicMock` for Telegram client and DB. Telegram client — `tests/mocks.py::make_tg_client()` (sync Telethon methods `iter_dialogs`/`add_event_handler` are `MagicMock`, `send_message` returns a `MagicMock`); for `iter_messages` use `MagicMock(side_effect=lambda *a, **k: async_iter([...]))`, never a bare `AsyncMock`.
- **No unawaited coroutines**: `pytest.ini` has `filterwarnings = error::RuntimeWarning` + `error::pytest.PytestUnraisableExceptionWarning` — a forgotten `await` or an `AsyncMock` on a sync method fails the test.
- Current counts (2026-10-29): test_action_policy (11), test_ai (10), test_analysis_export (10), test_analytics (17), test_audit (31), test_auto_action (106), test_auto_action_audit (5), test_captcha_memory (11), test_collector (176), test_control_bot (19), test_decision (23), test_deterministic_scoring (84), test_dialog_logger (27), test_error_reporter (8), test_filter (26), test_manual_review (13), test_match_greeter (30), test_match_intro (30), test_match_script (69), test_parser (63), test_photo_taste (44), test_photo_vice (13), test_preferences (26), test_profile (21), test_web (161) → **1034 total**. Reset the exact counts from the real file (`tests/baseline/baseline_tests.txt`) when editing them; the summary here is indicative.

## Deploy Rule (IMPORTANT)

После **каждой новой версии** (коммит с изменениями кода/шаблонов/статики/конфига) работа не завершена, пока изменения не задеплоены и сервис не перезапущен:

1. Локально: `python -m pytest tests/ -q` (минимум — затронутые файлы тестов).
2. `git add <файлы>` → `git commit -m "<conventional-commit>"` → `git push origin main`.
3. Сервер (Ubuntu, `deploy/DEPLOY.md`): `ssh root@144.31.118.3 -p 2200 -i ~/.ssh/homekey` → `cd /opt/dvai && git pull origin main` → `systemctl restart dvai` → `systemctl is-active dvai` + `journalctl -u dvai -n 8 --no-pager`.
4. Не задеплоено после последнего коммита — ошибка процесса: откатить или додеплоить.

## Documentation Rule (IMPORTANT)

Новые фичи, архитектурные решения, модули, эндпоинты, изменения схемы БД или конфига **сразу** отражаются в `README.md`; без этого изменение не завершено.
- Новый Stage — обнови `Roadmap.md` и раздел «Roadmap».
- Схема БД — блок «База данных» в README; конфиг/эндпоинты — соответствующие секции.
- Новый инвариант — короткий пункт в этот файл с тестом, который его защищает; разбор сбоя — в README.

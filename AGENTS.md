# AGENTS.md

Правила для агентов. Подробности механик и история сбоев — в `README.md`, этапы — в `Roadmap.md`.

## Commands

```bash
# Run the app
python main.py

# Run all tests (~1200 total, no linter/formatter configured)
python -m pytest tests/ -v

# Run a single test file
python -m pytest tests/test_ai.py -v
```

## Архитектура

- Поток: Telegram → `raw_messages` (RAW-first) → классификация `dvinchik_parser` (PROFILE / MEDIA_ONLY / MATCH / SERVICE / UNKNOWN) → `FilterService` (PASS/REJECT/REVIEW) → `DecisionService` (LIKE/REVIEW/DISLIKE) → фото-шаги `_apply_photo_taste` / `_apply_photo_vice` → `ActionPolicyResolver` → `AutoActionEngine` аккаунта.
- Решение по тексту: `profile_normalizer` → `feature_extractor` (H01–H09 / P01–P04) → `score_engine` → `decision_service`, версия `deterministic-v2`; правила из `config/preferences.yaml`.
- Решение по фото: `services/photo_taste.py` (модель вкуса, CLIP на CPU, `photo_taste.mode: shadow|decide`, на сервере `decide`) и `services/photo_vice.py` (сигареты/алкоголь, только для LIKE). Общий CLIP — `services/clip_encoder.py`.
- MATCH → `dialog_logger` (журнал лички) → `match_greeter` (стикер-привет) → `match_intro` (её анкета + закреп, только при «Авто») → `match_script` (сценарий знакомства по схеме владельца, правила — `services/match_script_rules.py`) → передача владельцу.
- Фон (`main.py`): `run_history_sync` (60 с: сверка чата Leo, повтор приветствий, автопереписка, архив чатов бота, сверка переписок), `run_photo_backfill`.
- Управление: два аккаунта, у каждого свой `AutoActionEngine` и переключатели «Скроллинг» / «Авто» (`auto_actions.account_controls`). Внутри кода `Mode`: OBSERVE = скроллинг выкл, SEMI_AUTO = скроллинг, AUTO = скроллинг + «Авто».
- UI: Web (`web/`, Flask в daemon-потоке; `/`, `/rate`, `/dialogs`) и `telegram/control_bot.py` (команды только от `control.allowed_user_ids`).
- **Telegram-free** (не импортировать Telethon): `services/*` (фильтр, скоринг, фото-модель, `action_policy`, `manual_review`, `reason_labels`), `models/*`, `app/preferences.py`. Telethon — только в `collectors/`, `telegram/`, `web/photos.py`.
- **Коллектор разделён на миксины (2026-10-29, чистый рефакторинг)**: `DvinchikCollector` в `collectors/dvinchik_collector.py` — конструктор, `register`, `_handle_new_message` (RAW-first), `_process_message`, `_handle_media_only`, `_handle_callback_query`. `_process_message` (2026-11-02) — только диспетчер по типу: `_on_profile` (сохранить → фильтр → решение), `_refine_decision` (фото-вкус → «уже был матч» → порок), `_react_to_card` (одна реакция + журнал), `_on_match`, `_on_unknown`, `_on_service`, `_check_like_rejection`; «сообщение на аккаунт активного движка» — одна проверка `_is_auto_receiver`. Новую ветку клади в свой шаг, не вкладывай в `_on_profile`. Остальные методы — в миксинах (общий `self`; тесты по-прежнему патчат их на экземпляре, ссылки `DvinchikCollector.<метод>` ниже верны): `collectors/account_engines.py` `AccountEnginesMixin` (`_engines`/`_auto_engine`, `set_controls`, `set_mode`, `auto_for`, `mode_block_reason`, `_session_scope`, `switch_auto_account`); `collectors/stream_control.py` `StreamControlMixin` (`start_auto_stream`, `_process_active_profile_if_any`, `_is_match_card`, `_halt_on_match_card`, `_stop_on_match_card*`, `resume_after_match_stop`, `_report_stream_stuck`, `_clear_stream_error`); `collectors/leo_navigation.py` `LeoNavigationMixin` (`_recent_history`, `_continue_stream_if_needed`, `_press_*_if_needed`, `_answer_city_check_if_needed`, `_handle_captcha`, `_extract_button_texts`); `collectors/history_sync.py` `HistorySyncMixin` (`sync_chat_history`, `run_history_sync`, `recover_backlog`); `collectors/match_dialogs.py` `MatchDialogsMixin` (`_handle_private_message`, `_greet_match`, `_enroll_match_intro`, `run_match_intros`, `retry_match_greetings`, `_track_dialog`, `sync_chat_folders`, `sync_dialogs`); `collectors/photo_steps.py` `PhotoStepsMixin` (`_schedule_photo_shadow`, `_apply_photo_taste`, `_apply_photo_vice`, `run_photo_backfill`); `collectors/outgoing.py` `OutgoingMixin` (`_handle_outgoing_message`, `_maybe_record_sent_message`, `_maybe_record_manual_review`); `collectors/console_output.py` `ConsoleOutputMixin` (`_print_*`, `_serialize_*`). Константы/маркеры Leo, `captcha_signature`, `is_manual_message_excluded`, `_detect_media_type`, contextvar `_CURRENT_SESSION` — `collectors/leo_markers.py` (реэкспорт из `collectors.dvinchik_collector` сохранён, `__all__`). Новый метод клади в миксин своей темы, а не в коллектор.

## Инварианты

Где указан тест — он защищает пункт; меняешь поведение — меняй тест осознанно.

### Сбор и данные
- **RAW-first**: сообщение сохраняется в `raw_messages` ДО разбора; порядок в `dvinchik_collector.py` не менять; ошибки разбора RAW не теряют. Тесты: `test_save_failure_stops_pipeline`, `test_get_sender_failure_still_saves_raw`.
- **Всё «за аккаунт»**: дедуп, лок чата, `_pending_profiles`, `chat_context`, `idx_raw_unique` ключуются аккаунтом; фото качает только аккаунт-получатель. Тесты: `test_same_message_from_two_accounts_enqueued_once`, `test_download_uses_receiver_account`.
- **Код «за аккаунт» ставит контекст** `_CURRENT_SESSION` (`_bind_client` / `_session_scope` / `task.account_session`), иначе подействует движок основного. Тесты: `test_message_handled_by_receiving_account_engine`, `test_each_account_has_own_engine_and_mode`.
- **Имя сессии клиента — из `main.py`** (`client_sessions`), не по позиции в `telegram.accounts`. Тест: `TestClientSessions`.
- **Миграции идемпотентны**; `account_session` и `is_outgoing` мигрируются ДО пересоздания `idx_raw_unique` в `Database.connect()`. Все вызовы `save_raw_message` — keyword.
- **Дедуп анкет — fingerprint + описание**: fingerprint («имя, возраст, город») лишь даёт кандидатов (`find_profiles_by_fingerprint`), та же анкета — кандидат с похожим описанием (`description_similarity` ≥ `DESCRIPTION_MATCH_RATIO` в `services/profile_service.py`); разные девушки с одним «имя, возраст» не склеиваются. Старые склеенные записи не трогать без согласия владельца. Тесты: `TestDedupByDescription`.
- **`human_decisions` append-only** (`UNIQUE(ai_decision_id)`), не UPDATE/DELETE.
- **Невалидные @username (хэши приглашений `+…`) не резолвятся**, потерянный чат — WARNING один раз за запуск. Тесты: `test_invite_hash_is_not_resolved`, `test_lost_chat_warned_once_per_run`.
- **Пропавшее фото не ищется повторно 6 ч** (`web/photos.py::MISSING_TTL_SEC`). Тест: `test_missing_photo_not_searched_again`.
- **История сверки не трогает сообщения моложе 120 с** (`HISTORY_SYNC_MIN_AGE_SEC`) — их отдаёт pipeline живой хендлер. Тест: `TestHistorySync`.

### Решения
- **Правила только в `config/preferences.yaml`** (`preferences.example.yaml` в git); в `decision_service.py` не хардкодить. Тесты: `TestUserPreferences`, `test_preferences.py`.
- **Нет данных → REVIEW, никогда DISLIKE** (`NO_HARD_NEGATIVE_MUST_NOT_BECOME_DISLIKE`); исключение — явный `low_info.action: skip`. Тесты: `TestDecisionServiceNoUnknownToDislike`, `TestLowInfoSkipRule`.
- **Текстовый скоринг детерминирован**: без LLM и сети, один текст → одно решение. CLIP живёт только в фото-шагах и не влияет на `score_engine`. Тест: `TestDeterminism`.
- **Фото решает лайк только в `decide`** и только если нет `USER_SKIP` / `HARD_NEGATIVE` / `FILTER_REJECTED` / `USER_LIKE_HARD` / `PHOTO_VICE` (`photo_taste_applies`). Тесты: `test_photo_taste.py`.
- **Фото-шаги не бросают исключений в авто-действие**: любая ошибка CLIP/скачивания оставляет решение как есть. Тест: `test_error_reporter.py::TestPhotoChecksNeverBreakActions`.
- **Режим «кенты»** (`config.friends`, аккаунт `friends.account_session`): свои фильтр и правила, без H/P (`builtin_features=False`) и фото-шагов; 💌 с `friends.like_message`; после матча бот парню не пишет — журнал + «напиши сам»; `profiles.audience='friends'` не склеивается с девушками и не идёт в /rate. Переключается кнопкой «Девушки / Кенты» в строке аккаунта (`/settings/audience`), на лету, кенты — только на одном аккаунте (тесты `test_audience_button_toggles_friends`, `test_switch_friends_on_the_fly`). Город «📍N км» проходит фильтр при ≤ `filters.city.nearby_km`. Тесты: `tests/test_friends.py`.
- **`DecisionService.evaluate()` — для ВСЕХ результатов фильтра**; фильтр-REJECT становится причиной `FILTER_REJECTED` → DISLIKE → `👎`, лента не встаёт.

### Действия в Telegram
- **Decision != Action (§30)**: `DecisionService` только возвращает LIKE/REVIEW/DISLIKE; что слать — `ActionPolicyResolver` (`NO_ACTION` / `LIKE_AND_MESSAGE` / `DISLIKE_ONLY`). Не смешивать.
- **❤️ не шлётся**: LIKE = только цепочка `💌 📹 🎤` (вся строка) → «Берем)»; `LIKE_ONLY` удалён, без `like` + `like_message` — NO_ACTION. Тесты: `test_like_only_policy_is_removed`, `test_like_sends_composer_button_without_heart`.
- **Реакция только в свою карточку**: `note_card` — в хендлере сразу после RAW-save; устаревшая карточка → `"STALE"`, ничего не шлём и не пишем. `interval_sec` держать малым (2 с). Тесты: `test_reaction_skipped_for_stale_card`, `test_stale_skip_beats_composer_defer`.
- **REVIEW — бот сам не действует**: уведомляет владельца и ждёт ручного решения (`manual_review`). Тест: `test_review_notifies_owner_when_message_id_given`.
- **Лимит ❤️ исчерпан** → LIKE не шлётся (`"LIKE_LIMIT"`), `👎` уходит; лимит хранится на аккаунт (`likes_exhausted_until:<session>`); «Скроллинг» не включается до сброса, а при отказе Leo «Слишком много ❤️» или упоре в лимит на анкете (`LIKE_LIMIT`) сам выключается у этого аккаунта (`stop_scroll_on_like_limit`; «Авто» не трогаем; владелец, 2026-11-03). Тесты: `TestAutoActionLikeLimit`, `test_mode_blocked_while_like_limit_exhausted`, `test_like_limit_turns_scroll_off`, `test_like_limit_on_card_turns_scroll_off`.
- **«Ты понравился N девушке, показать её?» → бот сам жмёт «Показать»** (владелец, 2026-11-03; `auto_actions.auto_show_likes: true`): лента не встаёт, её анкета (шапка «Кому-то понравилась твоя анкета:» снимается парсером) идёт в обычный pipeline, LIKE → матч → привет/сценарий; один раз на карточку (`_match_ack`), в хендлере карточка — активная (`note_card`). Уведомление без кнопок («…Заканчивай с вопросом выше», «Кто-то тобой заинтересовался!») ленту не держит, анкета над ним остаётся активной. Вопрос без кнопки показа — стоп. Тесты: `test_start_stream_auto_shows_liked`, `test_auto_show_pressed_once_per_card`, `test_match_card_auto_show_keeps_search`, `test_liked_notice_auto_show_keeps_card_active`, `test_liked_notice_does_not_hide_active_profile`, `test_match_question_without_button_still_stops`, `test_missed_match_card_auto_shows`, `test_liked_you_header_is_profile`.
- **«Скроллинг» выкл + «Авто» вкл → бот всё равно смотрит лайкнувших** (владелец, 2026-11-04; `_liked_only_mode`): жмёт «Показать» на «Ты понравился N девушкам» (Leo сразу даёт матч, лимит ❤️ не мешает) и «Главное меню» на «Слишком много ❤️», когда под ним «Заканчивай с вопросом выше» (`_limit_menu_button`); ленту («1 🚀») и реакции не трогает; фоновая проверка `check_liked_cards` раз в минуту. Прод: лимит выключил скроллинг, «Ты понравился 2 девушкам» висело. Тесты: `test_auto_without_scroll_shows_liked`, `test_no_auto_no_scroll_does_not_show_liked`, `test_liked_notice_under_like_limit_presses_main_menu`, `test_liked_notice_under_profile_does_not_press_menu`, `test_check_liked_cards_in_background`, `test_menu_not_pressed_without_scroll`.
- **При `auto_show_likes: false`** — прежний стоп: карточка лайка / уведомления → стоп скроллинга своего аккаунта, кнопок не жмём; стоп в хендлере до очереди. Тесты: `test_match_card_stops_search_in_handler`, `test_match_notice_without_buttons_stops_search`, `test_interested_notice_stops_search`.
- **Сами жмём только**: «Смотреть анкеты», «Пока без Premium», «1 🚀» в меню, «Главное меню» на «Слишком много ❤️» под уведомлением о лайке, «Нет» на «живешь в Питере?», выученный ответ капчи; «Показать» на карточке лайка (при `auto_show_likes: false` — только по «Продолжить» владельца). Тест: `test_start_stream_main_menu_never_presses_other_buttons`.
- **Неизвестная капча → последняя (правая) кнопка** (владелец, 2026-11-01; `captcha.fallback_press_last: true` по умолчанию): нажатая кнопка запоминается как ответ (`learn_captcha_answer`), на сайте строка «🧩 Капча: бот нажал …»; при `false` — ждёт ответа на сайте, наугад не жмём. Капча — и вопрос без маркеров с незнакомыми кнопками (`_is_unknown_keyboard`: ≥ 2 кнопок, ни одной известной Leo-кнопки), и в сообщениях класса SERVICE. Тесты: `test_start_stream_unknown_captcha_fallback_presses_last_button`, `test_service_captcha_presses_last_button`, `test_start_stream_unknown_captcha_waits_for_owner`, `test_known_keyboards_are_not_unknown`.
- **Идемпотентность — по карточке и виду действия** (`chat_id` + `telegram_message_id` + `action`), не по профилю; уведомление владельцу — только на первое действие профиля. Тесты: `test_same_profile_id_allows_same_action_twice`, `test_first_action_notifies`.
- **Одна реакция на карточку**: перед реакцией — `has_reaction_for_message` (LIKE **или** DISLIKE), отложенная реакция тоже проверяет журнал — второй 👎 ушёл бы на следующую анкету; карточка «занимается» в памяти (`_reacted_cards`) до первого `await` в `_react_to_card` — параллельные проходы (живой хендлер + «активная анкета» после капчи) журнал ещё не видят. Тесты: `test_deferred_reaction_skipped_when_card_already_logged`, `test_direct_action_clears_deferred_copy`, `test_card_reacted_once_before_journal_written`.
- **С кем был матч — не лайкаем снова**: LIKE на повторном показе → DISLIKE `ALREADY_MATCHED` (`was_profile_matched`: `match_responses` / `dialog_contacts` / `match_dialogs`); свой лайк без ответа повторный лайк НЕ блокирует. Тесты: `test_already_matched_profile_gets_dislike`, `test_was_profile_matched`.
- **Пересылка владельцу — только id своего аккаунта** (`_profile_message_ids`), иначе Telethon перешлёт чужие сообщения. Тест: `test_filters_ids_from_other_account`.
- **Проверка истории — одна выборка** (`_recent_history`) на все проверки кнопок; сбой чтения → `[]`, поток не падает. Тесты: `test_start_stream_reads_history_once_for_buttons`, `test_history_read_error_does_not_stop_stream`.
- **Перенаправление лички** (`auto_actions.account_redirects`, стрелка в шапке): матч на аккаунте → привет/журнал/автопереписка с аккаунта-цели, только если цель — другой вошедший аккаунт; анкета ищется в ленте аккаунта матча; аккаунты друг другу не пишут (владелец, 2026-11-01), карточка на другом аккаунте → **копия** (владелец, 2026-11-02): аккаунт карточки качает фото+текст, аккаунт лички шлёт их ей; его клиента нет → `OTHER_ACCOUNT` + «отправь сам». Девушка без @username при перенаправлении → бот не пишет, `NO_ACCESS` на аккаунте матча + «напиши сам» (владелец, 2026-11-02; тест `test_redirect_without_username_only_notifies`). Она пишет на аккаунт матча — бот с цели молчит: привет с цели через 120 с и только при пустом её чате на аккаунте матча, её сообщение туда позже → `MAIN_CHAT` / сценарий `owner` на других аккаунтах (владелец, 2026-11-02; тесты `test_redirect_skipped_when_she_writes_to_match_account`, `test_she_writes_to_match_account_silences_redirect`). Тесты: `test_redirected_account_greets_from_target`, `test_redirect_to_unknown_account_greets_from_own`, `test_card_on_other_account_sent_as_copy`, `test_card_on_other_account_not_relayed`.
- **Карточка матча при перенаправлении — в момент матча** (`_match_card_now`), не после паузы привета 120 с: аккаунт матча за это время листает дальше, «текущая» карточка чужая → `NO_PROFILE` (прод 2026-11-03: Софья, геля, килая). Тест: `test_redirect_card_taken_at_match_time`.
- **Карточка → профиль — по `profile_messages`** (каждый показ), не по `profiles.source_message_id` (первый показ): матч обычно на повторе. Тест: `test_find_match_card_on_repeat_showing`.
- **Имя матча ↔ имя анкеты — через `match_name_key`** (`database/database.py`): без регистра и без разметки `_ * ~ `` по краям — Leo съедает «_» в ссылке матча как курсив (прод 2026-11-04: анкета «_miss.E,», матч «miss.E,» → NO_PROFILE, анкета не ушла). Тест: `test_find_match_card_ignores_markup_around_name`.
- **Анкета с «город, район»** — PROFILE, фильтр по городу из первой части; иначе карточка UNKNOWN и лента встаёт молча. Тест: `test_city_with_district_is_profile`.
- **Матч — только ссылка на человека**: `MATCH_RE` берёт настоящий username (буква + 4..32 `[A-Za-z0-9_]`) или `tg://user?id=`; ссылки-приглашения `t.me/+…` / `joinchat` в рекламе Leo — не матч (прод 2026-11-02: привет «блогу для мужчин»). Тест: `test_channel_invite_ad_is_not_a_match`.
- **Девушка без @username** (`tg://user?id=`) — привет через `InputPeerUserFromMessage` (сообщение Leo своего аккаунта). Тест: `test_unknown_user_id_resolved_via_leo_message`.
- **Девушке — её карточка матча, не «последний показ профиля»**: при матче запоминаются `card_session` (аккаунт матча) + `card_message_id` (карточка перед уведомлением), пересылается только она; не нашлась или неизвестна — `NO_PROFILE` + уведомление владельцу, поиска «по имени / последнему показу» нет. Один `profile_id` склеивает разных девушек с тем же «имя, возраст». Тесты: `test_card_on_other_account_not_relayed`, `test_match_card_missing_sends_nothing`, `test_unknown_card_sends_nothing`, `test_run_notifies_owner_when_card_missing`, `test_redirected_match_card_from_match_account`.
- **Владелец пишет сам — бот молчит**: перед отправкой анкеты проверка её чата; наше исходящее новее привета → `OWNER_CHAT`, анкету не шлём. Свой стикер-привет бота (исходящий стикер в первые `GREETING_STICKER_SKEW_SEC` = 60 с после `greeted_at`) владельцем не считается: дата Telegram бывает на секунду позже часов сервера (прод 2026-11-02). Тесты: `test_owner_chatting_cancels_intro`, `test_own_sticker_dated_after_greeting_is_not_owner`.
- **Автопереписка — только то, что попросил владелец** (стикер-привет, её анкета + закреп, сценарий `match_script` по его схеме). Новых шагов и фраз не добавлять без явной команды; концы веток и ответы не по сценарию — передача владельцу. Тест: `test_her_story_goes_to_owner`.
- **Тайминги сценария — по канвасу владельца** («Знакомство для бота», 2026-11-02): молчит 24 ч после стикера или «Рассказывайте» → «АЛООО ПРОСЫПАЕМСЯ» только в 22:00–24:00 по местному времени, ещё 24 ч → бот больше не пишет (`NO_REPLY` / `stopped`); после анкеты — 24 ч до «Рассказывайте». «АЛООО» бота не считается сообщением владельца. Она написала первой (до стикера) — это ответ. Тесты: `TestWakeUpAfterSticker`, `test_wake_up_after_tell_me_silence`, `test_give_up_after_wake_up`, `test_she_wrote_first_counts_as_reply`.
- **Шапка «понравилась твоя анкета (и ещё N)» в её карточке → N случайное из `match_intro.liked_count` ([20, 30])** (владелец, 2026-11-04): такая карточка уходит ей копией (`boost_liked_count`), без шапки — пересылкой. Тесты: `test_liked_count_randomized_on_own_card`, `test_card_without_liked_header_is_forwarded`.
- **Сценарий: «что это?» / «зачем?» / «?» / «зачем закрепил?» / «почему ты это скинул?» → «Это твоя анкета из Дайвинчика) Закрепил, чтобы не потерять тебя среди чатов»**, а не непонятное «Пусть лежит здесь» / «Чтобы знать, с кем общаюсь» — девушек смущало, прод 2026-11-03: Оля → ЧС (владелец, 2026-11-04; `texts.card_here` = `texts.pin_why`). Вопрос про закреп — свой вид (`ASKS_PIN` — «закреп», «скинул», «отправил», «прислал», «переслал», «пометил»; важнее `ASKS_WHAT`; владелец, 2026-11-03 / 2026-11-04). «Помечаешь чтоб не забыть?» — как «чтобы не перепутать?» (`ASKS_TOO_MANY`) → «Это так-то удобно», не «Рассказывайте» (прод 2026-11-04). Тесты: `test_pin_question_answered_not_card_here`, `test_why_sent_answered_not_card_here`, `test_pin_question_beats_generic_what`, `test_mark_not_to_forget_answered_convenient`.
- **Архив и папки чатов девушек** (владелец, 2026-11-04; `collectors/chat_folders.py`, правила `services/chat_folders.py`): каждый её чат один раз в архив (`archived_at`; вынул владелец — не возвращаем), общение — папки по названию `Auto` (бот ведёт сейчас + «Авто» вкл; пустой шаг сценария и привет старше 72 ч без «АЛООО» — не Auto) / `Active` (она писала в 3+ разных дня и молчит < 48 ч) / `50/50` (молчит 48 ч+, не писала, или наше последнее без ответа 24 ч+) / `New` (остальное); удалённый и пустой чат — ни в одной. Сразу после стикера-привета чат в архив и без звука (`hide_now`); звук только у Active и New, Auto и 50/50 — без звука (`notify_mark`). Чат не там, где оставил бот, → `folder_manual`, больше не двигаем; сбой запроса — не «ручной». Кенты не входят. Молчит и после «АЛООО» → чат удаляется только у владельца (`delete_silent_chat`), и замолчавшие раньше тоже (`ChatFolders._delete_silent`); пропал из папки, а по правилу ему не место в папках, — не «перенёс владелец». Тесты: `tests/test_chat_folders.py`, `test_silent_after_wake_up_deletes_chat_for_owner`.
- **Удалённый чат — бот молчит, на сайте «Неудачные»** (владелец, 2026-11-03): её история пуста (нет даже стикера-привета / «закрепил») → сценарий `owner`, анкета/«АЛООО» — `OWNER_CHAT`; сверка журнала ставит `dialog_contacts.chat_deleted_at` (только если сообщения были, новое снимает) → причина `deleted`. Тесты: `test_deleted_chat_silences_bot`, `test_deleted_chat_cancels_intro`, `test_sync_marks_deleted_chat_and_clears_on_new_message`, `test_deleted_chat_is_failed`.
- **Сценарий: бот не отрицает, что он бот** — «ты бот?» → передача владельцу без ответа. Тест: `test_asks_bot_hands_off_without_lying`.
- **Сценарий: «не пиши» / прямое оскорбление → чат удаляется сразу у обоих** (`delete_for_both`), бытовой мат не по адресу — не грубость. Тесты: `test_go_away_deletes_chat_at_once`, `TestClassify`.
- **Сценарий молчит, если пишет владелец**: исходящее новее последнего сообщения бота (кроме служебного «закрепил») → `owner`. Тесты: `test_owner_wrote_silences_bot`, `test_pin_service_message_is_not_owner`.

### Web UI
- **Лента = зеркало чата Leo**: читает только `raw_messages`, строго выбранного аккаунта (без legacy `''`) и только `dvinchik.chat_id`; `sent_messages` / `auto_actions_log` не подмешивать. Тесты: `TestChatFeedMirror`, `test_strict_account_filter`, `test_feed_shows_only_leo_chat`.
- **Вёрстка ленты — одна колонка** (`max-width: 860px; margin: 0 auto`, `minmax(0, 1fr)`, миниатюры 192×256); без выравнивания по краям, `align-self` и абсолютного позиционирования. Тест: `test_feed_is_single_vertical_column`.
- **Десктоп ≥ 1180 px — две колонки** (`.layout`): слева липкий пульт `.layout__side` (аккаунты, баннер, `#captchas`), справа лента с липким тулбаром; уже — всё в одну колонку. На `html, body` — `overflow-x: clip` (одно `hidden` ломает `position: sticky`). Капчи при обновлении ленты заменяются целиком (`captchas.innerHTML = first.pending`), не дописываются. Тесты: `test_desktop_layout_sidebar_and_feed`, `test_captcha_blocks_replaced_not_appended`.
- **Альбомы склеиваются в хронологическом порядке, потом лента разворачивается**. Тест: `test_album_whole_in_newest_first_feed`.
- **Сырых цветов вне палитр в `app.css` нет** (темы через CSS-переменные). Тест: `TestTheme::test_no_raw_colors_outside_palettes`.
- **Коды причин переводит только `services/reason_labels.py`** (сайт и TG); новые коды добавлять туда. Тест: `TestReasonLabels`.
- **Запрос фото `/rate` идёт по `idx_raw_unique`** (`rm.account_session IN (pm.account_session, '')`). Тест: `test_photo_query_uses_raw_index`.
- **Шапка — только разделы и действия; состояние аккаунта — в его карточке (`.acct-state`); первый экран (`#hero`) — только карточки аккаунтов, сводка за сутки — в панели настроек** (владелец, 2026-11-02). Анимации появления — только сдвигом, без `opacity: 0` (контент виден, даже если анимация не проиграется). Тесты: `test_topbar_has_row_per_account`, `test_day_stats_in_settings_not_on_first_screen`.
- **Служебные сообщения Leo сворачиваются** (`item.service`: входящее без анкеты, фото и `MATCH_RE`), матч — нет; **фото открываются в `<dialog class="lightbox">`**, не в новой вкладке. Тесты: `test_leo_menu_collapsed_match_kept`, `test_photo_opens_in_lightbox`.
- **Итог переписки на `/dialogs` — только `services/dialog_outcome.classify`** (удачные / идут / неудачные с причиной + флаг «ждёт тебя»; детерминирован, без LLM; владелец, 2026-11-03): бот ещё ведёт (ждёт ответа, сценарий не дошёл до конца) — не «молчит», даже после 72 ч. Тесты: `tests/test_dialog_outcome.py`, `test_tabs_split_good_and_failed`.
- **Фото на `/rate` — целиком, без обрезки** (`object-fit: contain`, владелец 2026-11-02). Тест: `test_rate_photo_not_cropped`.
- **Текст Leo в ленте — через фильтр `leo_text`** (экранирование + markdown-ссылки → `<a>`, только http(s)/tg). Тесты: `TestRedesign`.
- **`app.js` удаляет незагрузившиеся картинки только из ленты/переписки** (`DROPPABLE_IMG`); фото `/rate` обрабатывает `rate.js` (заглушка «пропусти»). Тест: `test_broken_image_cleanup_only_for_feed_thumbs`.
- **Логи, ошибки и текст из БД в JS — только через `textContent`**; `innerHTML` — лишь для HTML-фрагментов, отрисованных сервером (Jinja-экранирование).

### Безопасность и приватность
- **Секреты не коммитить**: `config/config.yaml`, `config/preferences.yaml`, `proxy/`, `data/` — gitignored; в git только `*.example.yaml`.
- **Тесты пишут конфиг только во временные файлы**: `tests/conftest.py::_protect_real_config` роняет запись в настоящий `config/config.yaml`.
- **Текст переписки и чужих сообщений не пишется в лог** (loguru / debug-консоль): только в БД. ControlBot логирует лишь имя команды. Тесты: `TestControlBotPrivacy`, `test_dialog_logger.py`.
- **ControlBot — только входящие личные `/команды` от `control.allowed_user_ids`**. Бот рецензий (`ReviewBot`) удалён — не возвращать.
- **Экстренный стоп**: исходящее «стой» в чат Leo с любого аккаунта → `emergency_stop`: «Скроллинг» и «Авто» выкл на всех аккаунтах, цепочки сняты, баннер на сайте. Тесты: `test_emergency_stop_turns_everything_off`, `test_other_text_is_not_emergency_stop`.
- **Аккаунты не пишут владельцу/друг другу** (`auto_actions.notify_telegram: false` по умолчанию): `_notify`, `_notify_needs_action`, `notify_owner` молчат, всё — на сайте; реакции в Leo идут. В тестах уведомления включает `conftest.py::_telegram_notify_on` (маркер `real_notify_switch` — проверка самого выключателя). Тесты: `TestTelegramNotifySwitch`.
- **FloodWait на уведомлениях — пауза, не ERROR**: до срока Telegram пересылки анкет пропускаем, служебные тексты копим и шлём одним сообщением. Тесты: `TestNotifyFloodWait`.
- **Любой ERROR доходит до владельца** (`collectors/error_reporter.py`: `app_state.bot_error` → окно на сайте; в Telegram — только при `notify_telegram: true`, ≤ 1 раз в 10 мин). Тест: `test_error_reporter.py`.

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
- Current counts (2026-11-04): test_action_policy (11), test_ai (10), test_analysis_export (10), test_analytics (17), test_audit (31), test_auto_action (114), test_auto_action_audit (5), test_captcha_memory (11), test_chat_folders (43), test_collector (209), test_control_bot (19), test_decision (23), test_deterministic_scoring (84), test_dialog_logger (33), test_dialog_outcome (18), test_error_reporter (8), test_filter (26), test_friends (15), test_manual_review (13), test_match_greeter (37), test_match_intro (66), test_match_script (92), test_parser (69), test_photo_taste (44), test_photo_vice (13), test_preferences (26), test_profile (27), test_web (177) → **1251 total**. Reset the exact counts from the real file (`tests/baseline/baseline_tests.txt`) when editing them; the summary here is indicative.

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

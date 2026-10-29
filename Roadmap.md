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
│  │ 8a. photo taste (decide) / photo vice    │   │
│  │ 9. auto-action своего аккаунта           │   │
│  │    («Скроллинг») / manual review         │   │
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
         ▼ (Telegram-free pipeline)
┌─────────────────────────────────────────────────┐
│  profile_normalizer → feature_extractor         │
│  → score_engine → decision_service (LIKE/…)     │
│  → photo_taste / photo_vice (CLIP, CPU)         │
└─────────────────────────────────────────────────┘

MATCH → dialog_contacts → MatchGreeter (стикер) → её ответ
      → MatchIntro (её анкета + закреп, только «Авто») → dialog_messages
```

## Текущее состояние (2026-10-29)

Работают два аккаунта, у каждого свой движок и два независимых переключателя:
**«Скроллинг»** (бот листает ленту Leo и ставит реакции) и **«Авто»**
(автопереписка в личке после взаимного лайка). Решение по анкете: текстовые
правила (`deterministic-v2`, `config/preferences.yaml`) + модель вкуса по фото
(CLIP ViT-L-14 на CPU + логистическая регрессия, обучена на разметке `/rate`).
На сервере модель фото **решает** лайк (`photo_taste.mode: decide`), текстовые
запреты и сильные плюсы главнее. LLM в проекте нет.

### Завершено

- [x] Stage 0: Config (Pydantic), Logging (Loguru/Rich), DB (SQLite+aiosqlite), Telethon client, banner, `main.py`.
- [x] Stage 1 / 1.5: Парсер, классификатор, RAW-first, city-normalizer, MEDIA_ONLY, dedup, rate-limiter, stats.
- [x] Stage 2 / 2.5: Профили (fingerprint, upsert), `profiles` + `profile_messages`, ProfileService, DB Audit.
- [x] Stage 3: Filter Engine (возраст/город/полнота), история в БД, интеграция в коллектор.
- (удалено) Stage 4 / 4.1–4.3: LLM (Ollama) + CLIP + Ubuntu AI Server + remote-клиенты — выпилено при упрощении кодовой базы.
- [x] Stage 5: Decision Engine `models/decision.py`, `DecisionService`, `ai_decisions` (детерминированные пороги/правила).
- [x] Stage 6: Human Review & Analytics — `human_decisions`, ReviewService, AnalyticsService (Agreement Rate), Telegram UI, CSV-export. **Удалён 2026-10-21** (ReviewBot отвечал любому пользователю; заменён `manual_review` + ControlBot).
- [x] Preferences layer (SKIP/LIKE) — `config/preferences.yaml`, `app/preferences.py` (детерминированные правила).
- [x] Stage 7 (авто-действия): `AutoActionEngine` — `👎` и цепочка `💌 📹 🎤` → «Берем)» (❤️ не шлётся из-за дневного лимита), гейт «реакция в свою карточку», rate-limit, идемпотентность по `telegram_message_id`, поток через кнопку «Смотреть анкеты», обход капч Leo.
- [x] Stage 7.5: `ControlBot` — `/status /mode on|auto|off /stream /recent /msgs /top /help`, только личные команды от `allowed_user_ids`, управляет основным аккаунтом.
- [x] Stage 7.6: «Память капч» — капчу Leo, ответ на которую бот выучил, он пробивает автоматически; неизвестную выводит в блок «Капчи ждут ответа» на главном экране сайта `/` (владелец отвечает, ответ запоминается в `captcha_memory` и сразу отправляется Leo). `captcha_signature` = нормализованный текст; опции `enabled` / `auto_answer` / `fallback_press_last`.
- [x] Stage 7.7: Premium-промо после реакции (кнопки [⭐️Активировать, Пока без Premium]) — бот идемпотентно нажимает отказную кнопку (`_press_premium_decline_if_needed`, «Активировать» никогда), лента не встаёт.
- [x] Stage 8: детерминированный скоринг (normalizer → features H01–H09/P01–P04 → score → decision; правила только в `preferences.yaml`; `NO_HARD_NEGATIVE_MUST_NOT_BECOME_DISLIKE`; missing/unknown → REVIEW). Manual Review — ручные решения владельца по REVIEW-анкетам в файл (`services/manual_review.py`).
- [x] Stage 8.5: аналитика лайков — «что я написал при лайке и ответила ли девушка». `auto_actions_log.message_text` (авто-«Берем)»), `sent_messages` (ручные действия: ❤️→LIKE, 👎→DISLIKE, текст→MESSAGE), `match_responses` (взаимный лайк, привязка по имени), `like_outcomes` — результат на каждый лайк (содержание сообщения + `responded` 0/1). Отчёты `/msgs` (конверсия текстов) и `/top` (топ девушек по лайкам, ответила ли, последнее сообщение) в ControlBot. Read-only, не влияет на действия.
- [x] Stage 8.5.2: лимит ❤️ исчерпан → пауза до 07:00. При «Слишком много ❤️ за сегодня» `AutoActionEngine.handle_like_rejection` ставит `likes_exhausted_until` в `app_state` (переживает рестарт), снимает цепочки «Берем)», один раз уведомляет владельца; `maybe_act` больше не шлёт ❤️ (`"LIKE_LIMIT"`), а 👎 продолжает (лента Leo не замирает).
- [x] Stage 8.6: восстановление «зависшего» композера после рестарта + сбой автопотока (`app_state.stream_error`, красный баннер в вебе, TG-уведомление владельцу).
- [x] Debug Console (Web UI): кнопка отладки в топбаре + `GET /chat/debug` — логи из памяти (`app/logging.LOG_BUFFER`, stdlib `WARNING+` через `_InterceptHandler`) и снимок состояния (режим, аккаунт, `stream_error`, лимит ❤️, счётчики, последние авто-действия).
- [x] Лента = прямое дублирование Telegram-чата (Stage 9.3): `/` читает только `raw_messages` (одно сообщение = одна строка, входящие Leo + наши исходящие), фильтр по аккаунту-исполнителю (`raw_messages.account_session`, история размечена `backfill_raw_account_session` по id-диапазонам — иначе «супер-чат» двух аккаунтов), уникальный индекс включает направление `is_outgoing` (иначе наши исходящие молча терялись). Карточки/бейджи/inline-формы капч из ленты убраны — анкета показывается теми же сообщениями, что в Telegram.
- [x] Упрощение кодовой базы (Phase 1–10): удалена вся legacy-инфраструктура LLM/CLIP (сервисы, модели, конфиг, тесты, e2e-скрипт), пустые плейсхолдер-пакеты (`dialogs/`, `filters/`, `managers/`, `prompts/`, `utils/`), неиспользуемый код.
- [x] Stage 11: экспорт анализа всех анкет (liked/disliked/skipped/reviewed/pending/seen) в CSV (`main.py --export-analysis`) + полная очистка БД с бэкапом (`main.py --clear-db`; `data/exports/backups/`).
- [x] Stage 7.8–7.9: главное меню Leo → «1 🚀»; стоп поиска на карточке и уведомлении о взаимном лайке (кнопка «▶ Продолжить» на сайте); ответ «Нет» на «ты живешь в Питере?».
- [x] «Улучшенный» вид ленты: только ❤️/👎 бота с человеко-читаемыми причинами (`services/reason_labels.py`).
- [x] Ошибки бота → Telegram владельцу и всплывающее окно на сайте (`collectors/error_reporter.py`).
- [x] Фото: проверка сигарет/алкоголя (`services/photo_vice.py`, по умолчанию выкл).
- [x] Фото: модель вкуса — теневой расчёт каждой карточки (`photo_scores`), режим `decide` (модель решает лайк, одобрено 2026-10-23), видео-анкеты по превью-кадру.
- [x] Фото: active learning — фоновый досчёт отпечатков, страница `/rate` (Tinder-свайпы, с 2026-10-29 оценка каждого фото отдельно, переоценка старых анкет), очередь «модель сомневается», переобучение `scripts/train_photo_taste.py` с заменой модели только если не хуже.
- [x] Стикер-привет на каждый взаимный лайк (`collectors/match_greeter.py`), пауза и очередь при спам-блоке Telegram.
- [x] Два аккаунта с раздельным управлением: свой движок, лимит ❤️, баннер сбоя; переключатели «Скроллинг»/«Авто» вместо режимов OBSERVE/SEMI_AUTO/AUTO; «Скроллинг» не включается при исчерпанном лимите ❤️.
- [x] Журнал переписок с матчами (`collectors/dialog_logger.py`, `dialog_contacts`/`dialog_messages`, страница `/dialogs`, экспорт JSON).
- [x] Stage 12 (начало): автопереписка при включённом «Авто» — девушка ответила на стикер-привет → через 30 мин бот пересылает ей её же анкету и закрепляет у обоих (`collectors/match_intro.py`, таблица `match_dialogs`, конфиг `auto_actions.match_intro`).

## Следующие этапы

**Stage 12: Dialog Manager (продолжение)** — только по явной команде владельца
- Анализ собранных переписок (`/dialogs/export.json`) → что работает
- Следующие шаги переписки после анкеты
- Генерация реплик

**Техдолг**
- Разделить `collectors/dvinchik_collector.py` (~3700 строк) и `database/database.py` (~2700 строк)
- Убрать ~120 предупреждений «coroutine was never awaited» в тестах
- Обновить `tests/baseline/baseline_tests.txt` (770 строк против ~950 тестов)

**Stage 10: Production**
- Частично есть: отладочная консоль, баннеры сбоев, ошибки бота в Telegram
- Мониторинг доступности сервиса и CPU/RAM (CLIP ~3.7 ГБ)
- Адаптация под изменения Leo

## Ограничения

### Не реализовывать без явной команды владельца:
- Новые шаги автопереписки сверх уже одобренных (стикер-привет, её анкета + закреп)
- Генерацию сообщений девушкам (Message Generator)
- Нажатие кнопок Leo, кроме уже разрешённых (смотреть анкеты, «1 🚀», отказ от Premium, «Нет» на проверку города, «Показать» по «Продолжить», выученные ответы на капчи)

## Тесты

```bash
# Все тесты
python -m pytest tests/ -v

# Аналитика лайков (Stage 8.5)
python -m pytest tests/test_analytics.py -v

# Фото-модель
python -m pytest tests/test_photo_taste.py tests/test_photo_vice.py -v

# Матчи и переписка (Stage 12)
python -m pytest tests/test_match_greeter.py tests/test_match_intro.py tests/test_dialog_logger.py -v

# Только детерминированный скоринг (Stage 8)
python -m pytest tests/test_deterministic_scoring.py tests/test_decision.py tests/test_preferences.py -v

# Только Stage 7 (авто-действия)
python -m pytest tests/test_auto_action.py tests/test_auto_action_audit.py tests/test_collector.py -v

# Только фильтр
python -m pytest tests/test_filter.py -v

# Только парсер
python -m pytest tests/test_parser.py -v
```

Текущий результат (2026-10-29): **950 passed**, ~3 мин.
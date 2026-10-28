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
- **Human Review (Stage 6) — удалён (2026-10-21)**: Telegram-бот рецензий (`telegram/review_bot.py`: `/review`, `/profile`, `/stats`, `/ai_stats`, `/disagreements`), `ReviewService`, `AnalyticsService` и `--export-review` убраны — бот отвечал на команды ЛЮБОМУ пользователю Telegram (без проверки отправителя), а функцию заменили ручная проверка REVIEW-анкет (`manual_review`) и ControlBot. Таблица `human_decisions` осталась: в неё пишут ручные кнопки на сайте.
- **Авто-действия (Stage 7, SEMI_AUTO)** (`collectors/auto_action.py`): Decision != Action (§30): `DecisionService` возвращает только LIKE/REVIEW/DISLIKE, а что слать в Telegram решает `ActionPolicyResolver` (`models/action.py`). LIKE → **только** цепочку «Берем)» ❤️→💌📹🎤→«Берем)» (LIKE_AND_MESSAGE): обычного лайка без сообщения больше нет — политика LIKE_ONLY удалена (голое ❤️ оставляло анкету без продолжения диалога); если выключен `like` или `like_message` — лайк не отправляется вовсе; DISLIKE → `👎`; rate-limit `interval_sec` (по умолчанию **2 с** — Leo принимает реакцию только пока карточка на экране); **гейт «реакция в свою карточку»** (`note_card`/`is_stale_card` → `"STALE"`): карточка помечается в хендлере по порядку прихода, и устаревшая карточка **не** получает реакцию вовсе (при `interval_sec = 10 с` промехали 54 из 60 действий); идемпотентность **по карточке + виду действия** (`chat_id`+`telegram_message_id`+`action` — LIKE и MESSAGE независимы); фильтровые не-PASS тоже получают `👎` (лента Leo не замирает); автопродолжение ленты **Карточка взаимного лайка** («Ты понравился N девушке, показать её?», кнопки [«1. Показать.», «2. Не хочу больше никого смотреть.»]): **поиск ОСТАНАВЛИВАЕТСЯ** (N — любое): бот не жмёт ни «Показать», ни «Не хочу…», переводит режим в `OBSERVE` (сохраняется в `config.yaml`), снимает незакрытые цепочки «Берем)», ставит баннер на сайте (`stream_error`) и один раз на карточку уведомляет владельца (`app_state.match_stop_card`); продолжить — `/mode on` или режим на сайте (стоп — прямо в Telegram-хендлере сразу после RAW-save, до очереди pipeline: `_halt_on_match_card` помечает карточку активной, поэтому 👎 на анкету, ещё стоящую в очереди, не уходит; карточку, пропущенную событиями Telegram, ловит сверка истории `sync_chat_history`; при старте — `_stop_on_match_card_if_needed()`, карточка актуальна, пока после неё нет нашего исходящего); кнопкой «🚀 Смотреть анкеты»; **Premium-промо после реакции** (Stage 7.7): после ❤️/👎 Leo присылает рекламу [⭐️Активировать, Пока без Premium] — бот идемпотентно нажимает отказную кнопку («Активировать» никогда), чтобы лента не встала. **Главное меню Leo (Stage 7.8)**: при исчерпании ленты бот шлёт меню «1. Смотреть анкеты. / 2. Моя анкета. …» с reply-клавиатурой `["1 🚀", "2", "3", "4"]` — кнопки-промо «🚀 Смотреть анкеты» там НЕТ, и лента вставала намертво (прод 2026-10-01, `msg=25309`, авто-аккаунт `dvai_2`); `_press_menu_view_if_needed()` жмёт «1 🚀» — только если самая свежая карточка с кнопками это меню, остальные кнопки («Моя анкета», «не искать», Premium) не жмём никогда. **Кнопки карточки лайка бывают двух видов** — [«1. Показать.», «2. Не хочу…»] и [«1 👍», «2 💤»]: `_is_match_show_button` матчит «показать» **и по 👍**, отказную отсекает по «не хочу»/💤 (без этого лента вставала и на карточке взаимного лайка). **Память капч (Stage 7.6)**: капчи/проверки Leo (сделки/подписки/подтверждения/гео-запросы — маркеры `CAPTCHA_MARKERS` + ≥2 reply-кнопки) — `_handle_captcha`: выученный ответ (`captcha_memory` по сигнатуре `captcha_signature(text)`) отправляется автоматически; **неизвестная капча выводится в блоке «⚠️ Капчи ждут ответ» над лентой главного экрана и наугад не нажимается** (ждёт ответа владельца; опционально `fallback_press_last` = старое поведение с последней обычной кнопкой; `captcha.enabled=false` — вообще без памяти). **Уведомления владельцу не дублируются**: реакция шлётся на каждую карточку, но пересылка владельцу — только для первого авто-действия профиля (`db.has_auto_action(profile_id)`), иначе повтор анкеты заваливал бы его историей. Гейт: режим `project.mode ∈ {SEMI_AUTO, AUTO}` + `auto_actions.enabled` + найден клиент по `account_session`. OBSERVE → действий нет.
- **Пересылка владельцу не тянет чужие чаты** (`_profile_message_ids`, прод-баг 2026-10-19): пересылаются только сообщения анкеты с `account_session` ТЕКУЩЕГО аккаунта-исполнителя. У каждого аккаунта своё пространство `telegram_message_id` в одном чате Leo; если анкету видели оба аккаунта, в `profile_messages` накапливались id обоих, и `forward_messages(from_peer=Leo)` с чужим id резолвился в посторонние сообщения — владельцу переслало чужие переписки. Строки с пустой сессией (legacy) не отбрасываются.
- **Manual Review (Stage 8)** (`services/manual_review.py`): когда скоринг выдаёт REVIEW, бот **не действует сам** — пересылает карточку владельцу и ждёт его ручного решения. Исходящее `❤️`/`👎` владельца перехватывается и записывается в файл `data/reviews/review_log.json`/`.md` (только для активных REVIEW-анкет).
- **Control Panel (Stage 7.5)** (`telegram/control_bot.py`): `/status /mode on|off /stream /recent /msgs /top /help` (+ inline-кнопки) только от `control.allowed_user_ids`; слушает **все** `telegram.accounts`; режим меняется на лету и персистится в `config.yaml`.
- **Аналитика лайков (Stage 8.5)**: собирается «что я написал при лайке и ответила ли девушка». Авто-текст «Берем)» — в `auto_actions_log.message_text`, ручные действия владельца («❤️»→LIKE, «👎»→DISLIKE, текст→MESSAGE) — в `sent_messages` (`_handle_outgoing_message`); сигнал ответа — MATCH «Начинай общаться 👉 Имя» → `match_responses` (привязка к профилю по имени, `UNIQUE(chat_id, tm_id)`). **Результат на каждый лайк** (авто/ручной) — таблица `like_outcomes`: содержание сообщения (`message_text`, '' если без текста) и `responded` (0/1 — лайкнула в ответ). Отклонённые из-за лимита Leo лайки («Слишком много ❤️ за сегодня», детектор `is_like_rejection`) помечаются `rejected=1` и исключаются из аналитики (`mark_like_rejected`). **Лимит ❤️ исчерпан (Stage 8.5.2)**: того же отклонения мало — раньше бот продолжал слать ❤️ (все отклонялись) и открывал композер «Берем)» после отклонённого лайка (Leo: «Нет такого варианта ответа»). Теперь при `is_like_rejection` `AutoActionEngine.handle_like_rejection(chat_id)` ставит `likes_exhausted_until` = ближайшие **07:00 локального времени** (в `app_state`, переживает рестарт), снимает незавершённые цепочки «Берем)» и **один раз** уведомляет владельца. В `maybe_act` при исчерпанном лимите `LIKE_AND_MESSAGE` не отправляется (возврат `"LIKE_LIMIT"`), а **👎 продолжает уходить** — иначе лента Leo замирает. Отчёты: `/msgs` — конверсия текстов (`sent`/`responded`/`rate`), `/top` — топ девушек по лайкам (+ ответила ли, последнее сообщение).
- **Web UI (Stage 9)** (`web/`): **один экран без вкладок** — Flask + Jinja2 + HTML/CSS/JS в daemon-потоке `main.py`. Бизнес-логики нет: читает существующую SQLite через `web/db.py` SyncDB и записывает ручные решения в `human_decisions`. Главная страница — `/` (синоним `/chat`): лента — прямое дублирование чата Leo (одно сообщение = одна строка; анкеты и их фото — теми же сообщениями, что в Telegram, без карточек/бейджей); сверху — переключатель режима OBSERVE/SEMI_AUTO/AUTO (переключается на лету), кнопка ⚙ настройки — drawer режима, аккоунта-исполнителя, фильтры и `SKIP/LIKE`-предпочтения. Карточек/кнопок ❤️/👎 и inline-форм капч в ленте **больше нет** (лента — только чат); маршруты `/chat/profile/<id>/<LIKE|DISLIKE>` (записывает ручное решение и отправляет реакцию через `web/actions.py` → `AutoActionEngine`/`manual_reaction`) и ответ на капчу из блока «Капчи ждут ответа» остаются рабочими. Блок «⚠️ Капчи ждут ответ» над лентой: неизвестные капчи Leo — ответ владельца сохраняется в `captcha_memory` и сразу отправляется Leo (`send_captcha_answer_sync`), разблокировая ленту. Автоообножения без перезаписивов: `/chat`, `/dashboard`, `/settings`, `/profiles/<id>` — 302 на `/`; legacy API роут `/dashboard/action/<id>/<action>` сохранён. Переключение режима и аккоунта работают **на лету** через `web/actions.py` (`set_mode_sync` / `set_account_sync`): режим персистится в `config.yaml` и сразу меняет живой `AutoActionEngine` (без рестарта) — см. блок «Web UI». **Красный баннер сбоя автопотока** (Stage 8.6): если бот не смог продолжить ленту анкет (`app_state.stream_error`) — над лентой выводится заметное предупреждение (`partials/app_stream_error.html`), владельцу уходит сообщение в Telegram.
- **SAFE по умолчанию**: режимы `project.mode` (OBSERVE / SEMI_AUTO / AUTO). `OBSERVE` только наблюдает и рекомендует; авто-действия включаются только явно.

---
- **Проверка фото на сигареты/алкоголь** (`services/photo_vice.py`, `PhotoViceDetector`; конфиг `photo_check`): правило владельца — сигарета/вейп/кальян или алкоголь на фото → 👎. Zero-shot CLIP ViT-L-14 (laion2b) на CPU: фото сравнивается с фразами «порок» и похожими безобидными сценами (кофе, телефон, газировка), softmax по всем фразам. Проверяются ТОЛЬКО анкеты с решением LIKE и только перед авто-действием (`DvinchikCollector._apply_photo_vice`: фото карточки + альбом по `grouped_id`, `download_media(bytes)`, расчёт в `asyncio.to_thread`); найдено выше строгого порога (`smoke_threshold` 0.99, `alcohol_threshold` 0.97) → `DecisionService.apply_photo_vice` пишет НОВОЕ решение DISLIKE с причиной `PHOTO_VICE:<smoke|alcohol>:<p>` («На фото сигарета/вейп (99%)»). Любая ошибка (нет `torch`/`open_clip_torch` — `requirements-ml.txt`, сеть, битое фото) решение не меняет. По умолчанию выключено. Метки «🚬/🍷 на фото» со страницы разметки (`photo_ratings.rating='VICE'`, клавиша `↑`) — данные для дообучения детектора.
- **Модель вкуса по фото — ТЕНЕВОЙ режим** (`services/photo_taste.py`: `PhotoTasteModel`, `PhotoShadowScorer`; общий CLIP — `services/clip_encoder.py`, `ClipEncoder`; конфиг `photo_taste`): логистическая регрессия поверх CLIP ViT-L-14 (среднее нормированных эмбеддингов фото карточки → sigmoid), обучена на разметке `/rate` (офлайн 2026-10-22: 1391 анкета, 168 ❤️, AUC 0.83 на отложенных). Веса — JSON `data/ml/taste_model.json` на сервере (в git нет: обучено на личных данных): `weights`, `bias`, `like_threshold` (верхние 15%), `dislike_threshold` (нижние 50%). Для КАЖДОЙ новой карточки с фото коллектор в фоне (`_schedule_photo_shadow`, очередь ≤ `max_pending`, лишние пропускаются) скачивает до `max_photos` фото, считает score и зону LIKE/REVIEW/DISLIKE + вероятности «сигарета»/«алкоголь» и пишет в `photo_scores`. **На действия бота не влияет** — для сравнения с решениями владельца. Проверка сигарет/алкоголя для будущего лайка берёт готовый теневой результат этой карточки (ждёт до 15 с), а не гоняет CLIP второй раз. Сервер: CPU, ~1.5 с на фото (4 потока), модель ~3.7 ГБ RAM при загрузке; веса CLIP — в HF-кэше пользователя сервиса (`/home/dvai/.cache/huggingface`).
- **Ошибки бота → Telegram и сайт** (`collectors/error_reporter.py`, `BotErrorReporter`, подключается в `main.py`): sink loguru уровня ERROR ловит ЛЮБУЮ ошибку бота (в т.ч. непредусмотренную), кладёт её в `app_state.bot_error` (JSON: `at`, `where` — модуль:функция:строка, `message`, `count`) и шлёт владельцу «⚠️ Ошибка бота …» на твинк — тот же получатель, что у уведомлений о лайках (`AutoActionEngine.notify_owner`); одинаковая ошибка (без чисел) — не чаще раза в 10 минут, с числом повторов. Ошибки самого репортёра не репортятся (петля), строки access-лога веб-сервера (`werkzeug`, «1.2.3.4 - - [...] code 400» от сканеров) — тоже (`BotErrorReporter.is_noise`). На сайте — всплывающее окно `#bot-error` на главной (данные в `/chat/feed` JSON `bot_error`, время ошибки входит в сигнатуру `/chat/new-count`; `app.js showBotError`, только `textContent`); «Понятно» запоминает время ошибки в `localStorage` (`dvai.botErrorSeen`) — та же ошибка больше не всплывает, новая всплывёт. Повод — прод 2026-10-23: бот встал на первой анкете, которую модель фото решила лайкнуть (`UnboundLocalError 'images'` в проверке сигарет/алкоголя оборвал авто-действие), а ошибка была видна только в journalctl. Теперь фото-проверки (`_apply_photo_taste`, `_apply_photo_vice`) обёрнуты: любая их ошибка оставляет решение как есть.
- **Модель вкуса РЕШАЕТ лайк — режим `photo_taste.mode: decide`** (одобрено владельцем 2026-10-23; по умолчанию `shadow`): `DvinchikCollector._apply_photo_taste` перед авто-действием берёт результат теневого расчёта этой карточки (кэш `_photo_shadow_results` или ожидание задачи до 15 с) и, если в решении нет текстовых запретов/сильного плюса (`DecisionService.photo_taste_applies`: `USER_SKIP`, `HARD_NEGATIVE`, `FILTER_REJECTED`, `USER_LIKE_HARD`, `PHOTO_VICE` — они главнее), пишет новое решение `DecisionService.apply_photo_taste`: score ≥ порога «верхние `like_share` анкет» (`like_thresholds` из обучения, по умолчанию 20%) → LIKE, иначе DISLIKE (причина `PHOTO_TASTE:<like|dislike>:<score>`, «Фото во вкусе (71%)»). REVIEW в этом режиме не возникает — владельца не дёргаем, лента не встаёт. Пустые анкеты (раньше `LOW_INFO_SKIP`) и «информативные» (раньше лайк за длину текста) решает фото. **Видео-анкета** (~1 из 9 карточек; в Telegram — документ `video/*`) оценивается по превью-кадру видео (`_card_photos`, `download_media(thumb=-1)`). Нет вердикта по карточке — оценка по отпечатку фото прошлого показа (`photo_embeddings`, `PhotoShadowScorer.score_embedding`, без CLIP); нет и его (новая видео-анкета без превью, CLIP недоступен) — решает старое правило. Затем для LIKE — проверка сигарет/алкоголя. Скорость: на сервере `max_photos: 2` (AUC 0.814 против 0.832 по 3 фото, ~3 с вместо ~4.5 с) и `photo_check.threads: 6` — Leo сам листает карточки, а устаревшая карточка не получает реакцию (`STALE`).
- **Дообучение модели вкуса (active learning)**: (1) **фоновый досчёт** `DvinchikCollector.run_photo_backfill` (таск из `main.py`, `photo_taste.backfill`, пауза `backfill_pause_sec`) берёт анкеты с фото без отпечатка (`Database.get_photo_backfill_batch`: сначала НЕоценённые на /rate, затем оценённые; фото последнего показа, первые `max_photos`), фото — из кэша `media/` или скачивает аккаунтом-получателем и кладёт в кэш (`_backfill_photo_bytes`), считает вердикт → `photo_scores` и **отпечаток** (среднее нормированных CLIP-эмбеддингов, float32) → `photo_embeddings` (его пишет и живая тень); не удалось получить фото — отметка `emb NULL`, чтобы не крутиться. (2) **Очередь /rate «модель сомневается»**: `SyncDB.get_rating_queue` ставит первыми неоценённые анкеты с последним score ближе всего к 0.5, каждая `RATE_EXPLORE_EVERY`=5-я — из обычного перемешанного порядка ВСЕХ неоценённых (после досчёта вердикт есть у всех, поэтому «разведка» берётся не из анкет без вердикта); такие анкеты помечены «🤔 модель сомневается» (`#rate-badge`). (3) **Переобучение** `scripts/train_photo_taste.py` (на сервере: `venv/bin/python scripts/train_photo_taste.py [--dry-run|--force]`): `photo_embeddings` × `photo_ratings` → логистическая регрессия, честный out-of-fold AUC (5×5), заменяет `data/ml/taste_model.json` только если новая модель не хуже текущей **на одних и тех же новых оценках**, поставленных после обучения текущей (`trained_at` в JSON; старая модель против out-of-fold прогнозов новой, допуск 0.01). Общий AUC для этого не годится: очередь даёт сначала пограничные анкеты, и он падает от трудных примеров, а не от ухудшения модели (прод 2026-10-23: общий 0.828 → 0.799, а на 506 новых оценках новая 0.688 против старой 0.639); старый JSON сохраняется рядом, запись атомарная; после замены пересчитывает последний вердикт каждой анкеты в `photo_scores` новой моделью по отпечаткам (без CLIP, `--no-rescore` — не трогать), чтобы очередь «сомневается» строилась по свежей модели. Бот подхватывает новый файл без рестарта (`PhotoTasteModel._maybe_reload`, проверка mtime раз в 30 с; битый файл — остаётся прежняя модель). Нужен `scikit-learn` (`requirements-ml.txt`).
- **Проверка «<имя>, ты живешь в Питере?» [Да, Нет]**: бот всегда отвечает «Нет» (`DvinchikCollector._answer_city_check_if_needed`, шаг `_continue_stream_if_needed` после отказа от Premium) — только если это самая свежая клавиатура Leo и после неё нет нашего ответа; иначе лента вставала до ручного ответа.
- **Стикер-привет после взаимного лайка** (`collectors/match_greeter.py`, `MatchGreeter`): на «Начинай общаться 👉 [Имя](ссылка)» бот сам пишет девушке в личку — ТОЛЬКО стикер из набора `auto_actions.match_greeting.sticker_set` (по умолчанию `Flame`, стикер с эмодзи из `sticker_emojis` (👋, затем 🔥) или по номеру `sticker_index`); текст Leo «Привет! Я с Дайвинчика ✨» не отправляется. ВСЕМ взаимным лайкам — и после лайка бота, и после ручного лайка владельца (любой режим из `modes`, по умолчанию все три, включая OBSERVE), с того аккаунта, где случился матч, один раз на девушку и аккаунт (`app_state` `match_greeting:<session>:<username|user_id>` = JSON статуса), если в её чате уже есть наше исходящее — не пишет. Ссылка без @username (`tg://user?id=…`) тоже распознаётся как MATCH (`ParsedMatch.telegram_user_id`). Ошибка отправки помечается и не повторяется — кроме **спам-блока** (`PeerFloodError`, «Too many requests» — аккаунту нельзя писать незнакомым) и `FloodWaitError`: девушка НЕ помечается, аккаунт уходит на паузу (`app_state` `match_greeting_pause:<session>` = ISO-срок, 24 ч для PeerFlood / срок FloodWait + 60 с), девушка — в очередь `match_greeting_queue:<session>` (JSON, до 100); пока пауза, незнакомым не пишем вовсе (статус `PAUSED`); владелец получает одно уведомление «⛔ … в спам-блоке» (статус `SPAM_BLOCK`), в лог — WARNING, а не ERROR. После паузы `DvinchikCollector.retry_match_greetings` (из `run_history_sync`, раз в 60 с) дописывает очереди (`MatchGreeter.retry_queued`); снова блок — девушки возвращаются в очередь; недоступный набор стикеров девушку не помечает. Владелец получает уведомление «🔥 Взаимный лайк: …». Вызов — `DvinchikCollector._greet_match` из MATCH-ветки `_process_message`. Тесты: `tests/test_match_greeter.py`.

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
   → ActionPolicyResolver(decision, informative) → LIKE_AND_MESSAGE / DISLIKE_ONLY / NO_ACTION
   → AutoActionEngine.maybe_act(policy)  # только SEMI_AUTO/AUTO (❤️/👎/«Берем)»/REVIEW-уведомление)
   → AutoActionEngine.step_pending(text)  # цепочка «Берем)»: 💌 📹 🎤 → «Отправь текст…» → «Берем)»
```

### Компоненты

| Зона | Процесс | Компоненты | Контакт с Telegram |
|---|---|---|---|
| **Collector** | сбор и сохранение | `dvinchik_collector`, `raw_worker`, `raw_queue`, `dedup`, `city_normalizer`, `stats`, `auto_action` | ✅ Telethon |
| **Scoring (детерминированный)** | решение | `filter_engine`, `filter_service`, `profile_normalizer`, `feature_extractor`, `score_engine`, `decision_service`, `app/preferences` | ❌ Telegram-free |
| **Manual review + экспорт** | ручные решения по REVIEW-анкетам, экспорт анализа | `manual_review`, `analysis_export` | ❌ Telegram-free |
| **Telegram UI** | вывод и управление | `control_bot` | ✅ Telethon |
| **Web UI (Stage 9)** | веб-интерфейс | `web/` (Flask, SyncDB, фото-кэш), `static/`, шаблоны | ⚠️ только фото по запросу |

> Единственные слои с Telethon: `collectors/dvinchik_collector.py`, `collectors/auto_action.py`, `telegram/`. Всё остальное — чистая логика (Profile/str/Config), тестируется без живого Telegram.

### Структура проекта

```
dvdatin/
├── main.py                      # Точка входа: конфиг, сборка стека, цикл, --export-analysis/--clear-db
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
├── telegram/                    # client.py (Telethon), control_bot.py
├── models/                      # raw.py, profile.py, filter.py, features.py, decision.py, human_decision.py
├── services/                    # Telegram-free бизнес-логика (см. таблицу выше)
├── collectors/                  # см. таблицу выше
├── web/                         # Stage 9 Web UI: Flask (create_app, blueprints, SyncDB, photos)
├── static/                      # CSS/JS для Web UI
├── tests/                       # 744 тестов, baseline в tests/baseline/
├── deploy/                      # systemd unit + runbook
├── proxy/                       # vendored xray-core + VLESS (НЕ коммитить)
└── data/                        # БД, сессии, логи, экспорт (gitignored)
```

### База данных (SQLite, WAL)

Схема идемпотентна (`CREATE TABLE IF NOT EXISTS`), внешние ключи через PRAGMA.

| Таблица | Назначение |
|---|---|
| `raw_messages` | Сырые сообщения чата (append-only; включает `raw_entities`, `reply_markup`, `is_outgoing`, `account_session` — аккаунт-получатель); UNIQUE `(account_session, chat_id, telegram_message_id, is_outgoing)` |
| `profiles` | Профили (name/age/city/description/fingerprint/status…) |
| `profile_messages` | Связь профиль ↔ сообщения (в т.ч. MEDIA_ONLY); `account_session` — аккаунт-получатель (для скачивания фото тем клиентом, кто реально видел сообщение) |
| `chat_context` | Контекст «последняя анкета» СТРОГО на аккаунт (`PRIMARY KEY (account_session, chat_id)`): у аккаунтов свой чат с Leo в одном `chat_id` |
| `filter_results` | История фильтрации (PASS/REJECT/REVIEW) |
| `ai_decisions` | Решения DecisionService (LIKE/REVIEW/DISLIKE; `scoring_version=deterministic-v2`) |
| `auto_actions_log` | Отправленные действия per-card + kind (`chat_id`+`telegram_message_id`+`action`): LIKE/DISLIKE/MESSAGE; `message_text` — текст сообщения при лайке («Берем)») |
| `match_responses` | Взаимные лайки (Stage 8.5): девушка ответила — привязка к `profile_id` по имени; `UNIQUE(chat_id, telegram_message_id)` |
| `sent_messages` | Ручные действия владельца (Stage 8.5, `source='manual'`): `action` LIKE («❤️» — сам лайкнул) / DISLIKE («👎») / MESSAGE (текст) |
| `like_outcomes` | Результат на КАЖДЫЙ лайк (Stage 8.5): `message_text` (содержание сообщения, '' если без текста) + `responded` (0/1 — лайкнула в ответ); бэкфилл из `auto_actions_log`/`sent_messages`; `UNIQUE(source, chat_id, like_tm_id)` |
| `captcha_memory` | Память капч (Stage 7.6): текст капчи Leo по сигнатуре (нормализованный текст) с выученным ответом владельца (`answer`); `NULL` = ждёт ответа на сайте; `used_count` — сколько раз ответ применялся |
| `app_state` | Рантайм key/value, переживающий рестарт (Stage 8.5.2/8.6): `likes_exhausted_until:<account_session>` — (лимит у каждого аккаунта свой) timestamp, до которого дневной лимит ❤️ исчерпан; `stream_error` — JSON-снимок сбоя автопотока (бот не может листать анкеты) для баннера на сайте |
| `photo_scores` | Теневой вердикт модели вкуса на карточку (`services/photo_taste.py`): `profile_id`, `account_session`, `chat_id`, `telegram_message_id` (UNIQUE втроём), `score` 0..1, `verdict` LIKE/REVIEW/DISLIKE, `smoke_prob`/`alcohol_prob` (максимум по фото), `n_photos`, `model_version`, `created_at`. На действия не влияет |
| `photo_embeddings` | Отпечаток фото анкеты для переобучения модели вкуса: `profile_id` (PK), `emb` (float32 LE BLOB, среднее нормированных CLIP-эмбеддингов фото последнего показа; NULL — фото получить не удалось), `n_photos`, `model`, `created_at`. Пишут фоновый досчёт и живая тень, читает `scripts/train_photo_taste.py` |
| `photo_ratings` | Разметка внешности на сайте (`/rate`): `profile_id` (PK, одна оценка на анкету, повтор перезаписывает, «Назад» удаляет), `rating` = `LIKE`/`DISLIKE`/`SKIP`/`VICE` (VICE — на фото сигарета/алкоголь), `created_at`. Обучающие метки для фото-модели вкуса владельца; на действия бота не влияют |
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
    enabled: true             # ❤️ — первый шаг цепочки «Берем)»
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
- **Восстановление «зависшего» композера + сбой автопотока** (Stage 8.6): после рестарта цепочка `LIKE_AND_MESSAGE` теряется — Leo уже открыл композер («Отправь текст, видео или голосовое…») и ждёт текст, а `_pending_chains` пуст, поэтому бот молчит и лента стоит. `start_auto_stream()` при исчерпании всех путей продолжения читает последнее сообщение (`DvinchikCollector._last_incoming_text`) и, если это запрос композера (`AutoActionEngine.is_composer_prompt`), сам досылает «Берем)» (`recover_open_composer` → `_send_composer_text`, `_pick_message_text`) — лента разблокируется. Если восстановить нельзя (`like_message.enabled=false`) или в `start_auto_stream` была ошибка, коллектор пишет сбой в `app_state.stream_error` (JSON: `message`, `reason`, `snapshot` последних сообщений, `at`; `_report_stream_stuck`) и один раз в `STREAM_ERROR_NOTIFY_COOLDOWN_SEC` (600 с) шлёт предупреждение владельцу через `AutoActionEngine.notify_owner` (тот же получатель, что и у обычных уведомлений: `notify_chat_id` или «другой» аккаунт). Сбой автоматически снимается (`_clear_stream_error`), как только лента снова пошла (любой путь `start_auto_stream` вернул `True`). Веб-панель показывает красный баннер (`app_state.stream_error` → `web/db.SyncDB.get_stream_error()` → `partials/app_stream_error.html`, обновляется polling'ом через `/chat/feed`; `stream_error_at` входит в сигнатуру `/chat/new-count`).
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

- **лента — плоская, одна колонка**: контейнер `.app` max-width 860px по центру, контейнер `.feed` — flex-column, строка `.row` — grid «время (46px) + содержимое (`minmax(0, 1fr)`)». Каждая строка = одно сообщение чата (текст или миниатюра фото). Без выравнивания по краям и подгонки под содержимое — из-за них строки разъезжались, а центр пустовал (регресс-тест `test_feed_is_single_vertical_column`);
- **вид ленты** — переключатель `#view-seg` в панели управления: **«1 · Прямой»** (зеркало Telegram-чата, по умолчанию) и **«2 · Улучшенный»** — только анкеты, на которые бот поставил ❤️ или 👎, с блоком «Почему» (человеко-читаемые причины решения) и текстом, отправленным при лайке. Выбор — ссылка `?view=direct|smart`, запоминается в сессии (`session['feed_view']`, `_feed_view` в `web/blueprints/chat.py`), polling `/chat/feed` отдаёт тот же вид (`view` в JSON). Данные — `SyncDB.get_decision_feed`: `auto_actions_log` (LIKE/DISLIKE на карточку) + решение `ai_decisions` на момент действия (последнее до `sent_at`), «Берем)» — строка `MESSAGE` той же карточки, фото — карточка + альбом той же анкеты (`_decision_photos`); аккаунт — строго, через входящую карточку в `raw_messages`. Шаблон `partials/app_decisions.html`. Перевод кодов причин — `services/reason_labels.py` (`describe_reasons`, Telegram-free), он же используется в Telegram-уведомлениях (`AutoActionEngine._format_reason`); служебные коды (`FILTER_REJECTED`, `AGE_OK`…) скрыты;
- **панель управления лентой**: фильтр по решению (Все / ❤️ / 🤔 / 👎) и поиск по имени-городу работают на клиенте по `data-decision` / `data-name` строк, поэтому перерисовка ленты polling-ом не сбрасывает выбор; счётчик показывает «N из M»;
- **выбор режима** в топбаре — `OBSERVE` / `SEMI_AUTO` / `AUTO`, текущий подсвечен, POST на `/settings/mode` (переключение на лету);
- **кнопки ❤️/👎** у анкеты — **только если AI-решение `REVIEW`** и человек ещё не решил; POST на `/chat/profile/<id>/<LIKE|DISLIKE>` (CSRF + серверный гейт по `get_latest_ai_decision`, `is_already_reviewed` → `save_human_decision` + реакция в чат Leo);
- **аккаунт-исполнитель** — селект в топбаре рядом с режимом (`#account-form`, авто-сабмит при выборе, кнопка `ОК` как fallback без JS); в drawer настроек его больше нет;
- **переключатель темы** — кнопка в топбаре (тёмная ⇄ светлая), выбор в `localStorage`, применяется **до первой отрисовки** (`web/templates/partials/theme_boot.html`), работает и на `/login`;
- **⚙ настройки** — одна кнопка открывает drawer поверх ленты (только фильтры и `SKIP/LIKE`-предпочтения);
- **🖥 отладка** — кнопка в топбаре (`#debug-open`) открывает второй, широкий drawer (`partials/app_debug.html`) со **снимком состояния** (режим, аккаунт-исполнитель, сбой автопотока `stream_error`, `likes_exhausted_until`, счётчики таблиц, сигналы ленты, последние 10 авто-действий) и **последними логами из памяти**; обновляется polling-ом `GET /chat/debug` (`?limit=1..500&level=WARNING|ERROR`), фильтр уровня и кнопка `⟳` — в шапке панели; Esc закрывает сначала её. Источник логов — кольцевой буфер `app/logging.LOG_BUFFER` (`deque(maxlen=500)`, sink `_buffer_sink`); туда же через `_InterceptHandler` попадают ошибки/предупреждения stdlib-библиотек (asyncio/telethon/werkzeug) уровня `WARNING+`;
- **🖼 разметка внешности** — кнопка в топбаре (`#rate-open`) ведёт на отдельную страницу `/rate` (`web/blueprints/rate.py`, `web/templates/rate.html`, `static/js/rate.js`): показываются **только фото** анкеты (до 4, с одного аккаунта-получателя, без текста — метка отражает внешность), оценка кнопками или клавишами `←` нет / `→` нравится / `↓` пропустить / `Backspace` назад; **на телефоне — как в Tinder**: одно большое фото, тап по краю листает фото анкеты (полоски сверху), свайп вправо — нравится, влево — нет (карточка `#rate-card` тянется за пальцем через pointer events, `touch-action: pan-y`). Оценка сохраняется сразу в `photo_ratings`; очередь — неоценённые анкеты с фото в перемешанном стабильном порядке, анкеты с фото уже в кэше `media/` идут первыми (не дёргаем Telegram), браузер держит очередь из ≥6 анкет и заранее грузит их фото. Проверка «сообщение — фото» обязана идти по индексу `idx_raw_unique` (условие `rm.account_session IN (pm.account_session, '')`): без него запрос сканировал всю `raw_messages` (прод: 7.9 с вместо 0.02 с) и между анкетами висело «Загружаю…». Браузер помнит все анкеты вкладки (`seen`) и не ставит анкету в очередь повторно (раньше список для сервера резался до 150 id, после ~150 оценок терялась сама очередь и анкеты шли по второму кругу); клавиши перехватываются на `window` в фазе захвата, автоповтор зажатой клавиши игнорируется, кнопки не забирают фокус при клике. API: `GET /rate/next?limit=&exclude=` (JSON), `POST /rate/<id>` (`rating`, `csrf_token`), `POST /rate/<id>/undo`. Метки нужны для обучения модели «вкус по фото» и на действия бота не влияют;
- **блок «⚠️ Капчи ждут ответ»** над лентой (`captcha_memory` с `answer IS NULL`) — виден всегда, независимо от пагинации: ответ (с подсказками из кнопок Leo, POST `/chat/captcha/<id>/answer`) и «забыть» (POST `/chat/captcha/<id>/delete`).

Лента обновляется без перезагрузки: polling `/chat/new-count` (сигнатура raw∪sent∪auto∪captcha, **на аккаунт-исполнитель**) → подмена фрагмента `/chat/feed`. Сигнатура считается по выбранному аккаунту, поэтому активность соседнего аккаунта (тот же `chat_id`) не дёргает чужую ленту. Обновление перерисовывает **все ранее загруженные страницы** (1..N) **одним запросом** `/chat/feed?page=1&pages=N` (раньше страницы тянулись параллельно по OFFSET, и новое сообщение между запросами дублировало/теряло строки на стыке) — подгруженная история не схлопывается, позиция скролла сохраняется. Время строк — в **местном** времени браузера (`<time datetime=…>` + `localizeTimes` в `app.js`; в БД `message_date` в UTC). Всегда открывается на **свежих** сообщениях (пагинация «с конца»: `page=1` — новые, `page=2` — более ранние, «Показать более ранние» (под лентой) дописывает их вниз, `FEED_PER_PAGE = 40`).


### Лента = прямое дублирование Telegram-чата

Лента `/` — **зеркало чата с Leo**: ОДНО сообщение Telegram = ОДНА строка, в хронологическом порядке. Коллектор пишет ВЕСЬ чат в `raw_messages` (RAW-first), и лента читает только её — и входящие от Leo, и наши исходящие. Никаких склеек с `sent_messages`/`auto_actions_log`: это **журналы действий**, а не события чата (их `telegram_message_id` указывает на карточку анкеты, а не на сообщение-реакцию, из-за чего лента съезжала на соседнюю анкету).

- **Фильтр по аккаунту-исполнителю — СТРОГИЙ**: у каждого аккаунта свой чат с Leo в ОДНОМ `chat_id` (свои диапазоны message id: `dvai` ~708k, `dvai_2` ~22k). `raw_messages.account_session` (миграция `_ensure_raw_account_session_column`) ставится коллектором при сохранении; `get_chat_feed(..., account_session=...)` показывает строки РОВНО выбранного аккаунта (legacy `''` не подмешивается — иначе соседний аккаунт «протекает» в ленту). Иначе сообщения двух аккаунтов перемешивались («супер-чат», не совпадающий ни с одним TG). **Историю размечает `Database.backfill_raw_account_session(sender_to_session)`** (вызывается из `main.py` после `get_me()` по каждому клиенту): исходящие — по `sender_id` (user id аккаунта), входящие — по ближайшему диапазону id исходящих аккаунта (пространства разнесены, разрыв огромный).
- **Парсинг строго на аккаунт**: дедуп (`_dedup`), лок чата (`_chat_locks`), контекст анкеты (`_pending_profiles`) и `chat_context` (таблица, `PRIMARY KEY (account_session, chat_id)`) ключуются парой `(account_session, chat_id)` — иначе MEDIA_ONLY одного аккаунта привязывался к карточке другого (у них разные пространства message id). Фото качается **только** аккаунтом-получателем (`web/photos._ordered_clients` — без fallback на чужой аккаунт, который тем же id вернул бы фото другой анкеты).
- **`raw_messages.is_outgoing`** — ставится перехватчиком исходящих (`_handle_outgoing_message`), чтобы наш `👎`/`❤️` не выглядел как сообщение от Leo.
- **Наши исходящие пишет и сам движок** (`AutoActionEngine._persist_outgoing`, вызывается из `_send`): коллектор ловит исходящие эхо-событием Telethon (`NewMessage(outgoing=True)`), но оно приходит не всегда (прод-замер 2026-10-19: 311 авто-действий за день, лишь 7 исходящих в `raw_messages`) — 👎/❤️/кнопки/«Берем)» пропадали из ленты, и порядок «карточка → реакция → сообщение» разъезжался (лента не совпадала с Telegram). Движок знает `send_message(...).id` и сохраняет сообщение сам (`is_outgoing=True`, `account_session`); дубликат с эхо-событием схлопывает `idx_raw_unique` (`INSERT OR IGNORE`). Уведомления владельцу (`_notify*`) шлются напрямую `client.send_message(target=…)`, мимо `_send`, поэтому в ленту Leo не попадают.
- **Сверка с Telegram** (`DvinchikCollector.sync_chat_history` / фоновая `run_history_sync`, запускается из `main.py`): раз в `HISTORY_SYNC_INTERVAL_SEC` (60 с) по КАЖДОМУ аккаунту читаются последние `HISTORY_SYNC_LIMIT` (100) сообщений чата Leo и недостающие досохраняются в `raw_messages` (`INSERT OR IGNORE`, верные `account_session`/`is_outgoing`, сразу `processed` — pipeline для прошлого не запускается). Без неё лента состояла только из того, что Telethon доставил событием: при обрывах прокси/рестартах и для ручных ответов с телефона в ленте были дыры и «разброс». Сообщения моложе `HISTORY_SYNC_MIN_AGE_SEC` (120 с) не трогаются — их сохраняет живой хендлер (иначе сверка «отняла» бы карточку у pipeline).
- **уникальный индекс включает аккаунт и направление**: `idx_raw_unique` = `(account_session, chat_id, telegram_message_id, is_outgoing)`. Номера входящих и исходящих пересекаются, а у аккаунтов свои пространства id; старый `UNIQUE(chat_id, telegram_message_id)` молча отбрасывал наши исходящие на `INSERT OR IGNORE`. Индекс пересоздаётся при старте (`DROP` + `CREATE`).
- **Аккаунт сообщения** определяется по имени сессии, с которым `main.py` создал клиент (`client_sessions`), а не по позиции в `telegram.accounts` — иначе при невошедшем первом аккаунте сообщения второго записывались первому.
- **Уведомления о дизлайках отключаемы**: `auto_actions.notify_dislikes: false` — 👎 по-прежнему уходит Leo, но анкета владельцу не пересылается (при живой ленте это пересылка каждые ~2 с). Лайки и «нужно твоё решение» приходят всегда.
- **Меньше запросов к Telegram**: проверки кнопок Leo (карточка лайка, «Смотреть анкеты», Premium, меню) делят одну выборку последних 15 сообщений (`_recent_history`) вместо двух скачиваний на каждую — меньше риск FloodWait.
- **Чистка 2026-10-21**: удалены мёртвые методы `web/db.py` (`get_profiles_paginated`, `get_profile_full`, `count_decisions`, `count_human_decisions`, `get_pending_review_count`, `get_feed_signals`, `set_captcha_answer_and_mark`), `web/photos.py` (`set_telegram_client`, `get_telegram_client`, `get_all_photo_paths`), дубль `ControlBot._cmd_status`, недостижимая ветка повтора лайка `AWAIT_LIKE_ACK`; из зависимостей убран неиспользуемый `pysocks` (прокси идёт через `python-socks`); `data/reviews/` добавлен в `.gitignore`. Таблица `ai_scores` (наследие LLM-оценки) кодом не используется — в старых базах лежит мёртвым грузом.
- **Только чат Leo** (`dvinchik.chat_id`, `_leo_chat_id` в `web/blueprints/chat.py`): лента и сигнатура polling фильтруются по `chat_id`. В `raw_messages` есть сообщения других чатов (история до allowlist'а: группы, личка) — их номера из чужих пространств id (например 262k против ~34k у `dvai_2`), и без фильтра они вставали «самыми свежими» между анкетами. Кнопка «Показать более ранние» — под лентой.
- **Анкета-альбом = одна строка** (`SyncDB._group_albums`): Leo шлёт анкету с N фото альбомом (фото + подпись, затем N-1 фото без текста); строки-фото без текста той же анкеты дописываются в `photos` предыдущей строки — как карточка в Telegram (раньше «безымянные» фото выглядели как лишние анкеты). Видео/документ помечается «🎬 видео». **Порядок — от новых к старым**: свежее СВЕРХУ, сразу под панелью управления (топбар), более ранние ниже; страница открывается сверху (`history.scrollRestoration = 'manual'`). Альбомы склеиваются в хронологическом порядке, затем лента разворачивается — внутри анкеты фото по порядку, подпись под ними. При polling, если пользователь листает историю ниже, позиция держится по дельте высоты (новые строки встают сверху).
- **пустые сообщения пропускаются** (нет текста и нет медиа — кнопки меню). Фото-сообщение рисуется миниатюрой: `profile_id` резолвится по `profile_messages` (для маршрута `photos.serve_photo`); анкета = те же сообщения, что в Telegram (карточек/бейджей/форм в ленте нет).
- **`data-decision`/`data-name`** остаются скрытыми атрибутами строк для клиентского фильтра и поиска.
- **пагинация «с конца»**: `page=1` — последние сообщения, на странице от новых к старым (свежее сверху), `FEED_PER_PAGE = 40`; порядок — по `telegram_message_id` (нативный порядок чата Telegram), затем `id`.

Регресс-тесты: `TestChatFeedMirror` — входящее и исходящее отдельными строками, аннотация не добавляет вторую строку, аннотация без сырого сообщения не показывается, входящее и исходящее с одним номером сохраняются оба, фото знает свою анкету, `total` считает сообщения, пустое сообщение пропускается, фильтр по аккаунту, обход всех страниц без дублей. `test_auto_action.py::TestOutgoingPersisted` — 👍/кнопка сохраняются в `raw_messages` (`is_outgoing=True`, `account_session`), отсутствие `db`/ошибка сохранения/сообщение без id не ломают отправку.

Старые адреса сохранены как **редиректы** на главный экран (чтобы старые закладки не отдавали 404): `/chat` (синоним `/`), `/dashboard`, `/settings`, `/profiles/<id>`, `/profiles/<id>/action`; legacy API-роут быстрых действий `/dashboard/action/<id>/<action>` остался для обратной совместимости. Шаблоны `chat.html` / `dashboard.html` / `settings.html` / `profile.html` / `captchas.html`, blueprint `web/blueprints/captchas.py` и `static/css/style.css` удалены (Stage 9). Подробнее в `web/` и `AGENTS.md`.

**Дизайн** (Stage 9.2, переписан с нуля): **тёмная и светлая темы**, статика без внешних зависимостей — `static/css/app.css` + `static/js/app.js`. Плотная лента-зеркало чата (без «пузырей», без карточек/бейджей), фильтр по решению и поиск, миниатюры фото (192×256), SVG-иконки ⚙/выход, toast-уведомления, drawer настроек. `data-decision`/`data-name` — скрытые атрибуты строк для клиентского фильтра и поиска.

Тема: палитра задана CSS-переменными (`:root` — тёмная, `[data-theme="light"]` — светлая), переключатель `#theme-toggle` в топбаре пишет выбор в `localStorage`, а `web/templates/partials/theme_boot.html` применяет его инлайн-скриптом **до первой отрисовки** (иначе при перезагрузке мигает тёмная тема). За пределами палитр сырых цветов в `app.css` нет — это проверяет `TestTheme::test_no_raw_colors_outside_palettes`, иначе в светлой теме остались бы тёмные пятна.

Жёсткие правила вёрстки (проверяются тестами и числовым аудитом в headless-браузере, `_audit.py`):

- **топбар — CSS-grid** `minmax(0, 1fr) auto` (левая группа тянется, правая прижата к краю и не может выехать за экран); на узких экранах (<620px) переносится на две строки;
- **лента — одна колонка** `.feed { display: flex; flex-direction: column; align-items: stretch; gap: 8px }` внутри `.app { max-width: 860px; margin: 0 auto }`, строка `.row { display: grid; grid-template-columns: 46px minmax(0, 1fr); width: 100% }` — без выравнивания по краям и подгонки под содержимое; аудит проверяет, что все строки одной ширины и с одним `x`;
- **высота страницы**: реальные 40 событий занимают ~4600px вместо ~22000px в чат-вёрстке — строки плотные (фото 192×256, шрифт 13px, gap 8px);
- **`min-width: 0` во всей flex/grid-цепочке** + `minmax(0, 1fr)` в колонках + `overflow-wrap: anywhere` — длинное слово, имя или ссылка в анкете не растягивают ленту (плюс `overflow-x: hidden` как страховка);
- **фото анкеты — фиксированные миниатюры** `.thumbs__item { width: 192px; height: 256px }` + `object-fit: cover`: высота не зависит от загрузки файла и его исходного разрешения, вёрстка не «прыгает», три фото занимают одну строку; битые/удалённые фото убирает `app.js` (пустая `.thumbs` скрывается через `:empty`);
- **кликабельные элементы ≥ 30px** (`.btn`, `.icon-btn`, `.input` — 36px);
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
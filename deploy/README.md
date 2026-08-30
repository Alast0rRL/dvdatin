# DvAI — Deploy Runbook (Ubuntu)

Готово к деплою в OBSERVE-режиме: Telegram collector → SQLite → RawQueue →
Worker → Remote AI (Ollama + CLIP через прокладку). Никаких自动ических
Telegram-действий (OBSERVE/REVIEW гарантирован архитектурно).

## 1. Подготовка сервера

```bash
# Клонировать (или скопировать архив репозитория)
sudo useradd -m -s /bin/bash dvai
sudo usermod -aG dvai dvai

sudo -u dvai -i
cd /opt && git clone <repo> dvai && cd dvai

# Виртуальное окружение
python3 -m venv venv
./venv/bin/pip install -U pip
./venv/bin/pip install -r requirements.txt
# (опционально, для тестов/сверки baseline)
./venv/bin/pip install -r requirements-dev.txt
```

## 2. Конфигурация (СЕКРЕТЫ — вне git)

`config/config.yaml` уже в `.gitignore`. Создать из шаблона и заполнить:

```bash
cp config/config.example.yaml config/config.yaml
nano config/config.yaml
```

Обязательно заполнить:
- `telegram.api_id`, `telegram.api_hash`, `telegram.phone`
- `dvinchik.chat_id` (Дайвинчик, дефолт 1234060895)
- `sources.allowed_chat_ids` (seed из `dvinchik.chat_id`)
- `ai.enabled: true` + `ai.backend: remote` + `ai.remote.base_url`/`api_key`
- при необходимости `telegram.proxy.*`

> ⚠️ `proxy/` (vendored xray + реальный VLESS) НЕ коммитится — деплоится
> отдельно (scp). Не добавляйте его в git.

## 3. Remote AI sidecar (Ollama + CLIP на GPU)

AI-сервер слушает `http://144.31.139.206:8000` (SSH reverse tunnel → Ubuntu
AI Server). Убедитесь, что туннель поднят и эндпоинты отвечают:

```bash
curl -s http://144.31.139.206:8000/health
# ожидается HTTP 200
```

При падении шлюза DvAI деградирует до `AI_UNAVAILABLE → REVIEW` (без краша).

## 4. Запуск через systemd

```bash
sudo cp deploy/dvai.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dvai
sudo journalctl -u dvai -f
```

Ручной запуск (без systemd):
```bash
./deploy/run.sh
```

Экспорт review-датасета в CSV (без Telegram):
```bash
./venv/bin/python main.py --export-review
```

## 5. Soak test (OBSERVE, ничего автоматически не делает)

Запустить и оставить на часы/сутки. Снимать метрики:

| Метрика | Где смотреть |
|---|---|
| Кол-во RAW | `data/database.db`: `SELECT COUNT(*) FROM raw_messages;` |
| Обработано | `SELECT COUNT(*) FROM raw_messages WHERE processed_at IS NOT NULL;` |
| REVIEW | AI-решения: `SELECT decision, COUNT(*) FROM ai_decisions GROUP BY decision;` |
| AI_UNAVAILABLE | логи `grep "AI_UNAVAILABLE"` / поле reasons в ai_decisions |
| Ошибки БД | `journalctl -u dvai \| grep -i "error"` (RAW save retry/permanent) |
| Backlog | `SELECT COUNT(*) FROM raw_messages WHERE processed_at IS NULL;` |
| Размер очереди | логи worker / `stats.qsize` |
| Память / CPU / GPU | `htop`, `nvidia-smi` |
| Повторная обработка | сравнить `raw_messages.id` до/после restart (W3 не дублирует) |
| MEDIA | `SELECT COUNT(*) FROM profile_messages WHERE telegram_message_id IN (SELECT id FROM raw_messages WHERE media_type<>'');` |
| Human review | `/review` в Telegram (ReviewBot) |

Сверка тестов после soak (regression):
```bash
./venv/bin/python -m pytest tests/ -q   # ожидается 387 passed
diff <(grep '::' tests/baseline/baseline_tests.txt | sort) \
     <(./venv/bin/python -m pytest tests/ --collect-only -q | grep '::' | sort)
```

## 6. Shutdown

`systemctl stop dvai` — корректно: cancel recovery → drain worker (sentinel) →
закрытие AI-клиентов → disconnect Telegram → close БД.

## 7. Файлы деплоя

- `deploy/dvai.service` — systemd unit
- `deploy/run.sh` — ручной запуск (UTF-8)
- `requirements.txt` / `requirements-dev.txt` — зависимости
- `tests/baseline/` — frozen baseline (387 tests)

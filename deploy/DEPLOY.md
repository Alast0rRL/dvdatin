# DvAI — Быстрый деплой на сервер (Ubuntu, 144.31.139.206)

> Шпаргалка «для себя в будущем»: как обновить код и перезапустить прод.
> Все сервисы и настройки уже развёрнуты — деплой сводится к `git pull` + `systemctl restart`.

## 1. Доступ по SSH

```bash
ssh root@144.31.139.206 -p 2222 -i ~/.ssh/id_ed25519
```

- Хост: `144.31.139.206`, порт **2222** (проверено, root + ключ `id_ed25519`).
- Проект: `/opt/dvai`, владелец `dvai`, репозиторий подключён к `origin/main`
  (GitHub: `https://github.com/Alast0rRL/dvdatin.git`).
- В `~/.ssh/config` на Windows уже есть алиасы на этот хост: `server_tunel`
  (порт 23) и `Senko`. Рабочий прямой вход — `root@144.31.139.206 -p 2222`.

## 2. Структура на сервере

| Путь | Что это |
|---|---|
| `/opt/dvai` | клон репозитория (git, `main`) + `venv/` |
| `/opt/dvai/config/config.yaml` | **gitignored** — секреты (api_id/hash/phone), прокси, режим |
| `/opt/dvai/proxy/` | vendored xray + реальный VLESS (`config.json`), **gitignored** |
| `/opt/dvinchik-ai` | AI-шлюз (FastAPI/uvicorn), слушает `127.0.0.1:8000` |

## 3. Сервисы (systemd)

| Сервис | Что делает |
|---|---|
| `dvai.service` | Telegram-коллектор `main.py` (режим из `config.yaml`) |
| `dvai-proxy.service` | SOCKS5-прокси xray (`xray26 run -config proxy/config.json`) |
| `dvinchik-ai.service` | AI-шлюз `uvicorn app.main:app --port 8000` |
| `ollama.service` | Ollama (LLM qwen3:8b) |
| `ollama-tunnel.service` | autossh reverse tunnel (Ollama) |
| `ai-tunnel.service` | autossh reverse tunnel (AI, порт 8000) |
| `dvai-login.service` / `dvai-fifo-hold.service` | интерактивный login-проход (не для штатной работы) |

Вспомогательные (не трогать при обычном деплое): `dvai-login`, `dvai-fifo-hold`,
`ai-tunnel`, `ollama-tunnel`, `ollama`, `dvinchik-ai`, `dvai-proxy` — их
перезапускать не нужно, если код DvAI менялся только в `/opt/dvai`.

## 4. Как обновить (стандартный деплой)

`config/config.yaml` и `proxy/` не перезаписываются — они вне git. Поэтому
`git pull` безопасен и не потеряет секреты/режим.

```bash
# 1) Локально: закоммитить и запушить
git add -A
git commit -m "..."
git push origin main

# 2) На сервере (root):
ssh root@144.31.139.206 -p 2222

cd /opt/dvai
git pull origin main
# если менялись зависимости (редко):
#   ./venv/bin/pip install -r requirements.txt

# 3) Быстрая проверка перед рестартом (импорты + регрессия):
./venv/bin/python -m pytest tests/test_collector.py::TestCollectorAutoActions tests/test_ai.py::TestHPWhitelist -q

# 4) Перезапуск и проверка:
systemctl restart dvai
systemctl is-active dvai                       # active
journalctl -u dvai -n 25 --no-pager           # смотрим логи запуска
journalctl -u dvai --since '...' | grep -iE 'error|traceback|exception'  # нет ошибок
```

`dvai.service`:
- `ExecStartPre` проверяет валидность YAML (`config/config.yaml`) fail-fast.
- `Restart=on-failure`, UTF-8 (PYTHONUTF8=1).
- `SuccessExitStatus=0` — штатный Ctrl+C не уходит в crash-loop.

## 5. Как разворачивалось изначально (архитектура, для справки)

- Windows (локальный, где разрабатывается код) и Ubuntu `144.31.139.206` —
  связаны reverse-SSH тоннелями (в `/root/.ssh/config` на Ubuntu: `vps-range`,
  `vps-ai` `RemoteForward 0.0.0.0:8000 127.0.0.1:8000`, `vps-ollama`
  `RemoteForward 0.0.0.0:11434 127.0.0.1:11435`).
- DvAI зовёт AI через `http://144.31.139.206:8000` — это публичный IP Ubuntu,
  где `dvinchik-ai.service` (uvicorn) отвечает на `127.0.0.1:8000`.
- Прокси: `dvai-proxy.service` (xray, VLESS-Reality-XHTTP) → SOCKS5 `127.0.0.1:10808`.
- Режим на сервере — `config.yaml → project.mode` (сейчас `OBSERVE`; SEMI_AUTO
  включает авто-действия). Режим меняется через ControlBot `/mode` или правкой
  `config.yaml` (переживает restart).

## 6. Полезное

- Полный лог: `journalctl -u dvai -f`
- Экспорт review-датасета: `/opt/dvai/venv/bin/python /opt/dvai/main.py --export-review`
- Сверка вручную после деплоя: `git -C /opt/dvai log --oneline -3` должен
  показывать последний локальный коммит; `git -C /opt/dvai status -sb` — без конфликтов.

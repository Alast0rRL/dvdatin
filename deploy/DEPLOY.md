# DvAI — Деплой на сервер (Ubuntu, 144.31.118.3)

> Шпаргалка «для себя в будущем»: как обновить код и перезапустить прод.
> Сервисы и настройки уже развёрнуты — обычный деплой сводится к `git pull` + `systemctl restart dvai`.

## 1. Доступ по SSH

```bash
ssh root@144.31.118.3 -p 2200 -i ~/.ssh/homekey
```

- Хост: `144.31.118.3`, порт **2200**, пользователь **root**, ключ **`homekey`**
  (`C:\Users\Hunter\.ssh\homekey` на машине разработки, **уже создан**).
- Публичный ключ `homekey.pub` добавлен в `/root/.ssh/authorized_keys` на сервере.
- Проект: `/opt/dvai`, владелец `dvai`, origin/main = GitHub `https://github.com/Alast0rRL/dvdatin.git`.

## 2. Структура на сервере

| Путь | Что это |
|---|---|
| `/opt/dvai` | клон репозитория (git, `main`) + `venv/` (Python 3.14) |
| `/opt/dvai/config/config.yaml` | **gitignored** — секреты (api_id/hash/phone), прокси, режим |
| `/opt/dvai/proxy/xray` | xray-core (Linux, v26.x), путь запуска — GitHub releases |
| `/opt/dvai/proxy/config.json` | **gitignored** — VLESS-XHTTP-конфиг (SOCKS5 → server.redline.surf:443) |
| `/opt/dvai/data/sessions/*.session` | **gitignored** — сессии Telegram-аккаунтов (сейчас `dvai.session`) |
| `/opt/dvai/data/database.db` | **gitignored** — SQLite (RAW-сообщения, профили, решения) |

## 3. Сервисы (systemd)

| Сервис | Что делает |
|---|---|
| `dvai.service` | Telegram-коллектор `main.py` (режим из `config.yaml`) |
| `dvai-proxy.service` | SOCKS5 `127.0.0.1:10808` → xray `run -config proxy/config.json` |

Старые AI-службы (`dvinchik-ai`, `ollama`, туннели) **удалены из архитектуры** —
детерминированный scoring (Stage 8) не использует LLM/CLIP. Не создавать заново.

## 4. Доступ к Telegram (важно!)

Прямой интернет сервера до Telegram **не доходит** (подсеть заблокирована:
`api.telegram.org` и DC по IP отвечают 000). Telegram доступен ТОЛЬКО через
SOCKS5-туннель `127.0.0.1:10808` (xray, VLESS-XHTTP к `server.redline.surf:443`).

- Конфиг туннеля — `proxy/config.json` (вне git). Если VPN-подписка redline
  протухла/сменилась — заменить outbound на новый `vless://`-линк:
  - `address`/`port`/`id` (uuid),
  - `network: xhttp`, `security: none`, `path`, `extra.mode: auto`,
    `extra.xPaddingBytes`.

## 5. Как обновить (стандартный деплой)

`config.yaml` и `proxy/` не перезаписываются — они вне git, поэтому `git pull` безопасен.

```bash
# 1) Локально: закоммитить и запушить
git add -A
git commit -m "..."
git push origin main

# 2) На сервере (root):
ssh root@144.31.118.3 -p 2200 -i ~/.ssh/homekey

cd /opt/dvai
git pull origin main
# если менялись зависимости (редко):
#   ./venv/bin/pip install -r requirements.txt

# 3) Быстрая проверка перед рестартом (импорты + регрессия):
./venv/bin/python -m pytest tests/test_collector.py::TestCollectorAutoActions tests/test_ai.py::TestHPWhitelist -q

# 4) Перезапуск и проверка:
systemctl restart dvai proxy        # если трогали прокси: systemctl restart dvai-proxy
systemctl is-active dvai            # active
journalctl -u dvai -n 25 --no-pager # смотрим логи запуска
```

`dvai.service`:
- `ExecStartPre` проверяет валидность YAML (`config/config.yaml`) fail-fast.
- `Restart=on-failure`, UTF-8 (PYTHONUTF8=1), `SuccessExitStatus=0` (Ctrl+C без crash-loop).

## 6. Аккаунты Telegram

`config.yaml → telegram.accounts`:
- **acc1** (`dvai`, +79375003342, melancholic) — сессия `data/sessions/dvai.session` уже на сервере.
- **acc2** (`dvai_2`, +79997621114, авто-аккаунт `auto_actions.account_session`) —
  сессии со старого сервера нет; для входа нужен интерактивный login-прогон:

```bash
systemctl stop dvai
cd /opt/dvai && PYTHONUTF8=1 ./venv/bin/python main.py
# ввести код из SMS/Telegram для +79997621114, после входа Ctrl+C
systemctl start dvai
```

## 7. Как разворачивалось изначально (bootstrap с нуля)

```bash
apt update && apt install -y git python3 python3-venv python3-pip curl unzip jq
useradd -r -s /usr/sbin/nologin dvai
git clone https://github.com/Alast0rRL/dvdatin.git /opt/dvai
chown -R dvai: /opt/dvai
cd /opt/dvai && python3 -m venv venv && ./venv/bin/pip install -r requirements.txt

# секреты (вне git): config/config.yaml, proxy/config.json — залить scp'ом с машины разработки
curl -L https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip -o /tmp/xray.zip
cd /tmp && unzip -o xray.zip xray && install -m 755 xray /opt/dvai/proxy/xray

# юниты: deploy/dvai.service → /etc/systemd/system/dvai.service
#        dvai-proxy.service (см. текст юнита ниже) → /etc/systemd/system/dvai-proxy.service
systemctl daemon-reload && systemctl enable --now dvai-proxy dvai
```

`dvai-proxy.service`:
```ini
[Unit]
Description=DvAI Xray Proxy (SOCKS5 127.0.0.1:10808)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/opt/dvai/proxy/xray run -config /opt/dvai/proxy/config.json
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=dvai-proxy

[Install]
WantedBy=multi-user.target
```

## 8. Полезное

- Полный лог: `journalctl -u dvai -f`
- Экспорт review-датасета: `/opt/dvai/venv/bin/python /opt/dvai/main.py --export-review`
- Сверка после деплоя: `git -C /opt/dvai log --oneline -3`, `git -C /opt/dvai status -sb`
- Проверка туннеля: `curl -x socks5h://127.0.0.1:10808 -s -o /dev/null -w '%{http_code}' https://api.telegram.org` (ожидание: `302`)
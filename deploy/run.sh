#!/usr/bin/env bash
# Запуск DvAI вручную (вместо systemd), с корректной UTF-8 средой.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
exec "$(dirname "$0")/../venv/bin/python" main.py "$@"

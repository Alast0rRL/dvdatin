# Settings blueprint — filter config, preferences, mode control.

from __future__ import annotations

from pathlib import Path

import yaml
from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for

from web.blueprints.auth import login_required
from web.db import SyncDB

settings_bp = Blueprint("settings", __name__)

CONFIG_PATH = Path("config/config.yaml")
PREFERENCES_PATH = Path("config/preferences.yaml")
PREFERENCES_EXAMPLE = Path("config/preferences.example.yaml")


def _get_db() -> SyncDB:
    return current_app.config["SYNC_DB"]


def _get_config_path() -> Path:
    from flask import current_app
    return current_app.config.get("CONFIG_PATH", CONFIG_PATH)


@settings_bp.route("/settings", methods=["GET"])
@login_required
def settings() -> str:
    db = _get_db()
    config_path = _get_config_path()

    # Filter config
    filter_cfg = db.get_filter_config(config_path)
    # Mode
    mode = db.get_mode(config_path)
    # Preferences
    prefs = _load_preferences(config_path.parent / "preferences.yaml")
    # Accounts (для выбора аккаунта-исполнителя авто-действий)
    accounts, account_session = _get_accounts(config_path)

    return render_template(
        "settings.html",
        filter_cfg=filter_cfg,
        mode=mode,
        preferences=prefs,
        accounts=accounts,
        account_session=account_session,
    )


@settings_bp.route("/settings/filters", methods=["POST"])
@login_required
def update_filters() -> tuple:
    """Обновляет настройки фильтров в config.yaml."""
    token = request.form.get("csrf_token", "")
    if token != session.get("_csrf_token"):
        flash("Ошибка безопасности", "error")
        return ("CSRF token invalid", 403)

    config_path = _get_config_path()
    age_min = request.form.get("age_min", "18", type=int)
    age_max = request.form.get("age_max", "19", type=int)
    city_raw = request.form.get("city_allowed", "")
    city_allowed = [c.strip() for c in city_raw.split(",") if c.strip()]

    try:
        _update_filters_in_yaml(config_path, age_min, age_max, city_allowed)
        flash("Фильтры обновлены", "success")
    except Exception as e:
        flash(f"Ошибка сохранения: {e}", "error")

    return redirect(url_for("settings.settings"))


@settings_bp.route("/settings/mode", methods=["POST"])
@login_required
def update_mode() -> tuple:
    """Переключает режим DvAI (OBSERVE/SEMI_AUTO/AUTO).

    Персистит режим в config.yaml (переживает restart) И — главное —
    переключает живой AutoActionEngine через бридж (web.actions),
    чтобы SEMI_AUTO заработал сразу, без рестарта приложения.
    """
    token = request.form.get("csrf_token", "")
    if token != session.get("_csrf_token"):
        flash("Ошибка безопасности", "error")
        return ("CSRF token invalid", 403)

    mode_str = request.form.get("mode", "OBSERVE")
    redirect_target = request.form.get("next", "")
    if not redirect_target or not redirect_target.startswith("/") or redirect_target.startswith("//"):
        redirect_target = url_for("settings.settings")

    from core.types import Mode
    try:
        mode = Mode(mode_str)
    except ValueError:
        flash(f"Неизвестный режим: {mode_str}", "error")
        return redirect(redirect_target)

    live_status = "OK"
    config_path = _get_config_path()
    try:
        from app.config import AppConfig
        config = AppConfig.load(config_path)
        config.persist_mode(config_path, mode)
    except Exception as e:
        flash(f"Ошибка: {e}", "error")
        return redirect(redirect_target)

    # Живое переключение: обновляем AutoActionEngine и пробуем запустить ленту.
    try:
        from web.actions import set_mode_sync
        live_status = set_mode_sync(mode)
    except Exception as e:
        from loguru import logger
        logger.error(f"Web mode switch: {type(e).__name__}: {e!r}")
        live_status = "ERROR"

    if live_status == "OK":
        flashed = f"Режим переключён на {mode.value} (активен сразу)"
    elif live_status == "ERROR":
        flashed = f"Режим сохранён, но не активирован на лету: {mode.value}"
    else:
        flashed = f"Режим переключён на {mode.value} (применится после перезапуска)"
    flash(flashed, "success" if live_status == "OK" else "warning")

    return redirect(redirect_target)


@settings_bp.route("/settings/account", methods=["POST"])
@login_required
def update_account() -> tuple:
    """Переключает аккаунт-исполнитель авто-действий (на лету)."""
    token = request.form.get("csrf_token", "")
    if token != session.get("_csrf_token"):
        flash("Ошибка безопасности", "error")
        return ("CSRF token invalid", 403)

    account_session = request.form.get("account_session", "")
    redirect_target = request.form.get("next", "")
    if not redirect_target or not redirect_target.startswith("/") or redirect_target.startswith("//"):
        redirect_target = url_for("settings.settings")

    config_path = _get_config_path()
    accounts, _ = _get_accounts(config_path)
    if account_session not in [a["session"] for a in accounts]:
        flash(f"Аккаунт {account_session!r} не найден", "error")
        return redirect(redirect_target)

    live_status = "OK"
    try:
        from app.config import AppConfig
        config = AppConfig.load(config_path)
        config.persist_account_session(config_path, account_session)
    except Exception as e:
        flash(f"Ошибка: {e}", "error")
        return redirect(redirect_target)

    # Живое переключение: обновляем AutoActionEngine и пробуем запустить ленту.
    try:
        from web.actions import set_account_sync
        live_status = set_account_sync(account_session)
    except Exception as e:
        from loguru import logger
        logger.error(f"Web account switch: {type(e).__name__}: {e!r}")
        live_status = "ERROR"

    if live_status == "OK":
        flashed = f"Аккаунт переключён на {account_session} (активен сразу)"
    elif live_status == "ERROR":
        flashed = f"Аккаунт сохранён, но не активирован на лету: {account_session}"
    else:
        flashed = f"Аккаунт переключён на {account_session} (применится после перезапуска)"
    flash(flashed, "success" if live_status == "OK" else "warning")

    return redirect(redirect_target)


@settings_bp.route("/settings/preferences", methods=["POST"])
@login_required
def update_preferences() -> tuple:
    """Обновляет preferences.yaml (SKIP/LIKE правила)."""
    token = request.form.get("csrf_token", "")
    if token != session.get("_csrf_token"):
        flash("Ошибка безопасности", "error")
        return redirect(url_for("settings.settings"))

    config_path = _get_config_path()
    prefs_path = config_path.parent / "preferences.yaml"

    skip_raw = request.form.get("skip_rules", "")
    like_raw = request.form.get("like_rules", "")

    try:
        prefs_data = _parse_preferences_text(skip_raw, like_raw)
        with open(prefs_path, "w", encoding="utf-8") as f:
            yaml.dump(prefs_data, f, allow_unicode=True, default_flow_style=False)
        flash("Предпочтения обновлены", "success")
    except Exception as e:
        flash(f"Ошибка: {e}", "error")

    return redirect(url_for("settings.settings"))


def _get_accounts(config_path: Path) -> tuple[list[dict], str]:
    """Возвращает список аккаунтов (session/phone) и текущий account_session.

    Читает config.yaml напрямую (без AppConfig-валидации порогов).
    """
    import yaml
    if not config_path.exists():
        return [], ""
    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    accounts_raw = raw.get("telegram", {}).get("accounts", [])
    accounts = []
    for acc in accounts_raw:
        session = acc.get("session") or ""
        if session:
            accounts.append({"session": session, "phone": acc.get("phone", "")})
    account_session = raw.get("auto_actions", {}).get("account_session", "")
    return accounts, account_session


def _load_preferences(prefs_path: Path) -> dict:
    """Загружает preferences.yaml."""
    if not prefs_path.exists():
        prefs_path = prefs_path.parent / "preferences.example.yaml"
    if not prefs_path.exists():
        return {"skip": [], "like": []}
    try:
        with open(prefs_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {"skip": [], "like": []}
    except Exception:
        return {"skip": [], "like": []}


def _update_filters_in_yaml(
    path: Path, age_min: int, age_max: int, city_allowed: list[str]
) -> None:
    """Обновляет секцию filters в config.yaml."""
    if path.exists():
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    else:
        raw = {}

    filters = raw.get("filters") or {}
    filters["age"] = {"min": age_min, "max": age_max}
    filters["city"] = {"allowed": city_allowed}
    raw["filters"] = filters

    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(raw, f, allow_unicode=True, default_flow_style=False)


def _parse_preferences_text(skip_raw: str, like_raw: str) -> dict:
    """Парсит текстовые правила SKIP/LIKE в формат preferences.yaml."""
    skip_rules = []
    for line in skip_raw.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 2:
            skip_rules.append({"label": parts[0], "match": parts[1:]})
        else:
            skip_rules.append({"label": parts[0], "match": [parts[0]]})

    like_rules = []
    for line in like_raw.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 2:
            like_rules.append({"label": parts[0], "match": parts[1:]})
        else:
            like_rules.append({"label": parts[0], "match": [parts[0]]})

    return {
        "skip": skip_rules,
        "like": like_rules,
        "scoring": {"skip_is_hard": True, "clip_cannot_override_skip": True},
    }


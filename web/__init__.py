# Web UI layer — Flask application factory.
#
# Архитектура:
#   Browser → Flask Web UI → существующие services → SQLite
#
# Flask НЕ содержит бизнес-логики. Все решения принимаются
# DecisionService / FilterEngine — Flask только
# отображает результаты и передаёт команды.

from __future__ import annotations

import os
import re
from pathlib import Path

from flask import Flask
from markupsafe import Markup, escape

from web.config import WebConfig

#: Корневая директория проекта (относительно web/__init__.py).
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def asset_version() -> str:
    """Версия статики = максимальный mtime файлов в ``static/``.

    Подставляется в ``?v=`` у CSS/JS, поэтому после деплоя браузер тянет
    свежую вёрстку, а не закэшированную (иначе пользователь видит «поплывший»
    старый интерфейс и не может открыть/закрыть панель).
    """
    static_dir = PROJECT_ROOT / "static"
    newest = 0.0
    for path in static_dir.rglob("*"):
        if path.is_file():
            newest = max(newest, path.stat().st_mtime)
    return str(int(newest))


#: Markdown-ссылка из текста Leo: «[кишлёнок](https://t.me/…)».
_MD_LINK = re.compile(r"\[([^\]\n]{1,200})\]\(((?:https?|tg)://[^\s)]{1,2000})\)")


def leo_text(text: str | None) -> Markup:
    """Текст сообщения Leo для ленты: экранирование + ссылки вместо разметки.

    Leo присылает ссылки markdown-разметкой, и лента показывала её сырой
    («[имя](https://t.me/…?text=%D0%9F…)»). Только http(s)/tg — другие
    схемы (javascript:) остаются текстом.
    """
    safe = str(escape(text or ""))

    def _link(m: re.Match[str]) -> str:
        return (
            f'<a class="row__link" href="{m.group(2)}" target="_blank" '
            f'rel="noopener noreferrer">{m.group(1)}</a>'
        )

    return Markup(_MD_LINK.sub(_link, safe))


def create_app(web_config: WebConfig | None = None) -> Flask:
    """Фабрика Flask-приложения.

    Принимает ``web_config`` для тестирования (allow testing с
    кастомными настройками). В продакшене берёт из ``WebConfig.from_env()``.
    """
    app = Flask(
        __name__,
        template_folder=str(PROJECT_ROOT / "web" / "templates"),
        static_folder=str(PROJECT_ROOT / "static"),
    )

    cfg = web_config or WebConfig.from_env()
    app.secret_key = cfg.secret_key
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    if cfg.cookie_secure:
        app.config["SESSION_COOKIE_SECURE"] = True

    app.config["WEB_CONFIG"] = cfg
    app.jinja_env.filters["leo_text"] = leo_text

    # Security headers
    @app.after_request
    def _set_security_headers(response):  # type: ignore[no-untyped-def]
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response

    # CSRF token generation (available in all templates)
    import secrets

    @app.before_request
    def _ensure_csrf_token():  # type: ignore[no-untyped-def]
        from flask import session
        if "_csrf_token" not in session:
            session["_csrf_token"] = secrets.token_hex(32)

    @app.context_processor
    def _inject_csrf_token():  # type: ignore[no-untyped-def]
        from flask import session
        return dict(csrf_token=session.get("_csrf_token", ""), asset_v=asset_version())

    # Register blueprints
    from web.blueprints.auth import auth_bp
    from web.blueprints.chat import chat_bp
    from web.blueprints.dashboard import dashboard_bp
    from web.blueprints.photos import photos_bp
    from web.blueprints.profiles import profiles_bp
    from web.blueprints.dialogs import dialogs_bp
    from web.blueprints.rate import rate_bp
    from web.blueprints.settings import settings_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(profiles_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(photos_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(rate_bp)
    app.register_blueprint(dialogs_bp)

    return app

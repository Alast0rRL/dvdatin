# Web UI layer — Flask application factory.
#
# Архитектура:
#   Browser → Flask Web UI → существующие services → SQLite
#
# Flask НЕ содержит бизнес-логики. Все решения принимаются
# DecisionService / FilterEngine / ReviewService — Flask только
# отображает результаты и передаёт команды.

from __future__ import annotations

import os
from pathlib import Path

from flask import Flask

from web.config import WebConfig

#: Корневая директория проекта (относительно web/__init__.py).
PROJECT_ROOT = Path(__file__).resolve().parent.parent


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
        return dict(csrf_token=session.get("_csrf_token", ""))

    # Register blueprints
    from web.blueprints.auth import auth_bp
    from web.blueprints.dashboard import dashboard_bp
    from web.blueprints.profiles import profiles_bp
    from web.blueprints.settings import settings_bp
    from web.blueprints.photos import photos_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(profiles_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(photos_bp)

    return app

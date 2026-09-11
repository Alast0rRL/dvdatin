# Web UI configuration — auth credentials, secret key, paths.

from __future__ import annotations

import os
from pathlib import Path

from werkzeug.security import generate_password_hash


class WebConfig:
    """Web UI настройки. Загружает из env-переменных или значений по умолчанию."""

    def __init__(
        self,
        secret_key: str | None = None,
        password_hash: str | None = None,
        host: str = "0.0.0.0",
        port: int = 5000,
        debug: bool = False,
        cookie_secure: bool = False,
    ) -> None:
        self.secret_key = secret_key or os.environ.get(
            "DVAI_WEB_SECRET", "dev-secret-change-me-in-production"
        )
        self.password_hash = password_hash or os.environ.get(
            "DVAI_WEB_PASSWORD_HASH",
            generate_password_hash("admin"),
        )
        self.host = host
        self.port = int(os.environ.get("DVAI_WEB_PORT", str(port)))
        self.debug = debug
        #: True только за TLS-реверс-прокси: без HTTPS Secure-cookie
        #: не отправляется браузером, и логин/CSRF ломаются.
        self.cookie_secure = cookie_secure or os.environ.get(
            "DVAI_WEB_COOKIE_SECURE", ""
        ).lower() in {"1", "true", "yes", "on"}

    @classmethod
    def from_env(cls) -> WebConfig:
        """Создаёт конфигурацию из переменных окружения."""
        return cls()

    def check_password(self, password: str) -> bool:
        """Проверяет пароль against stored hash."""
        from werkzeug.security import check_password_hash
        return check_password_hash(self.password_hash, password)

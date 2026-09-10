# Web UI тесты — Flask endpoints, auth, dashboard, profiles, settings.

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from werkzeug.security import generate_password_hash

from web import create_app
from web.config import WebConfig
from web.db import SyncDB


# ── Fixtures ──────────────────────────────────────────────────────

def make_config() -> WebConfig:
    return WebConfig(
        secret_key="test-secret-key",
        password_hash=generate_password_hash("testpass"),
        host="127.0.0.1",
        port=5000,
        debug=True,
    )


@pytest.fixture
def web_cfg() -> WebConfig:
    return make_config()


@pytest.fixture
def sync_db(tmp_path: Path):
    """Создаёт sync DB с тестовыми данными."""
    from database.database import Database
    db_path = tmp_path / "test_web.db"
    db = Database(path=db_path)
    loop = asyncio.get_event_loop()
    loop.run_until_complete(db.connect())

    # Вставляем тестовые данные
    loop.run_until_complete(_seed_test_data(db))

    sync = SyncDB(db_path)
    yield sync, db_path, db

    loop.run_until_complete(db.close())


async def _seed_test_data(db: Database) -> None:
    """Заполняет БД тестовыми профилями."""
    # Профиль 1: LIKE
    pid1 = await db.insert_profile(
        name="Алиса", age=19, raw_city="Санкт-Петербург",
        normalized_city="Санкт-Петербург",
        description="Люблю аниме и игры. Ищу друзей.",
        fingerprint="fp_1", source_chat_id=1234060895,
        source_message_id=101, first_seen_at="2025-01-01T00:00:00",
        last_seen_at="2025-01-01T00:00:00", status="NEW",
    )
    await db.save_filter_result(
        profile_id=pid1, decision="PASS",
        reasons=json.dumps(["AGE_OK", "CITY_OK"]),
        rules_checked=2, evaluated_at="2025-01-01T00:00:01",
    )
    ad1_id = await db.save_ai_decision(
        profile_id=pid1, decision="LIKE",
        combined_score=0.85, confidence=0.9,
        reasons=json.dumps(["INFORMATIVE_CLEAN", "POSITIVE:anime:"]),
        scoring_version="deterministic-v2",
        evaluated_at="2025-01-01T00:00:02",
    )
    await db.save_human_decision(
        profile_id=pid1, ai_decision_id=ad1_id,
        decision="APPROVE", agreement="AGREEMENT",
        created_at="2025-01-01T00:00:03",
    )

    # Профиль 2: REVIEW (pending)
    pid2 = await db.insert_profile(
        name="Барби", age=18, raw_city="Москва",
        normalized_city="Москва",
        description="Простая девочка",
        fingerprint="fp_2", source_chat_id=1234060895,
        source_message_id=102, first_seen_at="2025-01-02T00:00:00",
        last_seen_at="2025-01-02T00:00:00", status="NEW",
    )
    await db.save_filter_result(
        profile_id=pid2, decision="PASS",
        reasons=json.dumps(["AGE_OK"]),
        rules_checked=1, evaluated_at="2025-01-02T00:00:01",
    )
    await db.save_ai_decision(
        profile_id=pid2, decision="REVIEW",
        combined_score=0.45, confidence=0.5,
        reasons=json.dumps(["NO_FEATURES_FOUND"]),
        scoring_version="deterministic-v2",
        evaluated_at="2025-01-02T00:00:02",
    )

    # Профиль 3: DISLIKE
    pid3 = await db.insert_profile(
        name="Варвара", age=20, raw_city="Сургут",
        normalized_city="Сургут",
        description="Ищу друга/подругу",
        fingerprint="fp_3", source_chat_id=1234060895,
        source_message_id=103, first_seen_at="2025-01-03T00:00:00",
        last_seen_at="2025-01-03T00:00:00", status="NEW",
    )
    await db.save_filter_result(
        profile_id=pid3, decision="REJECT",
        reasons=json.dumps(["AGE_OUT_OF_RANGE", "CITY_OUT_OF_RANGE"]),
        rules_checked=2, evaluated_at="2025-01-03T00:00:01",
    )
    await db.save_ai_decision(
        profile_id=pid3, decision="DISLIKE",
        combined_score=0.2, confidence=0.8,
        reasons=json.dumps(["FILTER_REJECTED", "AGE_OUT_OF_RANGE", "CITY_OUT_OF_RANGE"]),
        scoring_version="deterministic-v2",
        evaluated_at="2025-01-03T00:00:02",
    )


@pytest.fixture
def client(sync_db, web_cfg, tmp_path: Path):
    """Flask test client с настроенной БД."""
    sync, db_path, _ = sync_db
    app = create_app(web_cfg)
    app.config["SYNC_DB"] = sync
    app.config["CONFIG_PATH"] = tmp_path / "config.yaml"
    app.config["TESTING"] = True

    # Создаём тестовый config.yaml
    import yaml
    cfg_data = {
        "telegram": {"accounts": [{"api_id": 12345, "api_hash": "testhash"}]},
        "project": {"mode": "OBSERVE"},
        "filters": {
            "age": {"min": 18, "max": 19},
            "city": {"allowed": ["Санкт-Петербург"]},
        },
    }
    with open(tmp_path / "config.yaml", "w", encoding="utf-8") as f:
        yaml.dump(cfg_data, f, allow_unicode=True)

    with app.test_client() as c:
        yield c


def _login(client) -> None:
    """Логинится через test client."""
    # Первый запрос — инициализация сессии + CSRF token
    client.get("/login")
    with client.session_transaction() as sess:
        token = sess.get("_csrf_token", "")
    client.post("/login", data={"password": "testpass", "csrf_token": token})


# ── Auth Tests ────────────────────────────────────────────────────

class TestLogin:
    """Тесты авторизации."""

    def test_login_page_renders(self, client) -> None:
        resp = client.get("/login")
        assert resp.status_code == 200
        assert b"login" in resp.data.lower() or "Войти" in resp.data.decode()

    def test_login_success(self, client) -> None:
        _login(client)
        resp = client.get("/")
        assert resp.status_code == 200

    def test_login_wrong_password(self, client) -> None:
        client.get("/login")
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token", "")
        resp = client.post("/login", data={"password": "wrong", "csrf_token": token})
        assert resp.status_code == 200
        assert "Неверный" in resp.data.decode()

    def test_protected_route_redirects(self, client) -> None:
        resp = client.get("/")
        assert resp.status_code == 302
        assert "/login" in resp.headers.get("Location", "")

    def test_protected_settings_redirects(self, client) -> None:
        resp = client.get("/settings")
        assert resp.status_code == 302
        assert "/login" in resp.headers.get("Location", "")

    def test_protected_profile_redirects(self, client) -> None:
        resp = client.get("/profiles/1")
        assert resp.status_code == 302
        assert "/login" in resp.headers.get("Location", "")

    def test_logout(self, client) -> None:
        _login(client)
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token", "")
        resp = client.post("/logout", data={"csrf_token": token})
        assert resp.status_code == 302
        assert "/login" in resp.headers.get("Location", "")

        # Теперь защищённые страницы недоступны
        resp = client.get("/")
        assert resp.status_code == 302
        assert "/login" in resp.headers.get("Location", "")

    def test_no_secrets_in_config(self, client, web_cfg) -> None:
        """Не раскрываем пароль/хеш через UI."""
        _login(client)
        resp = client.get("/settings")
        data = resp.data.decode()
        assert "testpass" not in data


# ── Dashboard Tests ──────────────────────────────────────────────

class TestDashboard:
    """Тесты Dashboard."""

    def test_dashboard_renders(self, client) -> None:
        _login(client)
        resp = client.get("/")
        assert resp.status_code == 200

    def test_dashboard_url_renders(self, client) -> None:
        _login(client)
        resp = client.get("/dashboard")
        assert resp.status_code == 200

    def test_profiles_displayed(self, client) -> None:
        _login(client)
        resp = client.get("/")
        data = resp.data.decode()
        # Все три профиля должны отображаться
        assert "Алиса" in data
        assert "Барби" in data
        assert "Варвара" in data

    def test_decision_badges(self, client) -> None:
        _login(client)
        resp = client.get("/")
        data = resp.data.decode()
        assert "LIKE" in data
        assert "REVIEW" in data
        assert "DISLIKE" in data

    def test_reasons_displayed(self, client) -> None:
        _login(client)
        resp = client.get("/")
        data = resp.data.decode()
        # Алиса: информативная анкета — позитивный признак "Аниме"
        assert "Аниме" in data or "anime" in data
        # Варвара: причины фильтра — возраст не подходит
        assert "Возраст" in data

    def test_stats_bar(self, client) -> None:
        _login(client)
        resp = client.get("/")
        data = resp.data.decode()
        assert "Анкет" in data  # Заголовок блока статистики

    def test_filter_by_decision(self, client) -> None:
        _login(client)
        resp = client.get("/?decision=LIKE")
        data = resp.data.decode()
        assert "Алиса" in data
        assert "Барби" not in data

    def test_review_action_buttons(self, client) -> None:
        _login(client)
        resp = client.get("/?decision=REVIEW")
        data = resp.data.decode()
        assert "LIKE" in data
        assert "DISLIKE" in data


# ── Quick Action Tests ───────────────────────────────────────────

class TestQuickAction:
    """Тесты быстрых действий (LIKE/DISLIKE) из ленты."""

    def _get_csrf(self, client) -> str:
        with client.session_transaction() as sess:
            return sess.get("_csrf_token", "")

    def test_quick_like(self, client, sync_db) -> None:
        sync, _, _ = sync_db
        _login(client)
        resp = client.get("/dashboard/action/2/LIKE")  # Барби = REVIEW
        assert resp.status_code == 200

        # Проверяем, что решение сохранилось
        hd = sync.get_human_decision(2)
        assert hd is not None
        assert hd["decision"] == "APPROVE"

    def test_quick_dislike(self, client, sync_db) -> None:
        sync, _, _ = sync_db
        _login(client)
        resp = client.get("/dashboard/action/2/DISLIKE")  # Барби = REVIEW
        assert resp.status_code == 200

        hd = sync.get_human_decision(2)
        assert hd is not None
        assert hd["decision"] == "REJECT"

    def test_quick_action_already_reviewed(self, client, sync_db) -> None:
        sync, _, _ = sync_db
        _login(client)
        # Сначала лайкаем
        client.get("/dashboard/action/2/LIKE")
        # Повторно — 409
        resp = client.get("/dashboard/action/2/LIKE")
        assert resp.status_code == 409

    def test_quick_action_non_review(self, client) -> None:
        _login(client)
        # Алиса = LIKE, не REVIEW
        resp = client.get("/dashboard/action/1/LIKE")
        assert resp.status_code == 400

    def test_quick_action_invalid(self, client) -> None:
        _login(client)
        resp = client.get("/dashboard/action/2/INVALID")
        assert resp.status_code == 400


# ── Profile Detail Tests ─────────────────────────────────────────

class TestProfileDetail:
    """Тесты страницы профиля."""

    def test_profile_renders(self, client) -> None:
        _login(client)
        resp = client.get("/profiles/1")
        assert resp.status_code == 200
        data = resp.data.decode()
        assert "Алиса" in data

    def test_profile_not_found(self, client) -> None:
        _login(client)
        resp = client.get("/profiles/9999")
        assert resp.status_code == 404

    def test_profile_shows_decision(self, client) -> None:
        _login(client)
        resp = client.get("/profiles/1")
        data = resp.data.decode()
        assert "LIKE" in data
        assert "0.850" in data

    def test_profile_shows_reasons(self, client) -> None:
        _login(client)
        resp = client.get("/profiles/1")
        data = resp.data.decode()
        assert "Аниме" in data or "anime" in data

    def test_profile_shows_filter(self, client) -> None:
        _login(client)
        resp = client.get("/profiles/3")
        data = resp.data.decode()
        assert "REJECT" in data
        assert "Возраст" in data

    def test_profile_shows_human_decision(self, client) -> None:
        _login(client)
        resp = client.get("/profiles/1")
        data = resp.data.decode()
        assert "APPROVE" in data

    def test_profile_review_buttons_for_pending(self, client) -> None:
        _login(client)
        resp = client.get("/profiles/2")
        data = resp.data.decode()
        assert "LIKE" in data
        assert "DISLIKE" in data

    def test_profile_action_like(self, client, sync_db) -> None:
        sync, _, _ = sync_db
        _login(client)
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token", "")
        resp = client.post(
            "/profiles/2/action",
            data={"action": "LIKE", "csrf_token": token},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        hd = sync.get_human_decision(2)
        assert hd is not None
        assert hd["decision"] == "APPROVE"

    def test_profile_action_dislike(self, client, sync_db) -> None:
        sync, _, _ = sync_db
        _login(client)
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token", "")
        resp = client.post(
            "/profiles/2/action",
            data={"action": "DISLIKE", "csrf_token": token},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        hd = sync.get_human_decision(2)
        assert hd is not None
        assert hd["decision"] == "REJECT"


# ── Settings Tests ───────────────────────────────────────────────

class TestSettings:
    """Тесты страницы настроек."""

    def test_settings_renders(self, client) -> None:
        _login(client)
        resp = client.get("/settings")
        assert resp.status_code == 200

    def test_settings_shows_current_values(self, client) -> None:
        _login(client)
        resp = client.get("/settings")
        data = resp.data.decode()
        assert "18" in data  # age_min
        assert "19" in data  # age_max

    def test_settings_shows_mode(self, client) -> None:
        _login(client)
        resp = client.get("/settings")
        data = resp.data.decode()
        assert "OBSERVE" in data

    def test_update_filters(self, client, tmp_path) -> None:
        _login(client)
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token", "")
        resp = client.post(
            "/settings/filters",
            data={
                "age_min": "18",
                "age_max": "25",
                "city_allowed": "Москва, Казань",
                "csrf_token": token,
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200

        # Проверяем, что файл обновился
        import yaml
        with open(tmp_path / "config.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        assert cfg["filters"]["age"]["max"] == 25
        assert "Москва" in cfg["filters"]["city"]["allowed"]

    def test_update_mode(self, client, tmp_path) -> None:
        _login(client)
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token", "")
        resp = client.post(
            "/settings/mode",
            data={"mode": "SEMI_AUTO", "csrf_token": token},
            follow_redirects=True,
        )
        assert resp.status_code == 200

        import yaml
        with open(tmp_path / "config.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        assert cfg["project"]["mode"] == "SEMI_AUTO"


# ── CSRF Protection Tests ────────────────────────────────────────

class TestCSRF:
    """Тесты CSRF-защиты."""

    def test_login_requires_csrf(self, client) -> None:
        resp = client.post("/login", data={"password": "testpass"})
        assert resp.status_code == 403

    def test_logout_requires_csrf(self, client) -> None:
        _login(client)
        resp = client.post("/logout")
        assert resp.status_code == 403

    def test_settings_update_requires_csrf(self, client) -> None:
        _login(client)
        resp = client.post(
            "/settings/filters",
            data={"age_min": "18", "age_max": "19", "city_allowed": ""},
        )
        assert resp.status_code == 403


# ── Security Headers Tests ──────────────────────────────────────

class TestSecurity:
    """Тесты безопасности."""

    def test_security_headers(self, client) -> None:
        _login(client)
        resp = client.get("/")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert resp.headers.get("X-XSS-Protection") == "1; mode=block"

    def test_no_secret_in_rendered_page(self, client) -> None:
        _login(client)
        resp = client.get("/settings")
        data = resp.data.decode()
        assert "test-secret-key" not in data


# ── SyncDB Tests ────────────────────────────────────────────────

class TestSyncDB:
    """Unit-тесты SyncDB (синхронный адаптер БД)."""

    def test_get_profiles_paginated(self, sync_db) -> None:
        sync, _, _ = sync_db
        profiles = sync.get_profiles_paginated(0, 10)
        assert len(profiles) == 3

    def test_get_profiles_count(self, sync_db) -> None:
        sync, _, _ = sync_db
        assert sync.get_profiles_count() == 3

    def test_get_profiles_count_filtered(self, sync_db) -> None:
        sync, _, _ = sync_db
        assert sync.get_profiles_count("LIKE") == 1
        assert sync.get_profiles_count("REVIEW") == 1
        assert sync.get_profiles_count("DISLIKE") == 1

    def test_get_profile_full(self, sync_db) -> None:
        sync, _, _ = sync_db
        profile = sync.get_profile_full(1)
        assert profile is not None
        assert profile["name"] == "Алиса"
        assert len(profile["ai_decisions"]) >= 1
        assert len(profile["filter_results"]) >= 1

    def test_get_profile_full_not_found(self, sync_db) -> None:
        sync, _, _ = sync_db
        assert sync.get_profile_full(9999) is None

    def test_count_decisions(self, sync_db) -> None:
        sync, _, _ = sync_db
        counts = sync.count_decisions()
        assert counts.get("LIKE") == 1
        assert counts.get("REVIEW") == 1
        assert counts.get("DISLIKE") == 1

    def test_pending_review_count(self, sync_db) -> None:
        sync, _, _ = sync_db
        assert sync.get_pending_review_count() == 1

    def test_is_already_reviewed(self, sync_db) -> None:
        sync, _, _ = sync_db
        # Алиса: AI decision ID = 1, уже reviewed
        ai_decisions = sync._query(
            "SELECT id FROM ai_decisions WHERE profile_id = 1"
        )
        assert sync.is_already_reviewed(ai_decisions[0]["id"])

    def test_save_human_decision(self, sync_db) -> None:
        sync, _, _ = sync_db
        # Барби: profile_id=2, ai_decision_id=2 (REVIEW)
        result = sync.save_human_decision(2, 2, "APPROVE", "AGREEMENT")
        assert result is not None
        hd = sync.get_human_decision(2)
        assert hd["decision"] == "APPROVE"

    def test_get_mode(self, sync_db, tmp_path) -> None:
        sync, _, _ = sync_db
        import yaml
        cfg_path = tmp_path / "config.yaml"
        with open(cfg_path, "w") as f:
            yaml.dump({"project": {"mode": "SEMI_AUTO"}}, f)
        assert sync.get_mode(cfg_path) == "SEMI_AUTO"

    def test_get_filter_config(self, sync_db, tmp_path) -> None:
        sync, _, _ = sync_db
        import yaml
        cfg_path = tmp_path / "config.yaml"
        with open(cfg_path, "w") as f:
            yaml.dump({
                "filters": {
                    "age": {"min": 18, "max": 25},
                    "city": {"allowed": ["Москва"]},
                }
            }, f)
        cfg = sync.get_filter_config(cfg_path)
        assert cfg["age_min"] == 18
        assert cfg["age_max"] == 25
        assert "Москва" in cfg["city_allowed"]

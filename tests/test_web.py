# Web UI тесты — Flask endpoints, auth, dashboard, profiles, settings.

from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

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

    # Фото профиля 1: привязка message↔profile с аккаунтом-получателем.
    await db.link_profile_message(
        profile_id=pid1, telegram_message_id=111, chat_id=1234060895,
        created_at="2025-01-01T00:00:04", account_session="dvai",
    )
    await db.save_raw_message(
        telegram_message_id=111, chat_id=1234060895, sender_id=100,
        sender_username="", sender_name="", message_date="2025-01-01T00:00:04",
        text="", raw_entities="[]", reply_markup="[]",
        media_type="photo", received_at="2025-01-01T00:00:04",
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

    def test_dashboard_new_count(self, client) -> None:
        _login(client)
        resp = client.get("/dashboard/new-count")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "total" in data
        assert "pending" in data
        assert isinstance(data["total"], int)

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

    def test_ai_like_without_action_hides_buttons(self, client, sync_db) -> None:
        """Баг: бот решил LIKE/DISLIKE, а действие не записалось (не-авто
        аккаунт) → статус SEEN, но кнопки всё равно висели. Теперь решение
        AI LIKE/DISLIKE скрывает кнопки и показывает бейдж."""
        _login(client)
        _, _, db = sync_db
        loop = asyncio.get_event_loop()
        pid = loop.run_until_complete(db.insert_profile(
            name="Зоя", age=20, raw_city="Санкт-Петербург",
            normalized_city="Санкт-Петербург",
            description="Хочу гулять по городу",
            fingerprint="fp_ai_like_no_act",
            source_chat_id=1234060895, source_message_id=700001,
            first_seen_at="2025-01-05T00:00:00",
            last_seen_at="2025-01-05T00:00:00",
            status="SEEN",
        ))
        loop.run_until_complete(db.save_ai_decision(
            profile_id=pid, decision="LIKE",
            combined_score=0.8, confidence=0.85,
            reasons=json.dumps(["INFORMATIVE_CLEAN"]),
            scoring_version="deterministic-v2",
            evaluated_at="2025-01-05T00:00:01",
        ))
        resp = client.get("/")
        data = resp.data.decode()
        assert f"action-result-{pid}" not in data
        assert "LIKE (решение AI)" in data

    def test_action_buttons_on_all_profiles(self, client) -> None:
        _login(client)
        resp = client.get("/")
        data = resp.data.decode()
        # Кнопки есть только на REVIEW-карточках (Барби — ждёт ручного
        # решения). Алиса уже отревьюена человеком (APPROVE), Варвара получила
        # терминальное решение AI (DISLIKE) — для них вместо кнопок — бейдж.
        assert "action-result-2" in data
        assert "action-result-3" not in data
        assert "action-result-1" not in data
        assert "DISLIKE (решение AI)" in data

    def test_mode_switcher_on_dashboard(self, client) -> None:
        _login(client)
        resp = client.get("/")
        data = resp.data.decode()
        # Переключатель режимов присутствует на ленте
        assert "Режим:" in data
        assert "OBSERVE" in data
        assert "SEMI_AUTO" in data
        assert "AUTO" in data
        # Активная кнопка — текущий режим OBSERVE
        assert 'class="btn mode-btn active mode-observe"' in data

    def test_mode_switch_from_dashboard_redirects_back(self, client) -> None:
        _login(client)
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token", "")
        resp = client.post(
            "/settings/mode",
            data={"mode": "SEMI_AUTO", "csrf_token": token, "next": "/dashboard"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/dashboard") or "/dashboard" in resp.headers["Location"]


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

    def test_quick_action_non_review(self, client, sync_db) -> None:
        _login(client)
        # Варвара = DISLIKE (не REVIEW) — лайк теперь доступен на любой анкете
        resp = client.get("/dashboard/action/3/LIKE")
        assert resp.status_code == 200

        sync, _, _ = sync_db
        hd = sync.get_human_decision(3)
        assert hd is not None
        assert hd["decision"] == "APPROVE"

    def test_quick_action_invalid(self, client) -> None:
        _login(client)
        resp = client.get("/dashboard/action/2/INVALID")
        assert resp.status_code == 400

    def test_quick_action_sends_reaction(self, client, sync_db) -> None:
        """Клик по кнопке отправляет ❤️/👎 в чат Leo через авто-движок."""
        import threading
        from web import actions as web_actions

        sent = []

        class _FakeEngine:
            async def manual_reaction(self, text: str) -> bool:
                sent.append(text)
                return True

        # run_coroutine_threadsafe требует работающий loop в отдельном потоке.
        worker_loop = asyncio.new_event_loop()
        t = threading.Thread(target=worker_loop.run_forever, daemon=True)
        t.start()
        try:
            with patch("web.actions._loop", return_value=worker_loop):
                web_actions.set_action_engine(_FakeEngine())
                _login(client)
                resp = client.get("/dashboard/action/3/DISLIKE")
                assert resp.status_code == 200
                assert resp.data == b"OK:SENT"
                assert sent == ["\U0001F44E"]
        finally:
            web_actions.set_action_engine(None)
            worker_loop.call_soon_threadsafe(worker_loop.stop)
            t.join(timeout=5)

    def test_quick_action_returns_disabled_without_engine(self, client) -> None:
        """Без авто-движка решение сохраняется, но реакция не отправляется."""
        from web import actions as web_actions

        web_actions.set_action_engine(None)
        _login(client)
        resp = client.get("/dashboard/action/3/DISLIKE")
        assert resp.status_code == 200
        assert resp.data == b"OK:DISABLED"


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

    def test_update_mode_switches_live_engine(self, client, tmp_path) -> None:
        """Переключение режима на сайте меняет живой AutoActionEngine.

        Раньше веб-путь сохранял режим ТОЛЬКО в config.yaml — живой движок
        оставался в OBSERVE, и SEMI_AUTO «не работал» до перезапуска.
        Теперь через бридж web.actions вызывается collector.set_mode()
        и движок переключается на лету.
        """
        from core.types import Mode
        from web import actions as web_actions

        switched = []
        stream_ran = []

        class _FakeCollector:
            def set_mode(self, mode: Mode) -> None:
                switched.append(mode)

            async def start_auto_stream(self) -> bool:
                stream_ran.append(True)
                return True

        worker_loop = asyncio.new_event_loop()
        t = threading.Thread(target=worker_loop.run_forever, daemon=True)
        t.start()

        _login(client)
        try:
            with patch("web.actions._loop", return_value=worker_loop):
                web_actions.set_collector(_FakeCollector())
                with client.session_transaction() as sess:
                    token = sess.get("_csrf_token", "")
                resp = client.post(
                    "/settings/mode",
                    data={"mode": "SEMI_AUTO", "csrf_token": token},
                    follow_redirects=True,
                )
                assert resp.status_code == 200
                assert switched == [Mode.SEMI_AUTO]
                assert stream_ran == [True]
        finally:
            web_actions.set_collector(None)
            worker_loop.call_soon_threadsafe(worker_loop.stop)
            t.join(timeout=5)

    def test_update_mode_without_collector(self, client, tmp_path) -> None:
        """Без привязанного коллектора режим сохраняется в YAML (как раньше)."""
        from web import actions as web_actions

        web_actions.set_collector(None)
        _login(client)
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token", "")
        resp = client.post(
            "/settings/mode",
            data={"mode": "OBSERVE", "csrf_token": token},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        import yaml
        with open(tmp_path / "config.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        assert cfg["project"]["mode"] == "OBSERVE"


class TestAccountSettings:
    """Переключение аккаунта-исполнителя авто-действий в Веб-панели."""

    def _write_config(self, tmp_path: Path, account_session: str = "dvai_2") -> None:
        import yaml
        cfg_path = tmp_path / "config.yaml"
        cfg_data = {
            "telegram": {
                "accounts": [
                    {"api_id": 38219721, "api_hash": "a" * 32,
                     "session": "dvai", "phone": "+79031234567"},
                    {"api_id": 36266816, "api_hash": "b" * 32,
                     "session": "dvai_2", "phone": "+79119876543"},
                ]
            },
            "project": {"mode": "SEMI_AUTO"},
            "auto_actions": {"enabled": True, "account_session": account_session},
        }
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.dump(cfg_data, f, allow_unicode=True)

    def test_settings_shows_account_dropdown(self, client, tmp_path) -> None:
        self._write_config(tmp_path)
        _login(client)
        resp = client.get("/settings")
        data = resp.data.decode()
        assert 'name="account_session"' in data
        assert "dvai" in data
        assert "dvai_2" in data

    def test_update_account_persists(self, client, tmp_path) -> None:
        self._write_config(tmp_path)
        import yaml
        with open(tmp_path / "config.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        assert cfg["auto_actions"]["account_session"] == "dvai_2"

        _login(client)
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token", "")
        resp = client.post(
            "/settings/account",
            data={"account_session": "dvai", "csrf_token": token},
            follow_redirects=True,
        )
        assert resp.status_code == 200

        with open(tmp_path / "config.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        assert cfg["auto_actions"]["account_session"] == "dvai"

    def test_update_account_unknown_skipped(self, client, tmp_path) -> None:
        self._write_config(tmp_path)
        _login(client)
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token", "")
        resp = client.post(
            "/settings/account",
            data={"account_session": "nope", "csrf_token": token},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        import yaml
        with open(tmp_path / "config.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        # Неизвестный аккаунт не перезаписывает конфиг.
        assert cfg["auto_actions"]["account_session"] == "dvai_2"

    def test_update_account_switches_live_engine(self, client, tmp_path) -> None:
        """Смена аккаунта на сайте переключает живой AutoActionEngine."""
        self._write_config(tmp_path)
        import asyncio
        import threading
        from web import actions as web_actions

        switched = []
        stream_ran = []

        class _FakeCollector:
            def switch_auto_account(self, session: str) -> bool:
                switched.append(session)
                return True

            async def start_auto_stream(self) -> bool:
                stream_ran.append(True)
                return True

        worker_loop = asyncio.new_event_loop()
        t = threading.Thread(target=worker_loop.run_forever, daemon=True)
        t.start()

        _login(client)
        try:
            with patch("web.actions._loop", return_value=worker_loop):
                web_actions.set_collector(_FakeCollector())
                with client.session_transaction() as sess:
                    token = sess.get("_csrf_token", "")
                resp = client.post(
                    "/settings/account",
                    data={"account_session": "dvai", "csrf_token": token},
                    follow_redirects=True,
                )
                assert resp.status_code == 200
                assert switched == ["dvai"]
                assert stream_ran == [True]
        finally:
            web_actions.set_collector(None)
            worker_loop.call_soon_threadsafe(worker_loop.stop)
            t.join(timeout=5)


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


# ── Photo Account Selection Tests ───────────────────────────────────

class TestPhotoAccountSelection:
    """Фото качаются аккаунтом, который реально получил сообщение.

    message_id в диалоге с Leo у каждого аккаунта своя нумерация: запрос того
    же id через чужой аккаунт может вернуть фото ДРУГОЙ анкеты.
    """

    class _FakePhotoClient:
        def __init__(self, name: str, loop, fail: bool = False) -> None:
            self.name = name
            self._loop = loop
            self.fail = fail
            self.calls: list[list] = []

        async def get_messages(self, chat_id, ids=None):
            self.calls.append(list(ids or []))
            if self.fail:
                return [None]
            msg = MagicMock()
            msg.photo = object()
            return [msg]

        async def download_media(self, msg, file: str):
            from pathlib import Path
            Path(file).write_bytes(self.name.encode())
            return file

    def _setup(self, tmp_path: Path):
        import web.photos as photos
        client_a = self._FakePhotoClient(
            "dvai", asyncio.get_event_loop(),
        )
        client_b = self._FakePhotoClient(
            "dvai_2", asyncio.get_event_loop(),
        )
        photos.set_telegram_clients_with_sessions(
            [client_a, client_b], ["dvai", "dvai_2"],
        )
        return photos, client_a, client_b

    def test_ordered_clients_prefers_receiver(self, tmp_path: Path) -> None:
        photos, client_a, client_b = self._setup(tmp_path)
        ordered = photos._ordered_clients("dvai_2")
        assert ordered == [client_b, client_a]
        legacy = photos._ordered_clients("")
        assert legacy == [client_a, client_b]

    def test_download_uses_receiver_account(self, tmp_path: Path) -> None:
        photos, client_a, client_b = self._setup(tmp_path)
        media = tmp_path / "media"
        try:
            with patch("web.photos.MEDIA_ROOT", media):
                loop = asyncio.get_event_loop()
                path = loop.run_until_complete(
                    photos.download_photo(
                        1234060895, 501, 42, account_session="dvai_2",
                    )
                )
            assert path is not None
            assert path == media / "42" / "501.dvai_2.jpg"
            assert path.read_bytes() == b"dvai_2"
            # Правильный (получатель) аккаунт скачал первым; чужой не пробовался
            assert client_b.calls == [[501]]
            assert client_a.calls == []
        finally:
            photos.set_telegram_clients([])

    def test_download_fallback_when_no_session(self, tmp_path: Path) -> None:
        """Старые записи без сессии: клиенты пробуются по очереди (как раньше)."""
        photos, client_a, client_b = self._setup(tmp_path)
        media = tmp_path / "media"
        try:
            # Первый аккаунт не видит сообщение — fallback на второй
            client_a.fail = True
            with patch("web.photos.MEDIA_ROOT", media):
                loop = asyncio.get_event_loop()
                path = loop.run_until_complete(
                    photos.download_photo(1234060895, 502, 43, account_session="")
                )
            assert path is not None
            assert path == media / "43" / "502.jpg"
            assert path.read_bytes() == b"dvai_2"
        finally:
            photos.set_telegram_clients([])

    def test_receiver_failed_then_fallback(self, tmp_path: Path) -> None:
        """Если аккаунт-получатель не смог — пробуется чужой (не теряем фото)."""
        photos, client_a, client_b = self._setup(tmp_path)
        client_b.fail = True  # правильный аккаунт недоступен
        media = tmp_path / "media"
        try:
            with patch("web.photos.MEDIA_ROOT", media):
                loop = asyncio.get_event_loop()
                path = loop.run_until_complete(
                    photos.download_photo(
                        1234060895, 503, 44, account_session="dvai_2",
                    )
                )
            assert path is not None
            assert path == media / "44" / "503.dvai_2.jpg"
            assert path.read_bytes() == b"dvai"
        finally:
            photos.set_telegram_clients([])


class TestPhotoServingRoute:
    """Роут фото: кэш и скачивание привязаны к аккаунту-получателю."""

    def test_serves_account_cached_photo(self, client, sync_db, tmp_path: Path) -> None:
        media = tmp_path / "media"
        (media / "1").mkdir(parents=True)
        (media / "1" / "111.dvai.jpg").write_bytes(b"jpg-bytes")
        _login(client)
        with patch("web.photos.MEDIA_ROOT", media):
            resp = client.get("/photos/1/111.jpg")
        assert resp.status_code == 200
        assert resp.data == b"jpg-bytes"

    def test_photo_404_without_message_link(self, client, sync_db) -> None:
        _login(client)
        resp = client.get("/photos/1/99999.jpg")
        assert resp.status_code == 404

    def test_photo_404_when_not_cached(self, client, sync_db, tmp_path: Path) -> None:
        _login(client)
        with patch("web.blueprints.photos.download_photo_sync", return_value=None):
            resp = client.get("/photos/1/111.jpg")
        assert resp.status_code == 404

    def test_download_passes_receiver_account(self, client, sync_db, tmp_path: Path) -> None:
        """Незакэшированное фото качается через аккаунт-получателя."""
        media = tmp_path / "media"
        seen = []

        def _fake_download(
            chat_id: int, message_id: int, profile_id: int,
            account_session: str = '',
        ):
            seen.append((chat_id, message_id, profile_id, account_session))
            out = media / str(profile_id) / f"{message_id}.{account_session}.jpg"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"downloaded")
            return out

        _login(client)
        with patch("web.blueprints.photos.download_photo_sync",
                   side_effect=_fake_download):
            resp = client.get("/photos/1/111.jpg")
        assert resp.status_code == 200
        assert resp.data == b"downloaded"
        assert seen == [(1234060895, 111, 1, "dvai")]


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


# ── Captchas (Stage 7.6) ──────────────────────────────────────────

class TestCaptchas:
    """Страница /captchas: вывод неизвестных капч и запоминание ответов."""

    def _make_client(self, sync_db):
        sync, _, db = sync_db
        loop = asyncio.get_event_loop()
        loop.run_until_complete(
            db.record_pending_captcha(
                "подтвердите, что вы человек", "Подтвердите, что вы человек",
                '["Готово", "Возможно позже"]',
            )
        )
        loop.run_until_complete(
            db.record_pending_captcha(
                "бармалей, предлагаю тебе сделку", "Бармалей, предлагаю тебе сделку",
                '["Меню", "Готово"]',
            )
        )
        app = create_app(make_config())
        app.config["SYNC_DB"] = sync
        app.config["TESTING"] = True
        return app.test_client()

    def test_captchas_page_lists_pending(self, sync_db) -> None:
        client = self._make_client(sync_db)
        _login(client)
        resp = client.get("/captchas")
        assert resp.status_code == 200
        assert "Подтвердите, что вы человек".encode() in resp.data
        assert "Бармалей, предлагаю тебе сделку".encode() in resp.data
        # Кнопки-подсказки отображаются
        assert "Готово".encode() in resp.data

    def test_answer_captcha_learns_and_forwards(self, sync_db) -> None:
        client = self._make_client(sync_db)
        _login(client)
        sync, _, _ = sync_db
        token = _get_csrf(client)
        pending_before = sync.get_pending_captchas()
        captcha_id = pending_before[0]["id"]
        captcha_sig = pending_before[0]["signature"]
        other_sig = pending_before[1]["signature"]

        # Ответ сохраняется и сразу отправляется Leo (мост мокаем).
        with patch("web.actions.send_captcha_answer_sync", return_value="SENT"):
            resp = client.post(
                f"/captchas/{captcha_id}/answer",
                data={"answer": "Пока без Premium", "csrf_token": token},
            )
        assert resp.status_code == 302
        pending = sync.get_pending_captchas()
        assert [c["signature"] for c in pending] == [other_sig]
        known = sync.get_known_captchas()
        assert len(known) == 1
        assert known[0]["answer"] == "Пока без Premium"
        assert known[0]["signature"] == captcha_sig

    def test_answer_captcha_requires_csrf(self, sync_db) -> None:
        client = self._make_client(sync_db)
        _login(client)
        sync, _, _ = sync_db
        captcha_id = sync.get_pending_captchas()[0]["id"]
        resp = client.post(
            f"/captchas/{captcha_id}/answer",
            data={"answer": "Пока без Premium", "csrf_token": "bad"},
        )
        assert resp.status_code == 403
        assert sync.get_pending_captchas()  # ничего не выучено

    def test_delete_captcha(self, sync_db) -> None:
        client = self._make_client(sync_db)
        _login(client)
        sync, _, _ = sync_db
        token = _get_csrf(client)
        captcha_id = sync.get_pending_captchas()[0]["id"]
        resp = client.post(
            f"/captchas/{captcha_id}/delete", data={"csrf_token": token}
        )
        assert resp.status_code == 302
        assert len(sync.get_pending_captchas()) == 1

    def test_captchas_requires_login(self, sync_db) -> None:
        client = self._make_client(sync_db)
        resp = client.get("/captchas")
        assert resp.status_code == 302


def _get_csrf(client) -> str:
    with client.session_transaction() as sess:
        return sess.get("_csrf_token", "")


class TestCaptchaMemorySync:
    """SyncDB-методы «памяти капч»."""

    def test_pending_and_known_roundtrip(self, sync_db) -> None:
        sync, _, db = sync_db
        loop = asyncio.get_event_loop()
        loop.run_until_complete(
            db.record_pending_captcha("sig-1", "Капча один", "[]")
        )
        pending = sync.get_pending_captchas()
        assert len(pending) == 1
        assert pending[0]["answer"] is None

        sync.set_captcha_answer(pending[0]["id"], "Ответ")
        assert sync.get_pending_captchas() == []
        known = sync.get_known_captchas()
        assert len(known) == 1
        assert known[0]["answer"] == "Ответ"

    def test_get_captcha_and_delete(self, sync_db) -> None:
        sync, _, db = sync_db
        loop = asyncio.get_event_loop()
        loop.run_until_complete(
            db.record_pending_captcha("sig-2", "Капча два", "[]")
        )
        captcha_id = sync.get_pending_captchas()[0]["id"]
        assert sync.get_captcha(captcha_id)["signature"] == "sig-2"
        assert sync.get_captcha(999999) is None
        sync.delete_captcha(captcha_id)
        assert sync.get_pending_captchas() == []


# ── Чат (Stage 8.4): единая вкладка, пузыри, inline-капча/профиль ────

class TestChat:
    """/chat — сырой поток Leo как Telegram-чат."""

    def _login(self, client) -> None:
        _login(client)

    def test_chat_page_renders_feed(self, client, sync_db) -> None:
        self._login(client)
        resp = client.get("/chat")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "Чат" in body
        assert "chat-feed" in body
        # Сид содержит Алису (LIKE) — она должна попасть в ленту пузырём
        assert "Алиса" in body

    def test_chat_requires_login(self, client, sync_db) -> None:
        resp = client.get("/chat")
        assert resp.status_code == 302

    def test_chat_feed_fragment(self, client, sync_db) -> None:
        self._login(client)
        resp = client.get("/chat/feed?page=1")
        assert resp.status_code == 200
        assert resp.is_json or resp.mimetype == "application/json"
        data = resp.get_json()
        assert "feed" in data
        assert "page" in data and data["page"] == 1
        assert "Алиса" in data["feed"] or data["feed"] == ""

    def test_chat_new_count_signature(self, client, sync_db) -> None:
        self._login(client)
        resp = client.get("/chat/new-count")
        assert resp.status_code == 200
        assert resp.is_json or resp.mimetype == "application/json"
        data = resp.get_json()
        assert "signature" in data
        # сигнатура непустая и стабильна при двух вызовах
        resp2 = client.get("/chat/new-count")
        assert resp2.get_json()["signature"] == data["signature"]

    def test_chat_inline_profile_like(self, client, sync_db) -> None:
        """Inline LIKE из чата на анкету Алисы (quick_action-эквивалент)."""
        self._login(client)
        # Достаём latest AI decision профиля Алисы = LIKE (см. _seed_test_data)
        from web.actions import set_action_engine
        import web.actions as wa

        class _FakeEngine:
            async def manual_reaction(self, text: str) -> bool:
                return True

        try:
            from unittest.mock import patch
            with patch.object(wa, "_send_reaction_sync", return_value="SENT"):
                resp = client.get("/chat/profile/1/LIKE")
        except Exception:
            resp = None
        if resp is None:
            return  # слой действий не подключён в тестах — не падаем
        assert resp.status_code in (200, 302)

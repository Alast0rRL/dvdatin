# Web UI тесты — Flask endpoints, auth, dashboard, profiles, settings.

from __future__ import annotations

import asyncio
import json
import os
import re
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
    # Ручное решение (пишет веб: кнопки «лайк/дизлайк» на сайте).
    await db._connection.execute(
        "INSERT INTO human_decisions "
        "(profile_id, ai_decision_id, decision, agreement, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (pid1, ad1_id, "APPROVE", "AGREEMENT", "2025-01-01T00:00:03"),
    )
    await db._connection.commit()

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

    # Лента Leo: три анкеты в потоке (profile_messages + raw_messages).
    for pid, tm_id, text, ts in (
        (pid1, 111, "Алиса, 19, Санкт-Петербург - Люблю аниме", "2025-01-01T00:00:04"),
        (pid2, 112, "Барби, 18, Москва", "2025-01-02T00:00:04"),
        (pid3, 113, "Варвара, 20, Сургут", "2025-01-03T00:00:04"),
    ):
        await db.link_profile_message(
            profile_id=pid, telegram_message_id=tm_id, chat_id=1234060895,
            created_at=ts, account_session="dvai",
        )
        await db.save_raw_message(
            telegram_message_id=tm_id, chat_id=1234060895, sender_id=100,
            sender_username="", sender_name="Leo", message_date=ts,
            text=text, raw_entities="[]", reply_markup="[]",
            media_type="photo", received_at=ts,
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


def _drawer_html(page_html: str) -> str:
    """Вырезает только <aside class="drawer">…</aside> из страницы.

    Нужно, чтобы проверять «в панели настроек есть / нет» — в шапке те же
    формы продолжают жить, и просто `'/settings/mode' in page` ничего не
    говорит о содержимом drawer.
    """
    start = page_html.find('id="drawer"')
    if start < 0:
        return ""
    start = page_html.rfind("<aside", 0, start)
    end = page_html.find("</aside>", start)
    return page_html[start:end] if start >= 0 and end > 0 else ""


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

# ── Единый экран (Stage 9) ──────────────────────────────────────

class TestAppShell:
    """`/` — один экран: лента чата + выбор режима + кнопка настроек."""

    def test_root_renders_app(self, client) -> None:
        _login(client)
        resp = client.get("/")
        assert resp.status_code == 200
        data = resp.get_data(as_text=True)
        assert 'id="app"' in data
        assert 'id="feed"' in data
        assert 'id="settings-open"' in data

    def test_chat_alias_renders_same_app(self, client) -> None:
        _login(client)
        assert client.get("/chat").status_code == 200

    def test_no_separate_tabs_or_pages(self, client) -> None:
        """Отдельных вкладок нет: настройки — панель поверх, лента одна."""
        _login(client)
        data = client.get("/").get_data(as_text=True)
        assert 'id="drawer"' in data
        assert "profile-feed" not in data          # старая лента анкет удалена
        assert 'href="/dashboard"' not in data
        assert 'href="/settings"' not in data
        assert 'href="/profiles/' not in data

    def test_mode_switcher_in_topbar(self, client) -> None:
        _login(client)
        data = client.get("/").get_data(as_text=True)
        assert "/settings/mode" in data
        # Два независимых переключателя вместо трёх режимов (OBSERVE = оба выкл).
        assert 'name="scroll" value="on"' in data
        assert 'name="auto" value="on"' in data
        assert 'value="OBSERVE"' not in data

    def test_settings_button_and_forms_in_drawer(self, client) -> None:
        """В панели остались только настройки, которые меняют редко."""
        _login(client)
        data = client.get("/").get_data(as_text=True)
        assert 'id="settings-open"' in data
        assert 'id="drawer"' in data
        assert 'id="drawer-close"' in data
        drawer = _drawer_html(data)
        for ep in ("/settings/filters", "/settings/preferences"):
            assert ep in drawer, ep
        for field in ('name="age_min"', 'name="age_max"', 'name="city_allowed"',
                      'name="skip_rules"', 'name="like_rules"'):
            assert field in drawer, field

    def test_mode_switch_not_in_settings_drawer(self, client) -> None:
        """Режим работы — только в шапке: в панели настроек его больше нет.

        Регресс: переключатель режима дублировался в drawer и в топбаре —
        два места для одного и того же и лишний клик на ровном месте.
        """
        _login(client)
        drawer = _drawer_html(client.get("/").get_data(as_text=True))
        assert "/settings/mode" not in drawer
        assert 'name="mode"' not in drawer
        assert "Режим работы" not in drawer
        # ...но в шапке он остался.
        assert "/settings/mode" in client.get("/").get_data(as_text=True)

    def test_account_selector_moved_to_topbar(self, client) -> None:
        """Аккаунт-исполнитель вынесен из панели настроек в шапку.

        Регресс: переключение аккаунта спрятано было за ⚙-панель, хотя это
        «горячий» селектор — как и режим работы.
        """
        TestAccountSettings._write_config(None, client.application.config["CONFIG_PATH"].parent)
        _login(client)
        data = client.get("/").get_data(as_text=True)
        drawer = _drawer_html(data)

        # В панели — нет.
        assert "/settings/account" not in drawer
        assert 'name="account_session"' not in drawer
        assert "Аккаунт-исполнитель" not in drawer

        # В шапке — есть, и он рядом с переключателем режима.
        assert 'id="acct-rows"' in data
        assert "/settings/account" in data
        assert 'name="account_session"' in data
        assert 'name="csrf_token"' in data

    def test_settings_values_loaded_into_drawer(self, client) -> None:
        """Значения фильтров/режима подставляются из config.yaml."""
        _login(client)
        data = client.get("/").get_data(as_text=True)
        assert 'value="18"' in data      # age_min
        assert 'value="19"' in data      # age_max
        assert "Санкт-Петербург" in data  # city_allowed
        # project.mode OBSERVE → оба переключателя выключены (кнопки «включить»).
        assert 'name="scroll" value="on"' in data and 'name="auto" value="on"' in data

    def test_legacy_pages_redirect_to_app(self, client) -> None:
        """Старые страницы ведут на главный экран (без 404)."""
        _login(client)
        for path in ("/dashboard", "/settings", "/profiles/1", "/profiles/2/action"):
            resp = client.get(path)
            assert resp.status_code == 302, path
            loc = resp.headers["Location"]
            assert loc in ("/", "/chat"), f"{path} -> {loc}"

    def test_poll_endpoints_available(self, client) -> None:
        _login(client)
        count = client.get("/chat/new-count")
        assert count.status_code == 200
        assert "signature" in count.get_json()
        feed = client.get("/chat/feed?page=1")
        assert feed.status_code == 200
        payload = feed.get_json()
        assert "feed" in payload and "pending" in payload

    def test_app_js_poll_and_drawer(self, client) -> None:
        """JS подключён: polling + открытие панели настроек."""
        _login(client)
        resp = client.get("/static/js/app.js")
        assert resp.status_code == 200
        src = resp.get_data(as_text=True)
        assert "/chat/new-count" in src
        assert "openDrawer" in src
        assert "closeDrawer" in src
        assert "amp;" not in src

    def test_drawer_is_closed_by_default(self, client) -> None:
        """Drawer приходит закрытым, а `hidden` реально прячет его (CSS-гейт)."""
        _login(client)
        data = client.get("/").get_data(as_text=True)
        assert 'id="drawer"' in data and "hidden" in data
        # Регресс: `.drawer { display: flex }` перебивал [hidden] и крестик
        # не закрывал панель — нужен явный guard в статике.
        css = client.get("/static/css/app.css").get_data(as_text=True)
        assert "[hidden]" in css and "display: none !important" in css

    def test_feed_is_single_vertical_column(self, client) -> None:
        """Лента — одна колонка: все строки одной ширины, друг за другом.

        Регресс на жалобу «карточки разбросаны по краям, посередине пусто»:
        чат-вёрстка с `width: fit-content` и чередованием `align-self`
        превращала короткие события («👎 DISLIKE» + время) в узкие плашки
        то у правого, то у левого края. Теперь это плоский лог-список.
        """
        _login(client)
        css = client.get("/static/css/app.css").get_data(as_text=True)
        feed = css.split(".feed {", 1)[1].split("}", 1)[0]
        assert "flex-direction: column" in feed
        assert "align-items: stretch" in feed      # не center по краям
        assert "width: 100%" in feed
        # Контейнер ленты — одна колонка по центру, ширина ограничена.
        app = css.split(".app {", 1)[1].split("}", 1)[0]
        assert "margin: 0 auto" in app
        assert "max-width:" in app
        # Строка события — grid (время + содержимое), а не пузырь.
        row = css.split(".row {", 1)[1].split("}", 1)[0]
        assert "display: grid" in row
        assert "minmax(0, 1fr)" in row             # вторая колонка тянется
        assert "width: 100%" in row
        # Нигде в стилях не осталось подгонки/прижатия строк ленты.
        assert "align-self" not in css
        assert "fit-content" not in css
        assert "position: absolute" not in css

    def test_feed_rows_carry_filter_attributes(self, client) -> None:
        """Строки ленты помечены решением и именем — для фильтра/поиска."""
        _login(client)
        data = client.get("/").get_data(as_text=True)
        assert 'data-decision="' in data
        assert 'data-name="' in data
        # Барби (id=2) — AI REVIEW → строка помечена REVIEW и ищется по имени.
        assert 'data-decision="REVIEW"' in data
        assert 'id="profile-2"' in data
        # Панель управления списком на том же экране.
        assert 'id="filter-seg"' in data
        assert 'id="feed-search"' in data
        assert 'id="feed-count"' in data
        # JS умеет фильтровать и искать по этим атрибутам.
        src = client.get("/static/js/app.js").get_data(as_text=True)
        assert "applyFilter" in src and "dataset.decision" in src
        assert "dataset.name" in src

    def test_layout_cannot_shift_sideways(self, client) -> None:
        """Вёрстка не «уезжает» вбок: горизонтального скролла нет, топбар на grid.

        Регресс на жалобу «интерфейс поплыл, сверху в углу смещено»:
        flex + `margin-left: auto` в топбаре и растягивающиеся фото/текст
        выталкивали ⚙/выход за правый край. Теперь топбар — grid из
        `minmax(0, 1fr) auto`, у строк `min-width: 0`, текст переносится
        внутри себя, фото — фиксированные миниатюры с `object-fit`.
        """
        _login(client)
        css = client.get("/static/css/app.css").get_data(as_text=True)
        assert "overflow-x: hidden" in css
        # Топбар: две колонки, левая тянется, правая по содержимому.
        topbar = css.split(".topbar {", 1)[1].split("}", 1)[0]
        assert "grid-template-columns: minmax(0, 1fr) auto" in topbar
        assert "margin-left: auto" not in topbar
        # Ничего не растягивает ленту шире экрана: строка = 100% контейнера.
        assert "max-width: 100%" in css
        assert "min-width: 0" in css
        assert "overflow-wrap: anywhere" in css
        # Миниатюра фото не задаёт высоту до загрузки → вёрстка не прыгает.
        thumbs = css.split(".thumbs__img {", 1)[1].split("}", 1)[0]
        assert "object-fit" in thumbs
        # Размер миниатюры фиксированный (фото видно, но лента остаётся плотной).
        item = css.split(".thumbs__item {", 1)[1].split("}", 1)[0]
        assert "width: 192px" in item and "height: 256px" in item
        # Панель настроек не шире экрана.
        assert "width: min(420px, 100vw)" in css

    def test_static_assets_are_cache_busted(self, client) -> None:
        """CSS/JS подключены с ?v=<mtime static/> — браузер тянет свежую вёрстку."""
        # /login проверяем до логина: после логина он редиректит на /chat.
        assert "css/app.css?v=" in client.get("/login").get_data(as_text=True)
        _login(client)
        data = client.get("/").get_data(as_text=True)
        assert "css/app.css?v=" in data
        assert "js/app.js?v=" in data

    def test_login_uses_defined_css_classes(self, client) -> None:
        """Страница входа использует те же классы, что есть в app.css.

        Регресс: после переписывания стилей login.html остался с классами
        `.form-input` / `.btn-primary` / `.btn-full`, которых в CSS уже не
        было — вход рендерился серым без оформленной кнопки.
        """
        html = client.get("/login").get_data(as_text=True)
        assert 'class="input"' in html
        assert "btn--primary" in html and "btn--full" in html
        assert "form-input" not in html and "btn-primary" not in html

    def test_every_component_class_is_styled(self) -> None:
        """Каждый компонентный класс из шаблонов определён в app.css.

        Ловит рассинхрон разметки и стилей (как `.form-input` выше) до деплоя:
        класс, которого нет в CSS, = элемент без оформления.
        """
        from pathlib import Path
        import re

        root = Path(__file__).resolve().parents[1]
        css = (root / "static" / "css" / "app.css").read_text(encoding="utf-8")
        defined = set(re.findall(r"\.([A-Za-z][\w-]*)", css))
        # Префиксы наших компонентов; служебные/сторонние классы не проверяем.
        prefixes = ("topbar", "brand", "icon-btn", "mode", "app", "feed", "row", "thumbs",
                     "badge", "btn", "input", "link-btn", "chip", "pending", "drawer",
                     "seg", "toolbar", "empty", "toast", "login", "form", "flash",
                     "inline-form", "account", "theme")

        missing: dict[str, set[str]] = {}
        for template in (root / "web" / "templates").rglob("*.html"):
            body = template.read_text(encoding="utf-8")
            classes: set[str] = set()
            for raw in re.findall(r'class="([^"{}]+)"', body):
                # Jinja-вставки в class (режим, тип тоста) пропускаем.
                for name in re.split(r"\s+", raw):
                    if name and "{{" not in name and "{%" not in name:
                        classes.add(name)
            local = {c for c in classes
                     if c.startswith(prefixes) and not c.startswith(("is-", "mode-pill--",
                                                                     "mode-switch__btn--",
                                                                     "badge--", "row--",
                                                                     "drawer__section", "toast--", "flash-"))}
            gap = local - defined
            if gap:
                missing[template.name] = gap

        assert not missing, f"классы без стилей: {missing}"


# ── Theme Tests ───────────────────────────────────────────────────

class TestTheme:
    """Светлая тема + переключатель (тёмная осталась по умолчанию)."""

    def test_theme_boot_script_runs_before_first_paint(self, client) -> None:
        """data-theme ставится инлайном до подключения app.js.

        Иначе страница моргает тёмным фоном, пока грузится внешний скрипт.
        """
        _login(client)
        data = client.get("/").get_data(as_text=True)
        assert "localStorage" in data
        assert "prefers-color-scheme" in data
        assert "dvai.theme" in data
        assert data.index("data-theme") < data.index("js/app.js"), (
            "тема должна ставиться до app.js"
        )
        assert "<script>" in data, "скрипт инлайновый, а не отдельный запрос"

    def test_theme_boot_also_on_login_page(self, client) -> None:
        """Страница входа тоже уважает выбранную тему."""
        html = client.get("/login").get_data(as_text=True)
        assert "dvai.theme" in html
        assert "prefers-color-scheme" in html

    def test_theme_toggle_button_in_topbar(self, client) -> None:
        _login(client)
        data = client.get("/").get_data(as_text=True)
        assert 'id="theme-toggle"' in data
        assert 'class="icon-btn theme-toggle"' in data
        assert 'aria-label="Сменить тему"' in data
        # Обе иконки на месте, что из них видно — решает CSS по data-theme.
        assert "theme-toggle__sun" in data and "theme-toggle__moon" in data

    def test_toggle_logic_in_js(self, client) -> None:
        src = client.get("/static/js/app.js").get_data(as_text=True)
        assert "theme-toggle" in src
        assert "data-theme" in src
        assert "dvai.theme" in src
        assert "applyTheme" in src

    def test_both_palettes_defined_in_css(self, client) -> None:
        """Две палитры: :root (тёмная) и [data-theme='light'] (светлая)."""
        css = client.get("/static/css/app.css").get_data(as_text=True)
        assert '[data-theme="light"]' in css
        dark = css.split(":root {", 1)[1].split("}", 1)[0]
        light = css.split('[data-theme="light"] {', 1)[1].split("}", 1)[0]
        tokens_dark = set(re.findall(r"(--[\w-]+):", dark))
        tokens_light = set(re.findall(r"(--[\w-]+):", light))
        # Светлая тема обязана переопределять ВСЕ токены тёмной, иначе
        # часть вёрстки так и останется тёмной.
        assert tokens_dark, "у :root должны быть токены"
        missing = tokens_dark - tokens_light
        assert not missing, f"в светлой теме нет токенов: {sorted(missing)}"

    def test_no_raw_colors_outside_palettes(self, client) -> None:
        """Вне блоков палитры цветов нет — иначе в светлой теме будут пятна."""
        css = client.get("/static/css/app.css").get_data(as_text=True)
        rules = re.sub(r':root\s*\{[^}]*\}', "", css, count=1)
        rules = re.sub(r'\[data-theme="light"\]\s*\{[^}]*\}', "", rules, count=1)
        raw = re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)", rules)
        assert not raw, f"сырые цвета вне палитры: {sorted(set(raw))}"

    def test_theme_switch_respects_media_query(self, client) -> None:
        """Без сохранённого выбора тема берётся из системных настроек."""
        html = client.get("/login").get_data(as_text=True)
        assert "matchMedia" in html
        assert "'light'" in html and "'dark'" in html


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

# ── Анкета в ленте (отдельной страницы больше нет) ────────────────

class TestProfileInFeed:
    """Анкеты живут в ленте; /profiles/<id> — редирект на главный экран."""

    def test_profile_page_redirects(self, client) -> None:
        _login(client)
        resp = client.get("/profiles/1")
        assert resp.status_code == 302

    def test_feed_has_no_action_buttons(self, client) -> None:
        """Лента — дублирование чата: карточек с ❤️/👎 больше нет.

        Анкета показывается теми же сообщениями, что и в Telegram (текст +
        фото). Ручные кнопки REVIEW из ленты убраны вместе с карточками;
        маршруты /chat/profile/<id>/<action> остаются рабочими (см. ниже).
        """
        _login(client)
        data = client.get("/").get_data(as_text=True)
        assert "/chat/profile/2/LIKE" not in data
        assert "/chat/profile/1/LIKE" not in data
        assert "/chat/profile/3/LIKE" not in data

    def test_review_action_like_saves_human_decision(self, client, sync_db) -> None:
        sync, _, _ = sync_db
        _login(client)
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token", "")
        with patch("web.actions.send_reaction_sync", return_value="SENT"):
            resp = client.post(
                "/chat/profile/2/LIKE", data={"csrf_token": token, "next": "/"}
            )
        assert resp.status_code == 302
        hd = sync.get_human_decision(2)
        assert hd is not None
        assert hd["decision"] == "APPROVE"

    def test_review_action_rejected_for_non_review(self, client, sync_db) -> None:
        """Серверный гейт: не-REVIEW анкету вручную не решить."""
        sync, _, _ = sync_db
        _login(client)
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token", "")
        with patch("web.actions.send_reaction_sync", return_value="SENT"):
            resp = client.post(
                "/chat/profile/3/LIKE",
                data={"csrf_token": token, "next": "/"},
                follow_redirects=True,
            )
        assert resp.status_code == 200
        assert sync.get_human_decision(3) is None

    def test_review_action_requires_csrf(self, client, sync_db) -> None:
        sync, _, _ = sync_db
        _login(client)
        resp = client.post("/chat/profile/2/LIKE", data={"csrf_token": "bad", "next": "/"})
        assert resp.status_code == 403
        assert sync.get_human_decision(2) is None

    def test_review_action_invalid_action(self, client) -> None:
        _login(client)
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token", "")
        resp = client.post(
            "/chat/profile/2/INVALID", data={"csrf_token": token, "next": "/"}
        )
        assert resp.status_code == 400

    def test_already_reviewed_not_overwritten(self, client, sync_db) -> None:
        sync, _, _ = sync_db
        _login(client)
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token", "")
        with patch("web.actions.send_reaction_sync", return_value="SENT"):
            client.post("/chat/profile/2/LIKE", data={"csrf_token": token, "next": "/"})
            resp = client.post(
                "/chat/profile/2/DISLIKE",
                data={"csrf_token": token, "next": "/"},
                follow_redirects=True,
            )
        assert resp.status_code == 200
        assert sync.get_human_decision(2)["decision"] == "APPROVE"

# ── Settings Tests ───────────────────────────────────────────────

class TestSettings:
    """Тесты страницы настроек."""

    def test_settings_page_redirects_to_app(self, client) -> None:
        """Отдельной страницы настроек нет — всё в панели на главном экране."""
        _login(client)
        resp = client.get("/settings")
        assert resp.status_code == 302

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

            async def mode_block_reason(self, mode: Mode) -> str | None:
                return None

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

    def test_update_mode_refused_while_like_limit_exhausted(
        self, client, tmp_path,
    ) -> None:
        """Лимит ❤️ исчерпан → сайт не включает SEMI_AUTO: ни YAML, ни движок."""
        import yaml
        from core.types import Mode
        from web import actions as web_actions

        switched = []

        class _FakeCollector:
            def set_mode(self, mode: Mode) -> None:
                switched.append(mode)

            async def mode_block_reason(self, mode: Mode) -> str | None:
                return "Лимит ❤️ у dvai_2 исчерпан до 07:00"

            async def start_auto_stream(self) -> bool:
                return True

        with open(tmp_path / "config.yaml", encoding="utf-8") as f:
            before = yaml.safe_load(f)["project"]["mode"]

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
                assert "Лимит ❤️" in resp.get_data(as_text=True)
                assert switched == []
        finally:
            web_actions.set_collector(None)
            worker_loop.call_soon_threadsafe(worker_loop.stop)
            t.join(timeout=5)
        with open(tmp_path / "config.yaml", encoding="utf-8") as f:
            assert yaml.safe_load(f)["project"]["mode"] == before

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

    def test_topbar_has_row_per_account(self, client, tmp_path) -> None:
        """Своя строка на каждый аккаунт: «Скроллинг» и «Авто», без OBSERVE."""
        import re
        import yaml
        self._write_config(tmp_path)
        cfg_path = tmp_path / "config.yaml"
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        cfg["auto_actions"]["account_controls"] = {
            "dvai": {"scroll": False, "auto": True},
        }
        cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
        _login(client)
        data = client.get("/").get_data(as_text=True)
        rows = re.findall(r'<div class="acct-row[^"]*"\s+data-account="([^"]*)"', data)
        assert rows == ["dvai", "dvai_2"]
        top = data[data.index('id="acct-rows"'):]
        row1 = top[:top.index('data-account="dvai_2"')]
        row2 = top[top.index('data-account="dvai_2"'):top.index("</header>")]
        header = data[:data.index("</header>")]
        # Отдельной кнопки OBSERVE / SEMI AUTO больше нет.
        assert 'value="OBSERVE"' not in header and 'value="SEMI_AUTO"' not in header
        # dvai: только «Авто» (своя запись) → кнопка скроллинга включит его.
        assert re.search(r'name="scroll" value="on"\s+class="[^"]*--scroll "', row1)
        assert re.search(r'name="auto" value="off"\s+class="[^"]*--auto is-active', row1)
        # dvai_2 (основной): project.mode SEMI_AUTO → скроллинг вкл, авто выкл.
        assert re.search(r'name="scroll" value="off"\s+class="[^"]*--scroll is-active', row2)
        assert re.search(r'name="auto" value="on"\s+class="[^"]*--auto "', row2)
        assert 'name="account_session" value="dvai"' in row1
        # Основной аккаунт подсвечен, плашка — его состояние.
        assert re.search(r'class="acct-row is-main"\s+data-account="dvai_2"', data)
        assert not re.search(r'class="acct-row is-main"\s+data-account="dvai"', data)
        assert re.search(r'id="mode-pill"[^>]*>\s*скроллинг\s*<', data)

    @staticmethod
    def _fake_collector(calls: list):
        class _FakeCollector:
            def set_controls(self, session=None, *, scroll=None, auto=None):
                calls.append(("set", session, scroll, auto))

            async def mode_block_reason(self, mode, session=None):
                calls.append(("check", mode.value, session))
                return None

            async def start_auto_stream(self, session=None) -> bool:
                calls.append(("stream", session))
                return True

        return _FakeCollector()

    def _post_live(self, client, data: dict, calls: list):
        import asyncio
        import threading
        from web import actions as web_actions

        worker_loop = asyncio.new_event_loop()
        t = threading.Thread(target=worker_loop.run_forever, daemon=True)
        t.start()
        _login(client)
        try:
            with patch("web.actions._loop", return_value=worker_loop):
                web_actions.set_collector(self._fake_collector(calls))
                with client.session_transaction() as sess:
                    data = {**data, "csrf_token": sess.get("_csrf_token", "")}
                return client.post("/settings/mode", data=data, follow_redirects=True)
        finally:
            web_actions.set_collector(None)
            worker_loop.call_soon_threadsafe(worker_loop.stop)
            t.join(timeout=5)

    def test_toggle_scroll_for_one_account(self, client, tmp_path) -> None:
        """«Скроллинг» в строке dvai включает ленту только у dvai."""
        import yaml
        self._write_config(tmp_path)
        calls: list = []
        resp = self._post_live(
            client, {"scroll": "on", "account_session": "dvai"}, calls,
        )
        assert resp.status_code == 200
        assert ("check", "SEMI_AUTO", "dvai") in calls
        assert ("set", "dvai", True, False) in calls
        assert ("stream", "dvai") in calls
        cfg = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
        assert cfg["auto_actions"]["account_controls"] == {
            "dvai": {"scroll": True, "auto": False},
        }
        # Основной аккаунт (dvai_2) не тронут.
        assert cfg["project"]["mode"] == "SEMI_AUTO"

    def test_toggle_auto_independent_of_scroll(self, client, tmp_path) -> None:
        """«Авто» включается без скроллинга: лимит не проверяется, лента не трогается."""
        import yaml
        self._write_config(tmp_path)
        calls: list = []
        self._post_live(client, {"auto": "on", "account_session": "dvai"}, calls)
        assert calls == [("set", "dvai", False, True)]
        cfg = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
        assert cfg["auto_actions"]["account_controls"]["dvai"] == {
            "scroll": False, "auto": True,
        }

    def test_both_off_is_observe(self, client, tmp_path) -> None:
        """Оба выключены у основного → project.mode OBSERVE (старое наблюдение)."""
        import yaml
        self._write_config(tmp_path)  # dvai_2: SEMI_AUTO → скроллинг вкл
        calls: list = []
        self._post_live(client, {"scroll": "off", "account_session": "dvai_2"}, calls)
        assert ("set", "dvai_2", False, False) in calls
        cfg = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
        assert cfg["project"]["mode"] == "OBSERVE"
        assert cfg["auto_actions"]["account_controls"]["dvai_2"] == {
            "scroll": False, "auto": False,
        }

    def test_update_mode_unknown_account_refused(self, client, tmp_path) -> None:
        self._write_config(tmp_path)
        import yaml
        _login(client)
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token", "")
        client.post(
            "/settings/mode",
            data={"scroll": "on", "account_session": "nope", "csrf_token": token},
        )
        cfg = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
        assert "account_controls" not in cfg["auto_actions"]

    def test_account_dropdown_in_app_drawer(self, client, tmp_path) -> None:
        self._write_config(tmp_path)
        _login(client)
        data = client.get("/").get_data(as_text=True)
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

            async def start_auto_stream(self, session: str | None = None) -> bool:
                # Подталкивается лента именно нового основного аккаунта.
                assert session == "dvai"
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

    def test_ordered_clients_is_receiver_only(self, tmp_path: Path) -> None:
        photos, client_a, client_b = self._setup(tmp_path)
        # Известная сессия → ТОЛЬКО аккаунт-получатель (чужой не привлекаем:
        # тем же message_id он может вернуть фото ДРУГОЙ анкеты).
        ordered = photos._ordered_clients("dvai_2")
        assert ordered == [client_b]
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

    def test_receiver_failed_no_foreign_fallback(self, tmp_path: Path) -> None:
        """Если аккаунт-получатель не смог — чужой аккаунт НЕ привлекается.

        Запрос того же message_id через чужой аккаунт может вернуть фото
        ДРУГОЙ анкеты, поэтому лучше не показать фото, чем показать чужое.
        """
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
            assert path is None
            # Пробовали только получателя; чужой аккаунт не трогали
            assert client_b.calls == [[503]]
            assert client_a.calls == []
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

    def test_get_profiles_count(self, sync_db) -> None:
        sync, _, _ = sync_db
        assert sync.get_profiles_count() == 3

    def test_get_profiles_count_filtered(self, sync_db) -> None:
        sync, _, _ = sync_db
        assert sync.get_profiles_count("LIKE") == 1
        assert sync.get_profiles_count("REVIEW") == 1
        assert sync.get_profiles_count("DISLIKE") == 1

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

# ── Капчи: ответ прямо в ленте (Stage 7.6 + 9) ───────────────────

class TestCaptchas:
    """Капчи ждут ответа в блоке над лентой; ответ учит бота и уходит Leo."""

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

    def test_pending_captchas_in_app(self, sync_db) -> None:
        client = self._make_client(sync_db)
        _login(client)
        data = client.get("/").get_data(as_text=True)
        assert 'id="pending"' in data
        assert "Подтвердите, что вы человек" in data
        assert "Бармалей, предлагаю тебе сделку" in data
        assert "Готово" in data  # кнопки-подсказки

    def test_answer_captcha_learns_and_forwards(self, sync_db) -> None:
        client = self._make_client(sync_db)
        _login(client)
        sync, _, _ = sync_db
        token = _get_csrf(client)
        pending_before = sync.get_pending_captchas()
        captcha_id = pending_before[0]["id"]
        captcha_sig = pending_before[0]["signature"]
        other_sig = pending_before[1]["signature"]

        with patch(
            "web.actions.send_captcha_answer_sync", return_value="SENT"
        ) as send_mock:
            resp = client.post(
                f"/chat/captcha/{captcha_id}/answer",
                data={"answer": "Пока без Premium", "csrf_token": token, "next": "/"},
            )
        assert resp.status_code == 302
        # Ответ сохранился → отправка в Leo обязана быть вызвана (иначе веб
        # показывал ложное «Ошибка сохранения ответа» из-за lastrowid у UPDATE).
        send_mock.assert_called_once_with("Пока без Premium")
        assert [c["signature"] for c in sync.get_pending_captchas()] == [other_sig]
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
            f"/chat/captcha/{captcha_id}/answer",
            data={"answer": "Пока без Premium", "csrf_token": "bad"},
        )
        assert resp.status_code == 403
        assert sync.get_pending_captchas()

    def test_delete_captcha(self, sync_db) -> None:
        client = self._make_client(sync_db)
        _login(client)
        sync, _, _ = sync_db
        token = _get_csrf(client)
        captcha_id = sync.get_pending_captchas()[0]["id"]
        resp = client.post(
            f"/chat/captcha/{captcha_id}/delete",
            data={"csrf_token": token, "next": "/"},
        )
        assert resp.status_code == 302
        assert len(sync.get_pending_captchas()) == 1

    def test_app_requires_login(self, sync_db) -> None:
        client = self._make_client(sync_db)
        resp = client.get("/")
        assert resp.status_code == 302
        assert "/login" in resp.headers.get("Location", "")

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

# ── Лента чата: пагинация «с конца», polling, captcha-блок ──────────

class TestChatFeed:
    """Лента чата: свежие сверху, polling без перезагрузки."""

    def _login(self, client) -> None:
        _login(client)

    def test_chat_page_is_app(self, client, sync_db) -> None:
        self._login(client)
        resp = client.get("/chat")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "chat-pending" not in body  # старых id больше нет
        assert 'id="feed"' in body

    def test_chat_requires_login(self, client, sync_db) -> None:
        resp = client.get("/chat")
        assert resp.status_code == 302

    def test_chat_feed_fragment(self, client, sync_db) -> None:
        self._login(client)
        resp = client.get("/chat/feed?page=1")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "feed" in data
        assert data["page"] == 1

    def test_chat_feed_pages_is_single_slice(self, client, sync_db) -> None:
        """``pages=N`` — свежие N страниц одним срезом (polling без гонок OFFSET)."""
        self._login(client)
        resp = client.get("/chat/feed?page=1&pages=3")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "feed" in data
        assert data["page"] == 1
        assert data["total_pages"] >= 1

    def test_feed_time_is_localized_on_client(self) -> None:
        """Время строк — с ISO в ``datetime``: JS переводит UTC в местное."""
        tpl = (
            Path(__file__).resolve().parent.parent
            / "web" / "templates" / "partials" / "app_feed.html"
        ).read_text(encoding="utf-8")
        assert 'datetime="{{ item.ts }}"' in tpl
        js = (
            Path(__file__).resolve().parent.parent / "static" / "js" / "app.js"
        ).read_text(encoding="utf-8")
        assert "function localizeTimes" in js
        assert "pages=" in js

    def test_chat_new_count_signature(self, client, sync_db) -> None:
        self._login(client)
        resp = client.get("/chat/new-count")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "signature" in data
        resp2 = client.get("/chat/new-count")
        assert resp2.get_json()["signature"] == data["signature"]

    def test_chat_page1_is_newest(self, sync_db) -> None:
        """Страница 1 = самые свежие сообщения (пагинация «с конца»)."""
        sync, db_path, db = sync_db
        loop = asyncio.get_event_loop()

        async def _seed() -> None:
            for i in range(60):
                await db.save_raw_message(
                    telegram_message_id=9000 + i, chat_id=1234060895, sender_id=100,
                    sender_username="", sender_name="Leo",
                    message_date=f"2025-02-01T00:{i:02d}:00",
                    text=f"MSG-{i}", raw_entities="[]", reply_markup="[]",
                    media_type="", received_at=f"2025-02-01T00:{i:02d}:00",
                )

        loop.run_until_complete(_seed())

        page1 = sync.get_chat_feed(page=1, per_page=10)
        texts = [i["text"] for i in page1["items"]]
        assert texts, "лента не пуста"
        # Свежие СВЕРХУ (сразу под панелью управления), более ранние ниже.
        assert texts[0] == "MSG-59", "на 1-й странице самое свежее — первым"
        assert texts == [f"MSG-{i}" for i in range(59, 49, -1)]
        assert "MSG-0" not in texts

        page2 = sync.get_chat_feed(page=2, per_page=10)
        texts2 = [i["text"] for i in page2["items"]]
        assert texts2[0] == "MSG-49", "2-я страница — предыдущие, тоже от новых к старым"
        assert page1["total"] == page2["total"]

    def test_pending_captcha_in_app_and_feed(self, client, sync_db) -> None:
        """Pending-капча выводится блоком и на главном экране, и в /chat/feed."""
        sync, db_path, db = sync_db
        loop = asyncio.get_event_loop()
        loop.run_until_complete(
            db.record_pending_captcha(
                signature="пришли свое расположение",
                message_text="Пришли свое расположение и увидишь анкеты рядом",
                buttons_json='["Продолжить смотреть анкеты"]',
            )
        )

        self._login(client)
        body = client.get("/chat").get_data(as_text=True)
        assert 'id="pending"' in body
        assert "Пришли свое расположение" in body
        assert body.index('id="pending"') < body.index('id="feed"')

        data = client.get("/chat/feed?page=1").get_json()
        assert "Пришли свое расположение" in data["pending"]

    def test_page_has_signature_and_clean_js(self, client, sync_db) -> None:
        """data-sig заполнен, внешний JS подключён (без битых '&amp;')."""
        self._login(client)
        body = client.get("/").get_data(as_text=True)
        m = re.search(r'data-sig="([^"]*)"', body)
        assert m and m.group(1).strip(), "data-sig должен содержать сигнатуру"
        assert "/static/js/app.js" in body


def _card_html(page_html: str, profile_id: int) -> str:
    """Весь <article> строки анкеты (data-decision живёт в открывающем теге)."""
    marker = f'id="profile-{profile_id}"'
    idx = page_html.index(marker)
    start = page_html.rfind("<article", 0, idx)
    end = page_html.index("</article>", idx)
    return page_html[start:end]


class TestStreamErrorBanner:
    """Красный баннер сбоя автопотока (бот не листает анкеты)."""

    _PAYLOAD = {
        "message": "Бот не может продолжить ленту анкет Дайвинчика",
        "reason": "Leo ждёт текст в композере",
        "snapshot": ["Leo: Отправь текст…"],
        "at": "2026-10-19T10:00:00+00:00",
    }

    def _seed(self, db) -> None:
        import json

        asyncio.get_event_loop().run_until_complete(
            db.set_state("stream_error", json.dumps(self._PAYLOAD, ensure_ascii=False))
        )

    def test_banner_on_index(self, client, sync_db) -> None:
        _login(client)
        _, _, db = sync_db
        self._seed(db)
        body = client.get("/").get_data(as_text=True)
        assert "Бот не может продолжить ленту анкет" in body
        assert 'id="stream-banner"' in body

    def test_banner_in_feed_json(self, client, sync_db) -> None:
        _login(client)
        _, _, db = sync_db
        self._seed(db)
        data = client.get("/chat/feed?page=1").get_json()
        assert "banner" in data
        assert "Бот не может продолжить ленту анкет" in data["banner"]

    def test_signature_changes_with_stream_error(self, client, sync_db) -> None:
        _login(client)
        _, _, db = sync_db
        before = client.get("/chat/new-count").get_json()["signature"]
        self._seed(db)
        after = client.get("/chat/new-count").get_json()["signature"]
        assert before != after

    def test_banner_absent_without_error(self, client, sync_db) -> None:
        _login(client)
        data = client.get("/chat/feed?page=1").get_json()
        assert "Бот не может продолжить ленту анкет" not in data.get("banner", "")

    def test_sync_db_get_stream_error(self, sync_db) -> None:
        _, _, db = sync_db
        sync = sync_db[0]
        assert sync.get_stream_error() is None
        self._seed(db)
        parsed = sync.get_stream_error()
        assert parsed and parsed["reason"] == "Leo ждёт текст в композере"

    def _seed_match_stop(self, db) -> None:
        import json

        payload = {
            "kind": "match_stop",
            "message": "Поиск остановлен: тебя лайкнули",
            "reason": "Ты понравился 1 девушке, показать её?",
            "at": "2026-10-29T04:18:31+00:00",
        }
        asyncio.get_event_loop().run_until_complete(
            db.set_state("stream_error", json.dumps(payload, ensure_ascii=False))
        )

    def test_match_stop_banner_has_resume_button(self, client, sync_db) -> None:
        _login(client)
        _, _, db = sync_db
        self._seed_match_stop(db)
        banner = client.get("/chat/feed?page=1").get_json()["banner"]
        assert 'id="resume-search"' in banner
        assert 'action="/chat/resume"' in banner

    def test_generic_banner_has_no_resume_button(self, client, sync_db) -> None:
        _login(client)
        _, _, db = sync_db
        self._seed(db)
        banner = client.get("/chat/feed?page=1").get_json()["banner"]
        assert 'id="resume-search"' not in banner

    def test_resume_route_calls_bridge(self, client, monkeypatch) -> None:
        _login(client)
        calls: list[int] = []

        def fake_resume() -> str:
            calls.append(1)
            return "OK"

        monkeypatch.setattr("web.actions.resume_search_sync", fake_resume)
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token", "")
        resp = client.post("/chat/resume", data={"csrf_token": token})
        assert resp.status_code == 302
        assert calls == [1]

    def test_resume_route_requires_csrf(self, client) -> None:
        _login(client)
        resp = client.post("/chat/resume", data={"csrf_token": "bad"})
        assert resp.status_code == 403


class TestFeedAlbums:
    """Анкета-альбом (фото + подпись, затем фото без текста) — одна строка, как в TG."""

    @staticmethod
    def _item(direction: str, kind: str, text: str, pid: int | None, mid: int) -> dict:
        return {
            "direction": direction, "kind": kind, "text": text,
            "profile_id": pid, "photos": [mid] if kind == "photo" and pid else [],
            "telegram_message_id": mid,
        }

    def test_album_photos_merge_into_card(self) -> None:
        items = [
            self._item("out", "message", "👎", None, 10),
            self._item("in", "photo", "суслик, 18, питер", 7, 11),
            self._item("in", "photo", "", 7, 12),
            self._item("in", "photo", "", 7, 13),
            self._item("out", "message", "👎", None, 14),
        ]
        out = SyncDB._group_albums(items)
        assert [i["telegram_message_id"] for i in out] == [10, 11, 14]
        assert out[1]["photos"] == [11, 12, 13]

    def test_album_whole_in_newest_first_feed(self, sync_db) -> None:
        """Лента от новых к старым, а альбом — одна строка: фото по порядку,
        подпись при них; наш 👎 выше анкеты (он позже), старое — ниже."""
        sync, _, db = sync_db
        loop = asyncio.get_event_loop()

        async def _seed() -> None:
            pid = await db.insert_profile(
                name="суслик", age=18, raw_city="питер",
                normalized_city="Санкт-Петербург", description="",
                fingerprint="fp-suslik", source_chat_id=1234060895,
                source_message_id=34787, first_seen_at="2025-03-01T10:00:00",
                last_seen_at="2025-03-01T10:00:00", status="NEW",
            )
            rows = [
                (34786, "✨🔍", "", False),
                (34787, "суслик, 18, питер", "photo", False),
                (34788, "", "photo", False),
                (34789, "", "photo", False),
                (34790, "👎", "", True),
            ]
            for tm, text, media, out in rows:
                ts = f"2025-03-01T10:00:{tm - 34780:02d}"
                if media:
                    await db.link_profile_message(
                        profile_id=pid, telegram_message_id=tm,
                        chat_id=1234060895, created_at=ts,
                    )
                await db.save_raw_message(
                    telegram_message_id=tm, chat_id=1234060895,
                    sender_id=1 if out else 1234060895, sender_username="",
                    sender_name="", message_date=ts, text=text,
                    raw_entities="[]", reply_markup="[]", media_type=media,
                    received_at=ts, is_outgoing=out,
                )

        loop.run_until_complete(_seed())
        items = sync.get_chat_feed(page=1, per_page=40)["items"]
        # Верх ленты — свежее (ниже — более ранние анкеты из фикстуры).
        assert [i["text"] for i in items[:3]] == ["👎", "суслик, 18, питер", "✨🔍"]
        assert items[1]["photos"] == [34787, 34788, 34789]

    def test_other_profile_photo_not_merged(self) -> None:
        """Фото чужой анкеты / после нашего сообщения — отдельная строка."""
        items = [
            self._item("in", "photo", "Аня, 18, СПб", 1, 20),
            self._item("in", "photo", "", 2, 21),
            self._item("out", "message", "👎", None, 22),
            self._item("in", "photo", "", 2, 23),
        ]
        out = SyncDB._group_albums(items)
        assert len(out) == 4
        assert out[0]["photos"] == [20]


class TestChatFeedMirror:
    """Лента = прямое дублирование Telegram-чата с Leo.

    ОДНО сообщение = ОДНА строка (входящее Leo / наше исходящее). Аннотации
    ``sent_messages``/``auto_actions_log`` в ленту НЕ подмешиваются: их
    ``telegram_message_id`` указывает на карточку анкеты, а не на само
    сообщение-реакцию, из-за чего лента съезжала на соседнюю анкету.
    """

    def _seed(self, db, *, card_tm: int, reaction_tm: int,
              account_session: str = "") -> None:
        loop = asyncio.get_event_loop()

        async def _seed() -> None:
            await db.save_raw_message(
                telegram_message_id=card_tm, chat_id=1234060895,
                sender_id=1234060895, sender_username="", sender_name="Leo",
                message_date="2025-03-01T10:00:00", text="Варвара, 20, Сургут",
                raw_entities="[]", reply_markup="[]", media_type="",
                received_at="2025-03-01T10:00:00",
                account_session=account_session,
            )
            await db.save_raw_message(
                telegram_message_id=reaction_tm, chat_id=1234060895,
                sender_id=1753676469, sender_username="", sender_name="",
                message_date="2025-03-01T10:00:00", text="👎", raw_entities="[]",
                reply_markup="[]", media_type="",
                received_at="2025-03-01T10:00:00", is_outgoing=True,
                account_session=account_session,
            )

        loop.run_until_complete(_seed())

    def test_incoming_and_outgoing_are_separate_rows(self, sync_db) -> None:
        sync, _, db = sync_db
        self._seed(db, card_tm=8001, reaction_tm=8002)
        rows = sync.get_chat_feed(page=1, per_page=50)["items"]
        by = {
            i["telegram_message_id"]: i for i in rows
            if i["telegram_message_id"] in (8001, 8002)
        }
        assert by[8001]["direction"] == "in" and by[8001]["sender"] == "Leo"
        assert by[8002]["direction"] == "out" and by[8002]["sender"] == "Я"
        assert by[8002]["text"] == "👎"

    def test_annotation_does_not_create_extra_row(self, sync_db) -> None:
        """Аннотация исходящего не добавляет вторую строку (только чат)."""
        sync, _, db = sync_db
        self._seed(db, card_tm=8001, reaction_tm=8002)
        loop = asyncio.get_event_loop()
        loop.run_until_complete(
            db.record_sent_message(
                "👎", 1234060895, 8002, source="manual", action="DISLIKE",
                profile_id=3,
            )
        )
        rows = sync.get_chat_feed(page=1, per_page=50)["items"]
        out = [i for i in rows if i["telegram_message_id"] == 8002]
        assert len(out) == 1, "аннотация не должна добавлять вторую строку"

    def test_annotation_without_raw_not_shown(self, sync_db) -> None:
        """Аннотация без сырого сообщения в ленту не попадает (только чат)."""
        sync, _, db = sync_db
        loop = asyncio.get_event_loop()
        loop.run_until_complete(
            db.record_sent_message(
                "👎", 1234060895, 8902, source="manual", action="DISLIKE",
                profile_id=3,
            )
        )
        rows = sync.get_chat_feed(page=1, per_page=50)["items"]
        assert all(i["telegram_message_id"] != 8902 for i in rows)

    def test_same_tm_in_and_out_both_shown(self, sync_db) -> None:
        """Входящее и исходящее с одним номером — два разных сообщения."""
        sync, _, db = sync_db
        loop = asyncio.get_event_loop()
        ts = "2025-03-04T10:00:00"

        async def _seed() -> None:
            await db.save_raw_message(
                telegram_message_id=9001, chat_id=1234060895,
                sender_id=1234060895, sender_username="", sender_name="Leo",
                message_date=ts, text="Лиза, 21, Казань", raw_entities="[]",
                reply_markup="[]", media_type="", received_at=ts,
            )
            await db.save_raw_message(
                telegram_message_id=9001, chat_id=1234060895,
                sender_id=1753676469, sender_username="", sender_name="",
                message_date=ts, text="❤️", raw_entities="[]", reply_markup="[]",
                media_type="", received_at=ts, is_outgoing=True,
            )

        loop.run_until_complete(_seed())
        rows = [
            i for i in sync.get_chat_feed(page=1, per_page=50)["items"]
            if i["telegram_message_id"] == 9001
        ]
        assert len(rows) == 2, "ожидали 2 разных сообщения с одним номером"
        assert {i["direction"] for i in rows} == {"in", "out"}

    def test_photo_message_maps_to_profile(self, sync_db) -> None:
        """Фото-сообщение отдаётся фото и знает свою анкету (для фильтра)."""
        sync, _, _ = sync_db
        rows = sync.get_chat_feed(page=1, per_page=50)["items"]
        photo = [i for i in rows if i["telegram_message_id"] == 111][0]
        assert photo["media_type"] == "photo"
        assert photo["kind"] == "photo"
        assert photo["profile_id"] == 1
        assert photo["decision"] == "LIKE"

    def test_total_counts_raw_messages(self, sync_db) -> None:
        sync, _, db = sync_db
        before = sync.get_chat_feed(page=1, per_page=50)["total"]
        self._seed(db, card_tm=8201, reaction_tm=8202)
        after = sync.get_chat_feed(page=1, per_page=50)["total"]
        assert after - before == 2, "карточка и реакция — два сообщения"

    def test_empty_message_skipped(self, sync_db) -> None:
        sync, _, db = sync_db
        loop = asyncio.get_event_loop()
        loop.run_until_complete(
            db.save_raw_message(
                telegram_message_id=8301, chat_id=1234060895,
                sender_id=1234060895, sender_username="", sender_name="Leo",
                message_date="2025-03-03T10:00:00", text="",
                raw_entities="[]", reply_markup="[]", media_type="",
                received_at="2025-03-03T10:00:00",
            )
        )
        rows = sync.get_chat_feed(page=1, per_page=50)["items"]
        assert all(i["telegram_message_id"] != 8301 for i in rows)

    def test_account_session_filters_other_account(self, sync_db) -> None:
        """Лента показывает только выбранный аккаунт (+ legacy с пустой сессией)."""
        sync, _, db = sync_db
        self._seed(db, card_tm=8401, reaction_tm=8402, account_session="dvai")
        self._seed(db, card_tm=8411, reaction_tm=8412, account_session="dvai_2")
        rows = sync.get_chat_feed(
            page=1, per_page=50, account_session="dvai"
        )["items"]
        tms = {i["telegram_message_id"] for i in rows}
        assert 8401 in tms and 8402 in tms
        assert 8411 not in tms and 8412 not in tms

    def test_feed_shows_only_leo_chat(self, client, sync_db) -> None:
        """Сообщения других чатов (история до allowlist'а) в ленту не попадают.

        Регресс: группа с номерами 262k вставала «самой свежей» между
        анкетами Leo (номера ~34k) — лента выглядела как анархия.
        """
        _login(client)
        _, _, db = sync_db
        self._seed(db, card_tm=34000, reaction_tm=34001)
        loop = asyncio.get_event_loop()
        loop.run_until_complete(db.save_raw_message(
            telegram_message_id=262734, chat_id=-1001880519982,
            sender_id=8779855177, sender_username="", sender_name="",
            message_date="2025-03-01T10:00:00",
            text="Окулисты есть здесь? Напишите в лс",
            raw_entities="[]", reply_markup="[]", media_type="",
            received_at="2025-03-01T10:00:00",
        ))
        body = client.get("/chat/feed?page=1").get_json()["feed"]
        assert "Варвара, 20, Сургут" in body
        assert "Окулисты" not in body
        page = client.get("/").get_data(as_text=True)
        assert "Окулисты" not in page

    def test_pagination_walks_all_pages_without_duplicates(self, sync_db) -> None:
        """Постраничный обход даёт каждое сообщение ровно один раз."""
        sync, _, db = sync_db
        loop = asyncio.get_event_loop()

        async def _seed() -> None:
            for k in range(12):
                ts = f"2025-03-05T10:{k:02d}:00"
                await db.save_raw_message(
                    telegram_message_id=9300 + k, chat_id=1234060895,
                    sender_id=1234060895, sender_username="", sender_name="Leo",
                    message_date=ts, text=f"MSG-{k}", raw_entities="[]",
                    reply_markup="[]", media_type="", received_at=ts,
                )
                await db.save_raw_message(
                    telegram_message_id=9400 + k, chat_id=1234060895,
                    sender_id=1753676469, sender_username="", sender_name="",
                    message_date=ts, text="👎", raw_entities="[]",
                    reply_markup="[]", media_type="", received_at=ts,
                    is_outgoing=True,
                )

        loop.run_until_complete(_seed())

        total = sync.get_chat_feed(page=1, per_page=50)["total"]
        seen: set[tuple] = set()
        rows: list[dict] = []
        page = 1
        while page <= 20:
            items = sync.get_chat_feed(page=page, per_page=5)["items"]
            if not items:
                break
            for item in items:
                key = (
                    item["chat_id"], item["telegram_message_id"], item["direction"]
                )
                assert key not in seen, f"дубль {key} на странице {page}"
                seen.add(key)
            rows += items
            page += 1

        assert len(rows) == len(seen) == total
        assert sum(1 for i in rows if i["direction"] == "out") == 12

# ── Debug console (Stage 9) ───────────────────────────────────────

class TestDebugConsole:
    """Панель отладки: снимок состояния + логи из памяти."""

    def test_debug_requires_login(self, client) -> None:
        resp = client.get("/chat/debug")
        assert resp.status_code == 302
        assert "/login" in resp.headers.get("Location", "")

    def test_debug_payload_shape(self, client, sync_db) -> None:
        _login(client)
        resp = client.get("/chat/debug")
        assert resp.status_code == 200
        data = json.loads(resp.get_data(as_text=True))
        assert isinstance(data["logs"], list)
        state = data["state"]
        for key in (
            "mode", "account_session", "stream_error", "likes_exhausted_until",
            "counts", "signals", "last_auto_actions",
        ):
            assert key in state, f"нет ключа {key} в состоянии отладки"
        assert state["mode"] == "OBSERVE"
        assert state["counts"]["raw_messages"] >= 3
        assert state["counts"]["profiles"] >= 3

    def test_debug_captures_logs_and_filters_level(self, sync_db) -> None:
        import app.logging as logging_mod
        from loguru import logger

        logging_mod._buffer_sink_id = None
        logging_mod._ensure_buffer_sink()
        with logging_mod._buffer_lock:
            logging_mod.LOG_BUFFER.clear()

        logger.info("dvai-debug-info")
        logger.error("dvai-debug-error")

        msgs = [e["message"] for e in logging_mod.get_recent_logs()]
        assert "dvai-debug-info" in msgs
        assert "dvai-debug-error" in msgs

        err_msgs = [e["message"] for e in logging_mod.get_recent_logs(min_level="ERROR")]
        assert "dvai-debug-error" in err_msgs
        assert "dvai-debug-info" not in err_msgs

    def test_debug_level_query_param(self, client, sync_db) -> None:
        import app.logging as logging_mod
        from loguru import logger

        logging_mod._buffer_sink_id = None
        logging_mod._ensure_buffer_sink()
        with logging_mod._buffer_lock:
            logging_mod.LOG_BUFFER.clear()
        logger.info("dvai-debug-info-2")
        logger.error("dvai-debug-error-2")

        _login(client)
        resp = client.get("/chat/debug?level=ERROR")
        assert resp.status_code == 200
        data = json.loads(resp.get_data(as_text=True))
        assert data["logs"], "должны быть ERROR-логи"
        assert {e["level"] for e in data["logs"]} <= {"ERROR", "CRITICAL"}
        assert any(e["message"] == "dvai-debug-error-2" for e in data["logs"])

    def test_debug_button_and_drawer_in_shell(self, client, sync_db) -> None:
        _login(client)
        html = client.get("/").get_data(as_text=True)
        assert 'id="debug-open"' in html
        assert 'id="debug-drawer"' in html
        assert 'id="debug-logs"' in html


class TestDecisionFeed:
    """Улучшенная лента: только ❤️/👎 бота и причина решения."""

    @staticmethod
    def _seed_actions(db) -> None:
        loop = asyncio.get_event_loop()

        async def _seed() -> None:
            # Алиса (#1, карточка 111) — лайк + «Берем)»; Варвара (#3, 113) — дизлайк.
            await db.record_auto_action(1, "LIKE", "LIKE", 1234060895, 111)
            await db.record_auto_action(
                1, "MESSAGE", "LIKE", 1234060895, 111, message_text="Берем)",
            )
            await db.record_auto_action(3, "DISLIKE", "DISLIKE", 1234060895, 113)

        loop.run_until_complete(_seed())

    def test_only_likes_and_dislikes_with_reasons(self, sync_db) -> None:
        sync, _, db = sync_db
        self._seed_actions(db)
        items = sync.get_decision_feed(page=1, per_page=50)["items"]

        assert [i["action"] for i in items] == ["DISLIKE", "LIKE"]  # свежие сверху
        dislike, like = items
        assert dislike["name"] == "Варвара"
        assert "Возраст не подходит" in dislike["reasons"]
        assert "Город не в списке" in dislike["reasons"]
        assert like["name"] == "Алиса"
        assert "Анкета содержательная, стоп-признаков нет" in like["reasons"]
        assert "Аниме" in like["reasons"]
        assert like["message_text"] == "Берем)"
        assert like["photos"] == [111]
        # Служебные коды не показываются.
        assert not any("FILTER_REJECTED" in r for r in dislike["reasons"])

    def test_strict_account_filter(self, sync_db) -> None:
        """Карточки другого аккаунта в улучшенную ленту не попадают."""
        sync, _, db = sync_db
        self._seed_actions(db)
        data = sync.get_decision_feed(account_session="dvai_2")
        assert data["items"] == [] and data["total"] == 0

    def test_view_toggle_in_toolbar(self, client) -> None:
        _login(client)
        html = client.get("/").get_data(as_text=True)
        assert 'id="view-seg"' in html
        assert "1 · Прямой" in html and "2 · Улучшенный" in html
        assert 'data-view="direct"' in html

    def test_smart_view_renders_reasons_and_is_remembered(
        self, client, sync_db,
    ) -> None:
        _, _, db = sync_db
        self._seed_actions(db)
        _login(client)
        html = client.get("/?view=smart").get_data(as_text=True)
        assert 'data-view="smart"' in html
        assert "row--decision" in html
        assert "Возраст не подходит" in html
        assert "Написал при лайке: «Берем)»" in html
        # Выбор запомнен: polling-фрагмент без параметра — тот же вид.
        data = json.loads(client.get("/chat/feed").get_data(as_text=True))
        assert data["view"] == "smart"
        assert "row--decision" in data["feed"]
        # Вернуться к прямому виду.
        data = json.loads(client.get("/chat/feed?view=direct").get_data(as_text=True))
        assert data["view"] == "direct"
        assert "row--decision" not in data["feed"]


class TestReasonLabels:
    """Перевод кодов причин — общий для Telegram и сайта."""

    def test_scoring_codes_are_humanized(self) -> None:
        from services.reason_labels import describe_reasons

        lines = describe_reasons([
            "LOW_INFO_SKIP:words=7", "USER_LIKE_HARD:политех",
            "HARD_NEGATIVE:smoking:курю", "FILTER_REJECTED",
        ])
        assert lines == [
            "Мало текста в анкете: 7 значимых слов",
            "Ваша сильная метка интереса: политех",
            "Курит: курю",
        ]


class TestPhotoRating:
    """Страница быстрой разметки внешности (/rate) — метки для фото-модели."""

    @staticmethod
    def _csrf(client) -> str:
        with client.session_transaction() as sess:
            return sess.get("_csrf_token", "")

    def _next(self, client, **params) -> dict:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        resp = client.get(f"/rate/next?{query}")
        assert resp.status_code == 200
        return json.loads(resp.get_data(as_text=True))

    def test_requires_login(self, client) -> None:
        for url in ("/rate", "/rate/next"):
            resp = client.get(url)
            assert resp.status_code == 302
            assert "/login" in resp.headers.get("Location", "")

    def test_page_and_link_from_main_screen(self, client) -> None:
        _login(client)
        html = client.get("/rate").get_data(as_text=True)
        assert 'id="rate"' in html
        assert "js/rate.js" in html
        assert 'data-rate="LIKE"' in html and 'data-rate="DISLIKE"' in html
        main = client.get("/").get_data(as_text=True)
        assert 'id="rate-open"' in main and 'href="/rate"' in main

    def test_next_returns_profiles_with_photo_urls(self, client) -> None:
        _login(client)
        data = self._next(client, limit=10)
        pids = {i["profile_id"] for i in data["items"]}
        assert pids == {1, 2, 3}
        item = next(i for i in data["items"] if i["profile_id"] == 1)
        assert item["photos"] == [{"message_id": 111, "src": "/photos/1/111.jpg"}]
        assert data["stats"]["left"] == 3
        assert data["stats"]["rated"] == 0

    def test_rate_saves_and_removes_from_queue(self, client, sync_db) -> None:
        sync, _, _ = sync_db
        _login(client)
        resp = client.post("/rate/1/111", data={"rating": "LIKE", "csrf_token": self._csrf(client)})
        assert resp.status_code == 200
        stats = json.loads(resp.get_data(as_text=True))["stats"]
        assert stats["LIKE"] == 1 and stats["rated"] == 1 and stats["left"] == 2
        assert 1 not in {i["profile_id"] for i in self._next(client, limit=10)["items"]}
        # Повторная оценка перезаписывает прежнюю, а не дублирует.
        client.post("/rate/1/111", data={"rating": "DISLIKE", "csrf_token": self._csrf(client)})
        stats = sync.get_photo_rating_stats()
        assert stats["LIKE"] == 0 and stats["DISLIKE"] == 1 and stats["rated"] == 1

    def test_partially_rated_profile_returns_only_unrated_photos(self, sync_db) -> None:
        """Анкета с 3 фото, оценено одно — в очереди остаются другие."""
        sync, _, db = sync_db

        async def _seed() -> None:
            for tm in (112, 113):
                await db.link_profile_message(
                    profile_id=1, telegram_message_id=tm, chat_id=1234060895,
                    created_at="2026-01-01", account_session="dvai",
                )
                await db.save_raw_message(
                    telegram_message_id=tm, chat_id=1234060895, sender_id=1,
                    sender_username="", sender_name="Leo", message_date="2026-01-01",
                    text="", raw_entities="[]", reply_markup="[]", media_type="photo",
                    received_at="2026-01-01", account_session="dvai",
                )

        asyncio.get_event_loop().run_until_complete(_seed())
        assert [p["message_id"] for p in sync._rating_photos(1)] == [111, 112, 113]
        sync.save_photo_item_rating(1, 111, "LIKE")
        item = next(i for i in sync.get_rating_queue(limit=10) if i["profile_id"] == 1)
        assert [p["message_id"] for p in item["photos"]] == [112, 113]
        sync.save_photo_item_rating(1, 112, "DISLIKE")
        sync.save_photo_item_rating(1, 113, "DISLIKE")
        assert 1 not in {i["profile_id"] for i in sync.get_rating_queue(limit=10)}
        sync.delete_photo_item_rating(1, 113)  # «назад» возвращает фото
        item = next(i for i in sync.get_rating_queue(limit=10) if i["profile_id"] == 1)
        assert [p["message_id"] for p in item["photos"]] == [113]

    def test_old_whole_profile_rating_excludes_profile(self, sync_db) -> None:
        sync, _, _ = sync_db
        sync._execute(
            "INSERT INTO photo_ratings (profile_id, rating, created_at) VALUES (2, 'LIKE', 'x')"
        )
        assert 2 not in {i["profile_id"] for i in sync.get_rating_queue(limit=10)}
        assert sync.get_photo_rating_stats()["profiles_rated"] == 1

    def test_old_multi_photo_profile_rerated_after_new_ones(self, sync_db) -> None:
        """Новые кончились — старая анкета с 2+ фото идёт на переоценку по фото."""
        sync, _, db = sync_db

        async def _seed() -> None:
            await db.link_profile_message(
                profile_id=1, telegram_message_id=112, chat_id=1234060895,
                created_at="2026-01-01", account_session="dvai",
            )
            await db.save_raw_message(
                telegram_message_id=112, chat_id=1234060895, sender_id=1,
                sender_username="", sender_name="Leo", message_date="2026-01-01",
                text="", raw_entities="[]", reply_markup="[]", media_type="photo",
                received_at="2026-01-01", account_session="dvai",
            )

        asyncio.get_event_loop().run_until_complete(_seed())
        for pid in (1, 2):
            sync._execute(
                "INSERT INTO photo_ratings (profile_id, rating, created_at) "
                "VALUES (?, 'LIKE', 'x')", (pid,),
            )
        items = sync.get_rating_queue(limit=10)
        assert [i["profile_id"] for i in items] == [3, 1]  # 2 — одно фото, не нужна
        assert not items[0].get("rerate") and items[1]["rerate"]
        assert [p["message_id"] for p in items[1]["photos"]] == [111, 112]

    def test_undo_returns_profile_to_queue(self, client, sync_db) -> None:
        sync, _, _ = sync_db
        _login(client)
        token = self._csrf(client)
        mid = self._next(client, limit=10, exclude="1,3")["items"][0]["photos"][0]["message_id"]
        client.post(f"/rate/2/{mid}", data={"rating": "SKIP", "csrf_token": token})
        assert 2 not in {i["profile_id"] for i in self._next(client, limit=10)["items"]}
        resp = client.post(f"/rate/2/{mid}/undo", data={"csrf_token": token})
        assert resp.status_code == 200
        assert sync.get_photo_rating_stats()["rated"] == 0
        assert 2 in {i["profile_id"] for i in self._next(client, limit=10)["items"]}

    def test_rejects_bad_rating_and_missing_csrf(self, client, sync_db) -> None:
        sync, _, _ = sync_db
        _login(client)
        assert client.post("/rate/1/111", data={"rating": "MAYBE",
                                                "csrf_token": self._csrf(client)}).status_code == 400
        assert client.post("/rate/1/111", data={"rating": "LIKE"}).status_code == 403
        assert sync.get_photo_rating_stats()["rated"] == 0

    def test_exclude_param_skips_queued_profiles(self, client) -> None:
        _login(client)
        data = self._next(client, limit=10, exclude="1,2")
        assert {i["profile_id"] for i in data["items"]} == {3}

    def test_cached_photos_come_first(self, sync_db) -> None:
        sync, _, _ = sync_db
        order = [i["profile_id"] for i in sync.get_rating_queue(limit=10)]
        for pid in order:
            first = sync.get_rating_queue(limit=1, prefer={pid})[0]["profile_id"]
            assert first == pid

    def test_photos_only_from_one_account(self, sync_db) -> None:
        """Фото анкеты — с одного аккаунта (последний показ): у аккаунтов свои
        id сообщений, смешивать нельзя."""
        sync, _, db = sync_db

        async def _seed() -> None:
            for tm_id, sess in ((90, "dvai_2"), (91, "dvai_2")):
                await db.link_profile_message(
                    profile_id=1, telegram_message_id=tm_id, chat_id=1234060895,
                    created_at="2025-01-05T00:00:00", account_session=sess,
                )
                await db.save_raw_message(
                    telegram_message_id=tm_id, chat_id=1234060895, sender_id=100,
                    sender_username="", sender_name="Leo",
                    message_date="2025-01-05T00:00:00", text="", raw_entities="[]",
                    reply_markup="[]", media_type="photo",
                    received_at="2025-01-05T00:00:00",
                )

        asyncio.get_event_loop().run_until_complete(_seed())
        photos = sync._rating_photos(1)
        # Последний показ — dvai (tm 111 > 91): берём только его фото.
        assert [p["message_id"] for p in photos] == [111]
        assert {p["account_session"] for p in photos} == {"dvai"}

    def test_photos_only_from_last_showing(self, sync_db) -> None:
        """Повторный показ анкеты не дублирует фото: берём только последний
        альбом (номера сообщений подряд)."""
        sync, _, db = sync_db

        async def _seed() -> None:
            for tm_id in (200, 201, 202):  # второй показ анкеты 1, альбом из 3 фото
                await db.link_profile_message(
                    profile_id=1, telegram_message_id=tm_id, chat_id=1234060895,
                    created_at="2025-01-06T00:00:00", account_session="dvai",
                )
                await db.save_raw_message(
                    telegram_message_id=tm_id, chat_id=1234060895, sender_id=100,
                    sender_username="", sender_name="Leo",
                    message_date="2025-01-06T00:00:00", text="", raw_entities="[]",
                    reply_markup="[]", media_type="photo",
                    received_at="2025-01-06T00:00:00",
                )

        asyncio.get_event_loop().run_until_complete(_seed())
        assert [p["message_id"] for p in sync._rating_photos(1)] == [200, 201, 202]

    def test_photo_query_uses_raw_index(self, sync_db) -> None:
        """Регресс: проверка «сообщение — фото» должна идти по idx_raw_unique.

        Без условия на account_session SQLite сканировал всю raw_messages на
        каждую строку: на проде 7.9 с на запрос, между анкетами висело
        «Загружаю…»."""
        sync, _, _ = sync_db
        plan = sync._query("EXPLAIN QUERY PLAN SELECT 1 " + SyncDB._RATE_PHOTO_SQL)
        details = " | ".join(str(r.get("detail", "")) for r in plan)
        assert "SCAN rm" not in details, details
        assert "idx_raw_unique" in details, details

    def test_swipe_card_on_page(self, client) -> None:
        """Телефон: карточка со свайпом (как в Tinder) и полосками фото."""
        _login(client)
        html = client.get("/rate").get_data(as_text=True)
        assert 'id="rate-card"' in html and 'id="rate-dots"' in html
        assert 'id="rate-pos"' in html  # «фото 2 из 3»
        js = client.get("/static/js/rate.js").get_data(as_text=True)
        for marker in ("pointerdown", "pointerup", "SWIPE_SHARE", "' из '"):
            assert marker in js, marker
        css = client.get("/static/css/app.css").get_data(as_text=True)
        card = css.split(".rate__card {", 1)[1].split("}", 1)[0]
        assert "touch-action: pan-y" in card  # вертикальный скролл не ломается

    def test_queue_never_repeats_and_keys_are_safe(self, client) -> None:
        """Регресс (прод 2026-10-22): после ~150 оценок анкеты шли по второму
        кругу — список «уже есть» для сервера обрезался до 150 и терял саму
        очередь. Плюс фокус на кнопке и автоповтор клавиши."""
        _login(client)
        js = client.get("/static/js/rate.js").get_data(as_text=True)
        assert "slice(-150)" not in js
        assert "seen[" in js                      # анкета в очередь — один раз
        assert "e.repeat" in js                   # зажатая клавиша не оценивает пачку
        assert "addEventListener('keydown'" in js and "}, true);" in js  # фаза захвата
        assert "mousedown" in js                  # кнопки не забирают фокус

    def test_vice_rating_button(self, client, sync_db) -> None:
        """«🚬/🍷 на фото» — отдельная оценка VICE (метки для детектора)."""
        sync, _, _ = sync_db
        _login(client)
        html = client.get("/rate").get_data(as_text=True)
        assert 'data-rate="VICE"' in html and 'id="rate-vice"' in html
        mid = self._next(client, limit=10, exclude="1,2")["items"][0]["photos"][0]["message_id"]
        resp = client.post(f"/rate/3/{mid}", data={"rating": "VICE", "csrf_token": self._csrf(client)})
        assert resp.status_code == 200
        assert json.loads(resp.get_data(as_text=True))["stats"]["VICE"] == 1
        assert sync.get_photo_rating_stats()["rated"] == 1
        js = client.get("/static/js/rate.js").get_data(as_text=True)
        assert "ArrowUp: 'VICE'" in js


    def test_uncertain_profiles_come_first(self, sync_db) -> None:
        """Очередь: сначала анкеты, где модель сомневается (score ближе к 0.5)."""
        sync, _, db = sync_db

        async def _seed() -> None:
            for pid, score in ((1, 0.95), (2, 0.52), (3, 0.30)):
                await db.save_photo_score(
                    profile_id=pid, account_session="dvai", chat_id=1234060895,
                    telegram_message_id=110 + pid, score=score, verdict="REVIEW",
                    smoke_prob=None, alcohol_prob=None, n_photos=1, model_version="v",
                )

        asyncio.get_event_loop().run_until_complete(_seed())
        items = sync.get_rating_queue(limit=10)
        assert [i["profile_id"] for i in items] == [2, 3, 1]
        assert all(i["uncertain"] for i in items)
        assert items[0]["score"] == 0.52

    def test_explore_slot_takes_normal_order(self, sync_db) -> None:
        """Каждая N-я анкета — из обычного порядка, даже если вердикт есть у всех
        (после фонового досчёта так и есть), и без повторов."""
        sync, _, _ = sync_db
        sync._latest_photo_scores = lambda: {1: 0.9, 2: 0.5, 3: 0.1}  # type: ignore[method-assign]
        sync.RATE_EXPLORE_EVERY = 2
        items = sync.get_rating_queue(limit=10)
        ids = [i["profile_id"] for i in items]
        assert sorted(ids) == [1, 2, 3]                 # все, без повторов
        assert ids[0] == 2 and items[0]["uncertain"]    # самая сомнительная — первой
        assert not items[1]["uncertain"]                # 2-я — «разведка»
        assert items[2]["uncertain"]



class TestBotErrorPopup:
    """Ошибка бота (app_state.bot_error) → всплывающее окно на главной."""

    @staticmethod
    def _set_error(db, at: str = "2026-10-23T13:01:48+00:00") -> None:
        payload = json.dumps({"at": at, "where": "collector:x:1",
                              "message": "AutoAction error: <boom>", "count": 2})
        asyncio.get_event_loop().run_until_complete(db.set_state("bot_error", payload))

    def test_popup_in_shell_and_data_escaped(self, client, sync_db) -> None:
        _, _, db = sync_db
        self._set_error(db)
        _login(client)
        html = client.get("/").get_data(as_text=True)
        assert 'id="bot-error"' in html and 'id="bot-error-close"' in html
        assert "AutoAction error" in html
        assert "<boom>" not in html            # данные экранированы
        js = client.get("/static/js/app.js").get_data(as_text=True)
        assert "showBotError" in js and "dvai.botErrorSeen" in js
        assert "textContent = err.message" in js   # без innerHTML из ошибки

    def test_feed_json_carries_error_and_signature_changes(self, client, sync_db) -> None:
        _, _, db = sync_db
        _login(client)
        before = json.loads(client.get("/chat/new-count").get_data(as_text=True))["signature"]
        self._set_error(db)
        after = json.loads(client.get("/chat/new-count").get_data(as_text=True))["signature"]
        assert before != after
        data = json.loads(client.get("/chat/feed").get_data(as_text=True))
        assert data["bot_error"]["message"] == "AutoAction error: <boom>"

    def test_no_error_no_popup_data(self, client, sync_db) -> None:
        _login(client)
        data = json.loads(client.get("/chat/feed").get_data(as_text=True))
        assert data["bot_error"] is None

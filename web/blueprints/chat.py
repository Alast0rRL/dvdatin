# App blueprint — единый экран DvAI (Stage 9 UI rewrite).
#
# Один экран `/` вместо набора вкладок: лента чата + выбор режима +
# кнопки настроек (выдвижная панель, без отдельной страницы).
#
# Что отдаёт:
#   GET  /                       — весь UI (лента, капчи, настройки в drawer)
#   GET  /chat                   — синоним / (старые закладки)
#   GET  /chat/feed              — фрагмент ленты + капч (in-place polling);
#                                  ?view=direct|smart — вид ленты (в сессии)
#   GET  /chat/new-count         — сигнатура состояния (polling)
#   GET  /chat/debug             — отладочный срез: логи + состояние (polling)
#   POST /chat/captcha/<id>/answer|delete — ответ на капчу
#   POST /chat/profile/<id>/<action>     — ручное решение по REVIEW-анкете
#
# Web-слой Telegram-free: только SyncDB + config.yaml + services.

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from web.blueprints.auth import login_required
from web.blueprints.settings import _get_accounts, _load_preferences
from web.db import SyncDB

chat_bp = Blueprint("chat", __name__)

#: Режимы в тулбаре (порядок как в core.types.Mode).
MODES = ("OBSERVE", "SEMI_AUTO", "AUTO")

#: Ручные кнопки показываем ТОЛЬКО для анкет, которые AI не смог решить.
ACTIONABLE_DECISION = "REVIEW"

#: Сколько сообщений на страницу ленты.
FEED_PER_PAGE = 40

#: Вид ленты: «прямой» (зеркало чата) и «улучшенный» (только ❤️/👎 + причина).
FEED_VIEWS = ("direct", "smart")
DEFAULT_FEED_VIEW = "direct"


def _feed_view() -> str:
    """Текущий вид ленты: ``?view=`` (запоминается в сессии) или из сессии."""
    view = (request.args.get("view") or "").strip().lower()
    if view in FEED_VIEWS:
        session["feed_view"] = view
        return view
    saved = session.get("feed_view")
    return saved if saved in FEED_VIEWS else DEFAULT_FEED_VIEW


def _load_feed(
    db: SyncDB, view: str, page: int, per_page: int,
    account_session: str, chat_id: int,
) -> dict[str, Any]:
    """Страница ленты нужного вида."""
    loader = db.get_decision_feed if view == "smart" else db.get_chat_feed
    return loader(
        page=page, per_page=per_page,
        account_session=account_session, chat_id=chat_id,
    )


def _feed_template(view: str) -> str:
    return "partials/app_decisions.html" if view == "smart" else "partials/app_feed.html"


def _get_db() -> SyncDB:
    return current_app.config["SYNC_DB"]


def _get_config_path() -> Path:
    return current_app.config.get("CONFIG_PATH", Path("config/config.yaml"))


#: Чат Дайвинчика (Leo) по умолчанию — как в ``config.example.yaml``.
DEFAULT_LEO_CHAT_ID = 1234060895


def _leo_chat_id(config_path: Path) -> int:
    """chat_id Дайвинчика из ``dvinchik.chat_id`` (лента — ТОЛЬКО этот чат).

    В ``raw_messages`` бывают сообщения других чатов (история до allowlist'а:
    группы, личка). Их номера из других пространств id, и без фильтра они
    вставали в ленту между анкетами как «самые свежие».
    """
    import yaml
    try:
        with open(config_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return int((raw.get("dvinchik") or {}).get("chat_id") or DEFAULT_LEO_CHAT_ID)
    except Exception:
        return DEFAULT_LEO_CHAT_ID


def _with_buttons(captcha: dict[str, Any]) -> dict[str, Any]:
    """buttons_json (строка) → list для шаблона."""
    raw = captcha.get("buttons_json") or "[]"
    try:
        buttons = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        buttons = []
    captcha["buttons"] = buttons if isinstance(buttons, list) else []
    return captcha


def _chat_signature(signals: dict[str, Any] | None) -> str:
    """Подпись состояния чата (изменилась → обновить ленту)."""
    signals = signals or {}
    return "|".join(
        [
            str(signals.get("max_raw") or ""),
            str(signals.get("max_sent") or ""),
            str(signals.get("max_auto") or ""),
            str(signals.get("max_captcha") or ""),
            str(signals.get("pending_captchas") or 0),
            str(signals.get("stream_error_at") or ""),
            str(signals.get("bot_error_at") or ""),
        ]
    )


def _safe_next(default: str | None = None) -> str:
    """Безопасный redirect-target из формы (только локальные пути)."""
    target = request.form.get("next", "") or default or ""
    if not target.startswith("/") or target.startswith("//"):
        return url_for("chat.index")
    return target


# ── Страница-приложение ────────────────────────────────────────────

@chat_bp.route("/")
@chat_bp.route("/chat")
@login_required
def index() -> str:
    """Единый экран: лента чата + режим + настройки (drawer)."""
    db = _get_db()
    config_path = _get_config_path()

    accounts, account_session = _get_accounts(config_path)
    view = _feed_view()
    data = _load_feed(
        db, view, 1, FEED_PER_PAGE, account_session, _leo_chat_id(config_path),
    )
    signals = db.get_chat_signals(account_session, _leo_chat_id(config_path))
    pending = [_with_buttons(c) for c in db.get_pending_captchas(limit=20)]

    return render_template(
        "app.html",
        items=data.get("items", []),
        total=data.get("total", 0),
        page=data.get("page", 1),
        total_pages=data.get("total_pages", 1),
        signature=_chat_signature(signals),
        pending=pending,
        modes=MODES,
        mode=db.get_mode(config_path),
        accounts=accounts,
        account_session=account_session,
        filter_cfg=db.get_filter_config(config_path),
        preferences=_load_preferences(config_path.parent / "preferences.yaml"),
        stream_error=db.get_stream_error(),
        bot_error=db.get_bot_error(),
        view=view,
        feed_template=_feed_template(view),
    )


@chat_bp.route("/chat/debug")
@login_required
def debug() -> tuple:
    """Отладочная консоль: последние логи из памяти + снимок состояния.

    Помогает понять, почему лента встала (``stream_error``/``likes_exhausted``),
    что делал бот последними действиями и не разъехались ли счётчики БД/ленты.
    """
    from app.logging import get_recent_logs

    db = _get_db()
    config_path = _get_config_path()
    limit = max(1, min(request.args.get("limit", 200, type=int) or 200, 500))
    level = (request.args.get("level") or "").strip().upper() or None
    _, account_session = _get_accounts(config_path)

    payload = {
        "generated_at": datetime.now().strftime("%H:%M:%S"),
        "logs": get_recent_logs(limit=limit, min_level=level),
        "state": {
            "mode": db.get_mode(config_path),
            "account_session": account_session,
            **db.get_debug_states(),
            "counts": db.get_debug_counts(),
            "signals": db.get_chat_signals(account_session, _leo_chat_id(config_path)),
            "last_auto_actions": db.get_recent_auto_actions(limit=10),
        },
    }
    return (
        json.dumps(payload, ensure_ascii=False),
        200,
        {"Content-Type": "application/json; charset=utf-8"},
    )


@chat_bp.route("/chat/feed")
@login_required
def feed() -> tuple:
    """Фрагменты ленты и капч для in-place обновления (polling)."""
    db = _get_db()
    config_path = _get_config_path()
    _, account_session = _get_accounts(config_path)
    chat_id = _leo_chat_id(config_path)
    page = request.args.get("page", 1, type=int)
    # ``pages=N`` — свежие N страниц ОДНИМ срезом (polling-перерисовка). Раньше
    # фронт тянул страницы 1..N параллельно через OFFSET, и новое сообщение
    # между запросами сдвигало границы: строки на стыке дублировались/терялись.
    pages = max(1, min(request.args.get("pages", 1, type=int) or 1, 50))
    view = _feed_view()
    if pages > 1:
        data = _load_feed(
            db, view, 1, FEED_PER_PAGE * pages, account_session, chat_id,
        )
        total = data.get("total", 0)
        data["total_pages"] = max(1, (total + FEED_PER_PAGE - 1) // FEED_PER_PAGE)
    else:
        data = _load_feed(
            db, view, page, FEED_PER_PAGE, account_session, chat_id,
        )
    return (
        json.dumps(
            {
                "feed": render_template(
                    _feed_template(view), items=data.get("items", []),
                ),
                "view": view,
                "pending": render_template(
                    "partials/app_captchas.html",
                    pending=[_with_buttons(c) for c in db.get_pending_captchas(limit=20)],
                ),
                "banner": render_template(
                    "partials/app_stream_error.html",
                    stream_error=db.get_stream_error(),
                ),
                # Последняя ошибка бота — всплывающее окно (app.js showBotError).
                "bot_error": db.get_bot_error(),
                "total": data.get("total", 0),
                "page": data.get("page", 1),
                "total_pages": data.get("total_pages", 1),
            }
        ),
        200,
        {"Content-Type": "application/json; charset=utf-8"},
    )


@chat_bp.route("/chat/new-count")
@login_required
def new_count() -> tuple:
    """Сигнатура для polling: изменилась → обновить ленту."""
    config_path = _get_config_path()
    _, account_session = _get_accounts(config_path)
    signals = _get_db().get_chat_signals(
        account_session, _leo_chat_id(config_path)
    ) or {}
    return (
        json.dumps({"signature": _chat_signature(signals), **signals}),
        200,
        {"Content-Type": "application/json; charset=utf-8"},
    )


# ── Капчи ──────────────────────────────────────────────────────────

@chat_bp.route("/chat/captcha/<int:captcha_id>/answer", methods=["POST"])
@login_required
def captcha_answer(captcha_id: int) -> tuple:
    """Ответ на капчу прямо из ленты: учит бота + сразу отправляет Leo."""
    if request.form.get("csrf_token", "") != session.get("_csrf_token"):
        flash("Ошибка безопасности", "error")
        return ("CSRF token invalid", 403)

    answer = (request.form.get("answer", "") or "").strip()
    if not answer:
        flash("Пустой ответ", "error")
        return redirect(_safe_next())

    db = _get_db()
    if db.get_captcha(captcha_id) is None:
        flash("Капча не найдена", "error")
        return redirect(_safe_next())

    if not db.set_captcha_answer(captcha_id, answer):
        flash("Ошибка сохранения ответа", "error")
        return redirect(_safe_next())

    from web.actions import send_captcha_answer_sync

    try:
        status = send_captcha_answer_sync(answer)
    except Exception:
        status = "ERROR"

    if status == "SENT":
        flash(f"Ответ «{answer}» отправлен Leo и запомнен", "success")
    elif status == "DISABLED":
        flash(
            f"Ответ «{answer}» запомнен (движок неактивен — применится "
            "при следующей такой капче)",
            "warning",
        )
    else:
        flash(f"Ответ «{answer}» запомнен, но отправка в Leo не удалась", "warning")
    return redirect(_safe_next())


@chat_bp.route("/chat/captcha/<int:captcha_id>/delete", methods=["POST"])
@login_required
def captcha_delete(captcha_id: int) -> tuple:
    """«Забыть» капчу (из ленты или из блока ждущих)."""
    if request.form.get("csrf_token", "") != session.get("_csrf_token"):
        flash("Ошибка безопасности", "error")
        return ("CSRF token invalid", 403)

    _get_db().delete_captcha(captcha_id)
    flash("Капча удалена из памяти", "success")
    return redirect(_safe_next())


@chat_bp.route("/chat/resume", methods=["POST"])
@login_required
def resume_search() -> tuple:
    """«Продолжить» в баннере «Поиск остановлен: тебя лайкнули».

    Возвращает режим до остановки, жмёт «Показать» на карточке лайка
    (если она ещё активна) и продолжает ленту.
    """
    if request.form.get("csrf_token", "") != session.get("_csrf_token"):
        flash("Ошибка безопасности", "error")
        return ("CSRF token invalid", 403)

    from web.actions import resume_search_sync

    try:
        status = resume_search_sync()
    except Exception:
        status = "ERROR"
    if status == "OK":
        flash("Поиск продолжен", "success")
    elif status == "DISABLED":
        flash("Бот не запущен — поиск не продолжен", "warning")
    else:
        flash("Не удалось продолжить поиск — см. отладку", "error")
    return redirect(_safe_next())


# ── Ручное решение по REVIEW-анкете ────────────────────────────────

@chat_bp.route("/chat/profile/<int:profile_id>/<action>", methods=["POST"])
@login_required
def profile_action(profile_id: int, action: str) -> tuple:
    """❤️/👎 по анкете, которую AI не смог решить (AI=REVIEW).

    Кнопки в UI показываются ТОЛЬКО для REVIEW: остальные анкеты бот уже
    отработал сам (LIKE/DISLIKE), ручное вмешательство не нужно.
    """
    if action not in ("LIKE", "DISLIKE"):
        return ("Invalid action", 400)

    if request.form.get("csrf_token", "") != session.get("_csrf_token"):
        flash("Ошибка безопасности", "error")
        return ("CSRF token invalid", 403)

    db = _get_db()
    ai_decision = db.get_latest_ai_decision(profile_id)
    if not ai_decision:
        flash("AI-решение не найдено", "error")
        return redirect(_safe_next())

    if (ai_decision.get("decision") or "") != ACTIONABLE_DECISION:
        flash(
            f"Ручное решение доступно только для анкет на REVIEW "
            f"(тут AI={ai_decision.get('decision')})",
            "warning",
        )
        return redirect(_safe_next())

    if db.is_already_reviewed(ai_decision["id"]):
        flash("Анкета уже обработана", "warning")
        return redirect(_safe_next())

    human_decision = "APPROVE" if action == "LIKE" else "REJECT"

    from models.human_decision import AgreementStatus, HumanDecision
    from web.actions import send_reaction_sync

    agreement = AgreementStatus.from_human(HumanDecision(human_decision))
    db.save_human_decision(
        profile_id=profile_id,
        ai_decision_id=ai_decision["id"],
        decision=human_decision,
        agreement=agreement.value,
    )
    send_reaction_sync(action)
    flash(
        "❤️ Лайк отправлен Leo" if action == "LIKE" else "👎 Дизлайк отправлен Leo",
        "success",
    )
    return redirect(_safe_next())

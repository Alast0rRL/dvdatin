# App blueprint — единый экран DvAI (Stage 9 UI rewrite).
#
# Один экран `/` вместо набора вкладок: лента чата + выбор режима +
# кнопки настроек (выдвижная панель, без отдельной страницы).
#
# Что отдаёт:
#   GET  /                       — весь UI (лента, капчи, настройки в drawer)
#   GET  /chat                   — синоним / (старые закладки)
#   GET  /chat/feed              — фрагмент ленты + капч (in-place polling)
#   GET  /chat/new-count         — сигнатура состояния (polling)
#   POST /chat/captcha/<id>/answer|delete — ответ на капчу
#   POST /chat/profile/<id>/<action>     — ручное решение по REVIEW-анкете
#
# Web-слой Telegram-free: только SyncDB + config.yaml + services.

from __future__ import annotations

import json
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


def _get_db() -> SyncDB:
    return current_app.config["SYNC_DB"]


def _get_config_path() -> Path:
    return current_app.config.get("CONFIG_PATH", Path("config/config.yaml"))


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

    data = db.get_chat_feed(page=1, per_page=FEED_PER_PAGE)
    signals = db.get_chat_signals()
    pending = [_with_buttons(c) for c in db.get_pending_captchas(limit=20)]
    accounts, account_session = _get_accounts(config_path)

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
    )


@chat_bp.route("/chat/feed")
@login_required
def feed() -> tuple:
    """Фрагменты ленты и капч для in-place обновления (polling)."""
    db = _get_db()
    page = request.args.get("page", 1, type=int)
    data = db.get_chat_feed(page=page, per_page=FEED_PER_PAGE)
    return (
        json.dumps(
            {
                "feed": render_template("partials/app_feed.html", items=data.get("items", [])),
                "pending": render_template(
                    "partials/app_captchas.html",
                    pending=[_with_buttons(c) for c in db.get_pending_captchas(limit=20)],
                ),
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
    signals = _get_db().get_chat_signals() or {}
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

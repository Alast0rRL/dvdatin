# Captchas blueprint — «память капч» (Stage 7.6).
#
# Неизвестные капчи Leo бот выводит сюда: владелец отвечает, ответ
# запоминается в captcha_memory (в следующий раз бот отвечает сам) и сразу
# отправляется в чат Leo, чтобы разблокировать зависшую ленту.

from __future__ import annotations

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
from web.db import SyncDB

captchas_bp = Blueprint("captchas", __name__)


def _get_db() -> SyncDB:
    return current_app.config["SYNC_DB"]


@captchas_bp.route("/captchas", methods=["GET"])
@login_required
def index() -> str:
    db = _get_db()
    pending = db.get_pending_captchas(limit=100)
    known = db.get_known_captchas(limit=50)
    return render_template(
        "captchas.html",
        pending=[_with_buttons(c) for c in pending],
        known=[_with_buttons(c) for c in known],
    )


def _with_buttons(captcha: dict) -> dict:
    """Парсит buttons_json строку в список кнопок для шаблона."""
    import json

    raw = captcha.get("buttons_json") or "[]"
    try:
        buttons = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        buttons = []
    captcha["buttons"] = buttons if isinstance(buttons, list) else []
    return captcha


@captchas_bp.route("/captchas/<int:captcha_id>/answer", methods=["POST"])
@login_required
def answer(captcha_id: int):
    """Сохраняет выученный ответ и сразу отправляет его Leo (разблокировка)."""
    token = request.form.get("csrf_token", "")
    if token != session.get("_csrf_token"):
        flash("Ошибка безопасности", "error")
        return ("CSRF token invalid", 403)

    answer_text = (request.form.get("answer", "") or "").strip()
    if not answer_text:
        flash("Пустой ответ", "error")
        return redirect(url_for("captchas.index"))

    db = _get_db()
    captcha = db.get_captcha(captcha_id)
    if captcha is None:
        flash("Капча не найдена", "error")
        return redirect(url_for("captchas.index"))

    saved = db.set_captcha_answer(captcha_id, answer_text)
    if not saved:
        flash("Ошибка сохранения ответа", "error")
        return redirect(url_for("captchas.index"))

    # Отправляем ответ в чат Leo сразу — лента не должна ждать следующего
    # сканирования. Если движок недоступен, ответ всё равно запомнен.
    try:
        from web.actions import send_captcha_answer_sync
        status = send_captcha_answer_sync(answer_text)
    except Exception:
        from loguru import logger
        logger.exception("Captcha bridge failed")
        status = "ERROR"

    if status == "SENT":
        flash(f"Ответ «{answer_text}» отправлен Leo и запомнен", "success")
    elif status == "DISABLED":
        flash(
            f"Ответ «{answer_text}» запомнен (Leo-движок неактивен — шаблон "
            "применится при следующей такой капче)",
            "warning",
        )
    else:
        flash(
            f"Ответ «{answer_text}» запомнен, но отправка в Leo не удалась",
            "warning",
        )
    return redirect(url_for("captchas.index"))


@captchas_bp.route("/captchas/<int:captcha_id>/delete", methods=["POST"])
@login_required
def delete(captcha_id: int):
    """Удаляет запись капчи из памяти (например, чтобы не отвечать на неё)."""
    token = request.form.get("csrf_token", "")
    if token != session.get("_csrf_token"):
        flash("Ошибка безопасности", "error")
        return ("CSRF token invalid", 403)

    db = _get_db()
    db.delete_captcha(captcha_id)
    flash("Капча удалена из памяти", "success")
    return redirect(url_for("captchas.index"))
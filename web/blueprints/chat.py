# Chat blueprint — единая вкладка «Чат» (Stage 8.4).
#
# Сырой поток Leo рисуется как Telegram-чат: входящие raw_messages —
# пузыри слева (Leo/девушки), исходящие sent_messages ∪ auto_actions_log —
# пузыри справа (наши действия). Профили и капчи встроены inline прямо в
# ленту (см. partials/chat_feed.html): капча — inline-форма ответа, анкета —
# inline карточка с фото и LIKE/DISLIKE. Авто-обновление — polling
# /chat/new-count (сигнатура) → подмена /chat/feed без перезагрузки.

from __future__ import annotations

import json
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
from web.db import SyncDB

chat_bp = Blueprint("chat", __name__)


def _get_db() -> SyncDB:
    return current_app.config["SYNC_DB"]


# ── Парсинг капчи (зеркало collectors.dvinchik_collector.captcha_signature) ──

def _captcha_signature(text: str) -> str:
    """Сигнатура текста для сопоставления с captcha_memory.

    Web-слой не импортирует Telethon, поэтому лёгкая нормализация
    (нижний регистр + схлопывание пробелов) продублирована локально —
    она зеркалит ``collectors.dvinchik_collector.captcha_signature``.
    """
    import re
    return " ".join(re.sub(r"\s+", " ", (text or "").lower()).strip().split())


def _prepare_captcha(c: dict[str, Any]) -> dict[str, Any]:
    """Добавляет inline-поля капчи для шаблона (кнопки JSON → list)."""
    raw = c.get("buttons_json") or "[]"
    try:
        buttons = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        buttons = []
    c["buttons"] = buttons if isinstance(buttons, list) else []
    return c


def _chat_signature(signals: dict[str, Any] | None) -> str:
    """Подпись состояния чата (для polling: изменилась → обновить ленту)."""
    signals = signals or {}
    return "|".join([
        str(signals.get("max_raw") or ""),
        str(signals.get("max_sent") or ""),
        str(signals.get("max_auto") or ""),
        str(signals.get("max_captcha") or ""),
        str(signals.get("pending_captchas") or 0),
    ])


@chat_bp.route("/chat")
@login_required
def index() -> str:
    db = _get_db()
    data = db.get_chat_feed(page=1, per_page=50)
    signals = db.get_chat_signals()
    pending = [_prepare_captcha(c) for c in db.get_pending_captchas(limit=20)]
    return render_template(
        "chat.html",
        items=data.get("items", []),
        total=data.get("total", 0),
        page=data.get("page", 1),
        total_pages=data.get("total_pages", 1),
        signals=signals,
        signature=_chat_signature(signals),
        pending=pending,
    )


@chat_bp.route("/chat/feed")
@login_required
def feed() -> tuple:
    """Фрагмент пузырей для in-place подмены (без перезагрузки)."""
    db = _get_db()
    page = request.args.get("page", 1, type=int)
    data = db.get_chat_feed(page=page, per_page=50)
    fragment = render_template(
        "partials/chat_feed.html",
        items=data.get("items", []),
    )
    pending = [_prepare_captcha(c) for c in db.get_pending_captchas(limit=20)]
    pending_html = render_template("partials/chat_captchas_pending.html", pending=pending)
    return (
        json.dumps({
            "feed": fragment,
            "pending": pending_html,
            "total": data.get("total", 0),
            "page": data.get("page", 1),
            "total_pages": data.get("total_pages", 1),
        }),
        200,
        {"Content-Type": "application/json; charset=utf-8"},
    )


@chat_bp.route("/chat/new-count")
@login_required
def new_count() -> tuple:
    """Сигнатура для polling — изменилась → /chat/feed подтягивается."""
    db = _get_db()
    signals = db.get_chat_signals() or {}
    signature = _chat_signature(signals)
    return (
        json.dumps({
            "signature": signature,
            **signals,
        }),
        200,
        {"Content-Type": "application/json; charset=utf-8"},
    )


@chat_bp.route("/chat/captcha/<int:captcha_id>/answer", methods=["POST"])
@login_required
def captcha_answer(captcha_id: int) -> tuple:
    """Inline-ответ на капчу из чата: учит капчу + сразу отправляет Leo."""
    token = request.form.get("csrf_token", "")
    if token != session.get("_csrf_token"):
        flash("Ошибка безопасности", "error")
        return ("CSRF token invalid", 403)

    answer = (request.form.get("answer", "") or "").strip()
    if not answer:
        flash("Пустой ответ", "error")
        return redirect(url_for("chat.index"))

    db = _get_db()
    captcha = db.get_captcha(captcha_id)
    if captcha is None:
        flash("Капча не найдена", "error")
        return redirect(url_for("chat.index"))

    saved = db.set_captcha_answer(captcha_id, answer)
    if not saved:
        flash("Ошибка сохранения ответа", "error")
        return redirect(url_for("chat.index"))

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
    return redirect(url_for("chat.index"))


@chat_bp.route("/chat/captcha/<int:captcha_id>/delete", methods=["POST"])
@login_required
def captcha_delete(captcha_id: int) -> tuple:
    """«Забыть» капчу прямо из чата."""
    token = request.form.get("csrf_token", "")
    if token != session.get("_csrf_token"):
        flash("Ошибка безопасности", "error")
        return ("CSRF token invalid", 403)

    _get_db().delete_captcha(captcha_id)
    flash("Капча удалена из памяти", "success")
    return redirect(url_for("chat.index"))


@chat_bp.route("/chat/profile/<int:profile_id>/<action>", methods=["POST"])
@login_required
def profile_action(profile_id: int, action: str) -> tuple:
    """Inline LIKE/DISLIKE анкеты из чата (через DecisionService модель)."""
    if action not in ("LIKE", "DISLIKE"):
        return ("Invalid action", 400)

    db = _get_db()
    ai_decision = db.get_latest_ai_decision(profile_id)
    if not ai_decision:
        flash("AI-решение не найдено", "error")
        return redirect(url_for("chat.index"))

    if db.is_already_reviewed(ai_decision["id"]):
        flash("Анкета уже обработана", "warning")
        return redirect(url_for("chat.index"))

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
    status = send_reaction_sync(action)
    flash("❤️ LIKE отправлен" if action == "LIKE" else "👎 DISLIKE отправлен", "success")
    return redirect(url_for("chat.index"))

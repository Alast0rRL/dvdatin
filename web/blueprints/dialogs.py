# Dialogs blueprint — переписки с девушками после взаимного лайка.
#
# Только чтение: журнал (dialog_contacts / dialog_messages) пишет бот
# (collectors/dialog_logger.py). Цель — данные для анализа и будущего
# алгоритма общения.
#
#   GET /dialogs                         — список девушек (?account=<session>)
#   GET /dialogs/<session>/<user_id>     — переписка с девушкой + её анкета
#   GET /dialogs/export.json             — все переписки целиком (для анализа)

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from flask import Blueprint, Response, abort, current_app, json, render_template, request

from web.blueprints.auth import login_required
from web.db import SyncDB

dialogs_bp = Blueprint("dialogs", __name__)

#: Подписи вложений (вместо текста, если его нет).
MEDIA_LABELS: dict[str, str] = {
    "sticker": "стикер",
    "photo": "📷 фото",
    "video": "🎬 видео",
    "voice": "🎤 голосовое",
    "video_note": "⭕ кружок",
    "gif": "GIF",
    "audio": "🎵 аудио",
    "document": "📎 файл",
    "other": "вложение",
}

#: Статусы автопереписки (match_dialogs.status) по-русски.
INTRO_LABELS: dict[str, str] = {
    "WAITING_REPLY": "ждём ответа на привет",
    "SCHEDULED": "ответила — анкета запланирована",
    "INTRO_SENT": "анкета отправлена",
    "NO_PROFILE": "анкета не найдена",
    "OWNER_CHAT": "пишешь сам — анкету не слал",
    "ERROR": "ошибка автопереписки",
}

#: Шаги сценария знакомства (match_dialogs.script_step) по-русски.
SCRIPT_LABELS: dict[str, str] = {
    "after_pin": "сценарий: ждём ответа на анкету",
    "wait_more": "сценарий: ответил про анкету",
    "tell_me": "сценарий: «Рассказывайте, дамочка»",
    "handoff": "сценарий пройден — пишешь ты",
    "owner": "пишешь сам — бот замолчал",
    "stopped": "молчит — бот больше не пишет",
    "deleted": "чат удалён (не пиши / грубость)",
    "error": "ошибка сценария",
}


def intro_label(row: dict[str, Any]) -> str:
    """Подпись автопереписки: шаг сценария, если он начат, иначе статус."""
    step = row.get("script_step") or ""
    if row.get("intro_status") == "INTRO_SENT" and step in SCRIPT_LABELS:
        return SCRIPT_LABELS[step]
    return INTRO_LABELS.get(row.get("intro_status") or "", "")


def _get_db() -> SyncDB:
    return current_app.config["SYNC_DB"]


def _who(row: dict[str, Any]) -> str:
    if row.get("username"):
        return f"@{row['username']}"
    return f"id {row.get('user_id')}"


@dialogs_bp.app_template_filter("dialog_media")
def dialog_media(kind: str) -> str:
    return MEDIA_LABELS.get(kind or "", kind or "")


@dialogs_bp.route("/dialogs")
@login_required
def index() -> str:
    db = _get_db()
    account = (request.args.get("account") or "").strip() or None
    girls = db.get_dialog_list(account)
    accounts = sorted({g["account_session"] for g in db.get_dialog_list()})
    for g in girls:
        g["who"] = _who(g)
        g["intro_label"] = intro_label(g)
    return render_template(
        "dialogs.html",
        girls=girls,
        accounts=accounts,
        account=account,
        stats=db.get_dialog_stats(),
    )


@dialogs_bp.route("/dialogs/export.json")
@login_required
def export() -> Response:
    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "dialogs": _get_db().export_dialogs(),
    }
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=1),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=dialogs.json"},
    )


@dialogs_bp.route("/dialogs/<session_name>/<int:user_id>")
@login_required
def dialog(session_name: str, user_id: int) -> str:
    data = _get_db().get_dialog(session_name, user_id)
    if data is None:
        abort(404)
    data["who"] = _who(data)
    data["intro_label"] = intro_label(data)
    return render_template("dialog.html", d=data)

# Dialogs blueprint — переписки с девушками после взаимного лайка.
#
# Только чтение: журнал (dialog_contacts / dialog_messages) пишет бот
# (collectors/dialog_logger.py). Цель — данные для анализа и будущего
# алгоритма общения.
#
#   GET /dialogs                         — папки как в Telegram (Auto / Active /
#                                          50/50 / New / ЧС / без папки /
#                                          удалённые): ?folder=…, ?account=<session>
#   POST /dialogs/<session>/<id>/take    — «Забрать себе»: бот ей больше не пишет
#   GET /dialogs/<session>/<user_id>     — переписка с девушкой + её анкета
#   GET /dialogs/export.json             — все переписки целиком (для анализа)

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from flask import (
    Blueprint, Response, abort, current_app, flash, json, redirect,
    render_template, request, session, url_for,
)

from web.blueprints.auth import login_required
from app.config import ChatFoldersConfig
from services import chat_folders, dialog_outcome as outcome
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
    "OTHER_ACCOUNT": "анкета на другом аккаунте — шлёшь сам",
    "OWNER_CHAT": "пишешь сам — анкету не слал",
    "NO_ACCESS": "нет @username — бот не написал, пиши сам",
    "MAIN_CHAT": "написала тебе на основу — бот молчит",
    "NO_REPLY": "молчит и после «АЛООО» — бот больше не пишет",
    "ERROR": "ошибка автопереписки",
}

#: Шаги сценария знакомства (match_dialogs.script_step) по-русски.
SCRIPT_LABELS: dict[str, str] = {
    "after_pin": "сценарий: ждём ответа на анкету",
    "wait_more": "сценарий: ответил про анкету",
    "tell_me": "сценарий: «Рассказывайте, дамочка»",
    "wake_up": "сценарий: «АЛООО ПРОСЫПАЕМСЯ», ждём ответа",
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


#: Вкладки — папки Telegram, которые ведёт бот (порядок как у владельца),
#: плюс «Без папки» (только архив) и «Удалённые».
NO_FOLDER: str = "none"
DELETED_TAB: str = "deleted"
TAB_ORDER: tuple[str, ...] = (
    chat_folders.AUTO, chat_folders.ACTIVE, chat_folders.FIFTY,
    chat_folders.NEW, chat_folders.BLOCKED, NO_FOLDER, DELETED_TAB,
)
DEFAULT_TAB: str = chat_folders.AUTO


def tabs() -> tuple[tuple[str, str], ...]:
    """Вкладки (ключ, название); названия папок — из ``folders`` конфига."""
    cfg = ChatFoldersConfig()
    titles = {
        chat_folders.AUTO: cfg.auto,
        chat_folders.ACTIVE: cfg.active,
        chat_folders.FIFTY: cfg.fifty,
        chat_folders.NEW: cfg.new,
        chat_folders.BLOCKED: cfg.blocked,
        NO_FOLDER: "Без папки",
        DELETED_TAB: "Удалённые",
    }
    return tuple((key, titles[key]) for key in TAB_ORDER)


def folder_of(row: dict[str, Any]) -> str:
    """Вкладка чата: удалён → ЧС → папка бота (``chat_folder``) → без папки."""
    if row.get("chat_deleted_at") or (row.get("script_step") or "") == "deleted":
        return DELETED_TAB
    if row.get("blocked_at"):
        return chat_folders.BLOCKED
    folder = row.get("chat_folder") or ""
    return folder if folder in chat_folders.FOLDERS else NO_FOLDER


def _summary(girls: list[dict[str, Any]]) -> dict[str, Any]:
    """Цифры: по вкладкам, матчи → ответили → удачно."""
    counts = {key: 0 for key in TAB_ORDER}
    for g in girls:
        counts[g["folder"]] += 1
    replied = sum(1 for g in girls if g.get("her_count"))
    total = len(girls)
    good = sum(1 for g in girls if g["outcome"].category == outcome.GOOD)
    return {
        "counts": counts,
        "good": good,
        "replied": replied,
        "replied_pct": round(100 * replied / total) if total else 0,
        "good_pct": round(100 * good / total) if total else 0,
        "easy": sum(1 for g in girls if g["outcome"].ease == "easy"),
    }


@dialogs_bp.route("/dialogs")
@login_required
def index() -> str:
    db = _get_db()
    account = (request.args.get("account") or "").strip() or None
    folder = (request.args.get("folder") or "").strip()
    if folder not in TAB_ORDER:
        folder = DEFAULT_TAB

    girls = db.get_dialog_list(account)
    accounts = sorted({g["account_session"] for g in db.get_dialog_list()})
    now = datetime.now(timezone.utc)
    for g in girls:
        g["who"] = _who(g)
        g["intro_label"] = intro_label(g)
        g["outcome"] = outcome.classify(g, now)
        g["folder"] = folder_of(g)
        g["bot_leads"] = chat_folders.bot_leads(g, now=now)
    summary = _summary(girls)
    shown = [g for g in girls if g["folder"] == folder]  # уже свежие сверху
    return render_template(
        "dialogs.html",
        girls=shown,
        total=len(girls),
        accounts=accounts,
        account=account,
        folder=folder,
        tabs=tabs(),
        reason_hints=outcome.REASON_HINTS,
        summary=summary,
        stats=db.get_dialog_stats(),
    )


@dialogs_bp.route("/dialogs/<session_name>/<int:user_id>/take", methods=["POST"])
@login_required
def take(session_name: str, user_id: int) -> Response:
    """«Забрать себе»: шаг сценария ``owner`` — бот ей больше не пишет."""
    if request.form.get("csrf_token", "") != session.get("_csrf_token"):
        flash("Ошибка безопасности", "error")
        return Response("CSRF token invalid", status=403)
    if _get_db().take_dialog_to_owner(session_name, user_id):
        flash("Забрал себе: бот ей больше не пишет", "success")
    else:
        flash("Автопереписки с ней нет — забирать нечего", "warning")
    target = request.form.get("next", "")
    if not target.startswith("/") or target.startswith("//"):
        target = url_for("dialogs.index")
    return redirect(target)


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
    data["outcome"] = outcome.classify(data)
    return render_template(
        "dialog.html", d=data, reason_hints=outcome.REASON_HINTS,
    )

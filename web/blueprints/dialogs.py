# Dialogs blueprint — переписки с девушками после взаимного лайка.
#
# Только чтение: журнал (dialog_contacts / dialog_messages) пишет бот
# (collectors/dialog_logger.py). Цель — данные для анализа и будущего
# алгоритма общения.
#
#   GET /dialogs                         — список девушек: ?account=<session>,
#                                          ?cat=you|active|good|failed,
#                                          ?reason=<причина неудачи>, ?sort=…
#   GET /dialogs/<session>/<user_id>     — переписка с девушкой + её анкета
#   GET /dialogs/export.json             — все переписки целиком (для анализа)

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from flask import Blueprint, Response, abort, current_app, json, render_template, request

from web.blueprints.auth import login_required
from services import dialog_outcome as outcome
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


#: Вкладки списка: «ждёт тебя» — флаг поверх итога, остальные — итоги.
TABS: tuple[tuple[str, str], ...] = (
    ("", "Все"),
    ("you", "Ждёт тебя"),
    (outcome.ACTIVE, outcome.CATEGORY_LABELS[outcome.ACTIVE]),
    (outcome.GOOD, outcome.CATEGORY_LABELS[outcome.GOOD]),
    (outcome.FAILED, outcome.CATEGORY_LABELS[outcome.FAILED]),
)


def _in_tab(g: dict[str, Any], cat: str) -> bool:
    if not cat:
        return True
    if cat == "you":
        return g["outcome"].needs_you
    return g["outcome"].category == cat


def _summary(girls: list[dict[str, Any]]) -> dict[str, Any]:
    """Цифры над списком: по вкладкам, по причинам неудач, конверсия."""
    counts = {key: sum(_in_tab(g, key) for g in girls) for key, _ in TABS}
    reasons = {
        r: sum(1 for g in girls if g["outcome"].reason == r)
        for r in outcome.REASON_LABELS
    }
    replied = sum(1 for g in girls if g.get("her_count"))
    total = len(girls)
    good = counts[outcome.GOOD]
    return {
        "counts": counts,
        "reasons": reasons,
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
    cat = (request.args.get("cat") or "").strip()
    if cat not in {key for key, _ in TABS}:
        cat = ""
    reason = (request.args.get("reason") or "").strip()
    if cat != outcome.FAILED or reason not in outcome.REASON_LABELS:
        reason = ""
    sort = (request.args.get("sort") or "").strip()
    if sort not in outcome.SORTS:
        sort = "recent"

    girls = db.get_dialog_list(account)
    accounts = sorted({g["account_session"] for g in db.get_dialog_list()})
    now = datetime.now(timezone.utc)
    for g in girls:
        g["who"] = _who(g)
        g["intro_label"] = intro_label(g)
        g["outcome"] = outcome.classify(g, now)
    summary = _summary(girls)
    shown = [
        g for g in girls
        if _in_tab(g, cat) and (not reason or g["outcome"].reason == reason)
    ]
    return render_template(
        "dialogs.html",
        girls=outcome.sort_rows(shown, sort),
        total=len(girls),
        accounts=accounts,
        account=account,
        cat=cat,
        reason=reason,
        sort=sort,
        tabs=TABS,
        sorts=outcome.SORTS,
        reason_labels=outcome.REASON_LABELS,
        reason_hints=outcome.REASON_HINTS,
        summary=summary,
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
    data["outcome"] = outcome.classify(data)
    return render_template(
        "dialog.html", d=data, reason_hints=outcome.REASON_HINTS,
    )

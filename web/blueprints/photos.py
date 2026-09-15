# Photos blueprint — serve cached Telegram photos for profiles.

from __future__ import annotations

from flask import Blueprint, abort, send_file

from web.blueprints.auth import login_required
from web.db import SyncDB
from web.photos import download_photo_sync, get_photo_path

photos_bp = Blueprint("photos", __name__)


def _get_db() -> SyncDB:
    from flask import current_app
    return current_app.config["SYNC_DB"]


@photos_bp.route("/photos/<int:profile_id>/<int:message_id>.jpg")
@login_required
def serve_photo(profile_id: int, message_id: int) -> tuple:
    """Отдаёт фото профиля (из кэша или скачивает из Telegram).

    Сообщение скачивается ТЕМ аккаунтом, который реально его получил
    (account_session из profile_messages): message_id в диалоге с Leo у
    каждого аккаунта своя нумерация, и запрос через другой аккаунт может
    вернуть фото ДРУГОЙ анкеты.
    """
    db = _get_db()

    # Аккаунт-получатель и chat_id берём из привязки сообщение↔профиль.
    link = db.get_profile_message(profile_id, message_id)
    if not link:
        abort(404)
    chat_id = link.get("chat_id") or 0
    account_session = link.get("account_session", "") or ""

    # Check cache first (ключ включает аккаунт)
    cached = get_photo_path(profile_id, message_id, account_session)
    if cached and cached.exists():
        return send_file(str(cached), mimetype="image/jpeg")

    if not chat_id:
        abort(404)

    # Try to download
    path = download_photo_sync(
        chat_id, message_id, profile_id, account_session=account_session,
    )
    if path and path.exists():
        return send_file(str(path), mimetype="image/jpeg")

    abort(404)

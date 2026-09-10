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
    """Отдаёт фото профиля (из кэша или скачивает из Telegram)."""
    # Check cache first
    cached = get_photo_path(profile_id, message_id)
    if cached and cached.exists():
        return send_file(str(cached), mimetype="image/jpeg")

    # Get chat_id from profile
    db = _get_db()
    profile = db.get_profile_full(profile_id)
    if not profile:
        abort(404)

    chat_id = profile.get("source_chat_id", 0)
    if not chat_id:
        abort(404)

    # Try to download
    path = download_photo_sync(chat_id, message_id, profile_id)
    if path and path.exists():
        return send_file(str(path), mimetype="image/jpeg")

    abort(404)

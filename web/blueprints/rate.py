# Rate blueprint — быстрая разметка внешности (обучающие метки для фото-модели).
#
# Владелец листает фото анкет (без текста, чтобы оценка была именно по
# внешности) и оценивает КАЖДОЕ фото отдельно: «нравится / нет / пропустить»
# (с 2026-10-29; раньше — анкету целиком, те оценки остались в photo_ratings).
# Метки пишутся в photo_item_ratings и на действия бота НЕ влияют.
#
#   GET  /rate                       — страница разметки
#   GET  /rate/next                  — JSON: следующие анкеты с неоценёнными фото
#                                      (?limit=, ?exclude=1,2)
#   POST /rate/<id>/<msg_id>         — оценка фото (form: rating=LIKE|DISLIKE|SKIP|VICE)
#   POST /rate/<id>/<msg_id>/undo    — отмена оценки фото
#
# Web-слой Telegram-free: фото отдаёт существующий роут photos.serve_photo.

from __future__ import annotations

import time
from typing import Any

from flask import Blueprint, abort, current_app, jsonify, render_template, request, session, url_for

from web.blueprints.auth import login_required
from web.db import SyncDB
from web.photos import MEDIA_ROOT

rate_bp = Blueprint("rate", __name__)

#: Сколько анкет отдавать за раз (браузер держит небольшую очередь и
#: подгружает фото заранее).
RATE_BATCH_MAX = 10

#: Кэш «у каких анкет фото уже на диске» (скан media/ раз в минуту).
_CACHED_TTL_SEC = 60.0
_cached: dict[str, Any] = {"at": 0.0, "ids": set()}


def _get_db() -> SyncDB:
    return current_app.config["SYNC_DB"]


def _cached_profile_ids() -> set[int]:
    """Анкеты, у которых есть хоть одно фото в кэше media/ (не качаем из TG)."""
    now = time.monotonic()
    if now - _cached["at"] < _CACHED_TTL_SEC:
        return _cached["ids"]
    ids: set[int] = set()
    try:
        for d in MEDIA_ROOT.iterdir():
            if d.is_dir() and d.name.isdigit() and any(d.glob("*.jpg")):
                ids.add(int(d.name))
    except OSError:
        pass
    _cached.update(at=now, ids=ids)
    return ids


def _check_csrf() -> None:
    if request.form.get("csrf_token", "") != session.get("_csrf_token"):
        abort(403)


def _item_json(item: dict[str, Any]) -> dict[str, Any]:
    pid = item["profile_id"]
    return {
        "profile_id": pid,
        "uncertain": bool(item.get("uncertain")),
        # Старая анкета, оценённая целиком, — теперь по одному фото.
        "rerate": bool(item.get("rerate")),
        "score": item.get("score"),
        "photos": [
            {
                "message_id": p["message_id"],
                "src": url_for("photos.serve_photo", profile_id=pid,
                               message_id=p["message_id"]),
            }
            for p in item["photos"]
        ],
    }


@rate_bp.route("/rate")
@login_required
def index() -> str:
    return render_template("rate.html", stats=_get_db().get_photo_rating_stats())


@rate_bp.route("/rate/next")
@login_required
def next_items() -> Any:
    try:
        limit = min(max(int(request.args.get("limit", 3)), 1), RATE_BATCH_MAX)
    except ValueError:
        limit = 3
    exclude = [
        int(x) for x in (request.args.get("exclude") or "").split(",") if x.strip().isdigit()
    ]
    db = _get_db()
    items = db.get_rating_queue(limit=limit, exclude=exclude, prefer=_cached_profile_ids())
    return jsonify(items=[_item_json(i) for i in items if i["photos"]],
                   stats=db.get_photo_rating_stats())


@rate_bp.route("/rate/<int:profile_id>/<int:message_id>", methods=["POST"])
@login_required
def rate(profile_id: int, message_id: int) -> Any:
    _check_csrf()
    rating = (request.form.get("rating") or "").upper()
    if rating not in SyncDB.PHOTO_RATINGS:
        abort(400)
    db = _get_db()
    db.save_photo_item_rating(profile_id, message_id, rating)
    return jsonify(ok=True, stats=db.get_photo_rating_stats())


@rate_bp.route("/rate/<int:profile_id>/<int:message_id>/undo", methods=["POST"])
@login_required
def undo(profile_id: int, message_id: int) -> Any:
    _check_csrf()
    db = _get_db()
    db.delete_photo_item_rating(profile_id, message_id)
    return jsonify(ok=True, stats=db.get_photo_rating_stats())

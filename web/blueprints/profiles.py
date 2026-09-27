# Profiles blueprint — совместимость: страница анкеты заменена единым экраном.

from __future__ import annotations

from flask import Blueprint, redirect, url_for

from web.blueprints.auth import login_required

profiles_bp = Blueprint("profiles", __name__)


@profiles_bp.route("/profiles/<int:profile_id>")
@profiles_bp.route("/profiles/<int:profile_id>/action", methods=["GET", "POST"])
@login_required
def profile_detail(profile_id: int) -> tuple:
    """Анкета теперь показывается прямо в ленте — ведём на главный экран."""
    return redirect(url_for("chat.index"))

# Profiles blueprint — profile detail page.

from __future__ import annotations

from flask import Blueprint, abort, render_template, request, redirect, url_for, flash

from web.blueprints.auth import login_required
from web.blueprints.dashboard import _parse_reasons
from web.db import SyncDB

profiles_bp = Blueprint("profiles", __name__)


def _get_db() -> SyncDB:
    from flask import current_app
    return current_app.config["SYNC_DB"]


@profiles_bp.route("/profiles/<int:profile_id>")
@login_required
def profile_detail(profile_id: int) -> str:
    db = _get_db()
    profile = db.get_profile_full(profile_id)
    if not profile:
        abort(404)

    # Parse all reasons
    for ai_d in profile.get("ai_decisions", []):
        ai_d["parsed_reasons"] = _parse_reasons(ai_d.get("reasons", "[]"))

    for fr in profile.get("filter_results", []):
        fr["parsed_reasons"] = _parse_reasons(fr.get("reasons", "[]"))

    # Latest AI decision for display
    latest_ai = profile["ai_decisions"][0] if profile.get("ai_decisions") else None
    latest_filter = profile["filter_results"][0] if profile.get("filter_results") else None
    latest_human = profile["human_decisions"][0] if profile.get("human_decisions") else None

    # Photos
    photos = [m for m in profile.get("messages", []) if m.get("media_type") == "photo"]

    return render_template(
        "profile.html",
        profile=profile,
        latest_ai=latest_ai,
        latest_filter=latest_filter,
        latest_human=latest_human,
        photos=photos,
    )


@profiles_bp.route("/profiles/<int:profile_id>/action", methods=["POST"])
@login_required
def profile_action(profile_id: int) -> tuple:
    """Применяет действие к профилю (LIKE/DISLIKE) через существующую систему."""
    action = request.form.get("action", "")
    if action not in ("LIKE", "DISLIKE"):
        flash("Неверное действие", "error")
        return redirect(url_for("profiles.profile_detail", profile_id=profile_id))

    db = _get_db()
    ai_decision = db.get_latest_ai_decision(profile_id)
    if not ai_decision:
        flash("AI-решение не найдено", "error")
        return redirect(url_for("profiles.profile_detail", profile_id=profile_id))

    if ai_decision["decision"] != "REVIEW":
        flash("Действие доступно только для REVIEW-профилей", "error")
        return redirect(url_for("profiles.profile_detail", profile_id=profile_id))

    if db.is_already_reviewed(ai_decision["id"]):
        flash("Уже обработано", "error")
        return redirect(url_for("profiles.profile_detail", profile_id=profile_id))

    human_decision = "APPROVE" if action == "LIKE" else "REJECT"

    # Compute agreement via existing model
    from models.human_decision import AgreementStatus, HumanDecision
    agreement = AgreementStatus.from_human(HumanDecision(human_decision))

    db.save_human_decision(
        profile_id=profile_id,
        ai_decision_id=ai_decision["id"],
        decision=human_decision,
        agreement=agreement.value,
    )

    flash(f"Решение сохранено: {'LIKE ❤️' if action == 'LIKE' else 'DISLIKE 👎'}", "success")
    return redirect(url_for("profiles.profile_detail", profile_id=profile_id))

# Legacy compat blueprint — бывшая «лента анкет».
#
# Stage 9: интерфейс переписан в один экран (`/` — web/blueprints/chat.py).
# Здесь остались только обратная совместимость (старые ссылки/закладки
# ведут на главный экран) и API-эндпоинт быстрого действия, который
# использует внешняя автоматизация/тесты.

from __future__ import annotations

from flask import Blueprint, redirect, url_for

from web.blueprints.auth import login_required
from web.db import SyncDB

dashboard_bp = Blueprint("dashboard", __name__)


def _get_db() -> SyncDB:
    from flask import current_app
    return current_app.config["SYNC_DB"]


@dashboard_bp.route("/dashboard")
@login_required
def dashboard() -> tuple:
    """Старая «лента анкет» — редирект на единый экран."""
    return redirect(url_for("chat.index"))


@dashboard_bp.route("/dashboard/action/<int:profile_id>/<action>")
@login_required
def quick_action(profile_id: int, action: str) -> tuple:
    """API быстрого действия (LIKE/DISLIKE) по анкете.

    Совместимость с прежней лентой анкет: сохраняет человеческое решение
    (APPROVE/REJECT) и отправляет реакцию в чат Leo. В самом интерфейсе
    кнопки показываются только для REVIEW-анкет (см. chat.profile_action).
    """
    if action not in ("LIKE", "DISLIKE"):
        return ("Invalid action", 400)

    db = _get_db()
    ai_decision = db.get_latest_ai_decision(profile_id)
    if not ai_decision:
        return ("AI decision not found", 404)

    if db.is_already_reviewed(ai_decision["id"]):
        return ("Already reviewed", 409)

    human_decision = "APPROVE" if action == "LIKE" else "REJECT"

    from models.human_decision import AgreementStatus, HumanDecision

    agreement = AgreementStatus.from_human(HumanDecision(human_decision))
    db.save_human_decision(
        profile_id=profile_id,
        ai_decision_id=ai_decision["id"],
        decision=human_decision,
        agreement=agreement.value,
    )

    from web.actions import send_reaction_sync

    return (f"OK:{send_reaction_sync(action)}", 200)

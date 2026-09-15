# Dashboard blueprint — main feed with vertical profile cards.

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, render_template, request

from web.blueprints.auth import login_required
from web.blueprints.settings import _get_accounts
from web.db import SyncDB

dashboard_bp = Blueprint("dashboard", __name__)


def _get_db() -> SyncDB:
    from flask import current_app
    return current_app.config["SYNC_DB"]


def _get_config_path() -> Path:
    from flask import current_app
    return current_app.config.get(
        "CONFIG_PATH", Path("config/config.yaml")
    )


def _parse_reasons(reasons_raw: str | list) -> list[dict[str, str]]:
    """Парсит JSON-причины в человекочитаемый формат."""
    import json
    if isinstance(reasons_raw, str):
        try:
            reasons = json.loads(reasons_raw)
        except (json.JSONDecodeError, TypeError):
            return []
    elif isinstance(reasons_raw, list):
        reasons = reasons_raw
    else:
        return []

    _FILTER_LABELS = {
        "AGE_OUT_OF_RANGE": "Возраст не подходит",
        "CITY_OUT_OF_RANGE": "Город не в списке",
        "AGE_UNKNOWN": "Возраст неизвестен",
        "CITY_UNKNOWN": "Город неизвестен",
        "INSUFFICIENT_DATA": "Недостаточно данных",
        "AGE_OK": "Возраст подходит",
        "CITY_OK": "Город подходит",
    }
    _NEGATIVE_LABELS = {
        "not_relationships": "Не ищет отношения",
        "has_boyfriend": "Есть парень",
        "smoking": "Курит",
        "alcohol": "Пьёт",
        "bad_habits": "Вредные привычки",
        "pokatayte": "Покатайте/прокат",
        "short_hair": "Волосы короче каре",
        "instagram": "Instagram",
        "plus_size": "+size",
        "age_mismatch": "Возраст в анкете не совпадает",
    }
    _POSITIVE_LABELS = {
        "spbpu": "СПбПУ",
        "anime": "Аниме",
        "games": "Игры",
        "relocated_to_spb": "Переехала в СПб",
    }
    _DECISION_CODES = {
        "FILTER_REJECTED", "FILTER_REVIEW", "USER_SKIP", "USER_LIKE",
        "AI_UNAVAILABLE",
    }
    _REVIEW_DATA_LABELS = {
        "NO_FEATURES_FOUND": "Мало информации в анкете",
    }

    result = []
    for reason in reasons:
        if reason in _DECISION_CODES:
            continue
        if reason.startswith("USER_SKIP:"):
            tag = reason.split(":", 1)[1]
            result.append({"label": f"Ваша стоп-метка: {tag}", "type": "negative"})
        elif reason.startswith("USER_LIKE:"):
            tag = reason.split(":", 1)[1]
            result.append({"label": f"Ваша метка интереса: {tag}", "type": "positive"})
        elif reason.startswith("HARD_NEGATIVE:"):
            parts = reason.split(":", 2)
            name = parts[1] if len(parts) > 1 else ""
            evidence = parts[2] if len(parts) > 2 else ""
            label = _NEGATIVE_LABELS.get(name, name)
            result.append({"label": f"{label}: {evidence}" if evidence else label, "type": "negative"})
        elif reason.startswith("POSITIVE:"):
            parts = reason.split(":", 2)
            name = parts[1] if len(parts) > 1 else ""
            evidence = parts[2] if len(parts) > 2 else ""
            label = _POSITIVE_LABELS.get(name, name)
            result.append({"label": f"{label}: {evidence}" if evidence else label, "type": "positive"})
        elif reason in _REVIEW_DATA_LABELS:
            result.append({"label": _REVIEW_DATA_LABELS[reason], "type": "neutral"})
        elif reason in _FILTER_LABELS:
            result.append({"label": _FILTER_LABELS[reason], "type": "negative" if "OUT_OF" in reason or "UNKNOWN" in reason or "INSUFFICIENT" in reason else "positive"})
        elif reason == "INFORMATIVE_CLEAN":
            result.append({"label": "Анкета информативная", "type": "positive"})
        else:
            result.append({"label": reason, "type": "neutral"})
    return result


def _format_profile_for_template(profile: dict) -> dict:
    """Подготавливает профиль для шаблона."""
    # Parse reasons
    ai_reasons_raw = profile.get("reasons", "[]")
    profile["parsed_reasons"] = _parse_reasons(ai_reasons_raw)

    # Parse filter reasons
    fr_raw = profile.get("filter_reasons", "[]")
    profile["parsed_filter_reasons"] = _parse_reasons(fr_raw)

    # Photo info
    db = _get_db()
    messages = db.get_profile_messages(profile["id"])
    profile["photos"] = [
        m for m in messages if m.get("media_type") == "photo"
    ]
    return profile


@dashboard_bp.route("/")
@login_required
def index() -> str:
    return dashboard_bpEndpoint("")


@dashboard_bp.route("/dashboard")
@login_required
def dashboard() -> str:  # type: ignore[no-untyped-def]
    return dashboard_bpEndpoint("")


def _load_dashboard_data(db: SyncDB, page: int, decision_filter: str | None) -> dict:
    """Общий загрузчик данных ленты (страница + статистика).

    Используется и для полной страницы, и для in-place обновления
    (эндпоинт /dashboard/feed), чтобы не дублировать SQL/форматирование.
    """
    per_page = 20
    offset = (page - 1) * per_page
    profiles = db.get_profiles_paginated(offset, per_page, decision_filter)
    total = db.get_profiles_count(decision_filter)
    total_pages = (total + per_page - 1) // per_page

    return {
        "profiles": [_format_profile_for_template(p) for p in profiles],
        "total": total,
        "total_pages": total_pages,
        "decisions": db.count_decisions(),
        "human_decisions": db.count_human_decisions(),
        "pending": db.get_pending_review_count(),
    }


@dashboard_bp.route("/dashboard/new-count")
@login_required
def new_count() -> tuple:
    """Лёгкие сигналы для авто-обновления ленты.

    Frontend опрашивает эндпоинт каждые ~8с; если сигнатура изменилась —
    через /dashboard/feed данные подставляются в DOM (без перезагрузки).
    Сигнатура ловит и новые анкеты, и повторы известных (меняется
    MAX(last_seen_at)), и любую новую активность (MAX(raw_messages.id)).
    """
    import json
    db = _get_db()
    decision_filter = request.args.get("decision", None) or None
    total = db.get_profiles_count(decision_filter)
    pending = db.get_pending_review_count()
    decisions = db.count_decisions()
    signals = db.get_feed_signals()

    signature = "|".join([
        str(total), str(pending),
        str(decisions.get("LIKE", 0)), str(decisions.get("REVIEW", 0)),
        str(decisions.get("DISLIKE", 0)),
        str(signals.get("max_profile_id") or ""),
        str(signals.get("max_last_seen") or ""),
        str(signals.get("max_raw_id") or ""),
    ])

    return (json.dumps({
        "total": total,
        "pending": pending,
        "decisions": decisions,
        "signature": signature,
    }), 200, {"Content-Type": "application/json"})


@dashboard_bp.route("/dashboard/feed")
@login_required
def feed() -> tuple:
    """Возвращает HTML-фрагменты текущей страницы ленты.

    {feed: <карточки>, pagination: <пагинация>} — для in-place замены DOM.
    """
    import json
    db = _get_db()
    page = request.args.get("page", 1, type=int)
    decision_filter = request.args.get("decision", None) or None

    data = _load_dashboard_data(db, page, decision_filter)
    feed_html = render_template(
        "partials/profile_cards.html", profiles=data["profiles"]
    )
    pagination_html = render_template(
        "partials/pagination.html",
        page=page, total_pages=data["total_pages"],
        decision_filter=decision_filter,
    )
    return (json.dumps({
        "feed": feed_html,
        "pagination": pagination_html,
    }), 200, {"Content-Type": "application/json; charset=utf-8"})


def dashboard_bpEndpoint(endpoint: str) -> str:  # noqa: N802
    db = _get_db()
    page = request.args.get("page", 1, type=int)
    decision_filter = request.args.get("decision", None) or None

    data = _load_dashboard_data(db, page, decision_filter)

    # Текущий режим и аккаунт-исполнитель (переключатели на ленте)
    mode = db.get_mode(_get_config_path())
    accounts, account_session = _get_accounts(_get_config_path())

    return render_template(
        "dashboard.html",
        page=page,
        decision_filter=decision_filter,
        mode=mode,
        accounts=accounts,
        account_session=account_session,
        **data,
    )


@dashboard_bp.route("/dashboard/action/<int:profile_id>/<action>")
@login_required
def quick_action(profile_id: int, action: str) -> tuple:
    """Быстрое действие для анкеты (LIKE/DISLIKE) прямо из ленты.

    Доступно для любой анкеты в истории, не только REVIEW: записывает
    ручное решение владельца (APPROVE/REJECT) по последней AI-оценке.
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

    # Compute agreement via existing model
    from models.human_decision import AgreementStatus, HumanDecision
    agreement = AgreementStatus.from_human(HumanDecision(human_decision))

    db.save_human_decision(
        profile_id=profile_id,
        ai_decision_id=ai_decision["id"],
        decision=human_decision,
        agreement=agreement.value,
    )

    # Stage 7: реально отправляем реакцию в чат Leo, чтобы лента
    # продолжилась (пользователь решил по анкете). Если авто-движок
    # выключен/недоступен — решение уже записано в БД, возвращаем 200.
    from web.actions import send_reaction_sync
    status = send_reaction_sync(action)

    return (f"OK:{status}", 200)

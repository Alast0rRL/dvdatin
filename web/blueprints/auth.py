# Auth blueprint — login / logout / session management.

from __future__ import annotations

from functools import wraps

from flask import Blueprint, redirect, render_template, request, session, url_for, flash

auth_bp = Blueprint("auth", __name__)


def login_required(f):  # type: ignore[no-untyped-def]
    """Decorator — все страницы кроме /login требуют авторизации."""
    @wraps(f)
    def decorated(*args, **kwargs):  # type: ignore[no-untyped-def]
        if not session.get("authenticated"):
            return redirect(url_for("auth.login", next=request.url))
        return f(*args, **kwargs)
    return decorated


@auth_bp.route("/login", methods=["GET", "POST"])
def login() -> str | tuple:
    if session.get("authenticated"):
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":
        from flask import current_app
        cfg = current_app.config["WEB_CONFIG"]

        # CSRF check
        token = request.form.get("csrf_token", "")
        if token != session.get("_csrf_token"):
            flash("Ошибка безопасности. Попробуйте ещё раз.", "error")
            return render_template("login.html"), 403

        password = request.form.get("password", "")
        if cfg.check_password(password):
            session["authenticated"] = True
            session.permanent = True
            next_url = request.args.get("next", url_for("dashboard.index"))
            return redirect(next_url)
        else:
            flash("Неверный пароль", "error")

    return render_template("login.html")


@auth_bp.route("/logout", methods=["POST"])
def logout() -> tuple:
    token = request.form.get("csrf_token", "")
    if token != session.get("_csrf_token"):
        flash("Ошибка безопасности.", "error")
        return render_template("login.html"), 403
    session.clear()
    flash("Вы вышли из системы", "info")
    return redirect(url_for("auth.login"))

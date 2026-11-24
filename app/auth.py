"""Authentication routes (login and logout) powered by Flask-Login."""

from urllib.parse import urlsplit

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app.csrf import CSRF_SESSION_KEY, require_csrf_token, rotate_csrf_token
from app.extensions import db
from app.models import User

auth_bp = Blueprint("auth", __name__)

INVALID_CREDENTIALS_MESSAGE = "Invalid username or password."


def _is_safe_next_url(target: str | None) -> bool:
    """Allow only relative internal URLs to prevent open redirects."""
    if not target:
        return False
    parsed = urlsplit(target)
    return (
        parsed.scheme == ""
        and parsed.netloc == ""
        and target.startswith("/")
        and not target.startswith("//")
        and not target.startswith("/\\")
    )


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    """Render the login form and authenticate existing users."""
    if current_user.is_authenticated:
        return redirect(url_for("home"))

    next_page = request.args.get("next") or request.form.get("next") or ""

    if request.method == "POST":
        require_csrf_token()

        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        user = (
            db.session.execute(db.select(User).filter_by(username=username)).scalar_one_or_none()
            if username
            else None
        )

        if user is None or not user.is_active or not user.check_password(password):
            flash(INVALID_CREDENTIALS_MESSAGE, "danger")
            return render_template(
                "auth/login.html",
                username=username,
                next_page=next_page,
            )

        login_user(user)
        rotate_csrf_token()

        if _is_safe_next_url(next_page):
            return redirect(next_page)
        return redirect(url_for("home"))

    return render_template("auth/login.html", username="", next_page=next_page)


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    """End the current user's session via a CSRF-protected POST request."""
    require_csrf_token()
    logout_user()
    session.pop(CSRF_SESSION_KEY, None)
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))

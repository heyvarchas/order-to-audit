"""Simple session-based CSRF protection helpers (no external dependency)."""

import hmac
import secrets

from flask import abort, request, session

CSRF_SESSION_KEY = "_csrf_token"


def generate_csrf_token() -> str:
    """Return the current session's CSRF token, creating one if needed."""
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def rotate_csrf_token() -> str:
    """Replace the session's CSRF token with a newly generated token."""
    token = secrets.token_urlsafe(32)
    session[CSRF_SESSION_KEY] = token
    return token


def validate_csrf_token(submitted_token: str | None) -> bool:
    """Compare the submitted token against the session token in constant time."""
    expected_token = session.get(CSRF_SESSION_KEY)
    if not expected_token or not submitted_token:
        return False
    return hmac.compare_digest(expected_token, submitted_token)


def require_csrf_token() -> None:
    """Abort with HTTP 400 if the POST form does not carry a valid CSRF token."""
    submitted_token = request.form.get("csrf_token")
    if not validate_csrf_token(submitted_token):
        abort(400, description="Invalid or missing CSRF token.")

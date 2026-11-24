"""Authentication foundation tests: password hashing, login, logout, and access control."""

import pytest

from app.csrf import CSRF_SESSION_KEY
from app.extensions import load_user
from app.models import User


def _create_user(
    db_session,
    *,
    username="alice",
    full_name="Alice Admin",
    password="correct-horse-battery-staple",
    role="admin",
    is_active=True,
):
    user = User(
        username=username,
        full_name=full_name,
        email=f"{username}@example.com",
        role=role,
        is_active=is_active,
    )
    user.set_password(password)
    db_session.add(user)
    db_session.commit()
    return user


def _get_session_csrf_token(client):
    with client.session_transaction() as sess:
        return sess.get(CSRF_SESSION_KEY)


def _login(client, username, password, *, next_url=None):
    login_url = f"/login?next={next_url}" if next_url else "/login"
    client.get(login_url)
    csrf_token = _get_session_csrf_token(client)
    return client.post(
        login_url,
        data={
            "username": username,
            "password": password,
            "csrf_token": csrf_token,
        },
    )


# ---------------------------------------------------------------------------
# 1. Password Hashing & Verification
# ---------------------------------------------------------------------------


def test_password_hashing_stores_salted_hash_and_verifies_correctly():
    user_a = User(username="user_a", full_name="User A", role="salesman")
    user_b = User(username="user_b", full_name="User B", role="salesman")

    user_a.set_password("MySecret123!")
    user_b.set_password("MySecret123!")

    assert user_a.password_hash != "MySecret123!"
    assert "MySecret123!" not in user_a.password_hash
    # Werkzeug uses a unique random salt per hash.
    assert user_a.password_hash != user_b.password_hash

    assert user_a.check_password("MySecret123!") is True
    assert user_a.check_password("WrongPassword") is False
    assert user_a.check_password("") is False


def test_set_password_rejects_empty_password():
    user = User(username="empty_pw", full_name="Empty PW", role="admin")
    with pytest.raises(ValueError, match="Password must not be empty"):
        user.set_password("")


def test_user_loader_handles_invalid_and_missing_ids(app, db_session):
    with app.app_context():
        assert load_user("not-an-integer") is None
        assert load_user(None) is None
        assert load_user("999999") is None


# ---------------------------------------------------------------------------
# 2. Unauthenticated Access & Login Page
# ---------------------------------------------------------------------------


def test_unauthenticated_access_to_home_redirects_to_login(client):
    response = client.get("/")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login?next=%2F")


def test_login_page_renders_form_without_public_registration(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert b"Sign In" in response.data
    assert b'name="username"' in response.data
    assert b'name="password"' in response.data
    assert b'name="csrf_token"' in response.data
    assert b"/register" not in response.data
    assert b"/signup" not in response.data


# ---------------------------------------------------------------------------
# 3. Valid Login & Safe Redirect Handling
# ---------------------------------------------------------------------------


def test_valid_login_authenticates_user_and_grants_access_to_home(client, db_session):
    _create_user(db_session, username="owner1", full_name="Factory Owner", password="secret-password", role="owner")

    response = _login(client, "owner1", "secret-password")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")

    home_response = client.get("/")
    assert home_response.status_code == 200
    assert b"Order-to-Audit" in home_response.data
    assert b"Factory Owner" in home_response.data
    assert b'action="/logout"' in home_response.data

    # Authenticated users visiting /login are redirected to home.
    login_again = client.get("/login")
    assert login_again.status_code == 302
    assert login_again.headers["Location"].endswith("/")


def test_valid_login_honors_safe_next_url_and_blocks_external_redirect(client, db_session):
    _create_user(db_session, username="admin1", password="secret-password")

    # Safe internal relative path is preserved.
    safe_response = _login(client, "admin1", "secret-password", next_url="/?welcome=1")
    assert safe_response.status_code == 302
    assert safe_response.headers["Location"].endswith("/?welcome=1")

    # Log out before testing external next URL rejection.
    csrf_token = _get_session_csrf_token(client)
    client.post("/logout", data={"csrf_token": csrf_token})

    # External URLs and protocol-relative URLs must fall back to home ("/").
    for unsafe_next in ("https://evil.example/steal", "//evil.example/steal"):
        unsafe_response = _login(client, "admin1", "secret-password", next_url=unsafe_next)
        assert unsafe_response.status_code == 302
        assert unsafe_response.headers["Location"].endswith("/")

        csrf_token = _get_session_csrf_token(client)
        client.post("/logout", data={"csrf_token": csrf_token})


# ---------------------------------------------------------------------------
# 4. Invalid Login Attempts & CSRF Protection on Login
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "username,password",
    [
        ("alice", "wrong-password"),
        ("nonexistent", "correct-horse-battery-staple"),
        ("", "correct-horse-battery-staple"),
        ("alice", ""),
    ],
)
def test_invalid_login_shows_generic_error_and_does_not_authenticate(
    client, db_session, username, password
):
    _create_user(db_session, username="alice", password="correct-horse-battery-staple")

    response = _login(client, username, password)
    assert response.status_code == 200
    assert b"Invalid username or password." in response.data

    # Confirm the user is still unauthenticated.
    home_response = client.get("/")
    assert home_response.status_code == 302
    assert "/login" in home_response.headers["Location"]


def test_inactive_user_cannot_log_in_and_receives_generic_error(client, db_session):
    _create_user(
        db_session,
        username="disabled_user",
        password="valid-password",
        is_active=False,
    )

    response = _login(client, "disabled_user", "valid-password")
    assert response.status_code == 200
    assert b"Invalid username or password." in response.data

    home_response = client.get("/")
    assert home_response.status_code == 302
    assert "/login" in home_response.headers["Location"]


def test_deactivated_user_loses_access_on_existing_session(app, client, db_session):
    user = _create_user(
        db_session,
        username="active_then_disabled",
        password="valid-password",
        is_active=True,
    )

    login_response = _login(client, "active_then_disabled", "valid-password")
    assert login_response.status_code == 302
    assert client.get("/").status_code == 200

    # Deactivate the user while their session cookie is still active.
    user.is_active = False
    db_session.commit()

    with app.app_context():
        assert load_user(str(user.id)) is None

    after_deactivation = client.get("/")
    assert after_deactivation.status_code == 302
    assert after_deactivation.headers["Location"].endswith("/login?next=%2F")


def test_login_post_rejects_missing_or_invalid_csrf_token(client, db_session):
    _create_user(db_session, username="alice", password="secret-password")

    client.get("/login")
    missing_csrf = client.post(
        "/login",
        data={"username": "alice", "password": "secret-password"},
    )
    assert missing_csrf.status_code == 400

    wrong_csrf = client.post(
        "/login",
        data={
            "username": "alice",
            "password": "secret-password",
            "csrf_token": "not-the-session-token",
        },
    )
    assert wrong_csrf.status_code == 400


# ---------------------------------------------------------------------------
# 5. Logout (POST-only & CSRF-Protected)
# ---------------------------------------------------------------------------


def test_logout_requires_post_method(client, db_session):
    _create_user(db_session, username="alice", password="secret-password")
    _login(client, "alice", "secret-password")

    response = client.get("/logout")
    assert response.status_code == 405

    # User must remain logged in after rejected GET /logout.
    assert client.get("/").status_code == 200


def test_logout_rejects_missing_or_invalid_csrf_token(client, db_session):
    _create_user(db_session, username="alice", password="secret-password")
    _login(client, "alice", "secret-password")

    missing_csrf = client.post("/logout")
    assert missing_csrf.status_code == 400
    assert client.get("/").status_code == 200

    bad_csrf = client.post("/logout", data={"csrf_token": "invalid-token"})
    assert bad_csrf.status_code == 400
    assert client.get("/").status_code == 200


def test_logout_with_valid_csrf_token_clears_session_and_redirects_to_login(
    client, db_session
):
    _create_user(db_session, username="alice", password="secret-password")
    _login(client, "alice", "secret-password")
    assert client.get("/").status_code == 200

    csrf_token = _get_session_csrf_token(client)
    logout_response = client.post("/logout", data={"csrf_token": csrf_token})
    assert logout_response.status_code == 302
    assert logout_response.headers["Location"].endswith("/login")

    # Protected route now redirects back to login.
    after_logout = client.get("/")
    assert after_logout.status_code == 302
    assert "/login" in after_logout.headers["Location"]


def test_unauthenticated_logout_post_redirects_to_login(client):
    response = client.post("/logout")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]

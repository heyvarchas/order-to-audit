from app.models import User


def test_app_creates_in_testing_mode(app):
    assert app is not None
    assert app.config["TESTING"] is True
    assert "sqlalchemy" in app.extensions
    assert hasattr(app, "login_manager")


def test_home_page_redirects_unauthenticated_users_to_login(client):
    response = client.get("/")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_home_page_responds_successfully_when_authenticated(client, db_session):
    user = User(
        username="smoke_user",
        full_name="Smoke Test User",
        role="admin",
    )
    user.set_password("smoke-password")
    db_session.add(user)
    db_session.commit()

    # Fetch login page to establish a CSRF token, then log in.
    client.get("/login")
    with client.session_transaction() as sess:
        csrf_token = sess["_csrf_token"]

    login_response = client.post(
        "/login",
        data={
            "username": "smoke_user",
            "password": "smoke-password",
            "csrf_token": csrf_token,
        },
    )
    assert login_response.status_code == 302

    response = client.get("/")
    assert response.status_code == 200
    assert b"Order-to-Audit" in response.data
    assert b"The Flask application skeleton is running." in response.data

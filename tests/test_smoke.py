def test_app_creates_in_testing_mode(app):
    assert app is not None
    assert app.config["TESTING"] is True
    assert "sqlalchemy" in app.extensions
    assert hasattr(app, "login_manager")


def test_home_page_responds_successfully(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"Order-to-Audit" in response.data
    assert b"The Flask application skeleton is running." in response.data

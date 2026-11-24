import pytest

from app import create_app
from app.db_init import init_db
from app.extensions import db
from config import TestingConfig


@pytest.fixture
def app():
    app = create_app(TestingConfig)
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db_session(app):
    """Provide an initialized isolated in-memory database session per test."""
    with app.app_context():
        init_db()
        yield db.session
        db.session.rollback()
        db.session.remove()
        db.drop_all()

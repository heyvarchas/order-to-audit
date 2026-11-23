import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"


class Config:
    """Base configuration shared across environments."""

    # Set SECRET_KEY in your environment for any non-local deployment.
    # The fallback value below is strictly for local development convenience.
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
    SQLALCHEMY_TRACK_MODIFICATIONS = False


class DevelopmentConfig(Config):
    """Local development configuration using a SQLite database in instance/."""

    DEBUG = True
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        f"sqlite:///{INSTANCE_DIR / 'order_to_audit.db'}",
    )


class TestingConfig(Config):
    """Automated test configuration using an isolated in-memory SQLite database."""

    TESTING = True
    SECRET_KEY = "testing-secret-key"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"

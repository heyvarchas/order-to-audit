import os

from flask import Flask, render_template

from app.db_init import register_cli_commands, register_sqlite_foreign_keys
from app.extensions import db, login_manager
from config import DevelopmentConfig


def create_app(config_class=DevelopmentConfig):
    """Application factory for the Order-to-Audit Flask app."""
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_class)

    # Ensure Flask's instance directory exists for the local SQLite database.
    os.makedirs(app.instance_path, exist_ok=True)

    # Enforce SQLite foreign-key constraints on every connection.
    register_sqlite_foreign_keys()

    # Initialize Flask extensions.
    db.init_app(app)
    login_manager.init_app(app)

    # Register explicit CLI commands without initializing the DB on startup.
    register_cli_commands(app)

    @app.route("/")
    def home():
        return render_template("home.html")

    return app

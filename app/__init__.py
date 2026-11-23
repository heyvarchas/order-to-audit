import os

from flask import Flask, render_template

from app.extensions import db, login_manager
from config import DevelopmentConfig


def create_app(config_class=DevelopmentConfig):
    """Application factory for the Order-to-Audit Flask app."""
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_class)

    # Ensure Flask's instance directory exists for the local SQLite database.
    os.makedirs(app.instance_path, exist_ok=True)

    # Initialize Flask extensions.
    db.init_app(app)
    login_manager.init_app(app)

    @app.route("/")
    def home():
        return render_template("home.html")

    return app

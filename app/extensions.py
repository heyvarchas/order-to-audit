from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
login_manager = LoginManager()


@login_manager.user_loader
def _load_user(_user_id):
    # Placeholder required by Flask-Login before the User model is implemented.
    return None

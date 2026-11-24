from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Please log in to access this page."
login_manager.login_message_category = "warning"


@login_manager.user_loader
def load_user(user_id):
    """Reload the active user object from the user ID stored in the session."""
    from app.models import User

    try:
        parsed_id = int(user_id)
    except (TypeError, ValueError):
        return None
    user = db.session.get(User, parsed_id)
    if user is None or not user.is_active:
        return None
    return user

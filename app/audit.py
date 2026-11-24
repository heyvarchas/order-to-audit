"""Minimal reusable audit logging helper backed by the immutable `audit_logs` table."""

import json
from typing import Any

from app.extensions import db
from app.models import AuditLog


def _serialize_values(values: dict[str, Any] | None) -> str | None:
    if values is None:
        return None
    return json.dumps(values, sort_keys=True, default=str)


def log_audit(
    *,
    user_id: int,
    action: str,
    entity_type: str,
    entity_id: int,
    summary: str,
    old_values: dict[str, Any] | None = None,
    new_values: dict[str, Any] | None = None,
) -> AuditLog:
    """Append an AuditLog entry to the current SQLAlchemy transaction.

    The caller is responsible for committing (or rolling back) the enclosing transaction
    so that the business change and its audit record succeed or fail atomically.
    """
    entry = AuditLog(
        user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        summary=summary,
        old_values_json=_serialize_values(old_values),
        new_values_json=_serialize_values(new_values),
    )
    db.session.add(entry)
    return entry

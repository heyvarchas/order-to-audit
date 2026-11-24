"""Role-Based Access Control (RBAC) roles, capability matrix, and route decorator."""

from functools import wraps

from flask import abort
from flask_login import current_user, login_required

# Canonical application roles (reused by User.role CheckConstraint in app/models.py).
ROLE_SALESMAN = "salesman"
ROLE_ADMIN = "admin"
ROLE_PRODUCTION_SUPERVISOR = "production_supervisor"
ROLE_ACCOUNTANT = "accountant"
ROLE_OWNER = "owner"

VALID_ROLES: tuple[str, ...] = (
    ROLE_SALESMAN,
    ROLE_ADMIN,
    ROLE_PRODUCTION_SUPERVISOR,
    ROLE_ACCOUNTANT,
    ROLE_OWNER,
)
_VALID_ROLES_SET: frozenset[str] = frozenset(VALID_ROLES)

# Centralized application capability identifiers.
CAP_SUBMIT_SALES_ORDERS = "submit_sales_orders"
CAP_VIEW_OWN_SALES_ORDERS = "view_own_sales_orders"
CAP_VERIFY_APPROVE_ORDERS = "verify_approve_orders"
CAP_MANAGE_PRODUCTS_CUSTOMERS = "manage_products_customers"
CAP_RECORD_PURCHASES = "record_purchases"
CAP_RECORD_PRODUCTION_RUNS = "record_production_runs"
CAP_VIEW_INVENTORY = "view_inventory"
CAP_MANAGE_INVOICES_DISPATCH = "manage_invoices_dispatch"
CAP_RECORD_PAYMENTS = "record_payments"
CAP_VIEW_FINANCIAL_REPORTS = "view_financial_reports"
CAP_VIEW_OWNER_DASHBOARD = "view_owner_dashboard"
CAP_MANAGE_USERS = "manage_users"

ALL_CAPABILITIES: tuple[str, ...] = (
    CAP_SUBMIT_SALES_ORDERS,
    CAP_VIEW_OWN_SALES_ORDERS,
    CAP_VERIFY_APPROVE_ORDERS,
    CAP_MANAGE_PRODUCTS_CUSTOMERS,
    CAP_RECORD_PURCHASES,
    CAP_RECORD_PRODUCTION_RUNS,
    CAP_VIEW_INVENTORY,
    CAP_MANAGE_INVOICES_DISPATCH,
    CAP_RECORD_PAYMENTS,
    CAP_VIEW_FINANCIAL_REPORTS,
    CAP_VIEW_OWNER_DASHBOARD,
    CAP_MANAGE_USERS,
)

# Role-to-capability permission matrix.
# Note: Salesman access to CAP_VIEW_INVENTORY is intended to be read-only.
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    ROLE_SALESMAN: frozenset(
        {
            CAP_SUBMIT_SALES_ORDERS,
            CAP_VIEW_OWN_SALES_ORDERS,
            CAP_VIEW_INVENTORY,
        }
    ),
    ROLE_ADMIN: frozenset(
        {
            CAP_SUBMIT_SALES_ORDERS,
            CAP_VIEW_OWN_SALES_ORDERS,
            CAP_VERIFY_APPROVE_ORDERS,
            CAP_MANAGE_PRODUCTS_CUSTOMERS,
            CAP_RECORD_PURCHASES,
            CAP_RECORD_PRODUCTION_RUNS,
            CAP_VIEW_INVENTORY,
            CAP_MANAGE_INVOICES_DISPATCH,
            CAP_RECORD_PAYMENTS,
            CAP_VIEW_FINANCIAL_REPORTS,
            CAP_MANAGE_USERS,
        }
    ),
    ROLE_PRODUCTION_SUPERVISOR: frozenset(
        {
            CAP_RECORD_PRODUCTION_RUNS,
            CAP_VIEW_INVENTORY,
        }
    ),
    ROLE_ACCOUNTANT: frozenset(
        {
            CAP_VIEW_OWN_SALES_ORDERS,
            CAP_VIEW_INVENTORY,
            CAP_VIEW_FINANCIAL_REPORTS,
        }
    ),
    ROLE_OWNER: frozenset(ALL_CAPABILITIES),
}


def is_valid_role(role: object) -> bool:
    """Return True only if ``role`` is one of the five defined role strings."""
    return isinstance(role, str) and role in _VALID_ROLES_SET


def role_has_permission(role: object, capability: str) -> bool:
    """Return True if a valid role is granted the given capability."""
    if not is_valid_role(role):
        return False
    return capability in ROLE_PERMISSIONS[role]


def roles_with_permission(*capabilities: str) -> tuple[str, ...]:
    """Return all valid roles granted at least one of the given capabilities."""
    return tuple(
        role
        for role in VALID_ROLES
        if any(capability in ROLE_PERMISSIONS[role] for capability in capabilities)
    )


def user_has_permission(user, *capabilities: str) -> bool:
    """Return True if an authenticated user holds at least one given capability."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    user_role = getattr(user, "role", None)
    return any(role_has_permission(user_role, cap) for cap in capabilities)


def roles_required(*roles: str):
    """Restrict a route to authenticated users whose role is in ``roles``.

    Unauthenticated users are redirected to the login page by Flask-Login.
    Authenticated users with an unauthorized, invalid, or unknown role receive HTTP 403.
    """
    if not roles:
        raise ValueError("roles_required() requires at least one valid role.")

    invalid_roles = [role for role in roles if not is_valid_role(role)]
    if invalid_roles:
        raise ValueError(
            f"Unknown role(s) passed to roles_required(): {', '.join(map(str, invalid_roles))}"
        )

    allowed_roles = frozenset(roles)

    def decorator(view_func):
        @wraps(view_func)
        @login_required
        def wrapped_view(*args, **kwargs):
            user_role = getattr(current_user, "role", None)
            if not is_valid_role(user_role) or user_role not in allowed_roles:
                abort(403)
            return view_func(*args, **kwargs)

        return wrapped_view

    return decorator

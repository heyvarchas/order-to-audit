"""Role-Based Access Control (RBAC) authorization and permission matrix tests."""

import pytest
from flask import Blueprint, g
from sqlalchemy.exc import IntegrityError

from app.csrf import CSRF_SESSION_KEY
from app.models import User
from app.permissions import (
    ALL_CAPABILITIES,
    CAP_MANAGE_INVOICES_DISPATCH,
    CAP_MANAGE_PRODUCTS_CUSTOMERS,
    CAP_MANAGE_STOCK_ADJUSTMENTS,
    CAP_MANAGE_USERS,
    CAP_RECORD_PAYMENTS,
    CAP_RECORD_PRODUCTION_RUNS,
    CAP_RECORD_PURCHASES,
    CAP_SUBMIT_SALES_ORDERS,
    CAP_VERIFY_APPROVE_ORDERS,
    CAP_VIEW_FINANCIAL_REPORTS,
    CAP_VIEW_INVENTORY,
    CAP_VIEW_OWN_SALES_ORDERS,
    CAP_VIEW_OWNER_DASHBOARD,
    ROLE_ACCOUNTANT,
    ROLE_ADMIN,
    ROLE_OWNER,
    ROLE_PERMISSIONS,
    ROLE_PRODUCTION_SUPERVISOR,
    ROLE_SALESMAN,
    VALID_ROLES,
    is_valid_role,
    role_has_permission,
    roles_required,
)


def _create_user(db_session, *, username: str, role: str, password: str = "test-password"):
    user = User(
        username=username,
        full_name=f"User {username}",
        email=f"{username}@example.com",
        role=role,
        is_active=True,
    )
    user.set_password(password)
    db_session.add(user)
    db_session.commit()
    return user


def _login(client, username: str, password: str = "test-password"):
    client.get("/login")
    with client.session_transaction() as sess:
        csrf_token = sess.get(CSRF_SESSION_KEY)
    response = client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            "csrf_token": csrf_token,
        },
    )
    assert response.status_code == 302
    return response


@pytest.fixture
def rbac_client(app, db_session):
    """Register a minimal test-only blueprint on the app fixture for RBAC checks."""
    rbac_test_bp = Blueprint("rbac_test", __name__, url_prefix="/_rbac_test")

    @rbac_test_bp.route("/salesman-only")
    @roles_required(ROLE_SALESMAN)
    def salesman_only():
        return "salesman ok", 200

    @rbac_test_bp.route("/admin-only")
    @roles_required(ROLE_ADMIN)
    def admin_only():
        return "admin ok", 200

    @rbac_test_bp.route("/production-supervisor-only")
    @roles_required(ROLE_PRODUCTION_SUPERVISOR)
    def production_supervisor_only():
        return "production_supervisor ok", 200

    @rbac_test_bp.route("/accountant-only")
    @roles_required(ROLE_ACCOUNTANT)
    def accountant_only():
        return "accountant ok", 200

    @rbac_test_bp.route("/owner-only")
    @roles_required(ROLE_OWNER)
    def owner_only():
        return "owner ok", 200

    @rbac_test_bp.route("/admin-or-owner")
    @roles_required(ROLE_ADMIN, ROLE_OWNER)
    def admin_or_owner():
        return "admin or owner ok", 200

    @rbac_test_bp.route("/any-valid-role")
    @roles_required(*VALID_ROLES)
    def any_valid_role():
        return "any valid role ok", 200

    app.register_blueprint(rbac_test_bp)
    return app.test_client()


ROLE_ENDPOINTS = {
    ROLE_SALESMAN: "/_rbac_test/salesman-only",
    ROLE_ADMIN: "/_rbac_test/admin-only",
    ROLE_PRODUCTION_SUPERVISOR: "/_rbac_test/production-supervisor-only",
    ROLE_ACCOUNTANT: "/_rbac_test/accountant-only",
    ROLE_OWNER: "/_rbac_test/owner-only",
}


# ---------------------------------------------------------------------------
# 1. Production App Hygiene
# ---------------------------------------------------------------------------


def test_production_app_has_no_rbac_test_routes(app):
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert not any(rule.startswith("/_rbac_test") for rule in rules)


# ---------------------------------------------------------------------------
# 2. Route-Level Authorization (roles_required)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", VALID_ROLES)
def test_every_valid_role_can_access_explicitly_allowed_route(rbac_client, db_session, role):
    _create_user(db_session, username=f"user_{role}", role=role)
    _login(rbac_client, f"user_{role}")

    response = rbac_client.get(ROLE_ENDPOINTS[role])
    assert response.status_code == 200
    assert f"{role} ok".encode() in response.data


@pytest.mark.parametrize(
    "user_role,target_role",
    [
        (ROLE_SALESMAN, ROLE_ADMIN),
        (ROLE_ADMIN, ROLE_OWNER),
        (ROLE_PRODUCTION_SUPERVISOR, ROLE_ACCOUNTANT),
        (ROLE_ACCOUNTANT, ROLE_SALESMAN),
        (ROLE_OWNER, ROLE_PRODUCTION_SUPERVISOR),
    ],
)
def test_valid_but_unauthorized_role_receives_403(rbac_client, db_session, user_role, target_role):
    _create_user(db_session, username=f"unauth_{user_role}", role=user_role)
    _login(rbac_client, f"unauth_{user_role}")

    response = rbac_client.get(ROLE_ENDPOINTS[target_role])
    assert response.status_code == 403
    assert b"Access Denied" in response.data
    assert b"You do not have permission to access this page." in response.data


def test_unauthenticated_user_is_redirected_to_login(rbac_client):
    response = rbac_client.get("/_rbac_test/admin-or-owner")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login?next=%2F_rbac_test%2Fadmin-or-owner")


@pytest.mark.parametrize("unknown_role", ["superadmin", "guest", "ADMIN", "", None])
def test_unknown_role_receives_403_even_when_all_valid_roles_are_allowed(
    app, rbac_client, db_session, monkeypatch, unknown_role
):
    user = _create_user(db_session, username="tampered_user", role=ROLE_SALESMAN)
    _login(rbac_client, "tampered_user")

    # Clear cached g._login_user from the outer test app context so Flask-Login
    # reloads the user via _user_callback on the next request.
    g.pop("_login_user", None)

    # Simulate an authenticated user whose role is invalid/unknown (bypassing DB constraint).
    tampered_user = User(
        id=user.id,
        username=user.username,
        full_name="Tampered User",
        role=unknown_role,
        is_active=True,
    )
    monkeypatch.setattr(app.login_manager, "_user_callback", lambda _uid: tampered_user)

    response = rbac_client.get("/_rbac_test/any-valid-role")
    assert response.status_code == 403
    assert b"Access Denied" in response.data


@pytest.mark.parametrize(
    "role,expected_status",
    [
        (ROLE_SALESMAN, 403),
        (ROLE_ADMIN, 200),
        (ROLE_PRODUCTION_SUPERVISOR, 403),
        (ROLE_ACCOUNTANT, 403),
        (ROLE_OWNER, 200),
    ],
)
def test_multiple_allowed_roles_work_correctly(rbac_client, db_session, role, expected_status):
    _create_user(db_session, username=f"multi_{role}", role=role)
    _login(rbac_client, f"multi_{role}")

    response = rbac_client.get("/_rbac_test/admin-or-owner")
    assert response.status_code == expected_status
    if expected_status == 200:
        assert b"admin or owner ok" in response.data
    else:
        assert b"Access Denied" in response.data


def test_roles_required_rejects_empty_or_unknown_roles_at_definition_time():
    with pytest.raises(ValueError, match="at least one valid role"):
        roles_required()

    with pytest.raises(ValueError, match="Unknown role"):
        roles_required("superadmin")

    with pytest.raises(ValueError, match="Unknown role"):
        roles_required(ROLE_ADMIN, "manager")


def test_user_model_rejects_unknown_role_in_database(db_session):
    bad_user = User(
        username="bad_role_user",
        full_name="Bad Role User",
        role="superadmin",
    )
    bad_user.set_password("secret")
    db_session.add(bad_user)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# ---------------------------------------------------------------------------
# 3. Role Definitions & Centralized Permission Mapping
# ---------------------------------------------------------------------------


def test_valid_roles_match_exact_five_expected_values():
    assert VALID_ROLES == (
        "salesman",
        "admin",
        "production_supervisor",
        "accountant",
        "owner",
    )
    assert set(ROLE_PERMISSIONS.keys()) == set(VALID_ROLES)

    for role in VALID_ROLES:
        assert is_valid_role(role) is True
    for bad_role in ("superadmin", "ADMIN", "", None, 123):
        assert is_valid_role(bad_role) is False
        assert role_has_permission(bad_role, CAP_VIEW_INVENTORY) is False


def test_role_permissions_matrix_matches_intended_capabilities():
    expected_capabilities = (
        CAP_SUBMIT_SALES_ORDERS,
        CAP_VIEW_OWN_SALES_ORDERS,
        CAP_VERIFY_APPROVE_ORDERS,
        CAP_MANAGE_PRODUCTS_CUSTOMERS,
        CAP_RECORD_PURCHASES,
        CAP_RECORD_PRODUCTION_RUNS,
        CAP_VIEW_INVENTORY,
        CAP_MANAGE_STOCK_ADJUSTMENTS,
        CAP_MANAGE_INVOICES_DISPATCH,
        CAP_RECORD_PAYMENTS,
        CAP_VIEW_FINANCIAL_REPORTS,
        CAP_VIEW_OWNER_DASHBOARD,
        CAP_MANAGE_USERS,
    )
    assert ALL_CAPABILITIES == expected_capabilities

    expected_matrix = {
        CAP_SUBMIT_SALES_ORDERS: {ROLE_SALESMAN, ROLE_ADMIN, ROLE_OWNER},
        CAP_VIEW_OWN_SALES_ORDERS: {ROLE_SALESMAN, ROLE_ADMIN, ROLE_ACCOUNTANT, ROLE_OWNER},
        CAP_VERIFY_APPROVE_ORDERS: {ROLE_ADMIN, ROLE_OWNER},
        CAP_MANAGE_PRODUCTS_CUSTOMERS: {ROLE_ADMIN, ROLE_OWNER},
        CAP_RECORD_PURCHASES: {ROLE_ADMIN, ROLE_OWNER},
        CAP_RECORD_PRODUCTION_RUNS: {ROLE_ADMIN, ROLE_PRODUCTION_SUPERVISOR, ROLE_OWNER},
        CAP_VIEW_INVENTORY: {
            ROLE_SALESMAN,
            ROLE_ADMIN,
            ROLE_PRODUCTION_SUPERVISOR,
            ROLE_ACCOUNTANT,
            ROLE_OWNER,
        },
        CAP_MANAGE_STOCK_ADJUSTMENTS: {ROLE_ADMIN, ROLE_OWNER},
        CAP_MANAGE_INVOICES_DISPATCH: {ROLE_ADMIN, ROLE_OWNER},
        CAP_RECORD_PAYMENTS: {ROLE_ADMIN, ROLE_OWNER},
        CAP_VIEW_FINANCIAL_REPORTS: {ROLE_ADMIN, ROLE_ACCOUNTANT, ROLE_OWNER},
        CAP_VIEW_OWNER_DASHBOARD: {ROLE_OWNER},
        CAP_MANAGE_USERS: {ROLE_ADMIN, ROLE_OWNER},
    }

    for capability, expected_roles in expected_matrix.items():
        for role in VALID_ROLES:
            expected_allowed = role in expected_roles
            assert role_has_permission(role, capability) is expected_allowed, (
                f"Mismatch for role={role!r}, capability={capability!r}"
            )
            assert (capability in ROLE_PERMISSIONS[role]) is expected_allowed

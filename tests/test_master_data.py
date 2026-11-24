"""Master Data tests: Items, Customers, Suppliers, Units, Conversions, and RBAC."""

import pytest

from app.csrf import CSRF_SESSION_KEY
from app.models import Customer, Item, ItemUnitConversion, Supplier, Unit, User
from app.permissions import (
    ROLE_ACCOUNTANT,
    ROLE_ADMIN,
    ROLE_OWNER,
    ROLE_PRODUCTION_SUPERVISOR,
    ROLE_SALESMAN,
)


def _create_user(db_session, *, username: str, role: str, password: str = "secret-pass"):
    user = User(
        username=username,
        full_name=f"{username.title()} User",
        email=f"{username}@example.com",
        role=role,
        is_active=True,
    )
    user.set_password(password)
    db_session.add(user)
    db_session.commit()
    return user


def _csrf(client) -> str:
    with client.session_transaction() as sess:
        return sess[CSRF_SESSION_KEY]


def _login(client, username: str, password: str = "secret-pass"):
    client.get("/login")
    res = client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            "csrf_token": _csrf(client),
        },
    )
    assert res.status_code == 302
    return res


def _seed_standard_units(db_session):
    """Create common mass, volume, and count units used across tests."""
    g = Unit(code="g", name="Gram", dimension="mass", factor_to_base=1)
    ml = Unit(code="ml", name="Millilitre", dimension="volume", factor_to_base=1)
    piece = Unit(code="piece", name="Piece", dimension="count", factor_to_base=1)
    packet = Unit(code="packet", name="Packet", dimension="count", factor_to_base=1)
    carton = Unit(code="carton", name="Carton", dimension="count", factor_to_base=1)
    db_session.add_all([g, ml, piece, packet, carton])
    db_session.flush()

    kg = Unit(code="kg", name="Kilogram", dimension="mass", base_unit_id=g.id, factor_to_base=1000)
    litre = Unit(code="L", name="Litre", dimension="volume", base_unit_id=ml.id, factor_to_base=1000)
    db_session.add_all([kg, litre])
    db_session.commit()
    return {
        "g": g,
        "kg": kg,
        "ml": ml,
        "L": litre,
        "piece": piece,
        "packet": packet,
        "carton": carton,
    }


# ---------------------------------------------------------------------------
# 1. Items CRUD, Validation & Deactivation
# ---------------------------------------------------------------------------


def test_items_create_list_view_edit_and_deactivate(client, db_session):
    _create_user(db_session, username="admin_user", role=ROLE_ADMIN)
    units = _seed_standard_units(db_session)
    _login(client, "admin_user")

    # Create item (finished good)
    res = client.post(
        "/master/items/new",
        data={
            "csrf_token": _csrf(client),
            "sku": "FG-MURUKKU-200G",
            "name": "Murukku 200g",
            "item_type": "finished_good",
            "stock_unit_id": str(units["packet"].id),
            "hsn_code": "21069099",
            "gst_rate": "5",
            "default_sale_price": "50.00",
            "is_active": "1",
        },
        follow_redirects=True,
    )
    assert res.status_code == 200
    assert b"Murukku 200g" in res.data
    assert b"FG-MURUKKU-200G" in res.data
    assert b"5%" in res.data

    item = db_session.execute(db_session.query(Item).statement).scalar_one()
    assert item.gst_rate_bps == 500
    assert item.default_sale_price_paise == 5000
    assert item.is_active is True

    # List & filter
    list_res = client.get("/master/items?item_type=finished_good&status=active")
    assert list_res.status_code == 200
    assert b"Murukku 200g" in list_res.data

    # Edit item
    edit_res = client.post(
        f"/master/items/{item.id}/edit",
        data={
            "csrf_token": _csrf(client),
            "sku": "FG-MURUKKU-200G",
            "name": "Crispy Murukku 200g",
            "item_type": "finished_good",
            "stock_unit_id": str(units["packet"].id),
            "hsn_code": "21069099",
            "gst_rate": "12.5",
            "default_sale_price": "55.50",
            "is_active": "1",
        },
        follow_redirects=True,
    )
    assert edit_res.status_code == 200
    assert b"Crispy Murukku 200g" in edit_res.data
    db_session.refresh(item)
    assert item.gst_rate_bps == 1250
    assert item.default_sale_price_paise == 5550

    # Deactivate (record remains in database, not deleted)
    deact_res = client.post(
        f"/master/items/{item.id}/toggle-active",
        data={"csrf_token": _csrf(client)},
        follow_redirects=True,
    )
    assert deact_res.status_code == 200
    assert b"Inactive" in deact_res.data
    db_session.refresh(item)
    assert item.is_active is False
    assert db_session.get(Item, item.id) is not None

    # Inactive filter shows the deactivated item; active filter hides it
    assert b"Crispy Murukku 200g" in client.get("/master/items?status=inactive").data
    assert b"Crispy Murukku 200g" not in client.get("/master/items?status=active").data


def test_items_validation_required_uniqueness_type_and_gst_rate(client, db_session):
    _create_user(db_session, username="owner_user", role=ROLE_OWNER)
    units = _seed_standard_units(db_session)
    _login(client, "owner_user")

    # Missing required fields
    missing_res = client.post(
        "/master/items/new",
        data={"csrf_token": _csrf(client)},
    )
    assert missing_res.status_code == 200
    assert b"SKU is required." in missing_res.data
    assert b"Item name is required." in missing_res.data
    assert b"Select a valid item type." in missing_res.data
    assert b"Base stock unit is required." in missing_res.data

    # Invalid item type and invalid GST rates
    for bad_gst, expected_msg in [
        ("-5", b"must be at least 0"),
        ("105", b"must not exceed 100"),
        ("abc", b"must be a valid number"),
        ("5.125", b"at most 2 decimal places"),
    ]:
        bad_res = client.post(
            "/master/items/new",
            data={
                "csrf_token": _csrf(client),
                "sku": "BAD-ITEM",
                "name": "Bad Item",
                "item_type": "invalid_type",
                "stock_unit_id": str(units["kg"].id),
                "gst_rate": bad_gst,
                "is_active": "1",
            },
        )
        assert bad_res.status_code == 200
        assert b"Select a valid item type." in bad_res.data
        assert expected_msg in bad_res.data

    assert db_session.query(Item).count() == 0

    # Create valid raw material, then attempt duplicate SKU (different case)
    ok_res = client.post(
        "/master/items/new",
        data={
            "csrf_token": _csrf(client),
            "sku": "RM-FLOUR",
            "name": "Rice Flour",
            "item_type": "raw_material",
            "stock_unit_id": str(units["kg"].id),
            "gst_rate": "0",
            "is_active": "1",
        },
    )
    assert ok_res.status_code == 302

    dup_res = client.post(
        "/master/items/new",
        data={
            "csrf_token": _csrf(client),
            "sku": "rm-flour",
            "name": "Duplicate Flour",
            "item_type": "raw_material",
            "stock_unit_id": str(units["kg"].id),
            "gst_rate": "0",
            "is_active": "1",
        },
    )
    assert dup_res.status_code == 200
    assert b"already in use" in dup_res.data
    assert db_session.query(Item).count() == 1


# ---------------------------------------------------------------------------
# 2. Customers CRUD, GSTIN & Deactivation
# ---------------------------------------------------------------------------


def test_customers_create_list_view_edit_gstin_and_deactivate(client, db_session):
    _create_user(db_session, username="admin_user", role=ROLE_ADMIN)
    _login(client, "admin_user")

    # Required field validation + invalid GSTIN
    bad_res = client.post(
        "/master/customers/new",
        data={
            "csrf_token": _csrf(client),
            "name": "",
            "customer_type": "invalid",
            "state": "",
            "gstin": "INVALID-GSTIN",
        },
    )
    assert bad_res.status_code == 200
    assert b"Customer name is required." in bad_res.data
    assert b"Select a valid customer type" in bad_res.data
    assert b"State is required" in bad_res.data
    assert b"valid 15-character Indian GSTIN" in bad_res.data

    # Create registered customer (lowercase GSTIN normalized to uppercase)
    reg_res = client.post(
        "/master/customers/new",
        data={
            "csrf_token": _csrf(client),
            "name": "Sri Murugan Traders",
            "customer_type": "distributor",
            "state": "Tamil Nadu",
            "gstin": "33aaaaa0000a1z5",
            "phone": "9876543210",
            "address": "12 Main Road, Madurai",
            "is_active": "1",
        },
        follow_redirects=True,
    )
    assert reg_res.status_code == 200
    assert b"Sri Murugan Traders" in reg_res.data
    assert b"33AAAAA0000A1Z5" in reg_res.data
    assert b"Tamil Nadu" in reg_res.data

    # Create unregistered customer without GSTIN
    unreg_res = client.post(
        "/master/customers/new",
        data={
            "csrf_token": _csrf(client),
            "name": "Local Corner Shop",
            "customer_type": "retailer",
            "state": "Karnataka",
            "gstin": "",
            "is_active": "1",
        },
        follow_redirects=True,
    )
    assert unreg_res.status_code == 200
    assert b"Local Corner Shop" in unreg_res.data
    assert b"Unregistered" in unreg_res.data

    unreg = db_session.query(Customer).filter_by(name="Local Corner Shop").one()
    assert unreg.gstin is None
    assert unreg.state == "Karnataka"

    # List & edit customer
    assert b"Sri Murugan Traders" in client.get("/master/customers").data
    edit_res = client.post(
        f"/master/customers/{unreg.id}/edit",
        data={
            "csrf_token": _csrf(client),
            "name": "Local Corner Store",
            "customer_type": "retailer",
            "state": "Kerala",
            "gstin": "",
            "phone": "9000011111",
            "address": "MG Road, Kochi",
            "is_active": "1",
        },
        follow_redirects=True,
    )
    assert edit_res.status_code == 200
    db_session.refresh(unreg)
    assert unreg.name == "Local Corner Store"
    assert unreg.state == "Kerala"

    # Deactivate preserves row in DB
    client.post(
        f"/master/customers/{unreg.id}/toggle-active",
        data={"csrf_token": _csrf(client)},
    )
    db_session.refresh(unreg)
    assert unreg.is_active is False
    assert db_session.get(Customer, unreg.id) is not None


# ---------------------------------------------------------------------------
# 3. Suppliers CRUD, GSTIN & Deactivation
# ---------------------------------------------------------------------------


def test_suppliers_create_list_view_edit_gstin_and_deactivate(client, db_session):
    _create_user(db_session, username="owner_user", role=ROLE_OWNER)
    _login(client, "owner_user")

    # Validation: missing required + bad GSTIN (including invalid state code 00)
    bad_res = client.post(
        "/master/suppliers/new",
        data={
            "csrf_token": _csrf(client),
            "name": "",
            "state": "",
            "gstin": "00AAAAA0000A1Z5",
        },
    )
    assert bad_res.status_code == 200
    assert b"Supplier name is required." in bad_res.data
    assert b"State is required" in bad_res.data
    assert b"valid 15-character Indian GSTIN" in bad_res.data

    # Create unregistered supplier (blank GSTIN allowed)
    ok_res = client.post(
        "/master/suppliers/new",
        data={
            "csrf_token": _csrf(client),
            "name": "Village Spice Farm",
            "state": "Tamil Nadu",
            "gstin": "",
            "phone": "9988776655",
            "is_active": "1",
        },
        follow_redirects=True,
    )
    assert ok_res.status_code == 200
    assert b"Village Spice Farm" in ok_res.data
    assert b"Unregistered" in ok_res.data

    supplier = db_session.query(Supplier).filter_by(name="Village Spice Farm").one()
    assert supplier.gstin is None

    # List & edit to add valid GSTIN
    assert b"Village Spice Farm" in client.get("/master/suppliers").data
    edit_res = client.post(
        f"/master/suppliers/{supplier.id}/edit",
        data={
            "csrf_token": _csrf(client),
            "name": "Village Spice Mills",
            "state": "Andhra Pradesh",
            "gstin": "37BBBBB1111B2Z6",
            "phone": "9988776655",
            "address": "Guntur",
            "is_active": "1",
        },
        follow_redirects=True,
    )
    assert edit_res.status_code == 200
    db_session.refresh(supplier)
    assert supplier.name == "Village Spice Mills"
    assert supplier.gstin == "37BBBBB1111B2Z6"
    assert supplier.state == "Andhra Pradesh"

    # Deactivate preserves row in DB
    client.post(
        f"/master/suppliers/{supplier.id}/toggle-active",
        data={"csrf_token": _csrf(client)},
    )
    db_session.refresh(supplier)
    assert supplier.is_active is False
    assert db_session.get(Supplier, supplier.id) is not None


# ---------------------------------------------------------------------------
# 4. Units & Item-Specific Conversions
# ---------------------------------------------------------------------------


def test_units_create_list_edit_and_dimension_validation(client, db_session):
    _create_user(db_session, username="admin_user", role=ROLE_ADMIN)
    _login(client, "admin_user")

    # Create base unit g (mass)
    res_g = client.post(
        "/master/units/new",
        data={
            "csrf_token": _csrf(client),
            "code": "g",
            "name": "Gram",
            "dimension": "mass",
            "base_unit_id": "",
            "factor_to_base": "1",
        },
        follow_redirects=True,
    )
    assert res_g.status_code == 200
    unit_g = db_session.query(Unit).filter_by(code="g").one()

    # Create derived unit kg (1 kg = 1000 g)
    res_kg = client.post(
        "/master/units/new",
        data={
            "csrf_token": _csrf(client),
            "code": "kg",
            "name": "Kilogram",
            "dimension": "mass",
            "base_unit_id": str(unit_g.id),
            "factor_to_base": "1000",
        },
        follow_redirects=True,
    )
    assert res_kg.status_code == 200
    assert b"1 kg = 1000 g" in res_kg.data

    # Duplicate unit code rejected (case-insensitive)
    dup_res = client.post(
        "/master/units/new",
        data={
            "csrf_token": _csrf(client),
            "code": "KG",
            "name": "Duplicate Kilogram",
            "dimension": "mass",
            "factor_to_base": "1000",
        },
    )
    assert dup_res.status_code == 200
    assert b"already exists" in dup_res.data

    # Cross-dimension base unit rejected (volume unit referencing mass base unit)
    bad_dim_res = client.post(
        "/master/units/new",
        data={
            "csrf_token": _csrf(client),
            "code": "L",
            "name": "Litre",
            "dimension": "volume",
            "base_unit_id": str(unit_g.id),
            "factor_to_base": "1000",
        },
    )
    assert bad_dim_res.status_code == 200
    assert b"incompatible with" in bad_dim_res.data

    # Edit unit name
    unit_kg = db_session.query(Unit).filter_by(code="kg").one()
    edit_res = client.post(
        f"/master/units/{unit_kg.id}/edit",
        data={
            "csrf_token": _csrf(client),
            "code": "kg",
            "name": "Kilogram (Metric)",
            "dimension": "mass",
            "base_unit_id": str(unit_g.id),
            "factor_to_base": "1000",
        },
        follow_redirects=True,
    )
    assert edit_res.status_code == 200
    assert b"Kilogram (Metric)" in edit_res.data


def test_item_specific_carton_ratios_and_incompatible_conversion_validation(client, db_session):
    _create_user(db_session, username="admin_user", role=ROLE_ADMIN)
    units = _seed_standard_units(db_session)
    _login(client, "admin_user")

    murukku = Item(
        sku="FG-MURUKKU",
        name="Murukku 200g",
        item_type="finished_good",
        stock_unit_id=units["packet"].id,
        gst_rate_bps=500,
    )
    chikki = Item(
        sku="FG-CHIKKI",
        name="Peanut Chikki 100g",
        item_type="finished_good",
        stock_unit_id=units["packet"].id,
        gst_rate_bps=500,
    )
    flour = Item(
        sku="RM-FLOUR",
        name="Rice Flour",
        item_type="raw_material",
        stock_unit_id=units["kg"].id,
        gst_rate_bps=0,
    )
    db_session.add_all([murukku, chikki, flour])
    db_session.commit()

    # Different items can have different carton-to-packet ratios: 20 vs 30
    res_a = client.post(
        f"/master/items/{murukku.id}/conversions",
        data={
            "csrf_token": _csrf(client),
            "from_unit_id": str(units["carton"].id),
            "to_unit_id": str(units["packet"].id),
            "multiplier": "20",
        },
        follow_redirects=True,
    )
    assert res_a.status_code == 200
    assert b"1 carton = 20 packet" in res_a.data

    res_b = client.post(
        "/master/conversions/new",
        data={
            "csrf_token": _csrf(client),
            "item_id": str(chikki.id),
            "from_unit_id": str(units["carton"].id),
            "to_unit_id": str(units["packet"].id),
            "multiplier": "30",
        },
        follow_redirects=True,
    )
    assert res_b.status_code == 200
    assert b"1 carton = 20 packet" in res_b.data
    assert b"1 carton = 30 packet" in res_b.data

    # piece <-> packet conversion on count item also works
    res_pkt = client.post(
        f"/master/items/{chikki.id}/conversions",
        data={
            "csrf_token": _csrf(client),
            "from_unit_id": str(units["packet"].id),
            "to_unit_id": str(units["piece"].id),
            "multiplier": "10",
        },
        follow_redirects=True,
    )
    assert res_pkt.status_code == 200
    assert b"1 packet = 10 piece" in res_pkt.data

    # Incompatible dimension conversion (mass kg -> count packet) rejected
    bad_cross = client.post(
        f"/master/items/{murukku.id}/conversions",
        data={
            "csrf_token": _csrf(client),
            "from_unit_id": str(units["kg"].id),
            "to_unit_id": str(units["packet"].id),
            "multiplier": "5",
        },
    )
    assert bad_cross.status_code == 200
    assert b"incompatible dimensions" in bad_cross.data

    # Same-dimension units that do not match the item's stock unit dimension (L -> ml on mass item)
    bad_item_dim = client.post(
        f"/master/items/{flour.id}/conversions",
        data={
            "csrf_token": _csrf(client),
            "from_unit_id": str(units["L"].id),
            "to_unit_id": str(units["ml"].id),
            "multiplier": "1000",
        },
    )
    assert bad_item_dim.status_code == 200
    assert b"match item stock unit dimension" in bad_item_dim.data

    # Inconsistent standard metric ratio (1 kg = 500 g) rejected
    bad_metric = client.post(
        f"/master/items/{flour.id}/conversions",
        data={
            "csrf_token": _csrf(client),
            "from_unit_id": str(units["kg"].id),
            "to_unit_id": str(units["g"].id),
            "multiplier": "500",
        },
    )
    assert bad_metric.status_code == 200
    assert b"Standard unit conversion requires 1 kg = 1000 g" in bad_metric.data

    # Same unit (packet -> packet) and non-positive multiplier rejected
    bad_same = client.post(
        f"/master/items/{murukku.id}/conversions",
        data={
            "csrf_token": _csrf(client),
            "from_unit_id": str(units["packet"].id),
            "to_unit_id": str(units["packet"].id),
            "multiplier": "0",
        },
    )
    assert bad_same.status_code == 200
    assert b"must be different" in bad_same.data
    assert b"positive whole number" in bad_same.data

    # Duplicate and reverse conversions rejected
    dup_conv = client.post(
        f"/master/items/{murukku.id}/conversions",
        data={
            "csrf_token": _csrf(client),
            "from_unit_id": str(units["carton"].id),
            "to_unit_id": str(units["packet"].id),
            "multiplier": "24",
        },
    )
    assert dup_conv.status_code == 200
    assert b"already exists" in dup_conv.data

    rev_conv = client.post(
        f"/master/items/{murukku.id}/conversions",
        data={
            "csrf_token": _csrf(client),
            "from_unit_id": str(units["packet"].id),
            "to_unit_id": str(units["carton"].id),
            "multiplier": "1",
        },
    )
    assert rev_conv.status_code == 200
    assert b"reverse conversion" in rev_conv.data

    # Editing an item's stock unit to another dimension while conversions exist is blocked
    bad_item_edit = client.post(
        f"/master/items/{murukku.id}/edit",
        data={
            "csrf_token": _csrf(client),
            "sku": murukku.sku,
            "name": murukku.name,
            "item_type": murukku.item_type,
            "stock_unit_id": str(units["kg"].id),
            "gst_rate": "5",
            "is_active": "1",
        },
    )
    assert bad_item_edit.status_code == 200
    assert b"different dimension" in bad_item_edit.data

    # Updating and removing a conversion works
    conv = (
        db_session.query(ItemUnitConversion)
        .filter_by(item_id=murukku.id, from_unit_id=units["carton"].id)
        .one()
    )
    upd_res = client.post(
        f"/master/items/{murukku.id}/conversions/{conv.id}/edit",
        data={"csrf_token": _csrf(client), "multiplier": "24"},
        follow_redirects=True,
    )
    assert upd_res.status_code == 200
    db_session.refresh(conv)
    assert conv.multiplier == 24

    del_res = client.post(
        f"/master/items/{murukku.id}/conversions/{conv.id}/delete",
        data={"csrf_token": _csrf(client)},
        follow_redirects=True,
    )
    assert del_res.status_code == 200
    assert db_session.get(ItemUnitConversion, conv.id) is None


# ---------------------------------------------------------------------------
# 5. Authorization, Unauthenticated Redirects & Role-Aware Navigation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/master/items",
        "/master/items/new",
        "/master/customers",
        "/master/customers/new",
        "/master/suppliers",
        "/master/suppliers/new",
        "/master/units",
        "/master/units/new",
        "/master/conversions/new",
    ],
)
def test_unauthenticated_master_data_routes_redirect_to_login(client, path):
    response = client.get(path)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


@pytest.mark.parametrize("role", [ROLE_SALESMAN, ROLE_PRODUCTION_SUPERVISOR, ROLE_ACCOUNTANT])
def test_non_admin_roles_cannot_create_or_edit_any_master_data(client, db_session, role):
    _create_user(db_session, username=f"user_{role}", role=role)
    units = _seed_standard_units(db_session)
    item = Item(
        sku="SKU-1",
        name="Sample Item",
        item_type="raw_material",
        stock_unit_id=units["kg"].id,
    )
    customer = Customer(name="Cust 1", customer_type="retailer", state="Tamil Nadu")
    supplier = Supplier(name="Supp 1", state="Tamil Nadu")
    db_session.add_all([item, customer, supplier])
    db_session.commit()

    _login(client, f"user_{role}")

    write_get_paths = [
        "/master/items/new",
        f"/master/items/{item.id}/edit",
        "/master/customers/new",
        f"/master/customers/{customer.id}/edit",
        "/master/suppliers/new",
        f"/master/suppliers/{supplier.id}/edit",
        "/master/units/new",
        f"/master/units/{units['kg'].id}/edit",
        "/master/conversions/new",
    ]
    for path in write_get_paths:
        assert client.get(path).status_code == 403, f"Expected 403 on GET {path} for {role}"

    write_post_paths = [
        "/master/items/new",
        f"/master/items/{item.id}/edit",
        f"/master/items/{item.id}/toggle-active",
        f"/master/items/{item.id}/conversions",
        "/master/customers/new",
        f"/master/customers/{customer.id}/edit",
        f"/master/customers/{customer.id}/toggle-active",
        "/master/suppliers/new",
        f"/master/suppliers/{supplier.id}/edit",
        f"/master/suppliers/{supplier.id}/toggle-active",
        "/master/units/new",
        f"/master/units/{units['kg'].id}/edit",
        "/master/conversions/new",
    ]
    for path in write_post_paths:
        res = client.post(path, data={"csrf_token": _csrf(client)})
        assert res.status_code == 403, f"Expected 403 on POST {path} for {role}"


@pytest.mark.parametrize(
    "role,items_ok,customers_ok,suppliers_ok,units_ok",
    [
        (ROLE_SALESMAN, True, True, False, True),
        (ROLE_PRODUCTION_SUPERVISOR, True, False, False, True),
        (ROLE_ACCOUNTANT, True, True, False, True),
        (ROLE_ADMIN, True, True, True, True),
        (ROLE_OWNER, True, True, True, True),
    ],
)
def test_master_data_read_permissions_and_navbar_visibility(
    client, db_session, role, items_ok, customers_ok, suppliers_ok, units_ok
):
    _create_user(db_session, username=f"nav_{role}", role=role)
    _login(client, f"nav_{role}")

    # Check route read access
    assert client.get("/master/items").status_code == (200 if items_ok else 403)
    assert client.get("/master/customers").status_code == (200 if customers_ok else 403)
    assert client.get("/master/suppliers").status_code == (200 if suppliers_ok else 403)
    assert client.get("/master/units").status_code == (200 if units_ok else 403)

    # Check navbar links match authorized sections only
    home_html = client.get("/").data
    assert (b'href="/master/items"' in home_html) is items_ok
    assert (b'href="/master/customers"' in home_html) is customers_ok
    assert (b'href="/master/suppliers"' in home_html) is suppliers_ok
    assert (b'href="/master/units"' in home_html) is units_ok

    # Check write buttons are hidden from read-only roles on list pages
    items_html = client.get("/master/items").data
    can_write = role in (ROLE_ADMIN, ROLE_OWNER)
    assert (b"+ New Item" in items_html) is can_write

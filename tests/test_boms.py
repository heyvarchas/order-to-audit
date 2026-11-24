"""BOM management, exact unit conversion, versioning, integrity, RBAC, CSRF, and audit tests."""

from datetime import date
import json

import pytest
from sqlalchemy.exc import OperationalError

from app.csrf import CSRF_SESSION_KEY
from app.extensions import db
from app.models import (
    BOM,
    AuditLog,
    BOMItem,
    Item,
    ItemUnitConversion,
    ProductionRun,
    Unit,
    User,
)
from app.permissions import (
    ROLE_ACCOUNTANT,
    ROLE_ADMIN,
    ROLE_OWNER,
    ROLE_PRODUCTION_SUPERVISOR,
    ROLE_SALESMAN,
)
from app.units import (
    UnitConversionError,
    convert_qty_milli,
    convert_qty_milli_to_stock,
    format_milli,
    get_conversion_factor,
    get_convertible_units_for_item,
    parse_qty_to_milli,
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


def _seed_murukku_master_data(db_session):
    """Seed units and items needed for the Murukku 200 g benchmark BOM and conversion tests."""
    g = Unit(code="g", name="Gram", dimension="mass", factor_to_base=1)
    ml = Unit(code="ml", name="Millilitre", dimension="volume", factor_to_base=1)
    piece = Unit(code="piece", name="Piece", dimension="count", factor_to_base=1)
    packet = Unit(code="packet", name="Packet", dimension="count", factor_to_base=1)
    pack = Unit(code="pack", name="Pack", dimension="count", factor_to_base=1)
    carton = Unit(code="carton", name="Carton", dimension="count", factor_to_base=1)
    case = Unit(code="case", name="Master Case", dimension="count", factor_to_base=1)
    db_session.add_all([g, ml, piece, packet, pack, carton, case])
    db_session.flush()

    kg = Unit(code="kg", name="Kilogram", dimension="mass", base_unit_id=g.id, factor_to_base=1000)
    litre = Unit(code="L", name="Litre", dimension="volume", base_unit_id=ml.id, factor_to_base=1000)
    db_session.add_all([kg, litre])
    db_session.flush()

    murukku = Item(
        sku="FG-MURUKKU-200G",
        name="Murukku 200 g",
        item_type="finished_good",
        stock_unit_id=packet.id,
        hsn_code="21069099",
        gst_rate_bps=500,
        default_sale_price_paise=5000,
        is_active=True,
    )
    chikki = Item(
        sku="FG-CHIKKI-100G",
        name="Peanut Chikki 100 g",
        item_type="finished_good",
        stock_unit_id=packet.id,
        hsn_code="17049090",
        gst_rate_bps=500,
        default_sale_price_paise=3000,
        is_active=True,
    )
    flour = Item(
        sku="RM-FLOUR",
        name="Flour",
        item_type="raw_material",
        stock_unit_id=kg.id,
        gst_rate_bps=0,
        is_active=True,
    )
    oil = Item(
        sku="RM-OIL",
        name="Oil",
        item_type="raw_material",
        stock_unit_id=litre.id,
        gst_rate_bps=500,
        is_active=True,
    )
    spice_mix = Item(
        sku="RM-SPICE",
        name="Spice mix",
        item_type="raw_material",
        stock_unit_id=kg.id,
        gst_rate_bps=500,
        is_active=True,
    )
    pouches = Item(
        sku="PKG-POUCH-200G",
        name="Pouches",
        item_type="packaging",
        stock_unit_id=piece.id,
        gst_rate_bps=1800,
        is_active=True,
    )
    cartons = Item(
        sku="PKG-CARTON-STD",
        name="Cartons",
        item_type="packaging",
        stock_unit_id=piece.id,
        gst_rate_bps=1200,
        is_active=True,
    )
    db_session.add_all([murukku, chikki, flour, oil, spice_mix, pouches, cartons])
    db_session.commit()

    return {
        "units": {
            "g": g,
            "kg": kg,
            "ml": ml,
            "L": litre,
            "piece": piece,
            "packet": packet,
            "pack": pack,
            "carton": carton,
            "case": case,
        },
        "items": {
            "murukku": murukku,
            "chikki": chikki,
            "flour": flour,
            "oil": oil,
            "spice_mix": spice_mix,
            "pouches": pouches,
            "cartons": cartons,
        },
    }


def _create_bom_with_component(db_session, *, finished_item, material_item, input_unit, version=1, name="Standard BOM", expected_yield_milli=200000, qty_milli=40000, is_active=False):
    bom = BOM(
        finished_item_id=finished_item.id,
        version=version,
        name=name,
        expected_yield_milli=expected_yield_milli,
        is_active=is_active,
    )
    db_session.add(bom)
    db_session.flush()
    line = BOMItem(
        bom_id=bom.id,
        material_item_id=material_item.id,
        input_unit_id=input_unit.id,
        standard_qty_milli=qty_milli,
    )
    db_session.add(line)
    db_session.commit()
    return bom, line


# ---------------------------------------------------------------------------
# 1. Exact Quantity Parsing, Formatting & Unit Conversion Engine Tests
# ---------------------------------------------------------------------------


def test_parse_and_format_milli_exact_decimal_handling():
    assert parse_qty_to_milli("40") == (40000, None)
    assert parse_qty_to_milli("1.2") == (1200, None)
    assert parse_qty_to_milli("0.005") == (5, None)
    assert parse_qty_to_milli("200.000") == (200000, None)

    assert format_milli(40000) == "40"
    assert format_milli(1200) == "1.2"
    assert format_milli(5) == "0.005"
    assert format_milli(200000) == "200"

    for bad_input in ("", "   ", "0", "-1", "-0.001", "abc", "NaN", "Infinity", "1.2345"):
        val, err = parse_qty_to_milli(bad_input, field_label="Qty")
        assert val is None
        assert err is not None


def test_standard_and_item_specific_exact_unit_conversions(db_session):
    seed = _seed_murukku_master_data(db_session)
    u = seed["units"]
    items = seed["items"]

    # Standard metric: g <-> kg and ml <-> L
    assert convert_qty_milli_to_stock(items["spice_mix"], 1_200_000, u["g"]) == 1200  # 1200 g -> 1.2 kg
    assert convert_qty_milli_to_stock(items["oil"], 500_000, u["ml"]) == 500  # 500 ml -> 0.5 L
    assert convert_qty_milli(items["flour"], 1500, u["kg"], u["g"]) == 1_500_000  # 1.5 kg -> 1500 g

    # Non-exact standard conversion rejected: 0.5 g -> 0.0005 kg (0.5 milli-kg)
    with pytest.raises(UnitConversionError, match="cannot be converted exactly"):
        convert_qty_milli_to_stock(items["spice_mix"], 500, u["g"])

    # Item-specific conversions: 1 pack = 100 piece for Pouches; 1 case = 10 pack (multi-hop)
    db_session.add_all(
        [
            ItemUnitConversion(
                item_id=items["pouches"].id,
                from_unit_id=u["pack"].id,
                to_unit_id=u["piece"].id,
                multiplier=100,
            ),
            ItemUnitConversion(
                item_id=items["pouches"].id,
                from_unit_id=u["case"].id,
                to_unit_id=u["pack"].id,
                multiplier=10,
            ),
        ]
    )
    db_session.commit()

    assert convert_qty_milli_to_stock(items["pouches"], 2000, u["pack"]) == 200_000  # 2 pack -> 200 piece
    assert convert_qty_milli_to_stock(items["pouches"], 1000, u["case"]) == 1_000_000  # 1 case -> 1000 piece

    convertible_codes = [unit.code for unit in get_convertible_units_for_item(items["pouches"])]
    assert set(convertible_codes) == {"piece", "pack", "case"}
    assert "carton" not in convertible_codes

    # Reverse item-specific conversion: exact vs non-exact
    carton_item = Item(
        sku="PKG-BOX-STOCKED-CARTON",
        name="Boxes",
        item_type="packaging",
        stock_unit_id=u["carton"].id,
        is_active=True,
    )
    db_session.add(carton_item)
    db_session.flush()
    db_session.add(
        ItemUnitConversion(
            item_id=carton_item.id,
            from_unit_id=u["carton"].id,
            to_unit_id=u["packet"].id,
            multiplier=30,
        )
    )
    db_session.commit()

    # 15 packets -> 0.5 carton (500 milli-carton): exact
    assert convert_qty_milli_to_stock(carton_item, 15_000, u["packet"]) == 500
    # 1 packet -> 1000/30 milli-cartons: non-exact, must reject
    with pytest.raises(UnitConversionError, match="cannot be converted exactly"):
        convert_qty_milli_to_stock(carton_item, 1000, u["packet"])

    # Cross-dimension and missing conversion rejected
    assert get_conversion_factor(items["flour"], u["L"], u["kg"]) is None
    with pytest.raises(UnitConversionError, match="incompatible"):
        convert_qty_milli_to_stock(items["flour"], 1000, u["L"])

    with pytest.raises(UnitConversionError, match="No unit conversion exists"):
        convert_qty_milli_to_stock(items["cartons"], 1000, u["carton"])


def test_conflicting_item_conversion_paths_raise_error(db_session):
    seed = _seed_murukku_master_data(db_session)
    u = seed["units"]
    pouches = seed["items"]["pouches"]

    # Conflicting loop: 1 pack = 10 piece, 1 carton = 2 pack (= 20 piece), but 1 carton = 25 piece
    db_session.add_all(
        [
            ItemUnitConversion(item_id=pouches.id, from_unit_id=u["pack"].id, to_unit_id=u["piece"].id, multiplier=10),
            ItemUnitConversion(item_id=pouches.id, from_unit_id=u["carton"].id, to_unit_id=u["pack"].id, multiplier=2),
            ItemUnitConversion(item_id=pouches.id, from_unit_id=u["carton"].id, to_unit_id=u["piece"].id, multiplier=25),
        ]
    )
    db_session.commit()

    with pytest.raises(UnitConversionError, match="Conflicting unit conversion"):
        convert_qty_milli_to_stock(pouches, 1000, u["carton"])


# ---------------------------------------------------------------------------
# 2. Murukku 200 g Benchmark BOM & Full CRUD Workflow
# ---------------------------------------------------------------------------


def test_murukku_benchmark_bom_end_to_end_and_crud(client, db_session):
    admin = _create_user(db_session, username="admin_bom", role=ROLE_ADMIN)
    seed = _seed_murukku_master_data(db_session)
    u = seed["units"]
    items = seed["items"]
    _login(client, "admin_bom")

    # Create BOM header (starts inactive with auto-assigned version 1)
    create_res = client.post(
        "/boms/new",
        data={
            "csrf_token": _csrf(client),
            "finished_item_id": str(items["murukku"].id),
            "name": "Murukku 200 g Standard Batch",
            "expected_yield": "200",
            "version": "",
        },
        follow_redirects=True,
    )
    assert create_res.status_code == 200
    assert b"Murukku 200 g Standard Batch" in create_res.data
    assert b"200 packet" in create_res.data

    bom = db_session.query(BOM).one()
    assert bom.finished_item_id == items["murukku"].id
    assert bom.version == 1
    assert bom.expected_yield_milli == 200_000
    assert bom.is_active is False

    # Add all 5 benchmark components
    benchmark_components = [
        (items["flour"], "40", u["kg"], 40_000),
        (items["oil"], "10", u["L"], 10_000),
        (items["spice_mix"], "1.2", u["kg"], 1_200),
        (items["pouches"], "200", u["piece"], 200_000),
        (items["cartons"], "10", u["piece"], 10_000),
    ]
    for mat_item, qty_str, unit_obj, expected_milli in benchmark_components:
        add_res = client.post(
            f"/boms/{bom.id}/items",
            data={
                "csrf_token": _csrf(client),
                "material_item_id": str(mat_item.id),
                "standard_qty": qty_str,
                "input_unit_id": str(unit_obj.id),
            },
            follow_redirects=True,
        )
        assert add_res.status_code == 200
        assert b"Component added to BOM." in add_res.data

        line = db_session.query(BOMItem).filter_by(bom_id=bom.id, material_item_id=mat_item.id).one()
        assert line.standard_qty_milli == expected_milli
        assert line.input_unit_id == unit_obj.id

    # Activate the benchmark BOM
    act_res = client.post(
        f"/boms/{bom.id}/activate",
        data={"csrf_token": _csrf(client)},
        follow_redirects=True,
    )
    assert act_res.status_code == 200
    assert b"activated." in act_res.data
    db_session.refresh(bom)
    assert bom.is_active is True

    # Verify detail page displays all 5 components and yield
    detail_html = client.get(f"/boms/{bom.id}").data
    for snippet in (b"Murukku 200 g", b"200 packet", b"40 kg", b"10 L", b"1.2 kg", b"200 piece", b"10 piece"):
        assert snippet in detail_html

    # List & filter
    assert b"Murukku 200 g Standard Batch" in client.get(f"/boms?finished_item_id={items['murukku'].id}&status=active").data
    assert b"Murukku 200 g Standard Batch" not in client.get("/boms?status=inactive").data

    # Item detail page links to the BOM
    item_html = client.get(f"/master/items/{items['murukku'].id}").data
    assert b"Murukku 200 g Standard Batch" in item_html

    # Edit BOM header (still unlocked because no ProductionRun exists)
    edit_res = client.post(
        f"/boms/{bom.id}/edit",
        data={
            "csrf_token": _csrf(client),
            "name": "Murukku 200 g Master Batch",
            "expected_yield": "200",
        },
        follow_redirects=True,
    )
    assert edit_res.status_code == 200
    db_session.refresh(bom)
    assert bom.name == "Murukku 200 g Master Batch"

    # Edit component line to use standard unit conversion: Spice mix 1200 g (= 1.2 kg)
    spice_line = db_session.query(BOMItem).filter_by(bom_id=bom.id, material_item_id=items["spice_mix"].id).one()
    edit_line_res = client.post(
        f"/boms/{bom.id}/items/{spice_line.id}/edit",
        data={
            "csrf_token": _csrf(client),
            "standard_qty": "1200",
            "input_unit_id": str(u["g"].id),
        },
        follow_redirects=True,
    )
    assert edit_line_res.status_code == 200
    db_session.refresh(spice_line)
    assert spice_line.input_unit_id == u["g"].id
    assert spice_line.standard_qty_milli == 1_200_000
    assert convert_qty_milli_to_stock(items["spice_mix"], spice_line.standard_qty_milli, spice_line.input_unit) == 1200
    assert b"1200 g" in edit_line_res.data
    assert b"1.2 kg" in edit_line_res.data

    # Audit log entries recorded for create, 5 item adds, activate, header edit, item edit
    logs = db_session.query(AuditLog).filter_by(entity_type="bom", entity_id=bom.id).order_by(AuditLog.id.asc()).all()
    actions = [log.action for log in logs]
    assert actions == [
        "bom_created",
        "bom_item_added",
        "bom_item_added",
        "bom_item_added",
        "bom_item_added",
        "bom_item_added",
        "bom_activated",
        "bom_updated",
        "bom_item_updated",
    ]
    assert all(log.user_id == admin.id for log in logs)
    assert json.loads(logs[-1].new_values_json)["stock_qty_milli"] == 1200


def test_bom_item_uses_item_specific_conversion_and_displays_stock_equivalent(client, db_session):
    _create_user(db_session, username="owner_conv", role=ROLE_OWNER)
    seed = _seed_murukku_master_data(db_session)
    u = seed["units"]
    items = seed["items"]

    # Define 1 pack = 100 piece for Pouches
    db_session.add(
        ItemUnitConversion(
            item_id=items["pouches"].id,
            from_unit_id=u["pack"].id,
            to_unit_id=u["piece"].id,
            multiplier=100,
        )
    )
    db_session.commit()

    bom = BOM(
        finished_item_id=items["murukku"].id,
        version=1,
        name="Murukku Pack Conversion BOM",
        expected_yield_milli=200_000,
        is_active=False,
    )
    db_session.add(bom)
    db_session.commit()

    _login(client, "owner_conv")

    # Add 2 pack of Pouches -> stored as 2000 milli-pack, converts to 200 piece
    res = client.post(
        f"/boms/{bom.id}/items",
        data={
            "csrf_token": _csrf(client),
            "material_item_id": str(items["pouches"].id),
            "standard_qty": "2",
            "input_unit_id": str(u["pack"].id),
        },
        follow_redirects=True,
    )
    assert res.status_code == 200
    assert b"2 pack" in res.data
    assert b"200 piece" in res.data

    line = db_session.query(BOMItem).filter_by(bom_id=bom.id, material_item_id=items["pouches"].id).one()
    assert line.standard_qty_milli == 2000
    assert line.input_unit_id == u["pack"].id


# ---------------------------------------------------------------------------
# 3. Validation, Duplicates, Invalid Conversions & Empty BOM Prevention
# ---------------------------------------------------------------------------


def test_bom_validation_prevents_invalid_items_duplicates_quantities_and_conversions(client, db_session):
    _create_user(db_session, username="admin_val", role=ROLE_ADMIN)
    seed = _seed_murukku_master_data(db_session)
    u = seed["units"]
    items = seed["items"]

    inactive_fg = Item(
        sku="FG-INACTIVE",
        name="Inactive Snack",
        item_type="finished_good",
        stock_unit_id=u["packet"].id,
        is_active=False,
    )
    inactive_rm = Item(
        sku="RM-INACTIVE",
        name="Inactive Salt",
        item_type="raw_material",
        stock_unit_id=u["kg"].id,
        is_active=False,
    )
    db_session.add_all([inactive_fg, inactive_rm])
    db_session.commit()

    _login(client, "admin_val")

    # Reject raw material as finished_item_id, invalid yield, and attempting active on empty create
    bad_create = client.post(
        "/boms/new",
        data={
            "csrf_token": _csrf(client),
            "finished_item_id": str(items["flour"].id),
            "name": "",
            "expected_yield": "0",
            "version": "0",
            "is_active": "1",
        },
    )
    assert bad_create.status_code == 200
    assert b"Finished Good items" in bad_create.data
    assert b"BOM name is required." in bad_create.data
    assert b"must be greater than zero" in bad_create.data
    assert b"positive whole number" in bad_create.data
    assert b"empty BOM cannot be activated" in bad_create.data

    # Reject inactive finished product
    bad_inactive_fg = client.post(
        "/boms/new",
        data={
            "csrf_token": _csrf(client),
            "finished_item_id": str(inactive_fg.id),
            "name": "Inactive BOM",
            "expected_yield": "100",
        },
    )
    assert bad_inactive_fg.status_code == 200
    assert b"inactive finished product" in bad_inactive_fg.data

    # Reject >3 decimals on expected yield
    bad_dec_yield = client.post(
        "/boms/new",
        data={
            "csrf_token": _csrf(client),
            "finished_item_id": str(items["murukku"].id),
            "name": "Bad Decimal Yield",
            "expected_yield": "10.1234",
        },
    )
    assert bad_dec_yield.status_code == 200
    assert b"at most 3 decimal places" in bad_dec_yield.data

    # Create valid BOM v1
    ok_res = client.post(
        "/boms/new",
        data={
            "csrf_token": _csrf(client),
            "finished_item_id": str(items["murukku"].id),
            "name": "Murukku v1",
            "expected_yield": "200",
            "version": "1",
        },
    )
    assert ok_res.status_code == 302
    bom = db_session.query(BOM).one()

    # Duplicate version rejected
    dup_ver = client.post(
        "/boms/new",
        data={
            "csrf_token": _csrf(client),
            "finished_item_id": str(items["murukku"].id),
            "name": "Murukku v1 Duplicate",
            "expected_yield": "200",
            "version": "1",
        },
    )
    assert dup_ver.status_code == 200
    assert b"Version 1 already exists" in dup_ver.data

    # Cannot activate an empty BOM
    act_empty = client.post(
        f"/boms/{bom.id}/activate",
        data={"csrf_token": _csrf(client)},
        follow_redirects=True,
    )
    assert act_empty.status_code == 200
    assert b"Cannot activate an empty BOM" in act_empty.data
    db_session.refresh(bom)
    assert bom.is_active is False

    # Reject self-reference, other finished_good, inactive material
    for bad_mat, expected_msg in [
        (items["murukku"], b"component of its own BOM"),
        (items["chikki"], b"Only Raw Material or Packaging Material"),
        (inactive_rm, b"inactive material item"),
    ]:
        res = client.post(
            f"/boms/{bom.id}/items",
            data={
                "csrf_token": _csrf(client),
                "material_item_id": str(bad_mat.id),
                "standard_qty": "10",
                "input_unit_id": str(bad_mat.stock_unit_id),
            },
        )
        assert res.status_code == 200
        assert expected_msg in res.data

    # Reject cross-dimension unit (L for Flour stocked in kg)
    bad_dim = client.post(
        f"/boms/{bom.id}/items",
        data={
            "csrf_token": _csrf(client),
            "material_item_id": str(items["flour"].id),
            "standard_qty": "10",
            "input_unit_id": str(u["L"].id),
        },
    )
    assert bad_dim.status_code == 200
    assert b"incompatible" in bad_dim.data

    # Reject missing item-specific conversion (carton for Pouches stocked in piece)
    missing_conv = client.post(
        f"/boms/{bom.id}/items",
        data={
            "csrf_token": _csrf(client),
            "material_item_id": str(items["pouches"].id),
            "standard_qty": "2",
            "input_unit_id": str(u["carton"].id),
        },
    )
    assert missing_conv.status_code == 200
    assert b"No unit conversion exists" in missing_conv.data

    # Reject non-exact conversion (0.5 g for Spice mix stocked in kg)
    non_exact = client.post(
        f"/boms/{bom.id}/items",
        data={
            "csrf_token": _csrf(client),
            "material_item_id": str(items["spice_mix"].id),
            "standard_qty": "0.5",
            "input_unit_id": str(u["g"].id),
        },
    )
    assert non_exact.status_code == 200
    assert b"cannot be converted exactly" in non_exact.data

    # Reject invalid quantities (0, negative, >3 decimals, non-numeric)
    for bad_qty in ("0", "-5", "1.2345", "abc"):
        res = client.post(
            f"/boms/{bom.id}/items",
            data={
                "csrf_token": _csrf(client),
                "material_item_id": str(items["flour"].id),
                "standard_qty": bad_qty,
                "input_unit_id": str(u["kg"].id),
            },
        )
        assert res.status_code == 200
        assert db_session.query(BOMItem).count() == 0

    # Add Flour once, then attempt duplicate Flour entry -> rejected
    ok_add = client.post(
        f"/boms/{bom.id}/items",
        data={
            "csrf_token": _csrf(client),
            "material_item_id": str(items["flour"].id),
            "standard_qty": "40",
            "input_unit_id": str(u["kg"].id),
        },
    )
    assert ok_add.status_code == 302

    dup_add = client.post(
        f"/boms/{bom.id}/items",
        data={
            "csrf_token": _csrf(client),
            "material_item_id": str(items["flour"].id),
            "standard_qty": "5",
            "input_unit_id": str(u["kg"].id),
        },
    )
    assert dup_add.status_code == 200
    assert b"already listed in this BOM" in dup_add.data
    assert db_session.query(BOMItem).filter_by(bom_id=bom.id).count() == 1


# ---------------------------------------------------------------------------
# 4. Active BOM Uniqueness, Last-Component Guard & Version Cloning
# ---------------------------------------------------------------------------


def test_active_bom_uniqueness_atomic_switch_and_last_component_guard(client, db_session):
    _create_user(db_session, username="owner_act", role=ROLE_OWNER)
    seed = _seed_murukku_master_data(db_session)
    u = seed["units"]
    items = seed["items"]

    # Create active v1 for Murukku, inactive v2 for Murukku, and active v1 for Chikki
    bom_v1, line_v1 = _create_bom_with_component(
        db_session,
        finished_item=items["murukku"],
        material_item=items["flour"],
        input_unit=u["kg"],
        version=1,
        name="Murukku v1",
        is_active=True,
    )
    bom_v2, _ = _create_bom_with_component(
        db_session,
        finished_item=items["murukku"],
        material_item=items["flour"],
        input_unit=u["kg"],
        version=2,
        name="Murukku v2",
        qty_milli=42_000,
        is_active=False,
    )
    chikki_bom, _ = _create_bom_with_component(
        db_session,
        finished_item=items["chikki"],
        material_item=items["oil"],
        input_unit=u["L"],
        version=1,
        name="Chikki v1",
        is_active=True,
    )

    _login(client, "owner_act")

    # Cannot remove last component from an active BOM
    rm_last = client.post(
        f"/boms/{bom_v1.id}/items/{line_v1.id}/delete",
        data={"csrf_token": _csrf(client)},
        follow_redirects=True,
    )
    assert rm_last.status_code == 200
    assert b"Cannot remove the last component from an active BOM" in rm_last.data
    assert db_session.get(BOMItem, line_v1.id) is not None

    # Cannot delete an active BOM directly
    del_active = client.post(
        f"/boms/{bom_v1.id}/delete",
        data={"csrf_token": _csrf(client)},
        follow_redirects=True,
    )
    assert del_active.status_code == 200
    assert b"Deactivate this BOM before deleting it." in del_active.data
    assert db_session.get(BOM, bom_v1.id) is not None

    # Activating v2 atomically deactivates v1, without touching Chikki's active BOM
    act_v2 = client.post(
        f"/boms/{bom_v2.id}/activate",
        data={"csrf_token": _csrf(client)},
        follow_redirects=True,
    )
    assert act_v2.status_code == 200
    assert b"Deactivated previous active version(s): v1" in act_v2.data

    db_session.refresh(bom_v1)
    db_session.refresh(bom_v2)
    db_session.refresh(chikki_bom)
    assert bom_v1.is_active is False
    assert bom_v2.is_active is True
    assert chikki_bom.is_active is True

    # Audit logs recorded both deactivation of v1 and activation of v2
    v1_logs = db_session.query(AuditLog).filter_by(entity_type="bom", entity_id=bom_v1.id).all()
    v2_logs = db_session.query(AuditLog).filter_by(entity_type="bom", entity_id=bom_v2.id).all()
    assert any(log.action == "bom_deactivated" for log in v1_logs)
    assert any(log.action == "bom_activated" for log in v2_logs)

    # Deleting now-inactive unused v1 succeeds and writes an audit log
    del_v1 = client.post(
        f"/boms/{bom_v1.id}/delete",
        data={"csrf_token": _csrf(client)},
        follow_redirects=True,
    )
    assert del_v1.status_code == 200
    assert db_session.get(BOM, bom_v1.id) is None
    assert db_session.query(AuditLog).filter_by(entity_type="bom", entity_id=bom_v1.id, action="bom_deleted").count() == 1


def test_bom_version_cloning_and_historical_immutability_after_production_run(client, db_session):
    admin = _create_user(db_session, username="admin_hist", role=ROLE_ADMIN)
    seed = _seed_murukku_master_data(db_session)
    u = seed["units"]
    items = seed["items"]

    bom_v1, flour_line = _create_bom_with_component(
        db_session,
        finished_item=items["murukku"],
        material_item=items["flour"],
        input_unit=u["kg"],
        version=1,
        name="Murukku Historical v1",
        expected_yield_milli=200_000,
        qty_milli=40_000,
        is_active=True,
    )
    oil_line = BOMItem(
        bom_id=bom_v1.id,
        material_item_id=items["oil"].id,
        input_unit_id=u["L"].id,
        standard_qty_milli=10_000,
    )
    db_session.add(oil_line)
    db_session.flush()

    # Reference BOM v1 in a ProductionRun -> locks v1 historically
    run = ProductionRun(
        batch_number="BATCH-HIST-001",
        bom_id=bom_v1.id,
        finished_item_id=items["murukku"].id,
        run_date=date(2026, 11, 24),
        planned_output_milli=200_000,
        created_by=admin.id,
    )
    db_session.add(run)
    db_session.commit()

    _login(client, "admin_hist")

    # Detail view displays lock banner and hides edit controls
    detail_res = client.get(f"/boms/{bom_v1.id}")
    assert detail_res.status_code == 200
    assert b"Historical Lock" in detail_res.data
    assert b"Edit Header" not in detail_res.data
    assert b"Add Component" not in detail_res.data

    # All mutations on locked BOM v1 are blocked
    assert client.get(f"/boms/{bom_v1.id}/edit").status_code == 302

    edit_hdr = client.post(
        f"/boms/{bom_v1.id}/edit",
        data={
            "csrf_token": _csrf(client),
            "name": "Tampered Name",
            "expected_yield": "999",
        },
        follow_redirects=True,
    )
    assert b"referenced by production runs" in edit_hdr.data
    db_session.refresh(bom_v1)
    assert bom_v1.name == "Murukku Historical v1"
    assert bom_v1.expected_yield_milli == 200_000

    add_line = client.post(
        f"/boms/{bom_v1.id}/items",
        data={
            "csrf_token": _csrf(client),
            "material_item_id": str(items["spice_mix"].id),
            "standard_qty": "1.2",
            "input_unit_id": str(u["kg"].id),
        },
        follow_redirects=True,
    )
    assert b"referenced by production runs" in add_line.data
    assert db_session.query(BOMItem).filter_by(bom_id=bom_v1.id).count() == 2

    edit_line = client.post(
        f"/boms/{bom_v1.id}/items/{flour_line.id}/edit",
        data={
            "csrf_token": _csrf(client),
            "standard_qty": "99",
            "input_unit_id": str(u["kg"].id),
        },
        follow_redirects=True,
    )
    assert b"referenced by production runs" in edit_line.data
    db_session.refresh(flour_line)
    assert flour_line.standard_qty_milli == 40_000

    del_line = client.post(
        f"/boms/{bom_v1.id}/items/{oil_line.id}/delete",
        data={"csrf_token": _csrf(client)},
        follow_redirects=True,
    )
    assert b"referenced by production runs" in del_line.data
    assert db_session.get(BOMItem, oil_line.id) is not None

    del_bom = client.post(
        f"/boms/{bom_v1.id}/delete",
        data={"csrf_token": _csrf(client)},
        follow_redirects=True,
    )
    assert b"referenced by production runs" in del_bom.data
    assert db_session.get(BOM, bom_v1.id) is not None

    # Clone via New Version -> creates v2 (inactive), preserves v1 unchanged and active
    clone_res = client.post(
        f"/boms/{bom_v1.id}/new-version",
        data={"csrf_token": _csrf(client)},
        follow_redirects=True,
    )
    assert clone_res.status_code == 200
    assert b"Created BOM version v2 (inactive)" in clone_res.data

    bom_v2 = db_session.query(BOM).filter_by(finished_item_id=items["murukku"].id, version=2).one()
    db_session.refresh(bom_v1)
    assert bom_v1.is_active is True
    assert bom_v2.is_active is False
    assert bom_v2.name == bom_v1.name
    assert bom_v2.expected_yield_milli == bom_v1.expected_yield_milli
    assert len(bom_v2.items) == 2

    v1_pairs = {(i.material_item_id, i.input_unit_id, i.standard_qty_milli) for i in bom_v1.items}
    v2_pairs = {(i.material_item_id, i.input_unit_id, i.standard_qty_milli) for i in bom_v2.items}
    assert v1_pairs == v2_pairs

    # Edit cloned v2 (unlocked) and activate it -> v1 becomes inactive while remaining preserved
    v2_flour = db_session.query(BOMItem).filter_by(bom_id=bom_v2.id, material_item_id=items["flour"].id).one()
    client.post(
        f"/boms/{bom_v2.id}/items/{v2_flour.id}/edit",
        data={
            "csrf_token": _csrf(client),
            "standard_qty": "42",
            "input_unit_id": str(u["kg"].id),
        },
    )
    client.post(f"/boms/{bom_v2.id}/activate", data={"csrf_token": _csrf(client)})

    db_session.refresh(bom_v1)
    db_session.refresh(bom_v2)
    db_session.refresh(flour_line)
    db_session.refresh(v2_flour)
    assert bom_v1.is_active is False
    assert bom_v2.is_active is True
    assert flour_line.standard_qty_milli == 40_000
    assert v2_flour.standard_qty_milli == 42_000

    assert db_session.query(AuditLog).filter_by(entity_type="bom", entity_id=bom_v2.id, action="bom_version_cloned").count() == 1


# ---------------------------------------------------------------------------
# 5. Master-Data Referential Integrity Guards
# ---------------------------------------------------------------------------


def test_master_data_guards_protect_existing_boms_and_conversions(client, db_session):
    _create_user(db_session, username="admin_guard", role=ROLE_ADMIN)
    seed = _seed_murukku_master_data(db_session)
    u = seed["units"]
    items = seed["items"]

    # Add item-specific conversion for Pouches (1 pack = 100 piece) and a BOM using it
    conv = ItemUnitConversion(
        item_id=items["pouches"].id,
        from_unit_id=u["pack"].id,
        to_unit_id=u["piece"].id,
        multiplier=100,
    )
    # Also add a carton-stocked item using packet with 1 carton = 20 packet (10 packet = 0.5 carton)
    box_item = Item(
        sku="PKG-BOX-GUARD",
        name="Guard Boxes",
        item_type="packaging",
        stock_unit_id=u["carton"].id,
        is_active=True,
    )
    db_session.add_all([conv, box_item])
    db_session.flush()
    box_conv = ItemUnitConversion(
        item_id=box_item.id,
        from_unit_id=u["carton"].id,
        to_unit_id=u["packet"].id,
        multiplier=20,
    )
    db_session.add(box_conv)
    db_session.flush()

    bom, _ = _create_bom_with_component(
        db_session,
        finished_item=items["murukku"],
        material_item=items["pouches"],
        input_unit=u["pack"],
        version=1,
        name="Guard BOM",
        qty_milli=2000,
        is_active=True,
    )
    db_session.add_all(
        [
            BOMItem(
                bom_id=bom.id,
                material_item_id=box_item.id,
                input_unit_id=u["packet"].id,
                standard_qty_milli=10_000,  # 10 packet = 0.5 carton (exact for 20, non-exact for 30)
            ),
            BOMItem(
                bom_id=bom.id,
                material_item_id=items["spice_mix"].id,
                input_unit_id=u["g"].id,
                standard_qty_milli=1_200_000,  # 1200 g = 1.2 kg
            ),
        ]
    )
    db_session.commit()

    _login(client, "admin_guard")

    # 1. Cannot change finished product item_type or stock_unit_id while BOM exists
    bad_fg_edit = client.post(
        f"/master/items/{items['murukku'].id}/edit",
        data={
            "csrf_token": _csrf(client),
            "sku": items["murukku"].sku,
            "name": items["murukku"].name,
            "item_type": "raw_material",
            "stock_unit_id": str(u["piece"].id),
            "gst_rate": "5",
            "is_active": "1",
        },
    )
    assert bad_fg_edit.status_code == 200
    assert b"Cannot change item type while BOMs exist" in bad_fg_edit.data
    assert b"Cannot change stock unit while BOMs exist" in bad_fg_edit.data

    # 2. Cannot change BOM component item_type to finished_good or incompatible stock unit
    bad_mat_edit = client.post(
        f"/master/items/{items['spice_mix'].id}/edit",
        data={
            "csrf_token": _csrf(client),
            "sku": items["spice_mix"].sku,
            "name": items["spice_mix"].name,
            "item_type": "finished_good",
            "stock_unit_id": str(u["L"].id),
            "gst_rate": "5",
            "is_active": "1",
        },
    )
    assert bad_mat_edit.status_code == 200
    assert b"used as a BOM component" in bad_mat_edit.data
    assert b"depends on exact conversion" in bad_mat_edit.data

    # 3. Cannot delete ItemUnitConversion depended on by BOM
    del_conv = client.post(
        f"/master/items/{items['pouches'].id}/conversions/{conv.id}/delete",
        data={"csrf_token": _csrf(client)},
        follow_redirects=True,
    )
    assert del_conv.status_code == 200
    assert b"Cannot remove conversion" in del_conv.data
    assert db_session.get(ItemUnitConversion, conv.id) is not None

    # 4. Cannot update ItemUnitConversion multiplier if it makes an existing BOM non-exact (10 / 30 = 0.333...)
    upd_conv = client.post(
        f"/master/items/{box_item.id}/conversions/{box_conv.id}/edit",
        data={"csrf_token": _csrf(client), "multiplier": "30"},
        follow_redirects=True,
    )
    assert upd_conv.status_code == 200
    assert b"would lose exact conversion" in upd_conv.data
    db_session.refresh(box_conv)
    assert box_conv.multiplier == 20

    # 5. Cannot change Unit dimension or break Unit factor_to_base referenced by BOMItem
    bad_unit_dim = client.post(
        f"/master/units/{u['pack'].id}/edit",
        data={
            "csrf_token": _csrf(client),
            "code": u["pack"].code,
            "name": u["pack"].name,
            "dimension": "mass",
            "base_unit_id": "",
            "factor_to_base": "1",
        },
    )
    assert bad_unit_dim.status_code == 200
    assert b"Cannot change dimension" in bad_unit_dim.data

    bad_unit_factor = client.post(
        f"/master/units/{u['kg'].id}/edit",
        data={
            "csrf_token": _csrf(client),
            "code": u["kg"].code,
            "name": u["kg"].name,
            "dimension": "mass",
            "base_unit_id": str(u["g"].id),
            "factor_to_base": "700",  # 1,200,000 / 700 is non-exact
        },
    )
    assert bad_unit_factor.status_code == 200
    assert b"Cannot change unit ratio" in bad_unit_factor.data
    db_session.refresh(u["kg"])
    assert u["kg"].factor_to_base == 1000


# ---------------------------------------------------------------------------
# 6. RBAC, Unauthenticated Redirects, Navbar Visibility & CSRF Protection
# ---------------------------------------------------------------------------


def test_unauthenticated_bom_routes_redirect_to_login(client, db_session):
    seed = _seed_murukku_master_data(db_session)
    bom, line = _create_bom_with_component(
        db_session,
        finished_item=seed["items"]["murukku"],
        material_item=seed["items"]["flour"],
        input_unit=seed["units"]["kg"],
    )

    for path in ("/boms", "/boms/new", f"/boms/{bom.id}", f"/boms/{bom.id}/edit"):
        res = client.get(path)
        assert res.status_code == 302
        assert "/login" in res.headers["Location"]

    for path in (
        "/boms/new",
        f"/boms/{bom.id}/edit",
        f"/boms/{bom.id}/activate",
        f"/boms/{bom.id}/deactivate",
        f"/boms/{bom.id}/new-version",
        f"/boms/{bom.id}/delete",
        f"/boms/{bom.id}/items",
        f"/boms/{bom.id}/items/{line.id}/edit",
        f"/boms/{bom.id}/items/{line.id}/delete",
    ):
        res = client.post(path)
        assert res.status_code == 302
        assert "/login" in res.headers["Location"]


@pytest.mark.parametrize(
    "role,can_read,can_write",
    [
        (ROLE_SALESMAN, False, False),
        (ROLE_PRODUCTION_SUPERVISOR, True, False),
        (ROLE_ACCOUNTANT, True, False),
        (ROLE_ADMIN, True, True),
        (ROLE_OWNER, True, True),
    ],
)
def test_bom_rbac_read_write_and_navbar_visibility(client, db_session, role, can_read, can_write):
    _create_user(db_session, username=f"bom_{role}", role=role)
    seed = _seed_murukku_master_data(db_session)
    bom, line = _create_bom_with_component(
        db_session,
        finished_item=seed["items"]["murukku"],
        material_item=seed["items"]["flour"],
        input_unit=seed["units"]["kg"],
    )
    _login(client, f"bom_{role}")

    # Navbar link visibility
    home_html = client.get("/").data
    assert (b'href="/boms"' in home_html) is can_read

    # Read routes
    list_res = client.get("/boms")
    view_res = client.get(f"/boms/{bom.id}")
    assert list_res.status_code == (200 if can_read else 403)
    assert view_res.status_code == (200 if can_read else 403)

    if can_read:
        assert (b"+ New BOM" in list_res.data) is can_write
        assert (b"Add Component" in view_res.data) is can_write

    # Write GET routes
    for path in ("/boms/new", f"/boms/{bom.id}/edit"):
        assert client.get(path).status_code == (200 if can_write else 403)

    # Write POST routes for non-writers must return 403
    if not can_write:
        for path in (
            "/boms/new",
            f"/boms/{bom.id}/edit",
            f"/boms/{bom.id}/activate",
            f"/boms/{bom.id}/deactivate",
            f"/boms/{bom.id}/new-version",
            f"/boms/{bom.id}/delete",
            f"/boms/{bom.id}/items",
            f"/boms/{bom.id}/items/{line.id}/edit",
            f"/boms/{bom.id}/items/{line.id}/delete",
        ):
            res = client.post(path, data={"csrf_token": _csrf(client)})
            assert res.status_code == 403, f"Expected 403 on POST {path} for {role}"


def test_bom_post_routes_enforce_csrf_protection(client, db_session):
    _create_user(db_session, username="admin_csrf", role=ROLE_ADMIN)
    seed = _seed_murukku_master_data(db_session)
    bom, line = _create_bom_with_component(
        db_session,
        finished_item=seed["items"]["murukku"],
        material_item=seed["items"]["flour"],
        input_unit=seed["units"]["kg"],
    )
    _login(client, "admin_csrf")

    post_paths = [
        "/boms/new",
        f"/boms/{bom.id}/edit",
        f"/boms/{bom.id}/activate",
        f"/boms/{bom.id}/deactivate",
        f"/boms/{bom.id}/new-version",
        f"/boms/{bom.id}/delete",
        f"/boms/{bom.id}/items",
        f"/boms/{bom.id}/items/{line.id}/edit",
        f"/boms/{bom.id}/items/{line.id}/delete",
    ]
    for path in post_paths:
        missing_res = client.post(path, data={})
        assert missing_res.status_code == 400, f"Expected 400 (missing CSRF) on {path}"
        bad_res = client.post(path, data={"csrf_token": "invalid-token"})
        assert bad_res.status_code == 400, f"Expected 400 (invalid CSRF) on {path}"


# ---------------------------------------------------------------------------
# 7. Hardening Tests: Conflict Paths, Inactive Components, Locked Conversions & Rollback
# ---------------------------------------------------------------------------


def test_master_data_routes_reject_conflicting_conversion_paths_and_bom_detail_handles_conflicts(
    client, db_session
):
    _create_user(db_session, username="admin_conflict", role=ROLE_ADMIN)
    seed = _seed_murukku_master_data(db_session)
    u = seed["units"]
    items = seed["items"]
    pouches = items["pouches"]
    _login(client, "admin_conflict")

    # Add two consistent conversions: 1 pack = 10 piece, 1 carton = 2 pack (implies 1 carton = 20 piece)
    res1 = client.post(
        f"/master/items/{pouches.id}/conversions",
        data={
            "csrf_token": _csrf(client),
            "from_unit_id": str(u["pack"].id),
            "to_unit_id": str(u["piece"].id),
            "multiplier": "10",
        },
        follow_redirects=True,
    )
    assert res1.status_code == 200

    res2 = client.post(
        f"/master/items/{pouches.id}/conversions",
        data={
            "csrf_token": _csrf(client),
            "from_unit_id": str(u["carton"].id),
            "to_unit_id": str(u["pack"].id),
            "multiplier": "2",
        },
        follow_redirects=True,
    )
    assert res2.status_code == 200
    assert db_session.query(ItemUnitConversion).filter_by(item_id=pouches.id).count() == 2

    # Attempt conflicting conversion via item detail route: 1 carton = 25 piece (!= 20)
    bad_item_conv = client.post(
        f"/master/items/{pouches.id}/conversions",
        data={
            "csrf_token": _csrf(client),
            "from_unit_id": str(u["carton"].id),
            "to_unit_id": str(u["piece"].id),
            "multiplier": "25",
        },
    )
    assert bad_item_conv.status_code == 200
    assert b"Conflicting unit conversion definitions" in bad_item_conv.data
    assert db_session.query(ItemUnitConversion).filter_by(item_id=pouches.id).count() == 2

    # Attempt conflicting conversion via standalone /master/conversions/new route
    bad_standalone = client.post(
        "/master/conversions/new",
        data={
            "csrf_token": _csrf(client),
            "item_id": str(pouches.id),
            "from_unit_id": str(u["carton"].id),
            "to_unit_id": str(u["piece"].id),
            "multiplier": "25",
        },
    )
    assert bad_standalone.status_code == 200
    assert b"Conflicting unit conversion definitions" in bad_standalone.data
    assert db_session.query(ItemUnitConversion).filter_by(item_id=pouches.id).count() == 2

    # Consistent redundant conversion (1 carton = 20 piece) is allowed
    ok_consistent = client.post(
        f"/master/items/{pouches.id}/conversions",
        data={
            "csrf_token": _csrf(client),
            "from_unit_id": str(u["carton"].id),
            "to_unit_id": str(u["piece"].id),
            "multiplier": "20",
        },
        follow_redirects=True,
    )
    assert ok_consistent.status_code == 200
    assert b"Item unit conversion added." in ok_consistent.data
    carton_piece_conv = (
        db_session.query(ItemUnitConversion)
        .filter_by(item_id=pouches.id, from_unit_id=u["carton"].id, to_unit_id=u["piece"].id)
        .one()
    )

    # Editing 1 carton = 20 piece -> 25 creates a conflict -> rejected and rolled back
    bad_edit = client.post(
        f"/master/items/{pouches.id}/conversions/{carton_piece_conv.id}/edit",
        data={"csrf_token": _csrf(client), "multiplier": "25"},
        follow_redirects=True,
    )
    assert bad_edit.status_code == 200
    assert b"Conflicting unit conversion definitions" in bad_edit.data
    db_session.refresh(carton_piece_conv)
    assert carton_piece_conv.multiplier == 20

    # Even if conflicting rows somehow exist directly in DB, BOM detail renders gracefully (no 500)
    bom, _ = _create_bom_with_component(
        db_session,
        finished_item=items["murukku"],
        material_item=pouches,
        input_unit=u["carton"],
        version=1,
        name="Conflict Display BOM",
        qty_milli=1000,
        is_active=False,
    )
    carton_piece_conv.multiplier = 25  # Direct DB tamper bypassing route validation
    db_session.commit()

    detail_res = client.get(f"/boms/{bom.id}")
    assert detail_res.status_code == 200
    assert b"Conflicting unit conversion definitions" in detail_res.data


def test_bom_activation_rejected_when_any_component_is_inactive(client, db_session):
    _create_user(db_session, username="admin_inact_comp", role=ROLE_ADMIN)
    seed = _seed_murukku_master_data(db_session)
    u = seed["units"]
    items = seed["items"]

    bom, _ = _create_bom_with_component(
        db_session,
        finished_item=items["murukku"],
        material_item=items["flour"],
        input_unit=u["kg"],
        version=1,
        name="Murukku Component Active Check",
        qty_milli=40_000,
        is_active=False,
    )
    db_session.add(
        BOMItem(
            bom_id=bom.id,
            material_item_id=items["oil"].id,
            input_unit_id=u["L"].id,
            standard_qty_milli=10_000,
        )
    )
    db_session.commit()

    _login(client, "admin_inact_comp")

    # Deactivate Oil via Master Data
    deact_res = client.post(
        f"/master/items/{items['oil'].id}/toggle-active",
        data={"csrf_token": _csrf(client)},
        follow_redirects=True,
    )
    assert deact_res.status_code == 200
    db_session.refresh(items["oil"])
    assert items["oil"].is_active is False

    # Attempt to activate BOM -> rejected naming Oil (RM-OIL)
    act_res = client.post(
        f"/boms/{bom.id}/activate",
        data={"csrf_token": _csrf(client)},
        follow_redirects=True,
    )
    assert act_res.status_code == 200
    assert b"component item(s) are inactive" in act_res.data
    assert b"Oil (RM-OIL)" in act_res.data

    db_session.refresh(bom)
    assert bom.is_active is False
    assert (
        db_session.query(AuditLog)
        .filter_by(entity_type="bom", entity_id=bom.id, action="bom_activated")
        .count()
        == 0
    )

    # Re-activate Oil -> BOM activation now succeeds
    client.post(
        f"/master/items/{items['oil'].id}/toggle-active",
        data={"csrf_token": _csrf(client)},
    )
    act_ok = client.post(
        f"/boms/{bom.id}/activate",
        data={"csrf_token": _csrf(client)},
        follow_redirects=True,
    )
    assert act_ok.status_code == 200
    db_session.refresh(bom)
    assert bom.is_active is True


def test_historical_bom_locks_item_and_global_unit_conversions_while_unlocked_boms_remain_editable(
    client, db_session
):
    admin = _create_user(db_session, username="admin_conv_lock", role=ROLE_ADMIN)
    seed = _seed_murukku_master_data(db_session)
    u = seed["units"]
    items = seed["items"]

    # Item-specific conversion used by BOM (pack -> piece) plus an unrelated conversion (case -> carton)
    used_conv = ItemUnitConversion(
        item_id=items["pouches"].id,
        from_unit_id=u["pack"].id,
        to_unit_id=u["piece"].id,
        multiplier=100,
    )
    unrelated_conv = ItemUnitConversion(
        item_id=items["pouches"].id,
        from_unit_id=u["case"].id,
        to_unit_id=u["carton"].id,
        multiplier=5,
    )
    # Intermediate mass unit (hg -> g = 100, and custom tonne -> hg = 10000) to test indirect global lock
    hg = Unit(code="hg", name="Hectogram", dimension="mass", base_unit_id=u["g"].id, factor_to_base=100)
    db_session.add_all([used_conv, unrelated_conv, hg])
    db_session.flush()
    quintal = Unit(code="qtl", name="Quintal", dimension="mass", base_unit_id=hg.id, factor_to_base=1000)
    db_session.add(quintal)
    db_session.flush()

    bom, _ = _create_bom_with_component(
        db_session,
        finished_item=items["murukku"],
        material_item=items["pouches"],
        input_unit=u["pack"],
        version=1,
        name="Conversion Lock BOM",
        qty_milli=2000,
        is_active=True,
    )
    db_session.add(
        BOMItem(
            bom_id=bom.id,
            material_item_id=items["spice_mix"].id,
            input_unit_id=u["g"].id,
            standard_qty_milli=1_200_000,  # 1200 g
        )
    )
    db_session.add(
        BOMItem(
            bom_id=bom.id,
            material_item_id=items["flour"].id,
            input_unit_id=quintal.id,
            standard_qty_milli=1000,  # 1 qtl -> 1000 hg -> 100,000 g -> 100 kg
        )
    )
    db_session.commit()

    _login(client, "admin_conv_lock")

    # 1. While BOM is UNLOCKED, exact edits to item-specific and global conversions are still allowed
    unlocked_item_edit = client.post(
        f"/master/items/{items['pouches'].id}/conversions/{used_conv.id}/edit",
        data={"csrf_token": _csrf(client), "multiplier": "120"},
        follow_redirects=True,
    )
    assert unlocked_item_edit.status_code == 200
    assert b"Item unit conversion updated." in unlocked_item_edit.data
    db_session.refresh(used_conv)
    assert used_conv.multiplier == 120

    unlocked_unit_edit = client.post(
        f"/master/units/{u['kg'].id}/edit",
        data={
            "csrf_token": _csrf(client),
            "code": "kg",
            "name": "Kilogram",
            "dimension": "mass",
            "base_unit_id": str(u["g"].id),
            "factor_to_base": "500",  # Exact for 1200 g (2.4 kg) and 1 qtl (200 kg)
        },
        follow_redirects=True,
    )
    assert unlocked_unit_edit.status_code == 200
    assert b"Unit &#39;kg&#39; updated." in unlocked_unit_edit.data or b"Unit 'kg' updated." in unlocked_unit_edit.data
    db_session.refresh(u["kg"])
    assert u["kg"].factor_to_base == 500

    # Restore kg to 1000 before locking
    client.post(
        f"/master/units/{u['kg'].id}/edit",
        data={
            "csrf_token": _csrf(client),
            "code": "kg",
            "name": "Kilogram",
            "dimension": "mass",
            "base_unit_id": str(u["g"].id),
            "factor_to_base": "1000",
        },
    )
    db_session.refresh(u["kg"])
    assert u["kg"].factor_to_base == 1000

    # 2. Lock BOM with a ProductionRun
    run = ProductionRun(
        batch_number="BATCH-CONV-LOCK-001",
        bom_id=bom.id,
        finished_item_id=items["murukku"].id,
        run_date=date(2026, 11, 24),
        planned_output_milli=200_000,
        created_by=admin.id,
    )
    db_session.add(run)
    db_session.commit()

    # Editing item-specific conversion used by locked BOM is blocked even if exact
    locked_item_edit = client.post(
        f"/master/items/{items['pouches'].id}/conversions/{used_conv.id}/edit",
        data={"csrf_token": _csrf(client), "multiplier": "150"},
        follow_redirects=True,
    )
    assert locked_item_edit.status_code == 200
    assert b"locked by production runs" in locked_item_edit.data
    db_session.refresh(used_conv)
    assert used_conv.multiplier == 120

    # Unrelated conversion on the same item is not falsely blocked
    unrelated_edit = client.post(
        f"/master/items/{items['pouches'].id}/conversions/{unrelated_conv.id}/edit",
        data={"csrf_token": _csrf(client), "multiplier": "6"},
        follow_redirects=True,
    )
    assert unrelated_edit.status_code == 200
    assert b"Item unit conversion updated." in unrelated_edit.data
    db_session.refresh(unrelated_conv)
    assert unrelated_conv.multiplier == 6

    # Editing directly referenced global unit factor (kg) is blocked even if exact
    locked_unit_direct = client.post(
        f"/master/units/{u['kg'].id}/edit",
        data={
            "csrf_token": _csrf(client),
            "code": "kg",
            "name": "Kilogram",
            "dimension": "mass",
            "base_unit_id": str(u["g"].id),
            "factor_to_base": "500",
        },
    )
    assert locked_unit_direct.status_code == 200
    assert b"locked by production runs" in locked_unit_direct.data
    db_session.refresh(u["kg"])
    assert u["kg"].factor_to_base == 1000

    # Editing indirect intermediate global unit factor (hg, used via qtl -> hg -> g -> kg) is also blocked
    locked_unit_indirect = client.post(
        f"/master/units/{hg.id}/edit",
        data={
            "csrf_token": _csrf(client),
            "code": "hg",
            "name": "Hectogram",
            "dimension": "mass",
            "base_unit_id": str(u["g"].id),
            "factor_to_base": "200",
        },
    )
    assert locked_unit_indirect.status_code == 200
    assert b"locked by production runs" in locked_unit_indirect.data
    db_session.refresh(hg)
    assert hg.factor_to_base == 100


def test_bom_transaction_rollback_prevents_partial_commits_and_orphan_audit_logs(
    client, db_session, monkeypatch
):
    _create_user(db_session, username="admin_rb", role=ROLE_ADMIN)
    seed = _seed_murukku_master_data(db_session)
    u = seed["units"]
    items = seed["items"]

    bom, flour_line = _create_bom_with_component(
        db_session,
        finished_item=items["murukku"],
        material_item=items["flour"],
        input_unit=u["kg"],
        version=1,
        name="Rollback BOM",
        qty_milli=40_000,
        is_active=True,
    )
    oil_line = BOMItem(
        bom_id=bom.id,
        material_item_id=items["oil"].id,
        input_unit_id=u["L"].id,
        standard_qty_milli=10_000,
    )
    db_session.add(oil_line)
    db_session.commit()

    _login(client, "admin_rb")
    csrf_token = _csrf(client)
    audit_count_before = db_session.query(AuditLog).count()

    def _boom():
        raise OperationalError("COMMIT", {}, Exception("simulated commit failure"))

    # 1. Rollback on deactivate_bom failure
    monkeypatch.setattr(db.session, "commit", _boom)
    deact_res = client.post(
        f"/boms/{bom.id}/deactivate",
        data={"csrf_token": csrf_token},
        follow_redirects=True,
    )
    assert deact_res.status_code == 200
    assert b"Failed to deactivate BOM. No changes were saved." in deact_res.data
    db_session.expire_all()
    assert db_session.get(BOM, bom.id).is_active is True
    assert db_session.query(AuditLog).count() == audit_count_before

    # 2. Rollback on delete_bom_item failure
    del_line_res = client.post(
        f"/boms/{bom.id}/items/{oil_line.id}/delete",
        data={"csrf_token": csrf_token},
        follow_redirects=True,
    )
    assert del_line_res.status_code == 200
    assert b"Failed to remove component" in del_line_res.data
    assert b"No changes were saved." in del_line_res.data
    db_session.expire_all()
    assert db_session.get(BOMItem, oil_line.id) is not None
    assert db_session.query(AuditLog).count() == audit_count_before

    # 3. Rollback on activate_bom failure (deactivating existing active BOM must also roll back)
    monkeypatch.undo()
    bom_v2, _ = _create_bom_with_component(
        db_session,
        finished_item=items["murukku"],
        material_item=items["flour"],
        input_unit=u["kg"],
        version=2,
        name="Rollback BOM v2",
        qty_milli=45_000,
        is_active=False,
    )
    audit_count_before = db_session.query(AuditLog).count()

    monkeypatch.setattr(db.session, "commit", _boom)
    act_res = client.post(
        f"/boms/{bom_v2.id}/activate",
        data={"csrf_token": csrf_token},
        follow_redirects=True,
    )
    assert act_res.status_code == 200
    assert b"Failed to activate BOM. No changes were saved." in act_res.data
    db_session.expire_all()
    assert db_session.get(BOM, bom.id).is_active is True
    assert db_session.get(BOM, bom_v2.id).is_active is False
    assert db_session.query(AuditLog).count() == audit_count_before


def test_component_item_deactivation_blocked_for_active_bom_and_allowed_for_inactive_bom(
    client, db_session
):
    _create_user(db_session, username="admin_deact_guard", role=ROLE_ADMIN)
    seed = _seed_murukku_master_data(db_session)
    u = seed["units"]
    items = seed["items"]
    flour = items["flour"]

    active_bom, _ = _create_bom_with_component(
        db_session,
        finished_item=items["murukku"],
        material_item=flour,
        input_unit=u["kg"],
        version=1,
        name="Murukku Active Recipe",
        qty_milli=40_000,
        is_active=True,
    )
    _create_bom_with_component(
        db_session,
        finished_item=items["chikki"],
        material_item=flour,
        input_unit=u["kg"],
        version=1,
        name="Chikki Inactive Recipe",
        qty_milli=10_000,
        is_active=False,
    )

    _login(client, "admin_deact_guard")
    csrf_token = _csrf(client)
    audit_count_before = db_session.query(AuditLog).count()

    # 1. Deactivation via toggle-active is blocked while referenced by an active BOM
    toggle_blocked = client.post(
        f"/master/items/{flour.id}/toggle-active",
        data={"csrf_token": csrf_token},
        follow_redirects=True,
    )
    assert toggle_blocked.status_code == 200
    assert b"Cannot deactivate item" in toggle_blocked.data
    assert b"Murukku Active Recipe (v1)" in toggle_blocked.data
    assert b"Chikki Inactive Recipe" not in toggle_blocked.data
    db_session.refresh(flour)
    assert flour.is_active is True
    assert db_session.query(AuditLog).count() == audit_count_before

    # 2. Deactivation via edit-item (unchecking is_active) is also blocked
    edit_blocked = client.post(
        f"/master/items/{flour.id}/edit",
        data={
            "csrf_token": csrf_token,
            "sku": flour.sku,
            "name": flour.name,
            "item_type": flour.item_type,
            "stock_unit_id": str(flour.stock_unit_id),
            "gst_rate": "0",
        },
    )
    assert edit_blocked.status_code == 200
    assert b"Cannot deactivate item" in edit_blocked.data
    assert b"Murukku Active Recipe (v1)" in edit_blocked.data
    db_session.refresh(flour)
    assert flour.is_active is True
    assert db_session.query(AuditLog).count() == audit_count_before

    # 3. Deactivate the active BOM -> now Flour is only in inactive BOMs, so deactivation succeeds
    client.post(
        f"/boms/{active_bom.id}/deactivate",
        data={"csrf_token": csrf_token},
    )
    audit_after_bom_deact = db_session.query(AuditLog).count()

    deact_ok = client.post(
        f"/master/items/{flour.id}/toggle-active",
        data={"csrf_token": csrf_token},
        follow_redirects=True,
    )
    assert deact_ok.status_code == 200
    assert b"deactivated." in deact_ok.data
    db_session.refresh(flour)
    assert flour.is_active is False
    assert db_session.query(AuditLog).count() == audit_after_bom_deact + 1
    assert (
        db_session.query(AuditLog)
        .filter_by(entity_type="item", entity_id=flour.id, action="item_deactivated")
        .count()
        == 1
    )

    # 4. Reactivation works normally
    react_ok = client.post(
        f"/master/items/{flour.id}/toggle-active",
        data={"csrf_token": csrf_token},
        follow_redirects=True,
    )
    assert react_ok.status_code == 200
    assert b"activated." in react_ok.data
    db_session.refresh(flour)
    assert flour.is_active is True
    assert (
        db_session.query(AuditLog)
        .filter_by(entity_type="item", entity_id=flour.id, action="item_activated")
        .count()
        == 1
    )


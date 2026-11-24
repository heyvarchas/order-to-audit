"""Step A tests: Money utilities, Stock Ledger foundation, WAC, Opening Stock, RBAC & UI."""

from datetime import date
from decimal import Decimal
import json

import pytest
from sqlalchemy.exc import IntegrityError

from app.csrf import CSRF_SESSION_KEY
from app.extensions import db
from app.inventory_service import (
    InsufficientStockError,
    InvalidMovementError,
    UnauthorizedInventoryActionError,
    financial_year_for_date,
    get_all_stock_positions,
    get_item_stock_position,
    post_ledger_reversal,
    post_stock_issue,
    post_stock_receipt,
    record_opening_stock,
)
from app.models import (
    AuditLog,
    Item,
    ItemUnitConversion,
    Purchase,
    PurchaseItem,
    StockAdjustment,
    StockLedger,
    Supplier,
    Unit,
    User,
)
from app.money import (
    divide_round_half_up,
    format_exact_rupees,
    format_paise_as_rupees,
    parse_rupees_to_paise,
    round_decimal_half_up,
)
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


def _seed_benchmark_master_data(db_session):
    """Seed all units and items for the benchmark food-manufacturer scenario."""
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
    db_session.flush()

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
        name="Spice Mix",
        item_type="raw_material",
        stock_unit_id=kg.id,
        gst_rate_bps=500,
        is_active=True,
    )
    pouches = Item(
        sku="PKG-POUCH-200G",
        name="200g Pouches",
        item_type="packaging",
        stock_unit_id=piece.id,
        gst_rate_bps=1800,
        is_active=True,
    )
    cartons = Item(
        sku="PKG-CARTON",
        name="Cartons",
        item_type="packaging",
        stock_unit_id=piece.id,
        gst_rate_bps=1800,
        is_active=True,
    )
    murukku = Item(
        sku="FG-MURUKKU-200G",
        name="Finished Murukku",
        item_type="finished_good",
        stock_unit_id=packet.id,
        hsn_code="21069099",
        gst_rate_bps=500,
        default_sale_price_paise=5000,
        is_active=True,
    )
    supplier = Supplier(
        name="Grain Mill Supplier",
        state="Tamil Nadu",
        is_active=True,
    )
    db_session.add_all([flour, oil, spice_mix, pouches, cartons, murukku, supplier])
    db_session.flush()

    # 1 carton = 20 packets for Finished Murukku
    conv = ItemUnitConversion(
        item_id=murukku.id,
        from_unit_id=carton.id,
        to_unit_id=packet.id,
        multiplier=20,
    )
    db_session.add(conv)
    db_session.commit()

    return {
        "units": {
            "g": g,
            "kg": kg,
            "ml": ml,
            "L": litre,
            "piece": piece,
            "packet": packet,
            "carton": carton,
        },
        "items": {
            "flour": flour,
            "oil": oil,
            "spice_mix": spice_mix,
            "pouches": pouches,
            "cartons": cartons,
            "murukku": murukku,
        },
        "supplier": supplier,
    }


def _make_source_purchase(
    db_session,
    *,
    supplier_id: int,
    user_id: int,
    number: str,
    dt: date,
    item: Item,
    qty_milli: int,
    total_value_paise: int,
):
    unit_rate_paise = divide_round_half_up(total_value_paise * 1000, qty_milli)
    purchase = Purchase(
        purchase_number=number,
        supplier_id=supplier_id,
        purchase_date=dt,
        subtotal_paise=total_value_paise,
        total_paise=total_value_paise,
        created_by=user_id,
    )
    db_session.add(purchase)
    db_session.flush()

    line = PurchaseItem(
        purchase_id=purchase.id,
        item_id=item.id,
        entered_unit_id=item.stock_unit_id,
        entered_qty_milli=qty_milli,
        qty_stock_milli=qty_milli,
        unit_rate_paise=unit_rate_paise,
        taxable_value_paise=total_value_paise,
        line_total_paise=total_value_paise,
    )
    db_session.add(line)
    db_session.flush()

    purchase.status = "posted"
    db_session.flush()
    return purchase


def _make_source_adjustment(
    db_session, *, user_id: int, number: str, adj_type: str, dt: date, reason: str = "Test adj"
):
    adj = StockAdjustment(
        adjustment_number=number,
        adjustment_type=adj_type,
        adjustment_date=dt,
        reason=reason,
        created_by=user_id,
    )
    db_session.add(adj)
    db_session.flush()
    return adj


# ---------------------------------------------------------------------------
# 0. Shared Money Utilities
# ---------------------------------------------------------------------------


def test_money_utilities_exact_decimal_and_round_half_up():
    assert parse_rupees_to_paise("45.00") == (4500, None)
    assert parse_rupees_to_paise("0") == (0, None)
    assert parse_rupees_to_paise("0", allow_zero=False)[0] is None
    assert parse_rupees_to_paise("-1")[0] is None
    assert parse_rupees_to_paise("12.345")[0] is None
    assert parse_rupees_to_paise("NaN")[0] is None

    assert format_paise_as_rupees(4500) == "45.00"
    assert format_paise_as_rupees(-172500) == "-1725.00"
    assert format_paise_as_rupees(0) == "0.00"
    assert format_paise_as_rupees(None) == ""

    assert format_exact_rupees(Decimal("43.125")) == "43.125"
    assert format_exact_rupees(Decimal("42")) == "42.00"
    assert format_exact_rupees(Decimal("33.33333333")) == "33.3333"

    # ROUND_HALF_UP (half away from zero)
    assert divide_round_half_up(5, 2) == 3
    assert divide_round_half_up(3, 2) == 2
    assert divide_round_half_up(-5, 2) == -3
    assert divide_round_half_up(690000 * 40000, 160000) == 172500
    assert divide_round_half_up(690000 * 1000, 160000) == 4313
    assert round_decimal_half_up(Decimal("2.5")) == 3
    assert round_decimal_half_up(Decimal("3.5")) == 4

    with pytest.raises(ValueError):
        divide_round_half_up(10, 0)
    with pytest.raises(TypeError):
        divide_round_half_up(True, 2)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 1. Empty Stock Position
# ---------------------------------------------------------------------------


def test_empty_stock_position_returns_zero_qty_zero_value_and_none_wac(db_session):
    data = _seed_benchmark_master_data(db_session)
    flour = data["items"]["flour"]

    pos = get_item_stock_position(flour.id)
    assert pos.item_id == flour.id
    assert pos.qty_milli == 0
    assert pos.value_paise == 0
    assert pos.wac_rupees is None
    assert pos.wac_paise_exact is None
    assert pos.unit_cost_paise is None

    all_positions = get_all_stock_positions()
    assert len(all_positions) == 6
    assert all(p.qty_milli == 0 and p.value_paise == 0 and p.wac_rupees is None for p in all_positions)


# ---------------------------------------------------------------------------
# 2. Opening Stock for All Six Benchmark Items + FY Numbering + Zero Cost
# ---------------------------------------------------------------------------


def test_opening_stock_for_all_six_benchmark_items_and_fy_numbering(db_session):
    admin = _create_user(db_session, username="admin_user", role=ROLE_ADMIN)
    data = _seed_benchmark_master_data(db_session)
    items = data["items"]
    dt = date(2026, 11, 24)

    assert financial_year_for_date(date(2026, 3, 31)) == "2025-26"
    assert financial_year_for_date(date(2026, 4, 1)) == "2026-27"
    assert financial_year_for_date(dt) == "2026-27"

    benchmark_openings = [
        (items["flour"], 100_000, 4200, 420_000),      # 100 kg @ ₹42.00
        (items["oil"], 30_000, 14000, 420_000),        # 30 L @ ₹140.00
        (items["spice_mix"], 5_000, 30000, 150_000),   # 5 kg @ ₹300.00
        (items["pouches"], 500_000, 250, 125_000),     # 500 pieces @ ₹2.50
        (items["cartons"], 30_000, 0, 0),              # 30 pieces @ ₹0.00 (zero cost allowed)
        (items["murukku"], 50_000, 3000, 150_000),     # 50 packets @ ₹30.00
    ]

    for idx, (item, qty_milli, unit_cost_paise, expected_val) in enumerate(benchmark_openings, start=1):
        adj, entry = record_opening_stock(
            item_id=item.id,
            qty_milli=qty_milli,
            unit_cost_paise=unit_cost_paise,
            movement_date=dt,
            created_by=admin.id,
            reason=f"Opening stock for {item.name}",
        )
        assert adj.adjustment_type == "opening_stock"
        assert adj.adjustment_number == f"ADJ/2026-27/{idx:04d}"
        assert adj.approved_by == admin.id
        assert entry.movement_type == "opening_stock"
        assert entry.source_doc_type == "stock_adjustment"
        assert entry.source_doc_id == adj.id
        assert entry.qty_change_milli == qty_milli
        assert entry.unit_cost_paise == unit_cost_paise
        assert entry.value_change_paise == expected_val

        # Verify audit log created atomically for each opening stock document.
        audit = db_session.execute(
            db.select(AuditLog).where(
                AuditLog.entity_type == "stock_adjustment",
                AuditLog.entity_id == adj.id,
            )
        ).scalar_one()
        assert audit.action == "opening_stock_recorded"
        assert audit.user_id == admin.id
        payload = json.loads(audit.new_values_json)
        assert payload["qty_stock_milli"] == qty_milli
        assert payload["value_change_paise"] == expected_val

    positions = {p.item_id: p for p in get_all_stock_positions()}
    assert positions[items["flour"].id].qty_milli == 100_000
    assert positions[items["oil"].id].qty_milli == 30_000
    assert positions[items["spice_mix"].id].qty_milli == 5_000
    assert positions[items["pouches"].id].qty_milli == 500_000
    assert positions[items["cartons"].id].qty_milli == 30_000
    assert positions[items["cartons"].id].value_paise == 0
    assert positions[items["cartons"].id].wac_rupees == Decimal("0")
    assert positions[items["murukku"].id].qty_milli == 50_000


# ---------------------------------------------------------------------------
# 3. Unit Conversion During Opening Stock Entry
# ---------------------------------------------------------------------------


def test_opening_stock_converts_entered_units_to_stock_units(db_session):
    owner = _create_user(db_session, username="owner_user", role=ROLE_OWNER)
    data = _seed_benchmark_master_data(db_session)
    units = data["units"]
    items = data["items"]
    dt = date(2026, 11, 24)

    # 5,000 g entered for Spice Mix stocked in kg -> 5 kg (5,000 milli kg)
    _adj1, entry1 = record_opening_stock(
        item_id=items["spice_mix"].id,
        qty_milli=5_000_000,  # 5,000.000 g
        input_unit_id=units["g"].id,
        unit_cost_paise=30_000,  # ₹300.00 per kg (stock unit)
        movement_date=dt,
        created_by=owner.id,
        reason="Weighed in grams",
    )
    assert entry1.qty_change_milli == 5_000
    assert entry1.value_change_paise == 150_000

    # 2 cartons entered for Finished Murukku (1 carton = 20 packets) -> 40 packets
    _adj2, entry2 = record_opening_stock(
        item_id=items["murukku"].id,
        qty_milli=2_000,  # 2 cartons
        input_unit_id=units["carton"].id,
        unit_cost_paise=3_000,  # ₹30.00 per packet
        movement_date=dt,
        created_by=owner.id,
        reason="Counted in cartons",
    )
    assert entry2.qty_change_milli == 40_000
    assert entry2.value_change_paise == 120_000

    # Incompatible dimension (ml for kg flour) must be rejected cleanly
    with pytest.raises(InvalidMovementError, match="incompatible"):
        record_opening_stock(
            item_id=items["flour"].id,
            qty_milli=10_000,
            input_unit_id=units["ml"].id,
            unit_cost_paise=4200,
            movement_date=dt,
            created_by=owner.id,
        )


# ---------------------------------------------------------------------------
# 4, 5, 6, 7. Exact Aggregation, Primitives, Unrounded WAC Issue & Full Depletion
# ---------------------------------------------------------------------------


def test_receipt_issue_primitives_unrounded_wac_and_no_internal_commit(db_session):
    admin = _create_user(db_session, username="admin_wac", role=ROLE_ADMIN)
    data = _seed_benchmark_master_data(db_session)
    flour = data["items"]["flour"]
    dt = date(2026, 11, 24)

    # Opening: 100 kg @ ₹42.00 = ₹4,200.00
    record_opening_stock(
        item_id=flour.id,
        qty_milli=100_000,
        unit_cost_paise=4200,
        movement_date=dt,
        created_by=admin.id,
    )

    # Receipt primitive does NOT commit internally: verify rollback discards it.
    temp_pur = _make_source_purchase(
        db_session,
        supplier_id=data["supplier"].id,
        user_id=admin.id,
        number="PUR-TMP",
        dt=dt,
        item=flour,
        qty_milli=60_000,
        total_value_paise=270_000,
    )
    post_stock_receipt(
        item_id=flour.id,
        movement_date=dt,
        movement_type="purchase_receipt",
        source_doc_type="purchase",
        source_doc_id=temp_pur.id,
        qty_milli=60_000,
        total_value_paise=270_000,
        created_by=admin.id,
    )
    db_session.rollback()
    assert get_item_stock_position(flour.id).qty_milli == 100_000

    # Commit benchmark purchase receipt: +60 kg @ ₹45.00 = ₹2,700.00
    pur = _make_source_purchase(
        db_session,
        supplier_id=data["supplier"].id,
        user_id=admin.id,
        number="PUR-001",
        dt=dt,
        item=flour,
        qty_milli=60_000,
        total_value_paise=270_000,
    )
    post_stock_receipt(
        item_id=flour.id,
        movement_date=dt,
        movement_type="purchase_receipt",
        source_doc_type="purchase",
        source_doc_id=pur.id,
        qty_milli=60_000,
        total_value_paise=270_000,
        created_by=admin.id,
    )
    db_session.commit()

    # Combined: 160 kg, ₹6,900.00 -> exact WAC = ₹43.125/kg (4312.5 paise/kg)
    mid_pos = get_item_stock_position(flour.id)
    assert mid_pos.qty_milli == 160_000
    assert mid_pos.value_paise == 690_000
    assert mid_pos.wac_rupees == Decimal("43.125")
    assert mid_pos.wac_paise_exact == Decimal("4312.5")
    assert mid_pos.unit_cost_paise == 4313  # Rounded display unit cost

    # Issue 40 kg: MUST use unrounded WAC -> 690,000 * 40,000 / 160,000 = 172,500 paise EXACT
    # (NOT 40 * 4313 = 172,520 paise!)
    issue_adj = _make_source_adjustment(
        db_session, user_id=admin.id, number="ADJ-ISS-1", adj_type="spoilage_writeoff", dt=dt
    )
    issue_entry = post_stock_issue(
        item_id=flour.id,
        movement_date=dt,
        movement_type="adjustment_out",
        source_doc_type="stock_adjustment",
        source_doc_id=issue_adj.id,
        qty_milli=40_000,
        created_by=admin.id,
    )
    db_session.commit()

    assert issue_entry.qty_change_milli == -40_000
    assert issue_entry.value_change_paise == -172_500
    assert issue_entry.unit_cost_paise == 4313

    # Closing: 120 kg, ₹5,175.00, exact WAC still ₹43.125/kg
    closing = get_item_stock_position(flour.id)
    assert closing.qty_milli == 120_000
    assert closing.value_paise == 517_500
    assert closing.wac_rupees == Decimal("43.125")

    # Backdated movement must be rejected by the application
    with pytest.raises(InvalidMovementError, match="Backdated"):
        post_stock_receipt(
            item_id=flour.id,
            movement_date=date(2026, 11, 23),
            movement_type="purchase_receipt",
            source_doc_type="purchase",
            source_doc_id=pur.id,
            qty_milli=10_000,
            total_value_paise=45_000,
            created_by=admin.id,
        )


def test_full_depletion_leaves_exact_zero_quantity_and_zero_residual_value(db_session):
    admin = _create_user(db_session, username="admin_deplete", role=ROLE_ADMIN)
    data = _seed_benchmark_master_data(db_session)
    oil = data["items"]["oil"]
    dt = date(2026, 11, 24)

    # Non-terminating WAC: 3 L for ₹100.00 (10,000 paise -> ₹33.3333.../L)
    adj_in = _make_source_adjustment(
        db_session, user_id=admin.id, number="ADJ-DEP-0", adj_type="opening_stock", dt=dt
    )
    post_stock_receipt(
        item_id=oil.id,
        movement_date=dt,
        movement_type="opening_stock",
        source_doc_type="stock_adjustment",
        source_doc_id=adj_in.id,
        qty_milli=3_000,
        total_value_paise=10_000,
        created_by=admin.id,
    )
    db_session.commit()

    # Issue 1 L -> round_half_up(10000 * 1000 / 3000) = 3333 paise
    adj1 = _make_source_adjustment(
        db_session, user_id=admin.id, number="ADJ-DEP-1", adj_type="spoilage_writeoff", dt=dt
    )
    e1 = post_stock_issue(
        item_id=oil.id,
        movement_date=dt,
        movement_type="adjustment_out",
        source_doc_type="stock_adjustment",
        source_doc_id=adj1.id,
        qty_milli=1_000,
        created_by=admin.id,
    )
    assert e1.value_change_paise == -3_333

    # Issue 1 L -> remaining is 2 L / 6667 paise -> round_half_up(6667 * 1000 / 2000) = 3334 paise
    adj2 = _make_source_adjustment(
        db_session, user_id=admin.id, number="ADJ-DEP-2", adj_type="spoilage_writeoff", dt=dt
    )
    e2 = post_stock_issue(
        item_id=oil.id,
        movement_date=dt,
        movement_type="adjustment_out",
        source_doc_type="stock_adjustment",
        source_doc_id=adj2.id,
        qty_milli=1_000,
        created_by=admin.id,
    )
    assert e2.value_change_paise == -3_334

    # Issue final 1 L -> full depletion must take exact remaining 3333 paise -> zero residual
    adj3 = _make_source_adjustment(
        db_session, user_id=admin.id, number="ADJ-DEP-3", adj_type="spoilage_writeoff", dt=dt
    )
    e3 = post_stock_issue(
        item_id=oil.id,
        movement_date=dt,
        movement_type="adjustment_out",
        source_doc_type="stock_adjustment",
        source_doc_id=adj3.id,
        qty_milli=1_000,
        created_by=admin.id,
    )
    db_session.commit()

    assert e3.value_change_paise == -3_333
    assert -(e1.value_change_paise + e2.value_change_paise + e3.value_change_paise) == 10_000

    final_pos = get_item_stock_position(oil.id)
    assert final_pos.qty_milli == 0
    assert final_pos.value_paise == 0
    assert final_pos.wac_rupees is None


# ---------------------------------------------------------------------------
# 8. Negative Stock Rejected by Both Service and Database Trigger
# ---------------------------------------------------------------------------


def test_negative_stock_rejected_by_service_and_database_trigger(db_session):
    admin = _create_user(db_session, username="admin_neg", role=ROLE_ADMIN)
    data = _seed_benchmark_master_data(db_session)
    flour = data["items"]["flour"]
    dt = date(2026, 11, 24)

    record_opening_stock(
        item_id=flour.id,
        qty_milli=10_000,
        unit_cost_paise=4200,
        movement_date=dt,
        created_by=admin.id,
    )

    adj_out = _make_source_adjustment(
        db_session, user_id=admin.id, number="ADJ-NEG-SVC", adj_type="spoilage_writeoff", dt=dt
    )
    db_session.commit()

    # Service-level safeguard
    with pytest.raises(InsufficientStockError, match="Insufficient stock"):
        post_stock_issue(
            item_id=flour.id,
            movement_date=dt,
            movement_type="adjustment_out",
            source_doc_type="stock_adjustment",
            source_doc_id=adj_out.id,
            qty_milli=10_001,
            created_by=admin.id,
        )

    # Direct ORM insert bypassing service -> blocked by SQLite trigger
    direct_bad = StockLedger(
        item_id=flour.id,
        movement_date=dt,
        movement_type="adjustment_out",
        source_doc_type="stock_adjustment",
        source_doc_id=adj_out.id,
        qty_change_milli=-10_001,
        unit_cost_paise=4200,
        value_change_paise=-42000,
        created_by=admin.id,
    )
    db_session.add(direct_bad)
    with pytest.raises(IntegrityError, match="negative inventory quantity"):
        db_session.commit()
    db_session.rollback()

    # Balance remains completely intact
    assert get_item_stock_position(flour.id).qty_milli == 10_000


def test_trigger_rejects_historically_negative_running_balance_even_with_zero_final_sum(
    db_session,
):
    admin = _create_user(db_session, username="admin_chrono", role=ROLE_ADMIN)
    data = _seed_benchmark_master_data(db_session)
    flour = data["items"]["flour"]
    oil = data["items"]["oil"]

    # Later receipt on Nov 24 (+10 kg / +₹420.00)
    later_pur = _make_source_purchase(
        db_session,
        supplier_id=data["supplier"].id,
        user_id=admin.id,
        number="PUR-LATER-QTY",
        dt=date(2026, 11, 24),
        item=flour,
        qty_milli=10_000,
        total_value_paise=42_000,
    )
    post_stock_receipt(
        item_id=flour.id,
        movement_date=date(2026, 11, 24),
        movement_type="purchase_receipt",
        source_doc_type="purchase",
        source_doc_id=later_pur.id,
        qty_milli=10_000,
        total_value_paise=42_000,
        created_by=admin.id,
    )
    early_adj = _make_source_adjustment(
        db_session,
        user_id=admin.id,
        number="ADJ-EARLY-ISS",
        adj_type="spoilage_writeoff",
        dt=date(2026, 11, 23),
    )
    db_session.commit()

    # Directly inserting an earlier issue on Nov 23 (-10 kg / -₹420.00) would have
    # final aggregate 0, but running qty on Nov 23 is -10 kg -> trigger must abort.
    backdated_qty_issue = StockLedger(
        item_id=flour.id,
        movement_date=date(2026, 11, 23),
        movement_type="adjustment_out",
        source_doc_type="stock_adjustment",
        source_doc_id=early_adj.id,
        qty_change_milli=-10_000,
        unit_cost_paise=4200,
        value_change_paise=-42_000,
        created_by=admin.id,
    )
    db_session.add(backdated_qty_issue)
    with pytest.raises(IntegrityError, match="negative inventory quantity"):
        db_session.commit()
    db_session.rollback()

    # Also verify historically negative inventory VALUE with non-negative quantity:
    # Nov 22: +10 L at ₹0; Nov 24: +10 L at ₹1,400.
    record_opening_stock(
        item_id=oil.id,
        qty_milli=10_000,
        unit_cost_paise=0,
        movement_date=date(2026, 11, 22),
        created_by=admin.id,
        reason="Zero-cost opening oil",
    )
    later_oil_pur = _make_source_purchase(
        db_session,
        supplier_id=data["supplier"].id,
        user_id=admin.id,
        number="PUR-LATER-VAL",
        dt=date(2026, 11, 24),
        item=oil,
        qty_milli=10_000,
        total_value_paise=140_000,
    )
    post_stock_receipt(
        item_id=oil.id,
        movement_date=date(2026, 11, 24),
        movement_type="purchase_receipt",
        source_doc_type="purchase",
        source_doc_id=later_oil_pur.id,
        qty_milli=10_000,
        total_value_paise=140_000,
        created_by=admin.id,
    )
    db_session.commit()

    # Directly inserting an issue on Nov 23 (-5 L / -₹700) keeps qty >= 0 (+5 L on Nov 23,
    # +15 L on Nov 24) and final value >= 0 (+₹700), but running value on Nov 23 is -₹700 -> abort.
    backdated_val_issue = StockLedger(
        item_id=oil.id,
        movement_date=date(2026, 11, 23),
        movement_type="adjustment_out",
        source_doc_type="stock_adjustment",
        source_doc_id=early_adj.id,
        qty_change_milli=-5_000,
        unit_cost_paise=14000,
        value_change_paise=-70_000,
        created_by=admin.id,
    )
    db_session.add(backdated_val_issue)
    with pytest.raises(IntegrityError, match="negative inventory value"):
        db_session.commit()
    db_session.rollback()

    # Balances remain unaffected after rejected inserts.
    assert get_item_stock_position(flour.id).qty_milli == 10_000
    assert get_item_stock_position(flour.id).value_paise == 42_000
    assert get_item_stock_position(oil.id).qty_milli == 20_000
    assert get_item_stock_position(oil.id).value_paise == 140_000


# ---------------------------------------------------------------------------
# 9. Exact Ledger Reversal
# ---------------------------------------------------------------------------


def test_ledger_reversal_exact_negation_and_guards(db_session):
    admin = _create_user(db_session, username="admin_rev", role=ROLE_ADMIN)
    data = _seed_benchmark_master_data(db_session)
    flour = data["items"]["flour"]
    dt = date(2026, 11, 24)

    _adj, open_entry = record_opening_stock(
        item_id=flour.id,
        qty_milli=50_000,
        unit_cost_paise=4200,
        movement_date=dt,
        created_by=admin.id,
    )

    # Issue 20 kg
    adj_out = _make_source_adjustment(
        db_session, user_id=admin.id, number="ADJ-REV-OUT", adj_type="spoilage_writeoff", dt=dt
    )
    out_entry = post_stock_issue(
        item_id=flour.id,
        movement_date=dt,
        movement_type="adjustment_out",
        source_doc_type="stock_adjustment",
        source_doc_id=adj_out.id,
        qty_milli=20_000,
        created_by=admin.id,
    )
    db_session.commit()

    # Reversing opening stock (50 kg) while only 30 kg remain would cause negative stock -> blocked
    with pytest.raises(InsufficientStockError, match="negative stock"):
        post_ledger_reversal(ledger_id=open_entry.id, movement_date=dt, created_by=admin.id)

    # Reversing the 20 kg issue restores balance to 50 kg / ₹2,100
    rev_in = post_ledger_reversal(
        ledger_id=out_entry.id,
        movement_date=dt,
        created_by=admin.id,
        notes="Reversing mistaken issue",
    )
    db_session.commit()
    assert rev_in.movement_type == "reversal_in"
    assert rev_in.reverses_ledger_id == out_entry.id
    assert rev_in.qty_change_milli == 20_000
    assert rev_in.value_change_paise == 84_000
    assert rev_in.unit_cost_paise == out_entry.unit_cost_paise
    assert get_item_stock_position(flour.id).qty_milli == 50_000
    assert get_item_stock_position(flour.id).value_paise == 210_000

    # Double reversal is rejected
    with pytest.raises(InvalidMovementError, match="already been reversed"):
        post_ledger_reversal(ledger_id=out_entry.id, movement_date=dt, created_by=admin.id)

    # Reversing a reversal is rejected
    with pytest.raises(InvalidMovementError, match="Cannot reverse a reversal"):
        post_ledger_reversal(ledger_id=rev_in.id, movement_date=dt, created_by=admin.id)

    # Now reversing opening stock succeeds and leaves exact zero
    rev_open = post_ledger_reversal(
        ledger_id=open_entry.id,
        movement_date=dt,
        created_by=admin.id,
    )
    db_session.commit()
    assert rev_open.movement_type == "reversal_out"
    assert rev_open.qty_change_milli == -50_000
    assert rev_open.value_change_paise == -210_000
    assert get_item_stock_position(flour.id).qty_milli == 0
    assert get_item_stock_position(flour.id).value_paise == 0


# ---------------------------------------------------------------------------
# 10, 11, 12. Duplicate Opening Stock, Prior Movements & Inactive Item Rejection
# ---------------------------------------------------------------------------


def test_duplicate_opening_stock_prior_movements_and_inactive_item_rejected(db_session):
    admin = _create_user(db_session, username="admin_rules", role=ROLE_ADMIN)
    data = _seed_benchmark_master_data(db_session)
    flour = data["items"]["flour"]
    oil = data["items"]["oil"]
    spice = data["items"]["spice_mix"]
    dt = date(2026, 11, 24)

    # 10. Duplicate opening stock rejected
    record_opening_stock(
        item_id=flour.id,
        qty_milli=100_000,
        unit_cost_paise=4200,
        movement_date=dt,
        created_by=admin.id,
    )
    with pytest.raises(InvalidMovementError, match="already been recorded"):
        record_opening_stock(
            item_id=flour.id,
            qty_milli=10_000,
            unit_cost_paise=4200,
            movement_date=dt,
            created_by=admin.id,
        )

    # 11. Opening stock rejected if item already has any previous ledger movement
    pur = _make_source_purchase(
        db_session,
        supplier_id=data["supplier"].id,
        user_id=admin.id,
        number="PUR-PRIOR",
        dt=dt,
        item=oil,
        qty_milli=10_000,
        total_value_paise=140_000,
    )
    post_stock_receipt(
        item_id=oil.id,
        movement_date=dt,
        movement_type="purchase_receipt",
        source_doc_type="purchase",
        source_doc_id=pur.id,
        qty_milli=10_000,
        total_value_paise=140_000,
        created_by=admin.id,
    )
    db_session.commit()

    with pytest.raises(InvalidMovementError, match="already has stock ledger movements"):
        record_opening_stock(
            item_id=oil.id,
            qty_milli=30_000,
            unit_cost_paise=14000,
            movement_date=dt,
            created_by=admin.id,
        )

    # 12. Inactive item rejected
    spice.is_active = False
    db_session.commit()
    with pytest.raises(InvalidMovementError, match="inactive"):
        record_opening_stock(
            item_id=spice.id,
            qty_milli=5_000,
            unit_cost_paise=30000,
            movement_date=dt,
            created_by=admin.id,
        )


# ---------------------------------------------------------------------------
# 13. Stock-Unit Changes Blocked After Ledger History Exists
# ---------------------------------------------------------------------------


def test_master_data_blocks_stock_unit_change_after_ledger_history_exists(client, db_session):
    admin = _create_user(db_session, username="admin_unit_guard", role=ROLE_ADMIN)
    data = _seed_benchmark_master_data(db_session)
    flour = data["items"]["flour"]
    gram = data["units"]["g"]
    kg = data["units"]["kg"]
    dt = date(2026, 11, 24)

    record_opening_stock(
        item_id=flour.id,
        qty_milli=100_000,
        unit_cost_paise=4200,
        movement_date=dt,
        created_by=admin.id,
    )

    _login(client, "admin_unit_guard")

    # Attempt to change Flour stock_unit from kg -> g must be blocked
    res = client.post(
        f"/master/items/{flour.id}/edit",
        data={
            "csrf_token": _csrf(client),
            "sku": flour.sku,
            "name": flour.name,
            "item_type": flour.item_type,
            "stock_unit_id": str(gram.id),
            "hsn_code": "",
            "gst_rate": "0",
            "default_sale_price": "",
            "is_active": "1",
        },
    )
    assert res.status_code == 200
    assert b"Cannot change base stock unit because this item already has stock ledger history" in res.data
    db_session.refresh(flour)
    assert flour.stock_unit_id == kg.id

    # Editing non-unit fields (e.g. name) while keeping stock_unit unchanged still works
    ok_res = client.post(
        f"/master/items/{flour.id}/edit",
        data={
            "csrf_token": _csrf(client),
            "sku": flour.sku,
            "name": "Wheat Flour",
            "item_type": flour.item_type,
            "stock_unit_id": str(kg.id),
            "hsn_code": "11010000",
            "gst_rate": "0",
            "default_sale_price": "",
            "is_active": "1",
        },
        follow_redirects=True,
    )
    assert ok_res.status_code == 200
    db_session.refresh(flour)
    assert flour.name == "Wheat Flour"
    assert flour.stock_unit_id == kg.id


# ---------------------------------------------------------------------------
# 14. Atomic Transaction Rollback (No Orphan Adjustment, Ledger, or Audit)
# ---------------------------------------------------------------------------


def test_opening_stock_rollback_leaves_no_orphan_records(db_session, monkeypatch):
    admin = _create_user(db_session, username="admin_atomic", role=ROLE_ADMIN)
    data = _seed_benchmark_master_data(db_session)
    flour = data["items"]["flour"]

    def _boom(**_kwargs):
        raise RuntimeError("Simulated audit write failure")

    monkeypatch.setattr("app.inventory_service.log_audit", _boom)

    with pytest.raises(RuntimeError, match="Simulated audit write failure"):
        record_opening_stock(
            item_id=flour.id,
            qty_milli=100_000,
            unit_cost_paise=4200,
            movement_date=date(2026, 11, 24),
            created_by=admin.id,
        )

    assert db_session.execute(db.select(StockAdjustment)).scalars().all() == []
    assert db_session.execute(db.select(StockLedger)).scalars().all() == []
    assert db_session.execute(db.select(AuditLog)).scalars().all() == []


# ---------------------------------------------------------------------------
# 15 & 16. RBAC, CSRF, Opening Stock UI & Financial Cost Visibility Restrictions
# ---------------------------------------------------------------------------


def test_opening_stock_ui_flow_with_unit_conversion_and_csrf(client, db_session):
    _create_user(db_session, username="admin_ui", role=ROLE_ADMIN)
    data = _seed_benchmark_master_data(db_session)
    spice = data["items"]["spice_mix"]
    gram = data["units"]["g"]

    # Unauthenticated user redirected to login
    unauth = client.get("/inventory")
    assert unauth.status_code == 302
    assert "/login" in unauth.headers["Location"]

    _login(client, "admin_ui")

    # Missing CSRF token rejected with 400
    bad_csrf = client.post(
        "/inventory/opening-stock",
        data={
            "item_id": str(spice.id),
            "quantity": "5000",
            "input_unit_id": str(gram.id),
            "unit_cost": "300.00",
            "movement_date": "2026-11-24",
            "reason": "Opening spice stock",
        },
    )
    assert bad_csrf.status_code == 400

    # Valid submission with gram -> kg conversion (5,000 g = 5 kg @ ₹300/kg = ₹1,500.00)
    res = client.post(
        "/inventory/opening-stock",
        data={
            "csrf_token": _csrf(client),
            "item_id": str(spice.id),
            "quantity": "5000",
            "input_unit_id": str(gram.id),
            "unit_cost": "300.00",
            "movement_date": "2026-11-24",
            "reason": "Opening spice stock",
            "notes": "Counted in grams",
        },
        follow_redirects=True,
    )
    assert res.status_code == 200
    assert b"ADJ/2026-27/0001" in res.data
    assert b"5 kg" in res.data
    assert b"1500.00" in res.data

    # Duplicate submission via UI returns 200 with error message
    dup = client.post(
        "/inventory/opening-stock",
        data={
            "csrf_token": _csrf(client),
            "item_id": str(spice.id),
            "quantity": "1",
            "input_unit_id": "",
            "unit_cost": "300.00",
            "movement_date": "2026-11-24",
            "reason": "Duplicate attempt",
        },
    )
    assert dup.status_code == 200
    assert b"already been recorded" in dup.data


@pytest.mark.parametrize(
    "role,can_write_opening,can_see_valuation",
    [
        (ROLE_ADMIN, True, True),
        (ROLE_OWNER, True, True),
        (ROLE_ACCOUNTANT, False, True),
        (ROLE_PRODUCTION_SUPERVISOR, False, False),
        (ROLE_SALESMAN, False, False),
    ],
)
def test_inventory_rbac_and_financial_cost_visibility(
    client, db_session, role, can_write_opening, can_see_valuation
):
    admin = _create_user(db_session, username="seed_admin", role=ROLE_ADMIN)
    actor = _create_user(db_session, username=f"actor_{role}", role=role)
    data = _seed_benchmark_master_data(db_session)
    flour = data["items"]["flour"]

    record_opening_stock(
        item_id=flour.id,
        qty_milli=100_000,
        unit_cost_paise=4200,
        movement_date=date(2026, 11, 24),
        created_by=admin.id,
        reason="Opening flour",
    )

    # Service-level authorization enforcement
    if not can_write_opening:
        with pytest.raises(UnauthorizedInventoryActionError):
            record_opening_stock(
                item_id=data["items"]["oil"].id,
                qty_milli=10_000,
                unit_cost_paise=14000,
                movement_date=date(2026, 11, 24),
                created_by=actor.id,
            )

    _login(client, f"actor_{role}")

    # 1. Opening stock form access
    open_get = client.get("/inventory/opening-stock")
    assert open_get.status_code == (200 if can_write_opening else 403)

    open_post = client.post(
        "/inventory/opening-stock",
        data={
            "csrf_token": _csrf(client),
            "item_id": str(data["items"]["oil"].id),
            "quantity": "30",
            "unit_cost": "140.00",
            "movement_date": "2026-11-24",
            "reason": "Opening oil",
        },
    )
    assert open_post.status_code == (302 if can_write_opening else 403)

    # 2. Stock summary visibility: all roles see qty, only financial roles see WAC & value
    summary = client.get("/inventory")
    assert summary.status_code == 200
    assert b"100 kg" in summary.data
    if can_see_valuation:
        assert b"WAC" in summary.data
        assert b"Stock Value" in summary.data
        assert b"4200.00" in summary.data
    else:
        assert b"WAC" not in summary.data
        assert b"Stock Value" not in summary.data
        assert b"4200.00" not in summary.data
        assert b"42.00" not in summary.data

    # 3. Stock ledger visibility: all roles see qty, only financial roles see Unit Cost & Value
    ledger = client.get(f"/inventory/ledger?item_id={flour.id}")
    assert ledger.status_code == 200
    assert b"+100" in ledger.data
    assert b"ADJ/2026-27/0001" in ledger.data
    if can_see_valuation:
        assert b"Unit Cost" in ledger.data
        assert b"Value Change" in ledger.data
        assert b"4200.00" in ledger.data
    else:
        assert b"Unit Cost" not in ledger.data
        assert b"Value Change" not in ledger.data
        assert b"4200.00" not in ledger.data
        assert b"42.00" not in ledger.data

    # 4. Item detail page: shows on-hand qty to all, valuation only to financial roles
    detail = client.get(f"/master/items/{flour.id}")
    assert detail.status_code == 200
    assert b"On-Hand Stock" in detail.data
    assert b"100 kg" in detail.data
    if can_see_valuation:
        assert b"Current WAC" in detail.data
        assert b"Inventory Value" in detail.data
        assert b"4200.00" in detail.data
    else:
        assert b"Current WAC" not in detail.data
        assert b"Inventory Value" not in detail.data
        assert b"4200.00" not in detail.data

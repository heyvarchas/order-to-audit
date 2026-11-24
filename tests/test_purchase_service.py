"""Step B — Step 2 tests: Purchase domain service, GST, unit conversion, WAC, rollback, cancellation & RBAC."""

from datetime import date
from decimal import Decimal
import json

import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.inventory_service import (
    InsufficientStockError,
    InvalidMovementError,
    get_item_stock_position,
    post_stock_issue,
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
from app.permissions import (
    ROLE_ACCOUNTANT,
    ROLE_ADMIN,
    ROLE_OWNER,
    ROLE_PRODUCTION_SUPERVISOR,
    ROLE_SALESMAN,
)
from app.purchase_service import (
    PurchaseError,
    PurchaseLineInput,
    PurchaseNotFoundError,
    PurchaseStateError,
    PurchaseValidationError,
    UnauthorizedPurchaseActionError,
    add_draft_purchase_item,
    cancel_purchase,
    compute_purchase_line,
    compute_purchase_totals,
    create_draft_purchase,
    generate_next_purchase_number,
    get_purchase,
    get_purchase_ledger_entries,
    list_purchases,
    post_purchase,
    remove_draft_purchase_item,
    update_draft_purchase,
    update_draft_purchase_item,
)


def _create_user(
    db_session,
    *,
    username: str,
    role: str,
    is_active: bool = True,
    password: str = "secret-pass",
) -> User:
    user = User(
        username=username,
        full_name=f"{username.title()} User",
        email=f"{username}@example.com",
        role=role,
        is_active=is_active,
    )
    user.set_password(password)
    db_session.add(user)
    db_session.commit()
    return user


def _seed_master_data(db_session):
    """Seed units, item-specific conversions, items of all 3 types, and suppliers."""
    g = Unit(code="g", name="Gram", dimension="mass", factor_to_base=1)
    ml = Unit(code="ml", name="Millilitre", dimension="volume", factor_to_base=1)
    piece = Unit(code="piece", name="Piece", dimension="count", factor_to_base=1)
    packet = Unit(code="packet", name="Packet", dimension="count", factor_to_base=1)
    carton = Unit(code="carton", name="Carton", dimension="count", factor_to_base=1)
    bag = Unit(code="bag", name="Bag", dimension="mass", factor_to_base=1)
    db_session.add_all([g, ml, piece, packet, carton, bag])
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
    spice = Item(
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

    supplier_tn = Supplier(
        name="Tamil Nadu Millers",
        gstin="33AAAAA0000A1Z5",
        state="Tamil Nadu",
        is_active=True,
    )
    supplier_ka = Supplier(
        name="Karnataka Oils Ltd",
        gstin="29BBBBB0000B1Z5",
        state="Karnataka",
        is_active=True,
    )
    db_session.add_all([flour, oil, spice, pouches, murukku, supplier_tn, supplier_ka])
    db_session.flush()

    # Item-specific conversions:
    # - 1 carton = 20 packets for Finished Murukku
    # - 1 bag = 3 kg for Spice Mix (tests non-terminating per-stock-unit cost)
    db_session.add_all(
        [
            ItemUnitConversion(
                item_id=murukku.id,
                from_unit_id=carton.id,
                to_unit_id=packet.id,
                multiplier=20,
            ),
            ItemUnitConversion(
                item_id=spice.id,
                from_unit_id=bag.id,
                to_unit_id=kg.id,
                multiplier=3,
            ),
        ]
    )
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
            "bag": bag,
        },
        "items": {
            "flour": flour,
            "oil": oil,
            "spice": spice,
            "pouches": pouches,
            "murukku": murukku,
        },
        "suppliers": {
            "tn": supplier_tn,
            "ka": supplier_ka,
        },
    }


def _make_issue_adjustment(db_session, *, user_id: int, number: str, dt: date) -> StockAdjustment:
    adj = StockAdjustment(
        adjustment_number=number,
        adjustment_type="spoilage_writeoff",
        adjustment_date=dt,
        reason="Test outward movement",
        created_by=user_id,
    )
    db_session.add(adj)
    db_session.flush()
    return adj


# ---------------------------------------------------------------------------
# 1. Sequential FY Numbering, Non-Reuse & Concurrency Collision Handling
# ---------------------------------------------------------------------------


def test_sequential_fy_numbering_non_reuse_and_cross_fy_draft_edit(db_session):
    admin = _create_user(db_session, username="admin_fy", role=ROLE_ADMIN)
    data = _seed_master_data(db_session)
    supplier = data["suppliers"]["tn"]
    flour = data["items"]["flour"]

    # Last day of FY 2025-26 -> PUR/2025-26/0001
    p1 = create_draft_purchase(
        supplier_id=supplier.id,
        purchase_date=date(2026, 3, 31),
        actor_user_id=admin.id,
        lines=[PurchaseLineInput(item_id=flour.id, entered_qty_milli=10_000, unit_rate_paise=4200)],
    )
    assert p1.purchase_number == "PUR/2025-26/0001"

    # First day of FY 2026-27 -> resets to PUR/2026-27/0001
    p2 = create_draft_purchase(
        supplier_id=supplier.id,
        purchase_date=date(2026, 4, 1),
        actor_user_id=admin.id,
        lines=[PurchaseLineInput(item_id=flour.id, entered_qty_milli=10_000, unit_rate_paise=4200)],
    )
    assert p2.purchase_number == "PUR/2026-27/0001"

    # Cancel p2 -> number must NOT be reused by the next purchase in FY 2026-27.
    cancel_purchase(p2.id, actor_user_id=admin.id, reason="Duplicate draft")
    p3 = create_draft_purchase(
        supplier_id=supplier.id,
        purchase_date=date(2026, 4, 2),
        actor_user_id=admin.id,
        lines=[PurchaseLineInput(item_id=flour.id, entered_qty_milli=20_000, unit_rate_paise=4300)],
    )
    assert p3.purchase_number == "PUR/2026-27/0002"

    # Cross-FY draft date edit on p1 (2026-03-31 -> 2026-04-15) regenerates purchase_number
    # to PUR/2026-27/0003 and records both old and new numbers in AuditLog.
    updated_p1 = update_draft_purchase(
        p1.id,
        actor_user_id=admin.id,
        purchase_date=date(2026, 4, 15),
    )
    assert updated_p1.purchase_number == "PUR/2026-27/0003"
    assert updated_p1.purchase_date == date(2026, 4, 15)

    update_audit = db_session.execute(
        db.select(AuditLog).where(
            AuditLog.entity_type == "purchase",
            AuditLog.entity_id == p1.id,
            AuditLog.action == "purchase_updated",
        )
    ).scalar_one()
    old_snap = json.loads(update_audit.old_values_json)
    new_snap = json.loads(update_audit.new_values_json)
    assert old_snap["purchase_number"] == "PUR/2025-26/0001"
    assert new_snap["purchase_number"] == "PUR/2026-27/0003"
    assert "PUR/2025-26/0001" in update_audit.summary
    assert "PUR/2026-27/0003" in update_audit.summary


def test_numbering_collision_retries_and_unrelated_integrity_error_propagates(
    db_session, monkeypatch
):
    admin = _create_user(db_session, username="admin_retry", role=ROLE_ADMIN)
    data = _seed_master_data(db_session)
    supplier = data["suppliers"]["tn"]
    flour = data["items"]["flour"]

    first = create_draft_purchase(
        supplier_id=supplier.id,
        purchase_date=date(2026, 11, 24),
        actor_user_id=admin.id,
        lines=[PurchaseLineInput(item_id=flour.id, entered_qty_milli=10_000, unit_rate_paise=4200)],
    )
    assert first.purchase_number == "PUR/2026-27/0001"

    # Simulate a concurrent collision on the first allocation attempt, succeeding on attempt 2.
    real_gen = generate_next_purchase_number
    calls = {"count": 0}

    def _flaky_number_gen(dt: date) -> str:
        calls["count"] += 1
        if calls["count"] == 1:
            return first.purchase_number  # Forces UNIQUE constraint failed: purchases.purchase_number
        return real_gen(dt)

    monkeypatch.setattr("app.purchase_service.generate_next_purchase_number", _flaky_number_gen)

    second = create_draft_purchase(
        supplier_id=supplier.id,
        purchase_date=date(2026, 11, 24),
        actor_user_id=admin.id,
        lines=[PurchaseLineInput(item_id=flour.id, entered_qty_milli=5_000, unit_rate_paise=4500)],
    )
    assert calls["count"] == 2
    assert second.purchase_number == "PUR/2026-27/0002"
    assert len(second.items) == 1

    # Persistent collisions exhaust retries and raise PurchaseError cleanly.
    monkeypatch.setattr(
        "app.purchase_service.generate_next_purchase_number",
        lambda _dt: first.purchase_number,
    )
    with pytest.raises(PurchaseError, match="Could not allocate a unique purchase number"):
        create_draft_purchase(
            supplier_id=supplier.id,
            purchase_date=date(2026, 11, 24),
            actor_user_id=admin.id,
        )

    # Unrelated IntegrityError must NOT be masked or retried as a numbering collision.
    monkeypatch.setattr("app.purchase_service.generate_next_purchase_number", real_gen)

    def _unrelated_integrity_boom(**_kwargs):
        raise IntegrityError(
            "INSERT INTO audit_logs ...",
            {},
            Exception("FOREIGN KEY constraint failed"),
        )

    monkeypatch.setattr("app.purchase_service.log_audit", _unrelated_integrity_boom)
    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        create_draft_purchase(
            supplier_id=supplier.id,
            purchase_date=date(2026, 11, 24),
            actor_user_id=admin.id,
        )


# ---------------------------------------------------------------------------
# 2. Draft CRUD, All Three Item Types, Line Operations & Duplicate Item Guard
# ---------------------------------------------------------------------------


def test_draft_creation_editing_line_operations_and_all_three_item_types(db_session):
    admin = _create_user(db_session, username="admin_draft", role=ROLE_ADMIN)
    data = _seed_master_data(db_session)
    items = data["items"]
    suppliers = data["suppliers"]

    # Create draft with all 3 item types: raw_material (Flour), packaging (Pouches), finished_good (Murukku)
    draft = create_draft_purchase(
        supplier_id=suppliers["tn"].id,
        purchase_date=date(2026, 11, 24),
        actor_user_id=admin.id,
        gst_type="intra_state",
        supplier_invoice_no="  SUP-INV-101  ",
        notes="Mixed item types purchase",
        lines=[
            PurchaseLineInput(item_id=items["flour"].id, entered_qty_milli=50_000, unit_rate_paise=4200),
            PurchaseLineInput(item_id=items["pouches"].id, entered_qty_milli=200_000, unit_rate_paise=250),
            PurchaseLineInput(item_id=items["murukku"].id, entered_qty_milli=20_000, unit_rate_paise=3000),
        ],
    )
    assert draft.status == "draft"
    assert draft.created_by == admin.id
    assert draft.supplier_invoice_no == "SUP-INV-101"
    assert len(draft.items) == 3

    # Expected totals:
    # Flour:   50 * 4200 = 210000 @ 0%  -> tax 0
    # Pouches: 200 * 250 =  50000 @ 18% -> CGST 4500, SGST 4500
    # Murukku: 20 * 3000 =  60000 @ 5%  -> CGST 1500, SGST 1500
    assert draft.subtotal_paise == 320_000
    assert draft.cgst_paise == 6_000
    assert draft.sgst_paise == 6_000
    assert draft.igst_paise == 0
    assert draft.total_paise == 332_000

    # Duplicate item on add_draft_purchase_item is rejected.
    with pytest.raises(PurchaseValidationError, match="already listed"):
        add_draft_purchase_item(
            draft.id,
            line=PurchaseLineInput(item_id=items["flour"].id, entered_qty_milli=10_000, unit_rate_paise=4200),
            actor_user_id=admin.id,
        )

    # Add a 4th line (Oil, 5% GST)
    oil_line = add_draft_purchase_item(
        draft.id,
        line=PurchaseLineInput(item_id=items["oil"].id, entered_qty_milli=10_000, unit_rate_paise=14_000),
        actor_user_id=admin.id,
    )
    assert oil_line.taxable_value_paise == 140_000
    assert oil_line.cgst_paise == 3_500
    assert oil_line.sgst_paise == 3_500

    # Update the Oil line quantity & custom GST rate override
    updated_oil = update_draft_purchase_item(
        draft.id,
        oil_line.id,
        entered_qty_milli=20_000,
        unit_rate_paise=14_000,
        gst_rate_bps=1200,
        actor_user_id=admin.id,
    )
    assert updated_oil.taxable_value_paise == 280_000
    assert updated_oil.gst_rate_bps == 1200
    assert updated_oil.cgst_paise == 16_800
    assert updated_oil.sgst_paise == 16_800

    # Remove the Pouches line
    pouch_line = next(li for li in get_purchase(draft.id, actor_user_id=admin.id).items if li.item_id == items["pouches"].id)
    after_remove = remove_draft_purchase_item(draft.id, pouch_line.id, actor_user_id=admin.id)
    assert len(after_remove.items) == 3
    assert after_remove.subtotal_paise == 210_000 + 60_000 + 280_000

    # Replace lines & change supplier in update_draft_purchase (including overlapping item_id Flour
    # to prove flush ordering avoids UNIQUE(purchase_id, item_id) collisions)
    replaced = update_draft_purchase(
        draft.id,
        actor_user_id=admin.id,
        supplier_id=suppliers["ka"].id,
        gst_type="inter_state",
        supplier_invoice_no="KA-900",
        notes=None,
        lines=[
            PurchaseLineInput(item_id=items["flour"].id, entered_qty_milli=30_000, unit_rate_paise=4500),
            PurchaseLineInput(item_id=items["spice"].id, entered_qty_milli=5_000, unit_rate_paise=30_000),
        ],
    )
    assert replaced.supplier_id == suppliers["ka"].id
    assert replaced.gst_type == "inter_state"
    assert replaced.supplier_invoice_no == "KA-900"
    assert replaced.notes is None
    assert {li.item_id for li in replaced.items} == {items["flour"].id, items["spice"].id}
    assert replaced.subtotal_paise == 135_000 + 150_000
    assert replaced.cgst_paise == 0
    assert replaced.sgst_paise == 0
    assert replaced.igst_paise == 7_500  # 5% of 150,000 (Flour is 0%)
    assert replaced.total_paise == 292_500

    # Duplicate item in create_draft_purchase / update_draft_purchase is rejected.
    with pytest.raises(PurchaseValidationError, match="cannot appear more than once"):
        create_draft_purchase(
            supplier_id=suppliers["tn"].id,
            purchase_date=date(2026, 11, 24),
            actor_user_id=admin.id,
            lines=[
                PurchaseLineInput(item_id=items["flour"].id, entered_qty_milli=10_000, unit_rate_paise=4200),
                PurchaseLineInput(item_id=items["flour"].id, entered_qty_milli=20_000, unit_rate_paise=4300),
            ],
        )


# ---------------------------------------------------------------------------
# 3. GST Type Switching, Mixed Rates & Exact Half-Up Rounding
# ---------------------------------------------------------------------------


def test_gst_type_switching_recalculates_lines_and_reseeds_from_items(db_session):
    admin = _create_user(db_session, username="admin_gst", role=ROLE_ADMIN)
    data = _seed_master_data(db_session)
    items = data["items"]
    supplier = data["suppliers"]["tn"]

    # Start as 'none': Flour (0% item) + Pouches (18% item) -> both must have 0% and 0 tax.
    draft = create_draft_purchase(
        supplier_id=supplier.id,
        purchase_date=date(2026, 11, 24),
        actor_user_id=admin.id,
        gst_type="none",
        lines=[
            PurchaseLineInput(item_id=items["flour"].id, entered_qty_milli=10_000, unit_rate_paise=4200),
            PurchaseLineInput(item_id=items["pouches"].id, entered_qty_milli=100_000, unit_rate_paise=250),
        ],
    )
    assert draft.subtotal_paise == 67_000
    assert draft.total_paise == 67_000
    assert all(li.gst_rate_bps == 0 and li.cgst_paise == 0 and li.igst_paise == 0 for li in draft.items)

    # Explicitly supplying a non-zero gst_rate_bps when gst_type='none' is rejected.
    with pytest.raises(PurchaseValidationError, match="must be 0%"):
        create_draft_purchase(
            supplier_id=supplier.id,
            purchase_date=date(2026, 11, 24),
            actor_user_id=admin.id,
            gst_type="none",
            lines=[
                PurchaseLineInput(
                    item_id=items["pouches"].id,
                    entered_qty_milli=10_000,
                    unit_rate_paise=250,
                    gst_rate_bps=1800,
                )
            ],
        )

    # Switch none -> intra_state: re-seeds each line's gst_rate_bps from item.gst_rate_bps.
    intra = update_draft_purchase(draft.id, actor_user_id=admin.id, gst_type="intra_state")
    by_item = {li.item_id: li for li in intra.items}
    assert by_item[items["flour"].id].gst_rate_bps == 0
    assert by_item[items["flour"].id].cgst_paise == 0
    assert by_item[items["pouches"].id].gst_rate_bps == 1800
    assert by_item[items["pouches"].id].cgst_paise == 2_250  # 9% of 25,000
    assert by_item[items["pouches"].id].sgst_paise == 2_250
    assert intra.cgst_paise == 2_250
    assert intra.sgst_paise == 2_250
    assert intra.igst_paise == 0
    assert intra.total_paise == 71_500

    # Override Pouches line to a custom 12% rate, then switch intra_state -> inter_state:
    # custom 12% rate must be preserved and moved to IGST.
    update_draft_purchase_item(
        intra.id,
        by_item[items["pouches"].id].id,
        entered_qty_milli=100_000,
        unit_rate_paise=250,
        gst_rate_bps=1200,
        actor_user_id=admin.id,
    )
    inter = update_draft_purchase(intra.id, actor_user_id=admin.id, gst_type="inter_state")
    by_item = {li.item_id: li for li in inter.items}
    assert by_item[items["pouches"].id].gst_rate_bps == 1200
    assert by_item[items["pouches"].id].cgst_paise == 0
    assert by_item[items["pouches"].id].sgst_paise == 0
    assert by_item[items["pouches"].id].igst_paise == 3_000
    assert inter.igst_paise == 3_000
    assert inter.total_paise == 70_000

    # Switch inter_state -> none: zeroes all line GST rates and taxes.
    back_to_none = update_draft_purchase(inter.id, actor_user_id=admin.id, gst_type="none")
    assert back_to_none.cgst_paise == 0
    assert back_to_none.sgst_paise == 0
    assert back_to_none.igst_paise == 0
    assert back_to_none.total_paise == 67_000
    assert all(li.gst_rate_bps == 0 and li.igst_paise == 0 for li in back_to_none.items)


def test_exact_gst_half_up_rounding_and_zero_percent_lines(db_session):
    admin = _create_user(db_session, username="admin_round", role=ROLE_ADMIN)
    data = _seed_master_data(db_session)
    pouches = data["items"]["pouches"]
    flour = data["items"]["flour"]
    murukku = data["items"]["murukku"]
    supplier = data["suppliers"]["tn"]

    # Pure half-up boundary checks matching SQLite trigger formulas:
    # Intra-state 0.5 paise (20 * 500 / 20000) -> 1 each
    c1 = compute_purchase_line(
        item=pouches, entered_qty_milli=1000, unit_rate_paise=20, gst_type="intra_state", gst_rate_bps=500
    )
    assert (c1.cgst_paise, c1.sgst_paise, c1.igst_paise) == (1, 1, 0)

    # Intra-state 2.5 paise (100 * 500 / 20000) -> 3 each (half-up, NOT banker's 2)
    c2 = compute_purchase_line(
        item=pouches, entered_qty_milli=1000, unit_rate_paise=100, gst_type="intra_state", gst_rate_bps=500
    )
    assert (c2.cgst_paise, c2.sgst_paise, c2.igst_paise) == (3, 3, 0)

    # Intra-state 0.475 paise (19 * 500 / 20000) -> 0 each
    c3 = compute_purchase_line(
        item=pouches, entered_qty_milli=1000, unit_rate_paise=19, gst_type="intra_state", gst_rate_bps=500
    )
    assert (c3.cgst_paise, c3.sgst_paise, c3.igst_paise) == (0, 0, 0)

    # Inter-state 2.5 paise (50 * 500 / 10000) -> 3
    c4 = compute_purchase_line(
        item=pouches, entered_qty_milli=1000, unit_rate_paise=50, gst_type="inter_state", gst_rate_bps=500
    )
    assert (c4.cgst_paise, c4.sgst_paise, c4.igst_paise) == (0, 0, 3)

    # Inter-state 0.45 paise (9 * 500 / 10000) -> 0
    c5 = compute_purchase_line(
        item=pouches, entered_qty_milli=1000, unit_rate_paise=9, gst_type="inter_state", gst_rate_bps=500
    )
    assert (c5.cgst_paise, c5.sgst_paise, c5.igst_paise) == (0, 0, 0)

    totals = compute_purchase_totals([c1, c2])
    assert totals.subtotal_paise == 120
    assert totals.cgst_paise == 4
    assert totals.sgst_paise == 4
    assert totals.total_paise == 128

    # End-to-end mixed-rate intra-state post with 0% + 5% (2.5p half-up) + 18% (299.97p -> 300p)
    # proves service math passes the strict SQLite trigger check!
    draft = create_draft_purchase(
        supplier_id=supplier.id,
        purchase_date=date(2026, 11, 24),
        actor_user_id=admin.id,
        gst_type="intra_state",
        lines=[
            PurchaseLineInput(item_id=flour.id, entered_qty_milli=60_000, unit_rate_paise=4500),  # 0%
            PurchaseLineInput(item_id=murukku.id, entered_qty_milli=1_000, unit_rate_paise=100, gst_rate_bps=500),  # 2.5p -> 3p
            PurchaseLineInput(item_id=pouches.id, entered_qty_milli=1_000, unit_rate_paise=3333, gst_rate_bps=1800),  # 299.97p -> 300p
        ],
    )
    posted, receipts = post_purchase(draft.id, actor_user_id=admin.id)
    assert posted.status == "posted"
    assert posted.cgst_paise == 303
    assert posted.sgst_paise == 303
    assert posted.total_paise == 270_000 + 100 + 3_333 + 606
    # Inventory valuation excludes GST completely:
    assert sum(r.value_change_paise for r in receipts) == posted.subtotal_paise


# ---------------------------------------------------------------------------
# 4. Unit Conversions, Entered-Unit Rate Semantics & Zero-Cost Purchases
# ---------------------------------------------------------------------------


def test_unit_conversions_entered_unit_rates_and_zero_cost_purchase(db_session):
    admin = _create_user(db_session, username="admin_units", role=ROLE_ADMIN)
    data = _seed_master_data(db_session)
    units = data["units"]
    items = data["items"]
    supplier = data["suppliers"]["tn"]
    dt = date(2026, 11, 24)

    # 1. Mass (500 g @ ₹0.05/g -> 0.5 kg, taxable ₹25.00),
    # 2. Volume (2500 ml @ ₹0.14/ml -> 2.5 L, taxable ₹350.00),
    # 3. Item-specific count (2 cartons @ ₹600/carton, 1 carton = 20 packets -> 40 packets, taxable ₹1200),
    # 4. Non-terminating stock cost (1 bag = 3 kg Spice @ ₹100/bag -> 3 kg, taxable ₹100.00 exact!),
    # 5. Zero-cost free sample (50 pouches @ ₹0.00 -> allowed).
    draft = create_draft_purchase(
        supplier_id=supplier.id,
        purchase_date=dt,
        actor_user_id=admin.id,
        gst_type="intra_state",
        lines=[
            PurchaseLineInput(
                item_id=items["flour"].id,
                entered_unit_id=units["g"].id,
                entered_qty_milli=500_000,  # 500 g
                unit_rate_paise=5,          # ₹0.05 / g
            ),
            PurchaseLineInput(
                item_id=items["oil"].id,
                entered_unit_id=units["ml"].id,
                entered_qty_milli=2_500_000,  # 2500 ml
                unit_rate_paise=14,           # ₹0.14 / ml
            ),
            PurchaseLineInput(
                item_id=items["murukku"].id,
                entered_unit_id=units["carton"].id,
                entered_qty_milli=2_000,    # 2 cartons
                unit_rate_paise=60_000,     # ₹600.00 / carton
            ),
            PurchaseLineInput(
                item_id=items["spice"].id,
                entered_unit_id=units["bag"].id,
                entered_qty_milli=1_000,    # 1 bag = 3 kg
                unit_rate_paise=10_000,     # ₹100.00 / bag (₹33.3333.../kg)
            ),
            PurchaseLineInput(
                item_id=items["pouches"].id,
                entered_unit_id=units["piece"].id,
                entered_qty_milli=50_000,   # 50 pieces
                unit_rate_paise=0,          # Free sample
            ),
        ],
    )
    posted, receipts = post_purchase(draft.id, actor_user_id=admin.id)
    assert posted.status == "posted"
    receipt_by_item = {r.item_id: r for r in receipts}

    # Flour: 500 g = 0.5 kg (500 milli kg), taxable = 2500 paise (₹25), display cost = ₹50/kg
    assert receipt_by_item[items["flour"].id].qty_change_milli == 500
    assert receipt_by_item[items["flour"].id].value_change_paise == 2_500
    assert receipt_by_item[items["flour"].id].unit_cost_paise == 5_000

    # Oil: 2500 ml = 2.5 L (2500 milli L), taxable = 35000 paise (5% GST excluded!)
    assert receipt_by_item[items["oil"].id].qty_change_milli == 2_500
    assert receipt_by_item[items["oil"].id].value_change_paise == 35_000
    assert receipt_by_item[items["oil"].id].unit_cost_paise == 14_000

    # Murukku: 2 cartons = 40 packets (40000 milli), taxable = 120000 paise (5% GST excluded!)
    assert receipt_by_item[items["murukku"].id].qty_change_milli == 40_000
    assert receipt_by_item[items["murukku"].id].value_change_paise == 120_000
    assert receipt_by_item[items["murukku"].id].unit_cost_paise == 3_000

    # Spice: 1 bag = 3 kg (3000 milli), exact 10000 paise capitalized -> zero residual on full depletion
    assert receipt_by_item[items["spice"].id].qty_change_milli == 3_000
    assert receipt_by_item[items["spice"].id].value_change_paise == 10_000
    assert receipt_by_item[items["spice"].id].unit_cost_paise == 3_333

    # Pouches: zero-cost receipt allowed
    assert receipt_by_item[items["pouches"].id].qty_change_milli == 50_000
    assert receipt_by_item[items["pouches"].id].value_change_paise == 0
    assert get_item_stock_position(items["pouches"].id).wac_rupees == Decimal("0")

    # Incompatible dimension (ml for kg-stocked Flour) rejected.
    with pytest.raises(PurchaseValidationError, match="incompatible"):
        create_draft_purchase(
            supplier_id=supplier.id,
            purchase_date=dt,
            actor_user_id=admin.id,
            lines=[
                PurchaseLineInput(
                    item_id=items["flour"].id,
                    entered_unit_id=units["ml"].id,
                    entered_qty_milli=1_000,
                    unit_rate_paise=100,
                )
            ],
        )

    # Non-exact conversion (0.5 g = 500 milli-g -> 0.0005 kg, cannot be represented in milli-kg) rejected.
    with pytest.raises(PurchaseValidationError, match="without rounding"):
        create_draft_purchase(
            supplier_id=supplier.id,
            purchase_date=dt,
            actor_user_id=admin.id,
            lines=[
                PurchaseLineInput(
                    item_id=items["flour"].id,
                    entered_unit_id=units["g"].id,
                    entered_qty_milli=500,  # 0.5 g
                    unit_rate_paise=100,
                )
            ],
        )


# ---------------------------------------------------------------------------
# 5. WAC Benchmark, GST Exclusion & Purchase-to-Ledger Traceability
# ---------------------------------------------------------------------------


def test_wac_benchmark_gst_exclusion_and_purchase_ledger_traceability(db_session):
    admin = _create_user(db_session, username="admin_wac", role=ROLE_ADMIN)
    accountant = _create_user(db_session, username="acct_wac", role=ROLE_ACCOUNTANT)
    data = _seed_master_data(db_session)
    flour = data["items"]["flour"]
    supplier = data["suppliers"]["tn"]
    dt = date(2026, 11, 24)

    # Opening: 100 kg Flour @ ₹42.00/kg = ₹4,200.00
    record_opening_stock(
        item_id=flour.id,
        qty_milli=100_000,
        unit_cost_paise=4200,
        movement_date=dt,
        created_by=admin.id,
    )

    # Purchase: 60 kg Flour @ ₹45.00/kg + 5% intra-state GST (₹135.00 GST MUST be excluded from stock value)
    draft = create_draft_purchase(
        supplier_id=supplier.id,
        purchase_date=dt,
        actor_user_id=admin.id,
        gst_type="intra_state",
        supplier_invoice_no="MILL-2026-88",
        lines=[
            PurchaseLineInput(
                item_id=flour.id,
                entered_qty_milli=60_000,
                unit_rate_paise=4500,
                gst_rate_bps=500,
            )
        ],
    )
    posted, receipts = post_purchase(draft.id, actor_user_id=admin.id)

    assert posted.subtotal_paise == 270_000
    assert posted.cgst_paise == 6_750
    assert posted.sgst_paise == 6_750
    assert posted.total_paise == 283_500

    assert len(receipts) == 1
    assert receipts[0].value_change_paise == 270_000  # Taxable value only; GST excluded!
    assert posted.purchase_number in (receipts[0].notes or "")
    assert "MILL-2026-88" in (receipts[0].notes or "")

    # Benchmark WAC: 100 kg @ ₹42 + 60 kg @ ₹45 = 160 kg, ₹6,900.00 -> exact WAC ₹43.125/kg
    pos = get_item_stock_position(flour.id)
    assert pos.qty_milli == 160_000
    assert pos.value_paise == 690_000
    assert pos.wac_rupees == Decimal("43.125")
    assert pos.unit_cost_paise == 4313

    # Traceability via RBAC-checked reader (accountant) and AuditLog
    linked = get_purchase_ledger_entries(posted.id, actor_user_id=accountant.id)
    assert [e.id for e in linked] == [receipts[0].id]
    assert linked[0].source_doc_type == "purchase"
    assert linked[0].source_doc_id == posted.id

    audit = db_session.execute(
        db.select(AuditLog).where(
            AuditLog.entity_type == "purchase",
            AuditLog.entity_id == posted.id,
            AuditLog.action == "purchase_posted",
        )
    ).scalar_one()
    payload = json.loads(audit.new_values_json)
    assert payload["ledger_ids"] == [receipts[0].id]
    assert payload["status"] == "posted"


# ---------------------------------------------------------------------------
# 6. Duplicate Posting, Inactive Master Data, Backdating & Atomic Rollbacks
# ---------------------------------------------------------------------------


def test_duplicate_posting_empty_draft_inactive_entities_and_backdating_rejected(db_session):
    admin = _create_user(db_session, username="admin_guards", role=ROLE_ADMIN)
    data = _seed_master_data(db_session)
    items = data["items"]
    supplier = data["suppliers"]["tn"]

    # 1. Empty draft cannot be posted.
    empty = create_draft_purchase(
        supplier_id=supplier.id,
        purchase_date=date(2026, 11, 24),
        actor_user_id=admin.id,
    )
    with pytest.raises(PurchaseValidationError, match="without at least one line item"):
        post_purchase(empty.id, actor_user_id=admin.id)

    # 2. Duplicate posting rejected cleanly without creating duplicate receipts.
    valid = create_draft_purchase(
        supplier_id=supplier.id,
        purchase_date=date(2026, 11, 24),
        actor_user_id=admin.id,
        lines=[PurchaseLineInput(item_id=items["flour"].id, entered_qty_milli=10_000, unit_rate_paise=4200)],
    )
    post_purchase(valid.id, actor_user_id=admin.id)
    with pytest.raises(PurchaseStateError, match="already been posted"):
        post_purchase(valid.id, actor_user_id=admin.id)
    assert len(get_purchase_ledger_entries(valid.id, actor_user_id=admin.id)) == 1

    # Editing or line-mutating a posted purchase is rejected.
    with pytest.raises(PurchaseStateError, match="cannot be edited"):
        update_draft_purchase(valid.id, actor_user_id=admin.id, notes="No")
    with pytest.raises(PurchaseStateError, match="Cannot add lines"):
        add_draft_purchase_item(
            valid.id,
            line=PurchaseLineInput(item_id=items["oil"].id, entered_qty_milli=5_000, unit_rate_paise=14_000),
            actor_user_id=admin.id,
        )

    # 3. Backdated posting rejected (Flour already has movement on 2026-11-24).
    backdated = create_draft_purchase(
        supplier_id=supplier.id,
        purchase_date=date(2026, 11, 23),
        actor_user_id=admin.id,
        lines=[PurchaseLineInput(item_id=items["flour"].id, entered_qty_milli=10_000, unit_rate_paise=4200)],
    )
    with pytest.raises(InvalidMovementError, match="Backdated"):
        post_purchase(backdated.id, actor_user_id=admin.id)
    assert get_purchase(backdated.id, actor_user_id=admin.id).status == "draft"

    # 4. Item deactivated AFTER draft creation is rejected at posting time.
    item_deact_draft = create_draft_purchase(
        supplier_id=supplier.id,
        purchase_date=date(2026, 11, 24),
        actor_user_id=admin.id,
        lines=[PurchaseLineInput(item_id=items["spice"].id, entered_qty_milli=2_000, unit_rate_paise=30_000)],
    )
    items["spice"].is_active = False
    db_session.commit()
    with pytest.raises(PurchaseValidationError, match="inactive"):
        post_purchase(item_deact_draft.id, actor_user_id=admin.id)
    assert get_purchase(item_deact_draft.id, actor_user_id=admin.id).status == "draft"
    items["spice"].is_active = True
    db_session.commit()

    # 5. Supplier deactivated AFTER draft creation is rejected at posting time.
    sup_deact_draft = create_draft_purchase(
        supplier_id=supplier.id,
        purchase_date=date(2026, 11, 24),
        actor_user_id=admin.id,
        lines=[PurchaseLineInput(item_id=items["oil"].id, entered_qty_milli=5_000, unit_rate_paise=14_000)],
    )
    supplier.is_active = False
    db_session.commit()
    with pytest.raises(PurchaseValidationError, match="inactive"):
        post_purchase(sup_deact_draft.id, actor_user_id=admin.id)
    assert get_purchase(sup_deact_draft.id, actor_user_id=admin.id).status == "draft"

    # Creating a draft with an inactive supplier is also rejected immediately.
    with pytest.raises(PurchaseValidationError, match="inactive"):
        create_draft_purchase(
            supplier_id=supplier.id,
            purchase_date=date(2026, 11, 24),
            actor_user_id=admin.id,
        )


def test_posting_rollback_on_later_line_failure_and_audit_failure(db_session, monkeypatch):
    admin = _create_user(db_session, username="admin_rollback", role=ROLE_ADMIN)
    data = _seed_master_data(db_session)
    items = data["items"]
    supplier = data["suppliers"]["tn"]

    # Seed Oil with a movement on 2026-11-25 so line 2 (Oil) fails backdating on 2026-11-24
    # while line 1 (Flour) would have succeeded!
    record_opening_stock(
        item_id=items["oil"].id,
        qty_milli=10_000,
        unit_cost_paise=14_000,
        movement_date=date(2026, 11, 25),
        created_by=admin.id,
    )

    multi = create_draft_purchase(
        supplier_id=supplier.id,
        purchase_date=date(2026, 11, 24),
        actor_user_id=admin.id,
        lines=[
            PurchaseLineInput(item_id=items["flour"].id, entered_qty_milli=20_000, unit_rate_paise=4200),
            PurchaseLineInput(item_id=items["oil"].id, entered_qty_milli=10_000, unit_rate_paise=14_000),
        ],
    )
    audit_count_before = db_session.execute(
        db.select(AuditLog).where(AuditLog.entity_type == "purchase", AuditLog.entity_id == multi.id)
    ).scalars().all()

    with pytest.raises(InvalidMovementError, match="Backdated"):
        post_purchase(multi.id, actor_user_id=admin.id)

    # Full rollback: purchase stays draft, Flour got ZERO receipts, no purchase_posted audit.
    assert get_purchase(multi.id, actor_user_id=admin.id).status == "draft"
    assert get_item_stock_position(items["flour"].id).qty_milli == 0
    assert get_purchase_ledger_entries(multi.id, actor_user_id=admin.id) == []
    assert len(
        db_session.execute(
            db.select(AuditLog).where(AuditLog.entity_type == "purchase", AuditLog.entity_id == multi.id)
        ).scalars().all()
    ) == len(audit_count_before)

    # Now update date to 2026-11-25 and simulate audit write failure -> full rollback!
    update_draft_purchase(multi.id, actor_user_id=admin.id, purchase_date=date(2026, 11, 25))

    def _boom_audit(**_kwargs):
        raise RuntimeError("Simulated audit write failure")

    monkeypatch.setattr("app.purchase_service.log_audit", _boom_audit)

    with pytest.raises(RuntimeError, match="Simulated audit write failure"):
        post_purchase(multi.id, actor_user_id=admin.id)

    assert get_purchase(multi.id, actor_user_id=admin.id).status == "draft"
    assert get_item_stock_position(items["flour"].id).qty_milli == 0
    assert get_item_stock_position(items["oil"].id).qty_milli == 10_000
    assert get_purchase_ledger_entries(multi.id, actor_user_id=admin.id) == []


# ---------------------------------------------------------------------------
# 7. Draft Cancellation, Posted Cancellation, Stock/Value Guards & Rollback
# ---------------------------------------------------------------------------


def test_draft_cancellation_and_posted_cancellation_restores_exact_stock(
    db_session, monkeypatch
):
    admin = _create_user(db_session, username="admin_cancel", role=ROLE_ADMIN)
    data = _seed_master_data(db_session)
    items = data["items"]
    supplier = data["suppliers"]["tn"]

    # 1. Draft cancellation: no ledger entries, cannot be posted or cancelled again.
    draft = create_draft_purchase(
        supplier_id=supplier.id,
        purchase_date=date(2026, 11, 24),
        actor_user_id=admin.id,
        lines=[PurchaseLineInput(item_id=items["flour"].id, entered_qty_milli=20_000, unit_rate_paise=4200)],
    )
    cancelled_draft, rev_entries = cancel_purchase(
        draft.id,
        actor_user_id=admin.id,
        reason="Supplier out of stock",
    )
    assert cancelled_draft.status == "cancelled"
    assert rev_entries == []
    assert get_purchase_ledger_entries(draft.id, actor_user_id=admin.id) == []

    with pytest.raises(PurchaseStateError, match="Cancelled purchase"):
        post_purchase(draft.id, actor_user_id=admin.id)
    with pytest.raises(PurchaseStateError, match="already cancelled"):
        cancel_purchase(draft.id, actor_user_id=admin.id, reason="Second cancel")

    # 2. Posted multi-line purchase cancellation using business-local default date restores exact stock.
    record_opening_stock(
        item_id=items["flour"].id,
        qty_milli=100_000,
        unit_cost_paise=4200,
        movement_date=date(2026, 11, 23),
        created_by=admin.id,
    )
    p_draft = create_draft_purchase(
        supplier_id=supplier.id,
        purchase_date=date(2026, 11, 24),
        actor_user_id=admin.id,
        gst_type="intra_state",
        notes="Original immutable note",
        lines=[
            PurchaseLineInput(item_id=items["flour"].id, entered_qty_milli=60_000, unit_rate_paise=4500),
            PurchaseLineInput(item_id=items["pouches"].id, entered_qty_milli=100_000, unit_rate_paise=250),
        ],
    )
    posted, receipts = post_purchase(p_draft.id, actor_user_id=admin.id)
    assert get_item_stock_position(items["flour"].id).qty_milli == 160_000
    assert get_item_stock_position(items["flour"].id).value_paise == 690_000
    assert get_item_stock_position(items["pouches"].id).qty_milli == 100_000

    # Backdated cancellation earlier than receipt date is rejected.
    with pytest.raises(InvalidMovementError, match="Backdated"):
        cancel_purchase(
            posted.id,
            actor_user_id=admin.id,
            reason="Too early",
            cancellation_date=date(2026, 11, 23),
        )
    assert get_purchase(posted.id, actor_user_id=admin.id).status == "posted"

    # Omit cancellation_date -> uses get_business_today().
    monkeypatch.setattr("app.purchase_service.get_business_today", lambda: date(2026, 11, 25))
    cancelled_posted, reversals = cancel_purchase(
        posted.id,
        actor_user_id=admin.id,
        reason="Wrong invoice entry",
    )
    assert cancelled_posted.status == "cancelled"
    assert cancelled_posted.notes == "Original immutable note"  # Immutable header preserved!
    assert len(reversals) == 2
    assert all(r.movement_type == "reversal_out" and r.movement_date == date(2026, 11, 25) for r in reversals)
    assert {r.reverses_ledger_id for r in reversals} == {r.id for r in receipts}

    # Exact pre-purchase balances restored!
    flour_pos = get_item_stock_position(items["flour"].id)
    assert flour_pos.qty_milli == 100_000
    assert flour_pos.value_paise == 420_000
    assert flour_pos.wac_rupees == Decimal("42")
    pouch_pos = get_item_stock_position(items["pouches"].id)
    assert pouch_pos.qty_milli == 0
    assert pouch_pos.value_paise == 0

    # Audit log and linked ledger traceability
    all_linked = get_purchase_ledger_entries(posted.id, actor_user_id=admin.id)
    assert len(all_linked) == 4  # 2 receipts + 2 reversals
    cancel_audit = db_session.execute(
        db.select(AuditLog).where(
            AuditLog.entity_type == "purchase",
            AuditLog.entity_id == posted.id,
            AuditLog.action == "purchase_cancelled",
        )
    ).scalar_one()
    payload = json.loads(cancel_audit.new_values_json)
    assert payload["reason"] == "Wrong invoice entry"
    assert payload["cancellation_date"] == "2026-11-25"
    assert set(payload["reversed_ledger_ids"]) == {r.id for r in receipts}
    assert set(payload["reversal_ledger_ids"]) == {r.id for r in reversals}


def test_posted_cancellation_blocked_on_consumption_negative_value_and_rolls_back_atomically(
    db_session,
):
    admin = _create_user(db_session, username="admin_cancel_guard", role=ROLE_ADMIN)
    data = _seed_master_data(db_session)
    items = data["items"]
    supplier = data["suppliers"]["tn"]
    dt = date(2026, 11, 24)

    # Case 1: Multi-line posted purchase where line 1 (Flour) is untouched,
    # but line 2 (Pouches) has partial consumption -> line 2 fails InsufficientStockError
    # and line 1's reversal MUST roll back atomically!
    multi = create_draft_purchase(
        supplier_id=supplier.id,
        purchase_date=dt,
        actor_user_id=admin.id,
        lines=[
            PurchaseLineInput(item_id=items["flour"].id, entered_qty_milli=50_000, unit_rate_paise=4200),
            PurchaseLineInput(item_id=items["pouches"].id, entered_qty_milli=100_000, unit_rate_paise=250),
        ],
    )
    posted_multi, _ = post_purchase(multi.id, actor_user_id=admin.id)

    adj1 = _make_issue_adjustment(db_session, user_id=admin.id, number="ADJ-ISS-POUCH", dt=dt)
    post_stock_issue(
        item_id=items["pouches"].id,
        movement_date=dt,
        movement_type="adjustment_out",
        source_doc_type="stock_adjustment",
        source_doc_id=adj1.id,
        qty_milli=10_000,
        created_by=admin.id,
    )
    db_session.commit()

    with pytest.raises(InsufficientStockError, match="negative stock"):
        cancel_purchase(
            posted_multi.id,
            actor_user_id=admin.id,
            reason="Attempt cancel after consumption",
            cancellation_date=dt,
        )

    # Atomic rollback verified: status still 'posted', Flour still 50 kg, only 2 receipt ledger entries exist.
    assert get_purchase(posted_multi.id, actor_user_id=admin.id).status == "posted"
    assert get_item_stock_position(items["flour"].id).qty_milli == 50_000
    assert len(get_purchase_ledger_entries(posted_multi.id, actor_user_id=admin.id)) == 2

    # Case 2: Negative inventory VALUE edge case even when quantity remains >= 0:
    # Opening Oil: 10 L @ ₹100/L (₹1,000)
    # Purchase Oil: 10 L @ ₹200/L (₹2,000) -> 20 L valued at ₹3,000 (WAC ₹150/L)
    # Issue Oil: 10 L @ WAC ₹150/L (-₹1,500) -> remaining 10 L valued at ₹1,500
    # Reversing the ₹2,000 purchase leaves qty = 0 L but value = ₹1,500 - ₹2,000 = -₹500 -> BLOCKED!
    record_opening_stock(
        item_id=items["oil"].id,
        qty_milli=10_000,
        unit_cost_paise=10_000,
        movement_date=dt,
        created_by=admin.id,
    )
    oil_draft = create_draft_purchase(
        supplier_id=supplier.id,
        purchase_date=dt,
        actor_user_id=admin.id,
        lines=[PurchaseLineInput(item_id=items["oil"].id, entered_qty_milli=10_000, unit_rate_paise=20_000)],
    )
    posted_oil, _ = post_purchase(oil_draft.id, actor_user_id=admin.id)

    adj2 = _make_issue_adjustment(db_session, user_id=admin.id, number="ADJ-ISS-OIL", dt=dt)
    post_stock_issue(
        item_id=items["oil"].id,
        movement_date=dt,
        movement_type="adjustment_out",
        source_doc_type="stock_adjustment",
        source_doc_id=adj2.id,
        qty_milli=10_000,
        created_by=admin.id,
    )
    db_session.commit()

    assert get_item_stock_position(items["oil"].id).qty_milli == 10_000
    assert get_item_stock_position(items["oil"].id).value_paise == 150_000

    with pytest.raises(InsufficientStockError, match="negative inventory value"):
        cancel_purchase(
            posted_oil.id,
            actor_user_id=admin.id,
            reason="Would create negative inventory value",
            cancellation_date=dt,
        )

    assert get_purchase(posted_oil.id, actor_user_id=admin.id).status == "posted"
    assert get_item_stock_position(items["oil"].id).value_paise == 150_000


# ---------------------------------------------------------------------------
# 8. RBAC Across All Five Roles & Inactive/Missing Actor Enforcement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role,can_read,can_write",
    [
        (ROLE_ADMIN, True, True),
        (ROLE_OWNER, True, True),
        (ROLE_ACCOUNTANT, True, False),
        (ROLE_PRODUCTION_SUPERVISOR, False, False),
        (ROLE_SALESMAN, False, False),
    ],
)
def test_purchase_service_rbac_all_five_roles(db_session, role, can_read, can_write):
    seed_admin = _create_user(db_session, username="seed_admin_rbac", role=ROLE_ADMIN)
    actor = _create_user(db_session, username=f"actor_{role}", role=role)
    data = _seed_master_data(db_session)
    supplier = data["suppliers"]["tn"]
    flour = data["items"]["flour"]
    oil = data["items"]["oil"]
    dt = date(2026, 11, 24)

    seeded_draft = create_draft_purchase(
        supplier_id=supplier.id,
        purchase_date=dt,
        actor_user_id=seed_admin.id,
        lines=[PurchaseLineInput(item_id=flour.id, entered_qty_milli=10_000, unit_rate_paise=4200)],
    )
    seeded_line_id = seeded_draft.items[0].id

    # Read operations
    if can_read:
        assert len(list_purchases(actor_user_id=actor.id)) == 1
        assert get_purchase(seeded_draft.id, actor_user_id=actor.id).id == seeded_draft.id
        assert get_purchase_ledger_entries(seeded_draft.id, actor_user_id=actor.id) == []
    else:
        with pytest.raises(UnauthorizedPurchaseActionError):
            list_purchases(actor_user_id=actor.id)
        with pytest.raises(UnauthorizedPurchaseActionError):
            get_purchase(seeded_draft.id, actor_user_id=actor.id)
        with pytest.raises(UnauthorizedPurchaseActionError):
            get_purchase_ledger_entries(seeded_draft.id, actor_user_id=actor.id)

    # Write / post / cancel operations
    if can_write:
        own_draft = create_draft_purchase(
            supplier_id=supplier.id,
            purchase_date=dt,
            actor_user_id=actor.id,
            lines=[PurchaseLineInput(item_id=flour.id, entered_qty_milli=10_000, unit_rate_paise=4200)],
        )
        assert own_draft.created_by == actor.id
        update_draft_purchase(own_draft.id, actor_user_id=actor.id, notes="Updated by authorized actor")
        added = add_draft_purchase_item(
            own_draft.id,
            line=PurchaseLineInput(item_id=oil.id, entered_qty_milli=5_000, unit_rate_paise=14_000),
            actor_user_id=actor.id,
        )
        update_draft_purchase_item(
            own_draft.id,
            added.id,
            entered_qty_milli=6_000,
            unit_rate_paise=14_000,
            actor_user_id=actor.id,
        )
        remove_draft_purchase_item(own_draft.id, added.id, actor_user_id=actor.id)
        posted, _ = post_purchase(own_draft.id, actor_user_id=actor.id)
        assert posted.status == "posted"
        cancelled, _ = cancel_purchase(
            posted.id,
            actor_user_id=actor.id,
            reason="Authorized reversal",
            cancellation_date=dt,
        )
        assert cancelled.status == "cancelled"
    else:
        with pytest.raises(UnauthorizedPurchaseActionError):
            create_draft_purchase(
                supplier_id=supplier.id,
                purchase_date=dt,
                actor_user_id=actor.id,
            )
        with pytest.raises(UnauthorizedPurchaseActionError):
            update_draft_purchase(seeded_draft.id, actor_user_id=actor.id, notes="Denied")
        with pytest.raises(UnauthorizedPurchaseActionError):
            add_draft_purchase_item(
                seeded_draft.id,
                line=PurchaseLineInput(item_id=oil.id, entered_qty_milli=5_000, unit_rate_paise=14_000),
                actor_user_id=actor.id,
            )
        with pytest.raises(UnauthorizedPurchaseActionError):
            update_draft_purchase_item(
                seeded_draft.id,
                seeded_line_id,
                entered_qty_milli=20_000,
                unit_rate_paise=4200,
                actor_user_id=actor.id,
            )
        with pytest.raises(UnauthorizedPurchaseActionError):
            remove_draft_purchase_item(seeded_draft.id, seeded_line_id, actor_user_id=actor.id)
        with pytest.raises(UnauthorizedPurchaseActionError):
            post_purchase(seeded_draft.id, actor_user_id=actor.id)
        with pytest.raises(UnauthorizedPurchaseActionError):
            cancel_purchase(seeded_draft.id, actor_user_id=actor.id, reason="Denied")


def test_inactive_or_missing_actor_and_not_found_errors(db_session):
    inactive_admin = _create_user(
        db_session, username="inactive_admin", role=ROLE_ADMIN, is_active=False
    )
    active_admin = _create_user(db_session, username="active_admin", role=ROLE_ADMIN)
    data = _seed_master_data(db_session)

    for bad_actor_id in (inactive_admin.id, 999_999, 0, -1):
        with pytest.raises(UnauthorizedPurchaseActionError):
            list_purchases(actor_user_id=bad_actor_id)
        with pytest.raises(UnauthorizedPurchaseActionError):
            create_draft_purchase(
                supplier_id=data["suppliers"]["tn"].id,
                purchase_date=date(2026, 11, 24),
                actor_user_id=bad_actor_id,
            )

    with pytest.raises(PurchaseNotFoundError):
        get_purchase(999_999, actor_user_id=active_admin.id)
    with pytest.raises(PurchaseNotFoundError):
        post_purchase(999_999, actor_user_id=active_admin.id)
    with pytest.raises(PurchaseNotFoundError):
        cancel_purchase(999_999, actor_user_id=active_admin.id, reason="Missing")

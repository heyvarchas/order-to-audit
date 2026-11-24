"""Database initialization, schema, constraint, and SQLite trigger tests."""

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.db_init import init_db
from app.extensions import db
from app.models import (
    BOM,
    BOMItem,
    AuditLog,
    Customer,
    Dispatch,
    DispatchItem,
    Invoice,
    InvoiceItem,
    Item,
    Payment,
    ProductionConsumption,
    ProductionOverhead,
    ProductionRun,
    Purchase,
    PurchaseItem,
    SalesOrder,
    SalesOrderItem,
    StockAdjustment,
    StockLedger,
    Supplier,
    Unit,
    User,
)

EXPECTED_TABLES = {
    "users",
    "app_settings",
    "audit_logs",
    "customers",
    "suppliers",
    "units",
    "item_unit_conversions",
    "items",
    "boms",
    "bom_items",
    "purchases",
    "purchase_items",
    "stock_adjustments",
    "production_runs",
    "production_consumptions",
    "production_overheads",
    "stock_ledger",
    "sales_orders",
    "sales_order_items",
    "invoices",
    "invoice_items",
    "dispatches",
    "dispatch_items",
    "payments",
}

EXPECTED_TRIGGERS = {
    "trg_audit_logs_no_update",
    "trg_audit_logs_no_delete",
    "trg_stock_ledger_no_update",
    "trg_stock_ledger_no_delete",
    "trg_stock_ledger_validate_source_doc",
    "trg_stock_ledger_validate_reversal",
    "trg_stock_ledger_no_negative_balance",
    "trg_stock_adjustments_no_delete_if_referenced",
    "trg_purchases_no_delete_if_referenced",
    "trg_production_runs_no_delete_if_referenced",
    "trg_dispatches_no_delete_if_referenced",
    "trg_invoice_items_insert_draft_only",
    "trg_invoice_items_update_draft_only",
    "trg_invoice_items_delete_draft_only",
    "trg_invoices_no_delete_posted",
    "trg_invoices_lifecycle_and_immutability",
}

EXPECTED_PARTIAL_UNIQUE_INDEXES = {
    "uq_stock_ledger_single_reversal",
    "uq_stock_ledger_prod_posting",
    "uq_invoices_fy_type_seq",
    "uq_invoices_fy_type_num",
    "uq_invoices_number",
    "uq_invoices_active_order",
    "uq_dispatches_active_invoice",
}


# ---------------------------------------------------------------------------
# Helpers for building minimal valid fixture records in tests
# ---------------------------------------------------------------------------


def _make_base_records(session):
    """Insert minimal master records needed by transaction tests."""
    user = User(
        username="admin1",
        full_name="Factory Admin",
        email="admin@example.com",
        password_hash="hashed-password",
        role="admin",
    )
    kg = Unit(code="kg", name="Kilogram", dimension="mass", factor_to_base=1000)
    pkt = Unit(code="packet", name="Packet", dimension="count", factor_to_base=1)
    piece = Unit(code="piece", name="Piece", dimension="count", factor_to_base=1)
    session.add_all([user, kg, pkt, piece])
    session.flush()

    flour = Item(
        sku="RM-FLOUR",
        name="Flour",
        item_type="raw_material",
        stock_unit_id=kg.id,
        gst_rate_bps=0,
    )
    pouch = Item(
        sku="PKG-POUCH-200G",
        name="200 g Pouch",
        item_type="packaging",
        stock_unit_id=piece.id,
        gst_rate_bps=0,
    )
    murukku = Item(
        sku="FG-MURUKKU-200G",
        name="Murukku 200 g",
        item_type="finished_good",
        stock_unit_id=pkt.id,
        hsn_code="21069099",
        gst_rate_bps=500,
        default_sale_price_paise=5000,
    )
    customer = Customer(
        name="Retailer A",
        customer_type="retailer",
        gstin="33AAAAA0000A1Z5",
        state="Tamil Nadu",
    )
    supplier = Supplier(
        name="Mill Supplier",
        state="Tamil Nadu",
    )
    session.add_all([flour, pouch, murukku, customer, supplier])
    session.commit()
    return {
        "user": user,
        "kg": kg,
        "pkt": pkt,
        "piece": piece,
        "flour": flour,
        "pouch": pouch,
        "murukku": murukku,
        "customer": customer,
        "supplier": supplier,
    }


def _make_draft_invoice_with_line(session, data, *, qty_milli=150000, price_paise=5000):
    """Create a draft intra-state tax invoice with one 5% GST line."""
    taxable = (qty_milli * price_paise) // 1000
    cgst = (taxable * 250) // 10000
    sgst = (taxable * 250) // 10000
    total = taxable + cgst + sgst

    inv = Invoice(
        financial_year="2026-27",
        document_type="tax_invoice",
        customer_id=data["customer"].id,
        customer_name_snapshot=data["customer"].name,
        customer_gstin_snapshot=data["customer"].gstin,
        invoice_date=date(2026, 11, 23),
        place_of_supply="Tamil Nadu",
        gst_type="intra_state",
        taxable_value_paise=taxable,
        cgst_paise=cgst,
        sgst_paise=sgst,
        igst_paise=0,
        total_paise=total,
        status="draft",
        created_by=data["user"].id,
    )
    session.add(inv)
    session.flush()

    line = InvoiceItem(
        invoice_id=inv.id,
        item_id=data["murukku"].id,
        item_sku_snapshot=data["murukku"].sku,
        item_description_snapshot=data["murukku"].name,
        unit_code_snapshot="packet",
        hsn_code_snapshot=data["murukku"].hsn_code,
        qty_stock_milli=qty_milli,
        unit_price_paise=price_paise,
        taxable_value_paise=taxable,
        gst_rate_bps=500,
        cgst_rate_bps=250,
        sgst_rate_bps=250,
        igst_rate_bps=0,
        cgst_paise=cgst,
        sgst_paise=sgst,
        igst_paise=0,
        line_total_paise=total,
    )
    session.add(line)
    session.commit()
    return inv, line


def _post_invoice(session, inv, seq=1, number="INV/2026-27/0001"):
    inv.sequence_no = seq
    inv.invoice_number = number
    inv.posted_at = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc).replace(
        tzinfo=None
    )
    inv.status = "posted"
    session.commit()


# ---------------------------------------------------------------------------
# 1. Database Initialization & Schema Verification
# ---------------------------------------------------------------------------


def test_init_db_creates_all_24_tables_and_is_idempotent(app, db_session):
    inspector = inspect(db.engine)
    table_names = set(inspector.get_table_names())
    assert table_names == EXPECTED_TABLES
    assert len(table_names) == 24

    # Calling init_db a second time should succeed cleanly without errors.
    init_db(app)
    assert set(inspect(db.engine).get_table_names()) == EXPECTED_TABLES


def test_no_floating_point_columns_in_schema(db_session):
    inspector = inspect(db.engine)
    for table_name in EXPECTED_TABLES:
        for col in inspector.get_columns(table_name):
            col_type = str(col["type"]).upper()
            assert "FLOAT" not in col_type
            assert "REAL" not in col_type
            assert "DOUBLE" not in col_type


def test_sqlite_triggers_and_partial_unique_indexes_are_installed(db_session):
    triggers = {
        row[0]
        for row in db_session.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        ).fetchall()
    }
    assert EXPECTED_TRIGGERS.issubset(triggers)

    indexes = {
        row[0]
        for row in db_session.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'index'")
        ).fetchall()
    }
    assert EXPECTED_PARTIAL_UNIQUE_INDEXES.issubset(indexes)


def test_cli_init_db_command_installs_tables_indexes_and_triggers(app):
    # App startup must not initialize the database automatically.
    with app.app_context():
        assert inspect(db.engine).get_table_names() == []

    runner = app.test_cli_runner()
    result = runner.invoke(args=["init-db"])
    assert result.exit_code == 0
    assert "Initialized the database." in result.output

    with app.app_context():
        table_names = set(inspect(db.engine).get_table_names())
        assert table_names == EXPECTED_TABLES

        triggers = {
            row[0]
            for row in db.session.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            ).fetchall()
        }
        assert EXPECTED_TRIGGERS.issubset(triggers)

        indexes = {
            row[0]
            for row in db.session.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'index'")
            ).fetchall()
        }
        assert EXPECTED_PARTIAL_UNIQUE_INDEXES.issubset(indexes)

        db.session.remove()
        db.drop_all()


# ---------------------------------------------------------------------------
# 2. Foreign Key Enforcement
# ---------------------------------------------------------------------------


def test_sqlite_foreign_keys_are_enforced(db_session):
    pragma_val = db_session.execute(text("PRAGMA foreign_keys")).scalar()
    assert pragma_val == 1

    bad_item = Item(
        sku="BAD-SKU",
        name="Orphan Item",
        item_type="raw_material",
        stock_unit_id=999999,
    )
    db_session.add(bad_item)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# ---------------------------------------------------------------------------
# 3. Stock Ledger Polymorphic Source Validation & Sign Conventions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "movement_type,source_doc_type,qty,value",
    [
        ("opening_stock", "stock_adjustment", 100000, 420000),
        ("purchase_receipt", "purchase", 60000, 270000),
        ("production_issue", "production_run", -40000, -172500),
        ("sales_dispatch", "dispatch", -150000, -463550),
    ],
)
def test_ledger_rejects_nonexistent_source_document(
    db_session, movement_type, source_doc_type, qty, value
):
    data = _make_base_records(db_session)
    entry = StockLedger(
        item_id=data["flour"].id,
        movement_date=date(2026, 11, 23),
        movement_type=movement_type,
        source_doc_type=source_doc_type,
        source_doc_id=999999,
        qty_change_milli=qty,
        unit_cost_paise=4200,
        value_change_paise=value,
        created_by=data["user"].id,
    )
    db_session.add(entry)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_ledger_rejects_invalid_source_doc_type_and_bad_signs(db_session):
    data = _make_base_records(db_session)
    adj = StockAdjustment(
        adjustment_number="ADJ-001",
        adjustment_type="opening_stock",
        adjustment_date=date(2026, 11, 23),
        reason="Opening stock",
        created_by=data["user"].id,
    )
    db_session.add(adj)
    db_session.commit()

    # Invalid source_doc_type
    bad_type = StockLedger(
        item_id=data["flour"].id,
        movement_date=date(2026, 11, 23),
        movement_type="opening_stock",
        source_doc_type="unknown_doc",
        source_doc_id=adj.id,
        qty_change_milli=100000,
        unit_cost_paise=4200,
        value_change_paise=420000,
        created_by=data["user"].id,
    )
    db_session.add(bad_type)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # Negative quantity on an inward movement violates sign convention
    bad_sign = StockLedger(
        item_id=data["flour"].id,
        movement_date=date(2026, 11, 23),
        movement_type="opening_stock",
        source_doc_type="stock_adjustment",
        source_doc_id=adj.id,
        qty_change_milli=-100000,
        unit_cost_paise=4200,
        value_change_paise=-420000,
        created_by=data["user"].id,
    )
    db_session.add(bad_sign)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_ledger_prevents_duplicate_production_issue_but_allows_used_plus_damaged(
    db_session,
):
    data = _make_base_records(db_session)
    bom = BOM(
        finished_item_id=data["murukku"].id,
        version=1,
        name="Murukku Standard BOM",
        expected_yield_milli=200000,
    )
    db_session.add(bom)
    db_session.flush()

    run = ProductionRun(
        batch_number="BATCH-001",
        bom_id=bom.id,
        finished_item_id=data["murukku"].id,
        run_date=date(2026, 11, 23),
        planned_output_milli=200000,
        created_by=data["user"].id,
    )
    opening_adj = StockAdjustment(
        adjustment_number="ADJ-POUCH-OPEN",
        adjustment_type="opening_stock",
        adjustment_date=date(2026, 11, 23),
        reason="Opening pouch stock",
        created_by=data["user"].id,
    )
    db_session.add_all([run, opening_adj])
    db_session.flush()

    opening_entry = StockLedger(
        item_id=data["pouch"].id,
        movement_date=date(2026, 11, 23),
        movement_type="opening_stock",
        source_doc_type="stock_adjustment",
        source_doc_id=opening_adj.id,
        qty_change_milli=500000,
        unit_cost_paise=250,
        value_change_paise=125000,
        created_by=data["user"].id,
    )
    db_session.add(opening_entry)
    db_session.commit()

    # 188 used + 6 damaged as two disjoint ledger entries must succeed.
    used_entry = StockLedger(
        item_id=data["pouch"].id,
        movement_date=date(2026, 11, 23),
        movement_type="production_issue",
        source_doc_type="production_run",
        source_doc_id=run.id,
        qty_change_milli=-188000,
        unit_cost_paise=250,
        value_change_paise=-47000,
        created_by=data["user"].id,
    )
    damaged_entry = StockLedger(
        item_id=data["pouch"].id,
        movement_date=date(2026, 11, 23),
        movement_type="packaging_damage",
        source_doc_type="production_run",
        source_doc_id=run.id,
        qty_change_milli=-6000,
        unit_cost_paise=250,
        value_change_paise=-1500,
        created_by=data["user"].id,
    )
    db_session.add_all([used_entry, damaged_entry])
    db_session.commit()

    # A second production_issue for the same run and item must be rejected.
    dup_issue = StockLedger(
        item_id=data["pouch"].id,
        movement_date=date(2026, 11, 23),
        movement_type="production_issue",
        source_doc_type="production_run",
        source_doc_id=run.id,
        qty_change_milli=-6000,
        unit_cost_paise=250,
        value_change_paise=-1500,
        created_by=data["user"].id,
    )
    db_session.add(dup_issue)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# ---------------------------------------------------------------------------
# 4. Stock Ledger & Audit Log Immutability + Source Delete Guard
# ---------------------------------------------------------------------------


def test_ledger_updates_deletes_and_referenced_source_deletes_are_rejected(db_session):
    data = _make_base_records(db_session)
    adj = StockAdjustment(
        adjustment_number="ADJ-OPEN-01",
        adjustment_type="opening_stock",
        adjustment_date=date(2026, 11, 23),
        reason="Opening flour stock",
        created_by=data["user"].id,
    )
    db_session.add(adj)
    db_session.flush()

    entry = StockLedger(
        item_id=data["flour"].id,
        movement_date=date(2026, 11, 23),
        movement_type="opening_stock",
        source_doc_type="stock_adjustment",
        source_doc_id=adj.id,
        qty_change_milli=100000,
        unit_cost_paise=4200,
        value_change_paise=420000,
        created_by=data["user"].id,
    )
    db_session.add(entry)
    db_session.commit()

    # UPDATE on stock_ledger is rejected
    entry.qty_change_milli = 90000
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # DELETE on stock_ledger is rejected
    db_session.delete(entry)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # Deleting the referenced StockAdjustment is also rejected
    db_session.delete(adj)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_audit_log_updates_and_deletes_are_rejected(db_session):
    data = _make_base_records(db_session)
    log = AuditLog(
        user_id=data["user"].id,
        action="test_action",
        entity_type="item",
        entity_id=data["flour"].id,
        summary="Created flour item",
    )
    db_session.add(log)
    db_session.commit()

    log.summary = "Tampered summary"
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    db_session.delete(log)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# ---------------------------------------------------------------------------
# 5. Reversal Integrity (Exact Negation, Direction, Single Reversal, No Chain)
# ---------------------------------------------------------------------------


def test_ledger_reversal_must_exactly_negate_and_match_direction(db_session):
    data = _make_base_records(db_session)
    purchase = Purchase(
        purchase_number="PUR-001",
        supplier_id=data["supplier"].id,
        purchase_date=date(2026, 11, 23),
        subtotal_paise=270000,
        total_paise=270000,
        created_by=data["user"].id,
    )
    db_session.add(purchase)
    db_session.flush()

    receipt = StockLedger(
        item_id=data["flour"].id,
        movement_date=date(2026, 11, 23),
        movement_type="purchase_receipt",
        source_doc_type="purchase",
        source_doc_id=purchase.id,
        qty_change_milli=60000,
        unit_cost_paise=4500,
        value_change_paise=270000,
        created_by=data["user"].id,
    )
    db_session.add(receipt)
    db_session.commit()

    # Partial quantity negation is rejected
    bad_qty_rev = StockLedger(
        item_id=data["flour"].id,
        movement_date=date(2026, 11, 23),
        movement_type="reversal_out",
        source_doc_type="purchase",
        source_doc_id=purchase.id,
        reverses_ledger_id=receipt.id,
        qty_change_milli=-50000,
        unit_cost_paise=4500,
        value_change_paise=-270000,
        created_by=data["user"].id,
    )
    db_session.add(bad_qty_rev)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # Wrong value negation is rejected
    bad_val_rev = StockLedger(
        item_id=data["flour"].id,
        movement_date=date(2026, 11, 23),
        movement_type="reversal_out",
        source_doc_type="purchase",
        source_doc_id=purchase.id,
        reverses_ledger_id=receipt.id,
        qty_change_milli=-60000,
        unit_cost_paise=4500,
        value_change_paise=-260000,
        created_by=data["user"].id,
    )
    db_session.add(bad_val_rev)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # Wrong direction (reversal_in on a positive original) is rejected
    bad_dir_rev = StockLedger(
        item_id=data["flour"].id,
        movement_date=date(2026, 11, 23),
        movement_type="reversal_in",
        source_doc_type="purchase",
        source_doc_id=purchase.id,
        reverses_ledger_id=receipt.id,
        qty_change_milli=60000,
        unit_cost_paise=4500,
        value_change_paise=270000,
        created_by=data["user"].id,
    )
    db_session.add(bad_dir_rev)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # Wrong item_id is rejected
    bad_item_rev = StockLedger(
        item_id=data["pouch"].id,
        movement_date=date(2026, 11, 23),
        movement_type="reversal_out",
        source_doc_type="purchase",
        source_doc_id=purchase.id,
        reverses_ledger_id=receipt.id,
        qty_change_milli=-60000,
        unit_cost_paise=4500,
        value_change_paise=-270000,
        created_by=data["user"].id,
    )
    db_session.add(bad_item_rev)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # Exact matching reversal succeeds
    valid_rev = StockLedger(
        item_id=data["flour"].id,
        movement_date=date(2026, 11, 23),
        movement_type="reversal_out",
        source_doc_type="purchase",
        source_doc_id=purchase.id,
        reverses_ledger_id=receipt.id,
        qty_change_milli=-60000,
        unit_cost_paise=4500,
        value_change_paise=-270000,
        created_by=data["user"].id,
    )
    db_session.add(valid_rev)
    db_session.commit()

    # Duplicate reversal of the same original row is rejected
    dup_rev = StockLedger(
        item_id=data["flour"].id,
        movement_date=date(2026, 11, 23),
        movement_type="reversal_out",
        source_doc_type="purchase",
        source_doc_id=purchase.id,
        reverses_ledger_id=receipt.id,
        qty_change_milli=-60000,
        unit_cost_paise=4500,
        value_change_paise=-270000,
        created_by=data["user"].id,
    )
    db_session.add(dup_rev)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # Reversal of a reversal is rejected
    rev_of_rev = StockLedger(
        item_id=data["flour"].id,
        movement_date=date(2026, 11, 23),
        movement_type="reversal_in",
        source_doc_type="purchase",
        source_doc_id=purchase.id,
        reverses_ledger_id=valid_rev.id,
        qty_change_milli=60000,
        unit_cost_paise=4500,
        value_change_paise=270000,
        created_by=data["user"].id,
    )
    db_session.add(rev_of_rev)
    with pytest.raises(IntegrityError, match="Cannot reverse a reversal ledger entry"):
        db_session.commit()
    db_session.rollback()

    # Exact reversal_in on a negative outward movement also succeeds (seed stock first)
    seed_adj = StockAdjustment(
        adjustment_number="ADJ-OPEN-SEED",
        adjustment_type="opening_stock",
        adjustment_date=date(2026, 11, 23),
        reason="Seed stock for outward test",
        created_by=data["user"].id,
    )
    adj = StockAdjustment(
        adjustment_number="ADJ-OUT-01",
        adjustment_type="spoilage_writeoff",
        adjustment_date=date(2026, 11, 23),
        reason="Test writeoff",
        created_by=data["user"].id,
    )
    db_session.add_all([seed_adj, adj])
    db_session.flush()
    seed_in = StockLedger(
        item_id=data["flour"].id,
        movement_date=date(2026, 11, 23),
        movement_type="opening_stock",
        source_doc_type="stock_adjustment",
        source_doc_id=seed_adj.id,
        qty_change_milli=10000,
        unit_cost_paise=4500,
        value_change_paise=45000,
        created_by=data["user"].id,
    )
    db_session.add(seed_in)
    db_session.flush()
    out_entry = StockLedger(
        item_id=data["flour"].id,
        movement_date=date(2026, 11, 23),
        movement_type="adjustment_out",
        source_doc_type="stock_adjustment",
        source_doc_id=adj.id,
        qty_change_milli=-5000,
        unit_cost_paise=4500,
        value_change_paise=-22500,
        created_by=data["user"].id,
    )
    db_session.add(out_entry)
    db_session.commit()

    valid_rev_in = StockLedger(
        item_id=data["flour"].id,
        movement_date=date(2026, 11, 23),
        movement_type="reversal_in",
        source_doc_type="stock_adjustment",
        source_doc_id=adj.id,
        reverses_ledger_id=out_entry.id,
        qty_change_milli=5000,
        unit_cost_paise=4500,
        value_change_paise=22500,
        created_by=data["user"].id,
    )
    db_session.add(valid_rev_in)
    db_session.commit()
    assert valid_rev_in.reverses_ledger_id == out_entry.id


# ---------------------------------------------------------------------------
# 6. Invoice Draft/Posting Lifecycle, Total Validation & Immutability
# ---------------------------------------------------------------------------


def test_invoice_lines_editable_only_in_draft_status(db_session):
    data = _make_base_records(db_session)
    inv, line = _make_draft_invoice_with_line(db_session, data)

    # Updating a line while draft is allowed
    line.qty_stock_milli = 100000
    line.taxable_value_paise = 500000
    line.cgst_paise = 12500
    line.sgst_paise = 12500
    line.line_total_paise = 525000
    db_session.commit()

    # Deleting a line while draft is allowed
    db_session.delete(line)
    db_session.commit()

    # Inserting a replacement line while draft is allowed
    new_line = InvoiceItem(
        invoice_id=inv.id,
        item_id=data["murukku"].id,
        item_sku_snapshot="FG-MURUKKU-200G",
        item_description_snapshot="Murukku 200 g",
        unit_code_snapshot="packet",
        hsn_code_snapshot="21069099",
        qty_stock_milli=150000,
        unit_price_paise=5000,
        taxable_value_paise=750000,
        gst_rate_bps=500,
        cgst_rate_bps=250,
        sgst_rate_bps=250,
        igst_rate_bps=0,
        cgst_paise=18750,
        sgst_paise=18750,
        igst_paise=0,
        line_total_paise=787500,
    )
    db_session.add(new_line)
    db_session.commit()
    assert len(inv.items) == 1


def test_invalid_invoice_posting_totals_and_empty_invoice_are_rejected(db_session):
    data = _make_base_records(db_session)

    # 1. Posting an invoice with zero lines must fail
    empty_inv = Invoice(
        financial_year="2026-27",
        document_type="tax_invoice",
        customer_id=data["customer"].id,
        customer_name_snapshot=data["customer"].name,
        invoice_date=date(2026, 11, 23),
        place_of_supply="Tamil Nadu",
        gst_type="intra_state",
        status="draft",
        created_by=data["user"].id,
    )
    db_session.add(empty_inv)
    db_session.commit()

    with pytest.raises(
        IntegrityError, match="Cannot post an invoice without at least one line item"
    ):
        _post_invoice(db_session, empty_inv, seq=1, number="INV/2026-27/0001")
    db_session.rollback()

    # 2. Posting an invoice whose header totals do not match line sums must fail
    inv, _line = _make_draft_invoice_with_line(db_session, data)
    cgst = inv.cgst_paise
    sgst = inv.sgst_paise
    inv.taxable_value_paise = 700000  # Line has 750000
    inv.total_paise = 700000 + cgst + sgst
    with pytest.raises(
        IntegrityError, match="Invoice header totals do not match sum of invoice lines"
    ):
        _post_invoice(db_session, inv, seq=1, number="INV/2026-27/0001")
    db_session.rollback()


def test_posted_invoice_header_and_lines_are_immutable_and_credit_note_works(db_session):
    data = _make_base_records(db_session)
    inv, line = _make_draft_invoice_with_line(db_session, data)
    _post_invoice(db_session, inv, seq=1, number="INV/2026-27/0001")

    # Inserting a new line into a posted invoice is rejected
    extra_line = InvoiceItem(
        invoice_id=inv.id,
        item_id=data["murukku"].id,
        item_sku_snapshot="FG-MURUKKU-200G",
        item_description_snapshot="Murukku 200 g",
        unit_code_snapshot="packet",
        hsn_code_snapshot="21069099",
        qty_stock_milli=10000,
        unit_price_paise=5000,
        taxable_value_paise=50000,
        gst_rate_bps=500,
        cgst_rate_bps=250,
        sgst_rate_bps=250,
        igst_rate_bps=0,
        cgst_paise=1250,
        sgst_paise=1250,
        igst_paise=0,
        line_total_paise=52500,
    )
    db_session.add(extra_line)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # Updating a posted invoice line is rejected
    line = db_session.get(InvoiceItem, line.id)
    line.item_description_snapshot = "Altered Name"
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # Deleting a posted invoice line is rejected
    line = db_session.get(InvoiceItem, line.id)
    db_session.delete(line)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # Updating correction_notes while a posted invoice stays posted is allowed
    inv = db_session.get(Invoice, inv.id)
    inv.correction_notes = "Customer requested address note check"
    db_session.commit()
    assert inv.status == "posted"
    assert inv.correction_notes == "Customer requested address note check"

    # Updating immutable posted invoice header fields is rejected (even when staying posted)
    inv = db_session.get(Invoice, inv.id)
    inv.customer_name_snapshot = "Tampered Customer"
    with pytest.raises(IntegrityError, match="Posted invoice header fields are immutable"):
        db_session.commit()
    db_session.rollback()

    inv = db_session.get(Invoice, inv.id)
    inv.invoice_number = "INV/2026-27/9999"
    with pytest.raises(IntegrityError, match="Posted invoice header fields are immutable"):
        db_session.commit()
    db_session.rollback()

    # Reverting posted -> draft is rejected
    inv = db_session.get(Invoice, inv.id)
    inv.status = "draft"
    inv.sequence_no = None
    inv.invoice_number = None
    inv.posted_at = None
    with pytest.raises(IntegrityError, match="Invalid invoice status transition"):
        db_session.commit()
    db_session.rollback()

    # Draft -> credited directly is rejected
    draft_inv, _ = _make_draft_invoice_with_line(db_session, data)
    draft_inv.sequence_no = 99
    draft_inv.invoice_number = "INV/2026-27/0099"
    draft_inv.posted_at = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc).replace(
        tzinfo=None
    )
    draft_inv.status = "credited"
    with pytest.raises(IntegrityError, match="Invalid invoice status transition"):
        db_session.commit()
    db_session.rollback()

    # Deleting a posted invoice is rejected
    inv = db_session.get(Invoice, inv.id)
    db_session.delete(inv)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # Marking posted -> credited without a posted credit note is rejected
    inv = db_session.get(Invoice, inv.id)
    inv.status = "credited"
    with pytest.raises(
        IntegrityError,
        match="Only a posted tax invoice with a posted credit note can be marked credited",
    ):
        db_session.commit()
    db_session.rollback()

    # Issuing and posting a linked credit note allows marking original invoice credited
    cn = Invoice(
        financial_year="2026-27",
        document_type="credit_note",
        original_invoice_id=inv.id,
        customer_id=data["customer"].id,
        customer_name_snapshot=data["customer"].name,
        customer_gstin_snapshot=data["customer"].gstin,
        invoice_date=date(2026, 11, 24),
        place_of_supply="Tamil Nadu",
        gst_type="intra_state",
        taxable_value_paise=750000,
        cgst_paise=18750,
        sgst_paise=18750,
        igst_paise=0,
        total_paise=787500,
        status="draft",
        created_by=data["user"].id,
    )
    db_session.add(cn)
    db_session.flush()
    cn_line = InvoiceItem(
        invoice_id=cn.id,
        item_id=data["murukku"].id,
        item_sku_snapshot="FG-MURUKKU-200G",
        item_description_snapshot="Murukku 200 g",
        unit_code_snapshot="packet",
        hsn_code_snapshot="21069099",
        qty_stock_milli=150000,
        unit_price_paise=5000,
        taxable_value_paise=750000,
        gst_rate_bps=500,
        cgst_rate_bps=250,
        sgst_rate_bps=250,
        igst_rate_bps=0,
        cgst_paise=18750,
        sgst_paise=18750,
        igst_paise=0,
        line_total_paise=787500,
    )
    db_session.add(cn_line)
    db_session.commit()

    # Sequence 1 is valid for credit_note in the same FY as tax_invoice seq 1.
    _post_invoice(db_session, cn, seq=1, number="CN/2026-27/0001")

    inv = db_session.get(Invoice, inv.id)
    inv.status = "credited"
    inv.correction_notes = "Full credit note CN/2026-27/0001 issued"
    db_session.commit()
    assert inv.status == "credited"

    # Once credited, even correction_notes cannot be edited anymore
    inv.correction_notes = "Further edit"
    with pytest.raises(IntegrityError, match="Invalid invoice status transition"):
        db_session.commit()
    db_session.rollback()

    # Credited invoice cannot transition back to posted or draft either
    inv = db_session.get(Invoice, inv.id)
    inv.status = "posted"
    with pytest.raises(IntegrityError, match="Invalid invoice status transition"):
        db_session.commit()
    db_session.rollback()


def test_invoice_numbering_uniqueness_within_fy_and_document_type(db_session):
    data = _make_base_records(db_session)
    inv1, _ = _make_draft_invoice_with_line(db_session, data)
    _post_invoice(db_session, inv1, seq=1, number="INV/2026-27/0001")

    inv2, _ = _make_draft_invoice_with_line(db_session, data)
    with pytest.raises(IntegrityError):
        _post_invoice(db_session, inv2, seq=1, number="INV/2026-27/0001-DUP")
    db_session.rollback()


def test_purchase_optional_gst_defaults_to_zero(db_session):
    data = _make_base_records(db_session)
    purchase = Purchase(
        purchase_number="PUR-FLOUR-001",
        supplier_id=data["supplier"].id,
        purchase_date=date(2026, 11, 23),
        subtotal_paise=270000,
        total_paise=270000,
        created_by=data["user"].id,
    )
    db_session.add(purchase)
    db_session.flush()

    item = PurchaseItem(
        purchase_id=purchase.id,
        item_id=data["flour"].id,
        entered_unit_id=data["kg"].id,
        entered_qty_milli=60000,
        qty_stock_milli=60000,
        unit_rate_paise=4500,
        taxable_value_paise=270000,
        line_total_paise=270000,
    )
    db_session.add(item)
    db_session.commit()

    assert purchase.gst_type == "none"
    assert purchase.cgst_paise == 0
    assert purchase.sgst_paise == 0
    assert purchase.igst_paise == 0
    assert item.gst_rate_bps == 0
    assert item.cgst_paise == 0
    assert item.sgst_paise == 0
    assert item.igst_paise == 0


def test_ledger_trigger_rejects_negative_quantity_and_value(db_session):
    data = _make_base_records(db_session)
    adj_in = StockAdjustment(
        adjustment_number="ADJ-NEG-OPEN",
        adjustment_type="opening_stock",
        adjustment_date=date(2026, 11, 23),
        reason="Opening flour",
        created_by=data["user"].id,
    )
    adj_out = StockAdjustment(
        adjustment_number="ADJ-NEG-OUT",
        adjustment_type="spoilage_writeoff",
        adjustment_date=date(2026, 11, 23),
        reason="Excess writeoff",
        created_by=data["user"].id,
    )
    db_session.add_all([adj_in, adj_out])
    db_session.flush()

    inward = StockLedger(
        item_id=data["flour"].id,
        movement_date=date(2026, 11, 23),
        movement_type="opening_stock",
        source_doc_type="stock_adjustment",
        source_doc_id=adj_in.id,
        qty_change_milli=10000,
        unit_cost_paise=4200,
        value_change_paise=42000,
        created_by=data["user"].id,
    )
    db_session.add(inward)
    db_session.commit()

    # Outward quantity exceeding on-hand balance must abort via trigger.
    bad_qty = StockLedger(
        item_id=data["flour"].id,
        movement_date=date(2026, 11, 23),
        movement_type="adjustment_out",
        source_doc_type="stock_adjustment",
        source_doc_id=adj_out.id,
        qty_change_milli=-11000,
        unit_cost_paise=4200,
        value_change_paise=-42000,
        created_by=data["user"].id,
    )
    db_session.add(bad_qty)
    with pytest.raises(IntegrityError, match="negative inventory quantity"):
        db_session.commit()
    db_session.rollback()

    # Outward value exceeding on-hand value must abort via trigger.
    bad_val = StockLedger(
        item_id=data["flour"].id,
        movement_date=date(2026, 11, 23),
        movement_type="adjustment_out",
        source_doc_type="stock_adjustment",
        source_doc_id=adj_out.id,
        qty_change_milli=-5000,
        unit_cost_paise=9000,
        value_change_paise=-45000,
        created_by=data["user"].id,
    )
    db_session.add(bad_val)
    with pytest.raises(IntegrityError, match="negative inventory value"):
        db_session.commit()
    db_session.rollback()

    # Exact depletion to zero qty and zero value is allowed.
    exact_out = StockLedger(
        item_id=data["flour"].id,
        movement_date=date(2026, 11, 23),
        movement_type="adjustment_out",
        source_doc_type="stock_adjustment",
        source_doc_id=adj_out.id,
        qty_change_milli=-10000,
        unit_cost_paise=4200,
        value_change_paise=-42000,
        created_by=data["user"].id,
    )
    db_session.add(exact_out)
    db_session.commit()

    # Reversing original inward after depletion would produce negative stock -> rejected.
    bad_rev = StockLedger(
        item_id=data["flour"].id,
        movement_date=date(2026, 11, 23),
        movement_type="reversal_out",
        source_doc_type="stock_adjustment",
        source_doc_id=adj_in.id,
        reverses_ledger_id=inward.id,
        qty_change_milli=-10000,
        unit_cost_paise=4200,
        value_change_paise=-42000,
        created_by=data["user"].id,
    )
    db_session.add(bad_rev)
    with pytest.raises(IntegrityError, match="negative inventory quantity"):
        db_session.commit()
    db_session.rollback()

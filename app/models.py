"""Relational database models for Order-to-Audit.

All monetary values are stored as integer paise (rupees * 100),
quantities as integer thousandths of the item's stock unit (qty * 1000),
and tax/variance percentages as integer basis points (percent * 100).
"""

from datetime import datetime, timezone

from flask_login import UserMixin
from sqlalchemy import CheckConstraint, Index, UniqueConstraint, text
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.permissions import VALID_ROLES

_USER_ROLES_SQL = ", ".join(f"'{role}'" for role in VALID_ROLES)


def utcnow():
    """Return a naive UTC timestamp without using deprecated datetime.utcnow()."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# A. Users, Configuration & Audit
# ---------------------------------------------------------------------------


class User(UserMixin, db.Model):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            f"role IN ({_USER_ROLES_SQL})",
            name="ck_users_role",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    full_name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(30), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    def set_password(self, password: str) -> None:
        """Hash and store the user's password securely (never plaintext)."""
        if not password:
            raise ValueError("Password must not be empty.")
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        """Verify a candidate password against the stored password hash."""
        if not password or not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)


class AppSetting(db.Model):
    __tablename__ = "app_settings"

    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
        Index("ix_audit_logs_created_at", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    action = db.Column(db.String(80), nullable=False)
    entity_type = db.Column(db.String(80), nullable=False)
    entity_id = db.Column(db.Integer, nullable=False)
    summary = db.Column(db.Text, nullable=False)
    old_values_json = db.Column(db.Text, nullable=True)
    new_values_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    user = db.relationship("User", foreign_keys=[user_id])


# ---------------------------------------------------------------------------
# B. Master Data (Parties, Units, Items, BOMs)
# ---------------------------------------------------------------------------


class Customer(db.Model):
    __tablename__ = "customers"
    __table_args__ = (
        CheckConstraint(
            "customer_type IN ('retailer', 'distributor')",
            name="ck_customers_type",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    customer_type = db.Column(db.String(30), nullable=False)
    gstin = db.Column(db.String(20), nullable=True)
    state = db.Column(db.String(80), nullable=False)
    address = db.Column(db.Text, nullable=True)
    phone = db.Column(db.String(30), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class Supplier(db.Model):
    __tablename__ = "suppliers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    gstin = db.Column(db.String(20), nullable=True)
    state = db.Column(db.String(80), nullable=False)
    address = db.Column(db.Text, nullable=True)
    phone = db.Column(db.String(30), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class Unit(db.Model):
    __tablename__ = "units"
    __table_args__ = (
        CheckConstraint(
            "dimension IN ('mass', 'volume', 'count')",
            name="ck_units_dimension",
        ),
        CheckConstraint("factor_to_base > 0", name="ck_units_factor_positive"),
    )

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(60), nullable=False)
    dimension = db.Column(db.String(20), nullable=False)
    base_unit_id = db.Column(db.Integer, db.ForeignKey("units.id"), nullable=True)
    factor_to_base = db.Column(db.Integer, nullable=False, default=1)

    base_unit = db.relationship("Unit", remote_side=[id], foreign_keys=[base_unit_id])


class Item(db.Model):
    __tablename__ = "items"
    __table_args__ = (
        CheckConstraint(
            "item_type IN ('raw_material', 'packaging', 'finished_good')",
            name="ck_items_type",
        ),
        CheckConstraint("gst_rate_bps >= 0", name="ck_items_gst_rate_nonneg"),
        CheckConstraint(
            "default_sale_price_paise IS NULL OR default_sale_price_paise >= 0",
            name="ck_items_sale_price_nonneg",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    sku = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(150), nullable=False)
    item_type = db.Column(db.String(30), nullable=False)
    stock_unit_id = db.Column(db.Integer, db.ForeignKey("units.id"), nullable=False)
    hsn_code = db.Column(db.String(20), nullable=True)
    gst_rate_bps = db.Column(db.Integer, nullable=False, default=0)
    default_sale_price_paise = db.Column(db.Integer, nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    stock_unit = db.relationship("Unit", foreign_keys=[stock_unit_id])
    unit_conversions = db.relationship(
        "ItemUnitConversion",
        back_populates="item",
        cascade="all, delete-orphan",
    )


class ItemUnitConversion(db.Model):
    __tablename__ = "item_unit_conversions"
    __table_args__ = (
        UniqueConstraint(
            "item_id",
            "from_unit_id",
            "to_unit_id",
            name="uq_item_unit_conv",
        ),
        CheckConstraint("multiplier > 0", name="ck_item_unit_conv_multiplier_positive"),
        CheckConstraint(
            "from_unit_id != to_unit_id",
            name="ck_item_unit_conv_distinct_units",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    from_unit_id = db.Column(db.Integer, db.ForeignKey("units.id"), nullable=False)
    to_unit_id = db.Column(db.Integer, db.ForeignKey("units.id"), nullable=False)
    multiplier = db.Column(db.Integer, nullable=False)

    item = db.relationship("Item", back_populates="unit_conversions")
    from_unit = db.relationship("Unit", foreign_keys=[from_unit_id])
    to_unit = db.relationship("Unit", foreign_keys=[to_unit_id])


class BOM(db.Model):
    __tablename__ = "boms"
    __table_args__ = (
        UniqueConstraint("finished_item_id", "version", name="uq_boms_item_version"),
        CheckConstraint("version > 0", name="ck_boms_version_positive"),
        CheckConstraint(
            "expected_yield_milli > 0",
            name="ck_boms_expected_yield_positive",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    finished_item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    version = db.Column(db.Integer, nullable=False, default=1)
    name = db.Column(db.String(150), nullable=False)
    expected_yield_milli = db.Column(db.Integer, nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    finished_item = db.relationship("Item", foreign_keys=[finished_item_id])
    items = db.relationship(
        "BOMItem",
        back_populates="bom",
        cascade="all, delete-orphan",
    )


class BOMItem(db.Model):
    __tablename__ = "bom_items"
    __table_args__ = (
        UniqueConstraint("bom_id", "material_item_id", name="uq_bom_items_bom_material"),
        CheckConstraint("standard_qty_milli > 0", name="ck_bom_items_qty_positive"),
    )

    id = db.Column(db.Integer, primary_key=True)
    bom_id = db.Column(
        db.Integer,
        db.ForeignKey("boms.id", ondelete="CASCADE"),
        nullable=False,
    )
    material_item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    input_unit_id = db.Column(db.Integer, db.ForeignKey("units.id"), nullable=False)
    standard_qty_milli = db.Column(db.Integer, nullable=False)

    bom = db.relationship("BOM", back_populates="items")
    material_item = db.relationship("Item", foreign_keys=[material_item_id])
    input_unit = db.relationship("Unit", foreign_keys=[input_unit_id])


# ---------------------------------------------------------------------------
# C. Purchases & Stock Adjustments (Inward / Adjustment Source Documents)
# ---------------------------------------------------------------------------


class Purchase(db.Model):
    __tablename__ = "purchases"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'posted', 'cancelled')",
            name="ck_purchases_status",
        ),
        CheckConstraint(
            "gst_type IN ('none', 'intra_state', 'inter_state')",
            name="ck_purchases_gst_type",
        ),
        CheckConstraint(
            "subtotal_paise >= 0 AND cgst_paise >= 0 AND sgst_paise >= 0 AND igst_paise >= 0",
            name="ck_purchases_amounts_nonneg",
        ),
        CheckConstraint(
            "total_paise = subtotal_paise + cgst_paise + sgst_paise + igst_paise",
            name="ck_purchases_total_sum",
        ),
        CheckConstraint(
            "(gst_type = 'none' AND cgst_paise = 0 AND sgst_paise = 0 AND igst_paise = 0) "
            "OR (gst_type = 'intra_state' AND igst_paise = 0) "
            "OR (gst_type = 'inter_state' AND cgst_paise = 0 AND sgst_paise = 0)",
            name="ck_purchases_gst_split",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    purchase_number = db.Column(db.String(50), unique=True, nullable=False)
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"), nullable=False)
    purchase_date = db.Column(db.Date, nullable=False)
    supplier_invoice_no = db.Column(db.String(80), nullable=True)
    gst_type = db.Column(db.String(20), nullable=False, default="none")
    subtotal_paise = db.Column(db.Integer, nullable=False, default=0)
    cgst_paise = db.Column(db.Integer, nullable=False, default=0)
    sgst_paise = db.Column(db.Integer, nullable=False, default=0)
    igst_paise = db.Column(db.Integer, nullable=False, default=0)
    total_paise = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(20), nullable=False, default="posted")
    notes = db.Column(db.Text, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    supplier = db.relationship("Supplier", foreign_keys=[supplier_id])
    creator = db.relationship("User", foreign_keys=[created_by])
    items = db.relationship(
        "PurchaseItem",
        back_populates="purchase",
        cascade="all, delete-orphan",
    )


class PurchaseItem(db.Model):
    __tablename__ = "purchase_items"
    __table_args__ = (
        CheckConstraint("entered_qty_milli > 0", name="ck_purchase_items_entered_qty_pos"),
        CheckConstraint("qty_stock_milli > 0", name="ck_purchase_items_stock_qty_pos"),
        CheckConstraint("unit_rate_paise >= 0", name="ck_purchase_items_rate_nonneg"),
        CheckConstraint(
            "taxable_value_paise >= 0",
            name="ck_purchase_items_taxable_nonneg",
        ),
        CheckConstraint("gst_rate_bps >= 0", name="ck_purchase_items_gst_nonneg"),
        CheckConstraint(
            "cgst_paise >= 0 AND sgst_paise >= 0 AND igst_paise >= 0",
            name="ck_purchase_items_taxes_nonneg",
        ),
        CheckConstraint(
            "line_total_paise = taxable_value_paise + cgst_paise + sgst_paise + igst_paise",
            name="ck_purchase_items_total_sum",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    purchase_id = db.Column(
        db.Integer,
        db.ForeignKey("purchases.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    entered_unit_id = db.Column(db.Integer, db.ForeignKey("units.id"), nullable=False)
    entered_qty_milli = db.Column(db.Integer, nullable=False)
    qty_stock_milli = db.Column(db.Integer, nullable=False)
    unit_rate_paise = db.Column(db.Integer, nullable=False)
    taxable_value_paise = db.Column(db.Integer, nullable=False)
    gst_rate_bps = db.Column(db.Integer, nullable=False, default=0)
    cgst_paise = db.Column(db.Integer, nullable=False, default=0)
    sgst_paise = db.Column(db.Integer, nullable=False, default=0)
    igst_paise = db.Column(db.Integer, nullable=False, default=0)
    line_total_paise = db.Column(db.Integer, nullable=False)

    purchase = db.relationship("Purchase", back_populates="items")
    item = db.relationship("Item", foreign_keys=[item_id])
    entered_unit = db.relationship("Unit", foreign_keys=[entered_unit_id])


class StockAdjustment(db.Model):
    __tablename__ = "stock_adjustments"
    __table_args__ = (
        CheckConstraint(
            "adjustment_type IN ('opening_stock', 'physical_count', 'spoilage_writeoff')",
            name="ck_stock_adjustments_type",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    adjustment_number = db.Column(db.String(50), unique=True, nullable=False)
    adjustment_type = db.Column(db.String(30), nullable=False)
    adjustment_date = db.Column(db.Date, nullable=False)
    reason = db.Column(db.Text, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    creator = db.relationship("User", foreign_keys=[created_by])
    approver = db.relationship("User", foreign_keys=[approved_by])


# ---------------------------------------------------------------------------
# D. Production, Consumption & Overheads
# ---------------------------------------------------------------------------


class ProductionRun(db.Model):
    __tablename__ = "production_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('in_progress', 'completed', 'reversed')",
            name="ck_production_runs_status",
        ),
        CheckConstraint("batch_multiplier_milli > 0", name="ck_production_runs_mult_pos"),
        CheckConstraint(
            "planned_output_milli > 0",
            name="ck_production_runs_planned_pos",
        ),
        CheckConstraint(
            "good_output_milli >= 0 AND wastage_qty_milli >= 0",
            name="ck_production_runs_outputs_nonneg",
        ),
        CheckConstraint(
            "material_cost_paise >= 0 AND overhead_cost_paise >= 0 AND unit_cost_paise >= 0",
            name="ck_production_runs_costs_nonneg",
        ),
        CheckConstraint(
            "total_cost_paise = material_cost_paise + overhead_cost_paise",
            name="ck_production_runs_total_cost",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    batch_number = db.Column(db.String(50), unique=True, nullable=False)
    bom_id = db.Column(db.Integer, db.ForeignKey("boms.id"), nullable=False)
    finished_item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    run_date = db.Column(db.Date, nullable=False)
    batch_multiplier_milli = db.Column(db.Integer, nullable=False, default=1000)
    planned_output_milli = db.Column(db.Integer, nullable=False)
    good_output_milli = db.Column(db.Integer, nullable=False, default=0)
    wastage_qty_milli = db.Column(db.Integer, nullable=False, default=0)
    wastage_notes = db.Column(db.Text, nullable=True)
    yield_variance_bps = db.Column(db.Integer, nullable=True)
    is_yield_variance_flagged = db.Column(db.Boolean, nullable=False, default=False)
    material_cost_paise = db.Column(db.Integer, nullable=False, default=0)
    overhead_cost_paise = db.Column(db.Integer, nullable=False, default=0)
    total_cost_paise = db.Column(db.Integer, nullable=False, default=0)
    unit_cost_paise = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(20), nullable=False, default="in_progress")
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    completed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    bom = db.relationship("BOM", foreign_keys=[bom_id])
    finished_item = db.relationship("Item", foreign_keys=[finished_item_id])
    creator = db.relationship("User", foreign_keys=[created_by])
    consumptions = db.relationship(
        "ProductionConsumption",
        back_populates="production_run",
        cascade="all, delete-orphan",
    )
    overheads = db.relationship(
        "ProductionOverhead",
        back_populates="production_run",
        cascade="all, delete-orphan",
    )


class ProductionConsumption(db.Model):
    __tablename__ = "production_consumptions"
    __table_args__ = (
        UniqueConstraint(
            "production_run_id",
            "item_id",
            name="uq_prod_consumptions_run_item",
        ),
        CheckConstraint(
            "planned_qty_milli > 0",
            name="ck_prod_consumptions_planned_pos",
        ),
        CheckConstraint(
            "actual_used_milli >= 0 AND damaged_qty_milli >= 0",
            name="ck_prod_consumptions_qtys_nonneg",
        ),
        CheckConstraint(
            "total_consumed_milli = actual_used_milli + damaged_qty_milli",
            name="ck_prod_consumptions_total_qty",
        ),
        CheckConstraint(
            "used_cost_paise >= 0 AND damaged_cost_paise >= 0",
            name="ck_prod_consumptions_costs_nonneg",
        ),
        CheckConstraint(
            "total_cost_paise = used_cost_paise + damaged_cost_paise",
            name="ck_prod_consumptions_total_cost",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    production_run_id = db.Column(
        db.Integer,
        db.ForeignKey("production_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    planned_qty_milli = db.Column(db.Integer, nullable=False)
    actual_used_milli = db.Column(db.Integer, nullable=False, default=0)
    damaged_qty_milli = db.Column(db.Integer, nullable=False, default=0)
    total_consumed_milli = db.Column(db.Integer, nullable=False, default=0)
    variance_bps = db.Column(db.Integer, nullable=True)
    is_variance_flagged = db.Column(db.Boolean, nullable=False, default=False)
    used_cost_paise = db.Column(db.Integer, nullable=False, default=0)
    damaged_cost_paise = db.Column(db.Integer, nullable=False, default=0)
    total_cost_paise = db.Column(db.Integer, nullable=False, default=0)

    production_run = db.relationship("ProductionRun", back_populates="consumptions")
    item = db.relationship("Item", foreign_keys=[item_id])


class ProductionOverhead(db.Model):
    __tablename__ = "production_overheads"
    __table_args__ = (
        CheckConstraint(
            "overhead_type IN ('labour', 'gas_power', 'other')",
            name="ck_prod_overheads_type",
        ),
        CheckConstraint("amount_paise >= 0", name="ck_prod_overheads_amount_nonneg"),
    )

    id = db.Column(db.Integer, primary_key=True)
    production_run_id = db.Column(
        db.Integer,
        db.ForeignKey("production_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    overhead_type = db.Column(db.String(30), nullable=False)
    description = db.Column(db.Text, nullable=True)
    amount_paise = db.Column(db.Integer, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    production_run = db.relationship("ProductionRun", back_populates="overheads")
    creator = db.relationship("User", foreign_keys=[created_by])


# ---------------------------------------------------------------------------
# E. Immutable Append-Only Inventory Ledger
# ---------------------------------------------------------------------------


class StockLedger(db.Model):
    __tablename__ = "stock_ledger"
    __table_args__ = (
        CheckConstraint("unit_cost_paise >= 0", name="ck_stock_ledger_unit_cost_nonneg"),
        CheckConstraint(
            "("
            "movement_type IN ('opening_stock', 'purchase_receipt', 'production_output', 'adjustment_in', 'reversal_in') "
            "AND qty_change_milli > 0 AND value_change_paise >= 0"
            ") OR ("
            "movement_type IN ('production_issue', 'packaging_damage', 'sales_dispatch', 'adjustment_out', 'reversal_out') "
            "AND qty_change_milli < 0 AND value_change_paise <= 0"
            ")",
            name="ck_stock_ledger_sign_convention",
        ),
        CheckConstraint(
            "("
            "movement_type IN ('opening_stock', 'adjustment_in', 'adjustment_out') "
            "AND source_doc_type = 'stock_adjustment' AND reverses_ledger_id IS NULL"
            ") OR ("
            "movement_type = 'purchase_receipt' "
            "AND source_doc_type = 'purchase' AND reverses_ledger_id IS NULL"
            ") OR ("
            "movement_type IN ('production_issue', 'packaging_damage', 'production_output') "
            "AND source_doc_type = 'production_run' AND reverses_ledger_id IS NULL"
            ") OR ("
            "movement_type = 'sales_dispatch' "
            "AND source_doc_type = 'dispatch' AND reverses_ledger_id IS NULL"
            ") OR ("
            "movement_type IN ('reversal_in', 'reversal_out') "
            "AND source_doc_type IN ('stock_adjustment', 'purchase', 'production_run', 'dispatch') "
            "AND reverses_ledger_id IS NOT NULL"
            ")",
            name="ck_stock_ledger_movement_source_pairing",
        ),
        Index("ix_stock_ledger_item_date_id", "item_id", "movement_date", "id"),
        Index("ix_stock_ledger_source_doc", "source_doc_type", "source_doc_id"),
        Index(
            "uq_stock_ledger_single_reversal",
            "reverses_ledger_id",
            unique=True,
            sqlite_where=text("reverses_ledger_id IS NOT NULL"),
        ),
        Index(
            "uq_stock_ledger_prod_posting",
            "source_doc_type",
            "source_doc_id",
            "item_id",
            "movement_type",
            unique=True,
            sqlite_where=text(
                "source_doc_type = 'production_run' "
                "AND movement_type IN ('production_issue', 'packaging_damage', 'production_output')"
            ),
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    movement_date = db.Column(db.Date, nullable=False)
    movement_type = db.Column(db.String(30), nullable=False)
    source_doc_type = db.Column(db.String(30), nullable=False)
    source_doc_id = db.Column(db.Integer, nullable=False)
    reverses_ledger_id = db.Column(
        db.Integer,
        db.ForeignKey("stock_ledger.id"),
        nullable=True,
    )
    qty_change_milli = db.Column(db.Integer, nullable=False)
    unit_cost_paise = db.Column(db.Integer, nullable=False)
    value_change_paise = db.Column(db.Integer, nullable=False)
    notes = db.Column(db.Text, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    item = db.relationship("Item", foreign_keys=[item_id])
    creator = db.relationship("User", foreign_keys=[created_by])
    reversed_entry = db.relationship(
        "StockLedger",
        remote_side=[id],
        foreign_keys=[reverses_ledger_id],
    )


# ---------------------------------------------------------------------------
# F. Sales Orders, GST Invoicing, Sequential Dispatches & Payments
# ---------------------------------------------------------------------------


class SalesOrder(db.Model):
    __tablename__ = "sales_orders"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'invoiced', 'dispatched', 'cancelled')",
            name="ck_sales_orders_status",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    order_number = db.Column(db.String(50), unique=True, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    salesman_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    order_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending")
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    approved_at = db.Column(db.DateTime, nullable=True)
    rejection_reason = db.Column(db.Text, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    customer = db.relationship("Customer", foreign_keys=[customer_id])
    salesman = db.relationship("User", foreign_keys=[salesman_id])
    approver = db.relationship("User", foreign_keys=[approved_by])
    items = db.relationship(
        "SalesOrderItem",
        back_populates="sales_order",
        cascade="all, delete-orphan",
    )


class SalesOrderItem(db.Model):
    __tablename__ = "sales_order_items"
    __table_args__ = (
        UniqueConstraint(
            "sales_order_id",
            "item_id",
            name="uq_sales_order_items_order_item",
        ),
        CheckConstraint(
            "entered_qty_milli > 0 AND qty_stock_milli > 0",
            name="ck_sales_order_items_qtys_pos",
        ),
        CheckConstraint(
            "unit_price_paise >= 0 AND line_subtotal_paise >= 0",
            name="ck_sales_order_items_amounts_nonneg",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    sales_order_id = db.Column(
        db.Integer,
        db.ForeignKey("sales_orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    entered_unit_id = db.Column(db.Integer, db.ForeignKey("units.id"), nullable=False)
    entered_qty_milli = db.Column(db.Integer, nullable=False)
    qty_stock_milli = db.Column(db.Integer, nullable=False)
    unit_price_paise = db.Column(db.Integer, nullable=False)
    line_subtotal_paise = db.Column(db.Integer, nullable=False)

    sales_order = db.relationship("SalesOrder", back_populates="items")
    item = db.relationship("Item", foreign_keys=[item_id])
    entered_unit = db.relationship("Unit", foreign_keys=[entered_unit_id])


class Invoice(db.Model):
    __tablename__ = "invoices"
    __table_args__ = (
        CheckConstraint(
            "sequence_no IS NULL OR sequence_no > 0",
            name="ck_invoices_seq_pos",
        ),
        CheckConstraint(
            "document_type IN ('tax_invoice', 'credit_note')",
            name="ck_invoices_doc_type",
        ),
        CheckConstraint(
            "gst_type IN ('intra_state', 'inter_state')",
            name="ck_invoices_gst_type",
        ),
        CheckConstraint(
            "status IN ('draft', 'posted', 'credited')",
            name="ck_invoices_status",
        ),
        CheckConstraint(
            "("
            "status = 'draft' AND sequence_no IS NULL AND invoice_number IS NULL AND posted_at IS NULL"
            ") OR ("
            "status IN ('posted', 'credited') AND sequence_no IS NOT NULL "
            "AND invoice_number IS NOT NULL AND posted_at IS NOT NULL"
            ")",
            name="ck_invoices_draft_post_fields",
        ),
        CheckConstraint(
            "("
            "document_type = 'tax_invoice' AND original_invoice_id IS NULL"
            ") OR ("
            "document_type = 'credit_note' AND original_invoice_id IS NOT NULL"
            ")",
            name="ck_invoices_credit_note_ref",
        ),
        CheckConstraint(
            "taxable_value_paise >= 0 AND cgst_paise >= 0 AND sgst_paise >= 0 AND igst_paise >= 0",
            name="ck_invoices_amounts_nonneg",
        ),
        CheckConstraint(
            "(gst_type = 'intra_state' AND igst_paise = 0) "
            "OR (gst_type = 'inter_state' AND cgst_paise = 0 AND sgst_paise = 0)",
            name="ck_invoices_gst_split",
        ),
        CheckConstraint(
            "total_paise = taxable_value_paise + cgst_paise + sgst_paise + igst_paise",
            name="ck_invoices_total_sum",
        ),
        Index(
            "uq_invoices_fy_type_seq",
            "financial_year",
            "document_type",
            "sequence_no",
            unique=True,
            sqlite_where=text("sequence_no IS NOT NULL"),
        ),
        Index(
            "uq_invoices_fy_type_num",
            "financial_year",
            "document_type",
            "invoice_number",
            unique=True,
            sqlite_where=text("invoice_number IS NOT NULL"),
        ),
        Index(
            "uq_invoices_number",
            "invoice_number",
            unique=True,
            sqlite_where=text("invoice_number IS NOT NULL"),
        ),
        Index(
            "uq_invoices_active_order",
            "sales_order_id",
            unique=True,
            sqlite_where=text(
                "document_type = 'tax_invoice' "
                "AND status IN ('draft', 'posted') "
                "AND sales_order_id IS NOT NULL"
            ),
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(50), nullable=True)
    financial_year = db.Column(db.String(10), nullable=False)
    sequence_no = db.Column(db.Integer, nullable=True)
    document_type = db.Column(db.String(20), nullable=False, default="tax_invoice")
    original_invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=True)
    sales_order_id = db.Column(db.Integer, db.ForeignKey("sales_orders.id"), nullable=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    customer_name_snapshot = db.Column(db.String(150), nullable=False)
    customer_gstin_snapshot = db.Column(db.String(20), nullable=True)
    invoice_date = db.Column(db.Date, nullable=False)
    place_of_supply = db.Column(db.String(80), nullable=False)
    gst_type = db.Column(db.String(20), nullable=False)
    taxable_value_paise = db.Column(db.Integer, nullable=False, default=0)
    cgst_paise = db.Column(db.Integer, nullable=False, default=0)
    sgst_paise = db.Column(db.Integer, nullable=False, default=0)
    igst_paise = db.Column(db.Integer, nullable=False, default=0)
    total_paise = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(20), nullable=False, default="draft")
    correction_notes = db.Column(db.Text, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    posted_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    original_invoice = db.relationship(
        "Invoice",
        remote_side=[id],
        foreign_keys=[original_invoice_id],
    )
    sales_order = db.relationship("SalesOrder", foreign_keys=[sales_order_id])
    customer = db.relationship("Customer", foreign_keys=[customer_id])
    creator = db.relationship("User", foreign_keys=[created_by])
    items = db.relationship(
        "InvoiceItem",
        back_populates="invoice",
        cascade="all, delete-orphan",
    )


class InvoiceItem(db.Model):
    __tablename__ = "invoice_items"
    __table_args__ = (
        CheckConstraint("qty_stock_milli > 0", name="ck_invoice_items_qty_pos"),
        CheckConstraint(
            "unit_price_paise >= 0 AND taxable_value_paise >= 0",
            name="ck_invoice_items_prices_nonneg",
        ),
        CheckConstraint(
            "gst_rate_bps >= 0 AND cgst_rate_bps >= 0 AND sgst_rate_bps >= 0 AND igst_rate_bps >= 0",
            name="ck_invoice_items_rates_nonneg",
        ),
        CheckConstraint(
            "cgst_rate_bps + sgst_rate_bps + igst_rate_bps = gst_rate_bps",
            name="ck_invoice_items_rate_sum",
        ),
        CheckConstraint(
            "cgst_paise >= 0 AND sgst_paise >= 0 AND igst_paise >= 0",
            name="ck_invoice_items_taxes_nonneg",
        ),
        CheckConstraint(
            "line_total_paise = taxable_value_paise + cgst_paise + sgst_paise + igst_paise",
            name="ck_invoice_items_total_sum",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(
        db.Integer,
        db.ForeignKey("invoices.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    item_sku_snapshot = db.Column(db.String(50), nullable=False)
    item_description_snapshot = db.Column(db.String(200), nullable=False)
    unit_code_snapshot = db.Column(db.String(20), nullable=False)
    hsn_code_snapshot = db.Column(db.String(20), nullable=False, default="")
    qty_stock_milli = db.Column(db.Integer, nullable=False)
    unit_price_paise = db.Column(db.Integer, nullable=False)
    taxable_value_paise = db.Column(db.Integer, nullable=False)
    gst_rate_bps = db.Column(db.Integer, nullable=False)
    cgst_rate_bps = db.Column(db.Integer, nullable=False, default=0)
    sgst_rate_bps = db.Column(db.Integer, nullable=False, default=0)
    igst_rate_bps = db.Column(db.Integer, nullable=False, default=0)
    cgst_paise = db.Column(db.Integer, nullable=False, default=0)
    sgst_paise = db.Column(db.Integer, nullable=False, default=0)
    igst_paise = db.Column(db.Integer, nullable=False, default=0)
    line_total_paise = db.Column(db.Integer, nullable=False)

    invoice = db.relationship("Invoice", back_populates="items")
    item = db.relationship("Item", foreign_keys=[item_id])


class Dispatch(db.Model):
    __tablename__ = "dispatches"
    __table_args__ = (
        CheckConstraint(
            "status IN ('posted', 'reversed')",
            name="ck_dispatches_status",
        ),
        Index(
            "uq_dispatches_active_invoice",
            "invoice_id",
            unique=True,
            sqlite_where=text("status = 'posted'"),
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    dispatch_number = db.Column(db.String(50), unique=True, nullable=False)
    sales_order_id = db.Column(db.Integer, db.ForeignKey("sales_orders.id"), nullable=False)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    dispatch_date = db.Column(db.Date, nullable=False)
    vehicle_or_lr_no = db.Column(db.String(80), nullable=True)
    status = db.Column(db.String(20), nullable=False, default="posted")
    notes = db.Column(db.Text, nullable=True)
    dispatched_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    sales_order = db.relationship("SalesOrder", foreign_keys=[sales_order_id])
    invoice = db.relationship("Invoice", foreign_keys=[invoice_id])
    dispatcher = db.relationship("User", foreign_keys=[dispatched_by])
    items = db.relationship(
        "DispatchItem",
        back_populates="dispatch",
        cascade="all, delete-orphan",
    )


class DispatchItem(db.Model):
    __tablename__ = "dispatch_items"
    __table_args__ = (
        UniqueConstraint(
            "dispatch_id",
            "item_id",
            name="uq_dispatch_items_dispatch_item",
        ),
        CheckConstraint("qty_stock_milli > 0", name="ck_dispatch_items_qty_pos"),
    )

    id = db.Column(db.Integer, primary_key=True)
    dispatch_id = db.Column(
        db.Integer,
        db.ForeignKey("dispatches.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    qty_stock_milli = db.Column(db.Integer, nullable=False)

    dispatch = db.relationship("Dispatch", back_populates="items")
    item = db.relationship("Item", foreign_keys=[item_id])


class Payment(db.Model):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount_paise > 0", name="ck_payments_amount_pos"),
        CheckConstraint(
            "payment_mode IN ('cash', 'upi', 'bank_transfer', 'cheque')",
            name="ck_payments_mode",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    receipt_number = db.Column(db.String(50), unique=True, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=True)
    payment_date = db.Column(db.Date, nullable=False)
    amount_paise = db.Column(db.Integer, nullable=False)
    payment_mode = db.Column(db.String(30), nullable=False)
    reference_no = db.Column(db.String(80), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    recorded_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    customer = db.relationship("Customer", foreign_keys=[customer_id])
    invoice = db.relationship("Invoice", foreign_keys=[invoice_id])
    recorder = db.relationship("User", foreign_keys=[recorded_by])

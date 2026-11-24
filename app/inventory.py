"""Inventory blueprint: Stock Summary, Stock Ledger, and Opening Stock Entry."""

from datetime import date

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from app.csrf import require_csrf_token
from app.extensions import db
from app.inventory_service import (
    InventoryError,
    UnauthorizedInventoryActionError,
    get_all_stock_positions,
    record_opening_stock,
)
from app.master_data import ITEM_TYPE_LABELS, ITEM_TYPES
from app.models import Item, StockAdjustment, StockLedger, Unit
from app.money import format_exact_rupees, format_paise_as_rupees, parse_rupees_to_paise
from app.permissions import (
    CAP_MANAGE_STOCK_ADJUSTMENTS,
    CAP_VIEW_FINANCIAL_REPORTS,
    CAP_VIEW_INVENTORY,
    roles_required,
    roles_with_permission,
    user_has_permission,
)
from app.units import format_milli, parse_qty_to_milli

inventory_bp = Blueprint("inventory", __name__, url_prefix="/inventory")

INVENTORY_VIEW_ROLES = roles_with_permission(CAP_VIEW_INVENTORY)
OPENING_STOCK_WRITE_ROLES = roles_with_permission(CAP_MANAGE_STOCK_ADJUSTMENTS)

MOVEMENT_TYPES: tuple[tuple[str, str], ...] = (
    ("opening_stock", "Opening Stock"),
    ("purchase_receipt", "Purchase Receipt"),
    ("production_issue", "Production Issue"),
    ("packaging_damage", "Packaging Damage"),
    ("production_output", "Production Output"),
    ("sales_dispatch", "Sales Dispatch"),
    ("adjustment_in", "Adjustment In"),
    ("adjustment_out", "Adjustment Out"),
    ("reversal_in", "Reversal In"),
    ("reversal_out", "Reversal Out"),
)
MOVEMENT_TYPE_LABELS: dict[str, str] = dict(MOVEMENT_TYPES)


def _can_view_valuation() -> bool:
    """Only admin, accountant, and owner may view WAC and inventory values."""
    return user_has_permission(current_user, CAP_VIEW_FINANCIAL_REPORTS)


def _resolve_source_doc_refs(entries: list[StockLedger]) -> dict[tuple[str, int], str]:
    """Resolve human-readable document numbers for ledger rows in bulk."""
    refs: dict[tuple[str, int], str] = {}
    adj_ids = {
        e.source_doc_id for e in entries if e.source_doc_type == "stock_adjustment"
    }
    if adj_ids:
        adjustments = db.session.execute(
            db.select(StockAdjustment.id, StockAdjustment.adjustment_number).where(
                StockAdjustment.id.in_(adj_ids)
            )
        ).all()
        for adj_id, adj_number in adjustments:
            refs[("stock_adjustment", adj_id)] = adj_number
    return refs


def _load_opening_stock_form_options():
    """Fetch active items, items with existing ledger history, and all units."""
    active_items = db.session.execute(
        db.select(Item)
        .options(joinedload(Item.stock_unit))
        .where(Item.is_active.is_(True))
        .order_by(Item.item_type.asc(), Item.name.asc(), Item.sku.asc())
    ).scalars().all()

    items_with_history = set(
        db.session.execute(db.select(StockLedger.item_id).distinct()).scalars().all()
    )

    all_units = db.session.execute(
        db.select(Unit).order_by(Unit.dimension.asc(), Unit.code.asc())
    ).scalars().all()

    return active_items, items_with_history, all_units


@inventory_bp.route("")
@roles_required(*INVENTORY_VIEW_ROLES)
def stock_summary():
    item_type_filter = (request.args.get("item_type") or "").strip()
    if item_type_filter not in ITEM_TYPE_LABELS:
        item_type_filter = ""

    status_filter = (request.args.get("status") or "all").strip()
    if status_filter not in ("all", "active", "inactive"):
        status_filter = "all"

    positions = get_all_stock_positions(
        item_type=item_type_filter or None,
        status=status_filter,
    )
    can_view_valuation = _can_view_valuation()
    total_value_paise = (
        sum(pos.value_paise for pos in positions) if can_view_valuation else None
    )

    return render_template(
        "inventory/stock_summary.html",
        positions=positions,
        item_types=ITEM_TYPES,
        item_type_labels=ITEM_TYPE_LABELS,
        item_type_filter=item_type_filter,
        status_filter=status_filter,
        can_view_valuation=can_view_valuation,
        total_value_paise=total_value_paise,
        format_milli=format_milli,
        format_paise=format_paise_as_rupees,
        format_exact_rupees=format_exact_rupees,
    )


@inventory_bp.route("/ledger")
@roles_required(*INVENTORY_VIEW_ROLES)
def stock_ledger():
    item_id_raw = (request.args.get("item_id") or "").strip()
    movement_type_filter = (request.args.get("movement_type") or "").strip()

    item_id_filter: int | None = None
    if item_id_raw:
        try:
            item_id_filter = int(item_id_raw)
        except ValueError:
            item_id_raw = ""

    if movement_type_filter not in MOVEMENT_TYPE_LABELS:
        movement_type_filter = ""

    query = (
        db.select(StockLedger)
        .options(
            joinedload(StockLedger.item).joinedload(Item.stock_unit),
            joinedload(StockLedger.creator),
        )
        .order_by(StockLedger.movement_date.desc(), StockLedger.id.desc())
    )
    if item_id_filter is not None:
        query = query.where(StockLedger.item_id == item_id_filter)
    if movement_type_filter:
        query = query.where(StockLedger.movement_type == movement_type_filter)

    entries = db.session.execute(query).scalars().all()
    source_doc_refs = _resolve_source_doc_refs(entries)

    all_items = db.session.execute(
        db.select(Item).order_by(Item.name.asc(), Item.sku.asc())
    ).scalars().all()

    can_view_valuation = _can_view_valuation()

    return render_template(
        "inventory/stock_ledger.html",
        entries=entries,
        source_doc_refs=source_doc_refs,
        all_items=all_items,
        item_id_filter=item_id_raw,
        movement_types=MOVEMENT_TYPES,
        movement_type_labels=MOVEMENT_TYPE_LABELS,
        movement_type_filter=movement_type_filter,
        can_view_valuation=can_view_valuation,
        format_milli=format_milli,
        format_paise=format_paise_as_rupees,
    )


@inventory_bp.route("/opening-stock", methods=["GET", "POST"])
@roles_required(*OPENING_STOCK_WRITE_ROLES)
def opening_stock():
    active_items, items_with_history, all_units = _load_opening_stock_form_options()

    values = {
        "item_id": (request.args.get("item_id") or "").strip(),
        "quantity": "",
        "input_unit_id": "",
        "unit_cost": "",
        "movement_date": date.today().isoformat(),
        "reason": "Opening stock balance",
        "notes": "",
    }
    errors: dict[str, str] = {}

    if request.method == "POST":
        require_csrf_token()

        item_raw = (request.form.get("item_id") or "").strip()
        qty_raw = (request.form.get("quantity") or "").strip()
        input_unit_raw = (request.form.get("input_unit_id") or "").strip()
        unit_cost_raw = (request.form.get("unit_cost") or "").strip()
        date_raw = (request.form.get("movement_date") or "").strip()
        reason_raw = (request.form.get("reason") or "").strip()
        notes_raw = (request.form.get("notes") or "").strip()

        values = {
            "item_id": item_raw,
            "quantity": qty_raw,
            "input_unit_id": input_unit_raw,
            "unit_cost": unit_cost_raw,
            "movement_date": date_raw,
            "reason": reason_raw,
            "notes": notes_raw,
        }

        item_id: int | None = None
        if not item_raw:
            errors["item_id"] = "Item is required."
        else:
            try:
                item_id = int(item_raw)
            except ValueError:
                errors["item_id"] = "Select a valid item."

        qty_milli, qty_err = parse_qty_to_milli(qty_raw, field_label="Quantity")
        if qty_err:
            errors["quantity"] = qty_err

        input_unit_id: int | None = None
        if input_unit_raw:
            try:
                input_unit_id = int(input_unit_raw)
            except ValueError:
                errors["input_unit_id"] = "Select a valid unit."

        unit_cost_paise, cost_err = parse_rupees_to_paise(
            unit_cost_raw,
            field_label="Unit cost",
            allow_zero=True,
        )
        if cost_err:
            errors["unit_cost"] = cost_err

        parsed_date: date | None = None
        if not date_raw:
            errors["movement_date"] = "Date is required."
        else:
            try:
                parsed_date = date.fromisoformat(date_raw)
            except ValueError:
                errors["movement_date"] = "Enter a valid date (YYYY-MM-DD)."

        if not reason_raw:
            errors["reason"] = "Reason is required."

        if not errors:
            try:
                adjustment, ledger_entry = record_opening_stock(
                    item_id=item_id,
                    qty_milli=qty_milli,
                    input_unit_id=input_unit_id,
                    unit_cost_paise=unit_cost_paise,
                    movement_date=parsed_date,
                    created_by=current_user.id,
                    reason=reason_raw,
                    notes=notes_raw or None,
                )
            except (InventoryError, UnauthorizedInventoryActionError) as exc:
                errors["form"] = str(exc)
            except IntegrityError:
                db.session.rollback()
                errors["form"] = (
                    "Could not save opening stock due to a database constraint error."
                )
            else:
                item = db.session.get(Item, ledger_entry.item_id)
                unit_code = item.stock_unit.code if item and item.stock_unit else ""
                item_name = item.name if item else f"Item #{ledger_entry.item_id}"
                flash(
                    f"Opening stock recorded ({adjustment.adjustment_number}): "
                    f"{format_milli(ledger_entry.qty_change_milli)} {unit_code} of '{item_name}'.",
                    "success",
                )
                return redirect(url_for("inventory.stock_summary"))

    return render_template(
        "inventory/opening_stock_form.html",
        active_items=active_items,
        items_with_history=items_with_history,
        all_units=all_units,
        item_type_labels=ITEM_TYPE_LABELS,
        values=values,
        errors=errors,
    )

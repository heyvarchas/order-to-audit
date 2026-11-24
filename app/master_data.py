"""Master Data blueprint: Items, Customers, Suppliers, Units, and Item-Unit Conversions."""

from decimal import Decimal
import re

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.audit import log_audit
from app.csrf import require_csrf_token
from app.extensions import db
from app.inventory_service import get_item_stock_position, item_has_ledger_history
from app.models import (
    BOM,
    BOMItem,
    Customer,
    Item,
    ItemUnitConversion,
    ProductionRun,
    Supplier,
    Unit,
)
from app.money import (
    format_exact_rupees,
    format_paise_as_rupees,
    parse_rupees_to_paise,
    parse_scaled_decimal,
)
from app.permissions import (
    CAP_MANAGE_PRODUCTS_CUSTOMERS,
    CAP_RECORD_PURCHASES,
    CAP_VIEW_FINANCIAL_REPORTS,
    CAP_VIEW_INVENTORY,
    CAP_VIEW_OWN_SALES_ORDERS,
    roles_required,
    roles_with_permission,
    user_has_permission,
)
from app.units import (
    UnitConversionError,
    convert_qty_milli,
    convert_qty_milli_to_stock,
    format_milli,
    get_conversion_factor,
    validate_dimension_unit_graph,
    validate_item_conversion_graph,
)

master_data_bp = Blueprint("master_data", __name__, url_prefix="/master")

# Role groups derived directly from the centralized ROLE_PERMISSIONS matrix.
MASTER_WRITE_ROLES = roles_with_permission(CAP_MANAGE_PRODUCTS_CUSTOMERS)
ITEM_AND_UNIT_VIEW_ROLES = roles_with_permission(CAP_VIEW_INVENTORY)
CUSTOMER_VIEW_ROLES = roles_with_permission(
    CAP_MANAGE_PRODUCTS_CUSTOMERS, CAP_VIEW_OWN_SALES_ORDERS
)
SUPPLIER_VIEW_ROLES = roles_with_permission(
    CAP_MANAGE_PRODUCTS_CUSTOMERS, CAP_RECORD_PURCHASES
)

ITEM_TYPES: tuple[tuple[str, str], ...] = (
    ("raw_material", "Raw Material"),
    ("packaging", "Packaging Material"),
    ("finished_good", "Finished Good"),
)
ITEM_TYPE_LABELS: dict[str, str] = dict(ITEM_TYPES)

CUSTOMER_TYPES: tuple[tuple[str, str], ...] = (
    ("retailer", "Retailer"),
    ("distributor", "Distributor"),
)
CUSTOMER_TYPE_LABELS: dict[str, str] = dict(CUSTOMER_TYPES)

UNIT_DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("mass", "Mass"),
    ("volume", "Volume"),
    ("count", "Count"),
)
UNIT_DIMENSION_LABELS: dict[str, str] = dict(UNIT_DIMENSIONS)

INDIAN_STATES: tuple[str, ...] = (
    "Andaman and Nicobar Islands",
    "Andhra Pradesh",
    "Arunachal Pradesh",
    "Assam",
    "Bihar",
    "Chandigarh",
    "Chhattisgarh",
    "Dadra and Nagar Haveli and Daman and Diu",
    "Delhi",
    "Goa",
    "Gujarat",
    "Haryana",
    "Himachal Pradesh",
    "Jammu and Kashmir",
    "Jharkhand",
    "Karnataka",
    "Kerala",
    "Ladakh",
    "Lakshadweep",
    "Madhya Pradesh",
    "Maharashtra",
    "Manipur",
    "Meghalaya",
    "Mizoram",
    "Nagaland",
    "Odisha",
    "Puducherry",
    "Punjab",
    "Rajasthan",
    "Sikkim",
    "Tamil Nadu",
    "Telangana",
    "Tripura",
    "Uttar Pradesh",
    "Uttarakhand",
    "West Bengal",
)

# Official 15-character Indian GSTIN structure:
# 2-digit state code + 10-char PAN + 1 entity char + 'Z' + 1 checksum char.
_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_VALID_GST_STATE_CODES = frozenset(
    {f"{n:02d}" for n in range(1, 39)} | {"97", "99"}
)


# ---------------------------------------------------------------------------
# Formatting & Validation Helpers
# ---------------------------------------------------------------------------


def format_bps_as_percent(bps: int | None) -> str:
    """Format integer basis points (e.g. 500 -> '5', 250 -> '2.5') for display/forms."""
    if bps is None:
        return "0"
    value = (Decimal(bps) / Decimal(100)).normalize()
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _parse_scaled_decimal(
    raw: str,
    *,
    field_label: str,
    min_value: Decimal = Decimal("0"),
    max_value: Decimal | None = None,
) -> tuple[int | None, str | None]:
    """Delegate scaled decimal parsing to shared `app.money.parse_scaled_decimal`."""
    return parse_scaled_decimal(
        raw,
        field_label=field_label,
        min_value=min_value,
        max_value=max_value,
    )


def _validate_gstin(raw: str) -> tuple[str | None, str | None]:
    """Validate optional Indian GSTIN; returns (normalized_value_or_None, error_or_None)."""
    cleaned = raw.strip().upper()
    if not cleaned:
        return None, None
    if len(cleaned) != 15 or not _GSTIN_RE.match(cleaned) or cleaned[:2] not in _VALID_GST_STATE_CODES:
        return (
            None,
            "GSTIN must be a valid 15-character Indian GSTIN (for example, 33AAAAA0000A1Z5).",
        )
    return cleaned, None


def _get_or_404(model, record_id: int):
    record = db.session.get(model, record_id)
    if record is None:
        abort(404)
    return record


def _active_boms_using_component(item_id: int) -> list[BOM]:
    """Return active BOMs that reference `item_id` as a component material."""
    return db.session.execute(
        db.select(BOM)
        .join(BOMItem, BOMItem.bom_id == BOM.id)
        .where(
            BOMItem.material_item_id == item_id,
            BOM.is_active.is_(True),
        )
        .order_by(BOM.name.asc(), BOM.version.asc())
    ).scalars().all()


def _validate_item_form(form, *, existing_item: Item | None = None):
    """Validate item create/edit form data and return (cleaned_data, errors, raw_form_values)."""
    sku = (form.get("sku") or "").strip()
    name = (form.get("name") or "").strip()
    item_type = (form.get("item_type") or "").strip()
    stock_unit_raw = (form.get("stock_unit_id") or "").strip()
    hsn_code = (form.get("hsn_code") or "").strip()
    gst_rate_raw = (form.get("gst_rate") or "").strip()
    sale_price_raw = (form.get("default_sale_price") or "").strip()
    is_active = form.get("is_active") == "1"

    values = {
        "sku": sku,
        "name": name,
        "item_type": item_type,
        "stock_unit_id": stock_unit_raw,
        "hsn_code": hsn_code,
        "gst_rate": gst_rate_raw if gst_rate_raw != "" else "0",
        "default_sale_price": sale_price_raw,
        "is_active": is_active,
    }
    errors: dict[str, str] = {}

    if not sku:
        errors["sku"] = "SKU is required."
    elif len(sku) > 50:
        errors["sku"] = "SKU must be 50 characters or fewer."
    else:
        existing_sku = db.session.execute(
            db.select(Item).where(func.lower(Item.sku) == sku.lower())
        ).scalar_one_or_none()
        if existing_sku is not None and (existing_item is None or existing_sku.id != existing_item.id):
            errors["sku"] = f"SKU '{sku}' is already in use."

    if not name:
        errors["name"] = "Item name is required."
    elif len(name) > 150:
        errors["name"] = "Item name must be 150 characters or fewer."

    if item_type not in ITEM_TYPE_LABELS:
        errors["item_type"] = "Select a valid item type."
    elif existing_item is not None and item_type != existing_item.item_type:
        has_finished_boms = (
            db.session.execute(
                db.select(BOM.id).where(BOM.finished_item_id == existing_item.id).limit(1)
            ).first()
            is not None
        )
        has_component_boms = (
            db.session.execute(
                db.select(BOMItem.id)
                .where(BOMItem.material_item_id == existing_item.id)
                .limit(1)
            ).first()
            is not None
        )
        if has_finished_boms and item_type != "finished_good":
            errors["item_type"] = (
                "Cannot change item type while BOMs exist for this finished product."
            )
        elif has_component_boms and item_type not in ("raw_material", "packaging"):
            errors["item_type"] = (
                "Cannot change item type to Finished Good while this item is used as a BOM component."
            )

    stock_unit = None
    if not stock_unit_raw:
        errors["stock_unit_id"] = "Base stock unit is required."
    else:
        try:
            stock_unit_id = int(stock_unit_raw)
            stock_unit = db.session.get(Unit, stock_unit_id)
        except ValueError:
            stock_unit = None
        if stock_unit is None:
            errors["stock_unit_id"] = "Select a valid base stock unit."
        elif existing_item is not None and stock_unit.id != existing_item.stock_unit_id:
            if item_has_ledger_history(existing_item.id):
                errors["stock_unit_id"] = (
                    "Cannot change base stock unit because this item already has "
                    "stock ledger history."
                )

            if "stock_unit_id" not in errors and existing_item.unit_conversions:
                existing_dims = {c.from_unit.dimension for c in existing_item.unit_conversions}
                if stock_unit.dimension not in existing_dims:
                    errors["stock_unit_id"] = (
                        "Cannot change stock unit to a different dimension while item unit "
                        "conversions exist."
                    )

            if "stock_unit_id" not in errors:
                has_finished_boms = (
                    db.session.execute(
                        db.select(BOM.id)
                        .where(BOM.finished_item_id == existing_item.id)
                        .limit(1)
                    ).first()
                    is not None
                )
                if has_finished_boms:
                    errors["stock_unit_id"] = (
                        "Cannot change stock unit while BOMs exist for this finished product."
                    )

            if "stock_unit_id" not in errors:
                dependent_bom_items = db.session.execute(
                    db.select(BOMItem).where(BOMItem.material_item_id == existing_item.id)
                ).scalars().all()
                for bom_item in dependent_bom_items:
                    try:
                        convert_qty_milli(
                            existing_item,
                            bom_item.standard_qty_milli,
                            bom_item.input_unit,
                            stock_unit,
                        )
                    except UnitConversionError as exc:
                        errors["stock_unit_id"] = (
                            f"Cannot change stock unit because BOM '{bom_item.bom.name}' "
                            f"(v{bom_item.bom.version}) depends on exact conversion: {exc}"
                        )
                        break

    normalized_hsn = None
    if hsn_code:
        if len(hsn_code) > 20 or not hsn_code.isdigit():
            errors["hsn_code"] = "HSN code must contain digits only (up to 20 digits)."
        else:
            normalized_hsn = hsn_code

    gst_rate_bps = 0
    if gst_rate_raw != "":
        parsed_gst, gst_err = _parse_scaled_decimal(
            gst_rate_raw,
            field_label="GST rate (%)",
            min_value=Decimal("0"),
            max_value=Decimal("100"),
        )
        if gst_err:
            errors["gst_rate"] = gst_err
        else:
            gst_rate_bps = parsed_gst

    default_sale_price_paise = None
    if sale_price_raw != "":
        parsed_price, price_err = parse_rupees_to_paise(
            sale_price_raw,
            field_label="Default sale price",
            allow_zero=True,
        )
        if price_err:
            errors["default_sale_price"] = price_err
        else:
            default_sale_price_paise = parsed_price

    if existing_item is not None and existing_item.is_active and not is_active:
        active_boms = _active_boms_using_component(existing_item.id)
        if active_boms:
            bom_list = ", ".join(f"{bom.name} (v{bom.version})" for bom in active_boms)
            errors["is_active"] = (
                f"Cannot deactivate item '{existing_item.name}' because it is a component of "
                f"active BOM(s): {bom_list}. Deactivate the BOM(s) first."
            )

    cleaned = {
        "sku": sku,
        "name": name,
        "item_type": item_type,
        "stock_unit_id": stock_unit.id if stock_unit is not None else None,
        "hsn_code": normalized_hsn,
        "gst_rate_bps": gst_rate_bps,
        "default_sale_price_paise": default_sale_price_paise,
        "is_active": is_active,
    }
    return cleaned, errors, values


def _validate_customer_form(form):
    """Validate customer create/edit form data."""
    name = (form.get("name") or "").strip()
    customer_type = (form.get("customer_type") or "").strip()
    gstin_raw = (form.get("gstin") or "").strip()
    state = (form.get("state") or "").strip()
    address = (form.get("address") or "").strip()
    phone = (form.get("phone") or "").strip()
    is_active = form.get("is_active") == "1"

    values = {
        "name": name,
        "customer_type": customer_type,
        "gstin": gstin_raw.upper(),
        "state": state,
        "address": address,
        "phone": phone,
        "is_active": is_active,
    }
    errors: dict[str, str] = {}

    if not name:
        errors["name"] = "Customer name is required."
    elif len(name) > 150:
        errors["name"] = "Customer name must be 150 characters or fewer."

    if customer_type not in CUSTOMER_TYPE_LABELS:
        errors["customer_type"] = "Select a valid customer type (Retailer or Distributor)."

    gstin, gstin_err = _validate_gstin(gstin_raw)
    if gstin_err:
        errors["gstin"] = gstin_err

    if not state:
        errors["state"] = "State is required for GST calculation."
    elif len(state) > 80:
        errors["state"] = "State must be 80 characters or fewer."

    if phone and len(phone) > 30:
        errors["phone"] = "Phone number must be 30 characters or fewer."

    cleaned = {
        "name": name,
        "customer_type": customer_type,
        "gstin": gstin,
        "state": state,
        "address": address or None,
        "phone": phone or None,
        "is_active": is_active,
    }
    return cleaned, errors, values


def _validate_supplier_form(form):
    """Validate supplier create/edit form data."""
    name = (form.get("name") or "").strip()
    gstin_raw = (form.get("gstin") or "").strip()
    state = (form.get("state") or "").strip()
    address = (form.get("address") or "").strip()
    phone = (form.get("phone") or "").strip()
    is_active = form.get("is_active") == "1"

    values = {
        "name": name,
        "gstin": gstin_raw.upper(),
        "state": state,
        "address": address,
        "phone": phone,
        "is_active": is_active,
    }
    errors: dict[str, str] = {}

    if not name:
        errors["name"] = "Supplier name is required."
    elif len(name) > 150:
        errors["name"] = "Supplier name must be 150 characters or fewer."

    gstin, gstin_err = _validate_gstin(gstin_raw)
    if gstin_err:
        errors["gstin"] = gstin_err

    if not state:
        errors["state"] = "State is required for GST calculation."
    elif len(state) > 80:
        errors["state"] = "State must be 80 characters or fewer."

    if phone and len(phone) > 30:
        errors["phone"] = "Phone number must be 30 characters or fewer."

    cleaned = {
        "name": name,
        "gstin": gstin,
        "state": state,
        "address": address or None,
        "phone": phone or None,
        "is_active": is_active,
    }
    return cleaned, errors, values


def _validate_unit_form(form, *, existing_unit: Unit | None = None):
    """Validate unit create/edit form data."""
    code = (form.get("code") or "").strip()
    name = (form.get("name") or "").strip()
    dimension = (form.get("dimension") or "").strip()
    base_unit_raw = (form.get("base_unit_id") or "").strip()
    factor_raw = (form.get("factor_to_base") or "").strip()

    values = {
        "code": code,
        "name": name,
        "dimension": dimension,
        "base_unit_id": base_unit_raw,
        "factor_to_base": factor_raw if factor_raw != "" else "1",
    }
    errors: dict[str, str] = {}

    if not code:
        errors["code"] = "Unit code is required."
    elif len(code) > 20:
        errors["code"] = "Unit code must be 20 characters or fewer."
    else:
        existing_code = db.session.execute(
            db.select(Unit).where(func.lower(Unit.code) == code.lower())
        ).scalar_one_or_none()
        if existing_code is not None and (existing_unit is None or existing_code.id != existing_unit.id):
            errors["code"] = f"Unit code '{code}' already exists."

    if not name:
        errors["name"] = "Unit name is required."
    elif len(name) > 60:
        errors["name"] = "Unit name must be 60 characters or fewer."

    if dimension not in UNIT_DIMENSION_LABELS:
        errors["dimension"] = "Select a valid dimension (Mass, Volume, or Count)."
    elif existing_unit is not None and dimension != existing_unit.dimension:
        has_dependents = (
            db.session.execute(
                db.select(Unit.id).where(Unit.base_unit_id == existing_unit.id).limit(1)
            ).first()
            is not None
            or db.session.execute(
                db.select(Item.id).where(Item.stock_unit_id == existing_unit.id).limit(1)
            ).first()
            is not None
            or db.session.execute(
                db.select(ItemUnitConversion.id)
                .where(
                    (ItemUnitConversion.from_unit_id == existing_unit.id)
                    | (ItemUnitConversion.to_unit_id == existing_unit.id)
                )
                .limit(1)
            ).first()
            is not None
            or db.session.execute(
                db.select(BOMItem.id)
                .where(BOMItem.input_unit_id == existing_unit.id)
                .limit(1)
            ).first()
            is not None
        )
        if has_dependents:
            errors["dimension"] = (
                "Cannot change dimension while this unit is referenced by items, "
                "other units, conversions, or BOM components."
            )

    base_unit = None
    if base_unit_raw:
        try:
            base_unit_id = int(base_unit_raw)
            base_unit = db.session.get(Unit, base_unit_id)
        except ValueError:
            base_unit = None

        if base_unit is None:
            errors["base_unit_id"] = "Selected base unit does not exist."
        elif existing_unit is not None and base_unit.id == existing_unit.id:
            errors["base_unit_id"] = "A unit cannot reference itself as its own base unit."
        elif dimension in UNIT_DIMENSION_LABELS and base_unit.dimension != dimension:
            errors["base_unit_id"] = (
                f"Base unit '{base_unit.code}' has dimension '{base_unit.dimension}', "
                f"which is incompatible with '{dimension}'."
            )

    factor_to_base = 1
    if factor_raw != "":
        try:
            factor_to_base = int(factor_raw)
            if factor_to_base <= 0:
                errors["factor_to_base"] = "Factor to base must be a positive whole number."
        except ValueError:
            errors["factor_to_base"] = "Factor to base must be a positive whole number."

    cleaned = {
        "code": code,
        "name": name,
        "dimension": dimension,
        "base_unit_id": base_unit.id if base_unit is not None else None,
        "factor_to_base": factor_to_base,
    }
    return cleaned, errors, values


def _validate_conversion_form(
    form,
    *,
    fixed_item: Item | None = None,
    existing_conversion: ItemUnitConversion | None = None,
):
    """Validate item-specific unit conversion data."""
    item_raw = str(fixed_item.id) if fixed_item is not None else (form.get("item_id") or "").strip()
    from_unit_raw = (form.get("from_unit_id") or "").strip()
    to_unit_raw = (form.get("to_unit_id") or "").strip()
    multiplier_raw = (form.get("multiplier") or "").strip()

    values = {
        "item_id": item_raw,
        "from_unit_id": from_unit_raw,
        "to_unit_id": to_unit_raw,
        "multiplier": multiplier_raw,
    }
    errors: dict[str, str] = {}

    item = fixed_item
    if item is None:
        if not item_raw:
            errors["item_id"] = "Item is required."
        else:
            try:
                item = db.session.get(Item, int(item_raw))
            except ValueError:
                item = None
            if item is None:
                errors["item_id"] = "Selected item does not exist."

    from_unit = None
    if not from_unit_raw:
        errors["from_unit_id"] = "From unit is required."
    else:
        try:
            from_unit = db.session.get(Unit, int(from_unit_raw))
        except ValueError:
            from_unit = None
        if from_unit is None:
            errors["from_unit_id"] = "Selected 'From' unit does not exist."

    to_unit = None
    if not to_unit_raw:
        errors["to_unit_id"] = "To unit is required."
    else:
        try:
            to_unit = db.session.get(Unit, int(to_unit_raw))
        except ValueError:
            to_unit = None
        if to_unit is None:
            errors["to_unit_id"] = "Selected 'To' unit does not exist."

    multiplier = None
    if not multiplier_raw:
        errors["multiplier"] = "Multiplier is required."
    else:
        try:
            multiplier = int(multiplier_raw)
            if multiplier <= 0:
                errors["multiplier"] = "Multiplier must be a positive whole number."
        except ValueError:
            errors["multiplier"] = "Multiplier must be a positive whole number."

    if from_unit is not None and to_unit is not None:
        if from_unit.id == to_unit.id:
            errors["to_unit_id"] = "From unit and To unit must be different."
        elif from_unit.dimension != to_unit.dimension:
            errors["to_unit_id"] = (
                f"Cannot convert between incompatible dimensions "
                f"('{from_unit.dimension}' and '{to_unit.dimension}')."
            )
        elif item is not None and from_unit.dimension != item.stock_unit.dimension:
            errors["from_unit_id"] = (
                f"Conversion units must match item stock unit dimension "
                f"('{item.stock_unit.dimension}')."
            )
        elif (
            multiplier is not None
            and multiplier > 0
            and from_unit.base_unit_id == to_unit.id
            and multiplier != from_unit.factor_to_base
        ):
            errors["multiplier"] = (
                f"Standard unit conversion requires 1 {from_unit.code} = "
                f"{from_unit.factor_to_base} {to_unit.code}."
            )

        if item is not None and not errors:
            duplicate = db.session.execute(
                db.select(ItemUnitConversion).where(
                    ItemUnitConversion.item_id == item.id,
                    ItemUnitConversion.from_unit_id == from_unit.id,
                    ItemUnitConversion.to_unit_id == to_unit.id,
                )
            ).scalar_one_or_none()
            if duplicate is not None and (
                existing_conversion is None or duplicate.id != existing_conversion.id
            ):
                errors["to_unit_id"] = (
                    f"A conversion from '{from_unit.code}' to '{to_unit.code}' "
                    f"already exists for this item."
                )

            reverse_dup = db.session.execute(
                db.select(ItemUnitConversion).where(
                    ItemUnitConversion.item_id == item.id,
                    ItemUnitConversion.from_unit_id == to_unit.id,
                    ItemUnitConversion.to_unit_id == from_unit.id,
                )
            ).scalar_one_or_none()
            if reverse_dup is not None and (
                existing_conversion is None or reverse_dup.id != existing_conversion.id
            ):
                errors["to_unit_id"] = (
                    f"A reverse conversion from '{to_unit.code}' to '{from_unit.code}' "
                    f"already exists for this item."
                )

    cleaned = {
        "item_id": item.id if item is not None else None,
        "from_unit_id": from_unit.id if from_unit is not None else None,
        "to_unit_id": to_unit.id if to_unit is not None else None,
        "multiplier": multiplier,
    }
    return cleaned, errors, values


# ---------------------------------------------------------------------------
# A. Items Routes
# ---------------------------------------------------------------------------


@master_data_bp.route("/items")
@roles_required(*ITEM_AND_UNIT_VIEW_ROLES)
def list_items():
    item_type_filter = (request.args.get("item_type") or "").strip()
    status_filter = (request.args.get("status") or "all").strip()

    query = db.select(Item).order_by(Item.name.asc(), Item.sku.asc())
    if item_type_filter in ITEM_TYPE_LABELS:
        query = query.where(Item.item_type == item_type_filter)
    else:
        item_type_filter = ""

    if status_filter == "active":
        query = query.where(Item.is_active.is_(True))
    elif status_filter == "inactive":
        query = query.where(Item.is_active.is_(False))
    else:
        status_filter = "all"

    items = db.session.execute(query).scalars().all()
    return render_template(
        "master_data/items_list.html",
        items=items,
        item_types=ITEM_TYPES,
        item_type_labels=ITEM_TYPE_LABELS,
        item_type_filter=item_type_filter,
        status_filter=status_filter,
        format_bps=format_bps_as_percent,
        format_paise=format_paise_as_rupees,
    )


@master_data_bp.route("/items/new", methods=["GET", "POST"])
@roles_required(*MASTER_WRITE_ROLES)
def create_item():
    units = db.session.execute(
        db.select(Unit).order_by(Unit.dimension.asc(), Unit.code.asc())
    ).scalars().all()

    values = {
        "sku": "",
        "name": "",
        "item_type": "",
        "stock_unit_id": "",
        "hsn_code": "",
        "gst_rate": "0",
        "default_sale_price": "",
        "is_active": True,
    }
    errors: dict[str, str] = {}

    if request.method == "POST":
        require_csrf_token()
        cleaned, errors, values = _validate_item_form(request.form)
        if not errors:
            item = Item(**cleaned)
            db.session.add(item)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                errors["sku"] = "Could not save item because SKU must be unique."
            else:
                flash(f"Item '{item.name}' ({item.sku}) created.", "success")
                return redirect(url_for("master_data.view_item", item_id=item.id))

    return render_template(
        "master_data/item_form.html",
        mode="create",
        item=None,
        values=values,
        errors=errors,
        units=units,
        item_types=ITEM_TYPES,
    )


@master_data_bp.route("/items/<int:item_id>")
@roles_required(*ITEM_AND_UNIT_VIEW_ROLES)
def view_item(item_id: int):
    item = _get_or_404(Item, item_id)
    compatible_units = db.session.execute(
        db.select(Unit)
        .where(Unit.dimension == item.stock_unit.dimension)
        .order_by(Unit.code.asc())
    ).scalars().all()
    all_units = db.session.execute(
        db.select(Unit).order_by(Unit.dimension.asc(), Unit.code.asc())
    ).scalars().all()
    item_boms = (
        db.session.execute(
            db.select(BOM)
            .where(BOM.finished_item_id == item.id)
            .order_by(BOM.version.desc())
        ).scalars().all()
        if item.item_type == "finished_good"
        else []
    )

    stock_position = get_item_stock_position(item.id)
    can_view_valuation = user_has_permission(current_user, CAP_VIEW_FINANCIAL_REPORTS)

    return render_template(
        "master_data/item_detail.html",
        item=item,
        item_boms=item_boms,
        stock_position=stock_position,
        can_view_valuation=can_view_valuation,
        item_type_labels=ITEM_TYPE_LABELS,
        dimension_labels=UNIT_DIMENSION_LABELS,
        compatible_units=compatible_units,
        all_units=all_units,
        conversion_values={"from_unit_id": "", "to_unit_id": str(item.stock_unit_id), "multiplier": ""},
        conversion_errors={},
        format_bps=format_bps_as_percent,
        format_paise=format_paise_as_rupees,
        format_exact_rupees=format_exact_rupees,
        format_milli=format_milli,
    )


@master_data_bp.route("/items/<int:item_id>/edit", methods=["GET", "POST"])
@roles_required(*MASTER_WRITE_ROLES)
def edit_item(item_id: int):
    item = _get_or_404(Item, item_id)
    units = db.session.execute(
        db.select(Unit).order_by(Unit.dimension.asc(), Unit.code.asc())
    ).scalars().all()

    values = {
        "sku": item.sku,
        "name": item.name,
        "item_type": item.item_type,
        "stock_unit_id": str(item.stock_unit_id),
        "hsn_code": item.hsn_code or "",
        "gst_rate": format_bps_as_percent(item.gst_rate_bps),
        "default_sale_price": format_paise_as_rupees(item.default_sale_price_paise),
        "is_active": item.is_active,
    }
    errors: dict[str, str] = {}

    if request.method == "POST":
        require_csrf_token()
        cleaned, errors, values = _validate_item_form(request.form, existing_item=item)
        if not errors:
            for field, val in cleaned.items():
                setattr(item, field, val)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                errors["sku"] = "Could not update item because SKU must be unique."
            else:
                flash(f"Item '{item.name}' updated.", "success")
                return redirect(url_for("master_data.view_item", item_id=item.id))

    return render_template(
        "master_data/item_form.html",
        mode="edit",
        item=item,
        values=values,
        errors=errors,
        units=units,
        item_types=ITEM_TYPES,
    )


@master_data_bp.route("/items/<int:item_id>/toggle-active", methods=["POST"])
@roles_required(*MASTER_WRITE_ROLES)
def toggle_item_active(item_id: int):
    require_csrf_token()
    item = _get_or_404(Item, item_id)

    if item.is_active:
        active_boms = _active_boms_using_component(item.id)
        if active_boms:
            bom_list = ", ".join(f"{bom.name} (v{bom.version})" for bom in active_boms)
            flash(
                f"Cannot deactivate item '{item.name}' because it is a component of "
                f"active BOM(s): {bom_list}. Deactivate the BOM(s) first.",
                "danger",
            )
            return redirect(url_for("master_data.view_item", item_id=item.id))

    old_active = item.is_active
    new_active = not old_active
    state_word = "activated" if new_active else "deactivated"
    try:
        item.is_active = new_active
        log_audit(
            user_id=current_user.id,
            action="item_activated" if new_active else "item_deactivated",
            entity_type="item",
            entity_id=item.id,
            summary=f"{state_word.title()} item '{item.name}' ({item.sku})",
            old_values={"is_active": old_active},
            new_values={"is_active": new_active},
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash(
            f"Failed to update status for item '{item.name}'. No changes were saved.",
            "danger",
        )
        return redirect(url_for("master_data.view_item", item_id=item.id))

    flash(f"Item '{item.name}' {state_word}.", "info")
    return redirect(url_for("master_data.view_item", item_id=item.id))


@master_data_bp.route("/items/<int:item_id>/conversions", methods=["POST"])
@roles_required(*MASTER_WRITE_ROLES)
def add_item_conversion(item_id: int):
    require_csrf_token()
    item = _get_or_404(Item, item_id)
    cleaned, errors, values = _validate_conversion_form(request.form, fixed_item=item)

    if not errors:
        conversion = ItemUnitConversion(**cleaned)
        db.session.add(conversion)
        try:
            db.session.flush()
            validate_item_conversion_graph(item)
            db.session.commit()
        except UnitConversionError as exc:
            db.session.rollback()
            errors["multiplier"] = str(exc)
            flash(f"Cannot add conversion: {exc}", "danger")
        except IntegrityError:
            db.session.rollback()
            errors["to_unit_id"] = "Could not save conversion due to duplicate or invalid units."
        else:
            flash("Item unit conversion added.", "success")
            return redirect(url_for("master_data.view_item", item_id=item.id))

    compatible_units = db.session.execute(
        db.select(Unit)
        .where(Unit.dimension == item.stock_unit.dimension)
        .order_by(Unit.code.asc())
    ).scalars().all()
    all_units = db.session.execute(
        db.select(Unit).order_by(Unit.dimension.asc(), Unit.code.asc())
    ).scalars().all()
    item_boms = (
        db.session.execute(
            db.select(BOM)
            .where(BOM.finished_item_id == item.id)
            .order_by(BOM.version.desc())
        ).scalars().all()
        if item.item_type == "finished_good"
        else []
    )

    stock_position = get_item_stock_position(item.id)
    can_view_valuation = user_has_permission(current_user, CAP_VIEW_FINANCIAL_REPORTS)

    return render_template(
        "master_data/item_detail.html",
        item=item,
        item_boms=item_boms,
        stock_position=stock_position,
        can_view_valuation=can_view_valuation,
        item_type_labels=ITEM_TYPE_LABELS,
        dimension_labels=UNIT_DIMENSION_LABELS,
        compatible_units=compatible_units,
        all_units=all_units,
        conversion_values=values,
        conversion_errors=errors,
        format_bps=format_bps_as_percent,
        format_paise=format_paise_as_rupees,
        format_exact_rupees=format_exact_rupees,
        format_milli=format_milli,
    )


@master_data_bp.route(
    "/items/<int:item_id>/conversions/<int:conversion_id>/edit", methods=["POST"]
)
@roles_required(*MASTER_WRITE_ROLES)
def edit_item_conversion(item_id: int, conversion_id: int):
    require_csrf_token()
    item = _get_or_404(Item, item_id)
    conversion = _get_or_404(ItemUnitConversion, conversion_id)
    if conversion.item_id != item.id:
        abort(404)

    form_data = {
        "from_unit_id": str(conversion.from_unit_id),
        "to_unit_id": str(conversion.to_unit_id),
        "multiplier": request.form.get("multiplier", ""),
    }
    cleaned, errors, _values = _validate_conversion_form(
        form_data,
        fixed_item=item,
        existing_conversion=conversion,
    )
    if errors:
        first_error = next(iter(errors.values()))
        flash(first_error, "danger")
        return redirect(url_for("master_data.view_item", item_id=item.id))

    old_multiplier = conversion.multiplier
    multiplier_changed = cleaned["multiplier"] != old_multiplier

    locked_bom_items = db.session.execute(
        db.select(BOMItem)
        .join(BOM, BOMItem.bom_id == BOM.id)
        .where(
            BOMItem.material_item_id == item.id,
            db.exists(db.select(ProductionRun.id).where(ProductionRun.bom_id == BOM.id)),
        )
    ).scalars().all()

    locked_before: dict[int, tuple] = {}
    if multiplier_changed:
        for bom_item in locked_bom_items:
            if bom_item.input_unit_id != item.stock_unit_id:
                try:
                    old_factor = get_conversion_factor(item, bom_item.input_unit, item.stock_unit)
                except UnitConversionError:
                    old_factor = None
                try:
                    factor_without = get_conversion_factor(
                        item,
                        bom_item.input_unit,
                        item.stock_unit,
                        exclude_conversion_id=conversion.id,
                    )
                except UnitConversionError:
                    factor_without = None
                locked_before[bom_item.id] = (old_factor, factor_without)

    conversion.multiplier = cleaned["multiplier"]
    try:
        db.session.flush()
        validate_item_conversion_graph(item)
    except UnitConversionError as exc:
        db.session.rollback()
        flash(f"Cannot update conversion: {exc}", "danger")
        return redirect(url_for("master_data.view_item", item_id=item.id))

    if multiplier_changed:
        for bom_item in locked_bom_items:
            if bom_item.id in locked_before:
                old_factor, factor_without = locked_before[bom_item.id]
                new_factor = get_conversion_factor(item, bom_item.input_unit, item.stock_unit)
                if factor_without is None or new_factor != old_factor:
                    db.session.rollback()
                    flash(
                        f"Cannot update conversion multiplier because BOM '{bom_item.bom.name}' "
                        f"(v{bom_item.bom.version}) is locked by production runs and depends on "
                        f"this conversion for historical quantity interpretation.",
                        "danger",
                    )
                    return redirect(url_for("master_data.view_item", item_id=item.id))

    dependent_bom_items = db.session.execute(
        db.select(BOMItem).where(BOMItem.material_item_id == item.id)
    ).scalars().all()
    for bom_item in dependent_bom_items:
        try:
            convert_qty_milli_to_stock(
                item,
                bom_item.standard_qty_milli,
                bom_item.input_unit,
            )
        except UnitConversionError as exc:
            db.session.rollback()
            flash(
                f"Cannot update conversion multiplier because BOM '{bom_item.bom.name}' "
                f"(v{bom_item.bom.version}) would lose exact conversion: {exc}",
                "danger",
            )
            return redirect(url_for("master_data.view_item", item_id=item.id))

    db.session.commit()
    flash("Item unit conversion updated.", "success")
    return redirect(url_for("master_data.view_item", item_id=item.id))


@master_data_bp.route(
    "/items/<int:item_id>/conversions/<int:conversion_id>/delete", methods=["POST"]
)
@roles_required(*MASTER_WRITE_ROLES)
def delete_item_conversion(item_id: int, conversion_id: int):
    require_csrf_token()
    item = _get_or_404(Item, item_id)
    conversion = _get_or_404(ItemUnitConversion, conversion_id)
    if conversion.item_id != item.id:
        abort(404)

    dependent_bom_items = db.session.execute(
        db.select(BOMItem).where(BOMItem.material_item_id == item.id)
    ).scalars().all()
    for bom_item in dependent_bom_items:
        try:
            convert_qty_milli_to_stock(
                item,
                bom_item.standard_qty_milli,
                bom_item.input_unit,
                exclude_conversion_id=conversion.id,
            )
        except UnitConversionError:
            flash(
                f"Cannot remove conversion '1 {conversion.from_unit.code} = "
                f"{conversion.multiplier} {conversion.to_unit.code}' because BOM "
                f"'{bom_item.bom.name}' (v{bom_item.bom.version}) depends on it.",
                "danger",
            )
            return redirect(url_for("master_data.view_item", item_id=item.id))

    db.session.delete(conversion)
    db.session.commit()
    flash("Item unit conversion removed.", "info")
    return redirect(url_for("master_data.view_item", item_id=item.id))


# ---------------------------------------------------------------------------
# B. Customers Routes
# ---------------------------------------------------------------------------


@master_data_bp.route("/customers")
@roles_required(*CUSTOMER_VIEW_ROLES)
def list_customers():
    customer_type_filter = (request.args.get("customer_type") or "").strip()
    status_filter = (request.args.get("status") or "all").strip()

    query = db.select(Customer).order_by(Customer.name.asc())
    if customer_type_filter in CUSTOMER_TYPE_LABELS:
        query = query.where(Customer.customer_type == customer_type_filter)
    else:
        customer_type_filter = ""

    if status_filter == "active":
        query = query.where(Customer.is_active.is_(True))
    elif status_filter == "inactive":
        query = query.where(Customer.is_active.is_(False))
    else:
        status_filter = "all"

    customers = db.session.execute(query).scalars().all()
    return render_template(
        "master_data/customers_list.html",
        customers=customers,
        customer_types=CUSTOMER_TYPES,
        customer_type_labels=CUSTOMER_TYPE_LABELS,
        customer_type_filter=customer_type_filter,
        status_filter=status_filter,
    )


@master_data_bp.route("/customers/new", methods=["GET", "POST"])
@roles_required(*MASTER_WRITE_ROLES)
def create_customer():
    values = {
        "name": "",
        "customer_type": "",
        "gstin": "",
        "state": "",
        "address": "",
        "phone": "",
        "is_active": True,
    }
    errors: dict[str, str] = {}

    if request.method == "POST":
        require_csrf_token()
        cleaned, errors, values = _validate_customer_form(request.form)
        if not errors:
            customer = Customer(**cleaned)
            db.session.add(customer)
            db.session.commit()
            flash(f"Customer '{customer.name}' created.", "success")
            return redirect(url_for("master_data.view_customer", customer_id=customer.id))

    return render_template(
        "master_data/customer_form.html",
        mode="create",
        customer=None,
        values=values,
        errors=errors,
        customer_types=CUSTOMER_TYPES,
        indian_states=INDIAN_STATES,
    )


@master_data_bp.route("/customers/<int:customer_id>")
@roles_required(*CUSTOMER_VIEW_ROLES)
def view_customer(customer_id: int):
    customer = _get_or_404(Customer, customer_id)
    return render_template(
        "master_data/customer_detail.html",
        customer=customer,
        customer_type_labels=CUSTOMER_TYPE_LABELS,
    )


@master_data_bp.route("/customers/<int:customer_id>/edit", methods=["GET", "POST"])
@roles_required(*MASTER_WRITE_ROLES)
def edit_customer(customer_id: int):
    customer = _get_or_404(Customer, customer_id)
    values = {
        "name": customer.name,
        "customer_type": customer.customer_type,
        "gstin": customer.gstin or "",
        "state": customer.state,
        "address": customer.address or "",
        "phone": customer.phone or "",
        "is_active": customer.is_active,
    }
    errors: dict[str, str] = {}

    if request.method == "POST":
        require_csrf_token()
        cleaned, errors, values = _validate_customer_form(request.form)
        if not errors:
            for field, val in cleaned.items():
                setattr(customer, field, val)
            db.session.commit()
            flash(f"Customer '{customer.name}' updated.", "success")
            return redirect(url_for("master_data.view_customer", customer_id=customer.id))

    return render_template(
        "master_data/customer_form.html",
        mode="edit",
        customer=customer,
        values=values,
        errors=errors,
        customer_types=CUSTOMER_TYPES,
        indian_states=INDIAN_STATES,
    )


@master_data_bp.route("/customers/<int:customer_id>/toggle-active", methods=["POST"])
@roles_required(*MASTER_WRITE_ROLES)
def toggle_customer_active(customer_id: int):
    require_csrf_token()
    customer = _get_or_404(Customer, customer_id)
    customer.is_active = not customer.is_active
    db.session.commit()
    state_word = "activated" if customer.is_active else "deactivated"
    flash(f"Customer '{customer.name}' {state_word}.", "info")
    return redirect(url_for("master_data.view_customer", customer_id=customer.id))


# ---------------------------------------------------------------------------
# C. Suppliers Routes
# ---------------------------------------------------------------------------


@master_data_bp.route("/suppliers")
@roles_required(*SUPPLIER_VIEW_ROLES)
def list_suppliers():
    status_filter = (request.args.get("status") or "all").strip()

    query = db.select(Supplier).order_by(Supplier.name.asc())
    if status_filter == "active":
        query = query.where(Supplier.is_active.is_(True))
    elif status_filter == "inactive":
        query = query.where(Supplier.is_active.is_(False))
    else:
        status_filter = "all"

    suppliers = db.session.execute(query).scalars().all()
    return render_template(
        "master_data/suppliers_list.html",
        suppliers=suppliers,
        status_filter=status_filter,
    )


@master_data_bp.route("/suppliers/new", methods=["GET", "POST"])
@roles_required(*MASTER_WRITE_ROLES)
def create_supplier():
    values = {
        "name": "",
        "gstin": "",
        "state": "",
        "address": "",
        "phone": "",
        "is_active": True,
    }
    errors: dict[str, str] = {}

    if request.method == "POST":
        require_csrf_token()
        cleaned, errors, values = _validate_supplier_form(request.form)
        if not errors:
            supplier = Supplier(**cleaned)
            db.session.add(supplier)
            db.session.commit()
            flash(f"Supplier '{supplier.name}' created.", "success")
            return redirect(url_for("master_data.view_supplier", supplier_id=supplier.id))

    return render_template(
        "master_data/supplier_form.html",
        mode="create",
        supplier=None,
        values=values,
        errors=errors,
        indian_states=INDIAN_STATES,
    )


@master_data_bp.route("/suppliers/<int:supplier_id>")
@roles_required(*SUPPLIER_VIEW_ROLES)
def view_supplier(supplier_id: int):
    supplier = _get_or_404(Supplier, supplier_id)
    return render_template(
        "master_data/supplier_detail.html",
        supplier=supplier,
    )


@master_data_bp.route("/suppliers/<int:supplier_id>/edit", methods=["GET", "POST"])
@roles_required(*MASTER_WRITE_ROLES)
def edit_supplier(supplier_id: int):
    supplier = _get_or_404(Supplier, supplier_id)
    values = {
        "name": supplier.name,
        "gstin": supplier.gstin or "",
        "state": supplier.state,
        "address": supplier.address or "",
        "phone": supplier.phone or "",
        "is_active": supplier.is_active,
    }
    errors: dict[str, str] = {}

    if request.method == "POST":
        require_csrf_token()
        cleaned, errors, values = _validate_supplier_form(request.form)
        if not errors:
            for field, val in cleaned.items():
                setattr(supplier, field, val)
            db.session.commit()
            flash(f"Supplier '{supplier.name}' updated.", "success")
            return redirect(url_for("master_data.view_supplier", supplier_id=supplier.id))

    return render_template(
        "master_data/supplier_form.html",
        mode="edit",
        supplier=supplier,
        values=values,
        errors=errors,
        indian_states=INDIAN_STATES,
    )


@master_data_bp.route("/suppliers/<int:supplier_id>/toggle-active", methods=["POST"])
@roles_required(*MASTER_WRITE_ROLES)
def toggle_supplier_active(supplier_id: int):
    require_csrf_token()
    supplier = _get_or_404(Supplier, supplier_id)
    supplier.is_active = not supplier.is_active
    db.session.commit()
    state_word = "activated" if supplier.is_active else "deactivated"
    flash(f"Supplier '{supplier.name}' {state_word}.", "info")
    return redirect(url_for("master_data.view_supplier", supplier_id=supplier.id))


# ---------------------------------------------------------------------------
# D. Units & Conversions Routes
# ---------------------------------------------------------------------------


@master_data_bp.route("/units")
@roles_required(*ITEM_AND_UNIT_VIEW_ROLES)
def list_units():
    units = db.session.execute(
        db.select(Unit).order_by(Unit.dimension.asc(), Unit.code.asc())
    ).scalars().all()
    conversions = db.session.execute(
        db.select(ItemUnitConversion)
        .join(Item, ItemUnitConversion.item_id == Item.id)
        .order_by(Item.name.asc(), ItemUnitConversion.id.asc())
    ).scalars().all()
    return render_template(
        "master_data/units_list.html",
        units=units,
        conversions=conversions,
        dimension_labels=UNIT_DIMENSION_LABELS,
    )


@master_data_bp.route("/units/new", methods=["GET", "POST"])
@roles_required(*MASTER_WRITE_ROLES)
def create_unit():
    existing_units = db.session.execute(
        db.select(Unit).order_by(Unit.dimension.asc(), Unit.code.asc())
    ).scalars().all()

    values = {
        "code": "",
        "name": "",
        "dimension": "",
        "base_unit_id": "",
        "factor_to_base": "1",
    }
    errors: dict[str, str] = {}

    if request.method == "POST":
        require_csrf_token()
        cleaned, errors, values = _validate_unit_form(request.form)
        if not errors:
            unit = Unit(**cleaned)
            db.session.add(unit)
            try:
                db.session.flush()
                validate_dimension_unit_graph(unit.dimension)
                db.session.commit()
            except UnitConversionError as exc:
                db.session.rollback()
                errors["factor_to_base"] = str(exc)
                flash(f"Cannot create unit: {exc}", "danger")
            except IntegrityError:
                db.session.rollback()
                errors["code"] = "Could not save unit because code must be unique."
            else:
                flash(f"Unit '{unit.code}' ({unit.name}) created.", "success")
                return redirect(url_for("master_data.list_units"))

    return render_template(
        "master_data/unit_form.html",
        mode="create",
        unit=None,
        values=values,
        errors=errors,
        dimensions=UNIT_DIMENSIONS,
        existing_units=existing_units,
    )


@master_data_bp.route("/units/<int:unit_id>/edit", methods=["GET", "POST"])
@roles_required(*MASTER_WRITE_ROLES)
def edit_unit(unit_id: int):
    unit = _get_or_404(Unit, unit_id)
    existing_units = db.session.execute(
        db.select(Unit)
        .where(Unit.id != unit.id)
        .order_by(Unit.dimension.asc(), Unit.code.asc())
    ).scalars().all()

    values = {
        "code": unit.code,
        "name": unit.name,
        "dimension": unit.dimension,
        "base_unit_id": str(unit.base_unit_id) if unit.base_unit_id else "",
        "factor_to_base": str(unit.factor_to_base),
    }
    errors: dict[str, str] = {}

    if request.method == "POST":
        require_csrf_token()
        cleaned, errors, values = _validate_unit_form(request.form, existing_unit=unit)
        if not errors:
            ratio_changed = (
                unit.base_unit_id != cleaned["base_unit_id"]
                or unit.factor_to_base != cleaned["factor_to_base"]
            )
            locked_before: dict[int, tuple] = {}
            direct_locked_bom = None
            if ratio_changed:
                direct_locked_bom_item = db.session.execute(
                    db.select(BOMItem)
                    .join(BOM, BOMItem.bom_id == BOM.id)
                    .join(Item, BOMItem.material_item_id == Item.id)
                    .where(
                        db.exists(db.select(ProductionRun.id).where(ProductionRun.bom_id == BOM.id)),
                        (BOMItem.input_unit_id == unit.id) | (Item.stock_unit_id == unit.id),
                    )
                    .limit(1)
                ).scalar_one_or_none()
                if direct_locked_bom_item is not None:
                    direct_locked_bom = direct_locked_bom_item.bom
                else:
                    direct_locked_bom = db.session.execute(
                        db.select(BOM)
                        .join(Item, BOM.finished_item_id == Item.id)
                        .where(
                            db.exists(
                                db.select(ProductionRun.id).where(ProductionRun.bom_id == BOM.id)
                            ),
                            Item.stock_unit_id == unit.id,
                        )
                        .limit(1)
                    ).scalar_one_or_none()

                locked_dim_bom_items = db.session.execute(
                    db.select(BOMItem)
                    .join(BOM, BOMItem.bom_id == BOM.id)
                    .join(Unit, BOMItem.input_unit_id == Unit.id)
                    .where(
                        Unit.dimension == unit.dimension,
                        db.exists(db.select(ProductionRun.id).where(ProductionRun.bom_id == BOM.id)),
                    )
                ).scalars().all()
                for bom_item in locked_dim_bom_items:
                    if bom_item.input_unit_id != bom_item.material_item.stock_unit_id:
                        try:
                            old_factor = get_conversion_factor(
                                bom_item.material_item,
                                bom_item.input_unit,
                                bom_item.material_item.stock_unit,
                            )
                        except UnitConversionError:
                            old_factor = None
                        locked_before[bom_item.id] = (bom_item, old_factor)

            if ratio_changed and direct_locked_bom is not None:
                msg = (
                    f"Cannot change unit conversion factor because BOM '{direct_locked_bom.name}' "
                    f"(v{direct_locked_bom.version}) is locked by production runs and references "
                    f"unit '{unit.code}'."
                )
                errors["factor_to_base"] = msg
                flash(msg, "danger")
            else:
                for field, val in cleaned.items():
                    setattr(unit, field, val)
                try:
                    db.session.flush()
                    validate_dimension_unit_graph(unit.dimension)
                    if ratio_changed:
                        for bom_item, old_factor in locked_before.values():
                            new_factor = get_conversion_factor(
                                bom_item.material_item,
                                bom_item.input_unit,
                                bom_item.material_item.stock_unit,
                            )
                            if new_factor is None or new_factor != old_factor:
                                db.session.rollback()
                                msg = (
                                    f"Cannot change unit conversion factor because BOM "
                                    f"'{bom_item.bom.name}' (v{bom_item.bom.version}) is locked "
                                    f"by production runs and its historical quantities would be "
                                    f"reinterpreted."
                                )
                                errors["factor_to_base"] = msg
                                flash(msg, "danger")
                                break

                        if not errors:
                            dim_bom_items = db.session.execute(
                                db.select(BOMItem)
                                .join(Unit, BOMItem.input_unit_id == Unit.id)
                                .where(Unit.dimension == unit.dimension)
                            ).scalars().all()
                            for bom_item in dim_bom_items:
                                try:
                                    convert_qty_milli_to_stock(
                                        bom_item.material_item,
                                        bom_item.standard_qty_milli,
                                        bom_item.input_unit,
                                    )
                                except UnitConversionError as exc:
                                    db.session.rollback()
                                    errors["factor_to_base"] = (
                                        f"Cannot change unit ratio because BOM '{bom_item.bom.name}' "
                                        f"(v{bom_item.bom.version}) depends on it: {exc}"
                                    )
                                    break
                    if not errors:
                        db.session.commit()
                except UnitConversionError as exc:
                    db.session.rollback()
                    errors["factor_to_base"] = str(exc)
                    flash(f"Cannot update unit: {exc}", "danger")
                except IntegrityError:
                    db.session.rollback()
                    errors["code"] = "Could not update unit because code must be unique."
                else:
                    if not errors:
                        flash(f"Unit '{unit.code}' updated.", "success")
                        return redirect(url_for("master_data.list_units"))

    return render_template(
        "master_data/unit_form.html",
        mode="edit",
        unit=unit,
        values=values,
        errors=errors,
        dimensions=UNIT_DIMENSIONS,
        existing_units=existing_units,
    )


@master_data_bp.route("/conversions/new", methods=["GET", "POST"])
@roles_required(*MASTER_WRITE_ROLES)
def create_conversion():
    items = db.session.execute(
        db.select(Item).order_by(Item.name.asc(), Item.sku.asc())
    ).scalars().all()
    units = db.session.execute(
        db.select(Unit).order_by(Unit.dimension.asc(), Unit.code.asc())
    ).scalars().all()

    values = {
        "item_id": request.args.get("item_id", ""),
        "from_unit_id": "",
        "to_unit_id": "",
        "multiplier": "",
    }
    errors: dict[str, str] = {}

    if request.method == "POST":
        require_csrf_token()
        cleaned, errors, values = _validate_conversion_form(request.form)
        if not errors:
            conversion = ItemUnitConversion(**cleaned)
            db.session.add(conversion)
            try:
                db.session.flush()
                target_item = db.session.get(Item, cleaned["item_id"])
                if target_item is not None:
                    validate_item_conversion_graph(target_item)
                db.session.commit()
            except UnitConversionError as exc:
                db.session.rollback()
                errors["multiplier"] = str(exc)
                flash(f"Cannot create conversion: {exc}", "danger")
            except IntegrityError:
                db.session.rollback()
                errors["to_unit_id"] = "Could not save conversion due to duplicate or invalid units."
            else:
                flash("Item unit conversion created.", "success")
                return redirect(url_for("master_data.list_units"))

    return render_template(
        "master_data/conversion_form.html",
        values=values,
        errors=errors,
        items=items,
        units=units,
        dimension_labels=UNIT_DIMENSION_LABELS,
    )

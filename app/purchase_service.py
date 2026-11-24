"""Purchase management domain service.

Implements sequential Indian financial-year purchase numbering, exact integer/Decimal
line and GST calculations, draft creation/editing, atomic posting to the immutable
stock ledger via `post_stock_receipt()`, and atomic draft/posted cancellation via
`post_ledger_reversal()`.

All monetary values are stored and calculated in integer paise (rupees * 100),
quantities in integer thousandths of the unit (*_milli), and GST rates in integer
basis points (percent * 100). Floating-point arithmetic is never used.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app, has_app_context
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from app.audit import log_audit
from app.extensions import db
from app.inventory_service import (
    financial_year_for_date,
    post_ledger_reversal,
    post_stock_receipt,
)
from app.models import Item, Purchase, PurchaseItem, StockLedger, Supplier, Unit, User
from app.money import MAX_SCALED_INT, divide_round_half_up
from app.permissions import (
    CAP_RECORD_PURCHASES,
    CAP_VIEW_FINANCIAL_REPORTS,
    role_has_permission,
)
from app.units import MAX_MILLI_QTY, UnitConversionError, convert_qty_milli_to_stock

VALID_GST_TYPES: frozenset[str] = frozenset({"none", "intra_state", "inter_state"})
VALID_PURCHASE_STATUSES: frozenset[str] = frozenset({"draft", "posted", "cancelled"})
MAX_GST_RATE_BPS = 10_000  # 100.00%
# Keeps (taxable_value_paise * 10_000) + 10_000 safely within signed 64-bit SQLite INTEGER.
MAX_TAXABLE_PAISE = 900_000_000_000_000
MAX_SUPPLIER_INVOICE_NO_LEN = 80
MAX_NUMBER_ALLOCATION_RETRIES = 5

DEFAULT_BUSINESS_TIMEZONE = "Asia/Kolkata"
_IST_FALLBACK = timezone(timedelta(hours=5, minutes=30))

_UNSET = object()


class PurchaseError(ValueError):
    """Base exception for purchase workflow and validation errors."""


class PurchaseNotFoundError(PurchaseError):
    """Raised when a referenced purchase or purchase line does not exist."""


class PurchaseStateError(PurchaseError):
    """Raised when an operation violates the purchase status lifecycle."""


class PurchaseValidationError(PurchaseError):
    """Raised when purchase input or master-data invariants fail validation."""


class UnauthorizedPurchaseActionError(PermissionError):
    """Raised when an actor is missing, inactive, or lacks required purchase permissions."""


@dataclass(frozen=True)
class PurchaseLineInput:
    """Input payload for a single purchase line item."""

    item_id: int
    entered_qty_milli: int
    unit_rate_paise: int
    entered_unit_id: int | None = None
    gst_rate_bps: int | None = None


@dataclass(frozen=True)
class ComputedPurchaseLine:
    """Exact integer-computed stock quantity, taxable value, GST split, and line total."""

    item_id: int
    entered_unit_id: int
    entered_qty_milli: int
    qty_stock_milli: int
    unit_rate_paise: int
    taxable_value_paise: int
    gst_rate_bps: int
    cgst_paise: int
    sgst_paise: int
    igst_paise: int
    line_total_paise: int


@dataclass(frozen=True)
class ComputedPurchaseTotals:
    """Exact integer-summed purchase header totals."""

    subtotal_paise: int
    cgst_paise: int
    sgst_paise: int
    igst_paise: int
    total_paise: int


# ---------------------------------------------------------------------------
# Internal Validation, Authorization & Snapshot Helpers
# ---------------------------------------------------------------------------


def _is_strict_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_strict_date(value: object) -> bool:
    return isinstance(value, date) and not isinstance(value, datetime)


def get_business_today() -> date:
    """Return the current business-local date (defaulting to Asia/Kolkata / IST)."""
    tz_name = DEFAULT_BUSINESS_TIMEZONE
    if has_app_context():
        tz_name = current_app.config.get("BUSINESS_TIMEZONE", DEFAULT_BUSINESS_TIMEZONE)
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz = _IST_FALLBACK if tz_name == DEFAULT_BUSINESS_TIMEZONE else timezone.utc
    return datetime.now(tz).date()


def _require_actor(actor_user_id: int, *capabilities: str) -> User:
    """Load an active user and verify they hold at least one required capability."""
    if not _is_strict_int(actor_user_id) or actor_user_id <= 0:
        raise UnauthorizedPurchaseActionError("Active authorized user is required.")
    user = db.session.get(User, actor_user_id)
    if user is None or not user.is_active:
        raise UnauthorizedPurchaseActionError("Active authorized user is required.")
    if not any(role_has_permission(user.role, cap) for cap in capabilities):
        raise UnauthorizedPurchaseActionError(
            "You do not have permission to perform this purchase operation."
        )
    return user


def _require_purchase_writer(actor_user_id: int) -> User:
    return _require_actor(actor_user_id, CAP_RECORD_PURCHASES)


def _require_purchase_reader(actor_user_id: int) -> User:
    return _require_actor(actor_user_id, CAP_RECORD_PURCHASES, CAP_VIEW_FINANCIAL_REPORTS)


def _validate_gst_type(gst_type: object) -> str:
    if not isinstance(gst_type, str) or gst_type not in VALID_GST_TYPES:
        raise PurchaseValidationError(
            f"Invalid GST type: {gst_type!r}. Must be one of: none, intra_state, inter_state."
        )
    return gst_type


def _validate_purchase_date(purchase_date: object, *, label: str = "Purchase date") -> date:
    if not _is_strict_date(purchase_date):
        raise PurchaseValidationError(f"{label} must be a valid date.")
    return purchase_date


def _clean_optional_text(
    raw: object,
    *,
    field_label: str,
    max_length: int | None = None,
) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise PurchaseValidationError(f"{field_label} must be text.")
    cleaned = raw.strip()
    if not cleaned:
        return None
    if max_length is not None and len(cleaned) > max_length:
        raise PurchaseValidationError(
            f"{field_label} must be {max_length} characters or fewer."
        )
    return cleaned


def _load_active_supplier(supplier_id: object) -> Supplier:
    if not _is_strict_int(supplier_id) or supplier_id <= 0:
        raise PurchaseValidationError("Select a valid supplier.")
    supplier = db.session.get(Supplier, supplier_id)
    if supplier is None:
        raise PurchaseValidationError("Selected supplier does not exist.")
    if not supplier.is_active:
        raise PurchaseValidationError(
            f"Supplier '{supplier.name}' is inactive and cannot be used for purchases."
        )
    return supplier


def _load_active_item(item_id: object) -> Item:
    if not _is_strict_int(item_id) or item_id <= 0:
        raise PurchaseValidationError("Select a valid item.")
    item = db.session.get(Item, item_id)
    if item is None:
        raise PurchaseValidationError("Selected item does not exist.")
    if not item.is_active:
        raise PurchaseValidationError(
            f"Item '{item.name}' is inactive and cannot be purchased."
        )
    return item


def _load_unit_for_item(item: Item, entered_unit_id: object | None) -> Unit:
    if entered_unit_id is None:
        unit = item.stock_unit or db.session.get(Unit, item.stock_unit_id)
        if unit is None:
            raise PurchaseValidationError(
                f"Item '{item.name}' does not have a valid stock unit."
            )
        return unit
    if not _is_strict_int(entered_unit_id) or entered_unit_id <= 0:
        raise PurchaseValidationError("Select a valid purchase unit.")
    unit = db.session.get(Unit, entered_unit_id)
    if unit is None:
        raise PurchaseValidationError("Selected purchase unit does not exist.")
    return unit


def _load_purchase_with_details(purchase_id: object) -> Purchase:
    if not _is_strict_int(purchase_id) or purchase_id <= 0:
        raise PurchaseNotFoundError("Purchase does not exist.")
    purchase = db.session.execute(
        db.select(Purchase)
        .options(
            joinedload(Purchase.supplier),
            joinedload(Purchase.creator),
            joinedload(Purchase.items).joinedload(PurchaseItem.item).joinedload(Item.stock_unit),
            joinedload(Purchase.items).joinedload(PurchaseItem.entered_unit),
        )
        .where(Purchase.id == purchase_id)
    ).unique().scalar_one_or_none()
    if purchase is None:
        raise PurchaseNotFoundError(f"Purchase #{purchase_id} does not exist.")
    return purchase


def _is_purchase_number_collision(exc: IntegrityError) -> bool:
    """Return True only when `exc` is a UNIQUE constraint violation on `purchases.purchase_number`."""
    message = str(getattr(exc, "orig", exc)).lower()
    return (
        "unique constraint failed: purchases.purchase_number" in message
        or (
            "unique" in message
            and "purchase_number" in message
            and "purchases" in message
        )
    )


def _format_receipt_notes(purchase: Purchase) -> str:
    if purchase.supplier_invoice_no:
        return f"Purchase {purchase.purchase_number} (Inv: {purchase.supplier_invoice_no})"
    return f"Purchase {purchase.purchase_number}"


def _purchase_line_snapshot(line: PurchaseItem) -> dict:
    return {
        "id": line.id,
        "item_id": line.item_id,
        "entered_unit_id": line.entered_unit_id,
        "entered_qty_milli": line.entered_qty_milli,
        "qty_stock_milli": line.qty_stock_milli,
        "unit_rate_paise": line.unit_rate_paise,
        "taxable_value_paise": line.taxable_value_paise,
        "gst_rate_bps": line.gst_rate_bps,
        "cgst_paise": line.cgst_paise,
        "sgst_paise": line.sgst_paise,
        "igst_paise": line.igst_paise,
        "line_total_paise": line.line_total_paise,
    }


def _purchase_snapshot(purchase: Purchase) -> dict:
    sorted_lines = sorted(purchase.items, key=lambda li: (li.id or 0, li.item_id))
    return {
        "id": purchase.id,
        "purchase_number": purchase.purchase_number,
        "supplier_id": purchase.supplier_id,
        "purchase_date": purchase.purchase_date.isoformat(),
        "supplier_invoice_no": purchase.supplier_invoice_no,
        "gst_type": purchase.gst_type,
        "subtotal_paise": purchase.subtotal_paise,
        "cgst_paise": purchase.cgst_paise,
        "sgst_paise": purchase.sgst_paise,
        "igst_paise": purchase.igst_paise,
        "total_paise": purchase.total_paise,
        "status": purchase.status,
        "notes": purchase.notes,
        "created_by": purchase.created_by,
        "items": [_purchase_line_snapshot(li) for li in sorted_lines],
    }


def _apply_header_totals(purchase: Purchase, totals: ComputedPurchaseTotals) -> None:
    purchase.subtotal_paise = totals.subtotal_paise
    purchase.cgst_paise = totals.cgst_paise
    purchase.sgst_paise = totals.sgst_paise
    purchase.igst_paise = totals.igst_paise
    purchase.total_paise = totals.total_paise


def _recompute_header_totals_from_orm_lines(purchase: Purchase) -> ComputedPurchaseTotals:
    subtotal = sum(li.taxable_value_paise for li in purchase.items)
    cgst = sum(li.cgst_paise for li in purchase.items)
    sgst = sum(li.sgst_paise for li in purchase.items)
    igst = sum(li.igst_paise for li in purchase.items)
    total = subtotal + cgst + sgst + igst
    if total > MAX_SCALED_INT:
        raise PurchaseValidationError("Purchase total exceeds maximum allowed amount.")
    totals = ComputedPurchaseTotals(
        subtotal_paise=subtotal,
        cgst_paise=cgst,
        sgst_paise=sgst,
        igst_paise=igst,
        total_paise=total,
    )
    _apply_header_totals(purchase, totals)
    return totals


def _validate_and_compute_line_inputs(
    lines: Sequence[PurchaseLineInput],
    *,
    gst_type: str,
) -> list[ComputedPurchaseLine]:
    computed: list[ComputedPurchaseLine] = []
    seen_item_ids: set[int] = set()

    for idx, line_in in enumerate(lines, start=1):
        if not isinstance(line_in, PurchaseLineInput):
            raise PurchaseValidationError(
                f"Line {idx} must be a PurchaseLineInput instance."
            )
        item = _load_active_item(line_in.item_id)
        if item.id in seen_item_ids:
            raise PurchaseValidationError(
                f"Item '{item.name}' cannot appear more than once in the same purchase."
            )
        seen_item_ids.add(item.id)

        entered_unit = _load_unit_for_item(item, line_in.entered_unit_id)
        computed.append(
            compute_purchase_line(
                item=item,
                entered_qty_milli=line_in.entered_qty_milli,
                unit_rate_paise=line_in.unit_rate_paise,
                gst_type=gst_type,
                entered_unit=entered_unit,
                gst_rate_bps=line_in.gst_rate_bps,
            )
        )

    return computed


# ---------------------------------------------------------------------------
# 1. Pure Calculation & Sequential FY Numbering Helpers
# ---------------------------------------------------------------------------


def generate_next_purchase_number(purchase_date: date) -> str:
    """Generate the next sequential financial-year purchase number (e.g. 'PUR/2026-27/0001')."""
    validated_date = _validate_purchase_date(purchase_date)
    fy = financial_year_for_date(validated_date)
    prefix = f"PUR/{fy}/"
    existing_numbers = db.session.execute(
        db.select(Purchase.purchase_number).where(
            Purchase.purchase_number.like(f"{prefix}%")
        )
    ).scalars().all()

    max_seq = 0
    for number in existing_numbers:
        suffix = number[len(prefix) :]
        if suffix.isdigit():
            max_seq = max(max_seq, int(suffix))

    return f"{prefix}{max_seq + 1:04d}"


def compute_purchase_line(
    *,
    item: Item,
    entered_qty_milli: int,
    unit_rate_paise: int,
    gst_type: str,
    entered_unit: Unit | None = None,
    gst_rate_bps: int | None = None,
) -> ComputedPurchaseLine:
    """Compute exact stock quantity, taxable value, GST split, and line total.

    `unit_rate_paise` is the rate per entered unit. Taxable value is rounded half-up
    to integer paise from `unit_rate_paise * entered_qty_milli / 1000`. GST amounts
    use integer half-up rounding matching `trg_purchases_lifecycle_and_immutability`.
    """
    validated_gst_type = _validate_gst_type(gst_type)

    if not _is_strict_int(entered_qty_milli) or entered_qty_milli <= 0:
        raise PurchaseValidationError(
            "Purchase quantity must be a positive integer milli-unit value."
        )
    if entered_qty_milli > MAX_MILLI_QTY:
        raise PurchaseValidationError("Purchase quantity exceeds maximum allowed size.")

    if not _is_strict_int(unit_rate_paise) or unit_rate_paise < 0:
        raise PurchaseValidationError(
            "Unit rate must be a non-negative integer paise amount."
        )
    if unit_rate_paise > MAX_SCALED_INT:
        raise PurchaseValidationError("Unit rate exceeds maximum allowed amount.")

    resolved_unit = entered_unit or item.stock_unit or db.session.get(Unit, item.stock_unit_id)
    if resolved_unit is None:
        raise PurchaseValidationError(f"Item '{item.name}' does not have a valid stock unit.")

    try:
        qty_stock_milli = convert_qty_milli_to_stock(item, entered_qty_milli, resolved_unit)
    except UnitConversionError as exc:
        raise PurchaseValidationError(str(exc)) from exc

    taxable_value_paise = divide_round_half_up(unit_rate_paise * entered_qty_milli, 1000)
    if taxable_value_paise > MAX_TAXABLE_PAISE:
        raise PurchaseValidationError("Line taxable value exceeds maximum allowed amount.")

    if validated_gst_type == "none":
        if gst_rate_bps is not None:
            if not _is_strict_int(gst_rate_bps) or gst_rate_bps < 0:
                raise PurchaseValidationError(
                    "GST rate must be a non-negative integer basis-points value."
                )
            if gst_rate_bps != 0:
                raise PurchaseValidationError(
                    "GST rate must be 0% when purchase GST type is 'none'."
                )
        resolved_gst_rate_bps = 0
        cgst_paise = 0
        sgst_paise = 0
        igst_paise = 0
    else:
        resolved_gst_rate_bps = item.gst_rate_bps if gst_rate_bps is None else gst_rate_bps
        if (
            not _is_strict_int(resolved_gst_rate_bps)
            or resolved_gst_rate_bps < 0
            or resolved_gst_rate_bps > MAX_GST_RATE_BPS
        ):
            raise PurchaseValidationError(
                "GST rate must be an integer basis-points value between 0 and 10000."
            )

        if validated_gst_type == "intra_state":
            cgst_paise = divide_round_half_up(
                taxable_value_paise * resolved_gst_rate_bps, 20_000
            )
            sgst_paise = divide_round_half_up(
                taxable_value_paise * resolved_gst_rate_bps, 20_000
            )
            igst_paise = 0
        else:
            cgst_paise = 0
            sgst_paise = 0
            igst_paise = divide_round_half_up(
                taxable_value_paise * resolved_gst_rate_bps, 10_000
            )

    line_total_paise = taxable_value_paise + cgst_paise + sgst_paise + igst_paise
    if line_total_paise > MAX_SCALED_INT:
        raise PurchaseValidationError("Line total exceeds maximum allowed amount.")

    return ComputedPurchaseLine(
        item_id=item.id,
        entered_unit_id=resolved_unit.id,
        entered_qty_milli=entered_qty_milli,
        qty_stock_milli=qty_stock_milli,
        unit_rate_paise=unit_rate_paise,
        taxable_value_paise=taxable_value_paise,
        gst_rate_bps=resolved_gst_rate_bps,
        cgst_paise=cgst_paise,
        sgst_paise=sgst_paise,
        igst_paise=igst_paise,
        line_total_paise=line_total_paise,
    )


def compute_purchase_totals(
    lines: Sequence[ComputedPurchaseLine],
) -> ComputedPurchaseTotals:
    """Sum exact integer line amounts into purchase header totals."""
    subtotal = sum(line.taxable_value_paise for line in lines)
    cgst = sum(line.cgst_paise for line in lines)
    sgst = sum(line.sgst_paise for line in lines)
    igst = sum(line.igst_paise for line in lines)
    total = subtotal + cgst + sgst + igst
    if total > MAX_SCALED_INT:
        raise PurchaseValidationError("Purchase total exceeds maximum allowed amount.")
    return ComputedPurchaseTotals(
        subtotal_paise=subtotal,
        cgst_paise=cgst,
        sgst_paise=sgst,
        igst_paise=igst,
        total_paise=total,
    )


# ---------------------------------------------------------------------------
# 2. RBAC-Checked Read & Traceability Queries
# ---------------------------------------------------------------------------


def list_purchases(
    *,
    actor_user_id: int,
    supplier_id: int | None = None,
    status: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[Purchase]:
    """Return purchases matching optional filters for an authorized reader."""
    _require_purchase_reader(actor_user_id)

    if supplier_id is not None and (not _is_strict_int(supplier_id) or supplier_id <= 0):
        raise PurchaseValidationError("Supplier filter must be a positive integer.")
    if status is not None and status not in VALID_PURCHASE_STATUSES:
        raise PurchaseValidationError(f"Invalid purchase status filter: {status!r}.")
    if date_from is not None:
        _validate_purchase_date(date_from, label="Start date")
    if date_to is not None:
        _validate_purchase_date(date_to, label="End date")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise PurchaseValidationError("Start date cannot be later than end date.")

    query = (
        db.select(Purchase)
        .options(
            joinedload(Purchase.supplier),
            joinedload(Purchase.creator),
        )
        .order_by(Purchase.purchase_date.desc(), Purchase.id.desc())
    )
    if supplier_id is not None:
        query = query.where(Purchase.supplier_id == supplier_id)
    if status is not None:
        query = query.where(Purchase.status == status)
    if date_from is not None:
        query = query.where(Purchase.purchase_date >= date_from)
    if date_to is not None:
        query = query.where(Purchase.purchase_date <= date_to)

    return db.session.execute(query).scalars().all()


def get_purchase(purchase_id: int, *, actor_user_id: int) -> Purchase:
    """Fetch a single purchase with supplier, creator, and line details."""
    _require_purchase_reader(actor_user_id)
    return _load_purchase_with_details(purchase_id)


def get_purchase_ledger_entries(
    purchase_id: int, *, actor_user_id: int
) -> list[StockLedger]:
    """Return all stock ledger entries (receipts and reversals) linked to `purchase_id`."""
    _require_purchase_reader(actor_user_id)
    purchase = _load_purchase_with_details(purchase_id)
    return db.session.execute(
        db.select(StockLedger)
        .options(
            joinedload(StockLedger.item).joinedload(Item.stock_unit),
            joinedload(StockLedger.creator),
        )
        .where(
            StockLedger.source_doc_type == "purchase",
            StockLedger.source_doc_id == purchase.id,
        )
        .order_by(StockLedger.movement_date.asc(), StockLedger.id.asc())
    ).scalars().all()


# ---------------------------------------------------------------------------
# 3. Draft Creation, Editing & Granular Line Operations (Atomic per Call)
# ---------------------------------------------------------------------------


def create_draft_purchase(
    *,
    supplier_id: int,
    purchase_date: date,
    actor_user_id: int,
    gst_type: str = "none",
    supplier_invoice_no: str | None = None,
    notes: str | None = None,
    lines: Sequence[PurchaseLineInput] = (),
) -> Purchase:
    """Create a new draft purchase with zero or more validated lines atomically.

    Retries purchase-number allocation up to `MAX_NUMBER_ALLOCATION_RETRIES` times
    only when a concurrent insert triggers a UNIQUE collision on `purchases.purchase_number`.
    """
    user = _require_purchase_writer(actor_user_id)
    supplier = _load_active_supplier(supplier_id)
    validated_date = _validate_purchase_date(purchase_date)
    validated_gst_type = _validate_gst_type(gst_type)
    cleaned_invoice_no = _clean_optional_text(
        supplier_invoice_no,
        field_label="Supplier invoice number",
        max_length=MAX_SUPPLIER_INVOICE_NO_LEN,
    )
    cleaned_notes = _clean_optional_text(notes, field_label="Notes")

    computed_lines = _validate_and_compute_line_inputs(lines, gst_type=validated_gst_type)
    totals = compute_purchase_totals(computed_lines)

    last_collision: IntegrityError | None = None
    for _attempt in range(MAX_NUMBER_ALLOCATION_RETRIES):
        try:
            purchase_number = generate_next_purchase_number(validated_date)
            purchase = Purchase(
                purchase_number=purchase_number,
                supplier_id=supplier.id,
                purchase_date=validated_date,
                supplier_invoice_no=cleaned_invoice_no,
                gst_type=validated_gst_type,
                subtotal_paise=totals.subtotal_paise,
                cgst_paise=totals.cgst_paise,
                sgst_paise=totals.sgst_paise,
                igst_paise=totals.igst_paise,
                total_paise=totals.total_paise,
                status="draft",
                notes=cleaned_notes,
                created_by=user.id,
            )
            db.session.add(purchase)
            db.session.flush()

            for cl in computed_lines:
                db.session.add(
                    PurchaseItem(
                        purchase_id=purchase.id,
                        item_id=cl.item_id,
                        entered_unit_id=cl.entered_unit_id,
                        entered_qty_milli=cl.entered_qty_milli,
                        qty_stock_milli=cl.qty_stock_milli,
                        unit_rate_paise=cl.unit_rate_paise,
                        taxable_value_paise=cl.taxable_value_paise,
                        gst_rate_bps=cl.gst_rate_bps,
                        cgst_paise=cl.cgst_paise,
                        sgst_paise=cl.sgst_paise,
                        igst_paise=cl.igst_paise,
                        line_total_paise=cl.line_total_paise,
                    )
                )
            db.session.flush()

            log_audit(
                user_id=user.id,
                action="purchase_created",
                entity_type="purchase",
                entity_id=purchase.id,
                summary=(
                    f"Created draft purchase {purchase.purchase_number} for "
                    f"supplier '{supplier.name}' ({len(computed_lines)} line(s))"
                ),
                new_values=_purchase_snapshot(purchase),
            )
            db.session.commit()
            return _load_purchase_with_details(purchase.id)
        except IntegrityError as exc:
            db.session.rollback()
            if _is_purchase_number_collision(exc):
                last_collision = exc
                continue
            raise
        except Exception:
            db.session.rollback()
            raise

    raise PurchaseError(
        "Could not allocate a unique purchase number after multiple attempts. Please retry."
    ) from last_collision


def update_draft_purchase(
    purchase_id: int,
    *,
    actor_user_id: int,
    supplier_id: int | None = None,
    purchase_date: date | None = None,
    gst_type: str | None = None,
    supplier_invoice_no: object = _UNSET,
    notes: object = _UNSET,
    lines: Sequence[PurchaseLineInput] | None = None,
) -> Purchase:
    """Edit header fields and/or replace lines on a draft purchase atomically.

    - If `purchase_date` moves to a different Indian financial year, regenerates
      `purchase_number` with bounded collision retry and logs old/new numbers in AuditLog.
    - When `lines` is omitted and `gst_type` changes:
      - switching to `'none'` zeroes all line GST rates and taxes;
      - switching from `'none'` to `'intra_state'`/`'inter_state'` re-seeds each line's
        `gst_rate_bps` from its item's current `gst_rate_bps` and recalculates totals;
      - switching between `'intra_state'` and `'inter_state'` preserves each line's
        `gst_rate_bps` and recalculates tax splits and totals.
    """
    user = _require_purchase_writer(actor_user_id)

    cleaned_invoice_no = (
        _UNSET
        if supplier_invoice_no is _UNSET
        else _clean_optional_text(
            supplier_invoice_no,
            field_label="Supplier invoice number",
            max_length=MAX_SUPPLIER_INVOICE_NO_LEN,
        )
    )
    cleaned_notes = (
        _UNSET
        if notes is _UNSET
        else _clean_optional_text(notes, field_label="Notes")
    )

    last_collision: IntegrityError | None = None
    for _attempt in range(MAX_NUMBER_ALLOCATION_RETRIES):
        purchase = _load_purchase_with_details(purchase_id)
        if purchase.status != "draft":
            raise PurchaseStateError(
                f"Purchase {purchase.purchase_number} cannot be edited because it is "
                f"{purchase.status}."
            )

        old_values = _purchase_snapshot(purchase)
        old_gst_type = purchase.gst_type
        old_fy = financial_year_for_date(purchase.purchase_date)

        new_supplier = (
            _load_active_supplier(supplier_id)
            if supplier_id is not None
            else purchase.supplier
        )
        new_date = (
            _validate_purchase_date(purchase_date)
            if purchase_date is not None
            else purchase.purchase_date
        )
        new_gst_type = (
            _validate_gst_type(gst_type) if gst_type is not None else old_gst_type
        )
        new_fy = financial_year_for_date(new_date)
        fy_changed = new_fy != old_fy

        computed_replacement_lines: list[ComputedPurchaseLine] | None = None
        recalculated_existing: list[tuple[PurchaseItem, ComputedPurchaseLine]] = []

        if lines is not None:
            computed_replacement_lines = _validate_and_compute_line_inputs(
                lines, gst_type=new_gst_type
            )
            totals = compute_purchase_totals(computed_replacement_lines)
        elif new_gst_type != old_gst_type:
            for existing_line in sorted(purchase.items, key=lambda li: li.id):
                item = _load_active_item(existing_line.item_id)
                entered_unit = _load_unit_for_item(item, existing_line.entered_unit_id)
                if new_gst_type == "none":
                    effective_bps: int | None = 0
                elif old_gst_type == "none":
                    effective_bps = item.gst_rate_bps
                else:
                    effective_bps = existing_line.gst_rate_bps

                recomputed = compute_purchase_line(
                    item=item,
                    entered_qty_milli=existing_line.entered_qty_milli,
                    unit_rate_paise=existing_line.unit_rate_paise,
                    gst_type=new_gst_type,
                    entered_unit=entered_unit,
                    gst_rate_bps=effective_bps,
                )
                recalculated_existing.append((existing_line, recomputed))
            totals = compute_purchase_totals([cl for _, cl in recalculated_existing])
        else:
            totals = ComputedPurchaseTotals(
                subtotal_paise=sum(li.taxable_value_paise for li in purchase.items),
                cgst_paise=sum(li.cgst_paise for li in purchase.items),
                sgst_paise=sum(li.sgst_paise for li in purchase.items),
                igst_paise=sum(li.igst_paise for li in purchase.items),
                total_paise=sum(li.line_total_paise for li in purchase.items),
            )

        try:
            if fy_changed:
                purchase.purchase_number = generate_next_purchase_number(new_date)

            purchase.supplier_id = new_supplier.id
            purchase.purchase_date = new_date
            purchase.gst_type = new_gst_type
            if cleaned_invoice_no is not _UNSET:
                purchase.supplier_invoice_no = cleaned_invoice_no
            if cleaned_notes is not _UNSET:
                purchase.notes = cleaned_notes
            # Apply header totals alongside gst_type before any intermediate flush so
            # ck_purchases_gst_split and ck_purchases_total_sum always hold.
            _apply_header_totals(purchase, totals)

            if computed_replacement_lines is not None:
                existing_by_item = {li.item_id: li for li in purchase.items}
                incoming_item_ids = {cl.item_id for cl in computed_replacement_lines}

                # Delete removed lines and flush before inserts to avoid UNIQUE collisions.
                removed_any = False
                for existing_line in list(purchase.items):
                    if existing_line.item_id not in incoming_item_ids:
                        purchase.items.remove(existing_line)
                        db.session.delete(existing_line)
                        removed_any = True
                if removed_any:
                    db.session.flush()

                for cl in computed_replacement_lines:
                    existing_line = existing_by_item.get(cl.item_id)
                    if existing_line is not None:
                        existing_line.entered_unit_id = cl.entered_unit_id
                        existing_line.entered_qty_milli = cl.entered_qty_milli
                        existing_line.qty_stock_milli = cl.qty_stock_milli
                        existing_line.unit_rate_paise = cl.unit_rate_paise
                        existing_line.taxable_value_paise = cl.taxable_value_paise
                        existing_line.gst_rate_bps = cl.gst_rate_bps
                        existing_line.cgst_paise = cl.cgst_paise
                        existing_line.sgst_paise = cl.sgst_paise
                        existing_line.igst_paise = cl.igst_paise
                        existing_line.line_total_paise = cl.line_total_paise
                    else:
                        new_line = PurchaseItem(
                            purchase_id=purchase.id,
                            item_id=cl.item_id,
                            entered_unit_id=cl.entered_unit_id,
                            entered_qty_milli=cl.entered_qty_milli,
                            qty_stock_milli=cl.qty_stock_milli,
                            unit_rate_paise=cl.unit_rate_paise,
                            taxable_value_paise=cl.taxable_value_paise,
                            gst_rate_bps=cl.gst_rate_bps,
                            cgst_paise=cl.cgst_paise,
                            sgst_paise=cl.sgst_paise,
                            igst_paise=cl.igst_paise,
                            line_total_paise=cl.line_total_paise,
                        )
                        purchase.items.append(new_line)
                        db.session.add(new_line)
            elif recalculated_existing:
                for existing_line, cl in recalculated_existing:
                    existing_line.entered_unit_id = cl.entered_unit_id
                    existing_line.qty_stock_milli = cl.qty_stock_milli
                    existing_line.taxable_value_paise = cl.taxable_value_paise
                    existing_line.gst_rate_bps = cl.gst_rate_bps
                    existing_line.cgst_paise = cl.cgst_paise
                    existing_line.sgst_paise = cl.sgst_paise
                    existing_line.igst_paise = cl.igst_paise
                    existing_line.line_total_paise = cl.line_total_paise

            db.session.flush()

            new_values = _purchase_snapshot(purchase)
            summary = f"Updated draft purchase {purchase.purchase_number}"
            if fy_changed:
                summary = (
                    f"Updated draft purchase {old_values['purchase_number']} "
                    f"(renumbered to {purchase.purchase_number} for FY {new_fy})"
                )

            log_audit(
                user_id=user.id,
                action="purchase_updated",
                entity_type="purchase",
                entity_id=purchase.id,
                summary=summary,
                old_values=old_values,
                new_values=new_values,
            )
            db.session.commit()
            return _load_purchase_with_details(purchase.id)
        except IntegrityError as exc:
            db.session.rollback()
            if fy_changed and _is_purchase_number_collision(exc):
                last_collision = exc
                continue
            raise
        except Exception:
            db.session.rollback()
            raise

    raise PurchaseError(
        "Could not allocate a unique purchase number after multiple attempts. Please retry."
    ) from last_collision


def add_draft_purchase_item(
    purchase_id: int,
    *,
    line: PurchaseLineInput,
    actor_user_id: int,
) -> PurchaseItem:
    """Add a single validated item line to a draft purchase atomically."""
    user = _require_purchase_writer(actor_user_id)
    if not isinstance(line, PurchaseLineInput):
        raise PurchaseValidationError("Line must be a PurchaseLineInput instance.")

    purchase = _load_purchase_with_details(purchase_id)
    if purchase.status != "draft":
        raise PurchaseStateError(
            f"Cannot add lines to purchase {purchase.purchase_number} because it is "
            f"{purchase.status}."
        )

    item = _load_active_item(line.item_id)
    if any(existing.item_id == item.id for existing in purchase.items):
        raise PurchaseValidationError(
            f"Item '{item.name}' is already listed on purchase {purchase.purchase_number}."
        )

    entered_unit = _load_unit_for_item(item, line.entered_unit_id)
    computed = compute_purchase_line(
        item=item,
        entered_qty_milli=line.entered_qty_milli,
        unit_rate_paise=line.unit_rate_paise,
        gst_type=purchase.gst_type,
        entered_unit=entered_unit,
        gst_rate_bps=line.gst_rate_bps,
    )

    old_values = _purchase_snapshot(purchase)
    try:
        new_item = PurchaseItem(
            purchase_id=purchase.id,
            item_id=computed.item_id,
            entered_unit_id=computed.entered_unit_id,
            entered_qty_milli=computed.entered_qty_milli,
            qty_stock_milli=computed.qty_stock_milli,
            unit_rate_paise=computed.unit_rate_paise,
            taxable_value_paise=computed.taxable_value_paise,
            gst_rate_bps=computed.gst_rate_bps,
            cgst_paise=computed.cgst_paise,
            sgst_paise=computed.sgst_paise,
            igst_paise=computed.igst_paise,
            line_total_paise=computed.line_total_paise,
        )
        purchase.items.append(new_item)
        db.session.add(new_item)
        db.session.flush()

        _recompute_header_totals_from_orm_lines(purchase)
        db.session.flush()

        log_audit(
            user_id=user.id,
            action="purchase_item_added",
            entity_type="purchase",
            entity_id=purchase.id,
            summary=(
                f"Added '{item.name}' to draft purchase {purchase.purchase_number}"
            ),
            old_values=old_values,
            new_values=_purchase_snapshot(purchase),
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    return new_item


def update_draft_purchase_item(
    purchase_id: int,
    purchase_item_id: int,
    *,
    entered_qty_milli: int,
    unit_rate_paise: int,
    actor_user_id: int,
    entered_unit_id: int | None = None,
    gst_rate_bps: int | None = None,
) -> PurchaseItem:
    """Update quantity, unit, rate, and/or GST rate on a draft purchase line atomically.

    If `entered_unit_id` is None, defaults to the item's base stock unit.
    If `gst_rate_bps` is None, defaults to 0 when `gst_type == 'none'`, otherwise
    the item's current `gst_rate_bps`.
    """
    user = _require_purchase_writer(actor_user_id)
    purchase = _load_purchase_with_details(purchase_id)
    if purchase.status != "draft":
        raise PurchaseStateError(
            f"Cannot update lines on purchase {purchase.purchase_number} because it is "
            f"{purchase.status}."
        )

    if not _is_strict_int(purchase_item_id) or purchase_item_id <= 0:
        raise PurchaseNotFoundError("Purchase line does not exist.")
    target = next((li for li in purchase.items if li.id == purchase_item_id), None)
    if target is None:
        raise PurchaseNotFoundError(
            f"Purchase line #{purchase_item_id} does not exist on purchase #{purchase.id}."
        )

    item = _load_active_item(target.item_id)
    entered_unit = _load_unit_for_item(item, entered_unit_id)
    computed = compute_purchase_line(
        item=item,
        entered_qty_milli=entered_qty_milli,
        unit_rate_paise=unit_rate_paise,
        gst_type=purchase.gst_type,
        entered_unit=entered_unit,
        gst_rate_bps=gst_rate_bps,
    )

    old_values = _purchase_snapshot(purchase)
    try:
        target.entered_unit_id = computed.entered_unit_id
        target.entered_qty_milli = computed.entered_qty_milli
        target.qty_stock_milli = computed.qty_stock_milli
        target.unit_rate_paise = computed.unit_rate_paise
        target.taxable_value_paise = computed.taxable_value_paise
        target.gst_rate_bps = computed.gst_rate_bps
        target.cgst_paise = computed.cgst_paise
        target.sgst_paise = computed.sgst_paise
        target.igst_paise = computed.igst_paise
        target.line_total_paise = computed.line_total_paise

        _recompute_header_totals_from_orm_lines(purchase)
        db.session.flush()

        log_audit(
            user_id=user.id,
            action="purchase_item_updated",
            entity_type="purchase",
            entity_id=purchase.id,
            summary=(
                f"Updated line '{item.name}' on draft purchase {purchase.purchase_number}"
            ),
            old_values=old_values,
            new_values=_purchase_snapshot(purchase),
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    return target


def remove_draft_purchase_item(
    purchase_id: int,
    purchase_item_id: int,
    *,
    actor_user_id: int,
) -> Purchase:
    """Remove a single line from a draft purchase and recalculate header totals atomically."""
    user = _require_purchase_writer(actor_user_id)
    purchase = _load_purchase_with_details(purchase_id)
    if purchase.status != "draft":
        raise PurchaseStateError(
            f"Cannot remove lines from purchase {purchase.purchase_number} because it is "
            f"{purchase.status}."
        )

    if not _is_strict_int(purchase_item_id) or purchase_item_id <= 0:
        raise PurchaseNotFoundError("Purchase line does not exist.")
    target = next((li for li in purchase.items if li.id == purchase_item_id), None)
    if target is None:
        raise PurchaseNotFoundError(
            f"Purchase line #{purchase_item_id} does not exist on purchase #{purchase.id}."
        )

    item_name = target.item.name if target.item else f"Item #{target.item_id}"
    old_values = _purchase_snapshot(purchase)
    try:
        purchase.items.remove(target)
        db.session.delete(target)
        db.session.flush()

        _recompute_header_totals_from_orm_lines(purchase)
        db.session.flush()

        log_audit(
            user_id=user.id,
            action="purchase_item_removed",
            entity_type="purchase",
            entity_id=purchase.id,
            summary=(
                f"Removed line '{item_name}' from draft purchase {purchase.purchase_number}"
            ),
            old_values=old_values,
            new_values=_purchase_snapshot(purchase),
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    return _load_purchase_with_details(purchase.id)


# ---------------------------------------------------------------------------
# 4. Atomic Purchase Posting & Cancellation
# ---------------------------------------------------------------------------


def post_purchase(
    purchase_id: int,
    *,
    actor_user_id: int,
) -> tuple[Purchase, list[StockLedger]]:
    """Post a draft purchase atomically: validate, mark posted, post receipts, audit, commit.

    Inventory receipts capitalize `taxable_value_paise` only (excluding GST under
    ITC-eligible accounting). If any line, stock receipt, or audit log fails, the
    entire transaction rolls back and the purchase remains in draft status.
    """
    user = _require_purchase_writer(actor_user_id)
    purchase = _load_purchase_with_details(purchase_id)

    if purchase.status == "posted":
        raise PurchaseStateError(
            f"Purchase {purchase.purchase_number} has already been posted."
        )
    if purchase.status == "cancelled":
        raise PurchaseStateError(
            f"Cancelled purchase {purchase.purchase_number} cannot be posted."
        )

    existing_ledger = db.session.execute(
        db.select(StockLedger.id)
        .where(
            StockLedger.source_doc_type == "purchase",
            StockLedger.source_doc_id == purchase.id,
        )
        .limit(1)
    ).first()
    if existing_ledger is not None:
        raise PurchaseStateError(
            f"Purchase {purchase.purchase_number} already has stock ledger entries."
        )

    if purchase.supplier is None or not purchase.supplier.is_active:
        supplier_name = purchase.supplier.name if purchase.supplier else f"#{purchase.supplier_id}"
        raise PurchaseValidationError(
            f"Cannot post purchase {purchase.purchase_number}: supplier '{supplier_name}' is inactive."
        )

    sorted_lines = sorted(purchase.items, key=lambda li: li.id)
    if not sorted_lines:
        raise PurchaseValidationError(
            f"Cannot post purchase {purchase.purchase_number} without at least one line item."
        )

    seen_item_ids: set[int] = set()
    computed_lines: list[ComputedPurchaseLine] = []
    for line in sorted_lines:
        item = _load_active_item(line.item_id)
        if item.id in seen_item_ids:
            raise PurchaseValidationError(
                f"Duplicate item '{item.name}' found on purchase {purchase.purchase_number}."
            )
        seen_item_ids.add(item.id)

        entered_unit = _load_unit_for_item(item, line.entered_unit_id)
        recomputed = compute_purchase_line(
            item=item,
            entered_qty_milli=line.entered_qty_milli,
            unit_rate_paise=line.unit_rate_paise,
            gst_type=purchase.gst_type,
            entered_unit=entered_unit,
            gst_rate_bps=line.gst_rate_bps,
        )
        if (
            line.qty_stock_milli != recomputed.qty_stock_milli
            or line.taxable_value_paise != recomputed.taxable_value_paise
            or line.gst_rate_bps != recomputed.gst_rate_bps
            or line.cgst_paise != recomputed.cgst_paise
            or line.sgst_paise != recomputed.sgst_paise
            or line.igst_paise != recomputed.igst_paise
            or line.line_total_paise != recomputed.line_total_paise
        ):
            raise PurchaseValidationError(
                f"Line for '{item.name}' on purchase {purchase.purchase_number} is out of sync "
                f"with current unit conversions or GST calculations. Update the draft before posting."
            )
        computed_lines.append(recomputed)

    expected_totals = compute_purchase_totals(computed_lines)
    if (
        purchase.subtotal_paise != expected_totals.subtotal_paise
        or purchase.cgst_paise != expected_totals.cgst_paise
        or purchase.sgst_paise != expected_totals.sgst_paise
        or purchase.igst_paise != expected_totals.igst_paise
        or purchase.total_paise != expected_totals.total_paise
    ):
        raise PurchaseValidationError(
            f"Header totals on purchase {purchase.purchase_number} do not match line sums."
        )

    old_values = _purchase_snapshot(purchase)
    receipt_notes = _format_receipt_notes(purchase)
    receipts: list[StockLedger] = []

    try:
        # Transition status first so trg_stock_ledger_validate_purchase_receipt sees status='posted'.
        purchase.status = "posted"
        db.session.flush()

        for line in sorted_lines:
            receipt = post_stock_receipt(
                item_id=line.item_id,
                movement_date=purchase.purchase_date,
                movement_type="purchase_receipt",
                source_doc_type="purchase",
                source_doc_id=purchase.id,
                qty_milli=line.qty_stock_milli,
                total_value_paise=line.taxable_value_paise,
                unit_cost_paise=None,
                notes=receipt_notes,
                created_by=user.id,
                allow_inactive_item=False,
            )
            receipts.append(receipt)

        log_audit(
            user_id=user.id,
            action="purchase_posted",
            entity_type="purchase",
            entity_id=purchase.id,
            summary=(
                f"Posted purchase {purchase.purchase_number} with {len(receipts)} "
                f"stock receipt(s)"
            ),
            old_values=old_values,
            new_values={
                **_purchase_snapshot(purchase),
                "ledger_ids": [r.id for r in receipts],
                "receipts": [
                    {
                        "ledger_id": r.id,
                        "item_id": r.item_id,
                        "qty_change_milli": r.qty_change_milli,
                        "unit_cost_paise": r.unit_cost_paise,
                        "value_change_paise": r.value_change_paise,
                    }
                    for r in receipts
                ],
            },
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    return _load_purchase_with_details(purchase.id), receipts


def cancel_purchase(
    purchase_id: int,
    *,
    actor_user_id: int,
    reason: str,
    cancellation_date: date | None = None,
) -> tuple[Purchase, list[StockLedger]]:
    """Cancel a draft or posted purchase atomically.

    - Draft cancellation transitions `draft -> cancelled` and writes an audit log with
      no stock ledger entries.
    - Posted cancellation reverses every `purchase_receipt` entry via `post_ledger_reversal()`
      (defaulting to the business-local current date if `cancellation_date` is omitted),
      then transitions `posted -> cancelled` without modifying any immutable header field,
      and writes an audit log with the reason and reversal IDs. Any failure rolls back all
      reversals and keeps the purchase in `'posted'` status.
    """
    user = _require_purchase_writer(actor_user_id)
    cleaned_reason = _clean_optional_text(reason, field_label="Cancellation reason")
    if not cleaned_reason:
        raise PurchaseValidationError("Cancellation reason is required.")

    if cancellation_date is not None:
        _validate_purchase_date(cancellation_date, label="Cancellation date")

    purchase = _load_purchase_with_details(purchase_id)
    if purchase.status == "cancelled":
        raise PurchaseStateError(
            f"Purchase {purchase.purchase_number} is already cancelled."
        )

    old_values = _purchase_snapshot(purchase)

    if purchase.status == "draft":
        existing_ledger = db.session.execute(
            db.select(StockLedger.id)
            .where(
                StockLedger.source_doc_type == "purchase",
                StockLedger.source_doc_id == purchase.id,
            )
            .limit(1)
        ).first()
        if existing_ledger is not None:
            raise PurchaseStateError(
                f"Draft purchase {purchase.purchase_number} unexpectedly has stock ledger entries."
            )

        try:
            purchase.status = "cancelled"
            db.session.flush()

            log_audit(
                user_id=user.id,
                action="purchase_cancelled",
                entity_type="purchase",
                entity_id=purchase.id,
                summary=(
                    f"Cancelled draft purchase {purchase.purchase_number}: {cleaned_reason}"
                ),
                old_values=old_values,
                new_values={
                    **_purchase_snapshot(purchase),
                    "reason": cleaned_reason,
                    "reversed_ledger_ids": [],
                    "reversal_ledger_ids": [],
                },
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

        return _load_purchase_with_details(purchase.id), []

    # Posted purchase cancellation: reverse all receipts first, then transition status.
    rev_date = get_business_today() if cancellation_date is None else cancellation_date
    receipts = db.session.execute(
        db.select(StockLedger)
        .where(
            StockLedger.source_doc_type == "purchase",
            StockLedger.source_doc_id == purchase.id,
            StockLedger.movement_type == "purchase_receipt",
        )
        .order_by(StockLedger.id.asc())
    ).scalars().all()

    expected_item_ids = {li.item_id for li in purchase.items}
    receipt_item_ids = {r.item_id for r in receipts}
    if not receipts or receipt_item_ids != expected_item_ids or len(receipts) != len(purchase.items):
        raise PurchaseStateError(
            f"Cannot cancel posted purchase {purchase.purchase_number}: stock receipt entries "
            f"do not match purchase lines."
        )

    reversals: list[StockLedger] = []
    reversal_notes = f"Cancelled purchase {purchase.purchase_number}: {cleaned_reason}"

    try:
        for receipt in receipts:
            reversal = post_ledger_reversal(
                ledger_id=receipt.id,
                movement_date=rev_date,
                created_by=user.id,
                notes=reversal_notes,
            )
            reversals.append(reversal)

        # Only transition status after all reversals succeed; never mutate other header fields.
        purchase.status = "cancelled"
        db.session.flush()

        log_audit(
            user_id=user.id,
            action="purchase_cancelled",
            entity_type="purchase",
            entity_id=purchase.id,
            summary=(
                f"Cancelled posted purchase {purchase.purchase_number} "
                f"(reversed {len(reversals)} receipt(s)): {cleaned_reason}"
            ),
            old_values=old_values,
            new_values={
                **_purchase_snapshot(purchase),
                "reason": cleaned_reason,
                "cancellation_date": rev_date.isoformat(),
                "reversed_ledger_ids": [r.id for r in receipts],
                "reversal_ledger_ids": [rev.id for rev in reversals],
            },
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    return _load_purchase_with_details(purchase.id), reversals

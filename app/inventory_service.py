"""Stock ledger foundation, WAC stock position queries, and opening stock workflow.

All inventory balances and valuations are derived exclusively from the immutable
append-only `stock_ledger` table using exact integer / Decimal arithmetic.
Low-level ledger posting primitives (`post_stock_receipt`, `post_stock_issue`,
`post_ledger_reversal`) never commit internally so calling workflows own the
complete atomic transaction.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import joinedload

from app.audit import log_audit
from app.extensions import db
from app.models import Item, StockAdjustment, StockLedger, Unit, User
from app.money import divide_round_half_up
from app.permissions import CAP_MANAGE_STOCK_ADJUSTMENTS, role_has_permission
from app.units import UnitConversionError, convert_qty_milli_to_stock, format_milli

# Valid (movement_type -> source_doc_type) pairings mirroring DB check constraints.
INWARD_MOVEMENT_SOURCES: dict[str, str] = {
    "opening_stock": "stock_adjustment",
    "purchase_receipt": "purchase",
    "production_output": "production_run",
    "adjustment_in": "stock_adjustment",
}

OUTWARD_MOVEMENT_SOURCES: dict[str, str] = {
    "production_issue": "production_run",
    "packaging_damage": "production_run",
    "sales_dispatch": "dispatch",
    "adjustment_out": "stock_adjustment",
}

REVERSAL_MOVEMENT_TYPES: frozenset[str] = frozenset({"reversal_in", "reversal_out"})


class InventoryError(ValueError):
    """Base exception for inventory ledger and valuation errors."""


class InsufficientStockError(InventoryError):
    """Raised when an outward movement or reversal would cause negative inventory."""


class InvalidMovementError(InventoryError):
    """Raised when a stock movement violates business or integrity rules."""


class UnauthorizedInventoryActionError(PermissionError):
    """Raised when a user lacks permission for a restricted inventory operation."""


@dataclass(frozen=True)
class StockPosition:
    """Ledger-derived quantity, total value, and weighted average cost for an item."""

    item_id: int
    qty_milli: int
    value_paise: int
    item: Item | None = None

    @property
    def wac_rupees(self) -> Decimal | None:
        """Exact weighted average cost in rupees per stock unit, or None when empty."""
        if self.qty_milli <= 0:
            return None
        # (value_paise / 100) / (qty_milli / 1000) == (value_paise * 10) / qty_milli
        return (Decimal(self.value_paise) * Decimal(10)) / Decimal(self.qty_milli)

    @property
    def wac_paise_exact(self) -> Decimal | None:
        """Exact weighted average cost in paise per stock unit, or None when empty."""
        if self.qty_milli <= 0:
            return None
        return (Decimal(self.value_paise) * Decimal(1000)) / Decimal(self.qty_milli)

    @property
    def unit_cost_paise(self) -> int | None:
        """Display-rounded WAC in integer paise per stock unit, or None when empty."""
        if self.qty_milli <= 0:
            return None
        return divide_round_half_up(self.value_paise * 1000, self.qty_milli)


# ---------------------------------------------------------------------------
# Internal Validation & Numbering Helpers
# ---------------------------------------------------------------------------


def _is_strict_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_strict_date(value: object) -> bool:
    return isinstance(value, date) and not isinstance(value, datetime)


def _validate_not_backdated(item_id: int, movement_date: date) -> None:
    """Reject movements dated earlier than the item's latest posted ledger date."""
    latest_date = db.session.execute(
        db.select(func.max(StockLedger.movement_date)).where(StockLedger.item_id == item_id)
    ).scalar()
    if latest_date is not None and movement_date < latest_date:
        raise InvalidMovementError(
            f"Backdated stock movement ({movement_date.isoformat()}) is not allowed before "
            f"the item's latest movement date ({latest_date.isoformat()})."
        )


def financial_year_for_date(target_date: date) -> str:
    """Return the Indian financial year string (April–March) for `target_date`, e.g. '2026-27'."""
    if not _is_strict_date(target_date):
        raise InvalidMovementError("Movement date must be a valid date.")
    start_year = target_date.year if target_date.month >= 4 else target_date.year - 1
    end_suffix = str(start_year + 1)[-2:]
    return f"{start_year}-{end_suffix}"


def generate_next_adjustment_number(adjustment_date: date) -> str:
    """Generate the next sequential financial-year adjustment number (e.g. 'ADJ/2026-27/0001')."""
    fy = financial_year_for_date(adjustment_date)
    prefix = f"ADJ/{fy}/"
    existing_numbers = db.session.execute(
        db.select(StockAdjustment.adjustment_number).where(
            StockAdjustment.adjustment_number.like(f"{prefix}%")
        )
    ).scalars().all()

    max_seq = 0
    for number in existing_numbers:
        suffix = number[len(prefix) :]
        if suffix.isdigit():
            max_seq = max(max_seq, int(suffix))

    return f"{prefix}{max_seq + 1:04d}"


# ---------------------------------------------------------------------------
# Stock Position Queries (Derived Exclusively from StockLedger)
# ---------------------------------------------------------------------------


def get_item_stock_position(item_id: int) -> StockPosition:
    """Return the current ledger-derived quantity, value, and WAC for `item_id`."""
    if not _is_strict_int(item_id) or item_id <= 0:
        raise InvalidMovementError("Item ID must be a positive integer.")

    qty_milli, value_paise = db.session.execute(
        db.select(
            func.coalesce(func.sum(StockLedger.qty_change_milli), 0),
            func.coalesce(func.sum(StockLedger.value_change_paise), 0),
        ).where(StockLedger.item_id == item_id)
    ).one()

    return StockPosition(
        item_id=item_id,
        qty_milli=int(qty_milli),
        value_paise=int(value_paise),
    )


def get_all_stock_positions(
    *,
    item_type: str | None = None,
    status: str = "all",
    item_ids: list[int] | None = None,
) -> list[StockPosition]:
    """Return ledger-derived stock positions for items matching optional filters."""
    ledger_totals = (
        db.select(
            StockLedger.item_id.label("item_id"),
            func.coalesce(func.sum(StockLedger.qty_change_milli), 0).label("qty_milli"),
            func.coalesce(func.sum(StockLedger.value_change_paise), 0).label("value_paise"),
        )
        .group_by(StockLedger.item_id)
        .subquery()
    )

    query = (
        db.select(
            Item,
            func.coalesce(ledger_totals.c.qty_milli, 0).label("qty_milli"),
            func.coalesce(ledger_totals.c.value_paise, 0).label("value_paise"),
        )
        .outerjoin(ledger_totals, Item.id == ledger_totals.c.item_id)
        .options(joinedload(Item.stock_unit))
        .order_by(Item.item_type.asc(), Item.name.asc(), Item.sku.asc())
    )

    if item_type:
        query = query.where(Item.item_type == item_type)

    if status == "active":
        query = query.where(Item.is_active.is_(True))
    elif status == "inactive":
        query = query.where(Item.is_active.is_(False))

    if item_ids is not None:
        if not item_ids:
            return []
        query = query.where(Item.id.in_(item_ids))

    rows = db.session.execute(query).all()
    return [
        StockPosition(
            item_id=item.id,
            qty_milli=int(qty_milli),
            value_paise=int(value_paise),
            item=item,
        )
        for item, qty_milli, value_paise in rows
    ]


def item_has_ledger_history(item_id: int) -> bool:
    """Return True if at least one `stock_ledger` row exists for `item_id`."""
    return (
        db.session.execute(
            db.select(StockLedger.id).where(StockLedger.item_id == item_id).limit(1)
        ).first()
        is not None
    )


# ---------------------------------------------------------------------------
# Low-Level Append-Only Stock Ledger Primitives (No Internal Commit)
# ---------------------------------------------------------------------------


def post_stock_receipt(
    *,
    item_id: int,
    movement_date: date,
    movement_type: str,
    source_doc_type: str,
    source_doc_id: int,
    qty_milli: int,
    created_by: int,
    total_value_paise: int | None = None,
    unit_cost_paise: int | None = None,
    notes: str | None = None,
    allow_inactive_item: bool = False,
) -> StockLedger:
    """Append an immutable inward stock ledger entry without committing.

    Preserves exact `total_value_paise` whenever supplied so total inventory value
    never suffers rounding drift. If only `unit_cost_paise` is supplied, total value
    is calculated as `divide_round_half_up(unit_cost_paise * qty_milli, 1000)`.
    """
    expected_source = INWARD_MOVEMENT_SOURCES.get(movement_type)
    if expected_source is None:
        raise InvalidMovementError(f"Invalid inward movement type: {movement_type!r}.")
    if source_doc_type != expected_source:
        raise InvalidMovementError(
            f"Movement type '{movement_type}' requires source_doc_type='{expected_source}', "
            f"got {source_doc_type!r}."
        )
    if not _is_strict_int(source_doc_id) or source_doc_id <= 0:
        raise InvalidMovementError("Source document ID must be a positive integer.")
    if not _is_strict_date(movement_date):
        raise InvalidMovementError("Movement date must be a valid date.")
    if not _is_strict_int(qty_milli) or qty_milli <= 0:
        raise InvalidMovementError("Receipt quantity must be a positive integer milli-unit value.")

    item = db.session.get(Item, item_id) if _is_strict_int(item_id) else None
    if item is None:
        raise InvalidMovementError("Selected item does not exist.")
    if not allow_inactive_item and not item.is_active:
        raise InvalidMovementError(f"Item '{item.name}' is inactive.")

    user = db.session.get(User, created_by) if _is_strict_int(created_by) else None
    if user is None:
        raise InvalidMovementError("User does not exist.")

    _validate_not_backdated(item.id, movement_date)

    if total_value_paise is None and unit_cost_paise is None:
        raise InvalidMovementError("Either total_value_paise or unit_cost_paise must be provided.")

    if total_value_paise is not None:
        if not _is_strict_int(total_value_paise) or total_value_paise < 0:
            raise InvalidMovementError("Total value must be a non-negative integer paise amount.")
        resolved_total_value = total_value_paise
        if unit_cost_paise is not None:
            if not _is_strict_int(unit_cost_paise) or unit_cost_paise < 0:
                raise InvalidMovementError("Unit cost must be a non-negative integer paise amount.")
            resolved_unit_cost = unit_cost_paise
        else:
            resolved_unit_cost = divide_round_half_up(resolved_total_value * 1000, qty_milli)
    else:
        if not _is_strict_int(unit_cost_paise) or unit_cost_paise < 0:
            raise InvalidMovementError("Unit cost must be a non-negative integer paise amount.")
        resolved_unit_cost = unit_cost_paise
        resolved_total_value = divide_round_half_up(resolved_unit_cost * qty_milli, 1000)

    entry = StockLedger(
        item_id=item.id,
        movement_date=movement_date,
        movement_type=movement_type,
        source_doc_type=source_doc_type,
        source_doc_id=source_doc_id,
        qty_change_milli=qty_milli,
        unit_cost_paise=resolved_unit_cost,
        value_change_paise=resolved_total_value,
        notes=(notes or "").strip() or None,
        created_by=user.id,
    )
    db.session.add(entry)
    db.session.flush()
    return entry


def post_stock_issue(
    *,
    item_id: int,
    movement_date: date,
    movement_type: str,
    source_doc_type: str,
    source_doc_id: int,
    qty_milli: int,
    created_by: int,
    notes: str | None = None,
    allow_inactive_item: bool = False,
) -> StockLedger:
    """Append an immutable outward stock ledger entry at unrounded running WAC without committing.

    `qty_milli` is passed as a positive integer magnitude and stored negated in the ledger.
    Issue value is computed directly from unrounded running inventory totals:
        round_half_up(on_hand_value_paise * issue_qty_milli / on_hand_qty_milli)
    Full depletion guarantees exactly zero remaining quantity and zero remaining value.
    """
    expected_source = OUTWARD_MOVEMENT_SOURCES.get(movement_type)
    if expected_source is None:
        raise InvalidMovementError(f"Invalid outward movement type: {movement_type!r}.")
    if source_doc_type != expected_source:
        raise InvalidMovementError(
            f"Movement type '{movement_type}' requires source_doc_type='{expected_source}', "
            f"got {source_doc_type!r}."
        )
    if not _is_strict_int(source_doc_id) or source_doc_id <= 0:
        raise InvalidMovementError("Source document ID must be a positive integer.")
    if not _is_strict_date(movement_date):
        raise InvalidMovementError("Movement date must be a valid date.")
    if not _is_strict_int(qty_milli) or qty_milli <= 0:
        raise InvalidMovementError("Issue quantity must be a positive integer milli-unit value.")

    item = db.session.get(Item, item_id) if _is_strict_int(item_id) else None
    if item is None:
        raise InvalidMovementError("Selected item does not exist.")
    if not allow_inactive_item and not item.is_active:
        raise InvalidMovementError(f"Item '{item.name}' is inactive.")

    user = db.session.get(User, created_by) if _is_strict_int(created_by) else None
    if user is None:
        raise InvalidMovementError("User does not exist.")

    _validate_not_backdated(item.id, movement_date)

    position = get_item_stock_position(item.id)
    if qty_milli > position.qty_milli:
        unit_code = item.stock_unit.code if item.stock_unit else "units"
        raise InsufficientStockError(
            f"Insufficient stock for '{item.name}': requested {format_milli(qty_milli)} {unit_code}, "
            f"available {format_milli(position.qty_milli)} {unit_code}."
        )

    if qty_milli == position.qty_milli:
        issue_value_paise = position.value_paise
    else:
        issue_value_paise = divide_round_half_up(
            position.value_paise * qty_milli,
            position.qty_milli,
        )

    display_unit_cost_paise = divide_round_half_up(
        position.value_paise * 1000,
        position.qty_milli,
    )

    entry = StockLedger(
        item_id=item.id,
        movement_date=movement_date,
        movement_type=movement_type,
        source_doc_type=source_doc_type,
        source_doc_id=source_doc_id,
        qty_change_milli=-qty_milli,
        unit_cost_paise=display_unit_cost_paise,
        value_change_paise=-issue_value_paise,
        notes=(notes or "").strip() or None,
        created_by=user.id,
    )
    db.session.add(entry)
    db.session.flush()
    return entry


def post_ledger_reversal(
    *,
    ledger_id: int,
    movement_date: date,
    created_by: int,
    notes: str | None = None,
) -> StockLedger:
    """Append an exact reversing ledger entry for `ledger_id` without committing.

    Rejects reversing a reversal, duplicate reversals, backdated reversals, and any
    reversal that would produce a negative cumulative quantity or value.
    """
    if not _is_strict_int(ledger_id) or ledger_id <= 0:
        raise InvalidMovementError("Ledger entry ID must be a positive integer.")
    if not _is_strict_date(movement_date):
        raise InvalidMovementError("Movement date must be a valid date.")

    user = db.session.get(User, created_by) if _is_strict_int(created_by) else None
    if user is None:
        raise InvalidMovementError("User does not exist.")

    original = db.session.get(StockLedger, ledger_id)
    if original is None:
        raise InvalidMovementError(f"Stock ledger entry #{ledger_id} does not exist.")

    if original.movement_type in REVERSAL_MOVEMENT_TYPES or original.reverses_ledger_id is not None:
        raise InvalidMovementError("Cannot reverse a reversal ledger entry.")

    existing_reversal = db.session.execute(
        db.select(StockLedger.id).where(StockLedger.reverses_ledger_id == original.id).limit(1)
    ).first()
    if existing_reversal is not None:
        raise InvalidMovementError(f"Stock ledger entry #{original.id} has already been reversed.")

    _validate_not_backdated(original.item_id, movement_date)

    rev_qty_milli = -original.qty_change_milli
    rev_value_paise = -original.value_change_paise
    rev_movement_type = "reversal_out" if original.qty_change_milli > 0 else "reversal_in"

    if rev_qty_milli < 0 or rev_value_paise < 0:
        position = get_item_stock_position(original.item_id)
        if position.qty_milli + rev_qty_milli < 0:
            item = original.item
            unit_code = item.stock_unit.code if item and item.stock_unit else "units"
            item_name = item.name if item else f"Item #{original.item_id}"
            raise InsufficientStockError(
                f"Cannot reverse entry #{original.id} for '{item_name}': reversing "
                f"{format_milli(-rev_qty_milli)} {unit_code} would leave negative stock "
                f"(current balance: {format_milli(position.qty_milli)} {unit_code})."
            )
        if position.value_paise + rev_value_paise < 0:
            raise InsufficientStockError(
                f"Cannot reverse entry #{original.id}: reversing value would leave "
                f"negative inventory value."
            )

    reversal = StockLedger(
        item_id=original.item_id,
        movement_date=movement_date,
        movement_type=rev_movement_type,
        source_doc_type=original.source_doc_type,
        source_doc_id=original.source_doc_id,
        reverses_ledger_id=original.id,
        qty_change_milli=rev_qty_milli,
        unit_cost_paise=original.unit_cost_paise,
        value_change_paise=rev_value_paise,
        notes=(notes or "").strip() or None,
        created_by=user.id,
    )
    db.session.add(reversal)
    db.session.flush()
    return reversal


# ---------------------------------------------------------------------------
# Opening Stock Workflow (Atomic Document + Ledger + Audit)
# ---------------------------------------------------------------------------


def record_opening_stock(
    *,
    item_id: int,
    qty_milli: int,
    unit_cost_paise: int,
    movement_date: date,
    created_by: int,
    input_unit_id: int | None = None,
    reason: str = "Opening stock balance",
    notes: str | None = None,
) -> tuple[StockAdjustment, StockLedger]:
    """Record one-time opening stock for an active item atomically.

    `unit_cost_paise` is the cost per stock unit of `item` in integer paise (0 allowed).
    `qty_milli` is measured in `input_unit_id` (defaulting to the item's stock unit) and
    converted to the item's stock unit via `app.units.convert_qty_milli_to_stock`.

    Creates the `StockAdjustment(adjustment_type='opening_stock')` header, the
    `StockLedger(movement_type='opening_stock')` row, and an `AuditLog` entry inside a
    single transaction. Any failure rolls back all changes completely.
    """
    user = db.session.get(User, created_by) if _is_strict_int(created_by) else None
    if user is None or not user.is_active:
        raise UnauthorizedInventoryActionError("Active authorized user is required.")
    if not role_has_permission(user.role, CAP_MANAGE_STOCK_ADJUSTMENTS):
        raise UnauthorizedInventoryActionError(
            "Only Admin and Owner users are authorized to record opening stock."
        )

    item = db.session.get(Item, item_id) if _is_strict_int(item_id) else None
    if item is None:
        raise InvalidMovementError("Selected item does not exist.")
    if not item.is_active:
        raise InvalidMovementError(
            f"Cannot record opening stock for inactive item '{item.name}'."
        )

    if not _is_strict_date(movement_date):
        raise InvalidMovementError("Opening stock date must be a valid date.")

    cleaned_reason = (reason or "").strip()
    if not cleaned_reason:
        raise InvalidMovementError("Reason is required for opening stock.")

    if not _is_strict_int(qty_milli) or qty_milli <= 0:
        raise InvalidMovementError("Opening stock quantity must be greater than zero.")

    if not _is_strict_int(unit_cost_paise) or unit_cost_paise < 0:
        raise InvalidMovementError("Unit cost must be zero or greater.")

    if input_unit_id is None:
        input_unit = item.stock_unit or db.session.get(Unit, item.stock_unit_id)
    else:
        input_unit = db.session.get(Unit, input_unit_id) if _is_strict_int(input_unit_id) else None
    if input_unit is None:
        raise InvalidMovementError("Selected input unit does not exist.")

    try:
        stock_qty_milli = convert_qty_milli_to_stock(item, qty_milli, input_unit)
    except UnitConversionError as exc:
        raise InvalidMovementError(str(exc)) from exc

    existing_opening = db.session.execute(
        db.select(StockLedger.id)
        .where(
            StockLedger.item_id == item.id,
            StockLedger.movement_type == "opening_stock",
        )
        .limit(1)
    ).first()
    if existing_opening is not None:
        raise InvalidMovementError(
            f"Opening stock has already been recorded for '{item.name}'. "
            "Use a reversal and stock adjustment for corrections."
        )

    if item_has_ledger_history(item.id):
        raise InvalidMovementError(
            f"Cannot record opening stock for '{item.name}' because it already has "
            "stock ledger movements."
        )

    total_value_paise = divide_round_half_up(unit_cost_paise * stock_qty_milli, 1000)

    try:
        adjustment_number = generate_next_adjustment_number(movement_date)
        adjustment = StockAdjustment(
            adjustment_number=adjustment_number,
            adjustment_type="opening_stock",
            adjustment_date=movement_date,
            reason=cleaned_reason,
            created_by=user.id,
            approved_by=user.id,
        )
        db.session.add(adjustment)
        db.session.flush()

        ledger_entry = post_stock_receipt(
            item_id=item.id,
            movement_date=movement_date,
            movement_type="opening_stock",
            source_doc_type="stock_adjustment",
            source_doc_id=adjustment.id,
            qty_milli=stock_qty_milli,
            unit_cost_paise=unit_cost_paise,
            total_value_paise=total_value_paise,
            notes=notes,
            created_by=user.id,
        )

        stock_unit_code = item.stock_unit.code if item.stock_unit else ""
        log_audit(
            user_id=user.id,
            action="opening_stock_recorded",
            entity_type="stock_adjustment",
            entity_id=adjustment.id,
            summary=(
                f"Recorded opening stock {adjustment.adjustment_number} for '{item.name}' "
                f"({format_milli(stock_qty_milli)} {stock_unit_code})"
            ),
            new_values={
                "adjustment_id": adjustment.id,
                "adjustment_number": adjustment.adjustment_number,
                "adjustment_type": adjustment.adjustment_type,
                "adjustment_date": adjustment.adjustment_date.isoformat(),
                "reason": adjustment.reason,
                "ledger_id": ledger_entry.id,
                "item_id": item.id,
                "entered_unit_id": input_unit.id,
                "entered_qty_milli": qty_milli,
                "qty_stock_milli": stock_qty_milli,
                "unit_cost_paise": unit_cost_paise,
                "value_change_paise": total_value_paise,
                "notes": ledger_entry.notes,
            },
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    return adjustment, ledger_entry

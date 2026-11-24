"""Exact quantity parsing, formatting, and unit conversion helpers.

All quantities are stored as integer thousandths (*_milli) and converted using
exact Decimal / Fraction arithmetic. Floating-point arithmetic is never used.
"""

from collections import deque
from decimal import Decimal, InvalidOperation
from fractions import Fraction

from app.extensions import db
from app.models import Item, ItemUnitConversion, Unit

MILLI_SCALE = Decimal(1000)
MAX_MILLI_QTY = 9_000_000_000_000_000  # Safely within signed 64-bit SQLite INTEGER


class UnitConversionError(ValueError):
    """Raised when a unit conversion is missing, conflicting, or non-exact."""


def parse_qty_to_milli(
    raw: str | None,
    *,
    field_label: str = "Quantity",
) -> tuple[int | None, str | None]:
    """Parse a positive decimal string (up to 3 decimal places) into integer thousandths."""
    cleaned = (raw or "").strip()
    if not cleaned:
        return None, f"{field_label} is required."

    try:
        dec = Decimal(cleaned)
    except InvalidOperation:
        return None, f"{field_label} must be a valid number."

    if not dec.is_finite():
        return None, f"{field_label} must be a valid number."

    if dec <= 0:
        return None, f"{field_label} must be greater than zero."

    scaled = dec * MILLI_SCALE
    if scaled != scaled.to_integral_value():
        return None, f"{field_label} supports at most 3 decimal places."

    milli = int(scaled)
    if milli > MAX_MILLI_QTY:
        return None, f"{field_label} is too large."

    return milli, None


def format_milli(milli: int | None) -> str:
    """Format integer thousandths into a clean decimal string (e.g. 40000 -> '40', 1200 -> '1.2')."""
    if milli is None:
        return ""
    value = Decimal(milli) / MILLI_SCALE
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _build_global_unit_graph(dimension: str) -> tuple[dict[int, list[tuple[int, Fraction]]], set[int]]:
    """Build a bidirectional graph of global `Unit` ratios within `dimension`."""
    graph: dict[int, list[tuple[int, Fraction]]] = {}

    units = db.session.execute(
        db.select(Unit).where(Unit.dimension == dimension)
    ).scalars().all()
    unit_ids = {u.id for u in units}
    for u in units:
        graph.setdefault(u.id, [])

    # Standard global unit factors (e.g. 1 kg = 1000 g, 1 L = 1000 ml).
    for u in units:
        if u.base_unit_id is not None and u.base_unit_id in unit_ids and u.base_unit_id != u.id:
            if u.factor_to_base > 0:
                factor = Fraction(u.factor_to_base, 1)
                graph[u.id].append((u.base_unit_id, factor))
                graph[u.base_unit_id].append((u.id, Fraction(1, u.factor_to_base)))

    return graph, unit_ids


def _build_dimension_graph(
    item: Item,
    dimension: str,
    *,
    exclude_conversion_id: int | None = None,
) -> dict[int, list[tuple[int, Fraction]]]:
    """Build a bidirectional conversion graph for units in `dimension` for `item`."""
    graph, unit_ids = _build_global_unit_graph(dimension)

    # Item-specific conversions (e.g. 1 carton = 20 packet for Murukku).
    conversions = db.session.execute(
        db.select(ItemUnitConversion).where(ItemUnitConversion.item_id == item.id)
    ).scalars().all()
    for conv in conversions:
        if exclude_conversion_id is not None and conv.id == exclude_conversion_id:
            continue
        if conv.from_unit_id in unit_ids and conv.to_unit_id in unit_ids and conv.multiplier > 0:
            factor = Fraction(conv.multiplier, 1)
            graph[conv.from_unit_id].append((conv.to_unit_id, factor))
            graph[conv.to_unit_id].append((conv.from_unit_id, Fraction(1, conv.multiplier)))

    return graph


def _traverse_factors(
    graph: dict[int, list[tuple[int, Fraction]]],
    start_unit_id: int,
    *,
    conflict_message: str = "Conflicting unit conversion definitions exist for this item.",
) -> dict[int, Fraction]:
    """Compute exact conversion factors from `start_unit_id` to all reachable unit IDs."""
    factors: dict[int, Fraction] = {start_unit_id: Fraction(1, 1)}
    queue: deque[int] = deque([start_unit_id])

    while queue:
        current = queue.popleft()
        current_factor = factors[current]
        for neighbor, edge_factor in graph.get(current, ()):
            candidate = current_factor * edge_factor
            if neighbor not in factors:
                factors[neighbor] = candidate
                queue.append(neighbor)
            elif factors[neighbor] != candidate:
                raise UnitConversionError(conflict_message)

    return factors


def _validate_graph_consistency(
    graph: dict[int, list[tuple[int, Fraction]]],
    *,
    conflict_message: str,
) -> None:
    """Verify every connected component of `graph` is free of conflicting conversion paths."""
    visited: set[int] = set()
    for node_id in graph:
        if node_id not in visited:
            factors = _traverse_factors(
                graph,
                node_id,
                conflict_message=conflict_message,
            )
            visited.update(factors.keys())


def validate_item_conversion_graph(
    item: Item,
    *,
    exclude_conversion_id: int | None = None,
) -> None:
    """Raise `UnitConversionError` if `item`'s conversion graph has any conflicting paths."""
    stock_unit = item.stock_unit or db.session.get(Unit, item.stock_unit_id)
    if stock_unit is None:
        return
    graph = _build_dimension_graph(
        item,
        stock_unit.dimension,
        exclude_conversion_id=exclude_conversion_id,
    )
    _validate_graph_consistency(
        graph,
        conflict_message=(
            f"Conflicting unit conversion definitions exist for item '{item.name}'. "
            "Multiple paths produce different conversion factors."
        ),
    )


def validate_dimension_unit_graph(dimension: str) -> None:
    """Raise `UnitConversionError` if global or item conversion graphs in `dimension` conflict."""
    global_graph, _unit_ids = _build_global_unit_graph(dimension)
    _validate_graph_consistency(
        global_graph,
        conflict_message=(
            f"Conflicting global unit conversion ratios exist in dimension '{dimension}'."
        ),
    )

    items_with_conversions = db.session.execute(
        db.select(Item)
        .join(Unit, Item.stock_unit_id == Unit.id)
        .where(
            Unit.dimension == dimension,
            db.exists(
                db.select(ItemUnitConversion.id).where(ItemUnitConversion.item_id == Item.id)
            ),
        )
    ).scalars().all()
    for item in items_with_conversions:
        validate_item_conversion_graph(item)


def get_conversion_factor(
    item: Item,
    from_unit: Unit,
    to_unit: Unit,
    *,
    exclude_conversion_id: int | None = None,
) -> Fraction | None:
    """Return exact rational multiplier `f` such that `1 from_unit = f to_unit`, or None."""
    if from_unit.id == to_unit.id:
        return Fraction(1, 1)

    if from_unit.dimension != to_unit.dimension:
        return None

    graph = _build_dimension_graph(
        item,
        from_unit.dimension,
        exclude_conversion_id=exclude_conversion_id,
    )
    factors = _traverse_factors(graph, from_unit.id)
    return factors.get(to_unit.id)


def convert_qty_milli(
    item: Item,
    qty_milli: int,
    from_unit: Unit,
    to_unit: Unit,
    *,
    exclude_conversion_id: int | None = None,
) -> int:
    """Convert `qty_milli` from `from_unit` to `to_unit` using exact rational arithmetic.

    Raises `UnitConversionError` if units are incompatible, no conversion path exists,
    or the converted milli-quantity is not an exact positive integer.
    """
    if not isinstance(qty_milli, int) or isinstance(qty_milli, bool) or qty_milli <= 0:
        raise UnitConversionError("Quantity must be a positive integer milli-unit value.")

    if from_unit.id == to_unit.id:
        return qty_milli

    if from_unit.dimension != to_unit.dimension:
        raise UnitConversionError(
            f"Unit '{from_unit.code}' ({from_unit.dimension}) is incompatible with "
            f"'{to_unit.code}' ({to_unit.dimension}) for item '{item.name}'."
        )

    factor = get_conversion_factor(
        item,
        from_unit,
        to_unit,
        exclude_conversion_id=exclude_conversion_id,
    )
    if factor is None:
        raise UnitConversionError(
            f"No unit conversion exists between '{from_unit.code}' and '{to_unit.code}' "
            f"for item '{item.name}'. Define an item-specific conversion first."
        )

    exact_result = Fraction(qty_milli, 1) * factor
    if exact_result.denominator != 1 or exact_result.numerator <= 0:
        raise UnitConversionError(
            f"Quantity {format_milli(qty_milli)} {from_unit.code} cannot be converted "
            f"exactly to {to_unit.code} without rounding."
        )

    converted = int(exact_result.numerator)
    if converted > MAX_MILLI_QTY:
        raise UnitConversionError("Converted stock quantity exceeds maximum allowed size.")

    return converted


def convert_qty_milli_to_stock(
    item: Item,
    qty_milli: int,
    input_unit: Unit,
    *,
    exclude_conversion_id: int | None = None,
) -> int:
    """Convert `qty_milli` in `input_unit` to integer thousandths of `item.stock_unit`."""
    stock_unit = item.stock_unit or db.session.get(Unit, item.stock_unit_id)
    if stock_unit is None:
        raise UnitConversionError(f"Item '{item.name}' does not have a valid stock unit.")
    return convert_qty_milli(
        item,
        qty_milli,
        input_unit,
        stock_unit,
        exclude_conversion_id=exclude_conversion_id,
    )


def get_convertible_units_for_item(
    item: Item,
    *,
    exclude_conversion_id: int | None = None,
) -> list[Unit]:
    """Return all Unit records in `item.stock_unit`'s dimension convertible to stock unit."""
    stock_unit = item.stock_unit or db.session.get(Unit, item.stock_unit_id)
    if stock_unit is None:
        return []

    graph = _build_dimension_graph(
        item,
        stock_unit.dimension,
        exclude_conversion_id=exclude_conversion_id,
    )
    reachable_ids = set(_traverse_factors(graph, stock_unit.id).keys())
    if not reachable_ids:
        return []

    return db.session.execute(
        db.select(Unit)
        .where(Unit.id.in_(reachable_ids))
        .order_by(Unit.code.asc())
    ).scalars().all()

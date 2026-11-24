"""Exact monetary parsing, formatting, and rounding helpers.

All monetary values are stored as integer paise (rupees * 100) and calculated using
exact Decimal / integer arithmetic. Floating-point arithmetic is never used.
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

PAISE_SCALE = Decimal(100)
MAX_SCALED_INT = 9_000_000_000_000_000  # Safely within signed 64-bit SQLite INTEGER


def parse_scaled_decimal(
    raw: str | None,
    *,
    field_label: str,
    min_value: Decimal = Decimal("0"),
    max_value: Decimal | None = None,
    strictly_greater_than_min: bool = False,
) -> tuple[int | None, str | None]:
    """Parse a decimal string with at most 2 fractional digits into integer * 100."""
    cleaned = (raw or "").strip()
    if not cleaned:
        return None, f"{field_label} is required."

    try:
        dec = Decimal(cleaned)
    except InvalidOperation:
        return None, f"{field_label} must be a valid number."

    if not dec.is_finite():
        return None, f"{field_label} must be a valid number."

    if strictly_greater_than_min:
        if dec <= min_value:
            return None, f"{field_label} must be greater than {min_value}."
    elif dec < min_value:
        return None, f"{field_label} must be at least {min_value}."

    if max_value is not None and dec > max_value:
        return None, f"{field_label} must not exceed {max_value}."

    scaled = dec * PAISE_SCALE
    if scaled != scaled.to_integral_value():
        return None, f"{field_label} supports at most 2 decimal places."

    scaled_int = int(scaled)
    if abs(scaled_int) > MAX_SCALED_INT:
        return None, f"{field_label} is too large."

    return scaled_int, None


def parse_rupees_to_paise(
    raw: str | None,
    *,
    field_label: str = "Amount",
    allow_zero: bool = True,
    max_rupees: Decimal | None = None,
) -> tuple[int | None, str | None]:
    """Parse a rupee decimal string (up to 2 decimal places) into integer paise."""
    return parse_scaled_decimal(
        raw,
        field_label=field_label,
        min_value=Decimal("0"),
        max_value=max_rupees,
        strictly_greater_than_min=not allow_zero,
    )


def format_paise_as_rupees(paise: int | None) -> str:
    """Format integer paise as rupees with 2 decimal places (e.g. 5000 -> '50.00')."""
    if paise is None:
        return ""
    value = Decimal(paise) / PAISE_SCALE
    return f"{value:.2f}"


def format_exact_rupees(
    value: Decimal | None,
    *,
    min_decimals: int = 2,
    max_decimals: int = 4,
) -> str:
    """Format an exact Decimal rupee rate for display (e.g. 43.125 -> '43.125', 42 -> '42.00')."""
    if value is None:
        return ""
    quant = Decimal(1).scaleb(-max_decimals)
    rounded = value.quantize(quant, rounding=ROUND_HALF_UP)
    text = format(rounded, "f")
    if "." not in text:
        return f"{text}.{'0' * min_decimals}" if min_decimals > 0 else text
    int_part, frac_part = text.split(".", 1)
    trimmed = frac_part.rstrip("0")
    if len(trimmed) < min_decimals:
        trimmed = trimmed.ljust(min_decimals, "0")
    return f"{int_part}.{trimmed}" if trimmed else int_part


def divide_round_half_up(numerator: int, denominator: int) -> int:
    """Divide integer `numerator` by positive integer `denominator` with exact ROUND_HALF_UP.

    Uses exact integer division (no floating-point and no fixed-precision Decimal truncation).
    Halfway values round away from zero, matching Decimal's ROUND_HALF_UP semantics.
    """
    if not isinstance(numerator, int) or isinstance(numerator, bool):
        raise TypeError("numerator must be an integer.")
    if not isinstance(denominator, int) or isinstance(denominator, bool) or denominator <= 0:
        raise ValueError("denominator must be a positive integer.")

    if numerator < 0:
        return -divide_round_half_up(-numerator, denominator)

    quotient, remainder = divmod(numerator, denominator)
    if remainder * 2 >= denominator:
        quotient += 1
    return quotient


def round_decimal_half_up(value: Decimal) -> int:
    """Round a finite Decimal to the nearest integer using ROUND_HALF_UP."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("value must be a finite Decimal.")
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))

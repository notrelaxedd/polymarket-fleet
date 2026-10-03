"""Money helpers: cents in the database, dollars on screen and in forms."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, Overflow, ROUND_HALF_UP
from typing import Any

from host.errors import BadRequest

# One billion dollars: nothing in this fleet is bigger, and it keeps cents inside bigint.
MAX_DOLLARS = Decimal(10**9)
MAX_CENTS = 10**11
# A comma is accepted only as a thousands separator in proper groups ("1,500").
GROUPED = re.compile(r"^[+-]?\d{1,3}(,\d{3})+(\.\d*)?$")


def format_cents(cents: Any) -> str:
    """1250 -> "$12.50"; negative amounts as "-$3.00"; None -> "$0.00"."""
    try:
        value = int(cents or 0)
    except (TypeError, ValueError):
        value = 0
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value) // 100:,}.{abs(value) % 100:02d}"


def cents_to_dollars(cents: Any) -> str:
    """1250 -> "12.50" (what a dollars input field shows)."""
    try:
        value = int(cents or 0)
    except (TypeError, ValueError):
        value = 0
    sign = "-" if value < 0 else ""
    return f"{sign}{abs(value) // 100}.{abs(value) % 100:02d}"


def dollars_to_cents(text: str, label: str) -> int:
    """"12.50" -> 1250, rounding half up to the cent; 400 on anything that is not a number.

    "1,500" is accepted (thousands groups), "1,5" is not: a decimal-comma keyboard must
    not turn $1.50 into $15.00. Amounts above MAX_DOLLARS are refused.
    """
    cleaned = (text or "").strip().replace("$", "").replace(" ", "")
    if "," in cleaned:
        if not GROUPED.match(cleaned):
            raise BadRequest(f"{label}: use a dot for cents, such as 1.50")
        cleaned = cleaned.replace(",", "")
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        raise BadRequest(f"{label} must be a dollar amount such as 12.50") from None
    if not amount.is_finite():
        raise BadRequest(f"{label} must be a dollar amount such as 12.50")
    # Compared, not computed: abs() or a multiply would round through the decimal
    # context and raise Overflow for an exponent like 1e999999999.
    if amount > MAX_DOLLARS or amount < -MAX_DOLLARS:
        raise BadRequest(f"{label} must be at most {MAX_DOLLARS:,.0f} dollars")
    try:
        return int((amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except (InvalidOperation, Overflow):
        raise BadRequest(f"{label} must be a dollar amount such as 12.50") from None

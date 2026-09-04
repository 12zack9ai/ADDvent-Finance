"""Number formatting, in one place.

The marked-up invoice is rendered twice - as HTML for the screen and as a PDF
for sending back to the vendor - and the two must never disagree about a
number. They share these functions rather than each having their own.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

DASH = "—"


def fmt(value: Optional[Decimal], places: int) -> str:
    if value is None:
        return DASH
    q = Decimal(1).scaleb(-places)
    return f"${value.quantize(q):,}"


def money(value) -> str:
    return fmt(value, 2)


def money4(value) -> str:
    """Money with up to 4 decimals, but only as many as the price actually uses.

    Unit prices are frequently quoted at 3 or 4 decimals; showing $4.10 when the
    quote says $4.1025 would hide the very difference this system exists to find.
    """
    if value is None:
        return DASH
    d = Decimal(value).quantize(Decimal("0.0001")).normalize()
    exponent = d.as_tuple().exponent
    places = max(2, -exponent if isinstance(exponent, int) and exponent < 0 else 2)
    return fmt(value, min(places, 4))


def abs_money(value) -> str:
    return DASH if value is None else fmt(abs(value), 2)


def abs_money4(value) -> str:
    return DASH if value is None else money4(abs(value))


def qty(value) -> str:
    """Quantity as a person would write it: 20, not 2E+1.

    Decimal.normalize() strips trailing zeros by raising the exponent, so
    Decimal("20.0000") becomes Decimal("2E+1") and formats as scientific
    notation. Every round quantity - 10 RL, 20 PC, 100 EA - came out that way.
    Integers are therefore formatted through int(), and only genuinely
    fractional quantities go near normalize().
    """
    if value is None:
        return DASH
    d = Decimal(value)
    if d == d.to_integral_value():
        return f"{int(d):,}"
    return format(d.normalize(), "f")

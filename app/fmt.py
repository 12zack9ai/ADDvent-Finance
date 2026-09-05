"""Number formatting, in one place.

The marked-up invoice is rendered twice - as HTML for the screen and as a PDF
for sending back to the vendor - and the two must never disagree about a
number. They share these functions rather than each having their own.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

DASH = "—"

# The two PDF renderers use fpdf2's built-in Helvetica, which can only write
# Latin-1. Anything outside it raises rather than degrading, so a single em
# dash in a job name - or the DASH above, which stands in for every blank
# number on the page - took the whole download out with a 500. The text is
# mapped down to characters the font has instead.
_PDF_MAP = {
    ord("\u2014"): "-", ord("\u2013"): "-", ord("\u2212"): "-",
    ord("\u2018"): "'", ord("\u2019"): "'", ord("\u201a"): ",",
    ord("\u201c"): '"', ord("\u201d"): '"', ord("\u201e"): '"',
    ord("\u2026"): "...", ord("\u2022"): "-", ord("\u00a0"): " ",
    ord("\u2032"): "'", ord("\u2033"): '"', ord("\u2044"): "/",
    ord("\u20ac"): "EUR", ord("\u2122"): "(TM)", ord("\u00ae"): "(R)",
    ord("\u2264"): "<=", ord("\u2265"): ">=", ord("\u00d7"): "x",
}


def pdf_safe(text) -> str:
    """Text fpdf2's core fonts can actually write.

    Substitutes the typography that turns up in vendor documents and in our
    own copy, then replaces anything still outside Latin-1 rather than
    raising. A question mark in one word of a description is a blemish; a
    failed download is a person unable to send the marked-up invoice back to
    the vendor, which is the entire point of the document.
    """
    if text is None:
        return ""
    out = str(text).translate(_PDF_MAP)
    return out.encode("latin-1", "replace").decode("latin-1")


def fmt(value: Optional[Decimal], places: int) -> str:
    """Money, with the sign where a reader expects it.

    Formatting the amount and prefixing "$" puts the minus inside the number -
    "$-25,352.38" - which reads as a typo and is easy to skim past. On a cash
    flow forecast the negative weeks are the entire point, so the sign goes in
    front of the currency symbol where the eye catches it.
    """
    if value is None:
        return DASH
    q = Decimal(1).scaleb(-places)
    amount = Decimal(value).quantize(q)
    if amount < 0:
        return f"-${abs(amount):,}"
    return f"${amount:,}"


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

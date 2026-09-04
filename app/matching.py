"""Compare invoice lines against the job's master quote.

This module contains NO calls to Claude. Every comparison, every subtraction and
every total here is ordinary Decimal arithmetic, so the result is deterministic,
reproducible and testable. Claude reads the documents; this decides what the
numbers mean.

The master quote acts as a PRICE LIST for the job, not a consumable allocation:
materials arrive in several deliveries, so the same quoted item legitimately
appears on many invoices. A quote line can therefore be referenced by any number
of invoice lines.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional

from app.config import settings
from app.models import (
    VERDICT_MATCH,
    VERDICT_NOT_ON_QUOTE,
    VERDICT_OVER,
    VERDICT_UNDER,
    InvoiceLine,
    QuoteLine,
)

try:  # rapidfuzz is much better, but never let its absence break the app
    from rapidfuzz import fuzz

    def _similarity(a: str, b: str) -> float:
        return float(fuzz.token_set_ratio(a, b))
except ImportError:  # pragma: no cover
    from difflib import SequenceMatcher

    def _similarity(a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio() * 100.0


# Units that mean the same thing, so a fuzzy description match isn't rejected
# because one document says EA and the other says EACH.
_UOM_GROUPS = [
    {"ea", "each", "pc", "pcs", "piece", "unit"},
    {"sq", "square", "squares"},
    {"bdl", "bundle", "bundles"},
    {"lf", "linft", "linealfoot", "linearfoot", "ft", "foot", "feet"},
    {"sf", "sqft", "squarefoot", "squarefeet"},
    {"rl", "roll", "rolls"},
    {"bx", "box", "boxes"},
    {"pl", "pallet", "pallets", "plt"},
    {"gal", "gallon", "gallons"},
    {"lb", "lbs", "pound", "pounds"},
    {"cs", "case", "cases"},
]

_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")


def norm_text(value: Optional[str]) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    s = (value or "").lower().strip()
    s = _PUNCT_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def norm_sku(value: Optional[str]) -> str:
    """SKUs differ only by punctuation and case across documents."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def norm_uom(value: Optional[str]) -> str:
    return re.sub(r"[^a-z]", "", (value or "").lower())


def uom_compatible(a: Optional[str], b: Optional[str]) -> bool:
    """Unknown units are permissive; known-but-different units are not."""
    na, nb = norm_uom(a), norm_uom(b)
    if not na or not nb or na == nb:
        return True
    for group in _UOM_GROUPS:
        if na in group and nb in group:
            return True
    return False


# Corporate boilerplate that carries no identifying information. Stripped before
# comparing two vendor names so "Baker Supply Inc." and "BAKER SUPPLY, LLC"
# resolve to the same company.
_VENDOR_NOISE = {
    "inc", "incorporated", "llc", "llp", "lp", "ltd", "limited", "co", "corp",
    "corporation", "company", "the", "and", "of",
}
# A trailing branch or location, as supply houses print it:
# "ABC Supply Co. Inc. - Valley Cottage, NY".
#
# Whitespace on BOTH sides of the dash is required, and that is not cosmetic.
# Without it the pattern eats the second half of any hyphenated company name:
# "Smith-Cairns Roofing" and "Smith-Jones Supply" both reduce to "smith" and
# then match each other. A false match between two suppliers prices an invoice
# against the wrong company's quote, which is the worst thing this file can do.
_BRANCH_SUFFIX = re.compile(r"\s+[-\u2013\u2014]\s+[A-Za-z .'/]+(?:,\s*[A-Za-z]{2})?\s*$")

VENDOR_SIMILARITY = 82.0


def norm_vendor(value: Optional[str]) -> str:
    """Strip a supplier name down to the part that identifies the company.

    Branch suffixes come off first. Supply houses print the branch on their
    paperwork - "ABC Supply Co. Inc. - Valley Cottage, NY" on a real quote -
    and material for one job is routinely picked up from whichever branch has
    it. Two branches are one supplier honouring one quote, so leaving the
    branch in would file a Newburgh invoice as coming from a vendor we hold no
    quote for, and send every one of them to the owner to investigate.
    """
    text = _BRANCH_SUFFIX.sub("", value or "")
    words = [w for w in norm_text(text).split() if w and w not in _VENDOR_NOISE]
    return " ".join(words)


def vendor_matches(a: Optional[str], b: Optional[str]) -> bool:
    """Are these two strings the same supplier?

    Vendors abbreviate themselves inconsistently across their own paperwork -
    "New Castle Building Products" on the quote, "NEW CASTLE BLDG PRODUCTS" on
    the invoice. An exact string comparison would treat those as two suppliers
    and fail to price the invoice at all.
    """
    na, nb = norm_vendor(a), norm_vendor(b)
    if not na or not nb:
        # One side is unknown: don't claim a mismatch we can't substantiate.
        return True
    if na == nb:
        return True
    if na in nb or nb in na:
        return True
    return _similarity(na, nb) >= VENDOR_SIMILARITY


@dataclass
class QuoteIndex:
    """Lookup structure built once per quote, reused for every invoice line."""

    by_sku: dict[str, QuoteLine]
    by_desc: dict[str, QuoteLine]
    all_lines: list[QuoteLine]

    @classmethod
    def build(cls, lines: Iterable[QuoteLine]) -> "QuoteIndex":
        by_sku: dict[str, QuoteLine] = {}
        by_desc: dict[str, QuoteLine] = {}
        all_lines: list[QuoteLine] = []
        for line in lines:
            all_lines.append(line)
            sku = norm_sku(line.sku)
            if sku and sku not in by_sku:
                by_sku[sku] = line
            desc = norm_text(line.description)
            if desc and desc not in by_desc:
                by_desc[desc] = line
        return cls(by_sku=by_sku, by_desc=by_desc, all_lines=all_lines)


def match_line(line: InvoiceLine, index: QuoteIndex) -> tuple[Optional[QuoteLine], str]:
    """Find the quote line this invoice line refers to.

    Tiers run most-certain first and stop at the first hit, so a confident SKU
    match is never overridden by a fuzzy description elsewhere on the quote.
    """
    # A part number is definitive: trust it even if the units are written
    # differently on the two documents.
    sku = norm_sku(line.sku)
    if sku and sku in index.by_sku:
        return index.by_sku[sku], "sku"

    # Identical wording is NOT definitive on its own. The same description sold
    # by the roll and by the square is two different items at two different
    # prices, and comparing them would invent a variance that doesn't exist.
    desc = norm_text(line.description)
    if desc and desc in index.by_desc:
        candidate = index.by_desc[desc]
        if uom_compatible(line.uom, candidate.uom):
            return candidate, "exact"

    if desc:
        best: Optional[QuoteLine] = None
        best_score = 0.0
        for candidate in index.all_lines:
            cand_desc = norm_text(candidate.description)
            if not cand_desc:
                continue
            if not uom_compatible(line.uom, candidate.uom):
                continue
            score = _similarity(desc, cand_desc)
            if score > best_score:
                best, best_score = candidate, score
        if best is not None and best_score >= settings.fuzzy_threshold:
            return best, "fuzzy"

    return None, "none"


# Rounding slack when checking whether qty x unit_price reproduces the printed
# line total. Vendors round to the cent, so an exact match is not expected.
_RECONCILE_ABS = Decimal("0.05")
_RECONCILE_REL = Decimal("0.01")


def effective_unit_price(line) -> Optional[Decimal]:
    """Price per unit of the QUANTITY, derived from the printed line total.

    This is the packaging-independent way to price a line. Where a vendor
    quotes per box but counts in packs, the printed unit price and the quantity
    are in different units and cannot be compared directly - but
    extended / qty always lands in the same unit as qty.
    """
    if line.qty in (None, 0) or line.extended is None:
        return None
    try:
        return line.extended / line.qty
    except (ZeroDivisionError, InvalidOperation):
        return None


def units_disagree(line) -> bool:
    """True when the printed unit price is quoted per a different unit than qty.

    Detected two ways, either of which is sufficient:
      1. The stated price unit differs from the quantity unit (PK vs BX).
      2. qty x unit_price does not reproduce the printed line total.

    Real example from a New Castle quote:
        qty 8 PK, price 155.00/BX, amount 248.00
    8 x 155 is 1,240, not 248 - because 5 packs make a box. Comparing that
    155.00 against an invoice showing 31.00/PK would report a 400% price drop
    that did not happen.
    """
    if line.price_uom and line.uom and not uom_compatible(line.price_uom, line.uom):
        return True
    if line.qty is None or line.unit_price is None or line.extended is None:
        return False
    computed = line.qty * line.unit_price
    diff = abs(computed - line.extended)
    return diff > max(_RECONCILE_ABS, abs(line.extended) * _RECONCILE_REL)


def comparison_price(line) -> tuple[Optional[Decimal], str]:
    """The price to actually compare, plus which basis it is on.

    Returns the printed unit price when it is trustworthy, and the derived
    per-quantity price when the line's own units disagree. Falling back to the
    derived price is what keeps packaging differences from looking like price
    changes.
    """
    if units_disagree(line):
        effective = effective_unit_price(line)
        if effective is not None:
            return effective, "effective"
    if line.unit_price is not None:
        return line.unit_price, "unit"
    effective = effective_unit_price(line)
    if effective is not None:
        return effective, "effective"
    return None, "none"


def _extended_variance(
    line: InvoiceLine, unit_variance: Optional[Decimal], quote_unit: Optional[Decimal]
) -> Optional[Decimal]:
    """Dollar impact of the unit-price difference across the billed quantity.

    Prefers qty x unit-difference. Falls back to comparing the printed extended
    amount against what the quoted price would have produced.
    """
    if unit_variance is None:
        return None
    if line.qty is not None:
        return unit_variance * line.qty
    if line.extended is not None and quote_unit is not None and line.unit_price:
        # No quantity printed: infer it only for this comparison, never store it.
        if line.unit_price != 0:
            implied_qty = line.extended / line.unit_price
            return unit_variance * implied_qty
    return None


def compare_line(line: InvoiceLine, quote_line: Optional[QuoteLine], method: str) -> None:
    """Set verdict and variances on an invoice line, in place."""
    line.match_method = method
    line.quote_line_id = quote_line.id if quote_line else None
    line.quote_unit_price = quote_line.unit_price if quote_line else None
    line.unit_variance = None
    line.extended_variance = None

    if quote_line is None:
        line.verdict = VERDICT_NOT_ON_QUOTE
        return

    # Compare on a basis that survives packaging differences (see units_disagree).
    quoted, quote_basis = comparison_price(quote_line)
    billed, bill_basis = comparison_price(line)

    if quoted is None or billed is None:
        # Matched to a quote line, but one side has no usable price, so there is
        # nothing honest to compare. Flag rather than guess.
        line.verdict = VERDICT_NOT_ON_QUOTE
        line.match_method = f"{method}:no_price"
        return

    if "effective" in (quote_basis, bill_basis):
        # At least one side prices per a different unit than it counts in. Both
        # sides are put on the per-quantity basis so the comparison is like for
        # like; the printed prices are still what the reader sees.
        quoted = effective_unit_price(quote_line) or quoted
        billed = effective_unit_price(line) or billed
        line.match_method = f"{method}:effective"

    line.unit_variance = billed - quoted
    line.extended_variance = _extended_variance(line, line.unit_variance, quoted)

    if billed > quoted:
        line.verdict = VERDICT_OVER
    elif billed < quoted:
        line.verdict = VERDICT_UNDER
    else:
        line.verdict = VERDICT_MATCH


@dataclass
class InvoiceSummary:
    overbilled: Decimal = Decimal("0")
    underbilled: Decimal = Decimal("0")
    lines_over: int = 0
    lines_under: int = 0
    lines_match: int = 0
    lines_unmatched: int = 0

    @property
    def net_variance(self) -> Decimal:
        return self.overbilled - self.underbilled

    @property
    def has_issues(self) -> bool:
        return self.lines_over > 0


def compare_invoice(lines: list[InvoiceLine], quote_lines: list[QuoteLine]) -> InvoiceSummary:
    """Compare every line on an invoice against the master quote."""
    index = QuoteIndex.build(quote_lines)
    summary = InvoiceSummary()

    for line in lines:
        quote_line, method = match_line(line, index)
        compare_line(line, quote_line, method)

        if line.verdict == VERDICT_OVER:
            summary.lines_over += 1
            if line.extended_variance:
                summary.overbilled += line.extended_variance
        elif line.verdict == VERDICT_UNDER:
            summary.lines_under += 1
            if line.extended_variance:
                summary.underbilled += abs(line.extended_variance)
        elif line.verdict == VERDICT_MATCH:
            summary.lines_match += 1
        else:
            summary.lines_unmatched += 1

    return summary

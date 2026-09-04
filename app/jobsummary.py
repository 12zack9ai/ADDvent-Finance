"""The numbers at the top of a job, and the arithmetic that checks the system.

Every other check in this app looks at one invoice against one quote. That is
the right unit for "did they charge what they said they would", and it is blind
to a whole class of problem that only appears when you add the job up.

The clearest example, and the reason this exists: an invoice is wrong, we send
it back, the vendor issues a corrected one under a **new number** - and nobody
voids the original. Every individual check passes. Both invoices are perfectly
priced against the quote. And the job is now billed twice for the same
material. Nothing in a per-invoice comparison can see that. Adding the job up
can.

So the roll-up is not decoration. It is the system checking its own work:

    quoted + change orders + tolerance  <  billed     ->  something is wrong

and when that fires, this module goes looking for *why*, because "the total is
too high" is a fact and "these two invoices are for the same material" is
something a person can act on this afternoon.

Pure Python over rows already loaded. No model calls, no queries of its own -
it takes the job and adds up what is on it, in Decimal, like everything else
that touches money here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from app.config import settings
from app.matching import norm_sku, norm_text, norm_vendor, vendor_matches
from app.models import (
    APPROVAL_APPROVED,
    CO_PROPOSED,
    APPROVAL_PAID,
    APPROVAL_REJECTED,
    VERDICT_NOT_ON_QUOTE,
    Invoice,
    Job,
)

ZERO = Decimal("0")

# Two invoices this far apart in time are a re-order, not a correction. Six
# weeks covers "we sent it back, they fixed it" comfortably; beyond that, the
# second delivery is usually a real second delivery.
CORRECTION_WINDOW_DAYS = 45

# How much of the smaller invoice has to reappear on the larger one before it
# reads as the same material billed twice rather than a follow-on order.
OVERLAP_FRACTION = Decimal("0.5")


@dataclass
class VendorRoll:
    """One vendor's position on this job: quoted, billed, and the gap."""

    vendor: str
    quoted: Decimal = ZERO
    invoiced: Decimal = ZERO
    change_orders: Decimal = ZERO
    invoice_count: int = 0
    has_quote: bool = False

    @property
    def allowance(self) -> Decimal:
        """What this vendor may bill before it is worth asking why.

        Change orders count. Authorised extra scope is the ordinary reason a
        job bills above its original quote, and flagging it would train people
        to ignore the flag.
        """
        base = self.quoted + self.change_orders
        return base + _tolerance(base)

    @property
    def over_quote(self) -> Decimal:
        if not self.has_quote:
            return ZERO
        return max(self.invoiced - self.allowance, ZERO)

    @property
    def remaining(self) -> Decimal:
        """Still to bill against this quote. Negative means over."""
        return self.quoted + self.change_orders - self.invoiced


@dataclass
class Overlap:
    """Two invoices from one vendor that look like the same material twice."""

    earlier: Invoice
    later: Invoice
    shared_value: Decimal          # what the later one re-bills
    shared_lines: int
    days_apart: int
    identical_total: bool

    @property
    def headline(self) -> str:
        if self.identical_total:
            return "Same vendor, same total, billed twice"
        return "Same material billed on two invoices"

    @property
    def when(self) -> str:
        if self.days_apart == 0:
            # The common case for a correction: the vendor reissues under the
            # original date.
            return "dated the same day"
        return f"{self.days_apart} day{'' if self.days_apart == 1 else 's'} apart"

    @property
    def explanation(self) -> str:
        a = self.earlier.invoice_number or f"#{self.earlier.id}"
        b = self.later.invoice_number or f"#{self.later.id}"
        if self.identical_total:
            return (
                f"{a} and {b} are both for {_money(self.earlier.total)} from "
                f"{self.later.vendor}, {self.when}. Either one is a duplicate, "
                f"or one was meant to replace the other and the original was "
                f"never voided."
            )
        return (
            f"{self.shared_lines} line"
            f"{'' if self.shared_lines == 1 else 's'} worth "
            f"{_money(self.shared_value)} appear on both {a} and {b}, {self.when}. "
            f"If {b} is a corrected version of {a}, {a} has to be rejected - "
            f"otherwise this job is billed for the same material twice."
        )


@dataclass
class Summary:
    """Everything on the strip at the top of a job page."""

    quoted: Decimal = ZERO
    invoiced: Decimal = ZERO
    change_orders: Decimal = ZERO
    off_quote: Decimal = ZERO
    off_quote_lines: int = 0
    found: Decimal = ZERO           # overbilling the line check caught
    still_held: Decimal = ZERO      # of that, on invoices nobody has approved
    approved_anyway: Decimal = ZERO  # of that, on invoices signed off regardless
    invoice_count: int = 0
    vendors: list[VendorRoll] = field(default_factory=list)
    overlaps: list[Overlap] = field(default_factory=list)
    proposed_change_orders: list = field(default_factory=list)

    @property
    def proposed_total(self) -> Decimal:
        """Extra scope read off a document that nobody has signed yet."""
        total = sum((co.amount or ZERO for co in self.proposed_change_orders), ZERO)
        return Decimal(total).quantize(Decimal("0.01"))

    @property
    def has_quote(self) -> bool:
        return any(v.has_quote for v in self.vendors)

    @property
    def authorised(self) -> Decimal:
        """Quote plus written change orders. What this job is allowed to cost."""
        return self.quoted + self.change_orders

    @property
    def remaining(self) -> Decimal:
        return self.authorised - self.invoiced

    @property
    def over_quote(self) -> Decimal:
        """Billed beyond what is authorised, added up per vendor.

        Per vendor rather than job-wide on purpose. A job where the roofer is
        $9,000 over and the dumpster company is $9,000 under has a problem, and
        a single job-wide figure would report nothing at all.
        """
        return sum((v.over_quote for v in self.vendors), ZERO)

    @property
    def percent_billed(self) -> Optional[int]:
        if self.authorised <= ZERO:
            return None
        return int((self.invoiced / self.authorised * 100).quantize(Decimal("1")))

    @property
    def needs_explaining(self) -> bool:
        """The self-check. Billed past what anyone authorised."""
        return self.over_quote > ZERO


def _tolerance(total: Decimal) -> Decimal:
    """The same allowance the per-invoice router uses, applied to the job."""
    flat = Decimal(settings.tolerance_abs)
    pct = abs(total) * Decimal(str(settings.tolerance_pct)) / Decimal(100)
    return max(flat, pct)


def _money(value: Optional[Decimal]) -> str:
    if value is None:
        return "$0.00"
    return f"${value.quantize(Decimal('0.01')):,}"


def _same_supplier(a: str, b: str) -> bool:
    """One supplier, however they signed this particular piece of paper.

    `vendor_matches` is the rest of the system's answer to that question, and
    it has to be the answer here too. Grouping more strictly would split New
    Castle into two rolls on the strength of an abbreviation - understating an
    overrun and hiding a duplicate, which is the one thing this file exists to
    find.

    The one place it is deliberately stricter: `vendor_matches` returns True
    when either side is blank, because refusing to price an invoice over a
    missing name would be worse than pricing it. Here a blank name is its own
    bucket, since folding every unnamed vendor into whichever roll happens to
    be first would invent a supplier position nobody has.
    """
    na, nb = norm_vendor(a), norm_vendor(b)
    if not na or not nb:
        return not na and not nb
    return vendor_matches(a, b)


def _counts(invoice: Invoice) -> bool:
    """A rejected invoice is not money owed and must not inflate the roll-up."""
    return invoice.approval_status != APPROVAL_REJECTED


def build(job: Job) -> Summary:
    """Add the job up, then check the total against what was authorised."""
    invoices = [i for i in job.invoices if _counts(i)]
    summary = Summary(invoice_count=len(invoices))

    rolls: list[VendorRoll] = []

    def roll_for(vendor: str) -> VendorRoll:
        for roll in rolls:
            if _same_supplier(roll.vendor, vendor):
                return roll
        roll = VendorRoll(vendor=(vendor or "").strip() or "Unknown vendor")
        rolls.append(roll)
        return roll

    for quote in job.masters:
        roll = roll_for(quote.vendor)
        roll.has_quote = True
        roll.quoted += quote.total or ZERO
        summary.quoted += quote.total or ZERO

    for change_order in job.change_orders:
        # Approved only. A change order raises what this job is allowed to
        # cost, so counting a proposed one would quietly close the very gap
        # that is supposed to bring somebody to look at it.
        if not change_order.is_live:
            if change_order.status == CO_PROPOSED:
                summary.proposed_change_orders.append(change_order)
            continue
        amount = change_order.amount or ZERO
        summary.change_orders += amount
        roll_for(change_order.vendor).change_orders += amount

    for invoice in invoices:
        roll = roll_for(invoice.vendor)
        roll.invoiced += invoice.total or ZERO
        roll.invoice_count += 1
        summary.invoiced += invoice.total or ZERO

        over = invoice.overbilled_amount or ZERO
        summary.found += over
        if invoice.approval_status in (APPROVAL_APPROVED, APPROVAL_PAID):
            summary.approved_anyway += over
        else:
            summary.still_held += over

        for line in invoice.lines:
            if line.verdict == VERDICT_NOT_ON_QUOTE:
                summary.off_quote += line.extended or ZERO
                summary.off_quote_lines += 1

    summary.vendors = sorted(rolls, key=lambda v: (-v.invoiced, v.vendor.lower()))
    summary.overlaps = find_overlaps(invoices)
    return summary


# --- looking for the reason the total is too high --------------------------

def find_overlaps(invoices: list[Invoice]) -> list[Overlap]:
    """Pairs of invoices that appear to bill the same material twice.

    Two shapes, both of which pass every per-invoice check:

      * **The same invoice twice.** Same vendor, same total. Usually a resend
        that got filed as new because the vendor changed the number.
      * **A correction that never replaced anything.** We send an invoice back,
        the vendor issues a fixed one under a new number, and nobody rejects
        the original. The totals differ - that is the whole point of the
        correction - so only the line items give it away.

    Deliberately conservative. This produces a question for a person, not a
    verdict, and a false one costs more attention than it saves.
    """
    found: list[Overlap] = []
    groups: list[list[Invoice]] = []
    for invoice in invoices:
        for group in groups:
            if _same_supplier(group[0].vendor, invoice.vendor):
                group.append(invoice)
                break
        else:
            groups.append([invoice])

    for group in groups:
        if len(group) < 2:
            continue
        ordered = sorted(group, key=_sort_date)
        for i, earlier in enumerate(ordered):
            for later in ordered[i + 1:]:
                overlap = _compare(earlier, later)
                if overlap is not None:
                    found.append(overlap)

    found.sort(key=lambda o: (-o.shared_value, o.days_apart))
    return found


def _sort_date(invoice: Invoice):
    return (invoice.invoice_date or invoice.created_at.date(), invoice.id)


def _days_apart(a: Invoice, b: Invoice) -> int:
    return abs((_sort_date(b)[0] - _sort_date(a)[0]).days)


def _compare(earlier: Invoice, later: Invoice) -> Optional[Overlap]:
    days = _days_apart(earlier, later)
    if days > CORRECTION_WINDOW_DAYS:
        return None

    totals = (earlier.total, later.total)
    identical = all(t is not None for t in totals) and totals[0] == totals[1] \
        and (earlier.total or ZERO) > ZERO

    shared_value, shared_lines = _shared(earlier, later)

    if identical:
        return Overlap(earlier, later, earlier.total or ZERO,
                       shared_lines, days, identical_total=True)

    # Not identical: only interesting if most of the smaller invoice reappears
    # on the other one. A follow-on delivery of different material is normal
    # and must stay silent.
    smaller = min(
        (earlier.total or ZERO, later.total or ZERO),
        key=lambda t: t if t > ZERO else Decimal("Infinity"),
    )
    if smaller <= ZERO or shared_lines == 0:
        return None
    if shared_value < smaller * OVERLAP_FRACTION:
        return None

    return Overlap(earlier, later, shared_value, shared_lines, days,
                   identical_total=False)


def _line_key(line) -> str:
    """What makes two invoice lines "the same item"."""
    return norm_sku(line.sku) or norm_text(line.description)


def _shared(a: Invoice, b: Invoice) -> tuple[Decimal, int]:
    """Value and count of items billed on both invoices.

    Matched by item, not by amount, because a corrected invoice re-bills the
    same item at a different price - that is precisely what makes it a
    correction. The value reported is the smaller of the two, so the figure is
    never larger than what could actually be duplicated.
    """
    a_lines: dict[str, Decimal] = {}
    for line in a.lines:
        key = _line_key(line)
        if key:
            a_lines[key] = a_lines.get(key, ZERO) + (line.extended or ZERO)

    value, count = ZERO, 0
    seen: set[str] = set()
    for line in b.lines:
        key = _line_key(line)
        if not key or key not in a_lines or key in seen:
            continue
        seen.add(key)
        count += 1
        value += min(a_lines[key], line.extended or ZERO)

    # Line extensions do not always sum to the invoice total - a vendor
    # discount, a rounded subtotal, a page we never received. Reporting a
    # figure larger than either invoice would be indefensible, whatever the
    # lines say, so the smaller invoice is the ceiling.
    ceiling = min(
        (t for t in (a.total, b.total) if t is not None and t > ZERO),
        default=None,
    )
    if ceiling is not None:
        value = min(value, ceiling)
    return value, count

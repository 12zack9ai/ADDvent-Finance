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
    # False when one of the two had no readable line items, so the pair rests
    # on the totals alone. The note has to say so rather than imply we looked.
    lines_compared: bool = True

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
        if self.identical_total and not self.lines_compared:
            return (
                f"{a} and {b} are both for {_fmt(self.earlier.total)} from "
                f"{self.later.vendor}, {self.when}. Neither one had readable "
                f"line items, so this is the totals alone - worth opening both "
                f"to see whether they are the same delivery billed twice."
            )
        if self.identical_total:
            return (
                f"{a} and {b} are both for {_fmt(self.earlier.total)} from "
                f"{self.later.vendor}, {self.when}, and bill the same material. "
                f"Either one is a duplicate, or one was meant to replace the "
                f"other and the original was never voided."
            )
        return (
            f"{self.shared_lines} line"
            f"{'' if self.shared_lines == 1 else 's'} worth "
            f"{_fmt(self.shared_value)} appear on both {a} and {b}, {self.when}. "
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
    # A different supplier entirely, with no quote of their own on this job.
    # ABC quoted the roof; a couple of last-minute things got picked up at New
    # Castle because that is where they were in stock. Nothing about that is
    # wrong, and it is a different thing from an item the QUOTED supplier
    # slipped onto their own invoice - which is what off_quote means.
    unquoted_supplier: Decimal = ZERO
    unquoted_vendors: list[str] = field(default_factory=list)
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
    def unexplained(self) -> Decimal:
        """The part of the spend that is not a quoted item at its quoted price.

        Off-quote material plus anything billed above its quoted price. These
        are the two ways a job can genuinely cost more than it should.

        Spend with a supplier who has no quote here is NOT in this. A quote
        from ABC does not price a last-minute pickup at New Castle, and never
        was going to; treating it as unexplained would light the job up for
        doing something ordinary.
        """
        return _cents(self.off_quote + self.found)

    @property
    def over_but_accounted_for(self) -> bool:
        """Billed past the quote, and every dollar of it explained.

        The ordinary case on a roof, and the reason this exists: the crew uses
        more material than was quoted. The unit prices are the quoted unit
        prices, the vendor has done nothing wrong, and the job simply took more
        squares than somebody estimated in an office. Calling that a problem
        would put a red panel on most jobs, and a warning that fires on normal
        work is a warning people learn to click past.
        """
        return self.over_quote > ZERO and self.unexplained <= _tolerance(self.authorised)

    @property
    def needs_explaining(self) -> bool:
        """Something here is actually wrong, not merely large.

        Deliberately NOT "billed more than quoted". A quote prices material; it
        does not cap how much of it a roof turns out to need. What matters is
        whether the money that went out is accounted for - so this fires on
        money billed above a quoted price, on material nobody quoted, and on
        the same material billed twice. Not on quantity.
        """
        if self.overlaps:
            return True
        return self.over_quote > ZERO and self.unexplained > _tolerance(self.authorised)


def _tolerance(total: Decimal) -> Decimal:
    """The same allowance the per-invoice router uses, applied to the job."""
    flat = Decimal(settings.tolerance_abs)
    pct = abs(total) * Decimal(str(settings.tolerance_pct)) / Decimal(100)
    return max(flat, pct)


def _fmt(value: Optional[Decimal]) -> str:
    """Format for a sentence. Returns TEXT - never use it in arithmetic."""
    if value is None:
        return "$0.00"
    return f"${value.quantize(Decimal('0.01')):,}"


def _cents(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"))


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
    """Add the job up, then check the total against what was authorised.

    The supplier side only. A sub's contract and invoices are added up on the
    Subs page (subs.py); counted here too, a $25,000 draw read as $25,000 "not
    on the quote" against the shingle supplier's quote.
    """
    contracts = getattr(job, "subcontracts", None) or []

    def _subs(invoice) -> bool:
        # The same test as vendor_roles.is_subs, on whatever a caller passes.
        return bool(getattr(invoice, "is_subcontract", False)) or any(
            q.vendor and invoice.vendor and vendor_matches(q.vendor, invoice.vendor)
            for q in contracts)

    invoices = [i for i in job.invoices if _counts(i) and not _subs(i)]
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
        if getattr(quote, "is_subcontract", False):
            continue                      # a sub's contract: the Subs page's
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

        if invoice.quote_id is None:
            # No quote from THIS supplier on this job. Every line reads as
            # unmatched because there was nothing to match against, so counting
            # them as off-quote items would be counting the absence of a quote
            # as a discrepancy.
            summary.unquoted_supplier += invoice.total or ZERO
            name = (invoice.vendor or "").strip() or "Unknown vendor"
            if not any(vendor_matches(v, name) for v in summary.unquoted_vendors):
                summary.unquoted_vendors.append(name)
            continue

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
        # Two invoices from one supplier landing on the same total is evidence,
        # but it is not proof, and on a job with nine deliveries from the same
        # yard it happens by coincidence. When both invoices have readable
        # lines, they have to actually share material before this is a
        # question worth putting to anybody.
        comparable = _has_lines(earlier) and _has_lines(later)
        if comparable and not _shares_material(earlier, later, shared_value,
                                               earlier.total or ZERO):
            return None
        return Overlap(earlier, later, earlier.total or ZERO,
                       shared_lines, days, identical_total=True,
                       lines_compared=comparable)

    # Not identical: only interesting if most of BOTH invoices is the same
    # material. A follow-on delivery is normal and must stay silent.
    smaller = min(
        (earlier.total or ZERO, later.total or ZERO),
        key=lambda t: t if t > ZERO else Decimal("Infinity"),
    )
    larger = max(earlier.total or ZERO, later.total or ZERO)
    if smaller <= ZERO or shared_lines == 0:
        return None
    # Measured against the LARGER of the two, which is the whole point. A $158
    # delivery of nails is entirely contained in a $12,975 delivery that also
    # carried nails - every test below this one passes, and it is not a
    # duplicate, it is Tuesday. Something that replaces an invoice has to
    # account for most of what it replaces, so the shared material has to be a
    # real share of the bigger piece of paper, not just of the smaller.
    if shared_value < larger * OVERLAP_FRACTION:
        return None
    if not _rebills_everything(earlier, later):
        return None
    if _quantities_disagree(earlier, later):
        return None

    return Overlap(earlier, later, shared_value, shared_lines, days,
                   identical_total=False)


def _has_lines(invoice: Invoice) -> bool:
    """Did anything readable come off this invoice to compare?

    An unreadable scan produces an invoice with a total and no usable lines.
    Silence would be wrong there - we have not checked, and saying nothing
    reads as "checked and fine".
    """
    return bool({_line_key(line) for line in invoice.lines} - {""})


def _shares_material(a: Invoice, b: Invoice, shared_value: Decimal,
                     smaller: Decimal) -> bool:
    """Are these two invoices plausibly for the same material?

    Either one re-bills the whole of the other - a resend or a correction - or
    enough of the smaller one by value reappears on the larger to be worth a
    look. Two invoices that share no item at all are two deliveries, whatever
    their totals happen to come to.
    """
    if _rebills_everything(a, b):
        return True
    return smaller > ZERO and shared_value >= smaller * OVERLAP_FRACTION


def _rebills_everything(a: Invoice, b: Invoice) -> bool:
    """Does one of these invoices re-bill every item on the other?

    The test that tells a correction from a staged delivery, and it is needed
    because value alone cannot. A roof quoted at 186 squares arrives in two
    deliveries of 93; both invoices are mostly shingle, so most of the smaller
    one by value is "on" the larger one and the value rule above fires on a
    completely ordinary pair of delivery tickets.

    What a correction or a resend does that a second delivery does not is
    bill the whole of the earlier invoice again. The second delivery always
    carries something the first one did not - that is why there was a second
    delivery.
    """
    a_keys = {_line_key(line) for line in a.lines} - {""}
    b_keys = {_line_key(line) for line in b.lines} - {""}
    if not a_keys or not b_keys:
        return False
    return a_keys <= b_keys or b_keys <= a_keys


@dataclass
class SharedItem:
    """One item that appears on both invoices, as each of them billed it."""

    label: str
    a_qty: Optional[Decimal]
    b_qty: Optional[Decimal]
    a_extended: Decimal
    b_extended: Decimal

    @property
    def same_quantity(self) -> bool:
        return (self.a_qty is not None and self.b_qty is not None
                and self.a_qty == self.b_qty)


def shared_items(a: Invoice, b: Invoice) -> list[SharedItem]:
    """The items billed on both, so a person can see what was matched.

    The compare screen exists because "$12,975.00 on both" is a claim, and a
    claim about money should be shown rather than asserted.
    """
    def by_key(invoice: Invoice) -> dict[str, tuple[str, Optional[Decimal], Decimal]]:
        out: dict[str, tuple[str, Optional[Decimal], Decimal]] = {}
        for line in invoice.lines:
            key = _line_key(line)
            if not key:
                continue
            label = (line.description or line.sku or key).strip()
            name, qty, ext = out.get(key, (label, None, ZERO))
            if line.qty is not None:
                qty = (qty or ZERO) + line.qty
            out[key] = (name or label, qty, ext + (line.extended or ZERO))
        return out

    left, right = by_key(a), by_key(b)
    items = []
    for key, (label, qty, ext) in left.items():
        if key not in right:
            continue
        _, other_qty, other_ext = right[key]
        items.append(SharedItem(label, qty, other_qty, ext, other_ext))
    items.sort(key=lambda i: -min(i.a_extended, i.b_extended))
    return items


def _quantities_disagree(a: Invoice, b: Invoice) -> bool:
    """Do the shared items appear in different amounts on the two invoices?

    The last thing separating a replacement from a second load of the same
    material, and the one that survives a yard where every invoice shares
    items with every other. An invoice that replaces another bills the same
    quantities - that is what makes it the same delivery. A follow-on load
    bills a different amount of the same thing.

    Only ever used to stay quiet, and only on evidence: a quantity we do not
    have proves nothing, so an unread quantity leaves the earlier tests to
    decide rather than silencing them.
    """
    a_qty = _quantities(a)
    b_qty = _quantities(b)
    for key, qty in a_qty.items():
        other = b_qty.get(key)
        if other is None or qty is None:
            continue
        if qty != other:
            return True
    return False


def _quantities(invoice: Invoice) -> dict[str, Optional[Decimal]]:
    """How much of each item this invoice bills. None where it was not read."""
    out: dict[str, Optional[Decimal]] = {}
    for line in invoice.lines:
        key = _line_key(line)
        if not key:
            continue
        if line.qty is None:
            out[key] = None
        elif key in out and out[key] is not None:
            out[key] = out[key] + line.qty
        elif key not in out:
            out[key] = line.qty
    return out


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

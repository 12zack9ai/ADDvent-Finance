"""Asking for the money back.

Finding an overbill saves nothing. The saving happens when somebody tells the
vendor, in writing, with the numbers, and then follows it up - and that is
exactly the conversation that gets postponed past the point of being winnable.
Job 241640 found $2,596 over quote on its first day. That figure is worth
nothing until it is a credit memo.

So this composes the letter. It is a draft for a person to read, edit and
send from their own mail - deliberately not sent by the app, which writes to
nobody outside the company and should not start with a dispute.

Everything in the draft is computed here in Decimal from lines the matching
engine already priced. No model writes a number, and no number appears in the
letter that is not on the invoice or the quote.

The second half of the value is the part nobody has today: which vendors do
this repeatedly. A supplier who overbills once made a mistake. A supplier who
overbills on one invoice in three is a pricing decision, and that is a fact
worth having before the next order goes out.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from app.config import settings
from app.fmt import money
from app.models import (
    DISPUTE_CREDITED,
    DISPUTE_OPEN,
    Dispute,
    Invoice,
    VERDICT_NOT_ON_QUOTE,
    VERDICT_OVER,
)

ZERO = Decimal("0")


@dataclass
class Item:
    """One line we are asking about."""

    description: str
    qty: Optional[Decimal]
    quoted: Optional[Decimal]
    billed: Optional[Decimal]
    difference: Decimal
    reason: str                    # "priced above the quote" | "not on the quote"

    @property
    def percent(self) -> Optional[int]:
        if not self.quoted or self.quoted <= ZERO or self.billed is None:
            return None
        return int(round((self.billed - self.quoted) / self.quoted * 100))


@dataclass
class Draft:
    """A dispute letter, and the arithmetic behind it."""

    invoice: Invoice
    items: list[Item] = field(default_factory=list)
    priced_over: Decimal = ZERO
    unquoted: Decimal = ZERO

    @property
    def total(self) -> Decimal:
        return self.priced_over + self.unquoted

    @property
    def worth_sending(self) -> bool:
        return bool(self.items) and self.total > ZERO

    @property
    def subject(self) -> str:
        number = self.invoice.invoice_number or f"#{self.invoice.id}"
        return f"Invoice {number} — pricing query"


def build(invoice: Invoice) -> Draft:
    """What is wrong with this invoice, line by line, in Decimal."""
    draft = Draft(invoice=invoice)
    quote = invoice.quote

    for line in invoice.lines:
        name = (line.description or line.sku or "").strip() or "Unnamed line"
        if line.verdict == VERDICT_OVER:
            over = line.extended_variance or ZERO
            if over <= ZERO:
                continue
            draft.priced_over += over
            draft.items.append(Item(
                description=name, qty=line.qty,
                quoted=line.quote_unit_price, billed=line.unit_price,
                difference=over, reason="priced above the quote",
            ))
        elif line.verdict == VERDICT_NOT_ON_QUOTE and quote is not None:
            # Only worth raising when there IS a quote to be off. On a job with
            # no quote from this vendor, every line reads as unmatched and the
            # letter would be a list of everything they sent.
            amount = line.extended or ZERO
            if amount <= ZERO:
                continue
            draft.unquoted += amount
            draft.items.append(Item(
                description=name, qty=line.qty,
                quoted=None, billed=line.unit_price,
                difference=amount, reason="not on the quote",
            ))

    draft.items.sort(key=lambda i: -i.difference)
    return draft


def letter(draft: Draft) -> str:
    """The email a person reads, edits and sends. Plain text on purpose."""
    invoice = draft.invoice
    quote = invoice.quote
    number = invoice.invoice_number or f"#{invoice.id}"
    job = invoice.job.job_number if invoice.job else ""

    lines = [
        "Hello,",
        "",
        f"We are reviewing invoice {number}"
        + (f", dated {invoice.invoice_date:%d %B %Y}" if invoice.invoice_date else "")
        + (f", for job {job}" if job else "")
        + ", against the quote we accepted"
        + (f" ({quote.quote_number})" if quote and quote.quote_number else "")
        + (f" dated {quote.quote_date:%d %B %Y}" if quote and quote.quote_date else "")
        + ".",
        "",
    ]

    if draft.priced_over > ZERO:
        lines.append("These lines were billed above the quoted price:")
        lines.append("")
        for item in draft.items:
            if item.reason != "priced above the quote":
                continue
            detail = f"  - {item.description}: quoted {money(item.quoted)}"
            detail += f", billed {money(item.billed)}"
            if item.percent is not None:
                detail += f" (+{item.percent}%)"
            if item.qty is not None:
                # A stored quantity is Decimal("93.0000"), and "g" keeps every
                # place of a Decimal - the letter said "on 93.0000".
                detail += f" on {format(item.qty.normalize(), 'f')}"
            detail += f" — {money(item.difference)}"
            lines.append(detail)
        lines.append("")

    if draft.unquoted > ZERO:
        lines.append("And these were charged but are not on the quote:")
        lines.append("")
        for item in draft.items:
            if item.reason != "not on the quote":
                continue
            lines.append(f"  - {item.description} — {money(item.difference)}")
        lines.append("")

    lines += [
        f"That comes to {money(draft.total)}.",
        "",
        "Could you send a corrected invoice, or a credit memo for the "
        "difference? If any of these were agreed with someone here, let us "
        "know who and when and we will close it off at our end.",
        "",
        "Thank you,",
        "",
        settings.site_name.split("·")[0].strip(),
    ]
    return "\n".join(lines)


# --- what it tells us about a vendor ---------------------------------------

@dataclass
class Record:
    """One vendor's history of being asked for money back."""

    vendor: str
    raised: int = 0
    open_count: int = 0
    asked: Decimal = ZERO
    recovered: Decimal = ZERO

    @property
    def recovery_rate(self) -> Optional[int]:
        if self.asked <= ZERO:
            return None
        return int(round(self.recovered / self.asked * 100))


def history(disputes: list[Dispute]) -> list[Record]:
    """Group raised disputes by vendor, worst first.

    The number nobody has today: which suppliers this keeps happening with.
    """
    by_vendor: dict[str, Record] = {}
    for dispute in disputes:
        name = (dispute.vendor or "").strip() or "Unknown vendor"
        record = by_vendor.setdefault(name.lower(), Record(vendor=name))
        record.raised += 1
        record.asked += dispute.amount or ZERO
        if dispute.status == DISPUTE_OPEN:
            record.open_count += 1
        if dispute.status == DISPUTE_CREDITED:
            record.recovered += dispute.credited or ZERO

    out = list(by_vendor.values())
    out.sort(key=lambda r: (-r.asked, r.vendor.lower()))
    return out

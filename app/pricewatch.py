"""Items not on the quote, held to the price they were first billed at.

Zack, 2026-09-10: "invoice one comes in but didnt have a 3" pipe boot on it
but it was $65. invoice two comes in with the pipe boot at $66 dollars.....
thats a problem... need an easy way to see that"

Nothing prices an item the quote left out, so the first invoice that bills it
on a job sets the price, and every later invoice from the same supplier on
that job is held to it. A change in either direction is flagged: the colours
on the marked-up invoice already treat a difference as a difference, and a
cheaper pipe boot is as much a question as a dearer one.

Pure Python, like matching.py - and items are matched with matching.py's own
tiers, so two lines are "the same item" here exactly when they would be the
same item against a quote.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.matching import (
    QuoteIndex,
    comparison_price,
    effective_unit_price,
    match_line,
    norm_vendor,
    vendor_matches,
)
from app.models import (
    APPROVAL_REJECTED,
    VERDICT_NOT_ON_QUOTE,
    Invoice,
    InvoiceLine,
    Job,
    as_utc,
)

# Half a cent. A price derived from line total / quantity can carry division
# noise nobody printed; a difference smaller than this is the same price.
SAME_PRICE = Decimal("0.005")


def _oldest_first(invoice: Invoice) -> tuple:
    """By the date printed on the invoice, then by when it arrived.

    The printed date, because that is the order the vendor billed in - an
    invoice from last month forwarded today is still the first one.
    """
    arrived = as_utc(invoice.created_at)
    printed = invoice.invoice_date or (arrived.date() if arrived else date.max)
    return (printed, arrived.timestamp() if arrived else 0.0, invoice.id or 0)


def first_price_if_changed(line: InvoiceLine, first: InvoiceLine) -> Optional[Decimal]:
    """What `first` charged, when `line` charges something else. None if the same.

    None as well when either side has no usable price: there is nothing honest
    to compare, and a flag would be a guess.
    """
    billed, billed_basis = comparison_price(line)
    was, was_basis = comparison_price(first)
    if billed is None or was is None:
        return None
    if "effective" in (billed_basis, was_basis):
        # As in compare_line: one side prices per a different unit than it
        # counts in, so both go on the per-quantity basis.
        billed = effective_unit_price(line) or billed
        was = effective_unit_price(first) or was
    if abs(billed - was) < SAME_PRICE:
        return None
    return was


def check_job(session: Session, job: Job) -> int:
    """Re-flag every invoice on a job. Returns how many lines changed price.

    Always the whole job, never one invoice: a late arrival dated before the
    rest becomes the first time an item was billed, and moves the answer for
    every invoice after it.
    """
    invoices = session.scalars(select(Invoice).where(Invoice.job_id == job.id)).all()

    # Per supplier, every unquoted line with a price, oldest invoice first.
    # QuoteIndex keeps the first line it sees for each part number and
    # description, which is what makes the first invoice the one that counts.
    history: list[tuple[str, list[InvoiceLine]]] = []
    flagged = 0

    for invoice in sorted(invoices, key=_oldest_first):
        before = invoice.lines_price_changed or 0
        invoice.lines_price_changed = 0
        for line in invoice.lines:
            line.first_billed_price = None
            line.first_billed_invoice_id = None

        # A rejected invoice is not a price anybody accepted. And an unknown
        # supplier cannot be held to - or hold anyone to - a known one's price,
        # which is stricter than vendor_matches is when pricing against a quote.
        if invoice.approval_status == APPROVAL_REJECTED or not norm_vendor(invoice.vendor):
            if before:
                invoice.render_path = ""
            continue

        earlier = next(
            (lines for vendor, lines in history if vendor_matches(vendor, invoice.vendor)),
            None,
        )
        unquoted = [line for line in invoice.lines if line.verdict == VERDICT_NOT_ON_QUOTE]

        if earlier:
            index = QuoteIndex.build(earlier)
            for line in unquoted:
                first, _method = match_line(line, index)
                if first is None:
                    continue
                was = first_price_if_changed(line, first)
                if was is None:
                    continue
                line.first_billed_price = was
                line.first_billed_invoice_id = first.invoice_id
                invoice.lines_price_changed += 1

        if invoice.lines_price_changed != before:
            invoice.render_path = ""  # the cached marked-up PDF is stale
        flagged += invoice.lines_price_changed

        if earlier is None:
            earlier = []
            history.append((invoice.vendor, earlier))
        earlier.extend(line for line in unquoted if comparison_price(line)[0] is not None)

    session.flush()
    return flagged

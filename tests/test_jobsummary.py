"""The job roll-up, and the arithmetic that checks the system's own work.

Every other check here compares one invoice to one quote. That is blind to the
failure Zack described: an invoice comes back corrected under a new number, the
original is never voided, and the job is billed twice for the same material.
Both invoices pass their own price check perfectly. Only adding the job up
finds it.

So the tests that matter are the ones where nothing is individually wrong.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import jobsummary  # noqa: E402
from app.models import (  # noqa: E402
    APPROVAL_APPROVED,
    APPROVAL_PAID,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    VERDICT_MATCH,
    VERDICT_NOT_ON_QUOTE,
    VERDICT_OVER,
    ChangeOrder,
    Invoice,
    InvoiceLine,
    Job,
    Quote,
)

_ids = {"n": 0}


def _next_id() -> int:
    _ids["n"] += 1
    return _ids["n"]


def job(quotes=(), invoices=(), change_orders=()) -> Job:
    j = Job(job_number="260000", name="Daul Gardens")
    j.quotes = list(quotes)
    j.invoices = list(invoices)
    j.change_orders = list(change_orders)
    return j


def quote(total="100000.00", vendor="New Castle Building Products") -> Quote:
    return Quote(job_id=1, document_id=1, vendor=vendor, is_master=True,
                 total=D(total))


def line(sku="GAFT3PG", extended="1000.00", verdict=VERDICT_MATCH, desc=""):
    return InvoiceLine(sku=sku, description=desc, extended=D(extended),
                       verdict=verdict)


def invoice(total="10000.00", vendor="New Castle Building Products",
            number=None, over="0", on=None, lines=(),
            status=APPROVAL_PENDING) -> Invoice:
    n = _next_id()
    inv = Invoice(
        job_id=1, document_id=n, vendor=vendor,
        invoice_number=number or f"INV-{n}",
        invoice_date=on or date(2026, 9, 10),
        total=D(total), overbilled_amount=D(over),
        approval_status=status, quote_id=1,
    )
    inv.id = n
    inv.created_at = datetime(2026, 9, 10, tzinfo=timezone.utc)
    inv.lines = list(lines)
    return inv


# --- the five numbers -----------------------------------------------------

def test_an_empty_job_reports_zeroes_rather_than_nothing():
    s = jobsummary.build(job())
    assert s.quoted == D("0") and s.invoiced == D("0")
    assert s.percent_billed is None
    assert not s.needs_explaining


def test_quoted_and_billed_add_up_across_vendors():
    s = jobsummary.build(job(
        quotes=[quote("100000.00"), quote("8000.00", vendor="Bergen Dumpster")],
        invoices=[invoice("40000.00"), invoice("4000.00", vendor="Bergen Dumpster")],
    ))
    assert s.quoted == D("108000.00")
    assert s.invoiced == D("44000.00")
    assert s.remaining == D("64000.00")
    assert s.percent_billed == 41
    assert len(s.vendors) == 2


def test_change_orders_raise_what_the_job_is_allowed_to_cost():
    """Otherwise every job with authorised extra scope flags, and a flag that
    fires on normal work is a flag people learn to click past."""
    s = jobsummary.build(job(
        quotes=[quote("100000.00")],
        change_orders=[ChangeOrder(job_id=1, vendor="New Castle Building Products",
                                   amount=D("15000.00"))],
        invoices=[invoice("112000.00")],
    ))
    assert s.authorised == D("115000.00")
    assert not s.needs_explaining
    assert s.remaining == D("3000.00")


def test_off_quote_spend_is_totalled_from_the_lines():
    s = jobsummary.build(job(
        quotes=[quote()],
        invoices=[invoice(lines=[
            line("GAFT3PG", "4820.00", VERDICT_MATCH),
            line("DRIPEDGE", "612.50", VERDICT_NOT_ON_QUOTE),
            line("FASCIA", "980.00", VERDICT_NOT_ON_QUOTE),
        ])],
    ))
    assert s.off_quote == D("1592.50")
    assert s.off_quote_lines == 2


def test_money_found_is_split_by_whether_it_was_actually_held():
    """Finding an overcharge is not the same as keeping the money, and one
    number for both would overstate what this system has done."""
    s = jobsummary.build(job(
        quotes=[quote()],
        invoices=[
            invoice("10000.00", over="450.00", status=APPROVAL_PENDING),
            invoice("10000.00", over="300.00", status=APPROVAL_PAID),
        ],
    ))
    assert s.found == D("750.00")
    assert s.still_held == D("450.00")
    assert s.approved_anyway == D("300.00")


def test_a_rejected_invoice_leaves_every_total():
    """Rejecting the superseded invoice is how a person fixes a double-bill,
    so it has to actually come out of the arithmetic."""
    keep = invoice("10000.00", over="100.00")
    dropped = invoice("9800.00", over="50.00", status=APPROVAL_REJECTED)
    s = jobsummary.build(job(quotes=[quote()], invoices=[keep, dropped]))

    assert s.invoiced == D("10000.00")
    assert s.found == D("100.00")
    assert s.invoice_count == 1


# --- the self-check -------------------------------------------------------

def test_billing_past_the_quote_is_flagged_even_when_no_invoice_is_wrong():
    """The whole reason this module exists. Two invoices, each priced exactly
    as quoted, adding to more than the job was ever quoted for."""
    s = jobsummary.build(job(
        quotes=[quote("18000.00")],
        invoices=[invoice("17182.90", over="0"), invoice("16900.00", over="0")],
    ))
    assert s.needs_explaining
    assert s.over_quote > D("0")
    assert s.found == D("0")          # nothing was individually wrong


def test_a_small_overrun_inside_tolerance_is_not_flagged():
    s = jobsummary.build(job(
        quotes=[quote("100000.00")],
        invoices=[invoice("100200.00")],
    ))
    assert not s.needs_explaining


def test_one_vendor_over_is_not_cancelled_out_by_another_under():
    """A job-wide figure would report nothing here, which is why the roll-up
    is per vendor."""
    s = jobsummary.build(job(
        quotes=[quote("50000.00"), quote("50000.00", vendor="Bergen Dumpster")],
        invoices=[invoice("70000.00"), invoice("10000.00", vendor="Bergen Dumpster")],
    ))
    assert s.needs_explaining
    assert s.over_quote > D("17000.00")
    assert s.remaining == D("20000.00")   # job-wide, this still looks fine


def test_a_job_with_no_quote_is_not_accused_of_overrunning():
    s = jobsummary.build(job(invoices=[invoice("40000.00")]))
    assert not s.needs_explaining
    assert not s.has_quote


# --- finding the reason ---------------------------------------------------

def test_the_same_invoice_twice_under_two_numbers():
    a = invoice("6154.00", number="INV-551900", on=date(2026, 9, 10))
    b = invoice("6154.00", number="551900-R", on=date(2026, 9, 14))
    s = jobsummary.build(job(quotes=[quote()], invoices=[a, b]))

    assert len(s.overlaps) == 1
    o = s.overlaps[0]
    assert o.identical_total
    assert o.days_apart == 4
    assert "INV-551900" in o.explanation and "551900-R" in o.explanation


def test_a_corrected_invoice_that_never_replaced_the_original():
    """Zack's case exactly. Different number, different total - the correction
    is the point - so only the line items give it away."""
    wrong = invoice("6154.00", number="INV-551900", on=date(2026, 9, 10), lines=[
        line("GAFT3PG", "4820.00"),
        line("GAFTP", "1400.00", VERDICT_OVER),
    ])
    fixed = invoice("6063.00", number="INV-552140", on=date(2026, 9, 18), lines=[
        line("GAFT3PG", "4820.00"),
        line("GAFTP", "1309.00"),
    ])
    s = jobsummary.build(job(quotes=[quote("6200.00")], invoices=[wrong, fixed]))

    assert s.needs_explaining
    assert len(s.overlaps) == 1
    o = s.overlaps[0]
    assert not o.identical_total
    assert o.shared_lines == 2
    # Capped at the smaller invoice: the lines add to more than either total
    # here, and a figure bigger than both would be indefensible.
    assert o.shared_value == D("6063.00")
    assert "corrected version" in o.explanation


def test_a_follow_on_delivery_of_different_material_stays_quiet():
    """Progress billing is normal. Two invoices from one vendor for different
    material is not a duplicate, and saying so would be noise."""
    first = invoice("4820.00", on=date(2026, 9, 10), lines=[line("GAFT3PG", "4820.00")])
    second = invoice("1309.00", on=date(2026, 9, 24), lines=[line("GAFTP", "1309.00")])
    s = jobsummary.build(job(quotes=[quote()], invoices=[first, second]))
    assert s.overlaps == []


def test_a_partial_overlap_below_the_threshold_stays_quiet():
    """One shared item on an otherwise different order is a re-order, not a
    duplicate."""
    first = invoice("10000.00", on=date(2026, 9, 10), lines=[
        line("GAFT3PG", "9000.00"), line("NAILS", "1000.00"),
    ])
    second = invoice("5000.00", on=date(2026, 9, 20), lines=[
        line("NAILS", "1000.00"), line("ICEWATER", "4000.00"),
    ])
    s = jobsummary.build(job(quotes=[quote()], invoices=[first, second]))
    assert s.overlaps == []


def test_invoices_months_apart_are_a_re_order_not_a_correction():
    a = invoice("6154.00", on=date(2026, 3, 1))
    b = invoice("6154.00", on=date(2026, 9, 10))
    s = jobsummary.build(job(quotes=[quote()], invoices=[a, b]))
    assert s.overlaps == []


def test_two_vendors_that_both_billed_the_same_amount_are_not_a_duplicate():
    a = invoice("2500.00", vendor="Bergen Dumpster")
    b = invoice("2500.00", vendor="New Castle Building Products")
    s = jobsummary.build(job(quotes=[quote()], invoices=[a, b]))
    assert s.overlaps == []


def test_the_same_vendor_under_two_trading_names_is_still_one_vendor():
    """New Castle signs quotes one way and invoices another. That is already
    true everywhere else in this system, and it has to hold here too."""
    a = invoice("6154.00", vendor="New Castle Building Products")
    b = invoice("6154.00", vendor="NEW CASTLE BLDG PRODUCTS")
    s = jobsummary.build(job(quotes=[quote()], invoices=[a, b]))
    assert len(s.vendors) == 1
    assert len(s.overlaps) == 1


def test_a_rejected_duplicate_stops_being_reported():
    a = invoice("6154.00", number="INV-1")
    b = invoice("6154.00", number="INV-2", status=APPROVAL_REJECTED)
    s = jobsummary.build(job(quotes=[quote()], invoices=[a, b]))
    assert s.overlaps == []


def test_lines_with_no_sku_match_on_their_description():
    """Plenty of vendors bill labour and freight with no part number at all."""
    a = invoice("2000.00", on=date(2026, 9, 10), lines=[
        line("", "2000.00", desc="Tear off and dispose existing roof"),
    ])
    b = invoice("2200.00", on=date(2026, 9, 16), lines=[
        line("", "2200.00", desc="TEAR OFF AND DISPOSE EXISTING ROOF"),
    ])
    s = jobsummary.build(job(quotes=[quote()], invoices=[a, b]))
    assert len(s.overlaps) == 1
    assert s.overlaps[0].shared_value == D("2000.00")


def test_zero_totals_are_not_treated_as_matching_each_other():
    a = invoice("0.00", lines=[])
    b = invoice("0.00", lines=[])
    s = jobsummary.build(job(quotes=[quote()], invoices=[a, b]))
    assert s.overlaps == []


def test_three_duplicates_report_every_pair_biggest_first():
    a = invoice("1000.00", on=date(2026, 9, 1))
    b = invoice("1000.00", on=date(2026, 9, 3))
    c = invoice("5000.00", on=date(2026, 9, 5), lines=[line("X", "5000.00")])
    d = invoice("5000.00", on=date(2026, 9, 7), lines=[line("X", "5000.00")])
    s = jobsummary.build(job(quotes=[quote()], invoices=[a, b, c, d]))

    assert len(s.overlaps) == 2
    assert s.overlaps[0].shared_value == D("5000.00")   # biggest first

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
    CO_APPROVED,
    CO_PROPOSED,
    VERDICT_MATCH,
    VERDICT_NOT_ON_QUOTE,
    VERDICT_OVER,
    ChangeOrder,
    Invoice,
    InvoiceLine,
    Job,
    Quote,
)

ZERO_D = D("0")
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


def co(amount, vendor="New Castle Building Products", status=CO_APPROVED,
       number="CO-1"):
    # status passed explicitly: a column default lands at INSERT, not on a
    # transient object, and `is_live` fails closed.
    return ChangeOrder(job_id=1, vendor=vendor, number=number, amount=D(amount),
                       status=status, approved_by="Zack")


def _unquoted(total, vendor):
    """An invoice from a supplier who has no quote on this job at all: every
    line reads unmatched because there was nothing to match against."""
    inv = invoice(total, vendor=vendor, lines=[
        line("MISC", total, VERDICT_NOT_ON_QUOTE),
    ])
    inv.quote_id = None
    return inv


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
        change_orders=[co("15000.00")],
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

def test_using_more_material_than_quoted_is_not_a_problem():
    """Zack's correction, and it matters more than anything else in this file.

    A quote prices material. It does not cap how much of it a roof turns out to
    need. Two invoices at exactly the quoted prices, adding to nearly twice the
    quote, is an ordinary job where the crew used more squares than somebody
    estimated in an office - not an overbilling. Flagging it would put a red
    panel on most jobs, and a warning that fires on normal work is a warning
    people learn to click past."""
    s = jobsummary.build(job(
        quotes=[quote("18000.00")],
        invoices=[invoice("17182.90", over="0"), invoice("16900.00", over="0")],
    ))
    assert s.over_quote > D("0")       # the fact is still reported
    assert s.over_but_accounted_for    # and it is accounted for
    assert not s.needs_explaining      # so nothing is raised


def test_but_material_nobody_quoted_is():
    """The same overage, with spend on items that appear on no quote."""
    s = jobsummary.build(job(
        quotes=[quote("18000.00")],
        invoices=[
            invoice("17182.90", over="0"),
            invoice("16900.00", over="0", lines=[
                line("SKYLIGHT", "9000.00", VERDICT_NOT_ON_QUOTE),
            ]),
        ],
    ))
    assert s.needs_explaining
    assert s.unexplained == D("9000.00")


def test_and_so_is_a_price_that_moved():
    s = jobsummary.build(job(
        quotes=[quote("18000.00")],
        invoices=[invoice("17182.90"), invoice("16900.00", over="1400.00")],
    ))
    assert s.needs_explaining
    assert s.unexplained == D("1400.00")


def test_a_small_amount_of_unexplained_spend_rides_inside_tolerance():
    s = jobsummary.build(job(
        quotes=[quote("100000.00")],
        invoices=[invoice("120000.00", over="0", lines=[
            line("MISC", "40.00", VERDICT_NOT_ON_QUOTE),
        ])],
    ))
    assert not s.needs_explaining
    assert s.over_but_accounted_for


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


def test_two_deliveries_of_the_same_shingle_are_not_a_duplicate():
    """186 squares quoted, delivered as two loads of 93. Both invoices are
    mostly shingle, so by value alone most of the smaller one reappears on the
    larger - and this is the most ordinary thing that happens on a big roof."""
    first = invoice("13024.00", on=date(2026, 9, 10), lines=[
        line("SHG-TL-WW", "11290.00"), line("UND-SYN-10", "925.00"),
    ])
    second = invoice("16439.00", on=date(2026, 9, 18), lines=[
        line("SHG-TL-WW", "11290.00"), line("IWS-225", "1888.00"),
        line("DE-10-WHT", "1542.00"),
    ])
    s = jobsummary.build(job(quotes=[quote()], invoices=[first, second]))
    assert s.overlaps == []


def test_a_progress_billing_split_across_two_draws_is_not_a_duplicate():
    """A sub bills 60% of a line one month and the last 40% the next. The
    shared line is most of the smaller draw, and nothing is wrong."""
    draw2 = invoice("36600.00", on=date(2026, 9, 5), lines=[
        line("", "9000.00", desc="Dry-in: underlayment and ice & water"),
        line("", "27600.00", desc="Shingle installation"),
    ])
    draw3 = invoice("23300.00", on=date(2026, 9, 25), lines=[
        line("", "18400.00", desc="Shingle installation"),
        line("", "4900.00", desc="Flashing, vents and detail work"),
    ])
    s = jobsummary.build(job(quotes=[quote()], invoices=[draw2, draw3]))
    assert s.overlaps == []


def test_but_a_reissue_that_bills_every_item_again_is_still_caught():
    """The guard above must not cost us the case it was written around: the
    corrected invoice re-bills the whole of the original."""
    wrong = invoice("8903.00", on=date(2026, 9, 10), lines=[
        line("SHG-OC-DW", "7005.00"), line("UND-DK-10", "616.00"),
        line("IWS-OC-200", "729.00"),
    ])
    fixed = invoice("8944.00", on=date(2026, 9, 19), lines=[
        line("SHG-OC-DW", "7005.00"), line("UND-DK-10", "616.00"),
        line("IWS-OC-200", "768.00"),
    ])
    s = jobsummary.build(job(quotes=[quote()], invoices=[wrong, fixed]))
    assert len(s.overlaps) == 1
    assert not s.overlaps[0].identical_total


def test_a_resend_under_a_new_number_is_caught_however_the_lines_read():
    """Identical totals are their own evidence and are not asked to prove
    anything about line items."""
    a = invoice("6154.00", number="INV-551900", on=date(2026, 9, 10), lines=[
        line("GAFT3PG", "6154.00"),
    ])
    b = invoice("6154.00", number="551900-R", on=date(2026, 9, 14), lines=[
        line("GAFT3PG", "6154.00"), line("FREIGHT", "0.00"),
    ])
    s = jobsummary.build(job(quotes=[quote()], invoices=[a, b]))
    assert len(s.overlaps) == 1
    assert s.overlaps[0].identical_total


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


# --- change orders only count once somebody signs them --------------------

def test_a_proposed_change_order_raises_nothing():
    """The whole safety property. A change order read off a vendor's own email
    must not quietly close the gap that brings a person to look at the job."""
    s = jobsummary.build(job(
        quotes=[quote("100000.00")],
        change_orders=[co("15000.00", status=CO_PROPOSED)],
        invoices=[invoice("112000.00")],
    ))
    assert s.change_orders == D("0")
    assert s.authorised == D("100000.00")
    assert s.over_quote > D("0")              # still over, and still says so
    assert s.proposed_total == D("15000.00")  # and the paperwork is visible
    assert len(s.proposed_change_orders) == 1


def test_approving_it_is_what_closes_the_gap():
    approved = jobsummary.build(job(
        quotes=[quote("100000.00")],
        change_orders=[co("15000.00", status=CO_APPROVED)],
        invoices=[invoice("112000.00")],
    ))
    assert approved.change_orders == D("15000.00")
    assert approved.over_quote == D("0")      # the ceiling moved to cover it
    assert approved.proposed_change_orders == []


def test_a_rejected_change_order_authorises_nothing_and_is_not_pending():
    s = jobsummary.build(job(
        quotes=[quote("100000.00")],
        change_orders=[co("15000.00", status="rejected")],
        invoices=[invoice("112000.00")],
    ))
    assert s.change_orders == D("0")
    assert s.over_quote > D("0")
    assert s.proposed_change_orders == []     # decided, not waiting


# --- a different supplier, with no quote of their own ----------------------

def test_a_supplier_with_no_quote_here_is_not_an_off_quote_discrepancy():
    """Zack's case: ABC quoted the roof, and a couple of things got picked up
    at New Castle because that is where they were in stock. There was never
    going to be a New Castle quote, and their invoice must not be measured
    against ABC's."""
    s = jobsummary.build(job(
        quotes=[quote("100000.00", vendor="ABC Supply Co")],
        invoices=[
            invoice("40000.00", vendor="ABC Supply Co"),
            _unquoted("842.50", vendor="New Castle Building Products"),
        ],
    ))

    assert s.unquoted_supplier == D("842.50")
    assert s.unquoted_vendors == ["New Castle Building Products"]
    assert s.off_quote == ZERO_D          # nothing was slipped onto ABC's bill
    assert s.unexplained == ZERO_D
    assert not s.needs_explaining
    assert s.invoiced == D("40842.50")    # still counted as spend


def test_an_item_the_quoted_supplier_slipped_in_is_still_a_discrepancy():
    """The distinction that makes the previous test safe rather than blind."""
    s = jobsummary.build(job(
        quotes=[quote("100000.00", vendor="ABC Supply Co")],
        invoices=[invoice("40000.00", vendor="ABC Supply Co", lines=[
            line("SKYLIGHT", "9000.00", VERDICT_NOT_ON_QUOTE),
        ])],
    ))
    assert s.off_quote == D("9000.00")
    assert s.unquoted_supplier == ZERO_D


def test_several_stragglers_from_one_supplier_name_them_once():
    s = jobsummary.build(job(
        quotes=[quote("100000.00", vendor="ABC Supply Co")],
        invoices=[
            _unquoted("300.00", vendor="New Castle Building Products"),
            _unquoted("542.50", vendor="NEW CASTLE BLDG PRODUCTS"),
            _unquoted("120.00", vendor="Bergen Fasteners"),
        ],
    ))
    assert s.unquoted_supplier == D("962.50")
    assert len(s.unquoted_vendors) == 2

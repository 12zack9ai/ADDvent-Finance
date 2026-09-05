"""Job costing: what it cost, what it made, and what the report is not saying.

The number this produces is one somebody will quote in a meeting, so the tests
worth writing are about honesty rather than addition. A margin figure that
quietly omits the crew, or a stack of uncaptured receipts, or £40k sitting
unapproved in the queue, is worse than no margin figure at all - it is wrong
and it looks finished.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import costing  # noqa: E402
from app.models import (
    Purchase,  # noqa: E402
    APPROVAL_APPROVED,
    APPROVAL_HELD,
    APPROVAL_PAID,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    CHECK_APPROVED,
    CHECK_PAID,
    CHECK_REJECTED,
    CHECK_REQUESTED,
    CheckRequest,
    Invoice,
    Job,
    Quote,
)

_ids = {"n": 0}


def _next() -> int:
    _ids["n"] += 1
    return _ids["n"]


def job(invoices=(), checks=(), charged=None, labour=None, contracts=(),
        purchases=(), outcome="active"):
    j = Job(job_number="260000", name="Daul Gardens")
    j.invoices = list(invoices)
    j.check_requests = list(checks)
    j.quotes = list(contracts)
    j.change_orders = []
    j.purchases = list(purchases)
    j.outcome = outcome
    j.contract_amount = D(charged) if charged is not None else None
    j.labour_cost = D(labour) if labour is not None else None
    return j


def purchase(total="84.12", merchant="Home Depot"):
    return Purchase(job_id=1, merchant=merchant, total=D(total))


def invoice(total="10000.00", status=APPROVAL_APPROVED, vendor="New Castle"):
    inv = Invoice(job_id=1, document_id=_next(), vendor=vendor,
                  invoice_number=f"INV-{_next()}", total=D(total),
                  approval_status=status)
    inv.id = _next()
    inv.created_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    inv.lines = []
    return inv


def subcontract(total="120000.00", vendor="Reilly Roofing"):
    q = Quote(job_id=1, document_id=_next(), vendor=vendor, is_master=True,
              is_subcontract=True, total=D(total))
    q.id = _next()
    q.lines = []
    return q


def check(amount="450.00", status=CHECK_APPROVED):
    r = CheckRequest(job_id=1, vendor="Township of Oakland", amount=D(amount),
                     status=status, purpose="permit")
    r.id = _next()
    r.received_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    return r


# --- adding the job up ----------------------------------------------------

def test_a_fully_subbed_job_needs_nothing_typed_but_the_price():
    """Zack's case: fully subbed means no labour figure, and the report is
    complete without anyone doing anything."""
    j = job(contracts=[subcontract()],
            invoices=[invoice("12000.00", APPROVAL_PAID),
                      invoice("90000.00", APPROVAL_PAID, vendor="Reilly Roofing")],
            charged="150000.00", labour="0")
    c = costing.build(j)

    assert c.cost == D("102000.00")
    assert c.revenue == D("150000.00")
    assert c.margin == D("48000.00")
    assert c.margin_pct == D("32.0")


def test_material_and_subcontract_are_split_by_who_sent_it():
    """Both are invoices through the same pipeline. What separates them is
    whether that vendor holds a subcontract on this job."""
    c = costing.build(job(
        contracts=[subcontract()],
        invoices=[
            invoice("6000.00"), invoice("4000.00", APPROVAL_PAID),
            invoice("30000.00", vendor="Reilly Roofing"),
            invoice("20000.00", APPROVAL_PAID, vendor="REILLY ROOFING LLC"),
        ],
    ))
    assert c.bucket("material").agreed == D("10000.00")
    assert c.bucket("material").count == 2
    assert c.bucket("subcontract").agreed == D("50000.00")
    assert c.bucket("subcontract").count == 2


def test_a_permit_is_a_cost_that_appears_nowhere_else():
    """No invoice, no quote, no contract - and real money out on the job."""
    c = costing.build(job(checks=[check("450.00", CHECK_APPROVED)]))
    assert c.bucket("checks").agreed == D("450.00")
    assert c.cost == D("450.00")


def test_a_rejected_invoice_is_not_a_cost():
    c = costing.build(job(invoices=[
        invoice("10000.00", APPROVAL_APPROVED),
        invoice("9000.00", APPROVAL_REJECTED),
    ]))
    assert c.bucket("material").agreed == D("10000.00")
    assert c.bucket("material").count == 1


def test_a_refused_check_request_is_not_a_cost():
    c = costing.build(job(checks=[
        check("450.00", CHECK_APPROVED),
        check("300.00", CHECK_REJECTED),
    ]))
    assert c.bucket("checks").agreed == D("450.00")
    assert c.bucket("checks").pending == D("0")


# --- what is agreed versus what is claimed --------------------------------

@pytest.mark.parametrize("status", [APPROVAL_PENDING, APPROVAL_HELD])
def test_an_unapproved_invoice_is_a_claim_not_a_cost(status):
    """It may well be wrong - that is the entire point of this system - so it
    does not go into the cost. It is not hidden either."""
    c = costing.build(job(invoices=[invoice("8000.00", status)]))
    assert c.cost == D("0")
    assert c.pending == D("8000.00")
    assert c.worst_case_cost == D("8000.00")


def test_a_live_job_shows_both_numbers_rather_than_looking_profitable():
    """The exact way a job looks profitable right up until the last invoices
    clear is a report that ignores the queue."""
    c = costing.build(job(
        invoices=[invoice("20000.00", APPROVAL_PAID),
                  invoice("40000.00", APPROVAL_PENDING)],
        checks=[check("25000.00", CHECK_REQUESTED)],  # a big deposit, undecided
        charged="100000.00", labour="0",
    ))
    assert c.margin == D("80000.00")            # on what is agreed
    assert c.worst_case_margin == D("15000.00")  # if it all clears
    assert "still waiting on a decision" in " ".join(c.gaps)


# --- the honesty of the report --------------------------------------------

def test_a_report_with_no_price_entered_refuses_to_show_a_margin():
    c = costing.build(job(invoices=[invoice("10000.00")], labour="0"))
    assert not c.has_revenue
    assert c.margin_pct is None
    assert "what we billed" in " ".join(c.gaps)


def test_blank_labour_is_not_the_same_as_zero_labour():
    """Zero on a fully subbed job is a real answer. Blank means nobody has
    said, and the report has to know the difference."""
    unstated = costing.build(job(invoices=[invoice("10000.00")], charged="20000.00"))
    assert not unstated.labour_given
    assert "hours and cost" in " ".join(unstated.gaps)

    stated = costing.build(job(invoices=[invoice("10000.00")],
                               charged="20000.00", labour="0"))
    assert stated.labour_given
    assert "hours and cost" not in " ".join(stated.gaps)


def test_labour_is_part_of_the_cost_when_it_is_given():
    c = costing.build(job(invoices=[invoice("10000.00")],
                          charged="50000.00", labour="14000.00"))
    assert c.cost == D("24000.00")
    assert c.margin == D("26000.00")


def test_a_job_with_no_receipts_says_so_rather_than_omitting_the_category():
    """Leaving it out entirely would let the report read as complete when
    nobody has sent a receipt in."""
    c = costing.build(job(invoices=[invoice("10000.00")],
                          charged="50000.00", labour="0"))
    bucket = c.bucket("purchases")

    assert bucket is not None and bucket.agreed == D("0")
    assert not c.purchases_captured
    assert any("receipts" in gap for gap in c.gaps)
    assert not c.is_complete


def test_receipts_are_cost_the_moment_they_arrive():
    """Paid at the till before anybody here saw them. Nothing to approve and
    nothing pending - every one of them is cost."""
    c = costing.build(job(invoices=[invoice("10000.00")], charged="50000.00",
                          labour="0", purchases=[purchase("84.12"),
                                                 purchase("212.44", "Lowe's")]))
    bucket = c.bucket("purchases")

    assert bucket.agreed == D("296.56")
    assert bucket.pending == D("0")
    assert bucket.count == 2
    assert c.cost == D("10296.56")
    assert c.purchases_captured
    assert not any("receipts" in gap for gap in c.gaps)


def test_a_job_we_did_not_get_says_the_spend_is_a_loss():
    """Zack: "sometimes we spend money on jobs we don't get so this will turn
    out to be a loss." No document can say that - only the outcome can."""
    c = costing.build(job(purchases=[purchase("340.00")], outcome="lost",
                          labour="0"))
    assert c.cost == D("340.00")
    assert any("did not get this job" in gap for gap in c.gaps)


def test_an_empty_job_costs_nothing_and_says_everything_is_missing():
    c = costing.build(job())
    assert c.cost == D("0")
    assert not c.has_revenue
    assert len(c.gaps) >= 3


def test_a_job_that_lost_money_says_so():
    c = costing.build(job(
        invoices=[invoice("60000.00", APPROVAL_PAID)],
        checks=[check("70000.00", CHECK_PAID)],
        charged="100000.00", labour="0",
    ))
    assert c.margin == D("-30000.00")
    assert c.margin_pct == D("-30.0")


# --- what QuickBooks owns, and what only a person can answer ---------------

def test_collected_is_reported_and_is_not_the_same_as_billed():
    """Zack: "job costing should also be able to pull total billed and
    collected out of QuickBooks." Two different numbers - billing a roof and
    being paid for it are weeks apart."""
    j = job(invoices=[invoice("10000.00")], charged="50000.00", labour="0")
    j.collected_amount = D("32000.00")
    c = costing.build(j)

    assert c.billed == D("50000.00")
    assert c.collected == D("32000.00")
    assert c.outstanding == D("18000.00")
    assert c.collected_known


def test_not_knowing_what_was_collected_is_said_rather_than_shown_as_zero():
    c = costing.build(job(invoices=[invoice("10000.00")], charged="50000.00", labour="0"))
    assert not c.collected_known
    assert c.collected == D("0")
    assert "how much has been collected" in " ".join(c.gaps)


def test_a_hand_typed_billing_figure_says_it_was_hand_typed():
    """It will be stale the moment somebody bills again, and a number nobody
    can trace is worse than no number."""
    j = job(invoices=[invoice("10000.00")], charged="50000.00", labour="0")
    j.billing_source = "manual"
    c = costing.build(j)
    assert not c.billing_synced
    assert "typed by hand" in " ".join(c.gaps)

    j.billing_source = "quickbooks"
    assert costing.build(j).billing_synced
    assert "typed by hand" not in " ".join(costing.build(j).gaps)


def test_the_crews_rate_is_derived_so_a_foreman_can_check_it():
    j = job(invoices=[invoice("10000.00")], charged="50000.00", labour="41500.00")
    j.labour_hours = D("1000")
    c = costing.build(j)

    assert c.labour_hours == D("1000")
    assert c.labour_rate == D("41.50")


def test_a_crew_cost_with_no_hours_behind_it_is_flagged():
    """Not wrong, but nobody can check it, and this report exists to be
    checkable."""
    c = costing.build(job(invoices=[invoice("10000.00")],
                          charged="50000.00", labour="41500.00"))
    assert c.labour_rate is None
    assert "nobody can check the rate" in " ".join(c.gaps)


def test_a_fully_subbed_job_needs_no_crew_figure_at_all():
    """Zack: "when it isn't fully subbed out." On one that was, blank is the
    correct and complete answer."""
    c = costing.build(job(invoices=[invoice("10000.00")],
                          charged="50000.00", labour="0"))
    assert c.labour_given and c.labour == D("0")
    assert "hours and cost have not been entered" not in " ".join(c.gaps)


# --- numbers that are not numbers -----------------------------------------

def test_a_value_arithmetic_cannot_be_trusted_with_never_gets_in():
    """NaN is the dangerous one, and Decimal("nan") is a string a form field
    or a badly scanned document can produce. Every comparison against it is
    False, so an invoice total of NaN is not over the quote, not over
    tolerance, not over the contract ceiling and not greater than zero - it
    passes every check in this system in silence."""
    from app.db import to_decimal

    for hostile in ("nan", "NaN", "snan", "Infinity", "-Infinity", "1e999", "9" * 40):
        assert to_decimal(hostile) is None, hostile

    # And the ordinary shapes still work.
    assert to_decimal("$4,182.60") == D("4182.60")
    assert to_decimal("(1,200.50)") == D("-1200.50")
    assert to_decimal("999999999999.99") == D("999999999999.99")


def test_an_unstorable_number_is_refused_rather_than_taking_the_page_down():
    """A column type is the wrong place to raise. to_decimal screens these at
    the door; this is what happens if one is built somewhere else."""
    from decimal import Decimal as Dec
    from app.db import Money

    assert Money().process_bind_param(Dec("1e999"), None) is None
    assert Money().process_bind_param(Dec("NaN"), None) is None
    assert Money().process_bind_param(Dec("4182.60"), None) == "4182.6000"

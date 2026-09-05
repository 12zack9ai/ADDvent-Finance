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
from app.models import (  # noqa: E402
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
)

_ids = {"n": 0}


def _next() -> int:
    _ids["n"] += 1
    return _ids["n"]


def job(invoices=(), checks=(), charged=None, labour=None):
    j = Job(job_number="260000", name="Daul Gardens")
    j.invoices = list(invoices)
    j.check_requests = list(checks)
    j.quotes = []
    j.change_orders = []
    j.contract_amount = D(charged) if charged is not None else None
    j.labour_cost = D(labour) if labour is not None else None
    return j


def invoice(total="10000.00", status=APPROVAL_APPROVED):
    inv = Invoice(job_id=1, document_id=_next(), vendor="New Castle",
                  invoice_number=f"INV-{_next()}", total=D(total),
                  approval_status=status)
    inv.id = _next()
    return inv


def check(amount="30000.00", status=CHECK_APPROVED):
    r = CheckRequest(job_id=1, vendor="Reilly Roofing", amount=D(amount), status=status)
    r.id = _next()
    r.received_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    return r


# --- adding the job up ----------------------------------------------------

def test_a_fully_subbed_job_needs_nothing_typed_but_the_price():
    """Zack's case: fully subbed means no labour figure, and the report is
    complete without anyone doing anything."""
    j = job(invoices=[invoice("12000.00", APPROVAL_PAID)],
            checks=[check("90000.00", CHECK_PAID)],
            charged="150000.00", labour="0")
    c = costing.build(j)

    assert c.cost == D("102000.00")
    assert c.revenue == D("150000.00")
    assert c.margin == D("48000.00")
    assert c.margin_pct == D("32.0")


def test_material_and_subcontract_are_counted_separately():
    c = costing.build(job(
        invoices=[invoice("6000.00"), invoice("4000.00", APPROVAL_PAID)],
        checks=[check("30000.00"), check("20000.00", CHECK_PAID)],
    ))
    assert c.bucket("material").agreed == D("10000.00")
    assert c.bucket("material").count == 2
    assert c.bucket("subcontract").agreed == D("50000.00")


def test_a_rejected_invoice_is_not_a_cost():
    c = costing.build(job(invoices=[
        invoice("10000.00", APPROVAL_APPROVED),
        invoice("9000.00", APPROVAL_REJECTED),
    ]))
    assert c.bucket("material").agreed == D("10000.00")
    assert c.bucket("material").count == 1


def test_a_refused_check_request_is_not_a_cost():
    c = costing.build(job(checks=[
        check("30000.00", CHECK_APPROVED),
        check("15000.00", CHECK_REJECTED),
    ]))
    assert c.bucket("subcontract").agreed == D("30000.00")
    assert c.bucket("subcontract").pending == D("0")


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
        checks=[check("25000.00", CHECK_REQUESTED)],
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
    assert "what we charged" in " ".join(c.gaps)


def test_blank_labour_is_not_the_same_as_zero_labour():
    """Zero on a fully subbed job is a real answer. Blank means nobody has
    said, and the report has to know the difference."""
    unstated = costing.build(job(invoices=[invoice("10000.00")], charged="20000.00"))
    assert not unstated.labour_given
    assert "our own labour has not been entered" in " ".join(unstated.gaps)

    stated = costing.build(job(invoices=[invoice("10000.00")],
                               charged="20000.00", labour="0"))
    assert stated.labour_given
    assert "our own labour has not been entered" not in " ".join(stated.gaps)


def test_labour_is_part_of_the_cost_when_it_is_given():
    c = costing.build(job(invoices=[invoice("10000.00")],
                          charged="50000.00", labour="14000.00"))
    assert c.cost == D("24000.00")
    assert c.margin == D("26000.00")


def test_uncaptured_receipts_are_named_rather_than_omitted():
    """The category exists and is empty and says so. Leaving it out entirely
    would let the report read as complete when it is not."""
    c = costing.build(job(invoices=[invoice("10000.00")],
                          charged="50000.00", labour="0"))
    purchases = c.bucket("purchases")

    assert purchases is not None
    assert purchases.agreed == D("0")
    assert "Not captured yet" in purchases.note
    assert not c.purchases_captured
    assert any("receipts" in gap for gap in c.gaps)
    assert not c.is_complete


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

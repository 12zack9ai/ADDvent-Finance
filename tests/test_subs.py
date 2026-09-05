"""Subcontractors: their contract, and how much of it they have billed.

A sub's invoice goes through the vendor pipeline unchanged - read, matched
line by line against their subcontract, marked up, approved. What is tested
here is the single thing a subcontract adds on top: it is a **ceiling**.

A quote prices material and does not cap how much of it a roof needs. A
contract is a fixed award. Six invoices at $20,000 each fit inside a $120,000
contract and every one of them is individually correct; the seventh is the
first thing anyone could object to, and only the running total sees it.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import subs  # noqa: E402
from app.models import (  # noqa: E402
    APPROVAL_APPROVED,
    APPROVAL_HELD,
    APPROVAL_PAID,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    ChangeOrder,
    Invoice,
    Job,
    Quote,
)

ZERO = D("0")
_ids = {"n": 0}


def _next() -> int:
    _ids["n"] += 1
    return _ids["n"]


def job(contracts=(), invoices=(), change_orders=(), quotes=()):
    j = Job(job_number="260000", name="Daul Gardens")
    j.quotes = list(contracts) + list(quotes)
    j.invoices = list(invoices)
    j.change_orders = list(change_orders)
    j.check_requests = []
    return j


def contract(total="120000.00", vendor="Reilly Roofing LLC"):
    q = Quote(job_id=1, document_id=_next(), vendor=vendor, is_master=True,
              is_subcontract=True, total=D(total))
    q.id = _next()
    q.lines = []
    return q


def material(total="17182.90", vendor="New Castle Building Products"):
    q = Quote(job_id=1, document_id=_next(), vendor=vendor, is_master=True,
              is_subcontract=False, total=D(total))
    q.id = _next()
    q.lines = []
    return q


def invoice(total="30000.00", vendor="Reilly Roofing LLC",
            status=APPROVAL_APPROVED, on=None):
    inv = Invoice(job_id=1, document_id=_next(), vendor=vendor,
                  invoice_number=f"REQ-{_next()}", total=D(total),
                  approval_status=status, invoice_date=on or date(2026, 9, 1))
    inv.id = _next()
    inv.created_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    inv.lines = []
    return inv


def co(amount, vendor="Reilly Roofing LLC"):
    return ChangeOrder(job_id=1, vendor=vendor, amount=D(amount),
                       status="approved", approved_by="Zack")


# --- where a sub stands ---------------------------------------------------

def test_a_subs_position_is_the_running_total_of_their_invoices():
    j = job(contracts=[contract("120000.00")], invoices=[
        invoice("30000.00", status=APPROVAL_PAID),
        invoice("30000.00", status=APPROVAL_APPROVED),
        invoice("30000.00", status=APPROVAL_PENDING),
    ])
    p = subs.positions(j)[0]

    assert p.contract == D("120000.00")
    assert p.billed == D("60000.00")      # approved and paid both count
    assert p.paid == D("30000.00")
    assert p.pending == D("30000.00")     # under review, not yet owed
    assert p.remaining == D("60000.00")
    assert p.committed == D("90000.00")
    assert p.percent_drawn == 50


def test_a_rejected_invoice_counts_for_nothing():
    j = job(contracts=[contract("120000.00")],
            invoices=[invoice("30000.00", status=APPROVAL_REJECTED)])
    p = subs.positions(j)[0]
    assert p.billed == ZERO and p.pending == ZERO
    assert p.invoices == []


def test_extras_raise_what_a_sub_may_draw():
    j = job(contracts=[contract("120000.00")], change_orders=[co("18000.00")],
            invoices=[invoice("130000.00")])
    p = subs.positions(j)[0]

    assert p.awarded == D("138000.00")
    assert p.overage == ZERO
    assert p.remaining == D("8000.00")


def test_a_change_order_for_a_material_vendor_does_not_raise_a_subs_ceiling():
    j = job(contracts=[contract("120000.00")],
            change_orders=[co("18000.00", vendor="New Castle Building Products")])
    p = subs.positions(j)[0]
    assert p.change_orders == ZERO
    assert p.awarded == D("120000.00")


def test_only_vendors_holding_a_subcontract_are_subcontractors():
    """A supply house with a material quote is not a sub, even though their
    invoices go through the identical pipeline."""
    j = job(contracts=[contract("120000.00")], quotes=[material()],
            invoices=[invoice("6154.00", vendor="New Castle Building Products")])
    positions = subs.positions(j)

    assert len(positions) == 1
    assert positions[0].vendor == "Reilly Roofing LLC"
    assert positions[0].invoices == []          # the material invoice is not theirs
    assert not subs.is_subcontractor(j, "New Castle Building Products")
    assert subs.is_subcontractor(j, "Reilly Roofing LLC")


def test_a_job_with_no_subcontracts_has_no_subcontractors():
    assert subs.positions(job(quotes=[material()], invoices=[invoice()])) == []


def test_a_sub_who_signs_two_ways_is_one_sub():
    j = job(contracts=[contract("120000.00", vendor="Reilly Roofing LLC")],
            invoices=[invoice("30000.00", vendor="REILLY ROOFING")])
    positions = subs.positions(j)
    assert len(positions) == 1
    assert positions[0].billed == D("30000.00")


# --- the ceiling ----------------------------------------------------------

def test_the_seventh_invoice_is_the_one_that_gets_caught():
    """Six at $20,000 fit inside a $120,000 contract. Each was individually
    correct. Only the running total sees the seventh."""
    approved = [invoice("20000.00") for _ in range(6)]
    seventh = invoice("20000.00", status=APPROVAL_PENDING)
    j = job(contracts=[contract("120000.00")], invoices=approved + [seventh])

    check = subs.contract_check(j, seventh)
    assert check is not None
    assert check.over_contract
    assert check.exceeds_by == D("20000.00")
    assert "past it" in check.message


def test_an_invoice_inside_the_contract_says_what_is_left():
    later = invoice("30000.00", status=APPROVAL_PENDING)
    j = job(contracts=[contract("120000.00")], invoices=[invoice("30000.00"), later])

    check = subs.contract_check(j, later)
    assert not check.over_contract
    assert "$60,000.00 on the contract" in check.message


def test_an_invoice_landing_exactly_on_the_contract_is_fine():
    last = invoice("60000.00", status=APPROVAL_PENDING)
    j = job(contracts=[contract("120000.00")], invoices=[invoice("60000.00"), last])
    assert not subs.contract_check(j, last).over_contract


def test_extras_are_what_make_an_overage_acceptable():
    seventh = invoice("20000.00", status=APPROVAL_PENDING)
    approved = [invoice("20000.00") for _ in range(6)]
    j = job(contracts=[contract("120000.00")], change_orders=[co("25000.00")],
            invoices=approved + [seventh])
    assert not subs.contract_check(j, seventh).over_contract


def test_two_invoices_under_review_are_not_refused_on_each_others_account():
    """Two claims are two claims. Counting one against the other would refuse
    both on the strength of money nobody has agreed to yet."""
    a = invoice("70000.00", status=APPROVAL_PENDING)
    b = invoice("70000.00", status=APPROVAL_PENDING)
    j = job(contracts=[contract("120000.00")], invoices=[a, b])

    assert not subs.contract_check(j, a).over_contract
    assert not subs.contract_check(j, b).over_contract


def test_re_checking_an_already_approved_invoice_does_not_count_it_twice():
    approved = invoice("130000.00", status=APPROVAL_APPROVED)
    j = job(contracts=[contract("120000.00")], invoices=[approved])

    check = subs.contract_check(j, approved)
    assert check.billed_after == D("130000.00")
    assert check.exceeds_by == D("10000.00")


def test_a_material_supplier_gets_no_ceiling_check_at_all():
    """A quote prices material; it does not cap how much of it the roof needs."""
    material_invoice = invoice("250000.00", vendor="New Castle Building Products")
    j = job(contracts=[contract("120000.00")], quotes=[material()],
            invoices=[material_invoice])
    assert subs.contract_check(j, material_invoice) is None


def test_a_sub_with_no_contract_value_gets_no_ceiling_check():
    j = job(contracts=[contract("0")], invoices=[invoice("30000.00")])
    assert subs.contract_check(j, j.invoices[0]) is None


def test_the_invoice_that_would_go_past_the_award_is_identifiable():
    """So a list of a sub's invoices can say which one is the problem, rather
    than only showing a total that has already gone wrong."""
    j = job(contracts=[contract("120000.00")], invoices=[
        invoice("36000.00", status=APPROVAL_PAID),
        invoice("30000.00", status=APPROVAL_APPROVED),
        invoice("60000.00", status=APPROVAL_PENDING),
    ])
    p = subs.positions(j)[0]
    paid, approved, waiting = p.invoices

    assert p.would_exceed_with(waiting) == D("6000.00")
    assert p.would_exceed_with(paid) == ZERO
    assert p.would_exceed_with(approved) == ZERO

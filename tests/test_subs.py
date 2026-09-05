"""Subcontractor check requests.

Two things are being tested, and they are the two things this programme exists
for.

**The cumulative check.** A sub bills against one agreed number, not a list of
prices, so there is nothing to price-check. Any single request can look
perfectly reasonable and the seventh still takes them past their contract. Only
the running total sees that.

**The queue order.** Longest wait first, and nothing else - because the person
reading it is being telephoned by a subcontractor asking where their money is.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import subs  # noqa: E402
from app.models import (  # noqa: E402
    CHECK_APPROVED,
    CHECK_PAID,
    CHECK_REJECTED,
    CHECK_REQUESTED,
    ChangeOrder,
    CheckRequest,
    Job,
    Quote,
)

TODAY = date(2026, 9, 5)
_ids = {"n": 0}


def _next() -> int:
    _ids["n"] += 1
    return _ids["n"]


def job(contracts=(), requests=(), change_orders=(), quotes=()):
    j = Job(job_number="260000", name="Daul Gardens")
    j.quotes = list(contracts) + list(quotes)
    j.check_requests = list(requests)
    j.change_orders = list(change_orders)
    j.invoices = []
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


def request(amount="30000.00", vendor="Reilly Roofing LLC", status=CHECK_REQUESTED,
            days_ago=3, reference=None):
    r = CheckRequest(
        job_id=1, vendor=vendor, amount=D(amount), status=status,
        reference=reference or f"Req {_next()}",
        requested_on=TODAY - timedelta(days=days_ago),
    )
    r.id = _next()
    r.received_at = datetime(2026, 9, 5, tzinfo=timezone.utc) - timedelta(days=days_ago)
    return r


def co(amount, vendor="Reilly Roofing LLC"):
    return ChangeOrder(job_id=1, vendor=vendor, amount=D(amount),
                       status="approved", approved_by="Zack")


# --- where a sub stands ---------------------------------------------------

def test_a_subs_position_is_the_running_total_not_the_latest_request():
    j = job(
        contracts=[contract("120000.00")],
        requests=[
            request("30000.00", status=CHECK_PAID),
            request("30000.00", status=CHECK_APPROVED),
            request("30000.00", status=CHECK_REQUESTED),
        ],
    )
    p = subs.positions(j)[0]

    assert p.contract == D("120000.00")
    assert p.approved == D("60000.00")     # paid and approved both count
    assert p.paid == D("30000.00")
    assert p.waiting == D("30000.00")
    assert p.remaining == D("60000.00")
    assert p.committed == D("90000.00")
    assert p.percent_drawn == 50


def test_a_refused_request_counts_for_nothing():
    j = job(contracts=[contract("120000.00")],
            requests=[request("30000.00", status=CHECK_REJECTED)])
    p = subs.positions(j)[0]
    assert p.approved == ZERO_D and p.waiting == ZERO_D


ZERO_D = D("0")


def test_extras_raise_what_a_sub_may_draw():
    j = job(contracts=[contract("120000.00")], change_orders=[co("18000.00")],
            requests=[request("130000.00", status=CHECK_APPROVED)])
    p = subs.positions(j)[0]

    assert p.awarded == D("138000.00")
    assert p.overage == ZERO_D
    assert p.remaining == D("8000.00")


def test_a_change_order_for_a_material_vendor_does_not_raise_a_subs_ceiling():
    """A change order from the roofing supplier has nothing to do with what a
    labour sub may draw."""
    j = job(contracts=[contract("120000.00")],
            change_orders=[co("18000.00", vendor="New Castle Building Products")])
    p = subs.positions(j)[0]
    assert p.change_orders == ZERO_D
    assert p.awarded == D("120000.00")


def test_material_quotes_are_not_subcontracts():
    j = job(contracts=[contract("120000.00")], quotes=[material()])
    positions = subs.positions(j)
    assert len(positions) == 1
    assert positions[0].vendor == "Reilly Roofing LLC"


def test_a_sub_who_signs_two_ways_is_one_sub():
    j = job(contracts=[contract("120000.00", vendor="Reilly Roofing LLC")],
            requests=[request("30000.00", vendor="REILLY ROOFING")])
    positions = subs.positions(j)
    assert len(positions) == 1
    assert positions[0].waiting == D("30000.00")


# --- the check that matters -----------------------------------------------

def test_the_seventh_request_is_the_one_that_gets_caught():
    """Six at $20,000 fit inside a $120,000 contract. Each looked fine. The
    seventh is the first thing anyone could have objected to, and only the
    running total sees it."""
    approved = [request("20000.00", status=CHECK_APPROVED) for _ in range(6)]
    seventh = request("20000.00")
    j = job(contracts=[contract("120000.00")], requests=approved + [seventh])

    verdict = subs.check(j, seventh)
    assert not verdict.can_approve
    assert verdict.over_contract
    assert verdict.exceeds_by == D("20000.00")


def test_a_request_inside_the_contract_is_approvable_and_says_what_is_left():
    r = request("30000.00")
    j = job(contracts=[contract("120000.00")],
            requests=[request("30000.00", status=CHECK_APPROVED), r])

    verdict = subs.check(j, r)
    assert verdict.can_approve
    assert not verdict.over_contract
    assert any("Leaves $60,000.00" in reason for reason in verdict.reasons)


def test_a_request_landing_exactly_on_the_contract_is_fine():
    r = request("60000.00")
    j = job(contracts=[contract("120000.00")],
            requests=[request("60000.00", status=CHECK_APPROVED), r])
    assert subs.check(j, r).can_approve


def test_extras_are_what_make_an_overage_approvable():
    r = request("20000.00")
    approved = [request("20000.00", status=CHECK_APPROVED) for _ in range(6)]
    j = job(contracts=[contract("120000.00")], change_orders=[co("25000.00")],
            requests=approved + [r])

    assert subs.check(j, r).can_approve


def test_no_contract_on_file_blocks_for_a_different_reason():
    """Not an overage - there is simply nothing to check against, and that
    cannot be waved through the way an overage can."""
    r = request("30000.00")
    j = job(requests=[r])

    verdict = subs.check(j, r)
    assert not verdict.can_approve
    assert not verdict.over_contract
    assert "No subcontract on file" in verdict.blockers[0]


# --- the queue ------------------------------------------------------------

def test_the_queue_is_ordered_by_how_long_people_have_waited():
    a = request("10000.00", vendor="Alpha Siding", days_ago=2)
    b = request("90000.00", vendor="Bravo Electric", days_ago=40)
    c = request("50000.00", vendor="Charlie Gutters", days_ago=12)
    j = job(contracts=[contract("500000.00", vendor="Alpha Siding")],
            requests=[a, b, c])

    rows = subs.queue([j], TODAY)
    assert [w.request.vendor for w in rows] == [
        "Bravo Electric", "Charlie Gutters", "Alpha Siding",
    ]
    assert [w.days for w in rows] == [40, 12, 2]


def test_the_biggest_cheque_does_not_jump_the_queue():
    """Sorting by amount has the same failure as sorting by anything else: the
    request nobody has looked at stays the request nobody has looked at."""
    small_old = request("500.00", vendor="Alpha Siding", days_ago=30)
    huge_new = request("250000.00", vendor="Bravo Electric", days_ago=1)
    rows = subs.queue([job(requests=[small_old, huge_new])], TODAY)
    assert rows[0].request.vendor == "Alpha Siding"


def test_only_open_requests_are_in_the_queue():
    j = job(requests=[
        request("10000.00", status=CHECK_APPROVED, days_ago=90),
        request("10000.00", status=CHECK_PAID, days_ago=80),
        request("10000.00", status=CHECK_REJECTED, days_ago=70),
        request("10000.00", status=CHECK_REQUESTED, days_ago=1),
    ])
    rows = subs.queue([j], TODAY)
    assert len(rows) == 1
    assert rows[0].days == 1


def test_the_queue_carries_each_subs_position_with_it():
    """So the list can say 'and this one is already past their contract'
    without the reader opening the job."""
    over = request("100000.00", days_ago=5)
    j = job(contracts=[contract("120000.00")],
            requests=[request("60000.00", status=CHECK_APPROVED), over])

    row = subs.queue([j], TODAY)[0]
    assert row.over_contract
    assert row.position.would_exceed == D("40000.00")


def test_a_request_with_no_date_of_its_own_ages_from_when_it_arrived():
    r = CheckRequest(job_id=1, vendor="Alpha Siding", amount=D("1000.00"),
                     status=CHECK_REQUESTED, requested_on=None)
    r.id = 1
    r.received_at = datetime(2026, 8, 26, tzinfo=timezone.utc)
    rows = subs.queue([job(requests=[r])], TODAY)
    assert rows[0].days == 10


def test_a_request_dated_in_the_future_does_not_age_backwards():
    r = request("1000.00", days_ago=-5)
    assert subs.queue([job(requests=[r])], TODAY)[0].days == 0


@pytest.mark.parametrize("days,band", [
    (0, "This week"), (7, "This week"),
    (8, "Over a week"), (14, "Over a week"),
    (15, "Over two weeks"), (30, "Over two weeks"),
    (31, "Over a month"), (400, "Over a month"),
])
def test_the_bands_read_like_a_conversation_about_paying_people(days, band):
    assert subs.band_for(days) == band


def test_the_total_waiting_is_what_would_go_out_if_everything_were_approved():
    j = job(requests=[request("10000.00"), request("25000.00")])
    rows = subs.queue([j], TODAY)
    assert subs.total_waiting(rows) == D("35000.00")

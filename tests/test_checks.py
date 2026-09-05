"""The check queue, and the subcontractor invoices that are now in it.

Zack: *"check request should be automatic from sub invoices. If a sub invoice
is sent over it goes into check request as well."*

The queue answers one question - who is waiting on money, and how long - and a
sub's draw is one of the answers. What these tests hold down is that it is
answered by *reading* the invoice, not by writing a second row for it: the
money must be counted once, and approved in one place.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import checks, costing  # noqa: E402
from app.models import (  # noqa: E402
    APPROVAL_APPROVED,
    APPROVAL_HELD,
    APPROVAL_PAID,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    CHECK_APPROVED,
    CHECK_PAID,
    CHECK_PERMIT,
    CHECK_REQUESTED,
    CHECK_SUBCONTRACTOR,
    CheckRequest,
    Invoice,
    Job,
    Quote,
)

TODAY = date(2026, 9, 5)
_ids = {"n": 0}


def _next() -> int:
    _ids["n"] += 1
    return _ids["n"]


def job(number="269001", quotes=(), invoices=(), requests=()):
    j = Job(job_number=number, name="Daul Gardens")
    j.id = _next()
    j.quotes = list(quotes)
    j.invoices = list(invoices)
    j.change_orders = []
    j.check_requests = list(requests)
    for r in j.check_requests:
        r.job = j
    return j


def contract(total="120000.00", vendor="Reilly Roofing LLC"):
    q = Quote(job_id=1, document_id=_next(), vendor=vendor, is_master=True,
              is_subcontract=True, total=D(total))
    q.id = _next()
    q.lines = []
    return q


def material(total="34232.17", vendor="ABC Supply Co."):
    q = Quote(job_id=1, document_id=_next(), vendor=vendor, is_master=True,
              is_subcontract=False, total=D(total))
    q.id = _next()
    q.lines = []
    return q


def invoice(total="23300.00", vendor="Reilly Roofing LLC",
            status=APPROVAL_PENDING, days=10, number=None):
    inv = Invoice(job_id=1, document_id=_next(), vendor=vendor,
                  invoice_number=number or f"RR-{_next()}", total=D(total),
                  approval_status=status,
                  invoice_date=date.fromordinal(TODAY.toordinal() - days))
    inv.id = _next()
    inv.created_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    inv.lines = []
    return inv


def request(amount="450.00", payee="Township of Oakland", days=26,
            purpose=CHECK_PERMIT, status=CHECK_REQUESTED):
    r = CheckRequest(job_id=1, vendor=payee, amount=D(amount), purpose=purpose,
                     status=status,
                     requested_on=date.fromordinal(TODAY.toordinal() - days))
    r.id = _next()
    r.received_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    return r


# --- a sub's invoice is a request for a check -----------------------------

def test_a_subs_invoice_is_in_the_queue_without_anybody_retyping_it():
    j = job(quotes=[contract()], invoices=[invoice("23300.00", days=12)])
    rows = checks.queue(jobs=[j], today=TODAY)

    assert len(rows) == 1
    row = rows[0]
    assert row.payee == "Reilly Roofing LLC"
    assert row.amount == D("23300.00")
    assert row.job_number == "269001"
    assert row.purpose_label == "Subcontractor"
    assert row.days == 12
    assert row.band == "Over a week"


def test_the_clock_runs_from_the_day_they_billed_us():
    """Not from the day we got round to approving it. That gap is exactly the
    number somebody is phoning about."""
    j = job(quotes=[contract()],
            invoices=[invoice(days=40, status=APPROVAL_PENDING)])
    row = checks.queue(jobs=[j], today=TODAY)[0]
    assert row.days == 40
    assert row.band == "Over a month"


def test_an_invoice_nobody_has_decided_is_still_somebody_waiting():
    j = job(quotes=[contract()], invoices=[invoice(status=APPROVAL_PENDING)])
    row = checks.queue(jobs=[j], today=TODAY)[0]
    assert not row.ready
    assert row.state == "Not checked off yet"
    assert row.tone == "quiet"          # normal, not an alarm


def test_a_held_invoice_says_do_not_pay_it():
    j = job(quotes=[contract()], invoices=[invoice(status=APPROVAL_HELD)])
    row = checks.queue(jobs=[j], today=TODAY)[0]
    assert not row.ready
    assert "do not pay" in row.state.lower()


def test_an_approved_invoice_is_ready_to_pay():
    j = job(quotes=[contract()], invoices=[invoice(status=APPROVAL_APPROVED)])
    row = checks.queue(jobs=[j], today=TODAY)[0]
    assert row.ready


def test_a_paid_or_refused_invoice_leaves_the_queue():
    j = job(quotes=[contract()], invoices=[
        invoice(status=APPROVAL_PAID), invoice(status=APPROVAL_REJECTED),
    ])
    assert checks.queue(jobs=[j], today=TODAY) == []


def test_a_supply_houses_invoice_is_not_a_check_request():
    """It is paid on terms out of accounts payable and never was one. Putting
    it here would bury the people this queue exists for."""
    j = job(quotes=[contract(), material()], invoices=[
        invoice("6154.00", vendor="ABC Supply Co."),
        invoice("23300.00", vendor="Reilly Roofing LLC"),
    ])
    rows = checks.queue(jobs=[j], today=TODAY)
    assert [r.payee for r in rows] == ["Reilly Roofing LLC"]


def test_a_job_with_no_subcontract_contributes_nothing():
    j = job(quotes=[material()], invoices=[invoice(vendor="ABC Supply Co.")])
    assert checks.queue(jobs=[j], today=TODAY) == []


# --- the two kinds sit in one queue ---------------------------------------

def test_typed_requests_and_sub_invoices_are_ordered_together_by_wait():
    j = job(quotes=[contract()], requests=[request(days=26)], invoices=[
        invoice("23300.00", days=41), invoice("9000.00", days=3),
    ])
    rows = checks.queue(j.check_requests, [j], today=TODAY)

    assert [r.days for r in rows] == [41, 26, 3]
    assert rows[0].payee == "Reilly Roofing LLC"
    assert rows[1].payee == "Township of Oakland"


def test_only_the_typed_ones_are_decided_here():
    """A sub's draw is decided on the invoice, against their contract, with
    the award applied. Two places to approve the same money can disagree."""
    j = job(quotes=[contract()], requests=[request()],
            invoices=[invoice(status=APPROVAL_APPROVED)])
    rows = checks.queue(j.check_requests, [j], today=TODAY)

    typed = [r for r in rows if r.decide_here]
    derived = [r for r in rows if not r.decide_here]
    assert len(typed) == 1 and typed[0].request is not None
    assert len(derived) == 1 and derived[0].invoice is not None


def test_total_owed_and_total_ready_are_different_questions():
    j = job(quotes=[contract()], requests=[
        request("450.00"),                                  # nobody decided
        request("625.00", payee="Bergen County Clerk", status=CHECK_APPROVED),
    ], invoices=[
        invoice("23300.00", status=APPROVAL_APPROVED),
        invoice("9000.00", status=APPROVAL_HELD),
    ])
    rows = checks.queue(j.check_requests, [j], today=TODAY)

    assert checks.total_waiting(rows) == D("33375.00")
    # Only what somebody has signed off. Not the held invoice, and not the
    # permit nobody has looked at.
    assert checks.total_ready(rows) == D("23925.00")


def test_an_approved_request_stays_in_the_queue_until_the_check_goes_out():
    """It used to drop out the moment it was approved, so the one list of who
    is owed money quietly stopped including the people we had agreed to pay."""
    j = job(requests=[request("625.00", days=17, status=CHECK_APPROVED)])
    rows = checks.queue(j.check_requests, today=TODAY)

    assert len(rows) == 1
    assert rows[0].ready
    assert rows[0].days == 17            # the clock did not stop at approval
    assert not rows[0].decide_here       # nothing left to decide, only to pay


def test_a_paid_request_is_gone():
    j = job(requests=[request(status=CHECK_PAID)])
    assert checks.queue(j.check_requests, today=TODAY) == []


# --- and it is counted once -----------------------------------------------

def test_a_subs_draw_in_the_queue_does_not_double_the_cost_of_the_job():
    """The trap this design exists to avoid. Job costing counts subcontract
    invoices and check-request rows in separate buckets, so materialising a
    CheckRequest for a sub's invoice would put that money in both."""
    j = job(quotes=[contract()], requests=[request("450.00", status=CHECK_PAID)],
            invoices=[invoice("23300.00", status=APPROVAL_APPROVED)])
    j.contract_amount = D("268000.00")
    j.labour_cost = None
    j.costing_note = ""

    report = costing.build(j)
    by_label = {b.key: b for b in report.buckets}

    assert by_label["subcontract"].agreed == D("23300.00")
    assert by_label["checks"].agreed == D("450.00")   # the permit, and only it
    assert report.cost == D("23750.00")

    # And the queue reports the same money once, as one row.
    rows = checks.queue(j.check_requests, [j], today=TODAY)
    assert len(rows) == 1
    assert rows[0].amount == D("23300.00")


def test_a_draw_that_would_go_past_the_award_says_so_in_the_queue():
    """Every line correctly priced, and it still takes the sub past what they
    were awarded. This is the room where somebody decides to cut the check, so
    it is the room where that has to be visible."""
    j = job(quotes=[contract("120000.00")], invoices=[
        invoice("100000.00", status=APPROVAL_APPROVED, days=30, number="RR-1"),
        invoice("40000.00", status=APPROVAL_PENDING, days=5, number="RR-2"),
    ])
    rows = {r.reference: r for r in checks.queue(jobs=[j], today=TODAY)}

    assert "past the award" in rows["RR-2"].state
    assert "$20,000.00" in rows["RR-2"].state
    assert not rows["RR-2"].ready and rows["RR-2"].tone == "bad"
    # And it is kept out of what the office is told it can pay today.
    assert checks.total_ready(list(rows.values())) == D("100000.00")

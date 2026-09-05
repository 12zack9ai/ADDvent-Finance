"""The sample dataset.

It ships switched on, and it writes into the same database the real documents
will land in, so two things have to hold and neither is negotiable: it says
what it claims to say, and it comes back out cleanly.
"""
from __future__ import annotations

import os
import sys
import tempfile
from decimal import Decimal as D
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = Path(tempfile.mkdtemp(prefix="finance-seed-"))
os.environ["DATA_DIR"] = str(_TMP)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'seed.db'}"
os.environ["APP_PASSWORD"] = ""
os.environ["ANTHROPIC_API_KEY"] = "test"

from sqlalchemy import select                                        # noqa: E402

from app import checks, jobsummary, subs, trust                      # noqa: E402
from app.db import SessionLocal, init_db                             # noqa: E402
from app.models import (                                             # noqa: E402
    APPROVAL_HELD,
    CashReport,
    CheckRequest,
    Document,
    Invoice,
    Job,
    Quote,
)

from scripts import seed_samples                                     # noqa: E402


@pytest.fixture(scope="module")
def session():
    init_db()
    with SessionLocal() as s:
        seed_samples.seed(s)
        yield s


def job_of(session, number: str) -> Job:
    return session.scalar(select(Job).where(Job.job_number == number))


# --- it is there, and it is in the reserved band --------------------------

def test_seeding_produces_eight_jobs_and_a_forecast(session):
    """Real jobs run up from 260000, so the 269xxx band cannot collide with
    one - which is also what makes removal safe."""
    jobs = seed_samples.sample_jobs(session)
    assert len(jobs) == 8
    assert all(j.job_number.startswith("269") for j in jobs)
    assert session.scalar(select(CashReport)) is not None


def test_running_it_twice_does_not_double_anything(session):
    assert seed_samples.already_seeded(session)


# --- each job says what it is there to say --------------------------------

def test_the_healthy_job_adds_up_and_is_not_accused_of_anything(session):
    """Staged deliveries of the same shingle must not read as duplicates -
    that is the most ordinary thing that happens on a big roof."""
    s = jobsummary.build(job_of(session, "269001"))
    assert s.overlaps == []
    assert not s.needs_explaining
    assert s.found > 0                       # a price increase was caught
    assert s.unquoted_supplier > 0           # the New Castle straggler
    assert "New Castle Building Products" in s.unquoted_vendors


def test_the_price_that_moved_is_held_rather_than_waved_through(session):
    job = job_of(session, "269001")
    held = [i for i in job.invoices if i.approval_status == APPROVAL_HELD]
    assert len(held) == 1
    assert held[0].overbilled_amount > 0


def test_a_supplier_quoting_twice_leaves_both_quotes_live(session):
    """Roofing material and skylights, same supplier, same week."""
    job = job_of(session, "269002")
    assert len([q for q in job.quotes if q.is_master]) == 2
    for invoice in job.invoices:
        assert invoice.lines_unmatched == 0   # priced against the pair of them


def test_the_corrected_invoice_that_never_cancelled_the_original_is_flagged(session):
    s = jobsummary.build(job_of(session, "269003"))
    assert len(s.overlaps) == 1
    assert s.needs_explaining


def test_an_invoice_with_no_quote_says_so_and_the_pm_was_asked(session):
    job = job_of(session, "269004")
    assert job.quotes == []
    assert all(i.quote_id is None for i in job.invoices)
    assert job.quote_chase_sent_at is not None


def test_a_revised_quote_stands_the_old_one_down(session):
    job = job_of(session, "269005")
    live = [q for q in job.quotes if q.is_master]
    superseded = [q for q in job.quotes if not q.is_master]
    assert len(live) == 1 and len(superseded) == 1
    assert live[0].quote_number == "Q-NC-3186"
    assert superseded[0].superseded_at is not None
    assert len(job.change_orders) == 2
    assert sum(1 for c in job.change_orders if c.is_live) == 1


def test_the_sub_whose_next_invoice_goes_past_the_award(session):
    job = job_of(session, "269006")
    position = subs.positions(job)[0]
    assert position.awarded == D("84000.00")
    over = [i for i in position.open_invoices if position.would_exceed_with(i)]
    assert len(over) == 1
    assert position.would_exceed_with(over[0]) == D("4000.00")


def test_the_check_queue_covers_every_waiting_band(session):
    rows = checks.queue(session.scalars(select(CheckRequest)).all())
    assert [w.band for w in rows][0] == "Over a month"      # longest wait first
    assert len({w.band for w in rows}) == 4
    assert all(w.request.job_id for w in rows)              # every check has a job


def test_the_subs_own_invoices_are_in_the_check_queue_too(session):
    """A sub sending their draw is asking for a check, and nobody retypes it."""
    jobs = seed_samples.sample_jobs(session)
    rows = checks.queue(session.scalars(select(CheckRequest)).all(), jobs)

    subs_waiting = [r for r in rows if r.invoice is not None]
    assert {r.payee for r in subs_waiting} == {
        "Reilly Roofing LLC", "Vanguard Sheet Metal Inc.",
    }
    # Every one of them points back at the invoice for the decision.
    assert all(not r.decide_here for r in subs_waiting)
    # And the money is bigger than the typed requests alone.
    assert checks.total_waiting(rows) > checks.total_waiting(
        [r for r in rows if r.request is not None])


def test_the_two_bills_that_are_not_ours_are_both_blocked(session):
    job = job_of(session, "269008")
    flagged = {
        i.invoice_number: trust.blocking(trust.flags_for(i.document))
        for i in job.invoices
    }
    assert flagged["APX-4471"]          # stranger, freemail, pay-today, new bank
    assert flagged["ABC-99120"]         # a name we know, from a domain they never use
    assert not flagged["NC-772110"]     # the real one stays quiet


def test_the_forecast_sees_the_permits_and_deposits_too(session):
    """They arrive with no invoice behind them, so a forecast assembled from
    bills alone cannot see them - and a 13-week view that quietly omits money
    going out is wrong in the direction that matters."""
    from app import accounting
    from app.cashflow import CAT_CHECKS

    payables = accounting.LocalSource(session).payables()
    from_checks = [p for p in payables if p.category == CAT_CHECKS]

    open_requests = session.scalars(
        select(CheckRequest).where(
            CheckRequest.status.in_(("requested", "approved"))
        )
    ).all()
    assert len(from_checks) == len(open_requests)
    assert sum(p.amount for p in from_checks) == sum(
        r.amount for r in open_requests)

    # The undecided ones are listed, never scheduled as leaving.
    assert {p.on_hold for p in from_checks} == {True, False}


def test_no_dollar_reaches_the_forecast_twice(session):
    """A sub's draw is an invoice and is counted as one. It must not also
    arrive as a check request - and nothing else may be counted twice
    either."""
    from app import accounting

    payables = accounting.LocalSource(session).payables()
    keys = [(p.vendor, p.reference, p.amount) for p in payables]
    assert len(keys) == len(set(keys))

    invoices = session.scalars(
        select(Invoice).where(Invoice.approval_status != "paid")
    ).all()
    live = [i for i in invoices if i.approval_status != "rejected"]
    from_invoices = [p for p in payables if p.source == "This system"
                     and p.category != "Permits, deposits and other checks"]
    assert len(from_invoices) == len(live)


# --- and it comes back out ------------------------------------------------

def test_removal_takes_out_every_sample_row_and_nothing_else(session):
    real = Job(job_number="260014", name="A real job")
    session.add(real)
    session.commit()
    real_id = real.id

    seed_samples.remove(session)

    assert seed_samples.sample_jobs(session) == []
    assert session.scalars(
        select(Document).where(Document.sha256.like(f"{seed_samples.DOC_MARK}%"))
    ).all() == []
    assert session.scalars(
        select(CashReport).where(CashReport.source_label == seed_samples.CASH_MARK)
    ).all() == []
    # Nothing is left pointing at a job that no longer exists.
    for model in (Invoice, Quote, CheckRequest):
        orphans = [
            row for row in session.scalars(select(model)).all()
            if session.get(Job, row.job_id) is None
        ]
        assert orphans == []
    # And the real job is still standing.
    assert session.get(Job, real_id) is not None

"""Tests for three-way match and approval routing.

This decides who is allowed to authorise a payment, so it gets tested like it
matters. No network, no model calls - pure rules.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.approval import (  # noqa: E402
    ACTION_APPROVE,
    ACTION_HOLD,
    ACTION_INVESTIGATE,
    ACTION_SPOT_CHECK,
    route,
    tolerance_for,
)
from app.models import (  # noqa: E402
    TIER_OWNER,
    TIER_PM,
    ChangeOrder,
    Invoice,
    Job,
    Quote,
    Receipt,
)

D = Decimal


def make(total="1000.00", over="0", unmatched=0, quote_id=1,
         receipt=True, change_orders=(), quote_match="vendor", vendor="Baker Supply"):
    job = Job(job_number="4417", name="Test job")
    job.receipts = []
    job.change_orders = list(change_orders)
    if receipt:
        job.receipts.append(Receipt(
            job_id=1, vendor=vendor, reference="PS-1",
            confirmed_by="Site super",
            confirmed_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
        ))
    invoice = Invoice(
        job_id=1, document_id=1, vendor=vendor, invoice_number="INV-1",
        total=D(total), overbilled_amount=D(over), lines_unmatched=unmatched,
        quote_id=quote_id, quote_match=quote_match,
    )
    invoice.job = job
    invoice.receipt_id = None
    invoice.receipt = None
    return invoice


def co(amount, number="CO-1", vendor="Baker Supply"):
    return ChangeOrder(
        job_id=1, number=number, vendor=vendor, amount=D(amount),
        description="Hidden rot at north elevation", approved_by="Owner",
    )


# --- tolerance ------------------------------------------------------------

def test_tolerance_is_the_greater_of_percentage_and_flat_amount():
    # 5% of $1,000 is $50, so the $250 flat allowance governs.
    assert tolerance_for(D("1000.00")) == D("250")
    # 5% of $20,000 is $1,000, which is greater than $250.
    assert tolerance_for(D("20000.00")) == D("1000.00")


def test_missing_total_still_gets_the_flat_allowance():
    assert tolerance_for(None) == D("250")


# --- the four rows of the approval policy ---------------------------------

def test_clean_invoice_goes_to_the_project_manager():
    r = route(make(total="1000.00", over="0"))
    assert r.action == ACTION_APPROVE
    assert r.tier == TIER_PM
    assert r.can_approve is True


def test_small_overage_within_tolerance_still_goes_to_the_pm():
    r = route(make(total="1000.00", over="100.00"))
    assert r.action == ACTION_APPROVE
    assert r.tier == TIER_PM
    assert r.within_tolerance is True


def test_large_but_clean_invoice_gets_an_owner_spot_check():
    r = route(make(total="12000.00", over="0"))
    assert r.action == ACTION_SPOT_CHECK
    assert r.tier == TIER_OWNER
    assert r.can_approve is True          # a spot check, not a blocker


def test_overage_beyond_tolerance_with_no_change_order_is_held():
    r = route(make(total="1000.00", over="900.00"))
    assert r.action == ACTION_HOLD
    assert r.tier == TIER_OWNER
    assert r.within_tolerance is False


def test_no_quote_at_all_goes_to_the_owner_to_investigate():
    r = route(make(quote_id=None))
    assert r.action == ACTION_INVESTIGATE
    assert r.tier == TIER_OWNER


# --- the receiving leg ----------------------------------------------------

def test_unconfirmed_receipt_blocks_approval_even_when_priced_perfectly():
    """The whole point of a three-way match: right price, never delivered."""
    r = route(make(total="1000.00", over="0", receipt=False))
    assert r.can_approve is False
    assert any("delivered" in b or "completed" in b for b in r.blockers)


def test_confirmed_receipt_is_recorded_in_the_reasons():
    r = route(make())
    assert r.can_approve is True
    assert any("Receipt confirmed" in reason for reason in r.reasons)


def test_receipt_from_a_different_vendor_does_not_count():
    invoice = make(receipt=False)
    invoice.job.receipts.append(Receipt(
        job_id=1, vendor="Cornerstone Lumber", confirmed_by="Site super",
        confirmed_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    ))
    r = route(invoice)
    assert r.can_approve is False


# --- change orders --------------------------------------------------------

def test_change_order_authorises_an_overage_beyond_tolerance():
    r = route(make(total="1000.00", over="900.00", change_orders=[co("1200.00")]))
    assert r.action != ACTION_HOLD
    assert r.covering_change_order is not None
    assert any("change order" in reason.lower() for reason in r.reasons)


def test_change_order_that_is_too_small_does_not_authorise_the_overage():
    r = route(make(total="1000.00", over="900.00", change_orders=[co("400.00")]))
    assert r.action == ACTION_HOLD
    assert r.covering_change_order is None


def test_change_order_from_another_vendor_does_not_apply():
    r = route(make(total="1000.00", over="900.00",
                   change_orders=[co("5000.00", vendor="Cornerstone Lumber")]))
    assert r.action == ACTION_HOLD


def test_smallest_sufficient_change_order_is_used():
    """Don't consume a big authorisation on a small overage."""
    r = route(make(total="1000.00", over="900.00",
                   change_orders=[co("5000.00", "CO-BIG"), co("950.00", "CO-SMALL")]))
    assert r.covering_change_order.number == "CO-SMALL"


# --- surfacing the things a reviewer should know --------------------------

def test_unmatched_lines_are_called_out_even_on_an_approvable_invoice():
    r = route(make(total="1000.00", over="0", unmatched=3))
    assert r.can_approve is True
    assert any("not on the quote" in reason for reason in r.reasons)


def test_vendor_name_mismatch_is_surfaced_to_the_reviewer():
    r = route(make(quote_match="sole"))
    assert any("differs" in reason for reason in r.reasons)


def test_hold_outranks_the_size_based_spot_check():
    """A held invoice must not be presented as merely needing a glance."""
    r = route(make(total="50000.00", over="9000.00"))
    assert r.action == ACTION_HOLD


# --- a quote from another vendor must never be used -----------------------

def test_invoice_from_an_unquoted_vendor_is_not_priced_against_someone_elses_quote():
    """Caught by the demo data: a dumpster invoice was being compared against a
    roofing quote because it was the only quote on the job. Reporting a
    comparison that never meaningfully happened is worse than reporting none.
    """
    job = Job(job_number="4417")
    job.quotes = [Quote(job_id=1, document_id=1, is_master=True,
                        vendor="Baker Building Supply", quote_number="Q-1")]
    quote, how = job.master_for_vendor("ABC Waste Removal")
    assert quote is None
    assert how == "none"


def test_vendor_name_variant_still_finds_its_own_quote():
    job = Job(job_number="4417")
    job.quotes = [Quote(job_id=1, document_id=1, is_master=True,
                        vendor="New Castle Building Products", quote_number="Q-1")]
    quote, how = job.master_for_vendor("NEW CASTLE BLDG PRODUCTS INC")
    assert quote is not None
    assert how == "vendor"


def test_each_vendor_gets_its_own_master_on_a_shared_job():
    job = Job(job_number="4417")
    job.quotes = [
        Quote(job_id=1, document_id=1, is_master=True, vendor="Baker Building Supply"),
        Quote(job_id=1, document_id=2, is_master=True, vendor="ABC Waste Removal"),
    ]
    baker, _ = job.master_for_vendor("Baker Building Supply")
    waste, _ = job.master_for_vendor("ABC Waste Removal")
    assert baker is not waste
    assert baker.vendor == "Baker Building Supply"
    assert waste.vendor == "ABC Waste Removal"

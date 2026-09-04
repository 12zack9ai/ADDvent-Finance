"""The 13-day cash flow forecast.

The tests worth writing here are not "does it add up". They are the judgements
that decide whether the report tells the truth on a bad week: overdue money,
money that is owed but held, and receivables that will not actually arrive when
the terms say they will.
"""
from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from app.cashflow import Payable, Receivable, build_forecast

TODAY = date(2026, 9, 7)


def pay(days_out, amount, vendor="New Castle", **kw):
    due = None if days_out is None else TODAY + timedelta(days=days_out)
    return Payable(due_date=due, vendor=vendor, amount=D(amount), **kw)


def rec(days_out, amount, customer="Ridgeview Condo Assn", **kw):
    due = None if days_out is None else TODAY + timedelta(days=days_out)
    return Receivable(due_date=due, customer=customer, amount=D(amount), **kw)


def build(payables=(), receivables=(), opening="10000.00", **kw):
    return build_forecast(opening_balance=D(opening), payables=list(payables),
                          receivables=list(receivables), today=TODAY, **kw)


# --- shape ---------------------------------------------------------------

def test_the_window_is_thirteen_days_including_today():
    f = build()
    assert len(f.days) == 13
    assert f.days[0].on == TODAY
    assert f.days[-1].on == TODAY + timedelta(days=12)


def test_an_empty_week_leaves_the_balance_alone():
    f = build()
    assert f.closing_balance == D("10000.00")
    assert f.total_in == D("0.00") and f.total_out == D("0.00")


# --- the balance runs forward -------------------------------------------

def test_money_lands_on_the_day_it_is_due():
    f = build(payables=[pay(3, "1500.00")], receivables=[rec(5, "4000.00")])
    assert f.days[3].out == D("1500.00")
    assert f.days[5].incoming == D("4000.00")
    assert f.days[2].closing == D("10000.00")
    assert f.days[3].closing == D("8500.00")
    assert f.days[5].closing == D("12500.00")
    assert f.closing_balance == D("12500.00")


def test_the_low_point_is_reported_even_when_the_week_ends_healthy():
    """A comfortable closing balance can hide a day the account goes under.
    That day is the entire reason to read this report."""
    f = build(payables=[pay(2, "25000.00")], receivables=[rec(9, "40000.00")])
    assert f.closing_balance == D("25000.00")
    assert f.goes_negative
    assert f.low_point.on == TODAY + timedelta(days=2)
    assert f.low_point.closing == D("-15000.00")


def test_a_week_that_never_dips_is_not_reported_as_negative():
    f = build(payables=[pay(4, "500.00")])
    assert not f.goes_negative


# --- overdue -------------------------------------------------------------

def test_an_overdue_bill_is_due_now_not_forgotten():
    """It fell due last week. Leaving it out of the window is how a forecast
    tells you that you are fine when you are not."""
    f = build(payables=[pay(-9, "3200.00")])
    assert f.days[0].out == D("3200.00")
    assert [p.amount for p in f.overdue_payables] == [D("3200.00")]
    assert f.total_out == D("3200.00")


def test_an_overdue_receivable_is_not_assumed_to_arrive_today():
    """The opposite treatment, and deliberately so. Money we owe is certain;
    money owed to us that is already late is a collections problem."""
    f = build(receivables=[rec(-20, "18000.00")])
    assert f.total_in == D("0.00")
    assert f.closing_balance == D("10000.00")
    assert [r.amount for r in f.overdue_receivables] == [D("18000.00")]


# --- held by the three-way match ----------------------------------------

def test_a_held_invoice_is_not_counted_as_leaving():
    """Held means disputed. Counting it as paid this week overstates the
    outflow, and paying it would be wrong anyway."""
    f = build(payables=[pay(3, "9000.00", on_hold=True, hold_reason="Billed over quote")])
    assert f.total_out == D("0.00")
    assert f.held_total == D("9000.00")
    assert f.held_payables[0].hold_reason == "Billed over quote"


def test_a_held_invoice_is_still_visible_rather_than_dropped():
    f = build(payables=[pay(3, "9000.00", on_hold=True)])
    assert len(f.held_payables) == 1


# --- receivables arrive when they arrive ---------------------------------

def test_a_customer_who_pays_late_is_counted_late():
    """Net 30 from a customer who reliably pays at 45 days is not day-30 cash."""
    late = rec(4, "12000.00", expected_date=TODAY + timedelta(days=19),
               days_late_typical=15)
    f = build(receivables=[late])
    assert f.total_in == D("0.00")          # day 19 is outside a 13-day window
    assert f.days[4].incoming == D("0.00")


def test_an_expected_date_inside_the_window_is_used_over_the_due_date():
    early = rec(2, "5000.00", expected_date=TODAY + timedelta(days=6))
    f = build(receivables=[early])
    assert f.days[2].incoming == D("0.00")
    assert f.days[6].incoming == D("5000.00")


# --- things with no date -------------------------------------------------

def test_a_bill_with_no_due_date_is_listed_not_ignored():
    f = build(payables=[pay(None, "700.00")])
    assert f.total_out == D("0.00")
    assert [p.amount for p in f.unscheduled_payables] == [D("700.00")]


def test_money_beyond_the_window_does_not_appear_in_it():
    f = build(payables=[pay(40, "5000.00")], receivables=[rec(60, "9000.00")])
    assert f.total_out == D("0.00") and f.total_in == D("0.00")


# --- early payment discounts --------------------------------------------

def test_a_discount_expiring_in_the_window_is_surfaced():
    """2/10 net 30 is free money and it is missed constantly."""
    p = pay(25, "10000.00", discount_amount=D("200.00"),
            discount_deadline=TODAY + timedelta(days=6))
    f = build(payables=[p])
    assert f.discount_savings == D("200.00")
    assert f.discounts[0].discount_deadline == TODAY + timedelta(days=6)


def test_a_discount_already_expired_is_not_offered():
    p = pay(20, "10000.00", discount_amount=D("200.00"),
            discount_deadline=TODAY - timedelta(days=1))
    assert build(payables=[p]).discounts == []


# --- totals --------------------------------------------------------------

def test_the_totals_agree_with_the_day_by_day():
    f = build(
        payables=[pay(1, "1000.00"), pay(4, "2500.00"), pay(-3, "800.00")],
        receivables=[rec(2, "6000.00"), rec(11, "1500.00")],
    )
    assert f.total_out == D("4300.00")
    assert f.total_in == D("7500.00")
    assert f.net_movement == D("3200.00")
    assert f.closing_balance == D("13200.00")
    assert f.closing_balance == f.opening_balance + f.net_movement


def test_fractional_cents_do_not_accumulate():
    f = build(payables=[pay(1, "0.005"), pay(2, "0.005")], opening="0.00")
    assert f.closing_balance == D("-0.02") or f.closing_balance == D("0.00")
    assert f.closing_balance == f.days[-1].closing


# --- the whole path, as the finance team will use it ---------------------

def test_a_report_can_be_built_from_aging_exports_end_to_end():
    """The route that works before QuickBooks is connected: two CSV exports and
    the bank balance, in - a day-by-day position, out."""
    from app.accounting import payables_from_csv, receivables_from_csv

    ap = ("Type,Date,Num,Name,Terms,Due Date,Class,Open Balance\n"
          'Bill,07/28/2026,A1,New Castle,Net 30,08/27/2026,250148,"4,182.60"\n'
          'Bill,08/12/2026,A2,New Castle,Net 30,09/11/2026,260000,"8,411.00"\n')
    ar = ("Type,Date,Num,Name,Terms,Due Date,Class,Open Balance\n"
          'Invoice,08/05/2026,1041,Ridgeview Condo,Net 30,09/04/2026,260000,"42,500.00"\n'
          'Invoice,08/28/2026,1046,Mahwah Property,Net 15,09/12/2026,260000,"7,900.00"\n')

    f = build_forecast(
        opening_balance=D("84500.00"),
        payables=payables_from_csv(ap),
        receivables=receivables_from_csv(ar),
        today=TODAY,
    )
    # The overdue bill lands on day one; the one due in the window lands there.
    assert f.days[0].out == D("4182.60")
    assert f.days[4].out == D("8411.00")
    # The overdue receivable is a collections item, not incoming cash.
    assert f.total_in == D("7900.00")
    assert len(f.overdue_receivables) == 1
    assert f.closing_balance == D("79806.40")
    assert f.closing_balance == D("84500.00") - D("12593.60") + D("7900.00")


def test_a_report_is_reproducible_from_its_stored_inputs():
    """Reports are stored as inputs and rebuilt on view, so two people opening
    the same report must always see the same numbers."""
    from app.accounting import (
        payable_from_dict, payable_to_dict, receivable_from_dict, receivable_to_dict,
    )
    payables = [pay(2, "1500.00"), pay(-4, "900.00", on_hold=True, hold_reason="over quote")]
    receivables = [rec(6, "8000.00", expected_date=TODAY + timedelta(days=8))]

    again = build_forecast(
        opening_balance=D("10000.00"),
        payables=[payable_from_dict(payable_to_dict(p)) for p in payables],
        receivables=[receivable_from_dict(receivable_to_dict(r)) for r in receivables],
        today=TODAY,
    )
    first = build(payables=payables, receivables=receivables)
    assert again.closing_balance == first.closing_balance
    assert [d.closing for d in again.days] == [d.closing for d in first.days]
    assert again.held_total == first.held_total

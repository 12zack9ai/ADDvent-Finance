"""The rolling 13-week cash flow forecast.

Modelled on the partner's framework and checked against the figures in his
draft. The tests worth writing are the judgements that decide whether the
report tells the truth on a bad quarter - run-rates, collectability, backlog,
overdue money - not whether addition works.
"""
from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from app import cashflow as cf
from app.cashflow import (
    CAT_INSURANCE, CAT_OVERHEAD, CAT_PAYROLL, CAT_RENT, CAT_SUPPLIER,
    CollectionAssumption, Payable, Receivable, build_forecast,
)

# A Tuesday, so the week-start logic has something to do.
TODAY = date(2026, 9, 1)
MONDAY = date(2026, 8, 31)


def pay(week=None, amount="1000.00", category=CAT_SUPPLIER, days=None, **kw):
    when = None
    if days is not None:
        when = TODAY + timedelta(days=days)
    elif week is not None:
        when = MONDAY + timedelta(days=7 * (week - 1) + 2)
    return Payable(due_date=when, vendor="ABC Supply", amount=D(amount),
                   category=category, **kw)


def build(payables=(), receivables=(), opening="100000.00", **kw):
    return build_forecast(opening_balance=D(opening), payables=list(payables),
                          receivables=list(receivables), as_of=TODAY, **kw)


# --- shape ---------------------------------------------------------------

def test_thirteen_weekly_buckets_starting_on_a_monday():
    f = build()
    assert len(f.weeks) == 13
    assert f.weeks[0].starts == MONDAY
    assert f.weeks[0].ends == MONDAY + timedelta(days=6)
    assert f.weeks[-1].ends == MONDAY + timedelta(days=6 + 7 * 12)
    assert [w.number for w in f.weeks] == list(range(1, 14))


def test_the_first_week_is_broken_out_day_by_day():
    """A weekly total hides whether the money leaves on Monday or Friday."""
    f = build(payables=[pay(days=2, amount="5000.00")])
    assert len(f.weeks[0].days) == 7
    assert len(f.weeks[1].days) == 0
    assert sum(d.out for d in f.weeks[0].days) == D("5000.00")


def test_thirteen_weeks_reaches_trouble_a_thirteen_day_window_would_miss():
    """The whole reason for thirteen weeks: on the draft this was modelled from,
    one entity looks healthy for ten weeks and is overdrawn by week thirteen."""
    f = build(opening="100000.00", run_rates={CAT_PAYROLL: D("36983")})
    assert not f.weeks[1].is_overdrawn
    assert f.goes_negative
    assert f.first_negative_week.number == 3


# --- run-rates: weeks with no bill are not free --------------------------

def test_a_week_with_no_bill_still_costs_payroll():
    f = build(run_rates={CAT_PAYROLL: D("36983")})
    assert f.weeks[5].by_category[CAT_PAYROLL] == D("36983.00")
    assert f.category_total(CAT_PAYROLL) == D("480779.00")     # 13 x 36,983


def test_a_real_bill_replaces_the_run_rate_for_that_week():
    """Week one has a real payroll bill of 41,000; it is not 41,000 plus 36,983."""
    f = build(payables=[pay(week=1, amount="41000.00", category=CAT_PAYROLL)],
              run_rates={CAT_PAYROLL: D("36983")})
    assert f.weeks[0].by_category[CAT_PAYROLL] == D("41000.00")
    assert f.weeks[1].by_category[CAT_PAYROLL] == D("36983.00")
    assert CAT_PAYROLL not in f.weeks[0].run_rate_categories
    assert CAT_PAYROLL in f.weeks[1].run_rate_categories


def test_a_credit_note_does_not_buy_a_week_without_overhead():
    """Real bills netting to zero or less means no bill, not a free week."""
    f = build(payables=[pay(week=1, amount="-114.98", category=CAT_OVERHEAD)],
              run_rates={CAT_OVERHEAD: D("6388")})
    assert f.weeks[0].by_category[CAT_OVERHEAD] == D("6388.00")


def test_supplier_payments_get_no_run_rate():
    """Job-driven spend. Inventing a weekly figure would be fabricating cost."""
    f = build(run_rates={CAT_SUPPLIER: D("50000")})
    assert f.weeks[4].by_category.get(CAT_SUPPLIER, D("0")) == D("0")
    assert f.category_total(CAT_SUPPLIER) == D("0.00")


def test_run_rates_are_only_applied_where_a_rate_was_given():
    f = build(run_rates={CAT_RENT: D("1385")})
    assert f.weeks[3].by_category[CAT_RENT] == D("1385.00")
    assert CAT_INSURANCE not in f.weeks[3].by_category


# --- receivables: weighted, not assumed ---------------------------------

def test_an_aged_invoice_is_collected_late_and_discounted():
    """61-90 days: four weeks out at 85 cents on the dollar, per the draft."""
    r = Receivable(customer="Vlasevski", amount=D("4255.00"),
                   invoice_date=TODAY - timedelta(days=75))
    f = build(receivables=[r])
    assert r.bucket == "61-90"
    assert r.expected_amount == D("3616.75")
    assert f.weeks[3].inflow == D("3616.75")


def test_money_over_ninety_days_is_not_scheduled_at_all():
    """Half of it, and no date anyone can defend. Counting it as cash is how a
    forecast flatters itself."""
    r = Receivable(customer="Eitner", amount=D("7200.00"),
                   invoice_date=TODAY - timedelta(days=200))
    f = build(receivables=[r])
    assert f.total_in == D("0.00")
    assert f.unscheduled_receivables == [r]
    assert r.expected_amount == D("3600.00")


def test_collection_assumptions_can_be_changed():
    r = Receivable(customer="X", amount=D("10000.00"), invoice_date=TODAY)
    f = build(receivables=[r], assumptions={
        "Current": CollectionAssumption(1, D("0.90")),
    })
    assert f.weeks[0].inflow == D("9000.00")


# --- backlog: real work, no date ----------------------------------------

def test_unbilled_backlog_produces_no_cash_until_a_week_is_assigned():
    """$2.3m of work in progress is not $2.3m of cash on any particular day."""
    r = Receivable(customer="Daul", amount=D("280420.00"), is_backlog=True)
    f = build(receivables=[r])
    assert f.total_in == D("0.00")
    assert f.backlog_total == D("280420.00")


def test_a_week_outside_the_horizon_leaves_backlog_unscheduled():
    r = Receivable(customer="Daul", amount=D("1000.00"), is_backlog=True, assigned_week=40)
    assert build(receivables=[r]).backlog_total == D("1000.00")


# --- progress billings: the week it goes out is not the week it lands ----

def test_a_draw_is_billed_in_one_week_and_collected_in_another():
    """The judgement a person supplies. On a nine-building roof the draw is
    submitted, the board meets, then the check is cut - weeks apart."""
    r = Receivable(customer="Daul", amount=D("280420.00"), is_backlog=True,
                   assigned_week=5, collect_weeks=4)
    f = build(receivables=[r])

    assert f.weeks[4].inflow == D("0.00")        # week 5: billed, no money
    assert f.weeks[8].inflow == D("280420.00")   # week 9: paid
    assert r.billed_week == 5
    assert r.expected_week == 9
    assert f.backlog == []


def test_a_draw_with_no_stated_lag_uses_the_reports_own_current_assumption():
    """Because the moment it is sent, that is exactly what it becomes."""
    r = Receivable(customer="Daul", amount=D("100000.00"), is_backlog=True,
                   assigned_week=2)
    f = build(receivables=[r], assumptions={
        cf.BUCKET_CURRENT: CollectionAssumption(3, D("1.00")),
    })
    assert r.expected_week == 5                  # billed week 2, +3 weeks
    assert f.weeks[4].inflow == D("100000.00")


def test_a_draw_collected_past_the_horizon_is_not_counted_as_cash():
    r = Receivable(customer="Daul", amount=D("100000.00"), is_backlog=True,
                   assigned_week=12, collect_weeks=6)
    f = build(receivables=[r])
    assert f.total_in == D("0.00")
    assert f.backlog_total == D("100000.00")


# --- retainage: earned, withheld, and not this quarter's money ----------

def test_retainage_comes_off_the_draw_and_is_reported_separately():
    """The condo association holds it until closeout. Forecasting it as cash
    is how a project looks solvent and the bank account does not."""
    r = Receivable(customer="Daul", amount=D("100000.00"), is_backlog=True,
                   assigned_week=1, collect_weeks=2, retainage_pct=D("10"))
    f = build(receivables=[r])

    assert r.retained_amount == D("10000.00")
    assert f.weeks[2].inflow == D("90000.00")    # only what they will release
    assert f.retained_total == D("10000.00")
    assert f.total_in == D("90000.00")


def test_retainage_on_an_unphased_draw_is_still_reported():
    """It is owed whether or not anyone has said when the draw goes out."""
    r = Receivable(customer="Daul", amount=D("50000.00"), is_backlog=True,
                   retainage_pct=D("10"))
    f = build(receivables=[r])
    assert f.retained_total == D("5000.00")
    assert f.backlog_total == D("50000.00")
    assert f.total_in == D("0.00")


def test_no_retainage_means_no_retainage_line():
    r = Receivable(customer="Daul", amount=D("50000.00"), is_backlog=True,
                   assigned_week=1, collect_weeks=1)
    f = build(receivables=[r])
    assert f.retained == []
    assert f.retained_total == D("0.00")
    assert f.weeks[1].inflow == D("50000.00")


def test_scheduled_draws_read_in_the_order_they_go_out():
    late = Receivable(customer="Daul", amount=D("10000.00"), is_backlog=True,
                      assigned_week=6, collect_weeks=1)
    early = Receivable(customer="Bergen", amount=D("20000.00"), is_backlog=True,
                       assigned_week=2, collect_weeks=1)
    f = build(receivables=[late, early])
    assert [r.customer for r in f.scheduled_draws] == ["Bergen", "Daul"]


# --- payables: overdue, held, beyond the horizon ------------------------

def test_an_overdue_bill_lands_in_week_one():
    f = build(payables=[pay(days=-40, amount="10000.00")])
    assert f.weeks[0].outflow == D("10000.00")
    assert len(f.overdue_payables) == 1


def test_a_held_invoice_is_listed_but_not_scheduled():
    f = build(payables=[pay(week=2, amount="9000.00", on_hold=True,
                            hold_reason="Billed over quote")])
    assert f.total_out == D("0.00")
    assert f.held_total == D("9000.00")


def test_a_bill_due_after_the_horizon_is_noted_separately():
    """The draft carries a bill due next August. It is real, and it is not this
    quarter's problem - but dropping it silently would be wrong."""
    f = build(payables=[pay(days=350, amount="15305.50")])
    assert f.total_out == D("0.00")
    assert [p.amount for p in f.beyond_horizon] == [D("15305.50")]


def test_a_bill_with_no_due_date_is_listed_not_ignored():
    f = build(payables=[Payable(due_date=None, vendor="X", amount=D("700"))])
    assert [p.amount for p in f.unscheduled_payables] == [D("700.00")]


# --- the balance and the floor ------------------------------------------

def test_the_balance_runs_week_to_week():
    f = build(payables=[pay(week=1, amount="20000.00"), pay(week=3, amount="5000.00")],
              receivables=[Receivable(customer="C", amount=D("30000"), invoice_date=TODAY)])
    assert f.weeks[0].closing == D("80000.00")
    assert f.weeks[2].closing == D("105000.00")     # 30,000 lands in week 3
    assert f.closing_balance == f.opening_balance + f.net_movement


def test_the_minimum_cash_target_is_a_floor_not_zero():
    """Crossing the floor is the warning; reaching zero is the emergency."""
    f = build(payables=[pay(week=2, amount="60000.00")], minimum_cash=D("100000"))
    assert not f.goes_negative
    assert f.first_below_target.number == 2
    assert f.weeks[1].below_target(D("100000"))


def test_no_floor_means_no_warning():
    f = build(payables=[pay(week=2, amount="60000.00")])
    assert f.first_below_target is None


def test_the_low_point_is_the_worst_week_not_the_last():
    f = build(payables=[pay(week=4, amount="90000.00")],
              receivables=[Receivable(customer="C", amount=D("120000"),
                                      invoice_date=TODAY - timedelta(days=100))])
    assert f.low_point.number >= 4


# --- categories ----------------------------------------------------------

def test_categories_report_their_own_weekly_row():
    f = build(payables=[pay(week=1, amount="41000.00", category=CAT_PAYROLL),
                        pay(week=2, amount="101295.39", category=CAT_SUPPLIER)])
    assert f.category_row(CAT_PAYROLL)[0] == D("41000.00")
    assert f.category_row(CAT_SUPPLIER)[1] == D("101295.39")
    assert set(f.used_categories) == {CAT_PAYROLL, CAT_SUPPLIER}


def test_an_unknown_category_falls_back_to_supplier_rather_than_vanishing():
    f = build(payables=[pay(week=1, amount="500.00", category="Nonsense")])
    assert f.category_total(CAT_SUPPLIER) == D("500.00")

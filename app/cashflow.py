"""The rolling 13-week cash flow forecast.

Built to the structure in the partner's framework rather than invented here: a
direct (cash-basis) forecast in weekly buckets, updated weekly, with the first
week broken out day by day. It is the construction-industry standard, and the
reason for thirteen weeks rather than thirteen days is that the interesting
news is usually in weeks 8-13. On the draft this was modelled from, one entity
looks healthy until week 10 and is $125,000 overdrawn by week 13 - a two-week
window ends before any of that appears.

As everywhere else in this system, **this file does arithmetic and nothing
else**: no fetching, no network, no decisions about where numbers come from.

Five judgements are built in, each of which changes the answer materially:

  * **Weeks with no bill on file are not free.** Payroll, insurance, rent and
    overhead continue whether or not a bill has been entered yet, so each of
    those categories carries a weekly run-rate that fills any week where no
    real bill exists. Without it the back half of the forecast reads as
    costing nothing, which is where a forecast does real damage.

  * **Receivables are weighted, not assumed.** Aged invoices are collected on a
    per-bucket delay with a collectability percentage - money over 90 days is
    not worth its face value, and counting it as such is how a forecast
    flatters itself.

  * **Backlog is not revenue until somebody dates it.** Unbilled contract value
    on jobs in progress is real work, but it produces no cash on a date nobody
    has assigned. It is listed, and excluded, until a person says which week.

  * **Overdue payables are due now.** They land in week one, because they still
    have to be paid.

  * **A minimum cash target is a floor, not zero.** Crossing it is the warning;
    reaching zero is the emergency.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Optional

ZERO = Decimal("0.00")
DEFAULT_WEEKS = 13
DEFAULT_DAILY_WEEKS = 1          # how many leading weeks are shown day by day

# --- outflow categories, in the order they are presented -----------------
CAT_PAYROLL = "Payroll (labor, burden & payroll taxes)"
CAT_SUPPLIER = "Subcontractor & supplier payments"
CAT_INSURANCE = "Insurance"
CAT_RENT = "Rent / facilities"
CAT_VEHICLE = "Vehicle & equipment"
CAT_LOAN = "Other loan payments"
CAT_TAX = "Estimated tax payments"
CAT_OVERHEAD = "Other overhead (office, admin, software)"
# Permits, deposits, filing fees, reimbursements. Not supplier payments, and
# they are the one outflow in this business that arrives with no invoice at
# all - so a forecast built from bills alone cannot see them.
CAT_CHECKS = "Permits, deposits and other checks"

CATEGORIES = [
    CAT_PAYROLL, CAT_SUPPLIER, CAT_CHECKS, CAT_INSURANCE, CAT_RENT,
    CAT_VEHICLE, CAT_LOAN, CAT_TAX, CAT_OVERHEAD,
]

# Categories that continue whether or not a bill has been entered. Supplier
# payments and tax are deliberately absent: they are job- and event-driven, so
# inventing a weekly figure for them would be fabricating spend.
RUN_RATE_CATEGORIES = [CAT_PAYROLL, CAT_INSURANCE, CAT_RENT, CAT_VEHICLE, CAT_LOAN, CAT_OVERHEAD]

# --- receivable aging buckets --------------------------------------------
BUCKET_CURRENT = "Current"
BUCKET_1_30 = "1-30"
BUCKET_31_60 = "31-60"
BUCKET_61_90 = "61-90"
BUCKET_90_PLUS = ">90"

BUCKETS = [BUCKET_CURRENT, BUCKET_1_30, BUCKET_31_60, BUCKET_61_90, BUCKET_90_PLUS]


def _money(value) -> Decimal:
    return (Decimal(value or 0)).quantize(Decimal("0.01"))


@dataclass
class CollectionAssumption:
    """How long a bucket takes to collect, and how much of it actually lands."""

    weeks_out: Optional[int]        # None = not scheduled at all
    collectability: Decimal         # 1.00 = all of it


def default_assumptions() -> dict[str, CollectionAssumption]:
    """The partner's draft figures. Editable per report; these are the start."""
    return {
        BUCKET_CURRENT: CollectionAssumption(3, Decimal("1.00")),
        BUCKET_1_30: CollectionAssumption(2, Decimal("1.00")),
        BUCKET_31_60: CollectionAssumption(3, Decimal("0.95")),
        BUCKET_61_90: CollectionAssumption(4, Decimal("0.85")),
        # Over 90 days: half of it, and no date anyone can defend.
        BUCKET_90_PLUS: CollectionAssumption(None, Decimal("0.50")),
    }


def bucket_for(invoice_date: Optional[date], as_of: date) -> str:
    if invoice_date is None:
        return BUCKET_CURRENT
    age = (as_of - invoice_date).days
    if age <= 0:
        return BUCKET_CURRENT
    if age <= 30:
        return BUCKET_1_30
    if age <= 60:
        return BUCKET_31_60
    if age <= 90:
        return BUCKET_61_90
    return BUCKET_90_PLUS


@dataclass
class Payable:
    """Money going out: a bill we owe."""

    due_date: Optional[date]
    vendor: str
    amount: Decimal
    category: str = CAT_SUPPLIER
    reference: str = ""
    job_number: str = ""
    entity: str = ""
    source: str = ""
    on_hold: bool = False
    hold_reason: str = ""
    discount_amount: Decimal = ZERO
    discount_deadline: Optional[date] = None


@dataclass
class Receivable:
    """Money coming in: a customer invoice, or unbilled backlog."""

    customer: str
    amount: Decimal
    invoice_date: Optional[date] = None
    due_date: Optional[date] = None
    reference: str = ""
    job_number: str = ""
    entity: str = ""
    source: str = ""
    memo: str = ""
    is_backlog: bool = False           # unbilled contract value on a live job
    # For backlog, `assigned_week` is the week the requisition goes OUT, not the
    # week the money comes in. On a progress-billed roof the two are weeks
    # apart: the draw is submitted, the association's board meets, the check is
    # cut. `collect_weeks` is that gap.
    assigned_week: Optional[int] = None   # a person has said which week to bill
    collect_weeks: Optional[int] = None   # weeks from billing to money in hand
    # Held back by the customer until closeout - typically 10% on a condo
    # contract. Real money, but not this quarter's money, so it is taken off
    # the draw and reported separately rather than being forecast.
    retainage_pct: Decimal = ZERO
    bucket: str = ""                   # filled in when the forecast is built
    expected_amount: Decimal = ZERO    # after collectability
    expected_week: Optional[int] = None
    billed_week: Optional[int] = None  # backlog only: when it goes out

    @property
    def retained_amount(self) -> Decimal:
        if self.retainage_pct <= ZERO:
            return ZERO
        return _money(self.amount * self.retainage_pct / Decimal(100))

    @property
    def net_of_retainage(self) -> Decimal:
        return _money(self.amount - self.retained_amount)


@dataclass
class Day:
    on: date
    out: Decimal = ZERO
    incoming: Decimal = ZERO
    payables: list[Payable] = field(default_factory=list)
    opening: Decimal = ZERO
    closing: Decimal = ZERO


@dataclass
class Week:
    number: int                        # 1-13
    starts: date
    ends: date
    opening: Decimal = ZERO
    closing: Decimal = ZERO
    inflow: Decimal = ZERO
    outflow: Decimal = ZERO
    by_category: dict[str, Decimal] = field(default_factory=dict)
    run_rate_categories: set[str] = field(default_factory=set)
    payables: list[Payable] = field(default_factory=list)
    receivables: list[Receivable] = field(default_factory=list)
    days: list[Day] = field(default_factory=list)

    @property
    def net(self) -> Decimal:
        return _money(self.inflow - self.outflow)

    @property
    def is_overdrawn(self) -> bool:
        return self.closing < ZERO

    def below_target(self, target: Decimal) -> bool:
        return target > ZERO and self.closing < target


@dataclass
class Forecast:
    as_of: date
    weeks: list[Week]
    opening_balance: Decimal
    minimum_cash: Decimal = ZERO
    run_rates: dict[str, Decimal] = field(default_factory=dict)
    assumptions: dict[str, CollectionAssumption] = field(default_factory=dict)
    entity: str = ""
    sources: list[str] = field(default_factory=list)

    overdue_payables: list[Payable] = field(default_factory=list)
    held_payables: list[Payable] = field(default_factory=list)
    unscheduled_payables: list[Payable] = field(default_factory=list)
    beyond_horizon: list[Payable] = field(default_factory=list)
    backlog: list[Receivable] = field(default_factory=list)
    retained: list[Receivable] = field(default_factory=list)
    unscheduled_receivables: list[Receivable] = field(default_factory=list)
    discounts: list[Payable] = field(default_factory=list)

    @property
    def total_in(self) -> Decimal:
        return _money(sum((w.inflow for w in self.weeks), ZERO))

    @property
    def total_out(self) -> Decimal:
        return _money(sum((w.outflow for w in self.weeks), ZERO))

    @property
    def net_movement(self) -> Decimal:
        return _money(self.total_in - self.total_out)

    @property
    def closing_balance(self) -> Decimal:
        return self.weeks[-1].closing if self.weeks else self.opening_balance

    @property
    def low_point(self) -> Optional[Week]:
        return min(self.weeks, key=lambda w: w.closing) if self.weeks else None

    @property
    def goes_negative(self) -> bool:
        low = self.low_point
        return low is not None and low.closing < ZERO

    @property
    def first_negative_week(self) -> Optional[Week]:
        """When the trouble starts, which is more useful than how bad it gets."""
        return next((w for w in self.weeks if w.closing < ZERO), None)

    @property
    def first_below_target(self) -> Optional[Week]:
        if self.minimum_cash <= ZERO:
            return None
        return next((w for w in self.weeks if w.closing < self.minimum_cash), None)

    @property
    def backlog_total(self) -> Decimal:
        return _money(sum((r.amount for r in self.backlog), ZERO))

    @property
    def retained_total(self) -> Decimal:
        """Money earned and withheld until closeout. Owed, but not soon."""
        return _money(sum((r.retained_amount for r in self.retained), ZERO))

    @property
    def scheduled_draws(self) -> list[Receivable]:
        """Progress billings a person has phased, in the order they go out."""
        placed = [
            r for w in self.weeks for r in w.receivables if r.is_backlog
        ]
        return sorted(placed, key=lambda r: (r.billed_week or 0, r.customer))

    @property
    def held_total(self) -> Decimal:
        return _money(sum((p.amount for p in self.held_payables), ZERO))

    @property
    def discount_savings(self) -> Decimal:
        return _money(sum((p.discount_amount for p in self.discounts), ZERO))

    def category_row(self, category: str) -> list[Decimal]:
        return [w.by_category.get(category, ZERO) for w in self.weeks]

    def category_total(self, category: str) -> Decimal:
        return _money(sum(self.category_row(category), ZERO))

    @property
    def used_categories(self) -> list[str]:
        return [c for c in CATEGORIES if self.category_total(c) != ZERO]


def week_start(as_of: date) -> date:
    """Weeks run Monday to Sunday, so a week ending is always a Sunday."""
    return as_of - timedelta(days=as_of.weekday())


def build_forecast(
    *,
    opening_balance,
    payables: Iterable[Payable],
    receivables: Iterable[Receivable],
    as_of: Optional[date] = None,
    weeks: int = DEFAULT_WEEKS,
    daily_weeks: int = DEFAULT_DAILY_WEEKS,
    run_rates: Optional[dict[str, Decimal]] = None,
    minimum_cash=ZERO,
    assumptions: Optional[dict[str, CollectionAssumption]] = None,
    entity: str = "",
    sources: Optional[list[str]] = None,
) -> Forecast:
    today = as_of or date.today()
    start = week_start(today)
    rates = {k: _money(v) for k, v in (run_rates or {}).items() if _money(v) != ZERO}
    rules = assumptions or default_assumptions()

    buckets = [
        Week(number=i + 1,
             starts=start + timedelta(days=7 * i),
             ends=start + timedelta(days=7 * i + 6))
        for i in range(weeks)
    ]
    horizon_end = buckets[-1].ends

    f = Forecast(
        as_of=today, weeks=buckets, opening_balance=_money(opening_balance),
        minimum_cash=_money(minimum_cash), run_rates=rates, assumptions=rules,
        entity=entity, sources=sources or [],
    )

    _place_payables(f, payables, today, horizon_end)
    _place_receivables(f, receivables, today)
    _apply_run_rates(f)
    _expand_days(f, daily_weeks, today)
    _run_the_balance(f)
    return f


# --- placement ------------------------------------------------------------

def _week_index(f: Forecast, when: date) -> Optional[int]:
    for i, week in enumerate(f.weeks):
        if week.starts <= when <= week.ends:
            return i
    return None


def _place_payables(f: Forecast, payables: Iterable[Payable], today: date, horizon_end: date) -> None:
    for p in payables:
        p.amount = _money(p.amount)
        if p.category not in CATEGORIES:
            p.category = CAT_SUPPLIER
        if p.on_hold:
            f.held_payables.append(p)
            continue
        if p.due_date is None:
            f.unscheduled_payables.append(p)
            continue
        if p.discount_deadline and today <= p.discount_deadline <= horizon_end:
            f.discounts.append(p)
        if p.due_date > horizon_end:
            f.beyond_horizon.append(p)
            continue
        index = 0 if p.due_date < f.weeks[0].starts else _week_index(f, p.due_date)
        if index is None:
            continue
        if p.due_date < today:
            f.overdue_payables.append(p)
        week = f.weeks[index]
        week.payables.append(p)
        week.outflow += p.amount
        week.by_category[p.category] = week.by_category.get(p.category, ZERO) + p.amount

    f.overdue_payables.sort(key=lambda p: p.due_date or today)
    f.discounts.sort(key=lambda p: p.discount_deadline or today)


def _place_receivables(f: Forecast, receivables: Iterable[Receivable], today: date) -> None:
    for r in receivables:
        r.amount = _money(r.amount)

        if r.is_backlog:
            # A progress billing. Real work, and nobody but a person knows when
            # it gets requisitioned - that is the judgement this asks for.
            if r.retainage_pct > ZERO:
                f.retained.append(r)

            if r.assigned_week is None:
                f.backlog.append(r)
                continue
            if not 1 <= r.assigned_week <= len(f.weeks):
                f.backlog.append(r)
                continue

            r.billed_week = r.assigned_week
            # Billed in one week, paid in another. Default the gap to whatever
            # the report assumes for a current invoice, since that is exactly
            # what this becomes the moment it is sent.
            lag = r.collect_weeks
            if lag is None:
                current = f.assumptions.get(BUCKET_CURRENT)
                lag = current.weeks_out if current and current.weeks_out else 0
            index = r.assigned_week - 1 + max(lag, 0)
            if index >= len(f.weeks):
                # Billed inside the horizon, collected past the end of it.
                r.expected_amount = r.net_of_retainage
                r.expected_week = None
                f.backlog.append(r)
                continue

            r.expected_amount = r.net_of_retainage
            r.expected_week = index + 1
            week = f.weeks[index]
            week.receivables.append(r)
            week.inflow += r.expected_amount
            continue

        r.bucket = r.bucket or bucket_for(r.invoice_date, today)
        rule = f.assumptions.get(r.bucket) or CollectionAssumption(None, Decimal("1.00"))
        r.expected_amount = _money(r.amount * rule.collectability)

        if rule.weeks_out is None:
            # No defensible date - shown as a collections problem, not as cash.
            f.unscheduled_receivables.append(r)
            continue

        index = min(max(rule.weeks_out - 1, 0), len(f.weeks) - 1)
        r.expected_week = index + 1
        week = f.weeks[index]
        week.receivables.append(r)
        week.inflow += r.expected_amount


def _apply_run_rates(f: Forecast) -> None:
    """Fill categories that continue regardless of what has been billed.

    A week uses its real bills when it has any; otherwise the run-rate. A
    category whose real bills net to zero or less for that week - a credit note,
    say - counts as having none, because a refund is not a week without payroll.
    """
    for week in f.weeks:
        for category, rate in f.run_rates.items():
            if category not in RUN_RATE_CATEGORIES:
                continue
            booked = week.by_category.get(category, ZERO)
            if booked > ZERO:
                continue
            week.by_category[category] = rate
            week.outflow = _money(week.outflow - booked + rate)
            week.run_rate_categories.add(category)


def _expand_days(f: Forecast, daily_weeks: int, today: date) -> None:
    """Break the leading weeks out day by day. The near term is where a
    payment run is actually scheduled, and a weekly total hides whether the
    money leaves on Monday or Friday."""
    for week in f.weeks[:max(0, daily_weeks)]:
        for offset in range(7):
            on = week.starts + timedelta(days=offset)
            day = Day(on=on)
            for p in week.payables:
                landed = p.due_date if p.due_date and p.due_date >= week.starts else week.starts
                if landed == on:
                    day.payables.append(p)
                    day.out += p.amount
            week.days.append(day)


def _run_the_balance(f: Forecast) -> None:
    running = f.opening_balance
    for week in f.weeks:
        week.opening = _money(running)
        week.inflow = _money(week.inflow)
        week.outflow = _money(week.outflow)
        running = week.opening + week.inflow - week.outflow
        week.closing = _money(running)

        day_running = week.opening
        for day in week.days:
            day.opening = _money(day_running)
            day.out = _money(day.out)
            day_running = day.opening - day.out
            day.closing = _money(day_running)

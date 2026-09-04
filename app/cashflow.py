"""The 13-day cash flow forecast.

What has to go out, what is expected in, and what the bank balance does across
the next thirteen days. Assembled by hand today; the point of this file is that
it stops being assembled by hand.

The one rule from the rest of this system applies here too: **this file does
arithmetic and nothing else.** It takes payables, receivables and an opening
balance, and returns a day-by-day position. It does not fetch anything, decide
anything, or talk to QuickBooks - so the numbers are reproducible and the tests
below are the specification.

Two judgements are deliberately built in, because leaving them out produces a
report that is technically correct and practically misleading:

  * **Overdue money still has to be paid.** An invoice that was due last
    Tuesday is not absent from this week's cash needs; it is the most urgent
    thing in it. Anything already past due lands on day one rather than being
    silently excluded for falling outside the window.

  * **Receivables are what you expect, not what you are owed.** A customer who
    reliably pays at day 45 on net-30 terms should not be counted as arriving
    on day 30. Expected dates can be shifted per customer, and the shift is
    shown rather than buried.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Optional

ZERO = Decimal("0.00")
DEFAULT_HORIZON_DAYS = 13


def _money(value) -> Decimal:
    return (Decimal(value or 0)).quantize(Decimal("0.01"))


@dataclass
class Payable:
    """Money going out: a bill we owe."""

    due_date: Optional[date]
    vendor: str
    amount: Decimal
    reference: str = ""
    job_number: str = ""
    source: str = ""                    # where this came from, for the audit trail
    on_hold: bool = False               # flagged by the three-way match
    hold_reason: str = ""
    discount_amount: Decimal = ZERO     # e.g. 2/10 net 30
    discount_deadline: Optional[date] = None

    @property
    def is_scheduled(self) -> bool:
        return self.due_date is not None


@dataclass
class Receivable:
    """Money coming in: a customer invoice."""

    due_date: Optional[date]
    customer: str
    amount: Decimal
    reference: str = ""
    job_number: str = ""
    source: str = ""
    expected_date: Optional[date] = None   # when it will REALLY arrive
    days_late_typical: int = 0             # why expected_date differs from due

    @property
    def arrives_on(self) -> Optional[date]:
        return self.expected_date or self.due_date


@dataclass
class Day:
    on: date
    out: Decimal = ZERO
    incoming: Decimal = ZERO
    payables: list[Payable] = field(default_factory=list)
    receivables: list[Receivable] = field(default_factory=list)
    opening: Decimal = ZERO
    closing: Decimal = ZERO

    @property
    def net(self) -> Decimal:
        return self.incoming - self.out

    @property
    def is_overdrawn(self) -> bool:
        return self.closing < ZERO


@dataclass
class Forecast:
    start: date
    end: date
    opening_balance: Decimal
    days: list[Day]
    overdue_payables: list[Payable] = field(default_factory=list)
    overdue_receivables: list[Receivable] = field(default_factory=list)
    unscheduled_payables: list[Payable] = field(default_factory=list)
    unscheduled_receivables: list[Receivable] = field(default_factory=list)
    held_payables: list[Payable] = field(default_factory=list)
    discounts: list[Payable] = field(default_factory=list)
    generated_at: Optional[date] = None
    sources: list[str] = field(default_factory=list)

    # --- headline numbers ----------------------------------------------
    @property
    def total_out(self) -> Decimal:
        return _money(sum((d.out for d in self.days), ZERO))

    @property
    def total_in(self) -> Decimal:
        return _money(sum((d.incoming for d in self.days), ZERO))

    @property
    def closing_balance(self) -> Decimal:
        return self.days[-1].closing if self.days else self.opening_balance

    @property
    def net_movement(self) -> Decimal:
        return _money(self.total_in - self.total_out)

    @property
    def low_point(self) -> Optional[Day]:
        """The worst day in the window. The number that decides whether this
        report is interesting - a healthy closing balance can still hide a day
        where the account goes under."""
        return min(self.days, key=lambda d: d.closing) if self.days else None

    @property
    def goes_negative(self) -> bool:
        low = self.low_point
        return low is not None and low.closing < ZERO

    @property
    def discount_savings(self) -> Decimal:
        return _money(sum((p.discount_amount for p in self.discounts), ZERO))

    @property
    def held_total(self) -> Decimal:
        return _money(sum((p.amount for p in self.held_payables), ZERO))


def build_forecast(
    *,
    opening_balance,
    payables: Iterable[Payable],
    receivables: Iterable[Receivable],
    today: Optional[date] = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    sources: Optional[list[str]] = None,
) -> Forecast:
    """Lay the money out day by day and run the balance forward."""
    start = today or date.today()
    end = start + timedelta(days=horizon_days - 1)
    opening = _money(opening_balance)

    days = [Day(on=start + timedelta(days=i)) for i in range(horizon_days)]
    index = {d.on: d for d in days}

    forecast = Forecast(
        start=start, end=end, opening_balance=opening, days=days,
        generated_at=start, sources=sources or [],
    )

    for payable in payables:
        payable.amount = _money(payable.amount)
        if payable.on_hold:
            # Held by the three-way match. Real money, but not payable yet, so
            # counting it as leaving this week would overstate the outflow.
            forecast.held_payables.append(payable)
            continue
        if payable.due_date is None:
            forecast.unscheduled_payables.append(payable)
            continue
        if payable.discount_deadline and start <= payable.discount_deadline <= end:
            forecast.discounts.append(payable)
        if payable.due_date < start:
            # Overdue. This is money that must go out now, not money that has
            # gone away, so it lands on day one where somebody will see it.
            forecast.overdue_payables.append(payable)
            days[0].payables.append(payable)
            days[0].out += payable.amount
        elif payable.due_date in index:
            day = index[payable.due_date]
            day.payables.append(payable)
            day.out += payable.amount

    for receivable in receivables:
        receivable.amount = _money(receivable.amount)
        arrives = receivable.arrives_on
        if arrives is None:
            forecast.unscheduled_receivables.append(receivable)
            continue
        if arrives < start:
            # Overdue receipts are NOT assumed to arrive today. They are the
            # collections list, and counting them as incoming is how a forecast
            # tells you that you are fine when you are not.
            forecast.overdue_receivables.append(receivable)
            continue
        if arrives in index:
            day = index[arrives]
            day.receivables.append(receivable)
            day.incoming += receivable.amount

    running = opening
    for day in days:
        day.opening = _money(running)
        day.out = _money(day.out)
        day.incoming = _money(day.incoming)
        running = day.opening + day.incoming - day.out
        day.closing = _money(running)

    forecast.overdue_payables.sort(key=lambda p: (p.due_date or start))
    forecast.overdue_receivables.sort(key=lambda r: (r.arrives_on or start))
    forecast.discounts.sort(key=lambda p: (p.discount_deadline or start))
    return forecast

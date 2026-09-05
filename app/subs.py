"""Subcontractors: where each one stands, and who has been waiting longest.

The material side asks "did this vendor charge what they quoted?", line by
line, one invoice at a time. None of that applies here. A subcontractor's
check request is a claim on a number already agreed - *we are 30% done,
release 30%* - so there is no price to check. The question is cumulative:

    everything approved so far  +  this request   <=   the contract?

Any single request can look perfectly reasonable and the seventh still takes
the sub past what they were awarded. Only the running total sees that, which
is why the position below is computed across every request rather than per
document.

**Overages are not errors.** Subs do get more than they quoted, for extras, and
a system that treated that as a fault would be wrong about the business. So an
overage is reported, and approving one is allowed - it just has to be done
knowingly, by somebody who says why.

Pure Python over rows already loaded. Decimal throughout, no model calls, no
queries of its own.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Iterable, Optional

from app.matching import norm_vendor, vendor_matches
from app.models import (
    CHECK_APPROVED,
    CHECK_PAID,
    CHECK_REQUESTED,
    CheckRequest,
    Job,
    Quote,
)

ZERO = Decimal("0")

# How long a sub has been waiting, in the words a person would use. The bands
# are what a conversation about paying people actually sounds like - "anything
# over two weeks", "the ones from last month" - not evenly spaced buckets.
AGE_BANDS = (
    (0, 7, "This week"),
    (8, 14, "Over a week"),
    (15, 30, "Over two weeks"),
    (31, 10_000, "Over a month"),
)


def band_for(days: int) -> str:
    for low, high, label in AGE_BANDS:
        if low <= days <= high:
            return label
    return AGE_BANDS[-1][2]


@dataclass
class Position:
    """Where one subcontractor stands on one job."""

    vendor: str
    contract: Decimal = ZERO
    change_orders: Decimal = ZERO
    approved: Decimal = ZERO        # signed off, including already paid
    paid: Decimal = ZERO
    waiting: Decimal = ZERO         # requested, nobody has decided yet
    subcontract: Optional[Quote] = None
    requests: list[CheckRequest] = field(default_factory=list)

    @property
    def has_contract(self) -> bool:
        return self.subcontract is not None and self.contract > ZERO

    @property
    def awarded(self) -> Decimal:
        """The contract plus written extras. What they may be paid in total."""
        return self.contract + self.change_orders

    @property
    def remaining(self) -> Decimal:
        """Left on the contract after what has been approved."""
        return self.awarded - self.approved

    @property
    def overage(self) -> Decimal:
        """Approved beyond the award. Real, and not necessarily wrong."""
        return max(self.approved - self.awarded, ZERO)

    @property
    def committed(self) -> Decimal:
        """Approved plus still waiting - what this sub will have drawn if
        everything on the table is paid."""
        return self.approved + self.waiting

    @property
    def would_exceed(self) -> Decimal:
        """How far past the award the open requests would take them."""
        return max(self.committed - self.awarded, ZERO)

    @property
    def percent_drawn(self) -> Optional[int]:
        if self.awarded <= ZERO:
            return None
        return int((self.approved / self.awarded * 100).quantize(Decimal("1")))

    @property
    def open_requests(self) -> list[CheckRequest]:
        return [r for r in self.requests if r.is_open]

    def oldest_wait(self, today: Optional[date] = None) -> int:
        return max((r.days_waiting(today) for r in self.open_requests), default=0)


def positions(job: Job) -> list[Position]:
    """Every subcontractor on a job, largest contract first.

    Grouped by `vendor_matches`, the same answer the rest of the app gives, so
    a sub who signs their contract one way and their requisitions another is
    one sub rather than two half-visible ones.
    """
    found: list[Position] = []

    def bucket(vendor: str) -> Position:
        for position in found:
            if _same(position.vendor, vendor):
                return position
        position = Position(vendor=(vendor or "").strip() or "Unknown subcontractor")
        found.append(position)
        return position

    for contract in job.subcontracts:
        position = bucket(contract.vendor)
        position.subcontract = position.subcontract or contract
        position.contract += contract.total or ZERO

    for change_order in job.change_orders:
        if not change_order.is_live:
            continue
        # Only extras belonging to a sub already on this job. A change order
        # for the roofing supplier has nothing to do with what a sub may draw.
        for position in found:
            if _same(position.vendor, change_order.vendor):
                position.change_orders += change_order.amount or ZERO
                break

    for request in job.check_requests:
        position = bucket(request.vendor)
        position.requests.append(request)
        amount = request.amount or ZERO
        if request.status == CHECK_REQUESTED:
            position.waiting += amount
        elif request.status in (CHECK_APPROVED, CHECK_PAID):
            position.approved += amount
            if request.status == CHECK_PAID:
                position.paid += amount

    for position in found:
        position.requests.sort(key=lambda r: (r.waiting_since, r.id))
    found.sort(key=lambda p: (-p.awarded, -p.committed, p.vendor.lower()))
    return found


def position_for(job: Job, vendor: str) -> Position:
    """One sub's position, or an empty one if they are not on this job yet."""
    for position in positions(job):
        if _same(position.vendor, vendor):
            return position
    return Position(vendor=(vendor or "").strip() or "Unknown subcontractor")


def _same(a: str, b: str) -> bool:
    na, nb = norm_vendor(a), norm_vendor(b)
    if not na or not nb:
        return not na and not nb
    return vendor_matches(a, b)


# --- approving one -------------------------------------------------------

@dataclass
class Verdict:
    """What approving this request would do, before anybody does it."""

    position: Position
    request: CheckRequest
    exceeds_by: Decimal = ZERO
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    @property
    def can_approve(self) -> bool:
        return not self.blockers

    @property
    def over_contract(self) -> bool:
        return self.exceeds_by > ZERO


def check(job: Job, request: CheckRequest) -> Verdict:
    """The one question worth asking about a check request.

    Not "is this priced correctly" - there are no prices. It is: with this one
    approved, has this sub now been approved for more than they were awarded?
    """
    position = position_for(job, request.vendor)
    verdict = Verdict(position=position, request=request)

    if not position.has_contract:
        verdict.blockers.append(
            f"No subcontract on file for {request.vendor or 'this subcontractor'} "
            f"on job {job.job_number}, so there is nothing to check this against. "
            f"File their contract first."
        )
        return verdict

    would_be = position.approved + (request.amount or ZERO)
    verdict.exceeds_by = max(would_be - position.awarded, ZERO)

    verdict.reasons.append(
        f"Approved to date {_fmt(position.approved)} of {_fmt(position.awarded)}"
        + (f" (contract {_fmt(position.contract)} plus {_fmt(position.change_orders)} "
           f"in extras)" if position.change_orders else "")
        + "."
    )

    if verdict.exceeds_by > ZERO:
        verdict.blockers.append(
            f"This would take {request.vendor} to {_fmt(would_be)}, which is "
            f"{_fmt(verdict.exceeds_by)} past their {_fmt(position.awarded)}. "
            f"That happens - extras get agreed on site and papered later - but "
            f"somebody has to say so before the check goes out."
        )
    else:
        verdict.reasons.append(
            f"Leaves {_fmt(position.awarded - would_be)} on the contract."
        )
    return verdict


def _fmt(value: Optional[Decimal]) -> str:
    if value is None:
        return "$0.00"
    return f"${value.quantize(Decimal('0.01')):,}"


# --- the queue -----------------------------------------------------------

@dataclass
class Waiting:
    """One open request, ready to be listed by how long it has been sitting."""

    request: CheckRequest
    job: Job
    days: int
    position: Position

    @property
    def band(self) -> str:
        return band_for(self.days)

    @property
    def over_contract(self) -> bool:
        return self.position.would_exceed > ZERO


def queue(jobs: Iterable[Job], today: Optional[date] = None) -> list[Waiting]:
    """Every open check request, longest wait first.

    Longest wait first and nothing else. Not by amount, not by job, not by
    whether anything is flagged - those orderings all have the same failure,
    which is that the request nobody has looked at stays the request nobody
    has looked at. The person this list is for is being telephoned by a
    subcontractor who wants to know where their money is, and the only
    ordering that answers that is oldest first.
    """
    on = today or date.today()
    rows: list[Waiting] = []
    for job in jobs:
        by_vendor = {norm_vendor(p.vendor): p for p in positions(job)}
        for request in job.check_requests:
            if not request.is_open:
                continue
            position = by_vendor.get(norm_vendor(request.vendor)) or Position(
                vendor=request.vendor
            )
            rows.append(Waiting(
                request=request, job=job,
                days=request.days_waiting(on), position=position,
            ))
    rows.sort(key=lambda w: (-w.days, -(w.request.amount or ZERO)))
    return rows


def total_waiting(rows: Iterable[Waiting]) -> Decimal:
    return sum(((w.request.amount or ZERO) for w in rows), ZERO)

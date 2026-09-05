"""The check queue: who needs a check cut, and how long they have waited.

Deliberately not a subcontractor feature. Zack, correcting an earlier version
that assumed it was: *"not all checks are for subs and whatever, like there's
multiple reasons for check request, permits yes for jobs but not for subs."*

So a check request is a payment request and nothing narrower - a permit fee, a
deposit on an order, a reimbursement, a sub draw. Every one of them belongs to
a job, because that is how this business is organised and because it is what
puts a permit fee into that job's costing. What they share, and the only reason
they are one queue, is the question: who is waiting, and how long?

A subcontractor's *invoice* is not one of these. It goes through the invoice
pipeline like every other invoice, checked against their subcontract. Keeping
the two apart is what lets a permit exist at all - the earlier version could
not record one, because every check request had to be a draw against a
contract.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable, Optional

from app.models import CHECK_REQUESTED, CheckRequest

ZERO = Decimal("0")

# How long somebody has been waiting, in the words a person would use. These
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
class Waiting:
    """One open request, ready to be listed by how long it has been sitting."""

    request: CheckRequest
    days: int

    @property
    def band(self) -> str:
        return band_for(self.days)

    @property
    def job_number(self) -> str:
        return self.request.job.job_number


def queue(requests: Iterable[CheckRequest], today: Optional[date] = None) -> list[Waiting]:
    """Every open check request, longest wait first.

    Longest wait first and nothing else. Not by amount, not by job, not by
    purpose - those orderings all share one failure, which is that the request
    nobody has looked at stays the request nobody has looked at. The person
    this list is for is being telephoned by somebody who wants to know where
    their money is, and the only ordering that answers that is oldest first.
    """
    on = today or date.today()
    rows = [
        Waiting(request=r, days=r.days_waiting(on))
        for r in requests if r.status == CHECK_REQUESTED
    ]
    rows.sort(key=lambda w: (-w.days, -(w.request.amount or ZERO)))
    return rows


def total_waiting(rows: Iterable[Waiting]) -> Decimal:
    return sum(((w.request.amount or ZERO) for w in rows), ZERO)

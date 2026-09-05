"""What a job actually cost, and what it made.

Everything else here asks whether one document is right. This asks the only
question the owner actually cares about at the end: **we charged them X, it
cost us Y, what happened?**

The answer is already sitting in the database in pieces - vendor invoices on
one page, subcontractor draws on another - and nobody has ever added them up,
because adding them up by hand means opening two systems and a folder of
receipts. So this adds them up.

**On what counts as cost.** A bill that has been approved is money we have
agreed to pay, so it is cost. A bill still sitting in the review queue is a
claim, not yet a cost - it may be wrong, that is the entire point of this
system - and a rejected one is not cost at all. But a report that silently
ignored everything unapproved would understate a live job by whatever is in
the queue that week, which is the exact way a job looks profitable right up
until the last invoices clear. So both are reported: what is agreed, and what
is still on the table, separately and in the same place.

**Two numbers a person supplies**, because they cannot be read off anything
that arrives here: what we charged the customer, and what our own crews cost.
The second is deliberately optional - Zack's point is that a fully subbed-out
job needs no labour figure at all, and on those jobs this report is complete
without anyone doing anything.

Pure Python over rows already loaded. Decimal throughout.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from app import subs
from app.matching import vendor_matches
from app.models import (
    APPROVAL_APPROVED,
    APPROVAL_PAID,
    APPROVAL_REJECTED,
    CHECK_APPROVED,
    CHECK_PAID,
    CHECK_REQUESTED,
    Job,
)

ZERO = Decimal("0")


@dataclass
class Bucket:
    """One line of the cost stack."""

    key: str
    label: str
    agreed: Decimal = ZERO      # approved or paid: money we are committed to
    pending: Decimal = ZERO     # claimed, nobody has decided yet
    count: int = 0
    note: str = ""

    @property
    def worst_case(self) -> Decimal:
        return self.agreed + self.pending

    @property
    def has_anything(self) -> bool:
        return bool(self.count) or self.agreed != ZERO or self.pending != ZERO


@dataclass
class Costing:
    job: Job
    buckets: list[Bucket] = field(default_factory=list)
    revenue: Decimal = ZERO
    labour: Decimal = ZERO
    labour_given: bool = False
    purchases_captured: bool = False

    def bucket(self, key: str) -> Optional[Bucket]:
        return next((b for b in self.buckets if b.key == key), None)

    @property
    def cost(self) -> Decimal:
        """What the job has cost us, on what has actually been agreed."""
        return sum((b.agreed for b in self.buckets), ZERO) + self.labour

    @property
    def pending(self) -> Decimal:
        return sum((b.pending for b in self.buckets), ZERO)

    @property
    def worst_case_cost(self) -> Decimal:
        """If everything still in the queue is approved as claimed."""
        return self.cost + self.pending

    @property
    def has_revenue(self) -> bool:
        return self.revenue > ZERO

    @property
    def margin(self) -> Decimal:
        return self.revenue - self.cost

    @property
    def worst_case_margin(self) -> Decimal:
        return self.revenue - self.worst_case_cost

    @property
    def margin_pct(self) -> Optional[Decimal]:
        if self.revenue <= ZERO:
            return None
        return (self.margin / self.revenue * 100).quantize(Decimal("0.1"))

    @property
    def is_complete(self) -> bool:
        """Is this report actually telling the whole story?

        Deliberately conservative. A margin figure that quietly omits the crew
        or a stack of uncaptured receipts is worse than no margin figure, so
        the report says which parts are missing rather than presenting a number
        as final when it is not.
        """
        return not self.gaps

    @property
    def gaps(self) -> list[str]:
        missing: list[str] = []
        if not self.has_revenue:
            missing.append("what we charged the customer has not been entered")
        if not self.purchases_captured:
            missing.append(
                "receipts and card purchases are not captured yet, so anything "
                "bought over the counter is missing"
            )
        if not self.labour_given:
            missing.append(
                "our own labour has not been entered — leave it out on a job "
                "that was fully subbed"
            )
        if self.pending > ZERO:
            missing.append(
                "there is money still waiting on a decision, shown separately below"
            )
        return missing


def build(job: Job) -> Costing:
    """Add the job up: material, subcontract, purchases, labour, revenue."""
    report = Costing(job=job)

    # Split by who sent it, not by what kind of document it is. Both are
    # invoices and both went through the same pipeline; a vendor holding a
    # subcontract on this job is a subcontractor, and everybody else is a
    # supplier.
    material = Bucket("material", "Material and vendor invoices")
    sub = Bucket("subcontract", "Subcontractors")
    sub_vendors = [p.vendor for p in subs.positions(job)]

    for invoice in job.invoices:
        if invoice.approval_status == APPROVAL_REJECTED:
            continue
        bucket = sub if any(
            vendor_matches(v, invoice.vendor) for v in sub_vendors
        ) else material
        amount = invoice.total or ZERO
        bucket.count += 1
        if invoice.approval_status in (APPROVAL_APPROVED, APPROVAL_PAID):
            bucket.agreed += amount
        else:
            bucket.pending += amount

    report.buckets.append(material)
    report.buckets.append(sub)

    # Checks that are not invoices at all: permits, deposits, fees. Real cost
    # on the job, and invisible everywhere else in this system.
    written = Bucket("checks", "Permits, deposits and other checks")
    for request in job.check_requests:
        amount = request.amount or ZERO
        if request.status in (CHECK_APPROVED, CHECK_PAID):
            written.agreed += amount
            written.count += 1
        elif request.status == CHECK_REQUESTED:
            written.pending += amount
            written.count += 1
    report.buckets.append(written)

    # Card swipes, counter purchases, fuel. Nothing captures these yet, so the
    # bucket exists and is empty and says so - which is the honest shape. A
    # report that simply omitted the category would read as complete.
    purchases = Bucket(
        "purchases", "Receipts and card purchases",
        note="Not captured yet — anything bought over the counter is missing.",
    )
    report.buckets.append(purchases)
    report.purchases_captured = False

    report.labour = job.labour_cost or ZERO
    report.labour_given = job.labour_cost is not None
    report.revenue = job.contract_amount or ZERO
    return report

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

**One number a person supplies**, and only one: our own crews. Zack:
*"job costing should also be able to pull total billed and collected out of
QuickBooks. Not needed for manual entry. Only manual entry is our men. Labor
our hours cost if the guys who actually worked. When it isn't fully subbed
out."*

That is the right division. QuickBooks is the book of record for money and
already holds every customer invoice and every payment against the
Customer:Job, so asking a person to retype what we billed is asking them to
copy a number out of one system into another and be blamed when the two
disagree. Which of our men were on the roof and for how long is the one thing
it cannot answer, because nobody has ever told it - and on a fully subbed job
there is no answer to give, so blank stays a real answer.

Until the connector exists the billing figures can still be typed, and the
report says which of the two happened. A number nobody can trace is worse than
no number.

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
    JOB_LOST,
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
    revenue: Decimal = ZERO           # billed to the customer
    collected: Decimal = ZERO         # of that, what has actually arrived
    collected_known: bool = False
    billing_synced: bool = False      # came from QuickBooks, not a person
    labour: Decimal = ZERO
    labour_given: bool = False
    labour_hours: Optional[Decimal] = None
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
    def billed(self) -> Decimal:
        return self.revenue

    @property
    def outstanding(self) -> Decimal:
        """Billed and not yet in the bank."""
        return self.revenue - self.collected

    @property
    def labour_rate(self) -> Optional[Decimal]:
        if not self.labour or not self.labour_hours or self.labour_hours <= ZERO:
            return None
        return (self.labour / self.labour_hours).quantize(Decimal("0.01"))

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
            missing.append(
                "what we billed the customer is not known yet — it comes from "
                "QuickBooks, which is not connected"
            )
        elif not self.billing_synced:
            missing.append(
                "what we billed was typed in by hand rather than read from "
                "QuickBooks, so it is only as current as the day somebody typed it"
            )
        if not self.collected_known:
            missing.append(
                "how much of it has actually been collected is not known yet"
            )
        if not self.purchases_captured:
            missing.append(
                "no receipts have come in for this job, so anything bought "
                "over the counter is missing"
            )
        if not self.labour_given:
            missing.append(
                "our own crew's hours and cost have not been entered — leave "
                "them out on a job that was fully subbed"
            )
        elif self.labour_hours is None:
            missing.append(
                "our crew's cost was entered without the hours behind it, so "
                "nobody can check the rate"
            )
        if self.job.outcome == JOB_LOST:
            missing.append(
                "we did not get this job, so everything spent on it is a loss "
                "rather than a cost"
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

    # Card swipes, counter purchases, fuel - the receipt department. Already
    # paid at the till before anybody here saw them, so there is nothing
    # pending and nothing to approve: every one of them is cost.
    purchases = Bucket("purchases", "Receipts and card purchases")
    for purchase in job.purchases:
        purchases.agreed += purchase.total or ZERO
        purchases.count += 1
    if not purchases.count:
        purchases.note = "None on this job."
    report.buckets.append(purchases)
    # Captured the moment anything has come in for this job. It is not a claim
    # that every receipt was sent - nobody can know that - only that this
    # category is no longer structurally missing.
    report.purchases_captured = bool(purchases.count)

    report.labour = job.labour_cost or ZERO
    report.labour_given = job.labour_cost is not None
    report.labour_hours = job.labour_hours
    report.revenue = job.contract_amount or ZERO
    report.collected = job.collected_amount or ZERO
    report.collected_known = job.collected_amount is not None
    report.billing_synced = job.billing_is_synced
    return report

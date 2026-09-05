"""The check queue: who needs a check cut, and how long they have waited.

Deliberately not a subcontractor feature. Zack, correcting an earlier version
that assumed it was: *"not all checks are for subs and whatever, like there's
multiple reasons for check request, permits yes for jobs but not for subs."*

So a check request is a payment request and nothing narrower - a permit fee, a
deposit on an order, a reimbursement. Every one of them belongs to a job,
because that is how this business is organised and because it is what puts a
permit fee into that job's costing. What they share, and the only reason they
are one queue, is the question: who is waiting, and how long?

**A subcontractor's invoice is one of them.** Zack: *"check request should be
automatic from sub invoices. If a sub invoice is sent over it goes into check
request as well."* Obvious once said - a sub sending their draw IS asking for a
check, and the office should not have to retype it as one. It changes nothing
about how that invoice is handled: it still goes through the invoice pipeline,
still gets read and priced line by line against their subcontract, still stops
at the award. This queue is a second view of it, answering the one question the
invoice pages do not: who is waiting, and since when.

That view is **derived, never written**. No CheckRequest row is created for an
invoice, for two reasons that are worth stating because the shortcut is
tempting:

  * **It would double-count.** The job costing counts subcontract invoices and
    check-request rows in separate buckets, so a sub's draw appearing as both
    would show up twice in the cost of the job.
  * **It would be two places to approve the same money**, which can disagree.
    An invoice held for going past the award would sit here as an approvable
    check request, and approving it here would pay exactly what the ceiling was
    there to stop.

So an invoice-backed row carries no decision of its own. It shows where the
invoice stands and links to it, and the decision stays in the one place it has
always been.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable, Optional

from app import subs
from app.models import (
    APPROVAL_APPROVED,
    APPROVAL_HELD,
    APPROVAL_PENDING,
    CHECK_APPROVED,
    CHECK_PURPOSE_LABELS,
    CHECK_REQUESTED,
    CHECK_SUBCONTRACTOR,
    CheckRequest,
    Invoice,
    Job,
)

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

# An invoice from a sub is waiting for a check in all three of these. Approved
# is the obvious one. The other two matter more: the sub has been waiting since
# the day they billed us, whatever we have or have not decided since, and a
# queue that only showed the approved ones would answer "who is owed money"
# with a number that quietly excludes everybody we are still arguing with.
AWAITING_PAYMENT = (APPROVAL_APPROVED, APPROVAL_PENDING, APPROVAL_HELD)

# And the same for a typed request. A permit somebody approved on Tuesday is
# still a permit nobody has paid, and dropping it out of the queue the moment
# it was approved meant the one list of who is owed money quietly stopped
# including the people we had already agreed to pay.
UNPAID = (CHECK_REQUESTED, CHECK_APPROVED)


def band_for(days: int) -> str:
    for low, high, label in AGE_BANDS:
        if low <= days <= high:
            return label
    return AGE_BANDS[-1][2]


@dataclass
class Waiting:
    """One party owed money, and how long they have been owed it.

    Backed by a check request somebody typed, or by a subcontractor's invoice.
    The queue does not care which - the question it answers is the same either
    way - so everything it needs is on this object rather than reached through
    whichever thing produced it.
    """

    payee: str
    amount: Decimal
    job_number: str
    purpose_label: str
    days: int
    waiting_since: date
    reference: str = ""
    description: str = ""

    request: Optional[CheckRequest] = None
    invoice: Optional[Invoice] = None

    # Invoice-backed rows only: where that invoice stands, and whether cutting
    # the check would be premature.
    state: str = ""
    ready: bool = False
    tone: str = ""            # ok | quiet | bad — how loudly to say `state`

    @property
    def band(self) -> str:
        return band_for(self.days)

    @property
    def decide_here(self) -> bool:
        """Is this queue where the decision gets made?

        True for a typed check request nobody has decided - a permit fee has
        nowhere else to be decided. False for an invoice, whose decision
        belongs on the invoice, against the quote, with the contract ceiling
        applied. And false once a request is approved, because the only thing
        left to say about it is that the check went out.
        """
        return self.request is not None and not self.ready


def _fmt(value: Decimal) -> str:
    return f"${value.quantize(Decimal('0.01')):,}"


def _days(since: date, today: date) -> int:
    return max((today - since).days, 0)


def _from_request(request: CheckRequest, today: date) -> Waiting:
    approved = request.status == CHECK_APPROVED
    return Waiting(
        payee=request.payee or "Unknown",
        amount=request.amount or ZERO,
        job_number=request.job.job_number,
        purpose_label=request.purpose_label,
        # Deliberately not days_waiting, which stops counting the moment a
        # request is approved. The clock a person cares about runs until the
        # check is in the post, not until we agreed to write it.
        days=_days(request.waiting_since, today),
        waiting_since=request.waiting_since,
        reference=request.reference,
        description=request.description,
        request=request,
        state="Approved — ready to pay" if approved else "",
        ready=approved,
        tone="ok" if approved else "",
    )


_STATES = {
    APPROVAL_APPROVED: ("Approved — ready to pay", True, "ok"),
    APPROVAL_PENDING: ("Not checked off yet", False, "quiet"),
    APPROVAL_HELD: ("Held — do not pay yet", False, "bad"),
}


def _from_invoice(invoice: Invoice, job: Job, today: date) -> Waiting:
    since = invoice.invoice_date or invoice.created_at.date()
    state, ready, tone = _STATES.get(invoice.approval_status, ("", False, "quiet"))

    # The one thing worth knowing in the room where somebody decides to cut a
    # check: every line on this invoice can be correctly priced and it can
    # still take the sub past what they were awarded. That is invisible on the
    # invoice itself and it is the reason this queue must not be a paying list.
    contract = subs.contract_check(job, invoice)
    if contract is not None and contract.over_contract:
        state = f"Would go {_fmt(contract.exceeds_by)} past the award"
        ready, tone = False, "bad"

    return Waiting(
        payee=(invoice.vendor or "").strip() or "Unknown",
        amount=invoice.total or ZERO,
        job_number=job.job_number,
        purpose_label=CHECK_PURPOSE_LABELS[CHECK_SUBCONTRACTOR],
        days=_days(since, today),
        waiting_since=since,
        reference=invoice.invoice_number or f"Invoice #{invoice.id}",
        description=invoice.hold_reason or "",
        invoice=invoice,
        state=state,
        ready=ready,
        tone=tone,
    )


def sub_invoices_waiting(jobs: Iterable[Job], today: Optional[date] = None) -> list[Waiting]:
    """Every subcontractor invoice that is still waiting on a check.

    Only subcontractors. A supply house's invoice is paid on terms out of
    accounts payable and was never a check request; putting one here would
    bury the people this queue exists for.
    """
    on = today or date.today()
    rows: list[Waiting] = []
    for job in jobs:
        for invoice in job.invoices:
            if invoice.approval_status not in AWAITING_PAYMENT:
                continue
            if not subs.is_subcontractor(job, invoice.vendor):
                continue
            rows.append(_from_invoice(invoice, job, on))
    return rows


def queue(
    requests: Iterable[CheckRequest] = (),
    jobs: Iterable[Job] = (),
    today: Optional[date] = None,
) -> list[Waiting]:
    """Everyone waiting on a check, longest wait first.

    Longest wait first and nothing else. Not by amount, not by job, not by
    purpose - those orderings all share one failure, which is that the request
    nobody has looked at stays the request nobody has looked at. The person
    this list is for is being telephoned by somebody who wants to know where
    their money is, and the only ordering that answers that is oldest first.
    """
    on = today or date.today()
    rows = [_from_request(r, on) for r in requests if r.status in UNPAID]
    rows.extend(sub_invoices_waiting(jobs, on))
    rows.sort(key=lambda w: (-w.days, -w.amount))
    return rows


def total_waiting(rows: Iterable[Waiting]) -> Decimal:
    return sum((w.amount for w in rows), ZERO)


def total_ready(rows: Iterable[Waiting]) -> Decimal:
    """What could go out today: everything somebody has already signed off.

    Deliberately separate from the total - "$134,605 owed" and "$83,605 ready
    to pay" are different statements and only one of them is an instruction.
    """
    return sum((w.amount for w in rows if w.ready), ZERO)

"""Three-way match and approval routing.

The rule the business runs on: **no invoice gets paid unless it can be tied back
to (1) an approved quoted price, and (2) confirmation that the material or work
was actually received.** Price alone is not enough - an invoice can be perfectly
priced against the quote and still bill for a delivery that never arrived.

    quote / PO  ──┐
                  ├──▶  matched?  ──▶  who approves?  ──▶  pay
    receipt     ──┤
    invoice     ──┘

Like matching.py, this module is pure Python with no model calls. Who is allowed
to authorise a payment is not a judgement call to delegate to a language model.

Two separate ideas that are easy to conflate:

  * The **colours** on the marked-up invoice are literal. A one-cent difference
    is shown as a difference, because the reader deserves the truth.
  * The **tolerance** here decides who has to look at it. A $3 variance on a
    $40,000 order is not worth the owner's attention.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from app import subs, trust
from app.config import settings
from app.matching import vendor_matches
from app.models import (
    APPROVAL_APPROVED,
    APPROVAL_HELD,
    APPROVAL_PAID,
    APPROVAL_PENDING,
    CO_PROPOSED,
    TIER_OWNER,
    TIER_PM,
    ChangeOrder,
    Invoice,
    Receipt,
)

ZERO = Decimal("0")

# What the router concluded should happen next.
ACTION_APPROVE = "approve"        # matched and within tolerance
ACTION_SPOT_CHECK = "spot_check"  # within tolerance, but large enough to review
ACTION_HOLD = "hold"              # over tolerance with no authorisation on file
ACTION_INVESTIGATE = "investigate"  # nothing to match against at all


def tolerance_for(total: Optional[Decimal]) -> Decimal:
    """The allowance for this invoice: a percentage or a flat amount, whichever
    is greater. A small job should not be held for $3; a large one should not
    quietly absorb $900 because 5% of it is a big number.
    """
    flat = Decimal(settings.tolerance_abs)
    if total is None:
        return flat
    pct = (abs(total) * Decimal(str(settings.tolerance_pct)) / Decimal(100))
    return max(flat, pct)


@dataclass
class Routing:
    """The system's recommendation. A person still makes the decision."""

    action: str
    tier: str
    variance: Decimal = ZERO
    tolerance: Decimal = ZERO
    within_tolerance: bool = True
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    covering_change_order: Optional[ChangeOrder] = None
    available_change_orders: list[ChangeOrder] = field(default_factory=list)
    proposed_change_orders: list[ChangeOrder] = field(default_factory=list)
    trust_flags: list = field(default_factory=list)
    contract: object = None          # subs.ContractCheck, when the vendor is a sub

    @property
    def can_approve(self) -> bool:
        """Blockers stop approval outright, regardless of who is looking."""
        return not self.blockers

    @property
    def needs_owner(self) -> bool:
        return self.tier == TIER_OWNER

    @property
    def over_contract(self) -> bool:
        """A subcontractor billed past their award. Approvable, knowingly."""
        return bool(self.contract is not None and self.contract.over_contract)

    @property
    def untrusted(self) -> bool:
        """Something about where this came from does not add up."""
        return any(f.blocks for f in self.trust_flags)

    @property
    def headline(self) -> str:
        return {
            ACTION_APPROVE: "Ready to approve",
            ACTION_SPOT_CHECK: "Ready to approve — owner spot check",
            ACTION_HOLD: "Hold — billed above quote with no change order",
            ACTION_INVESTIGATE: "No quote from this supplier — price not checked",
        }[self.action]


def find_receipt(invoice: Invoice) -> Optional[Receipt]:
    """A confirmation of delivery or work completion covering this invoice.

    Matched on job and vendor. Deliberately loose: the point is that *somebody
    confirmed this vendor delivered something on this job*, not to reconcile
    packing slips line by line, which nobody is going to do.
    """
    if invoice.receipt_id and invoice.receipt:
        return invoice.receipt
    for receipt in invoice.job.receipts:
        if vendor_matches(receipt.vendor, invoice.vendor):
            return receipt
    return None


def covering_change_orders(invoice: Invoice) -> list[ChangeOrder]:
    """Approved change orders that could authorise an overage from this vendor.

    Approved only. A change order the system read off a vendor's email is a
    proposal until somebody here signs it, and letting a proposal raise the
    ceiling would let a vendor authorise their own overbilling.
    """
    return [
        co for co in invoice.job.change_orders
        if co.is_live and vendor_matches(co.vendor, invoice.vendor)
    ]


def proposed_change_orders(invoice: Invoice) -> list[ChangeOrder]:
    """Unapproved change orders that would cover this overage if signed.

    Reported so the reviewer knows the paperwork exists and where it is,
    rather than being told there is nothing on file when there nearly is.
    """
    return [
        co for co in invoice.job.change_orders
        if co.status == CO_PROPOSED and vendor_matches(co.vendor, invoice.vendor)
    ]


def route(invoice: Invoice) -> Routing:
    """Decide what happens to this invoice next.

    Mirrors the approval policy:

      | Situation                                   | Who    | Action        |
      |---------------------------------------------|--------|---------------|
      | Within tolerance                            | PM     | Approve       |
      | Within tolerance but over the review ceiling| Owner  | Spot check    |
      | Over tolerance, no change order             | Owner  | Hold          |
      | No quote/PO at all                          | Owner  | Investigate   |

    On top of that, a missing receipt confirmation blocks approval entirely -
    that is the third leg of the match, not a nice-to-have.
    """
    variance = invoice.overbilled_amount or ZERO
    tolerance = tolerance_for(invoice.total)
    within = variance <= tolerance

    routing = Routing(
        action=ACTION_APPROVE,
        tier=TIER_PM,
        variance=variance,
        tolerance=tolerance,
        within_tolerance=within,
        available_change_orders=covering_change_orders(invoice),
        proposed_change_orders=proposed_change_orders(invoice),
        trust_flags=trust.flags_for(invoice.document),
    )

    # --- blocker: is this invoice even ours? -------------------------------
    # Deliberately first. Everything below asks whether the price is right,
    # which is the wrong question about a bill from a supplier nobody ordered
    # from. A person can still approve it - but only after clearing this.
    for flag in trust.blocking(routing.trust_flags):
        routing.blockers.append(flag.message)
    for flag in routing.trust_flags:
        if not flag.blocks:
            routing.reasons.append(flag.message)
    if routing.blockers:
        routing.tier = TIER_OWNER

    # --- a subcontract is a ceiling, a quote is not ------------------------
    #
    # The line comparison above is identical for a sub and a supplier. This is
    # the one question only a subcontract asks: a quote prices material and
    # does not cap how much of it a roof needs, while a contract is a fixed
    # award that every invoice eats into. Six invoices can each be correct and
    # the seventh still take the sub past what they were awarded.
    routing.contract = subs.contract_check(invoice.job, invoice)
    if routing.contract is not None:
        if routing.contract.over_contract:
            routing.blockers.append(routing.contract.message)
            routing.tier = TIER_OWNER
        else:
            routing.reasons.append(routing.contract.message)

    # --- blocker: was it actually received? --------------------------------
    if settings.require_receipt:
        receipt = find_receipt(invoice)
        if receipt is None:
            routing.blockers.append(
                "No confirmation that the material was delivered or the work "
                "completed. Confirm receipt before approving."
            )
        else:
            routing.reasons.append(
                f"Receipt confirmed by {receipt.confirmed_by or 'site'} on "
                f"{receipt.confirmed_at:%d %b %Y}"
                + (f" ({receipt.reference})" if receipt.reference else "")
            )

    # --- no quote to match against -----------------------------------------
    if invoice.quote_id is None:
        routing.action = ACTION_INVESTIGATE
        routing.tier = TIER_OWNER
        # Deliberately not phrased as a fault. ABC quoted the roof and a couple
        # of things got picked up at New Castle because that is where they were
        # in stock - normal, and it is precisely why an invoice is never
        # measured against another supplier's quote. What is true is that
        # nothing checked this price, and somebody should read it.
        routing.reasons.append(
            f"No quote on this job from {invoice.vendor or 'this supplier'}, so "
            f"nothing checked these prices. Often a last-minute pickup, and "
            f"worth reading before it is paid."
        )
        return routing

    if invoice.quote_match == "sole":
        routing.reasons.append(
            "Vendor name on the invoice differs from the quote; it was the only "
            "quote on the job, so it was used. Worth confirming it is the same supplier."
        )

    # --- priced above the quote --------------------------------------------
    if variance > 0:
        if within:
            routing.reasons.append(
                f"Billed {_money(variance)} above quote, within the "
                f"{_money(tolerance)} tolerance for this invoice."
            )
        else:
            covering = _pick_covering(routing.available_change_orders, variance)
            if covering is not None:
                routing.covering_change_order = covering
                routing.reasons.append(
                    f"Billed {_money(variance)} above quote, authorised by change "
                    f"order {covering.number or covering.id} ({_money(covering.amount)})."
                )
            else:
                routing.action = ACTION_HOLD
                routing.tier = TIER_OWNER
                waiting = _pick_covering(routing.proposed_change_orders, variance)
                if waiting is not None:
                    # The paperwork exists and nobody has signed it. Saying
                    # "no change order on file" here would be false, and would
                    # send somebody looking for a document that is already here.
                    routing.reasons.append(
                        f"Billed {_money(variance)} above quote — beyond the "
                        f"{_money(tolerance)} tolerance. A change order for "
                        f"{_money(waiting.amount)} "
                        f"({waiting.number or 'unnumbered'}) is on this job but "
                        f"has not been approved. Approve it and this clears."
                    )
                else:
                    routing.reasons.append(
                        f"Billed {_money(variance)} above quote — beyond the "
                        f"{_money(tolerance)} tolerance — with no change order on file."
                    )
                return routing

    if invoice.lines_unmatched:
        routing.reasons.append(
            f"{invoice.lines_unmatched} line"
            f"{'' if invoice.lines_unmatched == 1 else 's'} not on the quote; "
            "those prices were not checked."
        )

    # --- size-based spot check ---------------------------------------------
    ceiling = Decimal(settings.owner_review_above)
    if invoice.total is not None and invoice.total > ceiling:
        routing.action = ACTION_SPOT_CHECK
        routing.tier = TIER_OWNER
        routing.reasons.append(
            f"Invoice total is over the {_money(ceiling)} owner-review threshold."
        )

    return routing


def _pick_covering(
    change_orders: list[ChangeOrder], variance: Decimal
) -> Optional[ChangeOrder]:
    """The smallest change order that fully covers the overage.

    Smallest-that-fits rather than largest, so a big change order is not
    consumed by a small overage it did not need to cover.
    """
    candidates = [
        co for co in change_orders if co.amount is not None and co.amount >= variance
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda co: co.amount)


def _money(value: Optional[Decimal]) -> str:
    if value is None:
        return "$0.00"
    return f"${value.quantize(Decimal('0.01')):,}"


def apply_routing(invoice: Invoice) -> Routing:
    """Recompute routing and park the invoice in the right state.

    Only touches invoices that are still open. An invoice a person has already
    approved or marked paid is left alone - re-running the matcher must never
    silently un-approve something someone signed for.
    """
    routing = route(invoice)

    if invoice.approval_status in (APPROVAL_APPROVED, APPROVAL_PAID):
        return routing

    if routing.action == ACTION_HOLD:
        invoice.approval_status = APPROVAL_HELD
        invoice.hold_reason = routing.reasons[-1] if routing.reasons else "Over tolerance."
    elif invoice.approval_status == APPROVAL_HELD and routing.action != ACTION_HOLD:
        # A change order arrived, or a corrected invoice - stop holding it.
        invoice.approval_status = APPROVAL_PENDING
        invoice.hold_reason = ""
    elif invoice.approval_status not in (APPROVAL_HELD,):
        invoice.approval_status = APPROVAL_PENDING

    return routing

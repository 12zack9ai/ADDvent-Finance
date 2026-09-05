"""Subcontractors: their contract, and how much of it they have billed.

A subcontractor's invoice is an ordinary invoice. It arrives the same way, it
is read the same way, and it is compared line by line against their
subcontract exactly as a supplier's invoice is compared against a quote -
Zack: *"the subcontractor invoice should work exactly like the vendor
invoicing."* None of that needed rebuilding; the vendor pipeline already does
it, and the vendor name is what keeps a delivery ticket away from a labour
contract.

What this module adds is the one question a subcontract asks and a quote does
not: **a contract is a ceiling on everything that sub will ever bill.**

A quote prices material and does not cap how much of it a roof needs - 250
squares at the quoted price is an ordinary job. A subcontract is the opposite:
it is a fixed award, and every invoice against it eats into a finite number.
Six invoices at $20,000 each fit inside a $120,000 contract and every one of
them is individually correct. The seventh is the first thing anyone could
object to, and only the running total sees it.

**Overages are not errors.** Subs do get more than they quoted, for extras, and
a system that treated that as a fault would be wrong about the business. So an
overage is reported and can be approved - it just has to be done knowingly, by
somebody who says what the extra work was.

Pure Python over rows already loaded. Decimal throughout.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from app.matching import norm_vendor, vendor_matches
from app.models import (
    APPROVAL_APPROVED,
    APPROVAL_PAID,
    APPROVAL_REJECTED,
    Invoice,
    Job,
    Quote,
)

ZERO = Decimal("0")


@dataclass
class Position:
    """Where one subcontractor stands on one job."""

    vendor: str
    contract: Decimal = ZERO
    change_orders: Decimal = ZERO
    billed: Decimal = ZERO          # approved or paid: money we owe them
    paid: Decimal = ZERO
    pending: Decimal = ZERO         # invoiced, nobody has decided yet
    subcontract: Optional[Quote] = None
    invoices: list[Invoice] = field(default_factory=list)

    @property
    def has_contract(self) -> bool:
        return self.subcontract is not None and self.contract > ZERO

    @property
    def awarded(self) -> Decimal:
        """The contract plus written extras. What they may be paid in total."""
        return self.contract + self.change_orders

    @property
    def remaining(self) -> Decimal:
        return self.awarded - self.billed

    @property
    def overage(self) -> Decimal:
        """Approved beyond the award. Real, and not necessarily wrong."""
        return max(self.billed - self.awarded, ZERO)

    @property
    def committed(self) -> Decimal:
        """Approved plus still under review."""
        return self.billed + self.pending

    @property
    def would_exceed(self) -> Decimal:
        return max(self.committed - self.awarded, ZERO)

    @property
    def percent_drawn(self) -> Optional[int]:
        if self.awarded <= ZERO:
            return None
        return int((self.billed / self.awarded * 100).quantize(Decimal("1")))

    def would_exceed_with(self, invoice: Invoice) -> Decimal:
        """How far past the award approving this one invoice would take them.

        Against what is already APPROVED, not against everything under review:
        two invoices in the queue are two claims, and counting each against the
        other would condemn both on the strength of money nobody has agreed to.
        """
        after = self.billed + (invoice.total or ZERO)
        return max(after - self.awarded, ZERO)

    @property
    def open_invoices(self) -> list[Invoice]:
        return [
            i for i in self.invoices
            if i.approval_status not in (APPROVAL_APPROVED, APPROVAL_PAID,
                                         APPROVAL_REJECTED)
        ]


def _same(a: str, b: str) -> bool:
    na, nb = norm_vendor(a), norm_vendor(b)
    if not na or not nb:
        return not na and not nb
    return vendor_matches(a, b)


def positions(job: Job) -> list[Position]:
    """Every subcontractor on a job, largest contract first.

    Only vendors who actually hold a subcontract. A supply house with a
    material quote is not a subcontractor and does not belong on this list,
    even though their invoices go through the identical pipeline.
    """
    found: list[Position] = []

    for contract in job.subcontracts:
        existing = next((p for p in found if _same(p.vendor, contract.vendor)), None)
        if existing is None:
            existing = Position(vendor=(contract.vendor or "").strip()
                                or "Unknown subcontractor")
            found.append(existing)
        existing.subcontract = existing.subcontract or contract
        existing.contract += contract.total or ZERO

    if not found:
        return []

    for change_order in job.change_orders:
        if not change_order.is_live:
            continue
        for position in found:
            if _same(position.vendor, change_order.vendor):
                position.change_orders += change_order.amount or ZERO
                break

    for invoice in job.invoices:
        if invoice.approval_status == APPROVAL_REJECTED:
            continue
        position = next((p for p in found if _same(p.vendor, invoice.vendor)), None)
        if position is None:
            continue                      # a material supplier, not a sub
        position.invoices.append(invoice)
        amount = invoice.total or ZERO
        if invoice.approval_status in (APPROVAL_APPROVED, APPROVAL_PAID):
            position.billed += amount
            if invoice.approval_status == APPROVAL_PAID:
                position.paid += amount
        else:
            position.pending += amount

    for position in found:
        # Defensive on both dates: created_at is unset until a flush, and this
        # is a display ordering. Crashing a costing report over a sort key
        # would be an absurd way to lose a page.
        position.invoices.sort(key=_when)
    found.sort(key=lambda p: (-p.awarded, p.vendor.lower()))
    return found


def position_for(job: Job, vendor: str) -> Optional[Position]:
    """One sub's position, or None if this vendor holds no subcontract here."""
    return next((p for p in positions(job) if _same(p.vendor, vendor)), None)


def is_subcontractor(job: Job, vendor: str) -> bool:
    return position_for(job, vendor) is not None


# --- the ceiling check ----------------------------------------------------

@dataclass
class ContractCheck:
    """What approving this invoice would do to the sub's running total."""

    position: Position
    invoice: Invoice
    billed_after: Decimal = ZERO
    exceeds_by: Decimal = ZERO

    @property
    def over_contract(self) -> bool:
        return self.exceeds_by > ZERO

    @property
    def message(self) -> str:
        if self.over_contract:
            return (
                f"This takes {self.position.vendor} to {_fmt(self.billed_after)} "
                f"against a {_fmt(self.position.awarded)} contract — "
                f"{_fmt(self.exceeds_by)} past it. That happens; extras get "
                f"agreed on site and papered later. Say what the extra work was "
                f"before approving it."
            )
        return (
            f"{_fmt(self.position.billed)} of {_fmt(self.position.awarded)} "
            f"billed so far; this leaves "
            f"{_fmt(self.position.awarded - self.billed_after)} on the contract."
        )


def contract_check(job: Job, invoice: Invoice) -> Optional[ContractCheck]:
    """The extra question a subcontract asks. None for a material supplier.

    Counts what has already been APPROVED plus this invoice - not everything
    sitting in the queue - because two invoices under review are two claims,
    and refusing them both on the strength of each other would be wrong.
    """
    position = position_for(job, invoice.vendor)
    if position is None or not position.has_contract:
        return None

    already = position.billed
    if invoice.approval_status in (APPROVAL_APPROVED, APPROVAL_PAID):
        already -= invoice.total or ZERO

    after = already + (invoice.total or ZERO)
    return ContractCheck(
        position=position,
        invoice=invoice,
        billed_after=after,
        exceeds_by=max(after - position.awarded, ZERO),
    )


def _when(invoice: Invoice):
    from datetime import date as _date
    if invoice.invoice_date:
        return (invoice.invoice_date, invoice.id or 0)
    created = getattr(invoice, "created_at", None)
    return (created.date() if created else _date.min, invoice.id or 0)


def _fmt(value: Optional[Decimal]) -> str:
    if value is None:
        return "$0.00"
    return f"${value.quantize(Decimal('0.01')):,}"

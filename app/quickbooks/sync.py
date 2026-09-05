"""What to ask QuickBooks for, in what order, and where the answers go.

One session is a short plan run to the end:

    1. which company file is this        (so we notice if it is the wrong one)
    2. customers and jobs, changed since last time
    3. customer invoices, changed since last time
    4. anything queued to write, one at a time

Then the two derived things a person actually sees: our jobs matched up to
QuickBooks jobs, and what each one has been billed and collected.

**Everything is incremental.** Each entity carries a cursor and we ask for what
changed since it. A full pull is not just slow - it holds a lock the people
working in the company file feel, and this runs while they are working.

**A failed step does not lose the rest of the run.** The cursor only moves when
a step finishes cleanly, so a step that fails is simply asked for again next
time rather than leaving a silent hole in the mirror.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app import jobnum
from app.models import BILLING_QUICKBOOKS, Job, utcnow
from app.quickbooks import qbxml
from app.quickbooks.mirror import (
    OUT_CONFIRMED,
    OUT_FAILED,
    OUT_PENDING,
    OUT_SENT,
    QbCustomer,
    QbInvoice,
    QbOutbox,
    QbSession,
    QbSyncState,
    SYNC_ERROR,
    SYNC_IDLE,
)

log = logging.getLogger(__name__)
ZERO = Decimal("0")

STEP_COMPANY = "company"
STEP_CUSTOMERS = "customers"
STEP_INVOICES = "invoices"
STEP_OUTBOX = "outbox"

READ_STEPS = (STEP_COMPANY, STEP_CUSTOMERS, STEP_INVOICES)


def cursor_for(session: OrmSession, entity: str) -> QbSyncState:
    state = session.scalar(select(QbSyncState).where(QbSyncState.entity == entity))
    if state is None:
        state = QbSyncState(entity=entity)
        session.add(state)
        session.flush()
    return state


class Sync:
    """One conversation with QuickBooks. Implements protocol.Conversation.

    Holds no connection and no transaction between calls: each callback
    arrives as its own HTTP request, minutes apart, and this process can be
    restarted in the middle. So the position in the plan lives in the
    database, not in this object.
    """

    def __init__(self, db: OrmSession, record: QbSession, version: str = qbxml.DEFAULT_VERSION):
        self.db = db
        self.record = record
        self.version = version

    # -- plan --------------------------------------------------------------

    @property
    def plan(self) -> list[str]:
        return json.loads(self.record.plan_json or "[]")

    @property
    def cursor(self) -> dict:
        return json.loads(self.record.cursor_json or "{}")

    def _set_cursor(self, data: dict) -> None:
        self.record.cursor_json = json.dumps(data)

    @property
    def current(self) -> Optional[str]:
        plan = self.plan
        return plan[self.record.step] if self.record.step < len(plan) else None

    def had_work(self) -> bool:
        return bool(self.plan)

    def progress(self) -> int:
        return self.record.percent

    # -- the loop ----------------------------------------------------------

    def next_request(self) -> str:
        step = self.current
        if step is None:
            return ""

        page = self.cursor.get("page")
        if step == STEP_COMPANY:
            xml = qbxml.company_query(self.version)
        elif step == STEP_CUSTOMERS:
            state = cursor_for(self.db, STEP_CUSTOMERS)
            xml = qbxml.customer_query(state.cursor, page, version=self.version)
        elif step == STEP_INVOICES:
            state = cursor_for(self.db, STEP_INVOICES)
            xml = qbxml.invoice_query(state.cursor, page, version=self.version)
        elif step == STEP_OUTBOX:
            item = self._next_outbox()
            if item is None:
                self._advance()
                return self.next_request()
            xml = qbxml.bill_add(json.loads(item.payload_json or "{}"), self.version)
            cursor = self.cursor
            cursor["outbox_id"] = item.id
            self._set_cursor(cursor)
            item.status = OUT_SENT
            item.attempts += 1
            item.sent_at = utcnow()
        else:
            self._advance()
            return self.next_request()

        self.record.requests_sent += 1
        self.db.commit()
        return xml

    def handle_response(self, xml: str) -> None:
        step = self.current
        if step is None:
            return

        response = qbxml.parse(xml)
        if not response.ok:
            self._fail(f"{response.request_type}: {response.status_message}")
            self._advance()
            self.db.commit()
            return

        if step == STEP_COMPANY:
            for row in response.rows:
                if row.get("kind") == "company":
                    self.record.company_file = (
                        row.get("company_name") or self.record.company_file
                    )
        elif step == STEP_CUSTOMERS:
            self._apply_customers(response.rows)
        elif step == STEP_INVOICES:
            self._apply_invoices(response.rows)
        elif step == STEP_OUTBOX:
            self._confirm_outbox(response)

        self.record.rows_seen += len(response.rows)
        cursor = self.cursor
        cursor["rows"] = cursor.get("rows", 0) + len(response.rows)

        # More pages of the same step, or on to the next one.
        if response.has_more:
            cursor["page"] = response.next_page
            self._set_cursor(cursor)
        else:
            if step in READ_STEPS:
                self._finish_step(step, cursor.get("rows", 0))
            if step == STEP_OUTBOX and self._next_outbox() is not None:
                self._set_cursor({})            # another write to send
            else:
                self._advance()
        self.db.commit()

    def _advance(self) -> None:
        self.record.step += 1
        self._set_cursor({})

    def _finish_step(self, step: str, rows: int = 0) -> None:
        """Move the cursor only when the step completed cleanly.

        The high-water mark is when the session started, not now: a record
        changed while we were reading would otherwise fall in the gap between
        the two and never be asked for again.
        """
        state = cursor_for(self.db, step)
        state.cursor = self.record.started_at
        state.last_run_at = utcnow()
        state.last_status = "ok"
        state.last_error = ""
        # What this run brought back, not a running total since the beginning
        # of time. "400 records" on a page where three things changed says
        # nothing about whether the last sync worked.
        state.rows = rows

    def _fail(self, message: str) -> None:
        log.warning("QB sync: %s", message)
        self.record.last_error = message[:2000]
        step = self.current
        if step in READ_STEPS:
            state = cursor_for(self.db, step)
            state.last_status = "error"
            state.last_error = message[:2000]
        if step == STEP_OUTBOX:
            self._reject_outbox(message)

    # -- applying what came back -------------------------------------------

    def _apply_customers(self, rows: list[dict]) -> None:
        for row in rows:
            if row.get("kind") != "customer" or not row.get("list_id"):
                continue
            existing = self.db.scalar(
                select(QbCustomer).where(QbCustomer.list_id == row["list_id"])
            )
            if existing is None:
                existing = QbCustomer(list_id=row["list_id"])
                self.db.add(existing)
            existing.edit_sequence = row.get("edit_sequence", "")
            existing.name = row.get("name", "")
            existing.full_name = row.get("full_name", "")
            existing.parent_list_id = row.get("parent_list_id", "")
            existing.is_active = bool(row.get("is_active", True))
            existing.time_modified = row.get("time_modified")
            existing.mirrored_at = utcnow()
        self.db.flush()
        link_jobs(self.db)

    def _apply_invoices(self, rows: list[dict]) -> None:
        for row in rows:
            if row.get("kind") != "invoice" or not row.get("txn_id"):
                continue
            existing = self.db.scalar(
                select(QbInvoice).where(QbInvoice.txn_id == row["txn_id"])
            )
            if existing is None:
                existing = QbInvoice(txn_id=row["txn_id"])
                self.db.add(existing)
            for field in ("edit_sequence", "ref_number", "customer_list_id",
                          "customer_full_name", "txn_date", "due_date",
                          "subtotal", "total", "balance_remaining", "memo",
                          "time_modified"):
                setattr(existing, field, row.get(field))
            existing.is_paid = bool(row.get("is_paid"))
            existing.mirrored_at = utcnow()
        self.db.flush()
        apply_billing(self.db)

    # -- the outbox --------------------------------------------------------

    def _next_outbox(self) -> Optional[QbOutbox]:
        return self.db.scalar(
            select(QbOutbox)
            .where(QbOutbox.status == OUT_PENDING)
            .order_by(QbOutbox.queued_at)
        )

    def _current_outbox(self) -> Optional[QbOutbox]:
        outbox_id = self.cursor.get("outbox_id")
        return self.db.get(QbOutbox, outbox_id) if outbox_id else None

    def _confirm_outbox(self, response) -> None:
        item = self._current_outbox()
        if item is None:
            return
        txn_id = next((r.get("txn_id") for r in response.rows if r.get("txn_id")), "")
        item.status = OUT_CONFIRMED
        item.qb_txn_id = txn_id or ""
        item.confirmed_at = utcnow()
        item.last_error = ""

    def _reject_outbox(self, message: str) -> None:
        item = self._current_outbox()
        if item is None:
            return
        # Failed, and left failed. A write QuickBooks refused is refused for a
        # reason - a vendor that does not exist, an account that does not -
        # and retrying it on a schedule would bury that reason in a log.
        item.status = OUT_FAILED
        item.last_error = message[:2000]

    # -- the end -----------------------------------------------------------

    def finish(self, error: str = "") -> str:
        self.record.finished_at = utcnow()
        self.record.status = SYNC_ERROR if (error or self.record.last_error) else SYNC_IDLE
        if error:
            self.record.last_error = error[:2000]
        self.record.step = max(self.record.step, self.record.steps_total)
        self.db.commit()

        if self.record.last_error:
            return f"Finished with a problem: {self.record.last_error[:200]}"
        return (f"Read {self.record.rows_seen} records from QuickBooks in "
                f"{self.record.requests_sent} requests.")


# --- deriving the two things people see ------------------------------------

def link_jobs(db: OrmSession) -> int:
    """Match QuickBooks jobs to ours, by the job number in the name.

    Every job here carries a six-digit number and it is how the business is
    organised, so a QuickBooks job called "Daul Gardens:269001 Building 4" is
    unambiguous. What this deliberately will not do is match on the name: two
    associations can have a "Building 4", and filing one job's money against
    another is not a mistake anybody would catch from a costing report.

    A QuickBooks job with no recognisable number is left unlinked and shows on
    the status page for a person to point at the right job.
    """
    linked = 0
    customers = db.scalars(select(QbCustomer).where(QbCustomer.job_id.is_(None))).all()
    for customer in customers:
        numbers = jobnum.find_job_numbers(customer.full_name or "")
        if len(numbers) != 1:
            continue
        job = db.scalar(select(Job).where(Job.job_number == numbers[0]))
        if job is None:
            continue
        customer.job_id = job.id
        linked += 1
    if linked:
        db.flush()
    return linked


@dataclass
class Billing:
    """One job's position with its customer, read off the mirror."""

    billed: Decimal = ZERO
    collected: Decimal = ZERO
    invoices: int = 0
    synced_at: Optional[datetime] = None

    @property
    def outstanding(self) -> Decimal:
        return self.billed - self.collected


def billing_for(db: OrmSession, job: Job) -> Optional[Billing]:
    """What QuickBooks says this job has been billed and paid.

    Collected is billed less what is still outstanding on each invoice, not
    the paid flag: on a progress-billed roof a partial payment is the normal
    case, and a boolean cannot say "$40,000 of $96,400".

    Returns None when QuickBooks has never been asked about this job, which is
    different from being asked and told zero - and the costing report says
    which.
    """
    customers = db.scalars(
        select(QbCustomer).where(QbCustomer.job_id == job.id)
    ).all()
    if not customers:
        return None

    list_ids = [c.list_id for c in customers]
    invoices = db.scalars(
        select(QbInvoice).where(QbInvoice.customer_list_id.in_(list_ids))
    ).all()

    out = Billing(
        invoices=len(invoices),
        synced_at=max((c.mirrored_at for c in customers), default=None),
    )
    for invoice in invoices:
        out.billed += invoice.total or ZERO
        out.collected += invoice.collected
    return out


def apply_billing(db: OrmSession) -> int:
    """Push what QuickBooks says onto the jobs, so every page reads one place.

    This is the whole point of the integration: nobody types what we billed
    again. A job that QuickBooks has never heard of is left exactly as it is,
    including a figure somebody typed by hand - overwriting that with zero
    would be worse than the manual entry it replaces.
    """
    updated = 0
    jobs = db.scalars(
        select(Job).join(QbCustomer, QbCustomer.job_id == Job.id).distinct()
    ).all()
    for job in jobs:
        billing = billing_for(db, job)
        if billing is None or billing.invoices == 0:
            continue
        job.contract_amount = billing.billed
        job.collected_amount = billing.collected
        job.billing_source = BILLING_QUICKBOOKS
        job.billing_synced_at = utcnow()
        updated += 1
    if updated:
        db.flush()
    return updated


# --- starting a session ----------------------------------------------------

def open_session(db: OrmSession, ticket: str, version: str = qbxml.DEFAULT_VERSION,
                 write_back: bool = True) -> Optional[Sync]:
    """Decide whether there is anything to do, and set up the plan.

    Returning None tells the connector "none", and it goes away quietly until
    its next scheduled run. There is always reading to do, so in practice this
    only returns None if somebody has switched the integration off.
    """
    plan = list(READ_STEPS)
    if write_back and db.scalar(
        select(QbOutbox).where(QbOutbox.status == OUT_PENDING)
    ) is not None:
        plan.append(STEP_OUTBOX)

    record = QbSession(
        ticket=ticket,
        qbxml_version=version,
        plan_json=json.dumps(plan),
        steps_total=len(plan),
    )
    db.add(record)
    db.commit()
    return Sync(db, record, version)

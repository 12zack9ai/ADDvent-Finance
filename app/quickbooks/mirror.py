"""What QuickBooks told us, kept so the site can read it instantly.

A page cannot ask QuickBooks a question. The Web Connector polls us on a
schedule, so by the time somebody opens a job the only truthful thing we can
show is what the last poll brought back - which means it has to be stored, with
the time it arrived, so the page can say how old it is rather than implying it
is live.

**Read model, never edited by the app.** Every row here is a copy of something
QuickBooks owns. Nothing in this system may change one, because QuickBooks is
the book of record for money and a mirror that disagrees with it is worse than
no mirror. Writes go the other way, through the outbox.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, Money
from app.models import utcnow

# --- what the connector is doing ------------------------------------------

SYNC_IDLE = "idle"
SYNC_RUNNING = "running"
SYNC_ERROR = "error"


class QbSession(Base):
    """One conversation with the Web Connector, start to finish.

    Kept after it ends. With a polling connector silence is ambiguous - a
    connector that has stopped looks exactly like a connector with nothing to
    do - so the only way to know it is alive is a record of when it last spoke
    and what it said.
    """

    __tablename__ = "qb_session"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    company_file: Mapped[str] = mapped_column(Text, default="")
    qbxml_version: Mapped[str] = mapped_column(String(16), default="")
    country: Mapped[str] = mapped_column(String(8), default="")

    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=SYNC_RUNNING, index=True)

    step: Mapped[int] = mapped_column(Integer, default=0)     # how far down the plan
    steps_total: Mapped[int] = mapped_column(Integer, default=0)
    requests_sent: Mapped[int] = mapped_column(Integer, default=0)
    rows_seen: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")

    # The plan for this session, and where we are in it. Stored rather than
    # held in memory because a session spans many HTTP requests and this
    # process can be restarted between any two of them.
    plan_json: Mapped[str] = mapped_column(Text, default="[]")
    cursor_json: Mapped[str] = mapped_column(Text, default="{}")

    @property
    def percent(self) -> int:
        if self.steps_total <= 0:
            return 100
        return min(int(self.step * 100 / self.steps_total), 100)


class QbCustomer(Base):
    """A customer or a job. In QuickBooks a job IS a customer, one level down.

    The `Customer:Job` hierarchy is why `full_name` matters more than `name`:
    "Daul Gardens Condominium Association:269001 Building 4 reroof" is one
    string, and the part before the colon is the association we bill.
    """

    __tablename__ = "qb_customer"

    id: Mapped[int] = mapped_column(primary_key=True)
    list_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    edit_sequence: Mapped[str] = mapped_column(String(64), default="")

    name: Mapped[str] = mapped_column(String(255), default="")
    full_name: Mapped[str] = mapped_column(String(511), default="", index=True)
    parent_list_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    is_active: Mapped[bool] = mapped_column(default=True)

    # Filled in when we can tell which of our jobs this is. Deliberately
    # nullable and deliberately not guessed at - see sync.link_jobs.
    job_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("job.id"), nullable=True, index=True
    )

    time_modified: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    mirrored_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    job = relationship("Job")


class QbInvoice(Base):
    """A customer invoice. Ours to read and nobody's to edit.

    `balance_remaining` is the whole reason this table exists: billed minus
    balance is collected, including partial payments, without asking a second
    question about how payments were applied.
    """

    __tablename__ = "qb_invoice"

    id: Mapped[int] = mapped_column(primary_key=True)
    txn_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    edit_sequence: Mapped[str] = mapped_column(String(64), default="")

    ref_number: Mapped[str] = mapped_column(String(64), default="", index=True)
    customer_list_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    customer_full_name: Mapped[str] = mapped_column(String(511), default="")

    txn_date: Mapped[Optional[date]] = mapped_column(DateTime, nullable=True)
    due_date: Mapped[Optional[date]] = mapped_column(DateTime, nullable=True)
    subtotal: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    total: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    balance_remaining: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    is_paid: Mapped[bool] = mapped_column(default=False)
    memo: Mapped[str] = mapped_column(Text, default="")

    time_modified: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    mirrored_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def collected(self) -> Decimal:
        """What has come in against this invoice.

        Total less the balance still outstanding, which is how a partial
        payment shows up. Never the `is_paid` flag: on a progress-billed roof
        a partial payment is the normal case, and a boolean cannot say
        "$40,000 of $96,400".
        """
        zero = Decimal("0")
        return (self.total or zero) - (self.balance_remaining or zero)


class QbSyncState(Base):
    """Where we got to, per entity, so the next poll asks for a little.

    QuickBooks is asked for what changed since a cursor rather than for
    everything, because a full pull locks other users out of the company file
    and this runs while people are working in it.
    """

    __tablename__ = "qb_sync_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    cursor: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str] = mapped_column(String(16), default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    rows: Mapped[int] = mapped_column(Integer, default=0)


# --- writes, the other way ------------------------------------------------

OUT_PENDING = "pending"
OUT_SENT = "sent"
OUT_CONFIRMED = "confirmed"
OUT_FAILED = "failed"

OUT_LABELS = {
    OUT_PENDING: "Waiting for the next sync",
    OUT_SENT: "Sent, waiting for QuickBooks to confirm",
    OUT_CONFIRMED: "In QuickBooks",
    OUT_FAILED: "Refused by QuickBooks",
}


class QbOutbox(Base):
    """A write queued for QuickBooks, and what became of it.

    Nothing is written to QuickBooks the moment a person clicks approve,
    because there is nothing to write to - the connector will not be back for
    minutes. So the intention is recorded here and drained on the next poll.

    `request_id` is ours and unique. With a polling connector a timeout is
    ordinary rather than exceptional, and a retry that created a second bill
    would make this system a source of the exact problem it exists to catch.
    """

    __tablename__ = "qb_outbox"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    op: Mapped[str] = mapped_column(String(32), index=True)      # BillAdd, ...
    entity_type: Mapped[str] = mapped_column(String(32), default="")
    entity_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")

    status: Mapped[str] = mapped_column(String(16), default=OUT_PENDING, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    qb_txn_id: Mapped[str] = mapped_column(String(64), default="")

    queued_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("op", "entity_type", "entity_id", name="uq_outbox_entity"),
    )

    @property
    def status_label(self) -> str:
        return OUT_LABELS.get(self.status, self.status)

"""Database models.

The shape follows how the business actually works:

    Job  ──▶  one MASTER Quote  (all material for that job)
      │
      └────▶  many Invoices     (materials arrive in several deliveries)

Each invoice line is compared back to the matching master-quote line, and the
result of that comparison is stored on the line as a `verdict`.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, Money

# --- verdicts -------------------------------------------------------------
# These drive the colours on the marked-up invoice.
VERDICT_OVER = "over"           # billed higher than quoted  -> RED
VERDICT_UNDER = "under"         # billed lower than quoted   -> GREEN
VERDICT_MATCH = "match"         # exactly as quoted          -> GOLD
VERDICT_NOT_ON_QUOTE = "not_on_quote"  # no matching quote line -> GREY

VERDICT_LABELS = {
    VERDICT_OVER: "Over quote",
    VERDICT_UNDER: "Under quote",
    VERDICT_MATCH: "As quoted",
    VERDICT_NOT_ON_QUOTE: "Not on quote",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Job(Base):
    __tablename__ = "job"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_number: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    quotes: Mapped[list["Quote"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    documents: Mapped[list["Document"]] = relationship(back_populates="job")
    aliases: Mapped[list["JobAlias"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )

    @property
    def masters(self) -> list["Quote"]:
        """Current master quote for each vendor on this job.

        Usually one. A job that also has a dumpster company or a lumber yard
        carries one master per vendor, and each vendor's invoices are checked
        against their own quote.
        """
        return [q for q in self.quotes if q.is_master]

    @property
    def master_quote(self) -> Optional["Quote"]:
        """The single master, when there is exactly one. Convenience only."""
        masters = self.masters
        return masters[0] if len(masters) == 1 else (masters[0] if masters else None)

    def master_for_vendor(self, vendor: str) -> tuple[Optional["Quote"], str]:
        """Find the master quote to price an invoice from `vendor` against.

        Returns (quote, how) where `how` is one of:
            "vendor"    - matched this vendor by name
            "sole"      - no name match, but the job has exactly one master, so
                          it is used and the mismatch is surfaced to the reader
            "none"      - nothing to compare against
        """
        from app.matching import vendor_matches

        masters = self.masters
        if not masters:
            return None, "none"
        for quote in masters:
            if vendor_matches(quote.vendor, vendor):
                return quote, "vendor"
        if len(masters) == 1:
            # Vendor names differ between the quote and the invoice - abbreviated
            # trading names are normal. Compare anyway rather than silently
            # checking nothing, and say so on the marked-up copy.
            return masters[0], "sole"
        return None, "none"


class JobAlias(Base):
    """Another name the same job answers to.

    Vendors rarely carry our job number. New Castle's PO field held
    "63 winding ridge" - the site address - and their Job field was blank. So
    when a document is filed by hand, whatever reference it DID carry is
    recorded here, and the next document quoting that address files itself.
    """

    __tablename__ = "job_alias"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("job.id"), index=True)
    alias: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(32), default="manual")  # manual|po|ship_to
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    job: Mapped["Job"] = relationship(back_populates="aliases")


class Document(Base):
    """A file that arrived, before we know what it is."""

    __tablename__ = "document"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("job.id"), nullable=True, index=True)

    filename: Mapped[str] = mapped_column(String(512))
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    stored_path: Mapped[str] = mapped_column(String(1024))
    mime_type: Mapped[str] = mapped_column(String(128), default="application/pdf")

    kind: Mapped[str] = mapped_column(String(32), default="unknown")  # quote|invoice|unknown
    source: Mapped[str] = mapped_column(String(32), default="upload")  # upload|email
    sender: Mapped[str] = mapped_column(String(255), default="")
    subject: Mapped[str] = mapped_column(Text, default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    email_message_id: Mapped[str] = mapped_column(String(512), default="", index=True)

    # extracted | matched | needs_job | needs_quote | error
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    error: Mapped[str] = mapped_column(Text, default="")

    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    job: Mapped[Optional[Job]] = relationship(back_populates="documents")


class Extraction(Base):
    """Audit trail: exactly what the model returned for a document."""

    __tablename__ = "extraction"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id"), index=True)
    model: Mapped[str] = mapped_column(String(64), default="")
    payload_json: Mapped[str] = mapped_column(Text, default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Quote(Base):
    __tablename__ = "quote"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("job.id"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id"))

    # Exactly one master quote per job at a time. Superseding a master keeps the
    # old row for history rather than deleting it.
    is_master: Mapped[bool] = mapped_column(default=True, index=True)
    superseded_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    supersede_reason: Mapped[str] = mapped_column(Text, default="")

    vendor: Mapped[str] = mapped_column(String(255), default="")
    quote_number: Mapped[str] = mapped_column(String(128), default="")
    quote_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    po_reference: Mapped[str] = mapped_column(String(255), default="")
    ship_to: Mapped[str] = mapped_column(Text, default="")
    page_info: Mapped[str] = mapped_column(String(32), default="")

    subtotal: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    tax: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    freight: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    total: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    job: Mapped[Job] = relationship(back_populates="quotes")
    document: Mapped[Document] = relationship()
    lines: Mapped[list["QuoteLine"]] = relationship(
        back_populates="quote", cascade="all, delete-orphan", order_by="QuoteLine.line_no"
    )


class QuoteLine(Base):
    __tablename__ = "quote_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    quote_id: Mapped[int] = mapped_column(ForeignKey("quote.id"), index=True)

    line_no: Mapped[int] = mapped_column(Integer, default=0)
    sku: Mapped[str] = mapped_column(String(128), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    qty: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    uom: Mapped[str] = mapped_column(String(32), default="")
    price_uom: Mapped[str] = mapped_column(String(32), default="")
    unit_price: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    extended: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)

    quote: Mapped[Quote] = relationship(back_populates="lines")


class Invoice(Base):
    __tablename__ = "invoice"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("job.id"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id"))
    # The master quote this invoice was compared against (may be null if none yet).
    quote_id: Mapped[Optional[int]] = mapped_column(ForeignKey("quote.id"), nullable=True)
    # How that master was chosen: "vendor" (name matched), "sole" (only master
    # on the job, names differ), or "none".
    quote_match: Mapped[str] = mapped_column(String(16), default="none")

    vendor: Mapped[str] = mapped_column(String(255), default="")
    invoice_number: Mapped[str] = mapped_column(String(128), default="", index=True)
    invoice_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    due_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    po_reference: Mapped[str] = mapped_column(String(255), default="")
    ship_to: Mapped[str] = mapped_column(Text, default="")
    page_info: Mapped[str] = mapped_column(String(32), default="")

    subtotal: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    tax: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    freight: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    total: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)

    # Rolled-up result of the line comparison.
    overbilled_amount: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    underbilled_amount: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    lines_over: Mapped[int] = mapped_column(Integer, default=0)
    lines_under: Mapped[int] = mapped_column(Integer, default=0)
    lines_match: Mapped[int] = mapped_column(Integer, default=0)
    lines_unmatched: Mapped[int] = mapped_column(Integer, default=0)

    render_path: Mapped[str] = mapped_column(String(1024), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    job: Mapped[Job] = relationship(back_populates="invoices")
    document: Mapped[Document] = relationship()
    quote: Mapped[Optional[Quote]] = relationship()
    lines: Mapped[list["InvoiceLine"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", order_by="InvoiceLine.line_no"
    )

    __table_args__ = (
        UniqueConstraint("job_id", "vendor", "invoice_number", name="uq_invoice_per_job"),
    )

    @property
    def has_overbilling(self) -> bool:
        return bool(self.overbilled_amount and self.overbilled_amount > 0)


class InvoiceLine(Base):
    __tablename__ = "invoice_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoice.id"), index=True)
    quote_line_id: Mapped[Optional[int]] = mapped_column(ForeignKey("quote_line.id"), nullable=True)

    line_no: Mapped[int] = mapped_column(Integer, default=0)
    sku: Mapped[str] = mapped_column(String(128), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    qty: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    uom: Mapped[str] = mapped_column(String(32), default="")
    price_uom: Mapped[str] = mapped_column(String(32), default="")
    unit_price: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    extended: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)

    # Comparison result
    quote_unit_price: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    verdict: Mapped[str] = mapped_column(String(32), default=VERDICT_NOT_ON_QUOTE, index=True)
    unit_variance: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    extended_variance: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    match_method: Mapped[str] = mapped_column(String(32), default="")  # sku|exact|fuzzy|llm|none

    invoice: Mapped[Invoice] = relationship(back_populates="lines")
    quote_line: Mapped[Optional[QuoteLine]] = relationship()

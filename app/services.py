"""The ingestion pipeline: file in, compared invoice out.

    ingest_file()
        -> store + de-duplicate
        -> extract with Claude
        -> work out which job it belongs to
        -> quote?   become (or replace) the job's master quote, then
                    re-compare every invoice already on that job
           invoice? compare against the job's master quote
"""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import to_decimal
from app.extract import (
    ExtractionError,
    ExtractionResult,
    extract_document,
    normalize_job_number,
    parse_job_answer,
    parse_job_directive,
)
from app.approval import apply_routing
from app.matching import compare_invoice, vendor_matches
from app.models import (
    Document,
    JobAlias,
    Extraction,
    Invoice,
    InvoiceLine,
    Job,
    Quote,
    QuoteLine,
    utcnow,
)

# Document.status values
ST_NEEDS_JOB = "needs_job"        # extracted, but nobody said which job
ST_NEEDS_QUOTE = "needs_quote"    # invoice filed, but the job has no master quote yet
ST_READY = "ready"
ST_ERROR = "error"
ST_OTHER = "other"                # not a quote or invoice (statement, packing slip)


class IngestError(RuntimeError):
    pass


class DuplicateDocument(IngestError):
    def __init__(self, document: Document):
        super().__init__(f"Already received this file: {document.filename}")
        self.document = document


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def resolve_job_by_alias(session: Session, reference: str) -> Optional[Job]:
    """Find a job by an address or PO reference we have seen before."""
    key = normalize_job_number(reference)
    if not key:
        return None
    alias = session.scalar(select(JobAlias).where(JobAlias.alias == key))
    return alias.job if alias else None


def remember_alias(session: Session, job: Job, reference: str, source: str = "manual") -> None:
    """Teach the system that `reference` means this job.

    Vendors put their own reference on our paperwork - usually the site address.
    Recording it the first time a human files the document by hand means the
    next one from that vendor files itself.
    """
    key = normalize_job_number(reference)
    if not key or key == normalize_job_number(job.job_number):
        return
    existing = session.scalar(select(JobAlias).where(JobAlias.alias == key))
    if existing is not None:
        return
    session.add(JobAlias(job_id=job.id, alias=key, source=source))
    session.flush()


def get_or_create_job(session: Session, job_number: str, name: str = "") -> Job:
    number = normalize_job_number(job_number)
    if not number:
        raise IngestError("A job number is required.")
    job = session.scalar(select(Job).where(Job.job_number == number))
    if job is None:
        job = Job(job_number=number, name=name or "")
        session.add(job)
        session.flush()
    elif name and not job.name:
        job.name = name
    return job


def store_upload(src: Path, filename: str) -> tuple[Path, str]:
    """Copy a file into the content-addressed store. Returns (path, sha256)."""
    digest = _sha256(src)
    suffix = Path(filename).suffix.lower() or ".pdf"
    dest = settings.uploads_dir / f"{digest}{suffix}"
    if not dest.exists():
        shutil.copyfile(src, dest)
    return dest, digest


# --- quote / invoice persistence -----------------------------------------

def _apply_lines(result: ExtractionResult, make_line):
    """Turn extracted line dicts into ORM rows via the supplied constructor."""
    rows = []
    for i, raw in enumerate(result.lines, start=1):
        row = make_line(
            line_no=int(raw.get("line_no") or i),
            sku=(raw.get("sku") or "").strip(),
            description=(raw.get("description") or "").strip(),
            qty=to_decimal(raw.get("qty")),
            uom=(raw.get("uom") or "").strip(),
            price_uom=(raw.get("price_uom") or "").strip(),
            unit_price=to_decimal(raw.get("unit_price")),
            extended=to_decimal(raw.get("extended")),
        )
        rows.append(row)
    return rows


def create_quote(
    session: Session,
    job: Job,
    document: Document,
    result: ExtractionResult,
    make_master: bool,
    reason: str = "",
) -> Quote:
    payload = result.payload
    quote = Quote(
        job_id=job.id,
        document_id=document.id,
        is_master=False,  # set below, after any existing master is stood down
        vendor=(payload.get("vendor") or "").strip(),
        quote_number=(payload.get("document_number") or "").strip(),
        quote_date=_parse_date(payload.get("document_date")),
        po_reference=(payload.get("job_number_hint") or "").strip(),
        ship_to=(payload.get("ship_to") or "").strip(),
        page_info=(payload.get("page_info") or "").strip(),
        subtotal=to_decimal(payload.get("subtotal")),
        tax=to_decimal(payload.get("tax")),
        freight=to_decimal(payload.get("freight")),
        total=to_decimal(payload.get("total")),
    )
    session.add(quote)
    session.flush()

    quote.lines = _apply_lines(result, lambda **kw: QuoteLine(quote_id=quote.id, **kw))
    session.flush()

    if make_master:
        set_master_quote(session, job, quote, reason=reason)
    return quote


def set_master_quote(session: Session, job: Job, quote: Quote, reason: str = "") -> None:
    """Promote a quote to master for its VENDOR on this job.

    A job usually has one supplier, but it can have several - a roofing
    supplier plus a dumpster company, say. Each vendor gets its own master, so a
    new roofing quote must not stand down the dumpster quote.

    Superseded masters are kept, not deleted; the history of what was agreed at
    each point is the whole value of this system.
    """
    existing = session.scalars(
        select(Quote).where(Quote.job_id == job.id, Quote.is_master == True)  # noqa: E712
    ).all()
    for old in existing:
        if old.id == quote.id:
            continue
        if not vendor_matches(old.vendor, quote.vendor):
            continue  # different supplier on the same job - leave it standing
        old.is_master = False
        old.superseded_at = utcnow()
        old.supersede_reason = reason or f"Replaced by quote {quote.quote_number or quote.id}"

    quote.is_master = True
    quote.superseded_at = None
    session.flush()


def create_invoice(
    session: Session, job: Job, document: Document, result: ExtractionResult
) -> Invoice:
    payload = result.payload
    number = (payload.get("document_number") or "").strip()
    vendor = (payload.get("vendor") or "").strip()

    existing = session.scalar(
        select(Invoice).where(
            Invoice.job_id == job.id,
            Invoice.vendor == vendor,
            Invoice.invoice_number == number,
            Invoice.invoice_number != "",
        )
    )
    if existing is not None:
        raise IngestError(
            f"Invoice {number} from {vendor} is already on job {job.job_number}. "
            "This looks like a duplicate."
        )

    invoice = Invoice(
        job_id=job.id,
        document_id=document.id,
        vendor=vendor,
        invoice_number=number,
        invoice_date=_parse_date(payload.get("document_date")),
        due_date=_parse_date(payload.get("due_date")),
        po_reference=(payload.get("job_number_hint") or "").strip(),
        ship_to=(payload.get("ship_to") or "").strip(),
        page_info=(payload.get("page_info") or "").strip(),
        subtotal=to_decimal(payload.get("subtotal")),
        tax=to_decimal(payload.get("tax")),
        freight=to_decimal(payload.get("freight")),
        total=to_decimal(payload.get("total")),
    )
    session.add(invoice)
    session.flush()

    invoice.lines = _apply_lines(result, lambda **kw: InvoiceLine(invoice_id=invoice.id, **kw))
    session.flush()

    recompare_invoice(session, job, invoice)
    return invoice


def recompare_invoice(session: Session, job: Job, invoice: Invoice) -> None:
    """Re-run the comparison for one invoice against the job's current master."""
    master, how = job.master_for_vendor(invoice.vendor)
    invoice.quote_id = master.id if master else None
    invoice.quote_match = how

    if master is None:
        for line in invoice.lines:
            line.verdict = "not_on_quote"
            line.match_method = "no_master_quote"
            line.quote_line_id = None
            line.quote_unit_price = None
            line.unit_variance = None
            line.extended_variance = None
        invoice.overbilled_amount = Decimal("0")
        invoice.underbilled_amount = Decimal("0")
        invoice.lines_over = invoice.lines_under = invoice.lines_match = 0
        invoice.lines_unmatched = len(invoice.lines)
        apply_routing(invoice)
        session.flush()
        return

    summary = compare_invoice(list(invoice.lines), list(master.lines))
    invoice.overbilled_amount = summary.overbilled
    invoice.underbilled_amount = summary.underbilled
    invoice.lines_over = summary.lines_over
    invoice.lines_under = summary.lines_under
    invoice.lines_match = summary.lines_match
    invoice.lines_unmatched = summary.lines_unmatched
    invoice.render_path = ""  # stale: the marked-up PDF must be regenerated
    apply_routing(invoice)
    session.flush()


def recompare_job(session: Session, job: Job) -> int:
    """Re-compare every invoice on a job. Called when the master quote changes."""
    invoices = session.scalars(select(Invoice).where(Invoice.job_id == job.id)).all()
    for invoice in invoices:
        recompare_invoice(session, job, invoice)
    return len(invoices)


# --- the entry point ------------------------------------------------------

def ingest_file(
    session: Session,
    src_path: Path,
    filename: str,
    *,
    source: str = "upload",
    sender: str = "",
    subject: str = "",
    body: str = "",
    note: str = "",
    message_id: str = "",
    job_number_override: str = "",
    force_master: bool = False,
) -> Document:
    """Ingest one document end to end. Returns the Document with status set."""
    stored, digest = store_upload(src_path, filename)

    existing = session.scalar(select(Document).where(Document.sha256 == digest))
    if existing is not None:
        raise DuplicateDocument(existing)

    document = Document(
        filename=filename,
        sha256=digest,
        stored_path=str(stored),
        mime_type="application/pdf" if stored.suffix == ".pdf" else "image/*",
        source=source,
        sender=sender,
        subject=subject,
        body_text=body,
        email_message_id=message_id,
        status="pending",
    )
    session.add(document)
    session.flush()

    # What did the human tell us? Subject line first - that is where the job
    # number is written when an invoice is forwarded in.
    directive = parse_job_directive(note, subject, body)

    try:
        hint_parts = [p for p in (note, subject) if p]
        result = extract_document(stored, hint="\n".join(hint_parts))
    except ExtractionError as exc:
        document.status = ST_ERROR
        document.error = str(exc)
        session.flush()
        return document

    session.add(Extraction(
        document_id=document.id,
        model=result.model,
        payload_json=result.raw_json,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    ))

    document.kind = result.doc_type
    if result.doc_type not in ("quote", "invoice"):
        document.status = ST_OTHER
        document.error = "Not a quote or invoice - filed without comparison."
        session.flush()
        return document

    # Job number: an explicit override wins, then what the person wrote, then
    # whatever was printed on the document itself.
    job_number = (
        normalize_job_number(job_number_override)
        or directive.job_number
        or normalize_job_number(result.payload.get("job_number_hint") or "")
    )
    # Before giving up, try the references the document DOES carry against
    # aliases learned from previously filed paperwork.
    job = None
    if not job_number:
        for candidate, src in (
            (result.payload.get("job_number_hint") or "", "po"),
            (result.payload.get("ship_to") or "", "ship_to"),
        ):
            job = resolve_job_by_alias(session, candidate)
            if job is not None:
                break

    if job is None and not job_number:
        document.status = ST_NEEDS_JOB
        document.error = (
            "No job number found. Assign one and this reference will be "
            "remembered for next time."
        )
        session.flush()
        return document

    if job is None:
        job = get_or_create_job(session, job_number)
        # Learn whatever reference the vendor used, so the next one self-files.
        remember_alias(session, job, result.payload.get("job_number_hint") or "", "po")
    document.job_id = job.id

    if result.doc_type == "quote":
        vendor_name = (result.payload.get("vendor") or "").strip()
        existing_master, _ = job.master_for_vendor(vendor_name)
        had_master = existing_master is not None
        make_master = force_master or directive.is_master_update or not had_master
        reason = directive.matched_phrase if directive.is_master_update else ""
        create_quote(session, job, document, result, make_master=make_master, reason=reason)
        if make_master and had_master:
            recompare_job(session, job)
        elif make_master:
            recompare_job(session, job)
        document.status = ST_READY
    else:
        invoice = create_invoice(session, job, document, result)
        document.status = ST_READY if invoice.quote_id else ST_NEEDS_QUOTE

    session.flush()
    return document


def file_stored_document(
    session: Session,
    document: Document,
    job_number: str,
    force_master: bool = False,
) -> Document:
    """File an already-extracted document against a job.

    Used when someone assigns a job number from the Inbox. Reuses the stored
    extraction rather than sending the document to Claude a second time - the
    reading was already done and paid for, and re-reading could produce a
    slightly different result for no benefit.
    """
    extraction = session.scalar(
        select(Extraction)
        .where(Extraction.document_id == document.id)
        .order_by(Extraction.created_at.desc())
    )
    if extraction is None:
        raise IngestError(
            f"{document.filename} was never read successfully, so it cannot be filed. "
            "Re-upload it."
        )

    try:
        payload = json.loads(extraction.payload_json)
    except (TypeError, ValueError) as exc:
        raise IngestError(f"Stored extraction for {document.filename} is unreadable.") from exc

    result = ExtractionResult(payload=payload, model=extraction.model)
    if result.doc_type not in ("quote", "invoice"):
        raise IngestError(
            f"{document.filename} was read as '{result.doc_type}', not a quote or invoice."
        )

    job = get_or_create_job(session, job_number)
    document.job_id = job.id
    document.kind = result.doc_type
    document.error = ""

    # The reason this document needed filing by hand was that its own reference
    # meant nothing to us. Now it does.
    remember_alias(session, job, payload.get("job_number_hint") or "", "po")

    if result.doc_type == "quote":
        make_master = force_master or job.master_quote is None
        create_quote(session, job, document, result, make_master=make_master)
        if make_master:
            recompare_job(session, job)
        document.status = ST_READY
    else:
        invoice = create_invoice(session, job, document, result)
        document.status = ST_READY if invoice.quote_id else ST_NEEDS_QUOTE

    session.flush()
    return document

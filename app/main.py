"""FastAPI application: dashboard, upload, and the marked-up invoice."""
from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus, urlencode

from fastapi import Depends, FastAPI, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app import auth
from app.config import settings
from app.db import get_session, init_db, to_decimal
from app.extract import normalize_job_number
from app.approval import (
    ACTION_HOLD,
    apply_routing,
    find_receipt,
    route,
)
from app.matching import norm_vendor
from app.models import (
    APPROVAL_APPROVED,
    APPROVAL_HELD,
    APPROVAL_PAID,
    APPROVAL_PENDING,
    APPROVAL_LABELS,
    APPROVAL_REJECTED,
    RECEIPT_DELIVERY,
    RECEIPT_WORK,
    Approval,
    ChangeOrder,
    Document,
    Invoice,
    Job,
    JobAlias,
    Quote,
    Receipt,
    TIER_LABELS,
    utcnow,
)
from app.pdf import PdfUnavailable, pdf_available, render_html_to_pdf
from app.services import (
    ST_ERROR,
    ST_NEEDS_JOB,
    ST_OTHER,
    DuplicateDocument,
    IngestError,
    file_stored_document,
    ingest_file,
)

BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title=settings.site_name)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

ZERO = Decimal("0")


# --- template filters -----------------------------------------------------

def _fmt(value: Optional[Decimal], places: int) -> str:
    if value is None:
        return "—"
    q = Decimal(1).scaleb(-places)
    return f"${value.quantize(q):,}"


def f_money(value):
    return _fmt(value, 2)


def f_money4(value):
    """Money with up to 4 decimals, but only as many as the price actually uses.

    Unit prices are frequently quoted at 3 or 4 decimals; showing $4.10 when the
    quote says $4.1025 would hide the very difference this system exists to find.
    """
    if value is None:
        return "—"
    d = Decimal(value).quantize(Decimal("0.0001")).normalize()
    exponent = d.as_tuple().exponent
    places = max(2, -exponent if isinstance(exponent, int) and exponent < 0 else 2)
    return _fmt(value, min(places, 4))


def f_abs_money(value):
    return "—" if value is None else _fmt(abs(value), 2)


def f_abs_money4(value):
    return "—" if value is None else f_money4(abs(value))


def f_qty(value):
    if value is None:
        return "—"
    d = Decimal(value).normalize()
    if d == d.to_integral_value():
        return f"{d.to_integral_value():,}"
    return format(d, "f")


templates.env.filters.update(
    money=f_money, money4=f_money4, qty=f_qty,
    abs_money=f_abs_money, abs_money4=f_abs_money4,
)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    for problem in auth.warnings():
        logging.getLogger("finance").warning("CONFIG: %s", problem)


# --- access control -------------------------------------------------------

@app.middleware("http")
async def require_login(request: Request, call_next):
    """Every page requires a session cookie, except the login page itself."""
    if auth.auth_required() and not auth.is_public(request.url.path):
        if not auth.valid_token(request.cookies.get(auth.COOKIE_NAME)):
            target = request.url.path
            if request.url.query:
                target = f"{target}?{request.url.query}"
            return RedirectResponse(f"/login?next={quote_plus(target)}", status_code=303)
    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if auth.valid_token(request.cookies.get(auth.COOKIE_NAME)):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {
        "request": request,
        "site_name": settings.site_name,
        "error": request.query_params.get("error", ""),
        "next_url": request.query_params.get("next", "/"),
    })


@app.post("/login")
def login_submit(password: str = Form(""), next: str = Form("/")):
    if not auth.verify(password):
        return RedirectResponse(
            f"/login?error={quote_plus('Incorrect password.')}&next={quote_plus(next or '/')}",
            status_code=303,
        )
    target = next if next.startswith("/") else "/"
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        auth.COOKIE_NAME, auth.make_token(),
        max_age=settings.session_days * 86400,
        httponly=True, samesite="lax",
        secure=settings.base_url.startswith("https"),
    )
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE_NAME)
    return response


# --- helpers --------------------------------------------------------------

def _messages(request: Request) -> list[tuple[str, str]]:
    """Flash messages passed through the redirect querystring."""
    out = []
    for cat in ("ok", "err", "warn"):
        for msg in request.query_params.getlist(cat):
            out.append((cat, msg))
    return out


def _ctx(request: Request, session: Session, **kw) -> dict:
    count = len(session.scalars(
        select(Document.id).where(Document.status.in_([ST_NEEDS_JOB, ST_ERROR, ST_OTHER]))
    ).all())
    base = {
        "request": request,
        "site_name": settings.site_name,
        "messages": _messages(request),
        "q": request.query_params.get("q", ""),
        "unassigned_count": count,
    }
    base.update(kw)
    return base


def _redirect(path: str, **params) -> RedirectResponse:
    query = urlencode([(k, v) for k, v in params.items() if v], doseq=True)
    return RedirectResponse(f"{path}?{query}" if query else path, status_code=303)


@dataclass
class JobRow:
    job: Job
    master: Optional[Quote]
    invoice_count: int
    invoiced_total: Decimal
    overbilled: Decimal


def _job_rows(session: Session, jobs: list[Job]) -> list[JobRow]:
    rows = []
    for job in jobs:
        invoices = job.invoices
        rows.append(JobRow(
            job=job,
            master=job.master_quote,
            invoice_count=len(invoices),
            invoiced_total=sum((i.total or ZERO for i in invoices), ZERO),
            overbilled=sum((i.overbilled_amount or ZERO for i in invoices), ZERO),
        ))
    return rows


# --- routes ---------------------------------------------------------------

@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "pdf": pdf_available(), "mail": settings.mail_configured()}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)):
    q = (request.query_params.get("q") or "").strip()
    flagged = request.query_params.get("flagged") == "1"

    stmt = select(Job).options(
        selectinload(Job.quotes).selectinload(Quote.lines),
        selectinload(Job.invoices),
    ).order_by(Job.created_at.desc())

    if q:
        like = f"%{q}%"
        matching_job_ids = set()
        for job in session.scalars(select(Job).where(
            or_(Job.job_number.ilike(like), Job.name.ilike(like))
        )).all():
            matching_job_ids.add(job.id)
        for inv in session.scalars(select(Invoice).where(
            or_(Invoice.vendor.ilike(like), Invoice.invoice_number.ilike(like))
        )).all():
            matching_job_ids.add(inv.job_id)
        for quote in session.scalars(select(Quote).where(
            or_(Quote.vendor.ilike(like), Quote.quote_number.ilike(like),
                Quote.po_reference.ilike(like))
        )).all():
            matching_job_ids.add(quote.job_id)
        # Vendors reference our jobs by site address; those aliases are searchable.
        for alias in session.scalars(select(JobAlias).where(JobAlias.alias.ilike(like))).all():
            matching_job_ids.add(alias.job_id)
        stmt = stmt.where(Job.id.in_(matching_job_ids or {-1}))

    rows = _job_rows(session, list(session.scalars(stmt).unique().all()))
    if flagged:
        rows = [r for r in rows if r.overbilled > 0]

    return templates.TemplateResponse(request, "dashboard.html", _ctx(
        request, session,
        jobs=rows,
        needs_attention=sum(1 for r in rows if r.overbilled > 0),
        total_overbilled=sum((r.overbilled for r in rows), ZERO),
    ))


@app.get("/job/{job_number}", response_class=HTMLResponse)
def job_detail(job_number: str, request: Request, session: Session = Depends(get_session)):
    job = session.scalar(
        select(Job).where(Job.job_number == normalize_job_number(job_number))
    )
    if job is None:
        return _redirect("/", err=f"No job {job_number}.")

    invoices = sorted(job.invoices, key=lambda i: (i.invoice_date or i.created_at.date(), i.id))
    superseded = [q for q in job.quotes if not q.is_master]

    return templates.TemplateResponse(request, "job.html", _ctx(
        request, session,
        job=job,
        masters=job.masters,
        superseded=superseded,
        invoices=invoices,
        invoiced_total=sum((i.total or ZERO for i in invoices), ZERO),
        quoted_total=sum((m.total or ZERO for m in job.masters), ZERO),
        total_overbilled=sum((i.overbilled_amount or ZERO for i in invoices), ZERO),
    ))


@app.get("/inbox", response_class=HTMLResponse)
def inbox(request: Request, session: Session = Depends(get_session)):
    docs = session.scalars(
        select(Document)
        .where(Document.status.in_([ST_NEEDS_JOB, ST_ERROR, ST_OTHER]))
        .order_by(Document.received_at.desc())
    ).all()
    return templates.TemplateResponse(request, "inbox.html", _ctx(request, session, documents=list(docs)))


@app.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "upload.html", _ctx(
        request, session,
        job=request.query_params.get("job", ""),
        force_master=request.query_params.get("master") == "1",
    ))


@app.post("/upload")
async def upload_submit(
    files: list[UploadFile],
    job_number: str = Form(""),
    note: str = Form(""),
    force_master: str = Form(""),
    session: Session = Depends(get_session),
):
    oks: list[str] = []
    errs: list[str] = []
    last_job: Optional[str] = None

    for upload in files:
        if not upload.filename:
            continue
        suffix = Path(upload.filename).suffix or ".pdf"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(await upload.read())
            tmp_path = Path(tmp.name)
        try:
            doc = ingest_file(
                session, tmp_path, upload.filename,
                source="upload", note=note,
                job_number_override=job_number,
                force_master=bool(force_master),
            )
            session.commit()

            if doc.status == ST_ERROR:
                errs.append(f"{upload.filename}: {doc.error}")
            elif doc.status == ST_NEEDS_JOB:
                errs.append(f"{upload.filename}: no job number found — waiting in the Inbox.")
            elif doc.status == ST_OTHER:
                errs.append(f"{upload.filename}: not a quote or invoice — filed in the Inbox.")
            else:
                job = doc.job
                last_job = job.job_number if job else last_job
                label = "Master quote" if doc.kind == "quote" else "Invoice"
                oks.append(f"{label} read from {upload.filename} → job {job.job_number}.")
        except DuplicateDocument as exc:
            existing = exc.document
            where = f"job {existing.job.job_number}" if existing.job else "the Inbox"
            errs.append(f"{upload.filename}: already received — filed under {where}.")
            session.rollback()
        except (IngestError, Exception) as exc:  # noqa: BLE001 - surface, never 500
            errs.append(f"{upload.filename}: {exc}")
            session.rollback()
        finally:
            tmp_path.unlink(missing_ok=True)

    target = f"/job/{last_job}" if last_job and not errs else "/upload"
    return _redirect(target, ok=oks, err=errs)


@app.post("/document/{doc_id}/assign")
def assign_document(
    doc_id: int, job_number: str = Form(...), session: Session = Depends(get_session)
):
    doc = session.get(Document, doc_id)
    if doc is None:
        return _redirect("/inbox", err="That document no longer exists.")
    try:
        file_stored_document(session, doc, job_number)
        session.commit()
    except IngestError as exc:
        session.rollback()
        return _redirect("/inbox", err=str(exc))

    if doc.job:
        return _redirect(f"/job/{doc.job.job_number}", ok=f"Filed {doc.filename}.")
    return _redirect("/inbox", err=f"Could not file {doc.filename}.")


@app.get("/document/{doc_id}/file")
def document_file(doc_id: int, session: Session = Depends(get_session)):
    doc = session.get(Document, doc_id)
    if doc is None or not Path(doc.stored_path).exists():
        return _redirect("/", err="That file is missing.")
    return FileResponse(
        doc.stored_path,
        media_type="application/pdf" if doc.stored_path.endswith(".pdf") else None,
        filename=doc.filename,
        content_disposition_type="inline",
    )


def _render_markup(request: Request, invoice: Invoice, print_mode: bool) -> str:
    routing = None if print_mode else route(invoice)
    return templates.get_template("markup.html").render(
        request=request,
        invoice=invoice,
        job=invoice.job,
        quote=invoice.quote,
        lines=invoice.lines,
        print_mode=print_mode,
        routing=routing,
        status_label=APPROVAL_LABELS.get(invoice.approval_status, invoice.approval_status),
        tier_label=TIER_LABELS.get(routing.tier, "") if routing else "",
        generated_at=datetime.now().strftime("%d %b %Y at %H:%M"),
    )


@app.get("/invoice/{invoice_id}", response_class=HTMLResponse)
def invoice_markup(invoice_id: int, request: Request, session: Session = Depends(get_session)):
    invoice = session.get(Invoice, invoice_id)
    if invoice is None:
        return _redirect("/", err="No such invoice.")
    return HTMLResponse(_render_markup(request, invoice, print_mode=False))


@app.get("/invoice/{invoice_id}/pdf")
def invoice_pdf(invoice_id: int, request: Request, session: Session = Depends(get_session)):
    invoice = session.get(Invoice, invoice_id)
    if invoice is None:
        return _redirect("/", err="No such invoice.")

    name = (invoice.invoice_number or str(invoice.id)).replace("/", "-")
    out = settings.renders_dir / f"job{invoice.job.job_number}-invoice-{name}-checked.pdf"

    try:
        render_html_to_pdf(_render_markup(request, invoice, print_mode=True), out)
    except PdfUnavailable as exc:
        return _redirect(f"/invoice/{invoice_id}", err=f"Could not build the PDF: {exc}")

    invoice.render_path = str(out)
    session.commit()

    return FileResponse(out, media_type="application/pdf", filename=out.name)


# --- three-way match: receipts, change orders, approval -------------------

def _actor(name: str) -> str:
    """Who took this action.

    A single shared password means the app cannot know who is signed in, so the
    approver types their name. That is weaker than real accounts and it is
    recorded as-typed - see README for the upgrade path to per-user logins.
    """
    return (name or "").strip()[:128] or "unnamed"


@app.post("/job/{job_number}/receipt")
def confirm_receipt(
    job_number: str,
    vendor: str = Form(""),
    kind: str = Form(RECEIPT_DELIVERY),
    reference: str = Form(""),
    confirmed_by: str = Form(""),
    note: str = Form(""),
    session: Session = Depends(get_session),
):
    """Record that material arrived, or that a phase of work was completed."""
    job = session.scalar(
        select(Job).where(Job.job_number == normalize_job_number(job_number))
    )
    if job is None:
        return _redirect("/", err=f"No job {job_number}.")

    session.add(Receipt(
        job_id=job.id,
        vendor=vendor.strip(),
        kind=kind if kind in (RECEIPT_DELIVERY, RECEIPT_WORK) else RECEIPT_DELIVERY,
        reference=reference.strip(),
        note=note.strip(),
        confirmed_by=_actor(confirmed_by),
    ))
    session.flush()

    # A confirmation can unblock invoices already sitting in the queue.
    session.refresh(job)
    for invoice in job.invoices:
        apply_routing(invoice)
    session.commit()

    label = "Delivery" if kind == RECEIPT_DELIVERY else "Work completion"
    return _redirect(f"/job/{job.job_number}", ok=f"{label} confirmed for {vendor or 'vendor'}.")


@app.post("/job/{job_number}/change-order")
def add_change_order(
    job_number: str,
    vendor: str = Form(""),
    number: str = Form(""),
    amount: str = Form(""),
    description: str = Form(""),
    approved_by: str = Form(""),
    session: Session = Depends(get_session),
):
    """Record written authorisation for scope beyond the original quote."""
    job = session.scalar(
        select(Job).where(Job.job_number == normalize_job_number(job_number))
    )
    if job is None:
        return _redirect("/", err=f"No job {job_number}.")

    value = to_decimal(amount)
    if value is None or value <= 0:
        return _redirect(f"/job/{job.job_number}", err="Enter the change order amount.")

    session.add(ChangeOrder(
        job_id=job.id,
        vendor=vendor.strip(),
        number=number.strip(),
        amount=value,
        description=description.strip(),
        approved_by=_actor(approved_by),
    ))
    session.flush()

    # A change order can release invoices being held for exactly this reason.
    session.refresh(job)
    released = 0
    for invoice in job.invoices:
        was_held = invoice.approval_status == APPROVAL_HELD
        apply_routing(invoice)
        if was_held and invoice.approval_status != APPROVAL_HELD:
            released += 1
    session.commit()

    msg = f"Change order recorded for {vendor or 'vendor'}."
    if released:
        msg += f" {released} held invoice{'' if released == 1 else 's'} released for approval."
    return _redirect(f"/job/{job.job_number}", ok=msg)


@app.post("/invoice/{invoice_id}/decide")
def decide_invoice(
    invoice_id: int,
    decision: str = Form(...),
    actor: str = Form(""),
    note: str = Form(""),
    session: Session = Depends(get_session),
):
    """Approve, hold, reject, or mark paid. Every decision is recorded."""
    invoice = session.get(Invoice, invoice_id)
    if invoice is None:
        return _redirect("/", err="No such invoice.")

    routing = route(invoice)
    who = _actor(actor)

    if decision == "approve":
        if not routing.can_approve:
            return _redirect(
                f"/invoice/{invoice.id}",
                err="Cannot approve: " + " ".join(routing.blockers),
            )
        invoice.approval_status = APPROVAL_APPROVED
        invoice.approved_by = who
        invoice.approved_at = utcnow()
        invoice.hold_reason = ""
        if routing.covering_change_order is not None:
            invoice.change_order_id = routing.covering_change_order.id
        receipt = find_receipt(invoice)
        if receipt is not None:
            invoice.receipt_id = receipt.id
        message = f"Invoice {invoice.invoice_number or invoice.id} approved."
    elif decision == "hold":
        invoice.approval_status = APPROVAL_HELD
        invoice.hold_reason = note.strip() or "Held for review."
        message = f"Invoice {invoice.invoice_number or invoice.id} held."
    elif decision == "reject":
        invoice.approval_status = APPROVAL_REJECTED
        invoice.hold_reason = note.strip() or "Rejected."
        message = f"Invoice {invoice.invoice_number or invoice.id} rejected."
    elif decision == "paid":
        if invoice.approval_status != APPROVAL_APPROVED:
            return _redirect(
                f"/invoice/{invoice.id}",
                err="Only an approved invoice can be marked paid.",
            )
        invoice.approval_status = APPROVAL_PAID
        message = f"Invoice {invoice.invoice_number or invoice.id} marked paid."
    elif decision == "reopen":
        invoice.approval_status = APPROVAL_PENDING
        invoice.hold_reason = ""
        invoice.approved_by = ""
        invoice.approved_at = None
        message = f"Invoice {invoice.invoice_number or invoice.id} reopened."
    else:
        return _redirect(f"/invoice/{invoice.id}", err="Unknown decision.")

    session.add(Approval(
        invoice_id=invoice.id,
        decision=decision,
        tier=routing.tier,
        actor=who,
        note=note.strip(),
        required_tier=routing.tier,
        variance_at_decision=invoice.overbilled_amount,
    ))
    session.commit()
    return _redirect(f"/invoice/{invoice.id}", ok=message)


@app.get("/approvals", response_class=HTMLResponse)
def approvals_queue(request: Request, session: Session = Depends(get_session)):
    """Everything waiting on a person, across every job."""
    invoices = session.scalars(
        select(Invoice)
        .where(Invoice.approval_status.in_([APPROVAL_PENDING, APPROVAL_HELD]))
        .order_by(Invoice.created_at.desc())
    ).all()

    rows = []
    for invoice in invoices:
        rows.append({"invoice": invoice, "routing": route(invoice)})

    rows.sort(key=lambda r: (
        r["routing"].action != ACTION_HOLD,          # held first
        not r["routing"].blockers,                   # then blocked
        -(r["invoice"].overbilled_amount or ZERO),   # then biggest overage
    ))

    return templates.TemplateResponse(request, "approvals.html", _ctx(
        request, session,
        rows=rows,
        held=sum(1 for r in rows if r["routing"].action == ACTION_HOLD),
        blocked=sum(1 for r in rows if r["routing"].blockers),
        owner_needed=sum(1 for r in rows if r["routing"].needs_owner),
    ))


@app.get("/vendors", response_class=HTMLResponse)
def vendor_scorecard(request: Request, session: Session = Depends(get_session)):
    """Which suppliers consistently bill above their own quotes.

    A vendor who over-bills once made a mistake. A vendor who over-bills on a
    third of their invoices is a pricing decision for the next bid, not a
    paperwork problem - and that pattern is invisible one invoice at a time.
    """
    invoices = session.scalars(select(Invoice)).all()
    by_vendor: dict[str, dict] = {}

    for invoice in invoices:
        key = norm_vendor(invoice.vendor) or "(unknown)"
        row = by_vendor.setdefault(key, {
            "vendor": invoice.vendor or "(unknown)",
            "invoices": 0, "over_count": 0,
            "billed": ZERO, "overbilled": ZERO, "underbilled": ZERO,
            "unmatched_lines": 0,
        })
        row["invoices"] += 1
        row["billed"] += invoice.total or ZERO
        row["overbilled"] += invoice.overbilled_amount or ZERO
        row["underbilled"] += invoice.underbilled_amount or ZERO
        row["unmatched_lines"] += invoice.lines_unmatched or 0
        if (invoice.overbilled_amount or ZERO) > 0:
            row["over_count"] += 1

    rows = list(by_vendor.values())
    for row in rows:
        row["over_rate"] = (row["over_count"] / row["invoices"] * 100) if row["invoices"] else 0
    rows.sort(key=lambda r: (-r["overbilled"], -r["over_rate"]))

    return templates.TemplateResponse(request, "vendors.html", _ctx(
        request, session,
        rows=rows,
        total_overbilled=sum((r["overbilled"] for r in rows), ZERO),
    ))

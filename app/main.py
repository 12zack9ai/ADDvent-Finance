"""FastAPI application: dashboard, upload, and the marked-up invoice."""
from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus, urlencode

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app import accounting, auth, cashflow, cashflow_pdf, fmt, invoice_pdf, scheduler, trust
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
    CashReport,
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
    Quote,
    Receipt,
    TIER_LABELS,
    TIER_OWNER,
    utcnow,
)
from app.pdf import PdfUnavailable, pdf_available, render_html_to_pdf
from app.services import (
    ingest_scan,
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

def f_ago(value) -> str:
    """How long ago, in the coarsest unit that is still useful.

    On a queue sorted by arrival the question is always "is this new?", and a
    timestamp makes the reader do the subtraction themselves.
    """
    if value is None:
        return "—"
    seconds = (datetime.utcnow() - value).total_seconds()
    if seconds < 90:
        return "just now"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)} min ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)} hour{'' if int(hours) == 1 else 's'} ago"
    days = hours / 24
    if days < 7:
        return f"{int(days)} day{'' if int(days) == 1 else 's'} ago"
    return value.strftime("%d %b")


templates.env.filters.update(
    money=fmt.money, money4=fmt.money4, qty=fmt.qty,
    abs_money=fmt.abs_money, abs_money4=fmt.abs_money4, ago=f_ago,
)

# The company name is on every page, so it is a global rather than something
# each route has to remember to pass. It was being passed by _ctx() only, which
# meant pages not using _ctx rendered an empty <title> and nobody noticed,
# because an undefined value renders as nothing at all.
templates.env.globals["site_name"] = settings.site_name
# Provenance flags, so any list of invoices can show that one of them came
# from somewhere unexpected without every route having to look it up.
templates.env.globals["trust_flags"] = trust.flags_for


def _configure_logging() -> None:
    """Make this application's own log lines visible in production.

    Uvicorn configures its own loggers and leaves the root logger alone, so
    anything this app logs below WARNING goes nowhere on the server. That is
    fine for chatter and not fine for the mailbox: the poller reports the
    outcome of every cycle at INFO, and without this the single line saying
    whether mail is being read is invisible - which is the exact failure the
    poller was written to make impossible.
    """
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s: %(name)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("app").setLevel(logging.INFO)


@app.on_event("startup")
async def _startup() -> None:
    _configure_logging()
    init_db()
    for problem in auth.warnings():
        logging.getLogger("finance").warning("CONFIG: %s", problem)
    # Polls the mailbox from inside this process, so it shares the database and
    # document store rather than needing a second service with its own disk.
    scheduler.start()

    if settings.load_samples:
        # Off the startup path: reading four documents takes about ninety
        # seconds, and blocking here would fail the host's health check.
        asyncio.get_running_loop().run_in_executor(None, _load_samples_once)


def _load_samples_once() -> None:
    from scripts.load_samples import load

    log = logging.getLogger("finance")
    try:
        log.warning("SAMPLES: %s", load())
    except Exception as exc:                          # noqa: BLE001
        log.warning("SAMPLES: failed - %s", exc)


@app.on_event("shutdown")
async def _shutdown() -> None:
    await scheduler.stop()


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
    """Liveness plus the things that fail quietly.

    `mail.stale` is the one to alert on: it means a mailbox is configured but
    nothing has been read for several cycles, which is how a background poller
    fails - silently, with the site still serving pages perfectly.
    """
    return {
        "ok": True,
        "pdf": pdf_available(),
        "server_pdf": pdf_available(),
        "mail": scheduler.status(),
    }


@app.get("/", response_class=HTMLResponse)
def home(request: Request, session: Session = Depends(get_session)):
    """The front door: which of the two programmes do you want.

    Each card carries the one number that says whether it needs attention
    today, so the choice is informed rather than blind.
    """
    jobs = session.scalar(select(func.count(Job.id))) or 0
    invoices = session.scalar(select(func.count(Invoice.id))) or 0

    overbilled = session.scalar(
        select(func.sum(Invoice.overbilled_amount)).where(Invoice.overbilled_amount > 0)
    )
    waiting = session.scalar(
        select(func.count(Invoice.id))
        .where(Invoice.approval_status.in_([APPROVAL_PENDING, APPROVAL_HELD]))
    ) or 0
    inbox = session.scalar(
        select(func.count(Document.id))
        .where(Document.status.in_([ST_NEEDS_JOB, ST_ERROR]))
    ) or 0

    newest = session.scalar(select(Invoice).order_by(Invoice.created_at.desc()))
    latest = session.scalar(select(CashReport).order_by(CashReport.created_at.desc()))
    latest_forecast = _report_forecast(latest) if latest is not None else None

    return templates.TemplateResponse(request, "home.html", _ctx(
        request, session,
        jobs_count=jobs,
        invoices_count=invoices,
        overbilled=Decimal(overbilled) if overbilled else ZERO,
        waiting=waiting,
        inbox_count=inbox,
        newest=newest,
        latest_report=latest,
        latest_forecast=latest_forecast,
    ))


@app.get("/jobs", response_class=HTMLResponse)
def jobs_list(request: Request, session: Session = Depends(get_session)):
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
        return _redirect("/jobs", err=f"No job {job_number}.")

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
            # A scan may hold several invoices. Each becomes its own document.
            docs = ingest_scan(
                session, tmp_path, upload.filename,
                source="upload", note=note,
                job_number_override=job_number,
                force_master=bool(force_master),
            )
            session.commit()

            if len(docs) > 1:
                oks.append(
                    f"{upload.filename} held {len(docs)} documents — split and read "
                    "separately."
                )

            for doc in docs:
                where = doc.filename if len(docs) > 1 else upload.filename
                if doc.status == ST_ERROR:
                    errs.append(f"{where}: {doc.error}")
                elif doc.status == ST_NEEDS_JOB:
                    errs.append(f"{where}: no job number found — waiting in the Inbox.")
                elif doc.status == ST_OTHER:
                    errs.append(f"{where}: not a quote or invoice — filed in the Inbox.")
                else:
                    job = doc.job
                    last_job = job.job_number if job else last_job
                    label = "Master quote" if doc.kind == "quote" else "Invoice"
                    oks.append(f"{label} read from {where} → job {job.job_number}.")
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
        return _redirect("/jobs", err="That file is missing.")
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
        server_pdf=pdf_available(),
        routing=routing,
        status_label=APPROVAL_LABELS.get(invoice.approval_status, invoice.approval_status),
        tier_label=TIER_LABELS.get(routing.tier, "") if routing else "",
        generated_at=datetime.now().strftime("%d %b %Y at %H:%M"),
    )


@app.get("/invoice/{invoice_id}", response_class=HTMLResponse)
def invoice_markup(invoice_id: int, request: Request, session: Session = Depends(get_session)):
    invoice = session.get(Invoice, invoice_id)
    if invoice is None:
        return _redirect("/jobs", err="No such invoice.")
    return HTMLResponse(_render_markup(request, invoice, print_mode=False))


@app.get("/invoice/{invoice_id}/pdf")
def download_invoice_pdf(invoice_id: int, request: Request, session: Session = Depends(get_session)):
    invoice = session.get(Invoice, invoice_id)
    if invoice is None:
        return _redirect("/jobs", err="No such invoice.")

    name = (invoice.invoice_number or str(invoice.id)).replace("/", "-")
    out = settings.renders_dir / f"job{invoice.job.job_number}-invoice-{name}-checked.pdf"

    # Drawn directly rather than printed from HTML. This file gets sent to the
    # vendor, so it cannot depend on Chromium being installed on the host, nor
    # on the reader's browser agreeing to print background colours - without
    # those colours the document looks checked and says nothing.
    invoice_pdf.build(invoice, invoice.job, invoice.quote, invoice.lines, out)

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
        return _redirect("/jobs", err=f"No job {job_number}.")

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
        return _redirect("/jobs", err=f"No job {job_number}.")

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
        return _redirect("/jobs", err="No such invoice.")

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


@app.post("/invoice/{invoice_id}/trust")
def clear_trust_flags(
    invoice_id: int,
    actor: str = Form(""),
    note: str = Form(""),
    session: Session = Depends(get_session),
):
    """Somebody checked, and this bill is genuinely ours.

    The flags are not deleted - they stay on the document with the name of
    whoever signed for them. If a fake invoice ever does get paid, the useful
    question is not "did the system warn us" but "who cleared the warning".
    """
    invoice = session.get(Invoice, invoice_id)
    if invoice is None:
        return _redirect("/jobs", err="No such invoice.")

    who = _actor(actor)
    reason = note.strip()
    if not reason:
        return _redirect(
            f"/invoice/{invoice.id}",
            err="Say how you confirmed it before clearing the warning.",
        )

    cleared = trust.clear(
        invoice.document, f"{who} ({reason})", date.today().strftime("%d %b %Y")
    )
    if not cleared:
        return _redirect(f"/invoice/{invoice.id}", err="Nothing to clear.")

    session.add(Approval(
        invoice_id=invoice.id,
        decision="trust_cleared",
        tier=TIER_OWNER,
        actor=who,
        note=reason,
        required_tier=TIER_OWNER,
        variance_at_decision=invoice.overbilled_amount,
    ))
    session.commit()
    return _redirect(
        f"/invoice/{invoice.id}",
        ok="Recorded. The invoice can now be reviewed on its numbers.",
    )


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


# --- 13-day cash flow forecast -------------------------------------------
#
# One button. What has to go out, what is expected in, and what the bank
# balance does over the next thirteen days.
#
# Two sources today, and the report cannot tell them apart:
#   * bills this system has already checked and approved
#   * A/P and A/R aging exported from QuickBooks Desktop as CSV
# A live QuickBooks connection becomes a third, and changes nothing here.

def _report_forecast(report: CashReport) -> cashflow.Forecast:
    payables = [accounting.payable_from_dict(d) for d in json.loads(report.payables_json)]
    receivables = [accounting.receivable_from_dict(d) for d in json.loads(report.receivables_json)]
    run_rates = {k: Decimal(v) for k, v in json.loads(report.run_rates_json or "{}").items()}
    return cashflow.build_forecast(
        opening_balance=report.opening_balance,
        payables=payables,
        receivables=receivables,
        as_of=date.fromisoformat(report.as_of),
        weeks=report.weeks or cashflow.DEFAULT_WEEKS,
        run_rates=run_rates,
        minimum_cash=report.minimum_cash or ZERO,
        assumptions=accounting.assumptions_from_dict(
            json.loads(report.assumptions_json or "{}")),
        entity=report.entity,
        sources=[s for s in report.source_label.split(" + ") if s],
    )


@app.get("/cashflow", response_class=HTMLResponse)
def cashflow_index(request: Request, session: Session = Depends(get_session)):
    reports = session.scalars(
        select(CashReport).order_by(CashReport.created_at.desc()).limit(30)
    ).all()
    return templates.TemplateResponse(request, "cashflow_index.html", {
        "reports": reports,
        "today": date.today().isoformat(),
        "quickbooks_connected": False,
    })


def _decimal_or_zero(raw: str) -> Decimal:
    text = (raw or "").replace(",", "").replace("$", "").strip()
    if not text:
        return ZERO
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return ZERO


@app.post("/cashflow/generate")
async def cashflow_generate(
    request: Request,
    session: Session = Depends(get_session),
    opening_balance: str = Form("0"),
    minimum_cash: str = Form("0"),
    as_of: str = Form(""),
    entity: str = Form(""),
    created_by: str = Form(""),
    note: str = Form(""),
    include_local: str = Form(""),
    ap_file: Optional[UploadFile] = File(None),
    ar_file: Optional[UploadFile] = File(None),
    rate_payroll: str = Form("0"),
    rate_insurance: str = Form("0"),
    rate_rent: str = Form("0"),
    rate_vehicle: str = Form("0"),
    rate_loan: str = Form("0"),
    rate_overhead: str = Form("0"),
    weeks_current: str = Form("3"),
    weeks_1_30: str = Form("2"),
    weeks_31_60: str = Form("3"),
    weeks_61_90: str = Form("4"),
    pct_current: str = Form("100"),
    pct_1_30: str = Form("100"),
    pct_31_60: str = Form("95"),
    pct_61_90: str = Form("85"),
    pct_90_plus: str = Form("50"),
):
    """The button. Build a 13-week forecast from whatever sources are available."""
    try:
        opening = Decimal((opening_balance or "0").replace(",", "").replace("$", "").strip() or "0")
    except (InvalidOperation, ValueError):
        return _redirect("/cashflow", err="Opening balance must be a number.")

    as_of_date = date.fromisoformat(as_of) if as_of else date.today()

    # Weekly run-rates: what continues whether or not a bill has been entered.
    run_rates = {
        cashflow.CAT_PAYROLL: _decimal_or_zero(rate_payroll),
        cashflow.CAT_INSURANCE: _decimal_or_zero(rate_insurance),
        cashflow.CAT_RENT: _decimal_or_zero(rate_rent),
        cashflow.CAT_VEHICLE: _decimal_or_zero(rate_vehicle),
        cashflow.CAT_LOAN: _decimal_or_zero(rate_loan),
        cashflow.CAT_OVERHEAD: _decimal_or_zero(rate_overhead),
    }
    run_rates = {k: v for k, v in run_rates.items() if v > ZERO}

    def _weeks(raw: str) -> Optional[int]:
        text = (raw or "").strip()
        return int(text) if text.isdigit() else None

    def _pct(raw: str) -> Decimal:
        value = _decimal_or_zero(raw)
        return (value / 100) if value else ZERO

    assumptions = {
        cashflow.BUCKET_CURRENT: cashflow.CollectionAssumption(_weeks(weeks_current), _pct(pct_current)),
        cashflow.BUCKET_1_30: cashflow.CollectionAssumption(_weeks(weeks_1_30), _pct(pct_1_30)),
        cashflow.BUCKET_31_60: cashflow.CollectionAssumption(_weeks(weeks_31_60), _pct(pct_31_60)),
        cashflow.BUCKET_61_90: cashflow.CollectionAssumption(_weeks(weeks_61_90), _pct(pct_61_90)),
        # Deliberately no week: over 90 days there is no date anyone can defend.
        cashflow.BUCKET_90_PLUS: cashflow.CollectionAssumption(None, _pct(pct_90_plus)),
    }

    payables: list = []
    receivables: list = []
    labels: list[str] = []

    if include_local:
        local = accounting.LocalSource(session)
        found = local.payables()
        if found:
            payables.extend(found)
            labels.append(local.name)

    ap_text = (await ap_file.read()).decode("utf-8-sig", "replace") if ap_file and ap_file.filename else ""
    ar_text = (await ar_file.read()).decode("utf-8-sig", "replace") if ar_file and ar_file.filename else ""
    if ap_text.strip() or ar_text.strip():
        try:
            csv_source = accounting.AgingCsvSource(ap_text, ar_text, opening)
        except accounting.AgingParseError as exc:
            return _redirect("/cashflow", err=str(exc))
        payables.extend(csv_source.payables())
        receivables.extend(csv_source.receivables())
        labels.append(csv_source.name)

    # Run-rates alone make a real report: payroll, rent and insurance go out
    # whether or not a bill has been entered anywhere, and a forecast of just
    # those against the opening balance is a fair question to ask. Progress
    # billings are then added to it by hand.
    if not payables and not receivables and not run_rates:
        return _redirect("/cashflow", err=(
            "Nothing to report on. Attach an A/P or A/R aging export, approve "
            "some invoices here, or at least put in the weekly run-rates."
        ))

    report = CashReport(
        as_of=as_of_date.isoformat(),
        weeks=cashflow.DEFAULT_WEEKS,
        entity=entity.strip(),
        opening_balance=opening,
        minimum_cash=_decimal_or_zero(minimum_cash),
        run_rates_json=json.dumps({k: str(v) for k, v in run_rates.items()}),
        assumptions_json=json.dumps(accounting.assumptions_to_dict(assumptions)),
        source_label=" + ".join(labels) or "run-rates and progress billings",
        created_by=_actor(created_by),
        note=note.strip(),
        payables_json=json.dumps([accounting.payable_to_dict(p) for p in payables]),
        receivables_json=json.dumps([accounting.receivable_to_dict(r) for r in receivables]),
    )
    session.add(report)
    session.commit()
    return _redirect(f"/cashflow/{report.id}")


@app.get("/cashflow/{report_id}", response_class=HTMLResponse)
def cashflow_report(report_id: int, request: Request, session: Session = Depends(get_session)):
    report = session.get(CashReport, report_id)
    if report is None:
        return _redirect("/cashflow", err="No such report.")
    return templates.TemplateResponse(request, "cashflow_report.html", {
        "report": report,
        "f": _report_forecast(report),
    })


DRAW_SOURCE = "progress billing"


@app.post("/cashflow/{report_id}/draw")
def add_draw(
    report_id: int,
    customer: str = Form(""),
    job_number: str = Form(""),
    amount: str = Form("0"),
    retainage_pct: str = Form("0"),
    assigned_week: str = Form("1"),
    collect_weeks: str = Form(""),
    memo: str = Form(""),
    session: Session = Depends(get_session),
):
    """Phase a progress billing by hand.

    The one number in this report that no accounting system holds. QuickBooks
    knows what has been invoiced; it has no idea when the next requisition on a
    nine-building roof goes out, or how long that association takes to release
    a check. A person does, and this is where they say so.
    """
    report = session.get(CashReport, report_id)
    if report is None:
        return _redirect("/cashflow", err="No such report.")

    who = customer.strip()
    if not who:
        return _redirect(f"/cashflow/{report_id}#draws", err="Say who is being billed.")

    value = _decimal_or_zero(amount)
    if value <= ZERO:
        return _redirect(f"/cashflow/{report_id}#draws",
                         err="A draw needs an amount greater than zero.")

    week = int(assigned_week) if assigned_week.strip().isdigit() else 1
    week = min(max(week, 1), report.weeks or cashflow.DEFAULT_WEEKS)

    lag_text = (collect_weeks or "").strip()
    lag = int(lag_text) if lag_text.isdigit() else None

    pct = _decimal_or_zero(retainage_pct)
    if not ZERO <= pct < Decimal(100):
        return _redirect(f"/cashflow/{report_id}#draws",
                         err="Retainage has to be between 0 and 100 percent.")

    draw = cashflow.Receivable(
        customer=who,
        amount=value,
        job_number=job_number.strip(),
        memo=memo.strip(),
        reference=uuid.uuid4().hex[:10],
        source=DRAW_SOURCE,
        is_backlog=True,
        assigned_week=week,
        collect_weeks=lag,
        retainage_pct=pct,
    )

    rows = json.loads(report.receivables_json or "[]")
    rows.append(accounting.receivable_to_dict(draw))
    report.receivables_json = json.dumps(rows)
    session.commit()
    return _redirect(f"/cashflow/{report_id}#draws",
                     ok=f"Draw added — billed in week {week}.")


@app.post("/cashflow/{report_id}/draw/{reference}/remove")
def remove_draw(report_id: int, reference: str, session: Session = Depends(get_session)):
    report = session.get(CashReport, report_id)
    if report is None:
        return _redirect("/cashflow", err="No such report.")

    rows = json.loads(report.receivables_json or "[]")
    # Only hand-entered draws can be removed here. Anything that came off an
    # aging export belongs to QuickBooks, and deleting it from the report would
    # make the two disagree with nothing to show why.
    kept = [
        r for r in rows
        if not (r.get("reference") == reference and r.get("source") == DRAW_SOURCE)
    ]
    if len(kept) == len(rows):
        return _redirect(f"/cashflow/{report_id}#draws", err="No such draw on this report.")

    report.receivables_json = json.dumps(kept)
    session.commit()
    return _redirect(f"/cashflow/{report_id}#draws", ok="Draw removed.")


@app.get("/cashflow/{report_id}/pdf")
def cashflow_report_pdf(report_id: int, session: Session = Depends(get_session)):
    report = session.get(CashReport, report_id)
    if report is None:
        return _redirect("/cashflow", err="No such report.")
    out = settings.renders_dir / f"cashflow-{report.as_of}-{report.id}.pdf"
    cashflow_pdf.build(_report_forecast(report), report, out)
    return FileResponse(out, media_type="application/pdf", filename=out.name)


# --- incoming: what has just arrived, newest first ------------------------

INCOMING_LIMIT = 60


@app.get("/incoming", response_class=HTMLResponse)
def incoming(request: Request, session: Session = Depends(get_session)):
    """Invoices in the order they arrived, newest at the top.

    Deliberately not the Approvals queue, which is sorted by urgency - held
    first, then blocked, then biggest variance. That ordering is right when
    working through a backlog and wrong when the question is "what came in
    today?", because a three-week-old dispute outranks the invoice that landed
    an hour ago and the new one is never seen.

    Here, arrival order is the only order. Nothing old can float to the top.
    """
    which = (request.query_params.get("show") or "").strip()

    stmt = select(Invoice).order_by(Invoice.created_at.desc())
    if which == "unreviewed":
        stmt = stmt.where(Invoice.approval_status.in_([APPROVAL_PENDING, APPROVAL_HELD]))
    elif which == "over":
        stmt = stmt.where(Invoice.overbilled_amount > 0)

    invoices = session.scalars(stmt.limit(INCOMING_LIMIT)).all()

    # Documents that arrived but could not be filed are arrivals too, and they
    # are invisible on this page unless they are counted somewhere.
    stuck = session.scalar(
        select(func.count(Document.id))
        .where(Document.status.in_([ST_NEEDS_JOB, ST_ERROR]))
    ) or 0

    unreviewed = session.scalar(
        select(func.count(Invoice.id))
        .where(Invoice.approval_status.in_([APPROVAL_PENDING, APPROVAL_HELD]))
    ) or 0

    return templates.TemplateResponse(request, "incoming.html", _ctx(
        request, session,
        invoices=invoices,
        show=which,
        stuck=stuck,
        unreviewed=unreviewed,
        limit=INCOMING_LIMIT,
    ))

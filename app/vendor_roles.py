"""Subcontractor or supplier: asked once per vendor, remembered after that.

Superior Seamless Gutters' invoice landed with the supplier bills instead of on
the Subs page, because a vendor only counted as a sub on a job once somebody
uploaded their contract through the Subs department. Zack, 2026-09-11, on
having the app ask instead: "Yes if the email doesn't know which it is it
shouldn't hesitate responding asking the question it needed."

  * The first time an invoice or quote arrives from a vendor nobody has
    classified, one question - "sub or supplier?" - is emailed straight back to
    whoever sent it in, or to ALERT_EMAIL when that was the vendor itself (the
    app writes to nobody outside the company). Once per vendor, never per
    document.
  * A one-word reply is read automatically; the invoice page has the same two
    buttons.
  * The answer is remembered and applied at once to every invoice from that
    vendor: a sub's invoices carry `Invoice.is_subcontract`, which puts them on
    the Subs page, in the check queue and in the subcontract line of job
    costing. A sub's quotes become their contract.

Until the answer comes the document is filed as a supplier's. Nothing waits on
the question.
"""
from __future__ import annotations

import logging
import re
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import alerts, mail_send
from app.config import settings
from app.extract import REPLY_SENTINEL, strip_quoted_reply
from app.matching import norm_vendor, vendor_matches
from app.models import Invoice, Job, Quote, VendorRole, utcnow

log = logging.getLogger(__name__)

SUB = "sub"
SUPPLIER = "supplier"

_MSGID = re.compile(r"<[^<>@\s]+@[^<>\s]+>")
_SAYS_SUB = re.compile(r"\bsub(?:s|contractor|contractors)?\b", re.I)
_SAYS_SUPPLIER = re.compile(
    r"\b(?:supplier|suppliers|supply|vendor|material|materials)\b", re.I)


def _same(a: Optional[str], b: Optional[str]) -> bool:
    # vendor_matches is permissive about a blank name; a role must never be.
    return bool(norm_vendor(a)) and bool(norm_vendor(b)) and vendor_matches(a, b)


def find(session: Session, vendor: str) -> Optional[VendorRole]:
    if not norm_vendor(vendor):
        return None
    for row in session.scalars(select(VendorRole)).all():
        if _same(row.vendor, vendor):
            return row
    return None


def role_of(session: Session, vendor: str) -> str:
    """"sub", "supplier", or "" - unknown, or asked and not answered yet."""
    row = find(session, vendor)
    return row.role if row is not None else ""


def holds_contract(job: Job, vendor: str) -> bool:
    return any(_same(q.vendor, vendor) for q in job.subcontracts)


def is_sub_invoice(session: Session, job: Job, vendor: str) -> bool:
    """A contract on this job says so, or somebody has."""
    return holds_contract(job, vendor) or role_of(session, vendor) == SUB


def set_role(session: Session, vendor: str, role: str, *, by: str = "") -> int:
    """Record the answer and apply it everywhere. Returns invoices that moved."""
    from app.services import recompare_job     # services imports this module

    if role not in (SUB, SUPPLIER):
        raise ValueError(f"role must be {SUB!r} or {SUPPLIER!r}")
    row = find(session, vendor)
    if row is None:
        row = VendorRole(vendor=vendor.strip()[:255])
        session.add(row)
    row.role = role
    row.decided_by = (by or "")[:128]
    row.decided_at = utcnow()
    session.flush()

    touched: dict[int, Job] = {}
    if role == SUB:
        # A sub's quote is what they were awarded: their contract.
        for quote in session.scalars(select(Quote).where(Quote.is_master == True)).all():  # noqa: E712
            if not quote.is_subcontract and _same(quote.vendor, vendor):
                quote.is_subcontract = True
                touched[quote.job_id] = quote.job
        session.flush()

    moved = 0
    for invoice in session.scalars(select(Invoice)).all():
        if not _same(invoice.vendor, vendor):
            continue
        # A contract on the job still wins over a "supplier" answer.
        wanted = is_sub_invoice(session, invoice.job, invoice.vendor)
        if bool(invoice.is_subcontract) != wanted:
            invoice.is_subcontract = wanted
            moved += 1
            touched[invoice.job_id] = invoice.job
    session.flush()

    for job in touched.values():
        recompare_job(session, job)
    return moved


# --- told without being asked ------------------------------------------------------

# Written on the email that brought the document in: "FW: sub invoice", or
# "this is a sub" typed above the forwarded message. Stricter than a reply to
# our question: unasked, "materials" or "vendor" describe half of what a sub
# bills for, and "sub total" is printed on most invoices.
_TOLD_SUB = re.compile(r"\bsub(?:s|contractors?)?\b(?![\s-]*totals?\b)", re.I)
_TOLD_SUPPLIER = re.compile(r"\bsuppliers?\b", re.I)


def told_in(document) -> str:
    """"sub" or "supplier" when somebody in the company wrote it on the email."""
    if document.source != "email":
        return ""
    if not settings.may_email(mail_send.reply_address(document.sender)):
        return ""                   # a vendor's own description is not our decision
    said = "\n".join([document.subject or "", strip_quoted_reply(document.body_text or "")])
    sub, supplier = bool(_TOLD_SUB.search(said)), bool(_TOLD_SUPPLIER.search(said))
    if sub == supplier:
        return ""                   # neither, or both: ask instead
    return SUB if sub else SUPPLIER


def decide_from_email(session: Session, document, vendor: str) -> str:
    """Apply what the email said, if it said. Returns the role applied, or ""."""
    role = told_in(document)
    if not role or not norm_vendor(vendor):
        return ""
    set_role(session, vendor, role, by=document.sender or "")
    return role


# --- asking -------------------------------------------------------------------

def _who_to_ask(document) -> str:
    if document.source != "email":
        return ""                   # an uploader is standing right there; the page asks
    to = mail_send.reply_address(document.sender)
    if to and settings.may_email(to):
        return to
    # The vendor sent it themselves. They are not someone this app writes to.
    return alerts.where_to()


def compose_question(*, to: str, vendor: str, filename: str,
                     in_reply_to: str = "", texting: bool = False) -> EmailMessage:
    _, _, _, _, from_address = settings.smtp_settings()
    msg = EmailMessage()
    msg["From"] = formataddr((settings.site_name, from_address))
    msg["To"] = to
    msg["Subject"] = f"Sub or supplier? {vendor}"
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg["Auto-Submitted"] = "auto-generated"
    if texting:
        msg.set_content(
            f"{REPLY_SENTINEL}\n\nIs {vendor} a sub or a supplier? Reply sub or supplier.\n")
        return msg
    msg.set_content(
        f"{REPLY_SENTINEL}\n"
        "\n"
        f"Is {vendor} a subcontractor or a supplier?\n"
        "\n"
        "Reply with one word: sub, or supplier.\n"
        "\n"
        f"We just received {filename} from them and filed it. Your answer decides\n"
        "where their invoices go: a sub's go to Subs and raise a check request, a\n"
        "supplier's stay with the supplier invoices. You will only be asked once\n"
        f"about {vendor}.\n"
        "\n"
        f"-- \n{settings.site_name}\n"
    )
    return msg


def ask(session: Session, document, vendor: str) -> str:
    """Email the question, the first time a vendor turns up. Returns who was asked."""
    vendor = (vendor or "").strip()
    if not norm_vendor(vendor) or find(session, vendor) is not None:
        return ""                   # known already, or the question is out
    to = _who_to_ask(document)
    if not to or not settings.can_send_mail():
        return ""
    msg = compose_question(
        to=to, vendor=vendor, filename=document.filename,
        in_reply_to=document.email_message_id or "",
        texting=mail_send.came_from_a_phone(document.sender),
    )
    try:
        mail_send.send(msg)
    except mail_send.SendError as exc:
        log.warning("could not ask whether %s is a sub: %s", vendor, exc)
        return ""
    session.add(VendorRole(vendor=vendor[:255], role="", asked_at=utcnow(),
                           asked_to=to[:255], ask_message_id=msg["Message-ID"]))
    session.flush()
    return to


def apply_answer(session: Session, references: str, body: str, *, by: str = "") -> list[str]:
    """Read a reply to "sub or supplier?". Returns what was decided."""
    ids = _MSGID.findall(references or "")
    if not ids:
        return []
    rows = session.scalars(
        select(VendorRole).where(VendorRole.ask_message_id.in_(ids))).all()
    if not rows:
        return []
    said = strip_quoted_reply(body or "")
    sub, supplier = bool(_SAYS_SUB.search(said)), bool(_SAYS_SUPPLIER.search(said))
    if sub == supplier:
        return []                   # neither, or both: never guessed at
    role = SUB if sub else SUPPLIER
    decided = []
    for row in rows:
        set_role(session, row.vendor, role, by=by)
        decided.append(
            f"{row.vendor} -> {'subcontractor' if role == SUB else 'supplier'} (from reply)")
    session.commit()
    return decided

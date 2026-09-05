"""Sending mail: asking the sender which job a document belongs to.

Vendors routinely leave the job field blank. When neither the document nor the
covering email says which job it is, the document sits in the Inbox waiting for
a person to notice - and on a busy week nobody does. So the system asks, by
replying to the email it arrived on.

Three things this is careful about, because replying to people is the only
thing the app does that leaves the building:

  * It is off unless ASK_FOR_JOB_NUMBER is set. Nothing starts emailing anyone
    by surprise on a deploy.
  * It asks once per document, recorded on the document itself, so a poller
    that runs every five minutes cannot turn one missing job number into a
    dozen emails to a supplier.
  * It replies in the original thread (In-Reply-To and References), so the
    answer comes back attached to the question and can be matched to the
    document automatically rather than landing as a loose message.
"""
from __future__ import annotations

import logging
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, getaddresses, make_msgid
from typing import Optional

from app.config import settings
from app.extract import REPLY_SENTINEL

log = logging.getLogger(__name__)


class SendError(RuntimeError):
    """The reply could not be sent."""


def reply_address(sender: str) -> str:
    """The bare address to answer, from a From: header like 'Paul <p@x.com>'."""
    addresses = [addr for _, addr in getaddresses([sender or ""]) if addr and "@" in addr]
    return addresses[0] if addresses else ""


def _reply_subject(subject: str) -> str:
    subject = (subject or "").strip() or "your document"
    if re.match(r"^\s*re:", subject, re.I):
        return subject
    return f"Re: {subject}"


def compose_job_query(
    *,
    to_address: str,
    subject: str,
    filename: str,
    vendor: str = "",
    document_number: str = "",
    in_reply_to: str = "",
) -> EmailMessage:
    """The question itself. Short, specific, and answerable in one line."""
    _, _, _, _, from_address = settings.smtp_settings()

    what = filename
    if vendor and document_number:
        what = f"{vendor} {document_number} ({filename})"
    elif vendor:
        what = f"{vendor} ({filename})"

    msg = EmailMessage()
    msg["From"] = formataddr((settings.site_name, from_address))
    msg["To"] = to_address
    msg["Subject"] = _reply_subject(subject)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    # Bounces and vacation replies should not be treated as an answer.
    msg["Auto-Submitted"] = "auto-generated"

    # The sentinel is the FIRST line, not the last. A reply quotes this whole
    # message beneath the sender's own words, so cutting a reply at the sentinel
    # keeps what they typed and discards everything we wrote - including the
    # example job number below, which would otherwise be read as their answer.
    msg.set_content(
        f"{REPLY_SENTINEL}\n"
        "\n"
        f"Thanks - we received {what}.\n"
        "\n"
        "It has not been filed yet because we could not find a job number on it,\n"
        "and there was none in the email.\n"
        "\n"
        "Could you reply to this message with the job number? Just the number is\n"
        "enough, for example:\n"
        "\n"
        "    Job 260000\n"
        "\n"
        "Replying to this email keeps it attached to the document, so it will be\n"
        "filed automatically as soon as your answer arrives. Nothing else is\n"
        "needed and the document is safe in the meantime - it simply is not\n"
        "priced against a quote until it is on a job.\n"
        "\n"
        f"-- \n{settings.site_name}\n"
    )
    return msg


def send(msg: EmailMessage) -> None:
    host, port, user, password, _ = settings.smtp_settings()
    if not settings.can_send_mail():
        raise SendError("SMTP is not configured - set SMTP_HOST/SMTP_USER/SMTP_PASSWORD.")

    context = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
                server.login(user, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(user, password)
                server.send_message(msg)
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        raise SendError(f"Could not send mail via {host}:{port} - {exc}") from exc


def ask_for_job_number(document, *, vendor: str = "", document_number: str = "") -> Optional[str]:
    """Ask the sender which job this document is for. Returns the address asked.

    Returns None when the question was not appropriate or not possible - not an
    error. Most documents arrive with a job number and never come near this.
    """
    if not settings.ask_for_job_number:
        return None
    if document.source != "email":
        return None                       # an uploader is standing right there
    if document.job_id is not None:
        return None
    if document.job_query_sent_at is not None:
        return None                       # asked already; never ask twice
    if not settings.can_send_mail():
        log.warning("ASK: %s has no job number but SMTP is not configured",
                    document.filename)
        return None

    to_address = reply_address(document.sender)
    if not to_address:
        log.warning("ASK: no reply address on %s", document.filename)
        return None

    # Nothing leaves for an outside address. A vendor who sent a document in is
    # not someone this system writes to - not yet, and not by accident. When a
    # vendor's document arrives forwarded by staff, the forwarder is who gets
    # asked, which is the right person anyway: they know the job, the vendor
    # does not.
    if not settings.may_email(to_address):
        log.warning(
            "ASK: %s is outside %s - not emailing, %s stays in the Inbox",
            to_address, ", ".join(sorted(settings.reply_domains())) or "(no domain set)",
            document.filename,
        )
        return None

    msg = compose_job_query(
        to_address=to_address,
        subject=document.subject,
        filename=document.filename,
        vendor=vendor,
        document_number=document_number,
        in_reply_to=document.email_message_id,
    )
    send(msg)
    return to_address


# --- asking the project manager for a missing quote ------------------------

def compose_quote_request(
    *,
    to_address: str,
    job_number: str,
    job_name: str = "",
    person_name: str = "",
    vendor: str = "",
    invoice_number: str = "",
    amount: str = "",
) -> EmailMessage:
    """Ask the PM for the quotes on a job that is already being invoiced.

    Deliberately not a reply to anything. This goes to a colleague about a job,
    not to the vendor about a document, so it starts its own thread with the
    job number in the subject where they can find it later.
    """
    _, _, _, _, from_address = settings.smtp_settings()

    job_label = f"Job {job_number}"
    if job_name:
        job_label += f" — {job_name}"

    billed = f"{vendor}" if vendor else "a supplier"
    if invoice_number:
        billed += f" invoice {invoice_number}"
    if amount:
        billed += f" for {amount}"

    msg = EmailMessage()
    msg["From"] = formataddr((settings.site_name, from_address))
    msg["To"] = to_address
    msg["Subject"] = f"{job_label}: we need the quotes — an invoice has arrived"
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg["Auto-Submitted"] = "auto-generated"

    greeting = f"Hi {person_name.split()[0]}," if person_name else "Hi,"

    msg.set_content(
        f"{greeting}\n"
        "\n"
        f"{billed} has come in on {job_label}, and there is no quote on file for\n"
        "that job yet — so nothing is being checked against anything. The prices\n"
        "on this invoice are going through unchecked until we have the quote.\n"
        "\n"
        f"JobNimbus has you as assigned to {job_number}, so this is a request to\n"
        "you: could you forward the quotes for this job to\n"
        "\n"
        f"    {from_address}\n"
        "\n"
        "All of them — if the job has more than one, for example the material\n"
        "quote and a separate one for skylights, send each and they will all be\n"
        "used. Put the job number in the subject line or the body and they file\n"
        "themselves.\n"
        "\n"
        "Nothing is being held up on your account and nobody is waiting on you to\n"
        "approve anything. The invoice is safe in the queue; it just cannot be\n"
        "priced until the quote is here.\n"
        "\n"
        f"-- \n{settings.site_name}\n"
    )
    return msg


def ask_for_quote(job, invoice, assignment, *, automatic: bool = True) -> Optional[str]:
    """Ask the job's project manager to send in the quotes. Returns who was asked.

    Returns None when the request is not appropriate or not possible, which is
    the ordinary case - most jobs have a quote before an invoice ever arrives.

    The once-per-job rule is NOT enforced here. It belongs to the caller that
    owns `quote_chase_sent_at` and writes it before calling - see
    services.chase_quote. Checking it in both places meant the caller marked
    the job as asked and this function then refused to send, which is exactly
    the sort of quiet nothing that a guard duplicated across two files
    produces.
    """
    # ASK_FOR_QUOTE governs the AUTOMATIC ask only. A person clicking a button
    # is not the thing that flag exists to hold back - it exists so a deploy
    # never starts emailing people by surprise. Someone deliberately asking for
    # the quote on a job in front of them is not a surprise.
    if automatic and not settings.ask_for_quote:
        return None
    if assignment is None or not assignment.usable:
        return None
    if not settings.can_send_mail():
        log.warning("QUOTE: job %s has no quote but SMTP is not configured",
                    job.job_number)
        return None

    to_address = assignment.email
    if not settings.may_email(to_address):
        # A project manager is staff, so this should not happen - and if it
        # does, something is wrong with the JobNimbus record rather than with
        # the address, so it is worth being loud about.
        log.warning(
            "QUOTE: JobNimbus gave %s for job %s, which is outside %s - not emailing",
            to_address, job.job_number,
            ", ".join(sorted(settings.reply_domains())) or "(no domain set)",
        )
        return None

    amount = f"${invoice.total:,.2f}" if invoice.total is not None else ""
    msg = compose_quote_request(
        to_address=to_address,
        job_number=job.job_number,
        job_name=assignment.job_name,
        person_name=assignment.person_name,
        vendor=invoice.vendor,
        invoice_number=invoice.invoice_number,
        amount=amount,
    )
    send(msg)
    log.info("QUOTE: asked %s for the quotes on job %s", to_address, job.job_number)
    return to_address

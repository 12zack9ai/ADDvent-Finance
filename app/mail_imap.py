"""Read the finance mailbox over IMAP.

This is the simple path, and for a mailbox on an ordinary mail host it is the
right one: create the address, put the credentials in the environment, done. No
app registration, no admin consent, no waiting on anyone.

Ours, for the record:

    IMAP_HOST=mail.protectedharborinc.com
    IMAP_PORT=993
    IMAP_USER=aifinance              # aifinance@addventuresinc.com
    IMAP_PASSWORD=...                # set on Render, never in a committed file
    MAIL_ENABLED=true

The thing that trips this up is the username. Some hosts want the whole email
address and some want only the part before the @; ours took the bare name. If
the login is refused with the full address, try the short one before assuming
the password is wrong.

Uses only the standard library - `imaplib` and `email` - so there is nothing
extra to install and nothing to keep patched.

Attachments are matched to a job by the same rules as an upload: the SUBJECT
LINE is read first, because that is where whoever forwards the invoice writes
the job number.
"""
from __future__ import annotations

import email
import hashlib
import imaplib
import logging
import re
import tempfile
from datetime import date, datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app import alerts, mail_send, paylink
from app.mail_types import ALLOWED_SUFFIXES, MAX_ATTACHMENT_BYTES, MailboxError, PollResult
from app.models import Document, MailSeen, utcnow
from app.services import (
    ST_NEEDS_JOB, DuplicateDocument, IngestError, file_stored_document,
    ingest_file, ingest_scan, parse_job_answer,
)

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


def _decode(value: Optional[str]) -> str:
    """Decode RFC 2047 headers ('=?utf-8?q?...?=') into readable text."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 - a malformed header must not stop the poll
        return value


def _body_text(message: Message) -> str:
    """Best-effort plain text of the message body."""
    html_fallback = ""
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_filename():
            continue
        content_type = part.get_content_type()
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
        if content_type == "text/plain":
            return text.strip()
        if content_type == "text/html" and not html_fallback:
            html_fallback = text
    if html_fallback:
        stripped = re.sub(r"(?is)<(script|style).*?</\1>", " ", html_fallback)
        return re.sub(r"\s+", " ", _TAG_RE.sub(" ", stripped)).strip()
    return ""


def _raw_bodies(message: Message) -> tuple[str, str]:
    """The HTML and plain-text bodies as sent, links and all: (html, text)."""
    html_parts: list[str] = []
    text_parts: list[str] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        content_type = part.get_content_type()
        if content_type not in ("text/html", "text/plain"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            decoded = payload.decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
        (html_parts if content_type == "text/html" else text_parts).append(decoded)
    return "\n".join(html_parts), "\n".join(text_parts)


def _attachments(message: Message) -> Iterator[tuple[str, bytes]]:
    """Every attachment worth reading, as (filename, bytes)."""
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = _decode(part.get_filename())
        if not filename:
            continue
        if Path(filename).suffix.lower() not in ALLOWED_SUFFIXES:
            continue
        # A logo in someone's signature is a real image part with a real
        # filename, so the suffix test alone lets every one of them through -
        # and each would be sent to Claude, paid for, and filed as "not a quote
        # or invoice". Embedded images carry a Content-ID so the HTML body can
        # reference them, or are marked inline.
        #
        # But a PDF is never a logo. Apple Mail - every iPhone - attaches a
        # forwarded PDF "inline" so it shows inside the message, and treating
        # that as a logo threw away Zack's quote on 2026-09-11. So an embedded
        # part is passed over only when it is an image small enough to be one.
        disposition = (part.get_content_disposition() or "").lower()
        embedded = bool(part.get("Content-ID")) or disposition == "inline"
        is_pdf = Path(filename).suffix.lower() == ".pdf"
        try:
            content = part.get_payload(decode=True) or b""
        except Exception:  # noqa: BLE001
            continue
        if embedded and not is_pdf and len(content) < _LOGO_BYTES:
            continue
        if content and len(content) <= MAX_ATTACHMENT_BYTES:
            yield filename, content


# A signature logo is a few KB; a photo of a receipt or a quote is hundreds.
_LOGO_BYTES = 60 * 1024


class ImapMailbox:
    def __init__(self) -> None:
        self.conn: Optional[imaplib.IMAP4_SSL] = None

    def __enter__(self) -> "ImapMailbox":
        try:
            if settings.imap_ssl:
                self.conn = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
            else:
                self.conn = imaplib.IMAP4(settings.imap_host, settings.imap_port)
                self.conn.starttls()
            self.conn.login(settings.imap_user, settings.imap_password)
        except imaplib.IMAP4.error as exc:
            raise MailboxError(
                f"The mail server rejected the login for {settings.imap_user}: {exc}. "
                "Check IMAP_USER and IMAP_PASSWORD, and that IMAP is enabled for "
                "that mailbox."
            ) from exc
        except OSError as exc:
            raise MailboxError(
                f"Could not reach {settings.imap_host}:{settings.imap_port} — {exc}"
            ) from exc
        return self

    def __exit__(self, *exc) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                self.conn.logout()
            except Exception:  # noqa: BLE001
                pass

    def select_inbox(self) -> None:
        status, _ = self.conn.select(settings.imap_folder)
        if status != "OK":
            raise MailboxError(f"Mailbox folder '{settings.imap_folder}' not found.")

    def unread_ids(self, limit: int) -> list[bytes]:
        status, data = self.conn.search(None, "UNSEEN")
        if status != "OK":
            raise MailboxError("Could not list unread messages.")
        return data[0].split()[:limit]

    def fetch(self, msg_id: bytes) -> Optional[Message]:
        # BODY.PEEK leaves the message unread, so a mid-poll failure doesn't
        # quietly consume an invoice we never actually filed.
        status, data = self.conn.fetch(msg_id, "(BODY.PEEK[])")
        if status != "OK" or not data or not isinstance(data[0], tuple):
            return None
        return email.message_from_bytes(data[0][1])

    def select_folder(self, name: str) -> bool:
        status, _ = self.conn.select(f'"{name}"' if " " in name else name)
        return status == "OK"

    def ids_since(self, days: int, seen_only: bool = False) -> list[bytes]:
        """Messages in the selected folder that arrived in the last `days` days."""
        day = date.today() - timedelta(days=days)
        since = f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"
        criteria = ("SEEN", "SINCE", since) if seen_only else ("SINCE", since)
        status, data = self.conn.search(None, *criteria)
        if status != "OK":
            raise MailboxError("Could not list recent messages.")
        return data[0].split()

    def message_key(self, msg_id: bytes) -> str:
        """Just enough of the headers to know the message, without its attachments."""
        status, data = self.conn.fetch(
            msg_id, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM DATE SUBJECT)])")
        if status != "OK" or not data or not isinstance(data[0], tuple):
            return ""
        return _message_key(email.message_from_bytes(data[0][1]))

    def ensure_folder(self, name: str) -> bool:
        try:
            self.conn.create(name)   # already exists -> NO, which is fine
        except Exception:  # noqa: BLE001
            pass
        status, _ = self.conn.list()
        return status == "OK"

    def file_away(self, msg_id: bytes, folder: str) -> None:
        """Mark read and move to the processed folder.

        Tries UID MOVE first; falls back to copy-then-delete for older servers
        that don't implement it.
        """
        self.conn.store(msg_id, "+FLAGS", "\\Seen")
        if not folder:
            return
        self.ensure_folder(folder)
        try:
            status, _ = self.conn.uid("MOVE", msg_id, folder)
            if status == "OK":
                return
        except Exception:  # noqa: BLE001
            pass
        try:
            status, _ = self.conn.copy(msg_id, folder)
            if status == "OK":
                self.conn.store(msg_id, "+FLAGS", "\\Deleted")
                self.conn.expunge()
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not move message to %s: %s", folder, exc)



# --- asking for a missing job number, and recognising the answer ----------

_MSGID = re.compile(r"<[^<>@\s]+@[^<>\s]+>")



# Mail that is not from a person, and must not be treated as any part of a
# conversation: bounces, vacation responders, mailing lists, marketing blasts.
# Without this, an out-of-office reply carrying the original attachment gets
# processed as a fresh document, and a bounce could be read as an answer.
def is_automatic(message: Message) -> bool:
    auto_submitted = (message.get("Auto-Submitted") or "").strip().lower()
    if auto_submitted and auto_submitted != "no":
        return True
    precedence = (message.get("Precedence") or "").strip().lower()
    if precedence in {"bulk", "junk", "list", "auto_reply"}:
        return True
    for header in ("List-Id", "List-Unsubscribe", "X-Autoreply",
                   "X-Autorespond", "X-Auto-Response-Suppress"):
        if message.get(header):
            return True
    # An empty return path is the null sender: a bounce, by definition.
    if (message.get("Return-Path") or "").strip() in {"<>", ""} and message.get("Return-Path"):
        return True
    return False


def _ask_about(session: Session, document: Document) -> str:
    """Email the sender asking which job this is. Returns who was asked, or "".

    Only for a quote or an invoice that is genuinely waiting on a job number.
    Anything else read out of the mailbox - a statement, a packing slip, a
    signed contract, somebody's screenshot - has no job number to ask for, and
    emailing a stranger to ask which job their PDF belongs to is worse than
    doing nothing at all.
    """
    if document.job_id is not None:
        return ""
    if document.kind not in ("quote", "invoice"):
        return ""
    if document.status != ST_NEEDS_JOB:
        return ""
    try:
        asked = mail_send.ask_for_job_number(document)
    except mail_send.SendError as exc:
        # Not being able to ask must never lose the document. It stays in the
        # Inbox, which is exactly where it would have sat anyway.
        log.warning("could not ask about %s: %s", document.filename, exc)
        return ""
    if not asked:
        return ""
    document.job_query_sent_at = utcnow()
    document.job_query_to = asked
    session.commit()
    return asked


def _apply_job_answer(session: Session, references: str, subject: str, body: str) -> list[str]:
    """File any documents this message answers the job number for.

    Matched on the Message-ID we asked from, carried back in In-Reply-To, so a
    reply is tied to the exact document. Subject lines get edited, forwarded and
    re-used; a message ID does not.
    """
    ids = _MSGID.findall(references or "")
    if not ids:
        return []

    waiting = session.scalars(
        select(Document)
        .where(Document.email_message_id.in_(ids))
        .where(Document.job_id.is_(None))
    ).all()
    if not waiting:
        return []

    directive = parse_job_answer(subject, body)
    if not directive.job_number:
        return []

    filed = []
    for document in waiting:
        try:
            file_stored_document(
                session, document, directive.job_number,
                force_master=directive.is_master_update,
            )
            session.commit()
            filed.append(f"{document.filename} -> job {directive.job_number} (from reply)")
        except Exception as exc:                       # noqa: BLE001
            session.rollback()
            log.warning("reply named job %s but filing %s failed: %s",
                        directive.job_number, document.filename, exc)
    return filed

def _file_pay_link(session: Session, message: Message, result: PollResult, *,
                   sender: str, subject: str, body: str, message_id: str) -> tuple[bool, bool]:
    """File the invoice behind a QuickBooks pay link. Returns (found, retry later).

    Only reached when nothing was attached. The first link that turns out to
    be an invoice is the one filed; an Intuit page that is not an invoice is
    passed over quietly.
    """
    html, text = _raw_bodies(message)
    retry = False
    for url in paylink.find_links(html, text):
        try:
            payload = paylink.read(url)
        except paylink.PaylinkError as exc:
            if exc.retry:
                retry = True
                result.errors.append(f"{subject or '(no subject)'}: QuickBooks pay link - {exc}")
            continue
        if payload is None:
            continue
        label = f"{payload['vendor']} invoice {payload['document_number']}"
        try:
            doc = paylink.ingest(session, payload, sender=sender, subject=subject,
                                 body=body, message_id=message_id)
            session.commit()
        except DuplicateDocument:
            session.rollback()
            result.skipped.append(f"{label} (already received)")
            return True, False
        except IngestError as exc:
            # Refused for a reason that another try will not change.
            session.rollback()
            result.errors.append(f"{label}: {exc}")
            return True, False
        except Exception as exc:  # noqa: BLE001 - leave the mail for the next poll
            session.rollback()
            result.errors.append(f"{label}: {exc}")
            return True, True
        where = f"job {doc.job.job_number}" if doc.job else "the Inbox"
        result.filed.append(f"{doc.filename} -> {where} ({doc.status}), from the pay link")
        asked = _ask_about(session, doc)
        if asked:
            result.skipped.append(f"{doc.filename} - asked {asked} for the job number")
        return True, False
    return False, retry


def _process(session: Session, message: Message, result: PollResult) -> bool:
    """Everything done with one message short of moving it.

    Returns False when the message should stay where it is and be tried again.
    """
    subject = _decode(message.get("Subject"))
    sender = _decode(message.get("From"))
    body = _body_text(message)
    message_id = (message.get("Message-ID") or "").strip()
    references = " ".join(filter(None, [
        message.get("In-Reply-To") or "", message.get("References") or "",
    ]))

    if is_automatic(message):
        result.skipped.append(f"{subject or '(no subject)'} (automatic mail)")
        return True

    handled_all = True
    found_any = False

    # Is this the answer to a job number we asked for? A reply carries
    # the original Message-ID in In-Reply-To, so the answer can be tied
    # back to the exact document rather than guessed at by subject line.
    answered = _apply_job_answer(session, references, subject, body)
    for filed in answered:
        result.filed.append(filed)

    for filename, content in _attachments(message):
        found_any = True
        with tempfile.NamedTemporaryFile(
            suffix=Path(filename).suffix, delete=False
        ) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        try:
            # A scanned attachment may hold several invoices.
            docs = ingest_scan(
                session, tmp_path, filename,
                source="email", sender=sender, subject=subject, body=body,
                message_id=message_id,
            )
            session.commit()
            if len(docs) > 1:
                result.filed.append(
                    f"{filename} -> {len(docs)} documents found and split")
            for doc in docs:
                where = f"job {doc.job.job_number}" if doc.job else "the Inbox"
                result.filed.append(f"{doc.filename} -> {where} ({doc.status})")

                # Nothing said which job this is. Ask, once.
                asked = _ask_about(session, doc)
                if asked:
                    result.skipped.append(
                        f"{doc.filename} - asked {asked} for the job number")
        except DuplicateDocument:
            session.rollback()
            result.skipped.append(f"{filename} (already received)")
        except (IngestError, Exception) as exc:  # noqa: BLE001
            session.rollback()
            result.errors.append(f"{filename}: {exc}")
            handled_all = False
        finally:
            tmp_path.unlink(missing_ok=True)

    if not found_any:
        # Nothing attached - but a QuickBooks Online vendor sends a
        # "View and pay" link instead, and the invoice is on that page.
        found_any, retry = _file_pay_link(
            session, message, result, sender=sender, subject=subject,
            body=body, message_id=message_id,
        )
        if retry:
            handled_all = False

    if not found_any:
        result.skipped.append(
            f"{subject or '(no subject)'} (no attachment and no invoice link)")

    return handled_all


# --- the background check ----------------------------------------------------
#
# Zack, 2026-09-11: "i sent an email at 7:04 new quote came in. it was not
# uploaded. we need to implement some sort of background check on these so no
# invoice or quote gets missed. its now 4 hours later and still not uploaded."
#
# It was read at 7:05 and skipped - an iPhone attaches a PDF "inline", and the
# reader took it for a signature logo - and nothing said so. Now:
#
#   * every message looked at is recorded (MailSeen), and /mail lists them;
#   * anything not filed is emailed to ALERT_EMAIL at once, with the reason;
#   * the poll only sees UNREAD mail in the Inbox, so a message somebody opened
#     first, or one skipped by a reader since improved, was gone for good.
#     sweep_once looks back SWEEP_DAYS over the Inbox and the Processed folder
#     and reads again anything that was never filed.

# Bump when the reader learns to read something it used to skip, so the sweep
# reads those skips again. 2: inline PDFs, and QuickBooks pay links.
READER_VERSION = 2
SWEEP_DAYS = 7
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_SETTLED = ("filed", "duplicate", "automatic")


def _message_key(message: Message) -> str:
    """The Message-ID, or for the rare message without one, a stand-in built
    from the headers that identify it."""
    key = (message.get("Message-ID") or "").strip()
    if key:
        return key[:512]
    basis = "|".join(
        (message.get("From") or "", message.get("Date") or "", message.get("Subject") or ""))
    return "noid:" + hashlib.sha256(basis.encode("utf-8", "replace")).hexdigest()


def _received(message: Message) -> Optional[datetime]:
    try:
        return parsedate_to_datetime(message.get("Date") or "")
    except (TypeError, ValueError, IndexError):
        return None


def _outcome(result: PollResult, marks: tuple[int, int, int]) -> tuple[str, str]:
    """What became of one message, from what the poll recorded while reading it."""
    filed = result.filed[marks[0]:]
    skipped = result.skipped[marks[1]:]
    errors = result.errors[marks[2]:]
    if errors:
        return "error", "; ".join(errors)
    if filed:
        return "filed", "; ".join(filed)
    if skipped and all(s.endswith("(automatic mail)") for s in skipped):
        return "automatic", "; ".join(skipped)
    if skipped and all(s.endswith("(already received)") for s in skipped):
        return "duplicate", "; ".join(skipped)
    return "skipped", "; ".join(skipped) or "nothing in it could be read"


def _record(session: Session, message: Message, folder: str,
            outcome: str, reason: str) -> MailSeen:
    key = _message_key(message)
    row = session.scalar(select(MailSeen).where(MailSeen.message_key == key))
    if row is None:
        row = MailSeen(message_key=key)
        session.add(row)
    row.folder = folder
    row.sender = _decode(message.get("From"))[:255]
    row.subject = _decode(message.get("Subject"))[:500]
    row.received_at = _received(message)
    row.outcome = outcome
    row.reason = reason[:4000]
    row.reader_version = READER_VERSION
    row.last_seen = utcnow()
    session.commit()
    return row


def _alert_if_missed(session: Session, row: MailSeen) -> None:
    """Email ALERT_EMAIL about a message that was not filed - once per message."""
    if row.outcome not in ("skipped", "error") or row.alerted_at is not None:
        return
    to = alerts.where_to()
    if not to or not settings.can_send_mail():
        return
    # Never to the mailbox being read: the alert would arrive, not be filed,
    # and alert again.
    _, _, _, _, own = settings.smtp_settings()
    if own and to.strip().lower() == own.strip().lower():
        return
    when = row.received_at.strftime("%a %d %b %Y %H:%M") if row.received_at else "unknown"
    body = "\n".join([
        "An email to the finance mailbox was not filed.",
        "",
        f"From: {row.sender or 'unknown'}",
        f"Subject: {row.subject or '(no subject)'}",
        f"Received: {when}",
        f"Why: {row.reason}",
        "",
        "What to do: forward it again with the quote or invoice attached as a PDF,",
        f"or upload it at {settings.base_url}/upload.",
        "",
        f"Every email the app has seen is listed at {settings.base_url}/mail.",
    ])
    msg = alerts._compose(
        to, f"[{settings.site_name}] Not filed: {row.subject or '(no subject)'}", body)
    msg["Auto-Submitted"] = "auto-generated"   # read back by is_automatic, never re-alerted
    try:
        mail_send.send(msg)
    except mail_send.SendError as exc:
        log.warning("could not send the not-filed alert for %s: %s", row.message_key, exc)
        return
    row.alerted_at = utcnow()
    session.commit()


def _handle(session: Session, message: Message, result: PollResult, *, folder: str) -> bool:
    """Read one message, record what became of it, and speak up if it was missed."""
    marks = (len(result.filed), len(result.skipped), len(result.errors))
    handled = _process(session, message, result)
    outcome, reason = _outcome(result, marks)
    row = _record(session, message, folder, outcome, reason)
    _alert_if_missed(session, row)
    return handled


def poll_once(session: Session, limit: int = 25) -> PollResult:
    """Read new mail, ingest every usable attachment, then file the message away.

    A message is only marked read and moved once every attachment on it has been
    handled. A transient failure therefore leaves the mail unread in the Inbox to
    be retried next time, rather than an invoice vanishing silently.
    """
    result = PollResult()

    with ImapMailbox() as mailbox:
        mailbox.select_inbox()
        message_ids = mailbox.unread_ids(limit)
        result.messages_seen = len(message_ids)

        # Highest number first: filing a message away can renumber the ones
        # after it, and working backwards means that never matters.
        for msg_id in reversed(message_ids):
            message = mailbox.fetch(msg_id)
            if message is None:
                result.errors.append(f"Message {msg_id!r} could not be fetched.")
                continue
            if _handle(session, message, result, folder="INBOX"):
                mailbox.file_away(msg_id, settings.mail_processed_folder)

    return result


def _needs_another_look(session: Session, key: str) -> bool:
    row = session.scalar(select(MailSeen).where(MailSeen.message_key == key))
    if row is not None:
        if row.outcome in _SETTLED:
            return False
        return row.reader_version < READER_VERSION or row.outcome == "error"
    # Never recorded: filed before this log existed, or missed.
    if session.scalar(select(Document.id).where(Document.email_message_id == key).limit(1)):
        session.add(MailSeen(message_key=key, outcome="filed",
                             reason="filed before the mail log existed",
                             reader_version=READER_VERSION))
        session.commit()
        return False
    return True


def _sweep(session: Session, mailbox, result: PollResult, ids: list[bytes], *,
           folder: str, move: bool) -> None:
    for msg_id in reversed(ids):
        key = mailbox.message_key(msg_id)
        if not key or not _needs_another_look(session, key):
            continue
        message = mailbox.fetch(msg_id)
        if message is None:
            continue
        result.messages_seen += 1
        if _handle(session, message, result, folder=folder) and move:
            mailbox.file_away(msg_id, settings.mail_processed_folder)


def sweep_once(session: Session, days: int = SWEEP_DAYS) -> PollResult:
    """The background check: nothing that reached the mailbox goes unread.

    Looks back `days` over the Inbox - mail a person opened before the poll got
    to it is still there, but no longer unread - and over the Processed folder,
    where every skipped message was filed away. Anything never filed, or
    skipped by an older reader, is read again. Filed mail is not touched.
    """
    result = PollResult()
    with ImapMailbox() as mailbox:
        mailbox.select_inbox()
        _sweep(session, mailbox, result, mailbox.ids_since(days, seen_only=True),
               folder="INBOX", move=True)
        processed = settings.mail_processed_folder
        if processed and mailbox.select_folder(processed):
            _sweep(session, mailbox, result, mailbox.ids_since(days),
                   folder=processed, move=False)
    return result

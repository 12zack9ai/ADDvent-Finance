"""Read the finance mailbox over IMAP.

This is the simple path, and for a mailbox hosted alongside the website (cPanel,
Plesk, or any ordinary mail host) it is the right one: create the address in the
control panel, put the credentials in `.env`, done. No app registration, no
admin consent, no waiting on anyone.

    IMAP_HOST=addventuresinc.com     # cPanel: usually the bare domain
    IMAP_PORT=993
    IMAP_USER=aiap@addventuresinc.com  # the FULL address, not just "aiap"
    IMAP_PASSWORD=...
    MAIL_ENABLED=true

The two things that trip this up: the incoming server on cPanel is normally the
bare domain rather than mail.<domain>, and the username must be the whole email
address. Both are shown under Email Accounts -> Connect Devices.

Uses only the standard library - `imaplib` and `email` - so there is nothing
extra to install and nothing to keep patched.

Attachments are matched to a job by the same rules as an upload: the SUBJECT
LINE is read first, because that is where whoever forwards the invoice writes
the job number.
"""
from __future__ import annotations

import email
import imaplib
import logging
import re
import tempfile
from email.header import decode_header, make_header
from email.message import Message
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app import mail_send
from app.mail_types import ALLOWED_SUFFIXES, MAX_ATTACHMENT_BYTES, MailboxError, PollResult
from app.models import Document, utcnow
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
        # reference them, or are marked inline. Neither is an attachment.
        if part.get("Content-ID"):
            continue
        disposition = (part.get_content_disposition() or "").lower()
        if disposition == "inline":
            continue
        try:
            content = part.get_payload(decode=True) or b""
        except Exception:  # noqa: BLE001
            continue
        if content and len(content) <= MAX_ATTACHMENT_BYTES:
            yield filename, content


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

        for msg_id in message_ids:
            message = mailbox.fetch(msg_id)
            if message is None:
                result.errors.append(f"Message {msg_id!r} could not be fetched.")
                continue

            subject = _decode(message.get("Subject"))
            sender = _decode(message.get("From"))
            body = _body_text(message)
            message_id = (message.get("Message-ID") or "").strip()
            references = " ".join(filter(None, [
                message.get("In-Reply-To") or "", message.get("References") or "",
            ]))

            if is_automatic(message):
                result.skipped.append(f"{subject or '(no subject)'} (automatic mail)")
                mailbox.file_away(msg_id, settings.mail_processed_folder)
                continue

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
                result.skipped.append(f"{subject or '(no subject)'} (no usable attachments)")

            if handled_all:
                mailbox.file_away(msg_id, settings.mail_processed_folder)

    return result

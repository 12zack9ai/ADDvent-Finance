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

from sqlalchemy.orm import Session

from app.config import settings
from app.mail_types import ALLOWED_SUFFIXES, MAX_ATTACHMENT_BYTES, MailboxError, PollResult
from app.services import DuplicateDocument, IngestError, ingest_file

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

            handled_all = True
            found_any = False

            for filename, content in _attachments(message):
                found_any = True
                with tempfile.NamedTemporaryFile(
                    suffix=Path(filename).suffix, delete=False
                ) as tmp:
                    tmp.write(content)
                    tmp_path = Path(tmp.name)
                try:
                    doc = ingest_file(
                        session, tmp_path, filename,
                        source="email", sender=sender, subject=subject, body=body,
                    )
                    session.commit()
                    where = f"job {doc.job.job_number}" if doc.job else "the Inbox"
                    result.filed.append(f"{filename} -> {where} ({doc.status})")
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

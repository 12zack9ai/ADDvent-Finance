"""Read the finance mailbox over Microsoft Graph (Exchange / Microsoft 365).

This is the ALTERNATIVE backend. For a mailbox on your own mail host - the kind
created in cPanel or Plesk - use IMAP instead (`mail_imap.py`): it needs no app
registration and nobody's approval. Graph is the better choice only when the
mailbox lives in Exchange / Microsoft 365, where app-only auth gives tighter
isolation than a password.

Uses app-only (client credentials) auth, so there is no shared password and no
mailbox left signed in on someone's desktop. IT registers one app and grants it
access to exactly one mailbox.

What IT needs to do, once:

  1. Entra ID -> App registrations -> New registration (single tenant).
  2. Certificates & secrets -> New client secret. Copy the VALUE immediately.
  3. API permissions -> Microsoft Graph -> APPLICATION permissions:
         Mail.ReadWrite      (read messages, move them to Processed)
     Then "Grant admin consent".
  4. Recommended - restrict the app to the one mailbox with an
     ApplicationAccessPolicy, so it cannot read anyone else's mail:

         New-ApplicationAccessPolicy -AppId <client-id> `
             -PolicyScopeGroupId ap-inbox@addventuresinc.com `
             -AccessRight RestrictAccess `
             -Description "Finance invoice reader"

  5. Give us: Tenant ID, Client ID, Client secret, and the mailbox address.

Attachments are matched to a job using the same rules as an upload note: the
SUBJECT LINE is read first, which is where whoever forwards the invoice writes
the job number.
"""
from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

import httpx
import msal
from sqlalchemy.orm import Session

from app.config import settings
from app.services import DuplicateDocument, IngestError, ingest_file

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPE = ["https://graph.microsoft.com/.default"]

from app.mail_types import (  # noqa: F401  (re-exported for callers)
    ALLOWED_SUFFIXES,
    MAX_ATTACHMENT_BYTES,
    MailboxError,
    PollResult,
)

_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html or "")
    text = _TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def get_token() -> str:
    if not settings.graph_configured():
        raise MailboxError(
            "Microsoft Graph is not configured. Set MS_TENANT_ID, MS_CLIENT_ID, "
            "MS_CLIENT_SECRET and MS_MAILBOX in .env."
        )
    app = msal.ConfidentialClientApplication(
        client_id=settings.ms_client_id,
        client_credential=settings.ms_client_secret,
        authority=f"https://login.microsoftonline.com/{settings.ms_tenant_id}",
    )
    result = app.acquire_token_for_client(scopes=SCOPE)
    if "access_token" not in result:
        raise MailboxError(
            f"Microsoft rejected the credentials: "
            f"{result.get('error_description') or result.get('error')}"
        )
    return result["access_token"]


class GraphClient:
    def __init__(self, token: str, mailbox: str):
        self.mailbox = mailbox
        self.http = httpx.Client(
            base_url=GRAPH,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60.0,
        )

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "GraphClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _user(self, path: str) -> str:
        return f"/users/{self.mailbox}{path}"

    def unread_with_attachments(self, limit: int = 25) -> list[dict[str, Any]]:
        """Messages in the Inbox that carry attachments, oldest first."""
        resp = self.http.get(
            self._user("/mailFolders/Inbox/messages"),
            params={
                "$filter": "hasAttachments eq true",
                "$select": "id,subject,from,receivedDateTime,bodyPreview,body,internetMessageId",
                "$orderby": "receivedDateTime asc",
                "$top": str(limit),
            },
        )
        if resp.status_code >= 400:
            raise MailboxError(f"Graph error listing messages ({resp.status_code}): {resp.text[:400]}")
        return resp.json().get("value", [])

    def attachments(self, message_id: str) -> list[dict[str, Any]]:
        resp = self.http.get(self._user(f"/messages/{message_id}/attachments"))
        if resp.status_code >= 400:
            raise MailboxError(f"Graph error reading attachments ({resp.status_code}): {resp.text[:400]}")
        return resp.json().get("value", [])

    def ensure_folder(self, name: str) -> Optional[str]:
        """Find (or create) a sibling folder of the Inbox by display name."""
        resp = self.http.get(
            self._user("/mailFolders"), params={"$top": "100", "$select": "id,displayName"}
        )
        if resp.status_code >= 400:
            return None
        for folder in resp.json().get("value", []):
            if folder.get("displayName", "").lower() == name.lower():
                return folder["id"]

        created = self.http.post(self._user("/mailFolders"), json={"displayName": name})
        if created.status_code < 400:
            return created.json().get("id")
        return None

    def move(self, message_id: str, folder_id: str) -> bool:
        resp = self.http.post(
            self._user(f"/messages/{message_id}/move"), json={"destinationId": folder_id}
        )
        return resp.status_code < 400

    def mark_read(self, message_id: str) -> None:
        self.http.patch(self._user(f"/messages/{message_id}"), json={"isRead": True})


def poll_graph(session: Session, limit: int = 25) -> PollResult:
    """Read new mail via Microsoft Graph, ingest every usable attachment, then file the message away.

    A message is only moved to Processed once every attachment on it has been
    handled - so a transient failure leaves the mail in the Inbox to be retried
    on the next poll rather than silently disappearing.
    """
    result = PollResult()
    token = get_token()

    with GraphClient(token, settings.ms_mailbox) as graph:
        processed_folder = graph.ensure_folder(settings.mail_processed_folder)
        messages = graph.unread_with_attachments(limit=limit)
        result.messages_seen = len(messages)

        for message in messages:
            msg_id = message["id"]
            subject = message.get("subject") or ""
            sender = (
                message.get("from", {}).get("emailAddress", {}).get("address", "")
                or ""
            )
            body = message.get("body", {}) or {}
            body_text = (
                _html_to_text(body.get("content", ""))
                if body.get("contentType") == "html"
                else (body.get("content") or message.get("bodyPreview") or "")
            )

            handled_all = True
            found_any = False

            try:
                attachments = graph.attachments(msg_id)
            except MailboxError as exc:
                result.errors.append(f"{subject}: {exc}")
                continue

            for att in attachments:
                if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
                    continue
                name = att.get("name") or "attachment.pdf"
                if Path(name).suffix.lower() not in ALLOWED_SUFFIXES:
                    result.skipped.append(f"{name} (unsupported type)")
                    continue
                if (att.get("size") or 0) > MAX_ATTACHMENT_BYTES:
                    result.skipped.append(f"{name} (too large)")
                    continue

                import base64

                try:
                    content = base64.b64decode(att.get("contentBytes") or "")
                except Exception:  # noqa: BLE001
                    result.errors.append(f"{name}: attachment could not be decoded")
                    handled_all = False
                    continue
                if not content:
                    result.skipped.append(f"{name} (empty)")
                    continue

                found_any = True
                with tempfile.NamedTemporaryFile(
                    suffix=Path(name).suffix, delete=False
                ) as tmp:
                    tmp.write(content)
                    tmp_path = Path(tmp.name)

                try:
                    doc = ingest_file(
                        session, tmp_path, name,
                        source="email", sender=sender, subject=subject, body=body_text,
                    )
                    session.commit()
                    where = f"job {doc.job.job_number}" if doc.job else "the Inbox"
                    result.filed.append(f"{name} -> {where} ({doc.status})")
                except DuplicateDocument:
                    session.rollback()
                    result.skipped.append(f"{name} (already received)")
                except (IngestError, Exception) as exc:  # noqa: BLE001
                    session.rollback()
                    result.errors.append(f"{name}: {exc}")
                    handled_all = False
                finally:
                    tmp_path.unlink(missing_ok=True)

            if not found_any and not result.errors:
                result.skipped.append(f"{subject or '(no subject)'} (no usable attachments)")

            # Only file the mail away if nothing failed - otherwise leave it for
            # the next poll so a blip doesn't lose an invoice.
            if handled_all:
                graph.mark_read(msg_id)
                if processed_folder:
                    graph.move(msg_id, processed_folder)

    return result


def poll_once(session: Session, limit: int = 25) -> PollResult:
    """Read the finance mailbox using whichever backend is configured.

    IMAP for an ordinary mailbox on your own mail host; Microsoft Graph for
    Exchange / Microsoft 365. Callers do not need to know which.
    """
    backend = settings.active_mail_backend()
    if backend == "imap":
        from app.mail_imap import poll_once as poll_imap

        return poll_imap(session, limit=limit)
    if backend == "graph":
        return poll_graph(session, limit=limit)

    raise MailboxError(
        "No mailbox is configured. Set MAIL_ENABLED=true and either the IMAP_* "
        "settings (an ordinary mailbox) or the MS_* settings (Microsoft 365)."
    )

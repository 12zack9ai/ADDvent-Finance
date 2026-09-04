"""Check the mailbox connection without ingesting anything.

Run this after creating the mailbox and filling in `.env`. It connects, counts
what is waiting, and lists the attachments it WOULD read - but files nothing and
changes nothing.

    python scripts/test_mail.py

If this passes, turn on the poller. If it fails, the message says what to fix.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.mail_types import ALLOWED_SUFFIXES, MailboxError  # noqa: E402


def check_imap() -> int:
    from app.mail_imap import ImapMailbox, _attachments, _decode

    print(f"Connecting to {settings.imap_host}:{settings.imap_port} "
          f"as {settings.imap_user} ...")
    with ImapMailbox() as mailbox:
        print("  Signed in.")
        mailbox.select_inbox()
        print(f"  Opened folder '{settings.imap_folder}'.")

        ids = mailbox.unread_ids(limit=10)
        print(f"  {len(ids)} unread message(s) waiting.\n")

        if not ids:
            print("Nothing unread. Send a test email with a PDF attached, "
                  "then run this again.")
            return 0

        for msg_id in ids:
            message = mailbox.fetch(msg_id)
            if message is None:
                print(f"  [{msg_id.decode()}] could not be fetched")
                continue
            subject = _decode(message.get("Subject")) or "(no subject)"
            sender = _decode(message.get("From"))
            print(f"  From:    {sender}")
            print(f"  Subject: {subject}")

            names = [name for name, _ in _attachments(message)]
            if names:
                for name in names:
                    print(f"    would read: {name}")
            else:
                print("    no readable attachments "
                      f"(looking for {', '.join(sorted(ALLOWED_SUFFIXES))})")

            from app.extract import parse_job_directive
            directive = parse_job_directive(subject)
            if directive.job_number:
                marker = " (marked as a master-quote update)" if directive.is_master_update else ""
                print(f"    job from subject: {directive.job_number}{marker}")
            else:
                print("    no job number in the subject — it would wait in the Inbox")
            print()

    print("Connection works. Nothing was filed or marked read.")
    return 0


def check_graph() -> int:
    from app.mailbox import GraphClient, get_token

    print(f"Authenticating against Microsoft Graph for {settings.ms_mailbox} ...")
    token = get_token()
    print("  Got a token.")
    with GraphClient(token, settings.ms_mailbox) as graph:
        messages = graph.unread_with_attachments(limit=10)
        print(f"  {len(messages)} message(s) with attachments.\n")
        for message in messages:
            print(f"  Subject: {message.get('subject') or '(no subject)'}")
    print("Connection works. Nothing was filed.")
    return 0


def main() -> int:
    backend = settings.active_mail_backend()

    if not backend:
        print("No mailbox configured.\n")
        print("Set MAIL_ENABLED=true in .env, then fill in EITHER:")
        print("  IMAP_HOST / IMAP_USER / IMAP_PASSWORD    (an ordinary mailbox)")
        print("  MS_TENANT_ID / MS_CLIENT_ID / ...        (Microsoft 365)")
        if settings.mail_enabled:
            print("\nMAIL_ENABLED is on but neither set is complete.")
            if settings.imap_host and not settings.imap_password:
                print("  IMAP_HOST is set but IMAP_PASSWORD is empty.")
        return 1

    print(f"Mailbox backend: {backend}\n")
    try:
        return check_imap() if backend == "imap" else check_graph()
    except MailboxError as exc:
        print(f"\nFAILED: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

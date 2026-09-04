"""Shared types for the mailbox backends.

Two ways to read the finance inbox:

  * **IMAP** (`mail_imap.py`) - an ordinary mailbox on your own mail host, the
    kind you create in cPanel or Plesk. Credentials in `.env` and it works.
  * **Microsoft Graph** (`mailbox.py`) - Exchange / Microsoft 365, app-only auth.
    Better isolation, but needs an app registration and admin consent.

They present the same surface, so the rest of the app neither knows nor cares
which one is in use.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Attachment types worth reading. Everything else - signatures, logos, calendar
# invites - is skipped rather than sent to the extractor.
ALLOWED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg"}
MAX_ATTACHMENT_BYTES = 30 * 1024 * 1024


class MailboxError(RuntimeError):
    """Anything that stopped the mailbox being read."""


@dataclass
class PollResult:
    messages_seen: int = 0
    filed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.messages_seen} message(s): {len(self.filed)} filed, "
            f"{len(self.skipped)} skipped, {len(self.errors)} error(s)"
        )

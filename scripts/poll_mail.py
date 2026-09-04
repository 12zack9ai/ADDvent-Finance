"""Poll the finance mailbox once and ingest anything new.

Run on a timer (systemd timer or cron) rather than as a long-lived loop, so a
crash can never leave polling silently stopped - the next tick just runs again.

    python scripts/poll_mail.py
    python scripts/poll_mail.py --limit 50
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.mailbox import MailboxError, poll_once  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Poll the finance mailbox.")
    parser.add_argument("--limit", type=int, default=25, help="Max messages per run")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("poll_mail")

    if not settings.mail_configured():
        log.warning(
            "Mailbox not configured - nothing to do. "
            "Set MAIL_ENABLED=true plus MS_TENANT_ID / MS_CLIENT_ID / "
            "MS_CLIENT_SECRET / MS_MAILBOX in .env."
        )
        return 0

    init_db()
    session = SessionLocal()
    try:
        result = poll_once(session, limit=args.limit)
    except MailboxError as exc:
        log.error("Mailbox error: %s", exc)
        return 2
    finally:
        session.close()

    log.info(result.summary())
    for item in result.filed:
        log.info("  filed:   %s", item)
    for item in result.skipped:
        log.info("  skipped: %s", item)
    for item in result.errors:
        log.error("  error:   %s", item)

    # Non-zero on failure so the systemd timer surfaces it rather than failing quietly.
    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

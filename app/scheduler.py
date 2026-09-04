"""Background mailbox polling, inside the web process.

Why in-process rather than a separate cron service: the database and the
document store live on one disk, and on most managed hosts a disk attaches to a
single service. A separate scheduler would be unable to reach them.

The usual objection to a background loop is that it can die quietly and nobody
notices the invoices stopped arriving. So this one is *observable*: every run
records its outcome, `/healthz` reports when the last successful poll was and
how stale that is, and the loop restarts itself after a failure instead of
exiting. Silence is the thing being guarded against - see `status()`.

`scripts/poll_mail.py` still exists for running a poll by hand.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.config import settings

log = logging.getLogger(__name__)

# Consecutive failures before backing off, so a wrong password doesn't hammer
# the mail server every five minutes forever.
_BACKOFF_AFTER = 3
_BACKOFF_FACTOR = 4

_task: Optional[asyncio.Task] = None
_state: dict[str, Any] = {
    "enabled": False,
    "started_at": None,
    "last_attempt": None,
    "last_success": None,
    "last_summary": "",
    "last_error": "",
    "runs": 0,
    "consecutive_failures": 0,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _poll_blocking() -> str:
    """One poll, on a worker thread. Own session; never reuse a request's."""
    from app.db import SessionLocal
    from app.mailbox import poll_once

    session = SessionLocal()
    try:
        result = poll_once(session, limit=25)
        for item in result.filed:
            log.info("mail: filed %s", item)
        for item in result.errors:
            log.warning("mail: %s", item)
        return result.summary()
    finally:
        session.close()


async def _loop() -> None:
    interval = max(60, settings.mail_poll_seconds)
    # A short initial delay so startup isn't competing with the first requests.
    await asyncio.sleep(15)

    while True:
        _state["last_attempt"] = _now()
        _state["runs"] += 1
        try:
            summary = await asyncio.to_thread(_poll_blocking)
            _state["last_success"] = _now()
            _state["last_summary"] = summary
            _state["last_error"] = ""
            _state["consecutive_failures"] = 0
            log.info("mail poll: %s", summary)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bad poll must not kill the loop
            _state["consecutive_failures"] += 1
            _state["last_error"] = str(exc)
            log.warning(
                "mail poll failed (%d in a row): %s",
                _state["consecutive_failures"], exc,
            )

        wait = interval
        if _state["consecutive_failures"] >= _BACKOFF_AFTER:
            wait = min(interval * _BACKOFF_FACTOR, 3600)
        await asyncio.sleep(wait)


def start() -> None:
    """Begin polling, if a mailbox is configured. Safe to call twice."""
    global _task

    if not settings.mail_configured():
        _state["enabled"] = False
        log.info("Mailbox not configured — documents arrive by upload only.")
        return
    if _task is not None and not _task.done():
        return

    _state["enabled"] = True
    _state["started_at"] = _now()
    _task = asyncio.get_event_loop().create_task(_loop())
    log.info(
        "Mailbox polling every %ds via %s",
        settings.mail_poll_seconds, settings.active_mail_backend(),
    )


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _task = None


def status() -> dict[str, Any]:
    """What /healthz reports, so a stalled poller is visible from outside.

    `stale` is the important field: true means the mailbox is configured but
    nothing has been read for well over the poll interval, which is exactly the
    failure a background loop is prone to hiding.
    """
    if not _state["enabled"]:
        return {"enabled": False}

    last_success = _state["last_success"]
    interval = max(60, settings.mail_poll_seconds)
    # Three missed cycles, with a floor, before calling it stale.
    stale_after = timedelta(seconds=max(interval * 3, 900))
    reference = last_success or _state["started_at"] or _now()

    return {
        "enabled": True,
        "backend": settings.active_mail_backend(),
        "alive": bool(_task and not _task.done()),
        "runs": _state["runs"],
        "last_success": last_success.isoformat() if last_success else None,
        "seconds_since_success": (
            int((_now() - last_success).total_seconds()) if last_success else None
        ),
        "stale": (_now() - reference) > stale_after,
        "consecutive_failures": _state["consecutive_failures"],
        "last_summary": _state["last_summary"],
        "last_error": _state["last_error"],
    }

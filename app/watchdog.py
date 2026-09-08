"""The loop that notices, and the loop that backs up.

Two jobs, one clock, because both are "wake up occasionally and check on
something" and neither deserves a service of its own:

  * **Watch.** Is the mailbox still being read? Is the disk filling up? Did
    last night's backup happen? Each answer that is "no" raises an alarm once
    and clears it once, through `app.alerts`.
  * **Back up.** Once a night, take a snapshot and push it off the disk.

Both run inside the web process for the same reason the mail poller does: the
database and the documents live on one disk, and on this host a disk attaches
to one service.

The watchdog is deliberately dumber than the things it watches. It reads state
those things already publish and sends an email. It never repairs anything -
a watchdog that tries to fix things is a second system that can be wrong.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

from app import alerts, backup, scheduler
from app.config import settings

log = logging.getLogger(__name__)

MAIL_KEY = "mail-stale"
BACKUP_KEY = "backup-failed"
DISK_KEY = "disk-low"

# How often to look. The things being watched move in hours, so this is about
# noticing within one working morning, not about precision.
CHECK_SECONDS = 900

_task: Optional[asyncio.Task] = None
_state: dict[str, Any] = {
    "started_at": None,
    "checks": 0,
    "last_check": None,
    "last_backup_day": None,
    "last_backup_error": "",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- the checks ------------------------------------------------------------

def check_mail() -> None:
    """The failure this whole file exists for: a poller that quietly stopped."""
    status = scheduler.status()
    if not status.get("enabled"):
        return                       # no mailbox configured; nothing to watch

    if status.get("stale"):
        since = status.get("last_success") or "never since this app started"
        alerts.raise_alarm(alerts.Alarm(
            key=MAIL_KEY,
            subject="The mailbox has stopped being read",
            body=(
                "Documents emailed to the finance inbox are not being picked "
                "up. The site is still working - this fails quietly, which is "
                "why you are being told rather than left to notice.\n\n"
                "Usually this is the mail password or the mail host. Nothing "
                "sent in has been lost; it is sitting in the inbox and will be "
                "read once the connection works again."
            ),
            fields={
                "Last successful check": str(since),
                "Failures in a row": str(status.get("consecutive_failures", 0)),
                "Last error": status.get("last_error") or "(none recorded)",
            },
        ))
    else:
        alerts.clear(MAIL_KEY, "The mailbox has stopped being read",
                     "The mailbox is being read again.")


def check_disk() -> None:
    free, total = backup.free_disk()
    if total <= 0:
        return
    if backup.disk_is_tight():
        alerts.raise_alarm(alerts.Alarm(
            key=DISK_KEY,
            subject="The disk is nearly full",
            body=(
                "When it fills, documents stop being stored and backups stop "
                "being written. This is worth acting on before that, not "
                "after."
            ),
            fields={
                "Free": f"{free / (1024 ** 3):.1f} GB of "
                        f"{total / (1024 ** 3):.1f} GB",
                "Percent free": f"{100 * free / total:.1f}%",
            },
        ))
    else:
        alerts.clear(DISK_KEY, "The disk is nearly full",
                     "There is room on the disk again.")


def backup_is_due(now: Optional[datetime] = None) -> bool:
    """One snapshot a day, at or after the configured hour."""
    if not settings.backup_enabled:
        return False
    now = now or _now()
    if now.hour < settings.backup_hour:
        return False
    return _state["last_backup_day"] != now.date()


def run_backup(now: Optional[datetime] = None) -> Optional[backup.Snapshot]:
    """Take tonight's snapshot. Alarms on failure, clears on success."""
    now = now or _now()
    try:
        snapshot = backup.run()
    except backup.BackupError as exc:
        _state["last_backup_error"] = str(exc)
        # The day is still marked done. Retrying a broken backup every fifteen
        # minutes turns one problem into a hundred emails, and the alarm has
        # already been raised - somebody is coming to look.
        _state["last_backup_day"] = now.date()
        alerts.raise_alarm(alerts.Alarm(
            key=BACKUP_KEY,
            subject="Last night's backup did not finish",
            body=(
                "There is no new copy of the database and documents from "
                "tonight. Older snapshots are still on the disk, so this is "
                "not yet a loss - but until it works again, the most recent "
                "copy is getting older every day."
            ),
            fields={"What went wrong": str(exc)},
        ))
        return None

    _state["last_backup_day"] = now.date()
    _state["last_backup_error"] = ""
    alerts.clear(BACKUP_KEY, "Last night's backup did not finish",
                 f"Tonight's backup worked: {snapshot.name}, "
                 f"{snapshot.megabytes} MB.")
    return snapshot


def check_backup_age() -> None:
    """A backup that silently stopped days ago looks exactly like one that ran."""
    if not settings.backup_enabled:
        return
    latest = backup.latest()
    if latest is None:
        return                       # nothing yet; the first run will make one
    if latest.age_hours > 48:
        alerts.raise_alarm(alerts.Alarm(
            key=BACKUP_KEY,
            subject="The backup is out of date",
            body="No snapshot has been written for more than two days.",
            fields={
                "Newest snapshot": latest.name,
                "Age": f"{latest.age_hours:.0f} hours",
            },
        ))


# --- the loop --------------------------------------------------------------

def check_once() -> None:
    """Everything the watchdog does in one pass. Safe to call from a test."""
    _state["checks"] += 1
    _state["last_check"] = _now()
    check_mail()
    check_disk()
    if backup_is_due():
        run_backup()
    else:
        check_backup_age()


async def _loop() -> None:
    await asyncio.sleep(60)          # let the app finish starting
    while True:
        try:
            await asyncio.to_thread(check_once)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                  # noqa: BLE001
            # A watchdog that dies is worse than no watchdog, because the
            # absence of alarms reads as "nothing is wrong".
            log.exception("watchdog: check failed: %s", exc)
        await asyncio.sleep(CHECK_SECONDS)


def start() -> None:
    global _task
    if _task is not None and not _task.done():
        return
    _state["started_at"] = _now()
    _task = asyncio.get_event_loop().create_task(_loop())
    log.info("Watchdog every %ds; backup at %02d:00 UTC, keeping %d",
             CHECK_SECONDS, settings.backup_hour, settings.backup_keep)


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
    latest = backup.latest()
    free, total = backup.free_disk()
    return {
        "checks": _state["checks"],
        "last_check": _state["last_check"].isoformat() if _state["last_check"] else None,
        "backup": {
            "enabled": settings.backup_enabled,
            "offsite": backup.supabase_configured(),
            "latest": latest.name if latest else None,
            "taken_at": latest.taken_at.isoformat() if latest else None,
            "age_hours": round(latest.age_hours, 1) if latest else None,
            "megabytes": latest.megabytes if latest else None,
            "count": len(backup.every()),
            "last_error": _state["last_backup_error"],
        },
        "disk": {
            "free_gb": round(free / (1024 ** 3), 1),
            "total_gb": round(total / (1024 ** 3), 1),
            "tight": backup.disk_is_tight(),
        },
        "alarms": sorted(alerts.open_incidents()),
    }

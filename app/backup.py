"""Nightly backup, and a copy you can hold in your hand.

The whole business record - every invoice, every approval, every PDF a vendor
ever sent - lives on one disk belonging to one hosting company. That is fine
until the day it is not, and the failure people actually hit is not the data
centre burning down. It is someone deleting the wrong job, or a bad release
writing nonsense into a column, on an ordinary Tuesday.

So there are two copies, and they answer different questions:

  * **On the disk, rotated.** Fourteen nights of snapshots. This is the "undo"
    - it answers "what did this look like before Thursday".
  * **Off the disk.** Uploaded to Supabase Storage where it is configured, and
    downloadable from the site at any time. This is the one that survives
    losing the host.

A snapshot is a single zip holding the database and every stored document,
because the database without the paper is a ledger of files nobody can open.

The database is copied with SQLite's own backup API rather than by reading the
file. Under WAL - which this app runs in - the file on disk is not the whole
database, and a plain copy of it can be silently short of the most recent
writes. `Connection.backup()` takes a consistent copy of a live database
without blocking writers, which is exactly the situation here.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import settings

log = logging.getLogger("finance.backup")

STAMP = "%Y%m%d-%H%M%S"
PREFIX = "addvent-finance-"
SUFFIX = ".zip"


class BackupError(RuntimeError):
    pass


@dataclass
class Snapshot:
    path: Path
    taken_at: datetime
    bytes: int
    documents: int

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def megabytes(self) -> float:
        return round(self.bytes / (1024 * 1024), 1)

    @property
    def age_hours(self) -> float:
        return (datetime.now(timezone.utc) - self.taken_at).total_seconds() / 3600


def _db_path() -> Optional[Path]:
    """The SQLite file behind the app, or None if it is not SQLite."""
    url = settings.resolved_db_url()
    if not url.startswith("sqlite"):
        return None
    return Path(url.split("///", 1)[1])


def _copy_database(into: Path) -> None:
    """A consistent copy of the live database, WAL and all."""
    source = _db_path()
    if source is None:
        raise BackupError("Only SQLite databases can be snapshotted here.")
    if not source.exists():
        raise BackupError(f"No database at {source}.")

    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(into)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def take(now: Optional[datetime] = None) -> Snapshot:
    """Write one snapshot. Returns it; raises BackupError if it cannot."""
    now = now or datetime.now(timezone.utc)
    settings.ensure_dirs()
    out_dir = settings.backups_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    target = out_dir / f"{PREFIX}{now.strftime(STAMP)}{SUFFIX}"
    working = target.with_suffix(".partial")
    db_copy = out_dir / "database.partial"
    documents = 0

    try:
        _copy_database(db_copy)
        with zipfile.ZipFile(working, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(db_copy, "finance.db")
            if settings.backup_documents:
                uploads = settings.uploads_dir
                for path in sorted(uploads.rglob("*")):
                    if not path.is_file():
                        continue
                    zf.write(path, str(Path("documents") / path.relative_to(uploads)))
                    documents += 1
        # Only now does it get its real name. A snapshot that appears in the
        # directory is a snapshot that finished - a half-written zip carrying
        # the right name is worse than no backup, because it looks like one.
        working.replace(target)
    except Exception as exc:                      # noqa: BLE001 - reported, not swallowed
        working.unlink(missing_ok=True)
        raise BackupError(str(exc)) from exc
    finally:
        db_copy.unlink(missing_ok=True)

    snapshot = Snapshot(target, now, target.stat().st_size, documents)
    log.info("backup: %s (%s MB, %d document(s))",
             snapshot.name, snapshot.megabytes, documents)
    return snapshot


def rotate(keep: Optional[int] = None) -> int:
    """Delete all but the newest `keep` snapshots. Returns how many went."""
    keep = settings.backup_keep if keep is None else keep
    snaps = every()
    removed = 0
    for snapshot in snaps[keep:]:
        snapshot.path.unlink(missing_ok=True)
        removed += 1
    if removed:
        log.info("backup: removed %d old snapshot(s), keeping %d", removed, keep)
    return removed


def every() -> list[Snapshot]:
    """Every snapshot on disk, newest first."""
    out_dir = settings.backups_dir
    if not out_dir.exists():
        return []
    found = []
    for path in out_dir.glob(f"{PREFIX}*{SUFFIX}"):
        stamp = path.name[len(PREFIX):-len(SUFFIX)]
        try:
            taken = datetime.strptime(stamp, STAMP).replace(tzinfo=timezone.utc)
        except ValueError:
            continue          # not one of ours; leave it alone
        found.append(Snapshot(path, taken, path.stat().st_size, 0))
    found.sort(key=lambda s: s.taken_at, reverse=True)
    return found


def latest() -> Optional[Snapshot]:
    found = every()
    return found[0] if found else None


def free_disk() -> tuple[int, int]:
    """Bytes free and bytes total on the volume holding the data."""
    usage = shutil.disk_usage(settings.data_dir)
    return usage.free, usage.total


def disk_is_tight() -> bool:
    free, total = free_disk()
    if total <= 0:
        return False
    return (free / total) < settings.disk_warn_below


# --- off the disk ----------------------------------------------------------

def supabase_configured() -> bool:
    return bool(settings.supabase_url and settings.supabase_key
                and settings.supabase_bucket)


def upload(snapshot: Snapshot) -> str:
    """Put one snapshot in Supabase Storage. Returns the object path.

    Deliberately a plain HTTPS PUT rather than a client library: one endpoint,
    one header, nothing to keep upgraded, and no new dependency in a codebase
    whose whole point is being auditable.
    """
    if not supabase_configured():
        raise BackupError("Supabase is not configured.")

    import requests

    base = settings.supabase_url.rstrip("/")
    key = f"{settings.backup_prefix}/{snapshot.name}".strip("/")
    url = f"{base}/storage/v1/object/{settings.supabase_bucket}/{key}"
    with snapshot.path.open("rb") as fh:
        response = requests.put(
            url,
            data=fh,
            headers={
                "Authorization": f"Bearer {settings.supabase_key}",
                "Content-Type": "application/zip",
                # Re-running a night's backup replaces it rather than failing.
                "x-upsert": "true",
            },
            timeout=settings.backup_upload_timeout,
        )
    if response.status_code >= 300:
        raise BackupError(
            f"Supabase refused the upload ({response.status_code}): "
            f"{response.text[:200]}"
        )
    log.info("backup: uploaded %s to Supabase", snapshot.name)
    return key


def run() -> Snapshot:
    """Take a snapshot, send it off the disk if we can, tidy up. One call."""
    snapshot = take()
    if supabase_configured():
        try:
            upload(snapshot)
        except BackupError as exc:
            # The local copy still exists and is still good. Losing the upload
            # is a degraded backup, not a failed one, and saying otherwise
            # would train people to ignore the alarm.
            log.warning("backup: local snapshot kept, upload failed: %s", exc)
            raise
    rotate()
    return snapshot

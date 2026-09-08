#!/usr/bin/env python3
"""Rebuild the app from a backup zip.

The backup was a guess until this existed. A copy nobody has ever restored is
not a backup - it is a file that resembles one, and the day you find out is
the worst possible day to find out.

    python scripts/restore_backup.py addvent-finance-20260908-070000.zip
    python scripts/restore_backup.py latest --into /var/lib/finance-automation

What it does, in order, and why that order:

  1. Reads the zip and refuses it if `finance.db` is missing or is not a
     readable SQLite database with this app's tables in it. A corrupt backup
     must fail here, not halfway through overwriting the live one.
  2. Moves anything already in the target aside as `*.superseded-<stamp>`
     rather than deleting it. If the restore turns out to be the wrong file,
     the thing you just replaced is still there.
  3. Puts `finance.db` and `documents/` into place.

Nothing here talks to the running app. Stop it, restore, start it again.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import backup                                    # noqa: E402
from app.config import settings                           # noqa: E402

# A restored database has to have these or it is not this app's database.
REQUIRED_TABLES = {"job", "invoice", "invoice_line", "quote", "document"}


class RestoreError(RuntimeError):
    pass


def _check(db: Path) -> dict[str, int]:
    """Prove the file is our database before anything is overwritten."""
    # connect() does not read the file, so a non-database only fails on the
    # first query. Both have to be inside the guard or the script dies with a
    # raw sqlite error instead of saying what is wrong with the backup.
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise RestoreError(f"Not a readable database: {exc}") from exc
    try:
        try:
            ok = con.execute("PRAGMA integrity_check").fetchone()
            names = {row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        except sqlite3.DatabaseError as exc:
            raise RestoreError(f"Not a readable database: {exc}") from exc

        if not ok or ok[0] != "ok":
            raise RestoreError(f"The database inside the backup is damaged: {ok}")

        missing = REQUIRED_TABLES - names
        if missing:
            raise RestoreError(
                "This zip does not hold this app's database - missing "
                + ", ".join(sorted(missing))
            )
        try:
            return {
                table: con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in sorted(REQUIRED_TABLES)
            }
        except sqlite3.DatabaseError as exc:
            raise RestoreError(f"Could not read the backup: {exc}") from exc
    finally:
        con.close()


def _set_aside(path: Path, stamp: str) -> Path | None:
    """Move something out of the way. Never delete - see the docstring."""
    if not path.exists():
        return None
    kept = path.with_name(f"{path.name}.superseded-{stamp}")
    path.rename(kept)
    return kept


def _default_db_name() -> str:
    """What the database file is called in the folder being restored into."""
    return Path(settings.resolved_db_url().split("///", 1)[1]).name


def restore(archive: Path, into: Path, db_name: str | None = None) -> str:
    """Rebuild `into` from `archive`.

    `db_name` is what the restored database should be called. It defaults to
    whatever the current configuration points at, which is right when this is
    run with the app's own environment - and is passed explicitly by anything
    restoring into a folder that is not the configured one, so the answer never
    depends on ambient settings that have nothing to do with the target.
    """
    if not archive.exists():
        raise RestoreError(f"No such file: {archive}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    into.mkdir(parents=True, exist_ok=True)
    staging = into / f".restore-{stamp}"
    staging.mkdir()

    try:
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
            if "finance.db" not in names:
                raise RestoreError("No finance.db in that zip.")
            # Refuse a zip that would write outside the staging directory.
            # These archives are ours, but a restore script is exactly the
            # thing somebody points at a file from an inbox one day.
            for name in names:
                target = (staging / name).resolve()
                if not str(target).startswith(str(staging.resolve())):
                    raise RestoreError(f"Refusing a path outside the folder: {name}")
            zf.extractall(staging)

        counts = _check(staging / "finance.db")

        db_target = into / (db_name or _default_db_name())
        kept_db = _set_aside(db_target, stamp)
        # And the WAL sidecars, or SQLite will replay them over the restore.
        for extra in (db_target.with_name(db_target.name + "-wal"),
                      db_target.with_name(db_target.name + "-shm")):
            _set_aside(extra, stamp)
        shutil.move(str(staging / "finance.db"), db_target)

        documents = staging / "documents"
        restored_files = 0
        if documents.exists():
            uploads = into / "uploads"
            _set_aside(uploads, stamp)
            shutil.move(str(documents), uploads)
            restored_files = sum(1 for p in uploads.rglob("*") if p.is_file())

        summary = ", ".join(f"{n} {t}" for t, n in counts.items())
        note = f"Restored {archive.name} into {into}: {summary}"
        if restored_files:
            note += f", {restored_files} document file(s)"
        if kept_db:
            note += f". The previous database is kept as {kept_db.name}"
        return note
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", help='A backup zip, or "latest"')
    parser.add_argument("--into", default=str(settings.data_dir),
                        help="Data directory to restore into")
    parser.add_argument("--db-name", default=None,
                        help="Name for the restored database file "
                             f"(default: {_default_db_name()})")
    args = parser.parse_args()

    if args.archive == "latest":
        newest = backup.latest()
        if newest is None:
            print("No backups found.", file=sys.stderr)
            return 1
        archive = newest.path
    else:
        archive = Path(args.archive)

    try:
        print(restore(archive, Path(args.into), args.db_name))
    except RestoreError as exc:
        print(f"Restore refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

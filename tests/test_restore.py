"""Actually restoring a backup.

Until this file existed the backup was a hypothesis. The failure it guards
against is specific and nasty: you only ever find out a backup is useless on
the day you need it, which is by definition the worst day.

So this does the whole round trip on real files - write a job and a document,
take a snapshot, destroy the data directory, restore from the zip, and read
the job back out through the app's own models.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = Path(tempfile.mkdtemp(prefix="finance-restore-"))
os.environ.setdefault("DATA_DIR", str(_TMP))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP / 'test.db'}")
os.environ["APP_PASSWORD"] = ""
os.environ["ANTHROPIC_API_KEY"] = "test"

from app import backup  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Job  # noqa: E402
from scripts.restore_backup import RestoreError, restore  # noqa: E402


def setup_module(_module) -> None:
    init_db()
    settings.ensure_dirs()


@pytest.fixture
def a_backup(tmp_path):
    """A snapshot with a known job and a known document inside it."""
    marker = "260980"
    with SessionLocal() as session:
        if session.query(Job).filter_by(job_number=marker).first() is None:
            session.add(Job(job_number=marker, name="Restore rehearsal"))
            session.commit()
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    (settings.uploads_dir / "kept.pdf").write_bytes(b"%PDF-1.4 the paper")
    return backup.take(), marker


def test_a_backup_restores_into_an_empty_folder(a_backup, tmp_path):
    """The whole point, end to end and on real files."""
    snapshot, marker = a_backup
    fresh = tmp_path / "restored"

    note = restore(snapshot.path, fresh, "test.db")
    assert marker not in note or True          # the note is prose, not the proof

    con = sqlite3.connect(fresh / "test.db")
    try:
        found = con.execute(
            "SELECT name FROM job WHERE job_number = ?", (marker,)
        ).fetchone()
    finally:
        con.close()
    assert found is not None and found[0] == "Restore rehearsal"
    assert (fresh / "uploads" / "kept.pdf").read_bytes() == b"%PDF-1.4 the paper"


def test_restoring_over_a_live_folder_keeps_what_it_replaced(a_backup, tmp_path):
    """If the wrong file gets restored, the thing it replaced must still exist.
    A restore that deletes is a restore you cannot undo."""
    snapshot, _ = a_backup
    target = tmp_path / "live"
    target.mkdir()
    (target / "test.db").write_bytes(b"the database that was already here")

    restore(snapshot.path, target, "test.db")
    kept = list(target.glob("test.db.superseded-*"))
    assert len(kept) == 1
    assert kept[0].read_bytes() == b"the database that was already here"


def test_the_wal_sidecars_are_moved_aside_too(a_backup, tmp_path):
    """Left in place, SQLite replays them over the restored file and quietly
    undoes the restore."""
    snapshot, _ = a_backup
    target = tmp_path / "live"
    target.mkdir()
    (target / "test.db").write_bytes(b"old")
    (target / "test.db-wal").write_bytes(b"stale wal")
    (target / "test.db-shm").write_bytes(b"stale shm")

    restore(snapshot.path, target, "test.db")
    assert not (target / "test.db-wal").exists()
    assert not (target / "test.db-shm").exists()
    assert list(target.glob("test.db-wal.superseded-*"))


def test_a_zip_without_our_database_is_refused_before_anything_is_touched(tmp_path):
    bad = tmp_path / "not-ours.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("finance.db", "this is not a database")

    target = tmp_path / "live"
    target.mkdir()
    (target / "test.db").write_bytes(b"precious")

    with pytest.raises(RestoreError):
        restore(bad, target)
    # Untouched. A corrupt backup must fail before it overwrites a good one.
    assert (target / "test.db").read_bytes() == b"precious"


def test_a_database_that_is_not_this_app_is_refused(tmp_path):
    """Valid SQLite, wrong application. Restoring it would leave an app that
    starts, serves pages, and has lost everything."""
    other = tmp_path / "other.db"
    con = sqlite3.connect(other)
    con.execute("CREATE TABLE recipes (id integer primary key)")
    con.commit()
    con.close()

    bad = tmp_path / "wrong-app.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.write(other, "finance.db")

    with pytest.raises(RestoreError, match="does not hold this app"):
        restore(bad, tmp_path / "live")


def test_a_zip_that_writes_outside_the_folder_is_refused(tmp_path):
    """These archives are ours, but a restore script is exactly the thing
    somebody eventually points at a file that arrived by email."""
    nasty = tmp_path / "nasty.zip"
    with zipfile.ZipFile(nasty, "w") as zf:
        zf.writestr("finance.db", "x")
        zf.writestr("../escaped.txt", "should never be written")

    with pytest.raises(RestoreError, match="outside"):
        restore(nasty, tmp_path / "live")
    assert not (tmp_path / "escaped.txt").exists()


def test_a_missing_file_says_so_rather_than_half_restoring(tmp_path):
    with pytest.raises(RestoreError, match="No such file"):
        restore(tmp_path / "nothing-here.zip", tmp_path / "live")

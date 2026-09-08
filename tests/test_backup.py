"""Backups, alarms, and the login delay.

Zack: *"Can't have issues with this."* Everything here is about the failures
nobody sees at the time - a copy that was never taken, a mailbox that quietly
stopped, a password being guessed at leisure.

The load-bearing test is `test_a_snapshot_captures_writes_still_in_the_wal`.
This app runs SQLite in WAL mode, which means the .db file on disk is not the
whole database: recent writes live in a sidecar until a checkpoint. Copying the
file would produce a backup that is silently missing the newest work, and it
would look completely fine. That is the exact shape of a backup you discover is
useless on the day you need it.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = Path(tempfile.mkdtemp(prefix="finance-backup-"))
os.environ.setdefault("DATA_DIR", str(_TMP))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP / 'test.db'}")
os.environ["APP_PASSWORD"] = ""
os.environ["ANTHROPIC_API_KEY"] = "test"

from fastapi.testclient import TestClient  # noqa: E402

from app import alerts, auth, backup, watchdog  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Job  # noqa: E402

client = TestClient(app)


def setup_module(_module) -> None:
    init_db()
    settings.ensure_dirs()


@pytest.fixture(autouse=True)
def _clean():
    for path in settings.backups_dir.glob("*"):
        path.unlink()
    alerts.forget_everything()
    auth.forget_attempts()
    watchdog._state["last_backup_day"] = None
    watchdog._state["last_backup_error"] = ""
    yield


# --- taking one ------------------------------------------------------------

def test_a_snapshot_captures_writes_still_in_the_wal():
    """The whole reason this uses SQLite's backup API and not a file copy.

    The row below is written and committed but not checkpointed, so it lives in
    the -wal sidecar rather than in finance.db. A copy of the file would miss
    it and say nothing about having done so.
    """
    marker = "260950"
    with SessionLocal() as session:
        session.add(Job(job_number=marker, name="In the WAL"))
        session.commit()

    snapshot = backup.take()
    out = _TMP / "restored.db"
    with zipfile.ZipFile(snapshot.path) as zf:
        out.write_bytes(zf.read("finance.db"))

    con = sqlite3.connect(out)
    try:
        found = con.execute(
            "SELECT name FROM job WHERE job_number = ?", (marker,)
        ).fetchone()
    finally:
        con.close()
    assert found is not None and found[0] == "In the WAL"


def test_a_snapshot_carries_the_documents_not_just_the_rows():
    """A ledger nobody can check against the paper is half a backup."""
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    (settings.uploads_dir / "an-invoice.pdf").write_bytes(b"%PDF-1.4 pretend")

    snapshot = backup.take()
    with zipfile.ZipFile(snapshot.path) as zf:
        names = zf.namelist()
    assert "finance.db" in names
    assert any(n.startswith("documents/") and n.endswith("an-invoice.pdf")
               for n in names)
    assert snapshot.documents >= 1


def test_a_half_written_backup_never_takes_the_real_name(monkeypatch):
    """A zip that failed halfway is worse than no backup - it looks like one."""
    def explode(*_args, **_kwargs):
        raise OSError("disk went away")

    monkeypatch.setattr(backup, "_copy_database", explode)
    with pytest.raises(backup.BackupError):
        backup.take()
    assert backup.every() == []
    assert list(settings.backups_dir.glob("*.partial")) == []


def test_rotation_keeps_the_newest_and_drops_the_rest():
    now = datetime.now(timezone.utc)
    for days in range(6):
        stamp = (now - timedelta(days=days)).strftime(backup.STAMP)
        (settings.backups_dir / f"{backup.PREFIX}{stamp}{backup.SUFFIX}").write_bytes(b"z")

    assert len(backup.every()) == 6
    assert backup.rotate(keep=3) == 3
    left = backup.every()
    assert len(left) == 3
    # And it kept the right three.
    assert left[0].taken_at > left[1].taken_at > left[2].taken_at
    assert (now - left[2].taken_at).days == 2


def test_a_stranger_s_file_in_the_backup_folder_is_left_alone():
    (settings.backups_dir / "notes.txt").write_text("someone put this here")
    assert backup.every() == []
    backup.rotate(keep=0)
    assert (settings.backups_dir / "notes.txt").exists()


# --- getting one out -------------------------------------------------------

def test_a_backup_can_be_downloaded():
    snapshot = backup.take()
    page = client.get(f"/backup/file/{snapshot.name}")
    assert page.status_code == 200
    assert page.headers["content-type"] == "application/zip"
    assert len(page.content) == snapshot.bytes


def test_the_download_route_cannot_be_walked_out_of_its_folder():
    """It returns whole files, so a path built from user input is a hole."""
    for attempt in ("../finance.db", "..%2Ffinance.db", "nonsense.zip"):
        r = client.get(f"/backup/file/{attempt}", follow_redirects=False)
        assert r.status_code in (303, 404), attempt
        if r.status_code == 303:
            assert "No+such+backup" in r.headers["location"]


def test_the_backup_page_says_when_there_is_no_copy_off_this_server():
    body = client.get("/backup").text
    assert "Stored off this server" in body
    assert ">No<" in body          # Supabase not configured in tests


# --- the watchdog ----------------------------------------------------------

def test_a_stale_mailbox_raises_one_alarm_and_only_one(monkeypatch):
    monkeypatch.setattr("app.scheduler.status", lambda: {
        "enabled": True, "stale": True, "consecutive_failures": 4,
        "last_error": "authentication failed", "last_success": None,
    })
    watchdog.check_mail()
    watchdog.check_mail()
    watchdog.check_mail()
    assert alerts.is_open(watchdog.MAIL_KEY)
    assert alerts.open_labels() == [alerts.LABELS["mail-stale"]]


def test_and_it_says_so_when_the_mailbox_comes_back(monkeypatch):
    monkeypatch.setattr("app.scheduler.status", lambda: {
        "enabled": True, "stale": True, "consecutive_failures": 4,
        "last_error": "", "last_success": None,
    })
    watchdog.check_mail()
    assert alerts.is_open(watchdog.MAIL_KEY)

    monkeypatch.setattr("app.scheduler.status", lambda: {
        "enabled": True, "stale": False, "consecutive_failures": 0,
        "last_error": "", "last_success": "2026-09-08T12:00:00+00:00",
    })
    watchdog.check_mail()
    assert not alerts.is_open(watchdog.MAIL_KEY)


def test_no_mailbox_configured_is_not_a_fault():
    watchdog.check_mail()          # scheduler reports disabled in tests
    assert not alerts.is_open(watchdog.MAIL_KEY)


def test_a_failed_backup_raises_an_alarm_and_does_not_retry_all_night(monkeypatch):
    def explode():
        raise backup.BackupError("no space left on device")

    monkeypatch.setattr(backup, "run", explode)
    assert watchdog.run_backup() is None
    assert alerts.is_open(watchdog.BACKUP_KEY)
    # The day is marked done even though it failed: retrying every fifteen
    # minutes would turn one problem into a hundred emails.
    assert not watchdog.backup_is_due()


def test_a_backup_that_works_clears_the_alarm(monkeypatch):
    monkeypatch.setattr(backup, "run", lambda: backup.take())
    watchdog._state["last_backup_error"] = "yesterday's failure"
    alerts.raise_alarm(alerts.Alarm(watchdog.BACKUP_KEY, "old", "old"))

    snapshot = watchdog.run_backup()
    assert snapshot is not None
    assert not alerts.is_open(watchdog.BACKUP_KEY)
    assert watchdog._state["last_backup_error"] == ""


def test_one_backup_a_day_not_one_every_check():
    now = datetime.now(timezone.utc).replace(hour=23)
    watchdog._state["last_backup_day"] = None
    assert watchdog.backup_is_due(now)
    watchdog._state["last_backup_day"] = now.date()
    assert not watchdog.backup_is_due(now)
    assert watchdog.backup_is_due(now + timedelta(days=1))


def test_healthz_reports_the_backup_and_the_disk():
    body = client.get("/healthz").json()
    assert body["ok"] is True
    assert "watchdog" in body
    assert "backup" in body["watchdog"] and "disk" in body["watchdog"]
    assert body["watchdog"]["disk"]["total_gb"] > 0


def test_an_alarm_appears_on_every_page_not_a_status_screen(monkeypatch):
    monkeypatch.setattr("app.scheduler.status", lambda: {
        "enabled": True, "stale": True, "consecutive_failures": 9,
        "last_error": "", "last_success": None,
    })
    watchdog.check_mail()
    assert alerts.LABELS["mail-stale"] in client.get("/").text


def test_an_alarm_never_emails_outside_the_company(monkeypatch):
    """The standing rule, and an alert address is not an exception to it."""
    monkeypatch.setattr(settings, "alert_email", "someone@gmail.com")
    assert alerts.where_to() == ""


# --- guessing the password -------------------------------------------------

def test_a_few_wrong_passwords_cost_nothing():
    for _ in range(5):
        assert auth.wait_for("10.0.0.1") == 0
        auth.note_failure("10.0.0.1")


def test_then_the_wait_grows():
    for _ in range(9):
        auth.note_failure("10.0.0.2")
    assert auth.wait_for("10.0.0.2") > 0


def test_the_delay_is_per_address_so_one_person_cannot_lock_out_the_office():
    for _ in range(20):
        auth.note_failure("10.0.0.3")
    assert auth.wait_for("10.0.0.3") > 0
    assert auth.wait_for("10.0.0.4") == 0


def test_getting_it_right_forgets_the_failures():
    for _ in range(9):
        auth.note_failure("10.0.0.5")
    assert auth.wait_for("10.0.0.5") > 0
    auth.note_success("10.0.0.5")
    assert auth.wait_for("10.0.0.5") == 0


def test_the_wait_is_capped_so_a_locked_out_office_always_gets_back_in():
    for _ in range(200):
        auth.note_failure("10.0.0.6")
    assert auth.wait_for("10.0.0.6") <= auth._MAX_WAIT + 1

"""SQLite under a slow write, because this application has one.

Reading a document is a Claude call taking around twenty seconds, and the
document row is written before the call and committed after it - so a write
transaction is held open for the whole of it. The mailbox poller does exactly
that from inside the web process every five minutes.

On SQLite's stock settings that meant the first person to approve an invoice
while a document was being read waited five seconds and then got a 500,
"database is locked", with nothing on the page to suggest why.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = Path(tempfile.mkdtemp(prefix="finance-conc-"))
os.environ.setdefault("DATA_DIR", str(_TMP))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP / 'c.db'}")
os.environ.setdefault("APP_PASSWORD", "")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from sqlalchemy import func, select, text  # noqa: E402

from app.db import SessionLocal, engine, init_db  # noqa: E402
from app.models import Job  # noqa: E402


def _pragma(name: str):
    with engine.begin() as conn:
        return conn.execute(text(f"PRAGMA {name}")).scalar()


def test_readers_are_not_shut_out_by_a_writer():
    """Every page in this app is a read. WAL is what lets them carry on while
    a document is being ingested."""
    if not str(engine.url).startswith("sqlite"):
        return
    assert _pragma("journal_mode") == "wal"


def test_a_write_waits_out_a_slow_one_instead_of_failing():
    """Longer than the write it has to wait for. Five seconds - the default -
    is shorter than a single document read, so a person's approval failed
    rather than queueing."""
    if not str(engine.url).startswith("sqlite"):
        return
    assert _pragma("busy_timeout") >= 30_000


def test_an_approval_goes_through_while_a_document_is_being_read():
    """The whole point, end to end: one transaction held open across a slow
    call, and a second write arriving in the middle of it."""
    init_db()
    outcome = {}

    def ingesting():
        with SessionLocal() as session:
            session.add(Job(job_number="260911", name="being read"))
            session.flush()                 # takes the write lock
            time.sleep(2)                   # the API call
            session.commit()

    def approving():
        time.sleep(0.4)                     # arrive mid-ingest
        started = time.time()
        try:
            with SessionLocal() as session:
                session.add(Job(job_number="260912", name="somebody clicking approve"))
                session.commit()
            outcome["approve"] = time.time() - started
        except Exception as exc:            # noqa: BLE001
            outcome["approve"] = f"failed: {type(exc).__name__}"

    threads = [threading.Thread(target=f) for f in (ingesting, approving)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not isinstance(outcome["approve"], str), outcome["approve"]

    with SessionLocal() as session:
        assert session.scalar(
            select(func.count(Job.id)).where(Job.job_number.in_(("260911", "260912")))
        ) == 2

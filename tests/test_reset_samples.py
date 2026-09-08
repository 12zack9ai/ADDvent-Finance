"""Emptying the app for the day the real documents start.

The dangerous part, and the reason this is not a one-line delete: **260000 is
the first real job number of 2026** and it is also the job the shipped sample
documents were loaded into. Deleting by job number would eventually delete real
work, and the day it did nobody would connect it to a script written months
earlier.
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = Path(tempfile.mkdtemp(prefix="finance-reset-"))
os.environ.setdefault("DATA_DIR", str(_TMP))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP / 'r.db'}")
os.environ.setdefault("APP_PASSWORD", "")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from sqlalchemy import func, select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import (  # noqa: E402
    CheckRequest,
    Document,
    Invoice,
    Job,
    Purchase,
    Quote,
)

from scripts import reset_samples, seed_samples  # noqa: E402


@pytest.fixture()
def db():
    init_db()
    with SessionLocal() as session:
        reset_samples.wipe(session)
        yield session


def _ingested_sample(session, job):
    """A document ingested from the files this repository ships."""
    path = next(iter(sorted(reset_samples.SAMPLE_DIR.rglob("*.pdf"))))
    doc = Document(
        job_id=job.id, filename=path.name,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        stored_path=str(path), kind="quote", status="ready",
    )
    session.add(doc)
    session.flush()
    quote = Quote(job_id=job.id, document_id=doc.id, vendor="New Castle",
                  is_master=True, total=D("17182.90"))
    session.add(quote)
    session.flush()
    return doc, quote


# --- it goes ---------------------------------------------------------------

def test_the_seeded_band_and_the_ingested_samples_both_go(db):
    seed_samples.seed(db)
    job = Job(job_number="260000", name="118 Ridgeview Terrace")
    db.add(job)
    db.flush()
    _ingested_sample(db, job)
    db.commit()

    assert seed_samples.sample_jobs(db)
    reset_samples.wipe(db)

    assert seed_samples.sample_jobs(db) == []
    assert db.scalars(select(Purchase)).all() == []
    assert db.scalars(select(CheckRequest)).all() == []
    assert db.scalar(select(Job).where(Job.job_number == "260000")) is None


def test_running_it_twice_finds_nothing_the_second_time(db):
    seed_samples.seed(db)
    reset_samples.wipe(db)
    assert "0 sample job" in reset_samples.wipe(db)


# --- and real work does not ------------------------------------------------

def test_a_real_invoice_on_job_260000_survives_with_its_job(db):
    """The whole point. 260000 is the first real job of 2026, so the sample
    documents are found by the hash of the files we ship - never by the job
    they happen to sit on."""
    job = Job(job_number="260000", name="A real job now")
    db.add(job)
    db.flush()
    doc, quote = _ingested_sample(db, job)

    real_doc = Document(job_id=job.id, filename="a-real-invoice.pdf",
                        sha256="a" * 64, stored_path="/real", kind="invoice",
                        status="ready")
    db.add(real_doc)
    db.flush()
    real = Invoice(job_id=job.id, document_id=real_doc.id, quote_id=quote.id,
                   vendor="ABC Supply Co.", invoice_number="INV-1",
                   total=D("6154.00"), invoice_date=date(2026, 9, 8))
    db.add(real)
    db.commit()
    real_id = real.id

    reset_samples.wipe(db)

    kept = db.scalar(select(Job).where(Job.job_number == "260000"))
    assert kept is not None                       # the job stays
    invoice = db.get(Invoice, real_id)
    assert invoice is not None                    # and so does the invoice
    assert invoice.quote_id is None               # no longer pointing at nothing
    assert db.get(Document, real_doc.id) is not None
    assert db.scalar(
        select(func.count(Document.id)).where(Document.sha256 == doc.sha256)
    ) == 0                                        # the sample document went


def test_a_real_job_in_the_reserved_band_is_still_removed(db):
    """The 269xxx band is reserved, so anything in it is ours by definition -
    that is what makes the band worth reserving."""
    db.add(Job(job_number="269001", name="Sample"))
    db.commit()
    reset_samples.wipe(db)
    assert db.scalar(select(Job).where(Job.job_number == "269001")) is None


def test_nothing_else_in_the_database_is_touched(db):
    job = Job(job_number="260714", name="Real work")
    db.add(job)
    db.flush()
    doc = Document(job_id=job.id, filename="real.pdf", sha256="b" * 64,
                   stored_path="/x", kind="invoice", status="ready")
    db.add(doc)
    db.flush()
    db.add(Invoice(job_id=job.id, document_id=doc.id, vendor="ABC Supply Co.",
                   invoice_number="INV-9", total=D("100.00")))
    db.add(CheckRequest(job_id=job.id, vendor="Township", amount=D("450"),
                        purpose="permit"))
    db.add(Purchase(job_id=job.id, merchant="Home Depot", total=D("84.12")))
    db.commit()

    reset_samples.wipe(db)

    kept = db.scalar(select(Job).where(Job.job_number == "260714"))
    assert kept is not None
    # Scoped to this job: earlier tests deliberately leave their own real work
    # behind, which is the behaviour being checked, not a leak.
    assert len(kept.invoices) == 1
    assert len(kept.check_requests) == 1
    assert len(kept.purchases) == 1
    assert len(kept.documents) == 1


# --- saying what it would do -----------------------------------------------

def test_the_survey_reports_without_removing_anything(db):
    seed_samples.seed(db)
    before = len(seed_samples.sample_jobs(db))

    state = reset_samples.survey(db)

    assert len(state["seeded_jobs"]) == before
    assert len(seed_samples.sample_jobs(db)) == before      # still all there

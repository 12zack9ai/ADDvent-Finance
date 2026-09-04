"""Load the sample job into an empty install, so a fresh deploy has something
real to look at.

Deliberately conservative:

  * It only runs when LOAD_SAMPLES is set, so it can never surprise anybody.
  * It skips entirely if the sample job already has a master quote, so a
    restart cannot duplicate documents or spend money re-reading them.
  * It runs off the request path. Each document is a Claude call taking around
    twenty seconds, and blocking startup for a minute and a half would fail the
    host's health check and roll the deploy back.

Run by hand with:  python scripts/load_samples.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import Job
from app import services

log = logging.getLogger(__name__)

JOB_NUMBER = "260000"
SAMPLE_DIR = Path(__file__).resolve().parent.parent / "samples" / "job-260000"

PLAN = [
    ("01-QUOTE-07RM0002891004.pdf",   f"Quote for job {JOB_NUMBER} - 118 Ridgeview Terrace reroof", True),
    ("02-INVOICE-07RM0003114872.pdf", f"Job {JOB_NUMBER} delivery 1", False),
    ("03-INVOICE-07RM0003119045.pdf", f"Job {JOB_NUMBER} delivery 2", False),
    ("04-INVOICE-07RM0003126310.pdf", f"Job {JOB_NUMBER} delivery 3", False),
]


def already_loaded() -> bool:
    with SessionLocal() as session:
        job = session.scalar(select(Job).where(Job.job_number == JOB_NUMBER))
        return job is not None and any(q.is_master for q in job.quotes)


def load() -> str:
    """Ingest the sample documents. Returns a one-line summary."""
    if not SAMPLE_DIR.is_dir():
        return f"no sample directory at {SAMPLE_DIR}"
    if already_loaded():
        return f"job {JOB_NUMBER} already loaded, nothing to do"

    loaded, skipped, failed = 0, 0, 0
    for filename, subject, is_master in PLAN:
        path = SAMPLE_DIR / filename
        if not path.exists():
            failed += 1
            log.warning("sample missing: %s", path)
            continue
        try:
            with SessionLocal() as session:
                services.ingest_file(
                    session, path, filename, source="upload", subject=subject,
                    job_number_override=JOB_NUMBER, force_master=is_master,
                )
                session.commit()
            loaded += 1
        except services.DuplicateDocument:
            skipped += 1
        except Exception as exc:                      # noqa: BLE001
            failed += 1
            log.warning("sample %s failed: %s", filename, exc)

    return f"{loaded} loaded, {skipped} already present, {failed} failed"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    init_db()
    print(load())

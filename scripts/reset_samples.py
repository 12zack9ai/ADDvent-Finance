"""Empty the app of sample data, for the day the real documents start.

Two different lots of samples end up in a running install, and they have to be
removed in two different ways:

  * **The 269xxx band** written by `seed_samples.py`. Easy: the band is
    reserved, so the job number identifies it.

  * **Job 260000**, ingested from `samples/job-260000/` by `load_samples.py`.
    Not easy, and the reason this script exists rather than a one-line delete:
    **260000 is also the first real job number of 2026.** Deleting by number
    would eventually delete real work, and the day it did nobody would connect
    it to a script written months earlier.

    So the sample documents are found by the **sha256 of the files this
    repository ships**. A real document cannot collide with one unless
    somebody uploads the identical file, and the job itself is only removed
    if nothing else is left on it. If a real invoice has already been filed
    against 260000 the job stays, with the real invoice on it.

Run by hand:

    python scripts/reset_samples.py            # say what would go
    python scripts/reset_samples.py --confirm  # do it

or set RESET_SAMPLES=true, which runs it once on the next start. That is the
only way to reach a hosted install, where there is no shell.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, select                                # noqa: E402
from sqlalchemy.orm import Session                                   # noqa: E402

from app.db import SessionLocal, init_db                             # noqa: E402
from app.models import (                                             # noqa: E402
    Approval,
    CashReport,
    ChangeOrder,
    CheckRequest,
    Document,
    Extraction,
    Invoice,
    InvoiceLine,
    Job,
    Purchase,
    Quote,
    QuoteLine,
    Receipt,
)

from scripts import seed_samples                                     # noqa: E402

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "samples"


def shipped_hashes() -> set[str]:
    """The sha256 of every sample document this repository ships."""
    found: set[str] = set()
    if not SAMPLE_DIR.is_dir():
        return found
    for path in SAMPLE_DIR.rglob("*"):
        if path.is_file() and path.suffix.lower() in (".pdf", ".png", ".jpg", ".jpeg"):
            found.add(hashlib.sha256(path.read_bytes()).hexdigest())
    return found


def _sample_documents(session: Session) -> list[Document]:
    hashes = shipped_hashes()
    if not hashes:
        return []
    return list(session.scalars(
        select(Document).where(Document.sha256.in_(hashes))
    ).all())


def survey(session: Session) -> dict:
    """What is here, without touching any of it."""
    band = seed_samples.sample_jobs(session)
    docs = _sample_documents(session)
    return {
        "seeded_jobs": [j.job_number for j in band],
        "ingested_documents": [d.filename for d in docs],
        "jobs_touched": sorted({d.job.job_number for d in docs if d.job}),
    }


def wipe(session: Session) -> str:
    """Remove both lots. Returns a line for the log."""
    removed_band = seed_samples.remove(session)

    docs = _sample_documents(session)
    jobs = {d.job_id for d in docs if d.job_id}
    doc_ids = [d.id for d in docs]

    if doc_ids:
        invoices = list(session.scalars(
            select(Invoice).where(Invoice.document_id.in_(doc_ids))
        ).all())
        quotes = list(session.scalars(
            select(Quote).where(Quote.document_id.in_(doc_ids))
        ).all())

        # Anything else priced against a quote that is going has to let go of
        # it first, or the row is left pointing at a quote that is not there.
        for quote in quotes:
            for invoice in session.scalars(
                select(Invoice).where(Invoice.quote_id == quote.id)
            ).all():
                invoice.quote_id = None
        session.flush()

        for invoice in invoices:
            session.execute(delete(Approval).where(Approval.invoice_id == invoice.id))
            session.execute(delete(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id))
        for quote in quotes:
            session.execute(delete(QuoteLine).where(QuoteLine.quote_id == quote.id))
        session.flush()

        session.execute(delete(Invoice).where(Invoice.document_id.in_(doc_ids)))
        session.execute(delete(Quote).where(Quote.document_id.in_(doc_ids)))
        session.execute(delete(Purchase).where(Purchase.document_id.in_(doc_ids)))
        session.execute(delete(Extraction).where(Extraction.document_id.in_(doc_ids)))
        session.execute(delete(Document).where(Document.id.in_(doc_ids)))
        session.flush()

    # Only a job with nothing left on it. If a real invoice has already been
    # filed against 260000, the job stays and the real invoice stays with it.
    emptied = []
    for job_id in jobs:
        job = session.get(Job, job_id)
        if job is None:
            continue
        still_here = (job.invoices or job.quotes or job.check_requests
                      or job.purchases or job.change_orders or job.receipts
                      or job.documents)
        if still_here:
            continue
        emptied.append(job.job_number)
        session.delete(job)

    session.commit()
    return (f"{removed_band}; removed {len(doc_ids)} ingested sample "
            f"document(s)"
            + (f" and job {', '.join(emptied)}" if emptied else ""))


def main() -> int:
    init_db()
    with SessionLocal() as session:
        state = survey(session)
        print("Seeded sample jobs:", ", ".join(state["seeded_jobs"]) or "none")
        print("Ingested sample documents:",
              len(state["ingested_documents"]) or "none")
        for name in state["ingested_documents"]:
            print("   ", name)
        if state["jobs_touched"]:
            print("On job(s):", ", ".join(state["jobs_touched"]))

        if "--confirm" not in sys.argv:
            print("\nNothing removed. Re-run with --confirm to empty it.")
            return 0

        print("\n" + wipe(session))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

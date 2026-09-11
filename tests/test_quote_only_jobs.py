"""A job appears on the Invoices page as soon as its quote arrives.

Zack, 2026-09-11, after the 7:04 quote was filed to job 261500 and he could
not find it: "once a quote is uploaded the invoice section should be creating
a job for that." It sits in its own section below the folders, so a waiting
quote never buries a job with invoices to look at, and moves up into the
folders with its first invoice.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = Path(tempfile.mkdtemp(prefix="finance-quoteonly-"))
os.environ["DATA_DIR"] = str(_TMP)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["APP_PASSWORD"] = ""
os.environ["ANTHROPIC_API_KEY"] = "test"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Document, Invoice, Job, Quote  # noqa: E402

D = Decimal
client = TestClient(app)
SECTION = "Quotes waiting for their first invoice"


def setup_module(_module) -> None:
    # Never drop_all: every test module shares one engine (see test_compare).
    init_db()


def _doc(session, tag: str, kind: str) -> int:
    doc = Document(filename=f"{tag}.pdf", sha256=f"qo-{tag}".ljust(64, "0"),
                   stored_path=str(_TMP / f"{tag}.pdf"), kind=kind, status="ready")
    session.add(doc)
    session.flush()
    return doc.id


def _quoted_job(number: str, *, live: bool = True) -> None:
    with SessionLocal() as session:
        job = Job(job_number=number, name="Harbor Point")
        session.add(job)
        session.flush()
        session.add(Quote(job_id=job.id, document_id=_doc(session, f"{number}q", "quote"),
                          vendor="ABC Supply Co. Inc.", is_master=live, total=D("12400.00")))
        session.commit()


def _section(body: str) -> str:
    return body.split(SECTION, 1)[1] if SECTION in body else ""


def test_a_quote_on_its_own_puts_the_job_on_the_invoices_page():
    _quoted_job("265601")

    section = _section(client.get("/incoming").text)

    assert 'href="/job/265601"' in section
    assert "ABC Supply Co. Inc." in section and "no invoices yet" in section
    assert "$12,400" in section


def test_a_replaced_quote_alone_does_not_count():
    _quoted_job("265602", live=False)

    assert 'href="/job/265602"' not in client.get("/incoming").text


def test_the_first_invoice_moves_the_job_up_into_the_folders():
    _quoted_job("265603")
    with SessionLocal() as session:
        job = session.query(Job).filter(Job.job_number == "265603").one()
        session.add(Invoice(job_id=job.id, document_id=_doc(session, "265603i", "invoice"),
                            vendor="ABC Supply Co. Inc.", invoice_number="INV-265603",
                            invoice_date=date(2026, 9, 11), total=D("900.00")))
        session.commit()

    body = client.get("/incoming").text

    assert 'href="/job/265603"' in body
    assert 'href="/job/265603"' not in _section(body)

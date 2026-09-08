"""The search box in the header.

Zack: *"I don't think the searching for a job number works. I did it and it
brought me back to main page."* It did exactly that - the form submitted to
"/", which reads no query at all and re-renders the front door. The matching
was already written and sitting on /jobs, unreachable from the one box in the
app that looks like it should reach it.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = Path(tempfile.mkdtemp(prefix="finance-search-"))
os.environ.setdefault("DATA_DIR", str(_TMP))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP / 'test.db'}")
os.environ["APP_PASSWORD"] = ""
os.environ["ANTHROPIC_API_KEY"] = "test"

from fastapi.testclient import TestClient  # noqa: E402

from decimal import Decimal  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Job  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


def setup_module(_module) -> None:
    init_db()
    with SessionLocal() as session:
        if session.query(Job).filter_by(job_number="260901").first() is None:
            session.add(Job(job_number="260901", name="Winding Ridge"))
            session.commit()


def test_the_search_box_submits_somewhere_that_reads_the_query():
    """The whole bug in one assertion. A form posting to "/" cannot search."""
    page = client.get("/").text
    assert 'class="search" action="/jobs"' in page


def test_a_job_number_goes_straight_to_that_job():
    r = client.get("/jobs?q=260901", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/job/260901"


def test_a_job_number_with_a_hash_and_spaces_still_lands():
    r = client.get("/jobs?q=%20%23260901%20", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/job/260901"


def test_a_name_filters_the_list_rather_than_redirecting():
    page = client.get("/jobs?q=Winding")
    assert page.status_code == 200
    assert "260901" in page.text


def test_a_search_that_matches_nothing_says_so_instead_of_listing_everything():
    page = client.get("/jobs?q=zzzznotathing")
    assert page.status_code == 200
    assert "260901" not in page.text


# --- the sub side of a job -------------------------------------------------
# Zack, having searched a job number and landed on it: "What about if I wanted
# to look up a sub for that job? It's only opening the job vendor invoices."
# Sub invoices go through the identical pipeline and land in the same table, so
# without a mark on the row the two departments are indistinguishable there.

def _job_with_a_sub() -> str:
    from datetime import date

    from app.models import Document, Invoice, Quote

    number = "260902"
    with SessionLocal() as session:
        if session.query(Job).filter_by(job_number=number).first():
            return number
        job = Job(job_number=number, name="Sub test")
        session.add(job)
        session.flush()

        def doc(tag: str, kind: str) -> int:
            d = Document(job_id=job.id, filename=f"{tag}.pdf",
                         sha256=f"{number}{tag}".ljust(64, "0"),
                         stored_path=f"/tmp/{tag}.pdf", kind=kind, status="ready")
            session.add(d)
            session.flush()
            return d.id

        session.add(Quote(job_id=job.id, document_id=doc("sc", "quote"),
                          vendor="Falcon Roofing LLC", is_master=True,
                          is_subcontract=True, total=Decimal("120000.00")))
        session.add(Invoice(job_id=job.id, document_id=doc("si", "invoice"),
                            vendor="Falcon Roofing LLC", invoice_number="FAL-1",
                            invoice_date=date(2026, 9, 5),
                            total=Decimal("20000.00")))
        session.add(Invoice(job_id=job.id, document_id=doc("mi", "invoice"),
                            vendor="New Castle Building Products",
                            invoice_number="NC-1", invoice_date=date(2026, 9, 6),
                            total=Decimal("5000.00")))
        session.commit()
    return number


def test_a_subcontractors_invoice_is_marked_as_one_on_the_job_page():
    number = _job_with_a_sub()
    body = client.get(f"/job/{number}").text
    row = body.split("FAL-1", 1)[1][:400]
    assert ">sub<" in row
    other = body.split("NC-1", 1)[1][:400]
    assert ">sub<" not in other


def test_the_job_page_links_to_that_job_s_subcontractor_invoices():
    number = _job_with_a_sub()
    assert f'/sub-invoices?job={number}' in client.get(f"/job/{number}").text


def test_the_sub_queue_can_be_narrowed_to_one_job():
    number = _job_with_a_sub()
    body = client.get(f"/sub-invoices?job={number}").text
    assert "Falcon Roofing LLC" in body
    assert f"Job {number} only" in body


def test_a_job_with_no_subcontractor_says_so_rather_than_listing_everyone():
    body = client.get("/sub-invoices?job=260901").text
    assert "No subcontractor on job 260901" in body
    assert "Falcon Roofing LLC" not in body

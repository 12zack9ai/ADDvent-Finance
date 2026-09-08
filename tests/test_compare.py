"""The compare screen, and the claim it has to justify.

The job page can only assert that two invoices bill the same material. Zack's
words on the note that could not be checked: "If it actually is and I click
view it should put the PDF side-by-side." So the screen shows what was
matched, item by item and quantity by quantity, and both originals.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = Path(tempfile.mkdtemp(prefix="finance-compare-"))
os.environ["DATA_DIR"] = str(_TMP)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["APP_PASSWORD"] = ""
os.environ["ANTHROPIC_API_KEY"] = "test"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Document, Invoice, InvoiceLine, Job  # noqa: E402

D = Decimal
client = TestClient(app)


def setup_module(_module) -> None:
    # Never drop_all here. Every test module in this suite shares one engine,
    # configured from the environment at import time, so dropping the tables
    # takes the other modules' rows with it - which is exactly what happened
    # the first time this file was written.
    init_db()


def _pair(job_number: str, a_lines, b_lines, a_total, b_total):
    """One job, two invoices from one yard, with the lines given."""
    with SessionLocal() as session:
        job = Job(job_number=job_number, name="Compare test")
        session.add(job)
        session.flush()
        made = []
        for n, (total, lines) in enumerate(((a_total, a_lines), (b_total, b_lines))):
            doc = Document(job_id=job.id, filename=f"{job_number}-{n}.pdf",
                           sha256=f"{job_number}{n}".ljust(64, "0"),
                           stored_path=str(_TMP / f"{job_number}-{n}.pdf"),
                           kind="invoice", status="ready")
            session.add(doc)
            session.flush()
            inv = Invoice(job_id=job.id, document_id=doc.id,
                          vendor="New Castle Building Products",
                          invoice_number=f"INV-{job_number}-{n}",
                          invoice_date=date(2026, 9, 10 + n), total=D(total))
            session.add(inv)
            session.flush()
            for sku, qty, ext in lines:
                session.add(InvoiceLine(invoice_id=inv.id, sku=sku, description=sku,
                                        qty=D(qty), extended=D(ext)))
            made.append(inv.id)
        session.commit()
        return made


def test_the_compare_screen_shows_the_items_it_matched():
    a, b = _pair("260801",
                 [("SHG-TL-WW", "40", "12975.00")],
                 [("SHG-TL-WW", "40", "12975.00")],
                 "12975.00", "12975.00")
    page = client.get(f"/compare/{a}/{b}")
    assert page.status_code == 200
    body = page.text
    assert "SHG-TL-WW" in body
    assert "same quantity" in body          # the finding, shown not asserted
    assert f"/document/" in body            # both originals are on the page
    assert body.count("/document/") >= 2


def test_a_different_quantity_of_the_same_item_says_so():
    a, b = _pair("260802",
                 [("SHG-TL-WW", "40", "12975.00")],
                 [("SHG-TL-WW", "18", "5838.75")],
                 "12975.00", "5838.75")
    body = client.get(f"/compare/{a}/{b}").text
    assert "different quantity" in body


def test_comparing_two_invoices_from_different_jobs_is_refused():
    a, _ = _pair("260803", [("X", "1", "10.00")], [("X", "1", "10.00")],
                 "10.00", "10.00")
    _, b = _pair("260804", [("Y", "1", "10.00")], [("Y", "1", "10.00")],
                 "10.00", "10.00")
    r = client.get(f"/compare/{a}/{b}", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert "different" in r.headers["location"]   # url-encoded: "different+jobs"


def test_a_missing_invoice_does_not_500():
    r = client.get("/compare/999999/999998", follow_redirects=False)
    assert r.status_code in (302, 303, 307)

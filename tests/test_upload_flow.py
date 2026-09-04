from __future__ import annotations

import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The app reads configuration at import time, so point it at a throwaway
# database and file store BEFORE importing anything from app.*. Reloading the
# package per test instead would leave two sets of SQLAlchemy mappers alive.
_TMP = Path(tempfile.mkdtemp(prefix="finance-test-"))
os.environ["DATA_DIR"] = str(_TMP)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["APP_PASSWORD"] = ""            # no login gate in tests
os.environ["ANTHROPIC_API_KEY"] = "test"   # never used; extraction is stubbed

from fastapi.testclient import TestClient  # noqa: E402

from app import services  # noqa: E402
from app.db import Base, SessionLocal, engine, init_db  # noqa: E402
from app.extract import ExtractionError, ExtractionResult  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Document, Invoice, Job  # noqa: E402
from app.pdf import pdf_available  # noqa: E402

D = Decimal

QUOTE_PAYLOAD = {
    "doc_type": "quote",
    "vendor": "New Castle Building Products",
    "document_number": "07RM0002885432",
    "document_date": "2026-09-02",
    "due_date": "",
    "currency": "USD",
    "subtotal": "17182.90", "tax": "", "freight": "", "total": "17182.90",
    "job_number_hint": "63 winding ridge",
    "ship_to": "63 winding ridge, oakland, NJ 07456",
    "page_info": "1 of 2",
    "confidence_notes": "",
    "lines": [
        {"line_no": 1, "sku": "GAFT3PG", "description": "GAF TIMBERLINE HDZ PEWTER GRAY 3 BN/SQ",
         "qty": "80", "uom": "SQ", "unit_price": "120.50", "price_uom": "SQ", "extended": "9640.00"},
        {"line_no": 2, "sku": "GAFTP", "description": "GAF TIGER PAW UNDERLAYMENT 10 SQ/RL",
         "qty": "7", "uom": "RL", "unit_price": "187.00", "price_uom": "RL", "extended": "1309.00"},
        # The packaging trap: priced per box, counted in packs.
        {"line_no": 3, "sku": "BB-SFM558", "description": "STEP FLASHING ALUM PREBENT MF 50 EA/PK",
         "qty": "8", "uom": "PK", "unit_price": "155.00", "price_uom": "BX", "extended": "248.00"},
    ],
}

INVOICE_PAYLOAD = {
    "doc_type": "invoice",
    "vendor": "NEW CASTLE BLDG PRODUCTS",     # abbreviated on the invoice
    "document_number": "INV-551900",
    "document_date": "2026-09-10",
    "due_date": "2026-10-10",
    "currency": "USD",
    "subtotal": "6154.00", "tax": "", "freight": "", "total": "6154.00",
    "job_number_hint": "63 winding ridge",
    "ship_to": "63 winding ridge, oakland, NJ 07456",
    "page_info": "1 of 1",
    "confidence_notes": "",
    "lines": [
        # As quoted -> gold
        {"line_no": 1, "sku": "GAFT3PG", "description": "GAF TIMBERLINE HDZ PEWTER GRAY 3 BN/SQ",
         "qty": "40", "uom": "SQ", "unit_price": "120.50", "price_uom": "SQ", "extended": "4820.00"},
        # Above quote -> red. +$13/RL x 7
        {"line_no": 2, "sku": "GAFTP", "description": "GAF TIGER PAW UNDERLAYMENT 10 SQ/RL",
         "qty": "7", "uom": "RL", "unit_price": "200.00", "price_uom": "RL", "extended": "1400.00"},
        # Same real price, different packaging -> must NOT read as a price drop.
        {"line_no": 3, "sku": "BB-SFM558", "description": "STEP FLASHING ALUM PREBENT MF 50 EA/PK",
         "qty": "4", "uom": "PK", "unit_price": "31.00", "price_uom": "PK", "extended": "124.00"},
    ],
}


@pytest.fixture()
def client(monkeypatch):
    """A real app instance against an empty database, with extraction stubbed."""
    init_db()
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())

    queue: list[dict] = []

    def fake_extract(path, hint=""):
        if not queue:
            raise AssertionError("extract_document called with no payload queued")
        return ExtractionResult(payload=queue.pop(0), model="stub")

    monkeypatch.setattr(services, "extract_document", fake_extract)

    test_client = TestClient(app)
    test_client.queue = queue      # tests push the payload they expect next
    return test_client


@pytest.fixture()
def tmp_path():
    """Somewhere to write the sample PDFs for a single test."""
    return Path(tempfile.mkdtemp(prefix="finance-docs-"))


def _pdf(tmp_path: Path, name: str, marker: str) -> Path:
    """A tiny but structurally valid PDF, unique by content."""
    body = (
        f"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
        f"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        f"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        f"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
        f"% {marker}\n"
        f"trailer<</Root 1 0 R>>\n%%EOF\n"
    ).encode("latin-1")
    path = tmp_path / name
    path.write_bytes(body)
    return path


def upload(client, path: Path, payload: dict, **form):
    client.queue.append(payload)
    with path.open("rb") as fh:
        return client.post(
            "/upload",
            files={"files": (path.name, fh, "application/pdf")},
            data={"job_number": "", "note": "", **form},
            follow_redirects=False,
        )


# --- the happy path, end to end -------------------------------------------

def test_quote_then_invoice_all_the_way_through(client, tmp_path):
    quote_pdf = _pdf(tmp_path, "quote.pdf", "quote")
    invoice_pdf = _pdf(tmp_path, "invoice.pdf", "invoice")

    # 1. The quote is filed against the job number given on the form.
    resp = upload(client, quote_pdf, QUOTE_PAYLOAD, job_number="4417")
    assert resp.status_code == 303
    assert "/job/4417" in resp.headers["location"]

    session = SessionLocal()
    job = session.query(Job).filter_by(job_number="4417").one()
    master = job.master_quote
    assert master is not None
    assert master.vendor == "New Castle Building Products"
    assert len(master.lines) == 3
    assert master.page_info == "1 of 2"          # missing-page detection

    # The site address is recorded for the reader, and is NOT a job reference:
    # quotes often carry our own office address rather than the site.
    assert master.po_reference == "63 winding ridge"

    # 2. The invoice, from the same vendor under an abbreviated name.
    resp = upload(client, invoice_pdf, INVOICE_PAYLOAD, job_number="4417")
    assert resp.status_code == 303

    session = SessionLocal()
    invoice = session.query(Invoice).filter_by(invoice_number="INV-551900").one()

    # It found the master quote despite the different trading name.
    assert invoice.quote_id == master.id

    verdicts = {line.sku: line.verdict for line in invoice.lines}
    assert verdicts["GAFT3PG"] == "match"        # gold
    assert verdicts["GAFTP"] == "over"           # red
    assert verdicts["BB-SFM558"] == "match"      # packaging, NOT a price drop

    assert invoice.overbilled_amount == D("91.00")   # $13/RL x 7
    assert invoice.lines_over == 1
    assert invoice.lines_match == 2
    assert invoice.lines_unmatched == 0


def test_marked_up_page_and_pdf_both_render(client, tmp_path):
    upload(client, _pdf(tmp_path, "q.pdf", "q"), QUOTE_PAYLOAD, job_number="4417")
    upload(client, _pdf(tmp_path, "i.pdf", "i"), INVOICE_PAYLOAD, job_number="4417")

    invoice_id = SessionLocal().query(Invoice).one().id

    page = client.get(f"/invoice/{invoice_id}")
    assert page.status_code == 200
    assert "quoted $187.00" in page.text        # the quoted price beside the red one
    assert "NEW CASTLE BLDG PRODUCTS" in page.text

    if pdf_available():
        pdf = client.get(f"/invoice/{invoice_id}/pdf")
        assert pdf.status_code == 200
        assert pdf.content.startswith(b"%PDF")


def test_the_same_file_twice_is_refused(client, tmp_path):
    path = _pdf(tmp_path, "dup.pdf", "same-bytes")
    upload(client, path, QUOTE_PAYLOAD, job_number="4417")
    resp = upload(client, path, QUOTE_PAYLOAD, job_number="4417")
    assert "already+received" in resp.headers["location"].replace("%20", "+")


def test_document_with_no_job_number_waits_in_the_inbox(client, tmp_path):
    payload = dict(QUOTE_PAYLOAD, job_number_hint="", ship_to="")
    resp = upload(client, _pdf(tmp_path, "orphan.pdf", "orphan"), payload)
    assert "/upload" in resp.headers["location"]

    doc = SessionLocal().query(Document).one()
    assert doc.status == "needs_job"
    assert doc.job_id is None
    assert client.get("/inbox").status_code == 200


def test_assigning_from_the_inbox_files_it_without_re_reading(client, tmp_path):
    payload = dict(QUOTE_PAYLOAD, job_number_hint="", ship_to="")
    upload(client, _pdf(tmp_path, "orphan.pdf", "orphan2"), payload)

    doc_id = SessionLocal().query(Document).one().id

    # No payload queued: re-extraction here would raise IndexError.
    resp = client.post(f"/document/{doc_id}/assign",
                       data={"job_number": "5150"}, follow_redirects=False)
    assert resp.status_code == 303

    job = SessionLocal().query(Job).filter_by(job_number="5150").one()
    assert job.master_quote is not None


def test_extraction_failure_is_reported_not_swallowed(client, tmp_path, monkeypatch):
    def boom(path, hint=""):
        raise ExtractionError("Claude API error (401): invalid x-api-key")

    monkeypatch.setattr(services, "extract_document", boom)

    path = _pdf(tmp_path, "bad.pdf", "bad")
    with path.open("rb") as fh:
        resp = client.post("/upload", files={"files": ("bad.pdf", fh, "application/pdf")},
                           data={"job_number": "4417"}, follow_redirects=False)

    assert resp.status_code == 303
    assert "invalid" in resp.headers["location"]      # the real reason reaches the user

    assert SessionLocal().query(Document).one().status == "error"


def test_a_new_master_quote_recompares_existing_invoices(client, tmp_path):
    upload(client, _pdf(tmp_path, "q1.pdf", "q1"), QUOTE_PAYLOAD, job_number="4417")
    upload(client, _pdf(tmp_path, "i1.pdf", "i1"), INVOICE_PAYLOAD, job_number="4417")

    assert SessionLocal().query(Invoice).one().overbilled_amount == D("91.00")

    # A revised quote where the underlayment price was renegotiated upward.
    revised = {**QUOTE_PAYLOAD, "document_number": "07RM0002885433"}
    revised["lines"] = [dict(line) for line in QUOTE_PAYLOAD["lines"]]
    revised["lines"][1]["unit_price"] = "200.00"

    upload(client, _pdf(tmp_path, "q2.pdf", "q2"), revised,
           job_number="4417", force_master="1")

    # The existing invoice is no longer over quote, without being re-uploaded.
    assert SessionLocal().query(Invoice).one().overbilled_amount == D("0")


# --- the front door ------------------------------------------------------

def test_the_home_page_offers_both_programmes(client):
    """One place to choose between invoice checking and cash flow."""
    body = client.get("/").text
    assert "Invoice checking" in body
    assert "Cash flow" in body
    assert 'href="/jobs"' in body
    assert 'href="/cashflow"' in body


def test_the_jobs_list_still_works_at_its_new_address(client):
    assert client.get("/jobs").status_code == 200


def test_the_home_page_works_before_anything_has_been_loaded(client):
    """A fresh install has no jobs and no reports. It must still render."""
    assert client.get("/").status_code == 200


def test_the_apps_own_log_lines_are_visible():
    """The mailbox poller reports each cycle at INFO. Uvicorn leaves the root
    logger alone, so without explicit configuration the one line saying whether
    mail is being read never reaches the server log."""
    import logging
    from app.main import _configure_logging

    _configure_logging()
    assert logging.getLogger("app.scheduler").isEnabledFor(logging.INFO)
    assert logging.getLogger().handlers


# --- incoming: arrival order, not urgency order --------------------------

def test_incoming_lists_invoices_newest_first(client, tmp_path):
    """The point of the page: an invoice that landed an hour ago must not sit
    below a three-week-old dispute."""
    from datetime import datetime, timedelta
    from app.models import Invoice

    session = SessionLocal()
    job = session.query(Job).first() or Job(job_number="260000")
    if job.id is None:
        session.add(job)
        session.flush()
    now = datetime.utcnow()

    def arrived(tag, when):
        doc = Document(filename=f"{tag}.pdf", sha256=f"sha-{tag}",
                       stored_path=f"/tmp/{tag}.pdf", kind="invoice",
                       source="email", status="matched", received_at=when)
        session.add(doc)
        session.flush()
        return doc

    session.add_all([
        Invoice(job_id=job.id, document_id=arrived("old", now - timedelta(days=21)).id,
                vendor="Old Vendor", invoice_number="OLD-1", total=D("100.00"),
                approval_status="held", created_at=now - timedelta(days=21)),
        Invoice(job_id=job.id, document_id=arrived("new", now - timedelta(hours=1)).id,
                vendor="New Vendor", invoice_number="NEW-1", total=D("200.00"),
                approval_status="pending_review", created_at=now - timedelta(hours=1)),
    ])
    session.commit()

    body = client.get("/incoming").text
    assert body.index("NEW-1") < body.index("OLD-1")
    session.close()


def test_incoming_can_be_narrowed_to_what_still_needs_review(client):
    assert client.get("/incoming?show=unreviewed").status_code == 200
    assert client.get("/incoming?show=over").status_code == 200


def test_incoming_renders_with_nothing_in_it(client):
    """A fresh install opens this page before anything has arrived."""
    assert client.get("/incoming").status_code == 200


def test_the_dashboard_offers_the_incoming_queue(client):
    body = client.get("/").text
    assert 'href="/incoming"' in body
    assert "just came in" in body


def test_relative_times_read_as_a_person_would_say_them():
    from datetime import datetime, timedelta
    from app.main import f_ago

    now = datetime.utcnow()
    assert f_ago(now) == "just now"
    assert f_ago(now - timedelta(minutes=12)) == "12 min ago"
    assert f_ago(now - timedelta(hours=1)) == "1 hour ago"
    assert f_ago(now - timedelta(hours=5)) == "5 hours ago"
    assert f_ago(now - timedelta(days=2)) == "2 days ago"
    assert f_ago(None) == "—"
    # Beyond a week the exact date is more use than "31 days ago".
    assert f_ago(now - timedelta(days=31)) == (now - timedelta(days=31)).strftime("%d %b")

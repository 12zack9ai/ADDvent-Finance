from __future__ import annotations

import json
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

from app import services, trust  # noqa: E402
from app.db import Base, SessionLocal, engine, init_db  # noqa: E402
from app.extract import ExtractionError, ExtractionResult  # noqa: E402
from app.main import app  # noqa: E402
from app.models import ChangeOrder, Document, Invoice, Job  # noqa: E402
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
    # The extended amount moves with the unit price, because on a real revised
    # quote it does - and a line whose own arithmetic does not hold is read as
    # a packaging difference rather than a price, which is a different test.
    revised = {**QUOTE_PAYLOAD, "document_number": "07RM0002885433"}
    revised["lines"] = [dict(line) for line in QUOTE_PAYLOAD["lines"]]
    revised["lines"][1]["unit_price"] = "200.00"
    revised["lines"][1]["extended"] = "1400.00"

    upload(client, _pdf(tmp_path, "q2.pdf", "q2"), revised,
           job_number="4417", force_master="1")

    session = SessionLocal()
    invoice = session.query(Invoice).one()
    # Re-priced against the new quote, without being re-uploaded.
    assert invoice.overbilled_amount == D("0")
    # And genuinely re-compared, not quietly emptied. This assertion is the
    # point: the version of this test that only checked the variance passed
    # for months while the job was silently losing its quote and zeroing every
    # comparison on it.
    assert invoice.quote_id is not None
    assert invoice.lines_unmatched == 0
    assert invoice.lines_match == 3
    session.close()


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


# --- every page actually renders -----------------------------------------

def test_every_page_renders(client):
    """A template that parses can still fail at render, and one that does not
    parse fails only when somebody opens it.

    The job page shipped broken this way: removing a panel left an orphaned
    {% endfor %}, every test still passed because nothing here had ever opened
    the page, and the first time anyone found out was a 500 in the browser.
    """
    for path in ("/", "/jobs", "/incoming", "/incoming?show=unreviewed",
                 "/incoming?show=over", "/approvals", "/cashflow", "/vendors",
                 "/inbox", "/upload", "/login"):
        response = client.get(path, follow_redirects=True)
        assert response.status_code == 200, f"{path} returned {response.status_code}"
        assert "Internal Server Error" not in response.text, path


def test_a_job_page_with_real_content_renders(client, tmp_path):
    """Not just the empty case: the job page only breaks once it has quotes,
    invoices, receipts and change orders to loop over."""
    quote_pdf = _pdf(tmp_path, "q.pdf", "quote")
    invoice_pdf = _pdf(tmp_path, "i.pdf", "invoice")
    upload(client, quote_pdf, QUOTE_PAYLOAD, job_number="260000")
    upload(client, invoice_pdf, INVOICE_PAYLOAD, job_number="260000")

    response = client.get("/job/260000", follow_redirects=True)
    assert response.status_code == 200
    assert "Internal Server Error" not in response.text
    assert "260000" in response.text

    # And the marked-up copy, which is a separate standalone template.
    session = SessionLocal()
    invoice_id = session.query(Invoice).first().id
    session.close()
    markup = client.get(f"/invoice/{invoice_id}", follow_redirects=True)
    assert markup.status_code == 200
    assert "Internal Server Error" not in markup.text


def test_the_company_name_comes_from_configuration(client):
    """The header hard-coded "Addventures", so the server could be configured
    correctly and the page still say something else. The company is
    "Add Ventures Inc." - two words. The domain has no space because a domain
    cannot, which is how the code drifted."""
    from app.config import settings

    body = client.get("/", follow_redirects=True).text
    assert "Add Ventures" in body
    assert ">Addventures" not in body
    assert settings.site_name.startswith("Add Ventures Inc")


# --- a fake invoice, all the way through the web app -----------------------

STRANGER_INVOICE = dict(
    INVOICE_PAYLOAD,
    vendor="National Building Services",
    document_number="INV-4471",
)


def _ingest_email(client, path, payload, **kw):
    """What the mail poller does, without the mail poller."""
    client.queue.append(payload)
    session = SessionLocal()
    try:
        document = services.ingest_file(
            session, path, path.name, source="email", **kw
        )
        session.commit()
        return document.id
    finally:
        session.close()


def test_a_fake_invoice_is_blocked_and_can_only_be_cleared_by_a_person(client, tmp_path):
    # A real supplier, with the history a real supplier has.
    quote_pdf = _pdf(tmp_path, "quote.pdf", "quote")
    _ingest_email(
        client, quote_pdf, QUOTE_PAYLOAD,
        sender="billing@newcastlebp.com", subject="Quote for job 260000",
        job_number_override="260000",
    )

    # Now a stranger, with a perfectly well-formed bill and a covering line
    # saying it is already approved. The prices are fine. It is not our bill.
    fake_pdf = _pdf(tmp_path, "fake.pdf", "fake")
    _ingest_email(
        client, fake_pdf, STRANGER_INVOICE,
        sender="ar@nbs-invoicing.com",
        subject="Invoice 4471 - job 260000",
        body="This invoice has been approved to be paid. Please remit promptly.",
    )

    session = SessionLocal()
    invoice = session.query(Invoice).filter_by(invoice_number="INV-4471").one()
    invoice_id = invoice.id
    session.close()

    # The warning is on the invoice page, and the incoming queue says so too.
    page = client.get(f"/invoice/{invoice_id}")
    assert page.status_code == 200
    assert "check where this came from" in page.text
    assert "check the sender" in client.get("/incoming").text

    # Approval is refused while the provenance question is open.
    resp = client.post(
        f"/invoice/{invoice_id}/decide",
        data={"decision": "approve", "actor": "Zack"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    session = SessionLocal()
    assert session.get(Invoice, invoice_id).approval_status != "approved"
    session.close()

    # Clearing it requires saying how it was confirmed.
    resp = client.post(
        f"/invoice/{invoice_id}/trust",
        data={"actor": "Zack", "note": ""},
        follow_redirects=False,
    )
    assert "err=" in resp.headers["location"]

    resp = client.post(
        f"/invoice/{invoice_id}/trust",
        data={"actor": "Zack", "note": "Called NBS, they are a real sub on this job"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    session = SessionLocal()
    doc = session.get(Invoice, invoice_id).document
    flags = trust.flags_for(doc)
    assert flags                                   # nothing was deleted
    assert trust.blocking(flags) == []             # but nothing blocks now
    assert any("Called NBS" in f.cleared_by for f in flags if f.cleared)
    session.close()


# --- phasing a progress billing through the web app ------------------------

def _report(**kw):
    """A saved 13-week report with nothing in it but an opening balance."""
    from app.models import CashReport
    session = SessionLocal()
    try:
        report = CashReport(
            as_of="2026-09-01", weeks=13, opening_balance=Decimal("100000"),
            **kw,
        )
        session.add(report)
        session.commit()
        return report.id
    finally:
        session.close()


def test_a_draw_is_phased_by_hand_and_lands_where_the_person_said(client):
    report_id = _report()

    resp = client.post(f"/cashflow/{report_id}/draw", data={
        "customer": "Daul Gardens Condo Assn",
        "job_number": "260000",
        "amount": "280,420.00",
        "retainage_pct": "10",
        "assigned_week": "3",
        "collect_weeks": "4",
        "memo": "Buildings 1-3, requisition 2",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert "err=" not in resp.headers["location"]

    page = client.get(f"/cashflow/{report_id}")
    assert page.status_code == 200
    assert "Daul Gardens Condo Assn" in page.text
    # Billed in week 3, four weeks to collect, so the money is week 7.
    assert "bill week 3 \u2192 in week 7" in page.text
    # 10% held back, so $252,378 is what the forecast counts.
    assert "252,378.00" in page.text
    assert "28,042.00" in page.text          # the retainage, shown separately

    # And it survives a round trip through storage.
    from app.models import CashReport
    session = SessionLocal()
    rows = json.loads(session.get(CashReport, report_id).receivables_json)
    session.close()
    assert len(rows) == 1
    assert rows[0]["retainage_pct"] == "10"
    assert rows[0]["collect_weeks"] == 4
    assert rows[0]["source"] == "progress billing"


def test_a_draw_with_no_amount_is_refused(client):
    report_id = _report()
    resp = client.post(f"/cashflow/{report_id}/draw", data={
        "customer": "Daul", "amount": "0", "assigned_week": "1",
    }, follow_redirects=False)
    assert "err=" in resp.headers["location"]


def test_a_draw_can_be_removed_again(client):
    report_id = _report()
    client.post(f"/cashflow/{report_id}/draw", data={
        "customer": "Daul", "amount": "50000", "assigned_week": "2",
        "retainage_pct": "0", "collect_weeks": "1",
    }, follow_redirects=False)

    from app.models import CashReport
    session = SessionLocal()
    ref = json.loads(session.get(CashReport, report_id).receivables_json)[0]["reference"]
    session.close()

    resp = client.post(f"/cashflow/{report_id}/draw/{ref}/remove", follow_redirects=False)
    assert resp.status_code == 303
    assert "err=" not in resp.headers["location"]

    session = SessionLocal()
    assert json.loads(session.get(CashReport, report_id).receivables_json) == []
    session.close()


def test_money_from_quickbooks_cannot_be_deleted_off_a_report(client):
    """A draw is ours to remove. A row off an A/R aging export belongs to
    QuickBooks, and quietly dropping it would make the two disagree."""
    report_id = _report(receivables_json=json.dumps([{
        "customer": "Bergen Point", "amount": "9000.00", "reference": "INV-3",
        "source": "A/R aging export", "invoice_date": "2026-08-01",
    }]))

    resp = client.post(f"/cashflow/{report_id}/draw/INV-3/remove", follow_redirects=False)
    assert "err=" in resp.headers["location"]

    from app.models import CashReport
    session = SessionLocal()
    assert len(json.loads(session.get(CashReport, report_id).receivables_json)) == 1
    session.close()


def test_the_report_pdf_still_builds_with_draws_on_it(client):
    report_id = _report()
    client.post(f"/cashflow/{report_id}/draw", data={
        "customer": "Daul", "amount": "50000", "assigned_week": "2",
        "retainage_pct": "10", "collect_weeks": "3",
    }, follow_redirects=False)

    resp = client.get(f"/cashflow/{report_id}/pdf")
    assert resp.status_code == 200
    assert resp.content[:4] == b"%PDF"


def test_run_rates_alone_are_enough_to_start_a_report(client):
    """So a forecast can be built and then phased by hand, without waiting on
    an export from anybody."""
    resp = client.post("/cashflow/generate", data={
        "opening_balance": "250000", "rate_payroll": "42000", "rate_rent": "6500",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert "/cashflow/" in resp.headers["location"]
    assert "err=" not in resp.headers["location"]

    report_id = int(resp.headers["location"].rsplit("/", 1)[-1].split("?")[0])
    page = client.get(f"/cashflow/{report_id}")
    assert "Progress billings" in page.text


def test_a_report_with_nothing_at_all_is_still_refused(client):
    resp = client.post("/cashflow/generate", data={"opening_balance": "250000"},
                       follow_redirects=False)
    assert "err=" in resp.headers["location"]


# --- the job scorecard, and the system checking its own work ---------------

def test_the_job_page_adds_the_job_up(client, tmp_path):
    upload(client, _pdf(tmp_path, "q.pdf", "q"), QUOTE_PAYLOAD, job_number="260000")
    upload(client, _pdf(tmp_path, "i.pdf", "i"), INVOICE_PAYLOAD, job_number="260000")

    page = client.get("/job/260000")
    assert page.status_code == 200
    assert "Quoted" in page.text
    assert "$17,182.90" in page.text        # the quote, lump sum
    assert "$6,154.00" in page.text         # billed to date
    assert "Caught by checking" in page.text
    assert "$91.00" in page.text            # the overbilling the line check found
    assert "Not on the quote" in page.text


def test_a_corrected_invoice_that_never_replaced_the_original_is_called_out(client, tmp_path):
    """The failure a per-invoice check cannot see: we send an invoice back, the
    vendor reissues it under a new number, and nobody rejects the first."""
    upload(client, _pdf(tmp_path, "q.pdf", "q"), QUOTE_PAYLOAD, job_number="260000")
    upload(client, _pdf(tmp_path, "i1.pdf", "i1"), INVOICE_PAYLOAD, job_number="260000")

    corrected = dict(INVOICE_PAYLOAD, document_number="INV-552140", total="6063.00")
    corrected["lines"] = [dict(ln) for ln in INVOICE_PAYLOAD["lines"]]
    corrected["lines"][1]["unit_price"] = "187.00"      # the price we disputed
    corrected["lines"][1]["extended"] = "1309.00"
    upload(client, _pdf(tmp_path, "i2.pdf", "i2"), corrected, job_number="260000")

    page = client.get("/job/260000")
    # The job is still inside its quote, so this is not an overrun - and the
    # panel has to say the right thing about which of the two it is.
    assert "Two invoices look like the same material" in page.text
    assert "This job does not add up" not in page.text
    assert "Same material billed on two invoices" in page.text
    assert "INV-551900" in page.text and "INV-552140" in page.text

    # Rejecting the superseded one clears it, and clears the totals with it.
    session = SessionLocal()
    first = session.query(Invoice).filter_by(invoice_number="INV-551900").one().id
    session.close()
    client.post(f"/invoice/{first}/decide",
                data={"decision": "reject", "actor": "Zack", "note": "Replaced by 552140"},
                follow_redirects=False)

    page = client.get("/job/260000")
    assert "Two invoices look like the same material" not in page.text
    assert "Same material billed on two invoices" not in page.text


# --- a change order emailed in, read, and signed ---------------------------

CHANGE_ORDER_PAYLOAD = {
    "doc_type": "change_order",
    "vendor": "New Castle Building Products",
    "document_number": "CO-7",
    "document_date": "2026-09-12",
    "due_date": "",
    "currency": "USD",
    "subtotal": "", "tax": "", "freight": "", "total": "150.00",
    "job_number_hint": "",
    "ship_to": "",
    "page_info": "",
    "confidence_notes": "",
    "lines": [
        {"line_no": 1, "sku": "GAFTP", "description": "Price increase on Tiger Paw per mill notice",
         "qty": "", "uom": "", "unit_price": "", "price_uom": "", "extended": "150.00"},
    ],
}


def test_a_change_order_arrives_as_a_document_and_waits_for_a_person(client, tmp_path):
    upload(client, _pdf(tmp_path, "q.pdf", "q-co"), QUOTE_PAYLOAD, job_number="260000")
    upload(client, _pdf(tmp_path, "i.pdf", "i-co"), INVOICE_PAYLOAD, job_number="260000")

    session = SessionLocal()
    invoice_id = session.query(Invoice).filter_by(invoice_number="INV-551900").one().id
    session.close()

    # $91 over quote, which is inside tolerance, so tighten it to force a hold.
    from app.config import settings
    old_abs, old_pct = settings.tolerance_abs, settings.tolerance_pct
    settings.tolerance_abs, settings.tolerance_pct = "1", 0.0
    try:
        session = SessionLocal()
        from app.approval import apply_routing
        apply_routing(session.get(Invoice, invoice_id))
        session.commit()
        session.close()

        page = client.get(f"/invoice/{invoice_id}")
        assert "no change order on file" in page.text

        # The vendor emails the change order in. It is read, not believed.
        upload(client, _pdf(tmp_path, "co.pdf", "co-release"), CHANGE_ORDER_PAYLOAD,
               job_number="260000")

        session = SessionLocal()
        co = session.query(ChangeOrder).one()
        co_id, co_status = co.id, co.status
        session.close()
        assert co_status == "proposed"

        page = client.get("/job/260000")
        assert "Waiting on you" in page.text
        assert "authorises nothing" in page.text

        # It has NOT closed the gap - but the reviewer is told it is here.
        page = client.get(f"/invoice/{invoice_id}")
        assert "has not been approved" in page.text
        assert "no change order on file" not in page.text

        session = SessionLocal()
        assert session.get(Invoice, invoice_id).approval_status == "held"
        session.close()

        # A person signs it, and that is what releases the invoice.
        resp = client.post(f"/change-order/{co_id}/decide",
                           data={"decision": "approve", "actor": "Zack"},
                           follow_redirects=False)
        assert resp.status_code == 303
        assert "released" in resp.headers["location"]

        session = SessionLocal()
        assert session.get(Invoice, invoice_id).approval_status != "held"
        assert session.get(ChangeOrder, co_id).approved_by == "Zack"
        session.close()
    finally:
        settings.tolerance_abs, settings.tolerance_pct = old_abs, old_pct


def test_a_change_order_can_be_refused(client, tmp_path):
    upload(client, _pdf(tmp_path, "q.pdf", "q-refuse"), QUOTE_PAYLOAD, job_number="260000")
    upload(client, _pdf(tmp_path, "co.pdf", "co-refuse"), CHANGE_ORDER_PAYLOAD,
           job_number="260000")

    session = SessionLocal()
    co_id = session.query(ChangeOrder).one().id
    session.close()

    client.post(f"/change-order/{co_id}/decide",
                data={"decision": "reject", "actor": "Zack", "note": "Never agreed to this"},
                follow_redirects=False)

    session = SessionLocal()
    co = session.get(ChangeOrder, co_id)
    assert co.status == "rejected"
    assert co.decided_note == "Never agreed to this"
    session.close()

    # And it cannot be decided a second time.
    resp = client.post(f"/change-order/{co_id}/decide",
                       data={"decision": "approve", "actor": "Someone Else"},
                       follow_redirects=False)
    assert "err=" in resp.headers["location"]


def test_a_change_order_typed_in_by_hand_is_approved_by_the_typing(client, tmp_path):
    upload(client, _pdf(tmp_path, "q.pdf", "q-typed"), QUOTE_PAYLOAD, job_number="260000")

    client.post("/job/260000/change-order", data={
        "vendor": "New Castle Building Products", "amount": "1500.00",
        "number": "CO-1", "approved_by": "Zack", "description": "Hidden rot",
    }, follow_redirects=False)

    session = SessionLocal()
    co = session.query(ChangeOrder).one()
    assert co.status == "approved"
    assert co.approved_by == "Zack"
    assert co.decided_on is not None
    session.close()


# --- two scopes, one job, one supplier ------------------------------------

SKYLIGHT_QUOTE = {
    **QUOTE_PAYLOAD,
    "document_number": "07RM0002885999",
    "subtotal": "8400.00", "total": "8400.00",
    "lines": [
        {"line_no": 1, "sku": "VELUX-FS-M08", "description": "VELUX FS M08 FIXED SKYLIGHT",
         "qty": "6", "uom": "EA", "unit_price": "1200.00", "price_uom": "EA",
         "extended": "7200.00"},
        {"line_no": 2, "sku": "VELUX-EDL-M08", "description": "VELUX EDL M08 FLASHING KIT",
         "qty": "6", "uom": "EA", "unit_price": "200.00", "price_uom": "EA",
         "extended": "1200.00"},
    ],
}

# One delivery drawing from both quotes, which is the whole point.
MIXED_INVOICE = {
    **INVOICE_PAYLOAD,
    "document_number": "INV-560000",
    "subtotal": "7220.00", "total": "7220.00",
    "lines": [
        {"line_no": 1, "sku": "GAFT3PG", "description": "GAF TIMBERLINE HDZ PEWTER GRAY 3 BN/SQ",
         "qty": "10", "uom": "SQ", "unit_price": "120.50", "price_uom": "SQ",
         "extended": "1205.00"},
        {"line_no": 2, "sku": "VELUX-FS-M08", "description": "VELUX FS M08 FIXED SKYLIGHT",
         "qty": "5", "uom": "EA", "unit_price": "1200.00", "price_uom": "EA",
         "extended": "6000.00"},
    ],
}


def test_a_second_quote_for_a_different_scope_stands_alongside_the_first(client, tmp_path):
    """The roof job with skylights. Same supply house, two quotes, both live —
    and an invoice can carry lines from either."""
    upload(client, _pdf(tmp_path, "q1.pdf", "roof"), QUOTE_PAYLOAD, job_number="260000")
    upload(client, _pdf(tmp_path, "q2.pdf", "sky"), SKYLIGHT_QUOTE, job_number="260000")

    session = SessionLocal()
    job = session.query(Job).filter_by(job_number="260000").one()
    masters = job.masters
    assert len(masters) == 2, "the skylight quote must not stand down the roofing quote"
    assert {q.quote_number for q in masters} == {"07RM0002885432", "07RM0002885999"}
    session.close()

    # The scorecard adds both.
    page = client.get("/job/260000")
    assert "$25,582.90" in page.text          # 17,182.90 + 8,400.00


def test_one_invoice_is_priced_against_both_quotes(client, tmp_path):
    upload(client, _pdf(tmp_path, "q1.pdf", "roof2"), QUOTE_PAYLOAD, job_number="260000")
    upload(client, _pdf(tmp_path, "q2.pdf", "sky2"), SKYLIGHT_QUOTE, job_number="260000")
    upload(client, _pdf(tmp_path, "i.pdf", "mixed"), MIXED_INVOICE, job_number="260000")

    session = SessionLocal()
    invoice = session.query(Invoice).filter_by(invoice_number="INV-560000").one()

    # Both lines found their price. Before this, whichever quote arrived second
    # was invisible and half the invoice read as unquoted material.
    assert invoice.lines_match == 2
    assert invoice.lines_unmatched == 0
    assert invoice.overbilled_amount == D("0")
    session.close()


def test_a_revision_of_one_scope_leaves_the_other_alone(client, tmp_path):
    upload(client, _pdf(tmp_path, "q1.pdf", "roof3"), QUOTE_PAYLOAD, job_number="260000")
    upload(client, _pdf(tmp_path, "q2.pdf", "sky3"), SKYLIGHT_QUOTE, job_number="260000")

    revised_roof = {**QUOTE_PAYLOAD, "document_number": "07RM0002885440"}
    revised_roof["lines"] = [dict(line) for line in QUOTE_PAYLOAD["lines"]]
    revised_roof["lines"][0]["unit_price"] = "125.00"
    revised_roof["lines"][0]["extended"] = "10000.00"
    upload(client, _pdf(tmp_path, "q3.pdf", "roof3b"), revised_roof, job_number="260000")

    session = SessionLocal()
    job = session.query(Job).filter_by(job_number="260000").one()
    live = {q.quote_number for q in job.masters}
    # The revision replaced the roofing quote and left the skylights standing.
    assert live == {"07RM0002885440", "07RM0002885999"}
    session.close()


def test_the_scopes_are_told_apart_by_what_is_on_them_not_by_who_sent_them(client, tmp_path):
    """No human said which of these replaces what. The overlap decides."""
    upload(client, _pdf(tmp_path, "q1.pdf", "ov1"), QUOTE_PAYLOAD, job_number="260000")

    # Same items, new prices, and nobody wrote the word "revised" anywhere.
    same_items = {**QUOTE_PAYLOAD, "document_number": "07RM0002885441"}
    same_items["lines"] = [dict(line) for line in QUOTE_PAYLOAD["lines"]]
    same_items["lines"][0]["unit_price"] = "131.00"
    same_items["lines"][0]["extended"] = "10480.00"
    upload(client, _pdf(tmp_path, "q2.pdf", "ov2"), same_items, job_number="260000")

    session = SessionLocal()
    job = session.query(Job).filter_by(job_number="260000").one()
    assert len(job.masters) == 1, "same items at new prices is a revision, not a scope"
    assert job.masters[0].quote_number == "07RM0002885441"
    session.close()


def test_a_dumpster_quote_never_stands_down_the_roofing_quote(client, tmp_path):
    """Already true, and it has to stay true."""
    upload(client, _pdf(tmp_path, "q1.pdf", "d1"), QUOTE_PAYLOAD, job_number="260000")
    dumpster = {
        **QUOTE_PAYLOAD, "vendor": "Bergen Dumpster Service",
        "document_number": "BD-1", "total": "1800.00",
        "lines": [{"line_no": 1, "sku": "30YD", "description": "30 YARD DUMPSTER",
                   "qty": "3", "uom": "EA", "unit_price": "600.00", "price_uom": "EA",
                   "extended": "1800.00"}],
    }
    upload(client, _pdf(tmp_path, "q2.pdf", "d2"), dumpster, job_number="260000")

    session = SessionLocal()
    job = session.query(Job).filter_by(job_number="260000").one()
    assert len(job.masters) == 2
    session.close()


def test_an_invoice_with_no_quote_asks_the_project_manager_once(client, tmp_path, monkeypatch):
    """End to end: invoice arrives, job has no quote, JobNimbus names the PM,
    one email goes out - and the second invoice on that job sends nothing."""
    from app import jobnimbus, mail_send, services as svc
    from app.config import settings

    sent = []
    monkeypatch.setattr(settings, "ask_for_quote", True)
    monkeypatch.setattr(settings, "can_send_mail", lambda: True)
    monkeypatch.setattr(settings, "reply_domains", lambda: {"addventuresinc.com"})
    monkeypatch.setattr(settings, "smtp_settings",
                        lambda: ("h", 587, "u", "p", "aifinance@addventuresinc.com"))
    monkeypatch.setattr(mail_send, "send", lambda msg: sent.append(msg))
    monkeypatch.setattr(svc.jobnimbus, "find_job", lambda number: jobnimbus.Assignment(
        job_number=number, job_name="Daul Gardens", person_name="Mike Reilly",
        email="mreilly@addventuresinc.com",
    ))

    upload(client, _pdf(tmp_path, "i1.pdf", "pm1"), INVOICE_PAYLOAD, job_number="260000")

    assert len(sent) == 1
    assert sent[0]["To"] == "mreilly@addventuresinc.com"
    assert "260000" in sent[0]["Subject"]

    session = SessionLocal()
    job = session.query(Job).filter_by(job_number="260000").one()
    assert job.quote_chase_sent_at is not None
    assert job.quote_chase_to == "mreilly@addventuresinc.com"
    session.close()

    second = {**INVOICE_PAYLOAD, "document_number": "INV-551901"}
    upload(client, _pdf(tmp_path, "i2.pdf", "pm2"), second, job_number="260000")
    assert len(sent) == 1, "a second invoice on the same job must not ask again"


def test_a_job_that_already_has_a_quote_asks_nobody(client, tmp_path, monkeypatch):
    from app import mail_send, services as svc
    from app.config import settings

    sent = []
    monkeypatch.setattr(settings, "ask_for_quote", True)
    monkeypatch.setattr(settings, "can_send_mail", lambda: True)
    monkeypatch.setattr(mail_send, "send", lambda msg: sent.append(msg))
    monkeypatch.setattr(svc.jobnimbus, "find_job",
                        lambda number: pytest.fail("JobNimbus must not be called"))

    upload(client, _pdf(tmp_path, "q.pdf", "pmq"), QUOTE_PAYLOAD, job_number="260000")
    upload(client, _pdf(tmp_path, "i.pdf", "pmi"), INVOICE_PAYLOAD, job_number="260000")
    assert sent == []


def test_jobnimbus_failing_never_stops_an_invoice_filing(client, tmp_path, monkeypatch):
    """An invoice that could not be priced is a problem. An invoice that failed
    to file because JobNimbus was slow would be a much bigger one."""
    from app import services as svc
    from app.config import settings

    monkeypatch.setattr(settings, "ask_for_quote", True)
    monkeypatch.setattr(svc.jobnimbus, "find_job",
                        lambda number: (_ for _ in ()).throw(RuntimeError("boom")))

    resp = upload(client, _pdf(tmp_path, "i.pdf", "pmboom"), INVOICE_PAYLOAD,
                  job_number="260000")
    assert resp.status_code == 303

    session = SessionLocal()
    assert session.query(Invoice).filter_by(invoice_number="INV-551900").one() is not None
    session.close()


def test_ordering_more_material_at_the_quoted_price_raises_nothing(client, tmp_path):
    """Zack: 'ordering more material... our price is gonna stay with the same
    quoted price.' A quote prices material; it does not cap how much of it the
    roof needs. This job bills nearly double the quote and nothing is wrong."""
    upload(client, _pdf(tmp_path, "q.pdf", "more-q"), QUOTE_PAYLOAD, job_number="260000")

    # Same items, same unit prices, far more of them.
    heavy = {**INVOICE_PAYLOAD, "document_number": "INV-570000",
             "subtotal": "30125.00", "total": "30125.00"}
    heavy["lines"] = [{
        "line_no": 1, "sku": "GAFT3PG",
        "description": "GAF TIMBERLINE HDZ PEWTER GRAY 3 BN/SQ",
        "qty": "250", "uom": "SQ", "unit_price": "120.50", "price_uom": "SQ",
        "extended": "30125.00",
    }]
    upload(client, _pdf(tmp_path, "i.pdf", "more-i"), heavy, job_number="260000")

    session = SessionLocal()
    invoice = session.query(Invoice).filter_by(invoice_number="INV-570000").one()
    assert invoice.overbilled_amount == D("0")
    assert invoice.lines_match == 1
    session.close()

    page = client.get("/job/260000")
    assert "$30,125.00" in page.text
    assert "more material than quoted, all at quoted prices" in page.text
    # And no alarm, even though billed is nearly double quoted.
    assert "Money on this job with no quoted price behind it" not in page.text


def test_but_unquoted_material_on_the_same_job_is_raised(client, tmp_path):
    upload(client, _pdf(tmp_path, "q.pdf", "unq-q"), QUOTE_PAYLOAD, job_number="260000")

    mixed = {**INVOICE_PAYLOAD, "document_number": "INV-570001",
             "subtotal": "18125.00", "total": "18125.00"}
    mixed["lines"] = [
        {"line_no": 1, "sku": "GAFT3PG",
         "description": "GAF TIMBERLINE HDZ PEWTER GRAY 3 BN/SQ",
         "qty": "100", "uom": "SQ", "unit_price": "120.50", "price_uom": "SQ",
         "extended": "12050.00"},
        {"line_no": 2, "sku": "VELUX-FS-M08", "description": "VELUX FS M08 SKYLIGHT",
         "qty": "5", "uom": "EA", "unit_price": "1215.00", "price_uom": "EA",
         "extended": "6075.00"},
    ]
    upload(client, _pdf(tmp_path, "i.pdf", "unq-i"), mixed, job_number="260000")

    page = client.get("/job/260000")
    assert "Money on this job with no quoted price behind it" in page.text
    assert "$6,075.00" in page.text
    assert "material that appears on no quote" in page.text


# --- the button: a person asking for the quote -----------------------------

@pytest.fixture()
def can_email(monkeypatch):
    from app import mail_send
    from app.config import settings
    sent = []
    monkeypatch.setattr(settings, "can_send_mail", lambda: True)
    monkeypatch.setattr(settings, "reply_domains", lambda: {"addventuresinc.com"})
    monkeypatch.setattr(settings, "smtp_settings",
                        lambda: ("h", 587, "u", "p", "aifinance@addventuresinc.com"))
    monkeypatch.setattr(mail_send, "send", lambda msg: sent.append(msg))
    return sent


def _bare_job(number="260000"):
    session = SessionLocal()
    try:
        job = Job(job_number=number)
        session.add(job)
        session.commit()
        return job.id
    finally:
        session.close()


def test_the_button_asks_the_address_a_person_types(client, can_email):
    _bare_job()
    resp = client.post("/job/260000/ask-for-quote", follow_redirects=False, data={
        "actor": "Jena", "to_address": "mreilly@addventuresinc.com",
    })
    assert resp.status_code == 303
    assert "err=" not in resp.headers["location"]
    assert len(can_email) == 1
    assert can_email[0]["To"] == "mreilly@addventuresinc.com"


def test_the_button_works_without_jobnimbus_or_the_automatic_flag(client, can_email, monkeypatch):
    """A person clicking a button is not the thing ASK_FOR_QUOTE holds back —
    that flag exists so a deploy never starts emailing people by surprise."""
    from app import services as svc
    from app.config import settings
    monkeypatch.setattr(settings, "ask_for_quote", False)
    monkeypatch.setattr(settings, "jobnimbus_api_key", "")
    _bare_job()

    client.post("/job/260000/ask-for-quote", follow_redirects=False,
                data={"actor": "Jena", "to_address": "mreilly@addventuresinc.com"})
    assert len(can_email) == 1


def test_it_can_only_be_clicked_once_a_day(client, can_email):
    """So somebody on a roof is not getting the same email all afternoon."""
    _bare_job()
    data = {"actor": "Jena", "to_address": "mreilly@addventuresinc.com"}

    first = client.post("/job/260000/ask-for-quote", data=data, follow_redirects=False)
    assert "err=" not in first.headers["location"]

    second = client.post("/job/260000/ask-for-quote", data=data, follow_redirects=False)
    assert "err=" in second.headers["location"]
    assert "24+hours" in second.headers["location"]
    assert len(can_email) == 1

    # And the page says so instead of offering the button again.
    page = client.get("/job/260000")
    assert "Already asked in the last 24 hours" in page.text
    assert "Ask for the quote" not in page.text

    # A day later it is allowed again.
    from datetime import timedelta
    session = SessionLocal()
    job = session.query(Job).filter_by(job_number="260000").one()
    job.quote_chase_sent_at = job.quote_chase_sent_at - timedelta(hours=25)
    session.commit()
    session.close()

    third = client.post("/job/260000/ask-for-quote", data=data, follow_redirects=False)
    assert "err=" not in third.headers["location"]
    assert len(can_email) == 2

    session = SessionLocal()
    assert session.query(Job).filter_by(job_number="260000").one().quote_chase_count == 2
    session.close()


def test_the_button_will_not_email_outside_the_company(client, can_email):
    _bare_job()
    resp = client.post("/job/260000/ask-for-quote", follow_redirects=False, data={
        "actor": "Jena", "to_address": "sales@newcastlebp.com",
    })
    assert "err=" in resp.headers["location"]
    assert can_email == []


def test_a_job_that_already_has_a_quote_offers_no_button(client, tmp_path, can_email):
    upload(client, _pdf(tmp_path, "q.pdf", "btn-q"), QUOTE_PAYLOAD, job_number="260000")

    page = client.get("/job/260000")
    assert "Ask for the quote" not in page.text

    resp = client.post("/job/260000/ask-for-quote", follow_redirects=False,
                       data={"actor": "Jena", "to_address": "pm@addventuresinc.com"})
    assert "err=" in resp.headers["location"]
    assert can_email == []


def test_with_no_address_and_no_assignee_it_says_so_rather_than_failing(client, can_email, monkeypatch):
    from app import services as svc
    monkeypatch.setattr(svc.jobnimbus, "find_job", lambda number: None)
    _bare_job()

    resp = client.post("/job/260000/ask-for-quote", follow_redirects=False,
                       data={"actor": "Jena", "to_address": ""})
    assert "err=" in resp.headers["location"]
    assert "type+the+address" in resp.headers["location"]
    assert can_email == []


def test_a_manual_ask_also_satisfies_the_automatic_one(client, can_email, monkeypatch):
    """They share one counter, deliberately. If a person chased this job this
    morning, an invoice arriving this afternoon must not produce a second email
    saying the same thing to the same person."""
    from app import jobnimbus, services as svc
    from app.config import settings
    monkeypatch.setattr(settings, "ask_for_quote", True)
    monkeypatch.setattr(svc.jobnimbus, "find_job", lambda number: jobnimbus.Assignment(
        job_number=number, person_name="Mike Reilly",
        email="mreilly@addventuresinc.com"))
    _bare_job()

    client.post("/job/260000/ask-for-quote", follow_redirects=False,
                data={"actor": "Jena", "to_address": "mreilly@addventuresinc.com"})
    assert len(can_email) == 1

    session = SessionLocal()
    job = session.query(Job).filter_by(job_number="260000").one()
    assert job.quote_chase_count == 1
    session.close()

    # An invoice now lands on the job. No second email.
    upload(client, _pdf(Path(tempfile.mkdtemp()), "i.pdf", "manual-auto"),
           INVOICE_PAYLOAD, job_number="260000")
    assert len(can_email) == 1


# --- "this is an updated quote" arriving by email --------------------------

def _ingest_email_doc(client, path, payload, **kw):
    client.queue.append(payload)
    session = SessionLocal()
    try:
        document = services.ingest_file(session, path, path.name, source="email", **kw)
        session.commit()
        return document.id
    finally:
        session.close()


def test_an_email_saying_updated_quote_supersedes_the_one_on_the_job(client, tmp_path):
    """Zack: 'if it's an updated quote in the email, says this is an updated
    quote for job number whatever, I'll take that as the superseder of the two.'

    Note the line items here are the SAME items at new prices, so the overlap
    rule would have caught it anyway. The next test is the one that proves the
    sentence is doing the work."""
    _ingest_email_doc(client, _pdf(tmp_path, "q1.pdf", "upd1"), QUOTE_PAYLOAD,
                      sender="sales@newcastlebp.com", subject="Quote for job 260000")

    revised = {**QUOTE_PAYLOAD, "document_number": "07RM0002885450"}
    revised["lines"] = [dict(line) for line in QUOTE_PAYLOAD["lines"]]
    revised["lines"][0]["unit_price"] = "131.00"
    revised["lines"][0]["extended"] = "10480.00"
    _ingest_email_doc(client, _pdf(tmp_path, "q2.pdf", "upd2"), revised,
                      sender="sales@newcastlebp.com",
                      subject="This is an updated quote for job 260000")

    session = SessionLocal()
    job = session.query(Job).filter_by(job_number="260000").one()
    assert len(job.masters) == 1
    assert job.masters[0].quote_number == "07RM0002885450"
    superseded = [q for q in job.quotes if not q.is_master]
    assert len(superseded) == 1
    assert superseded[0].superseded_at is not None
    session.close()


def test_the_sentence_supersedes_even_when_the_items_are_different(client, tmp_path):
    """The overlap rule alone would keep both of these, because they share no
    items. The vendor said outright that one replaces the other, and that is
    believed — this is what Zack actually asked for."""
    _ingest_email_doc(client, _pdf(tmp_path, "q1.pdf", "say1"), QUOTE_PAYLOAD,
                      sender="sales@newcastlebp.com", subject="Quote for job 260000")

    different = {**QUOTE_PAYLOAD, "document_number": "07RM0002885451"}
    different["lines"] = [
        {"line_no": 1, "sku": "CERTAIN-LM", "description": "CERTAINTEED LANDMARK WEATHERED WOOD",
         "qty": "80", "uom": "SQ", "unit_price": "118.00", "price_uom": "SQ",
         "extended": "9440.00"},
    ]
    _ingest_email_doc(client, _pdf(tmp_path, "q2.pdf", "say2"), different,
                      sender="sales@newcastlebp.com",
                      subject="Revised quote for job 260000 - switched to CertainTeed")

    session = SessionLocal()
    job = session.query(Job).filter_by(job_number="260000").one()
    assert len(job.masters) == 1
    assert job.masters[0].quote_number == "07RM0002885451"
    session.close()


def test_a_new_scope_by_email_still_stands_alongside(client, tmp_path):
    """And the sentence has to be absent for that to happen. 'New quote for the
    skylights' is a second scope, not a replacement."""
    _ingest_email_doc(client, _pdf(tmp_path, "q1.pdf", "sc1"), QUOTE_PAYLOAD,
                      sender="sales@newcastlebp.com", subject="Quote for job 260000")
    _ingest_email_doc(client, _pdf(tmp_path, "q2.pdf", "sc2"), SKYLIGHT_QUOTE,
                      sender="sales@newcastlebp.com",
                      subject="New quote for job 260000 - skylights")

    session = SessionLocal()
    job = session.query(Job).filter_by(job_number="260000").one()
    assert len(job.masters) == 2
    session.close()


def test_without_a_jobnimbus_key_the_address_is_required(client, can_email, monkeypatch):
    """Offering 'leave blank and we'll look them up' with no key configured is
    offering a button that cannot work."""
    from app.config import settings
    monkeypatch.setattr(settings, "jobnimbus_api_key", "")
    _bare_job()

    page = client.get("/job/260000")
    assert "leave blank to use whoever JobNimbus has assigned" not in page.text
    assert "an @addventuresinc.com address" in page.text
    assert 'name="to_address"' in page.text and "required" in page.text


def test_with_a_key_the_lookup_is_offered(client, can_email, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "jobnimbus_api_key", "test-key")
    _bare_job()

    page = client.get("/job/260000")
    assert "leave blank to use whoever JobNimbus has assigned" in page.text


# --- check requests: not only for subs -------------------------------------

def _sub_job(number="260000", contract="120000.00", vendor="Reilly Roofing LLC"):
    from app.models import Quote
    session = SessionLocal()
    try:
        job = Job(job_number=number)
        session.add(job)
        session.flush()
        session.add(Quote(job_id=job.id, document_id=1, vendor=vendor,
                          is_master=True, is_subcontract=True, total=D(contract)))
        session.commit()
        return job.id
    finally:
        session.close()


def test_a_permit_can_be_requested_without_any_contract(client):
    """The thing the old model could not express: a check that is not a draw
    against a subcontract. It still belongs to a job, because everything here
    does."""
    from app.models import CheckRequest
    _sub_job()

    resp = client.post("/checks/new", follow_redirects=False, data={
        "payee": "Township of Oakland", "amount": "450.00",
        "purpose": "permit", "job_number": "260000",
        "reference": "Permit 2026-1184",
    })
    assert resp.status_code == 303
    assert "err=" not in resp.headers["location"]

    session = SessionLocal()
    req = session.query(CheckRequest).one()
    assert req.purpose == "permit"
    assert req.job.job_number == "260000"
    assert req.payee == "Township of Oakland"
    session.close()

    page = client.get("/checks")
    assert "Township of Oakland" in page.text
    assert "Permit" in page.text


def test_a_check_without_a_job_number_is_refused(client):
    """Job numbers are how this business is organised, and a permit with no job
    would land in no costing report."""
    resp = client.post("/checks/new", follow_redirects=False, data={
        "payee": "Township of Oakland", "amount": "450", "purpose": "permit",
        "job_number": "",
    })
    assert "err=" in resp.headers["location"]
    assert "job+number" in resp.headers["location"]

    unknown = client.post("/checks/new", follow_redirects=False, data={
        "payee": "Township of Oakland", "amount": "450", "job_number": "269999",
    })
    assert "err=" in unknown.headers["location"]


def test_the_queue_puts_the_longest_wait_first(client):
    _sub_job("260000")
    _sub_job("260004", vendor="Coastal Electric Co")
    client.post("/checks/new", follow_redirects=False, data={
        "payee": "Township of Oakland", "amount": "450", "job_number": "260000",
        "purpose": "permit", "requested_on": "2026-09-04"})
    client.post("/checks/new", follow_redirects=False, data={
        "payee": "Bergen County Clerk", "amount": "90", "job_number": "260004",
        "purpose": "fee", "requested_on": "2026-07-01"})

    page = client.get("/checks")
    assert page.text.index("Bergen County Clerk") < page.text.index("Township of Oakland")
    assert "Longest wait" in page.text


def test_a_check_request_is_approved_then_paid_and_leaves_the_queue(client):
    from app.models import CheckRequest
    _sub_job()
    client.post("/checks/new", follow_redirects=False,
                data={"payee": "Township of Oakland", "amount": "450",
                      "purpose": "permit", "job_number": "260000"})
    session = SessionLocal()
    req_id = session.query(CheckRequest).one().id
    session.close()

    client.post(f"/check/{req_id}/decide", follow_redirects=False,
                data={"decision": "approve", "actor": "Zack"})
    assert "Nobody is waiting on a check." in client.get("/checks").text

    client.post(f"/check/{req_id}/decide", follow_redirects=False,
                data={"decision": "paid", "actor": "Jena"})
    session = SessionLocal()
    req = session.get(CheckRequest, req_id)
    assert req.status == "paid" and req.paid_at is not None
    session.close()


def test_a_check_with_no_amount_or_payee_is_refused(client):
    _sub_job()
    assert "err=" in client.post("/checks/new", follow_redirects=False, data={
        "payee": "Township", "amount": "0", "job_number": "260000"}).headers["location"]
    assert "err=" in client.post("/checks/new", follow_redirects=False, data={
        "payee": "", "amount": "450", "job_number": "260000"}).headers["location"]


def test_the_nav_shows_how_many_people_are_waiting(client):
    _sub_job()
    assert "Checks</a>" in client.get("/jobs").text
    client.post("/checks/new", follow_redirects=False,
                data={"payee": "Township of Oakland", "amount": "450",
                      "job_number": "260000"})
    assert "Checks (1)" in client.get("/jobs").text


# --- subcontractor invoices: exactly like vendor invoices ------------------

def test_a_subs_invoice_is_priced_against_their_contract(client, tmp_path):
    """Zack: 'the subcontractor invoice should work exactly like the vendor
    invoicing.' It goes through the same pipeline and matches the same way."""
    from app.models import Quote
    upload(client, _pdf(tmp_path, "q.pdf", "subq"), QUOTE_PAYLOAD, job_number="260000")

    session = SessionLocal()
    quote_id = session.query(Quote).one().id
    session.close()
    client.post("/job/260000/subcontract", follow_redirects=False,
                data={"quote_id": quote_id, "is_subcontract": "1"})

    upload(client, _pdf(tmp_path, "i.pdf", "subi"), INVOICE_PAYLOAD, job_number="260000")

    session = SessionLocal()
    invoice = session.query(Invoice).one()
    # Still matched line by line, exactly as a supplier's invoice would be.
    assert invoice.quote_id == quote_id
    assert invoice.lines_match == 2
    assert invoice.overbilled_amount == D("91.00")
    session.close()

    page = client.get("/sub-invoices")
    assert "NEW CASTLE BLDG PRODUCTS" in page.text or "New Castle" in page.text
    assert "INV-551900" in page.text


def test_a_subs_invoice_past_the_contract_is_held(client, tmp_path):
    """The one thing a contract adds that a quote does not: a ceiling."""
    _sub_job(contract="5000.00")
    big = {**INVOICE_PAYLOAD, "vendor": "Reilly Roofing LLC",
           "document_number": "REQ-9", "total": "9000.00"}
    upload(client, _pdf(tmp_path, "i.pdf", "ceil"), big, job_number="260000")

    session = SessionLocal()
    invoice = session.query(Invoice).filter_by(invoice_number="REQ-9").one()
    invoice_id = invoice.id
    session.close()

    page = client.get(f"/invoice/{invoice_id}")
    assert "past it" in page.text
    assert "Say what the extra work was" in page.text

    resp = client.post(f"/invoice/{invoice_id}/decide", follow_redirects=False,
                       data={"decision": "approve", "actor": "Zack"})
    session = SessionLocal()
    assert session.get(Invoice, invoice_id).approval_status != "approved"
    session.close()


def test_a_material_supplier_gets_no_ceiling(client, tmp_path):
    """A quote prices material and does not cap how much of it a roof needs."""
    upload(client, _pdf(tmp_path, "q.pdf", "nocap-q"), QUOTE_PAYLOAD, job_number="260000")
    heavy = {**INVOICE_PAYLOAD, "document_number": "INV-BIG", "total": "90000.00"}
    heavy["lines"] = [{
        "line_no": 1, "sku": "GAFT3PG",
        "description": "GAF TIMBERLINE HDZ PEWTER GRAY 3 BN/SQ",
        "qty": "746", "uom": "SQ", "unit_price": "120.50", "price_uom": "SQ",
        "extended": "89893.00"}]
    upload(client, _pdf(tmp_path, "i.pdf", "nocap-i"), heavy, job_number="260000")

    session = SessionLocal()
    invoice = session.query(Invoice).filter_by(invoice_number="INV-BIG").one()
    session.close()
    assert "past it" not in client.get(f"/invoice/{invoice.id}").text


# --- the job costing report through the web app ---------------------------

def test_the_costing_report_pulls_invoices_subs_and_permits_together(client, tmp_path):
    from app.models import CheckRequest
    _sub_job()

    upload(client, _pdf(tmp_path, "q.pdf", "cost-q"), QUOTE_PAYLOAD, job_number="260000")
    upload(client, _pdf(tmp_path, "i.pdf", "cost-i"), INVOICE_PAYLOAD, job_number="260000")

    session = SessionLocal()
    invoice = session.query(Invoice).one()
    invoice.approval_status = "paid"
    session.commit()
    session.close()

    client.post("/checks/new", follow_redirects=False, data={
        "payee": "Township of Oakland", "amount": "450",
        "purpose": "permit", "job_number": "260000"})
    session = SessionLocal()
    req_id = session.query(CheckRequest).one().id
    session.close()
    client.post(f"/check/{req_id}/decide", follow_redirects=False,
                data={"decision": "approve", "actor": "Zack"})

    page = client.get("/job/260000/costing")
    assert page.status_code == 200
    assert "$6,154.00" in page.text        # the vendor invoice
    assert "$450.00" in page.text          # the permit
    assert "$6,604.00" in page.text        # added up
    assert "Permits, deposits and other checks" in page.text
    # No price entered, so no margin is claimed.
    assert "what we charged the customer has not been entered" in page.text


def test_entering_the_price_produces_the_margin(client):
    _sub_job()
    resp = client.post("/job/260000/costing", follow_redirects=False, data={
        "contract_amount": "185,000.00", "labour_cost": "0",
        "costing_note": "Fully subbed",
    })
    assert resp.status_code == 303

    page = client.get("/job/260000/costing")
    assert "$185,000.00" in page.text
    assert "Fully subbed" in page.text

    session = SessionLocal()
    job = session.query(Job).filter_by(job_number="260000").one()
    assert job.contract_amount == D("185000.00")
    assert job.labour_cost == D("0")
    session.close()


def test_clearing_the_labour_figure_is_not_the_same_as_zero(client):
    _sub_job()
    client.post("/job/260000/costing", follow_redirects=False,
                data={"contract_amount": "100000", "labour_cost": "5000"})
    client.post("/job/260000/costing", follow_redirects=False,
                data={"contract_amount": "100000", "labour_cost": ""})

    session = SessionLocal()
    job = session.query(Job).filter_by(job_number="260000").one()
    assert job.labour_cost is None
    session.close()

    assert "our own labour has not been entered" in client.get("/job/260000/costing").text


def test_the_job_page_links_to_the_costing_report(client):
    _sub_job()
    assert "/job/260000/costing" in client.get("/job/260000").text


# --- the front door shows all four programmes ------------------------------

def test_the_home_page_offers_all_five_programmes(client):
    body = client.get("/").text
    assert "Five programmes" in body
    for heading in ("Invoice checking", "Subcontractor invoices",
                    "Check requests", "13-week cash flow", "Job costing"):
        assert heading in body
    for href in ('href="/incoming"', 'href="/sub-invoices"', 'href="/checks"',
                 'href="/cashflow"', 'href="/jobs"'):
        assert href in body


def test_the_checks_card_leads_with_the_longest_wait(client):
    """Somebody who has been waiting five weeks is the reason to open this
    card. "3 requests" is not."""
    _sub_job()
    client.post("/checks/new", follow_redirects=False, data={
        "payee": "Township of Oakland", "amount": "450", "job_number": "260000",
        "purpose": "permit", "requested_on": "2026-07-01"})

    body = client.get("/").text
    assert "longest wait" in body
    assert "Township of Oakland" in body
    assert "permit" in body


def test_the_checks_card_is_quiet_when_nobody_is_waiting(client):
    assert "Nobody is waiting." in client.get("/").text


def test_the_costing_card_counts_the_jobs_with_no_price_entered(client):
    """Saying how many cannot show a margin is more useful than averaging the
    ones that can."""
    _sub_job("260000")
    _sub_job("260001", vendor="Bravo Electric")
    client.post("/job/260000/costing", follow_redirects=False,
                data={"contract_amount": "185000", "labour_cost": "0"})

    body = client.get("/").text
    assert "$185,000.00" in body
    assert "1 job with no price entered" in body


def test_the_costing_card_does_not_show_a_margin_on_a_fraction_of_the_cost(client, tmp_path):
    """The front door must not carry the overstatement the costing report
    itself exists to prevent."""
    _sub_job()
    upload(client, _pdf(tmp_path, "i.pdf", "front-i"), INVOICE_PAYLOAD, job_number="260000")
    client.post("/checks/new", follow_redirects=False, data={
        "payee": "Township of Oakland", "amount": "60000",
        "purpose": "deposit", "job_number": "260000"})
    client.post("/job/260000/costing", follow_redirects=False,
                data={"contract_amount": "185000", "labour_cost": "0"})

    body = client.get("/").text
    assert "still waiting on a decision" in body
    assert "the first figure is the optimistic one" in body
    assert "if everything clears" in body
    # $185,000 less $6,154 of invoice and $60,000 of deposit.
    assert "$118,846.00" in body


def test_the_costing_card_says_so_when_nothing_is_priced(client):
    _sub_job()
    assert "No job has a price entered yet" in client.get("/").text

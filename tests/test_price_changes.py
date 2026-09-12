"""Items not on the quote are held to the price they were first billed at.

Zack: "invoice one comes in but didnt have a 3" pipe boot on it but it was $65.
invoice two comes in with the pipe boot at $66 dollars..... thats a problem...
need an easy way to see that"
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = Path(tempfile.mkdtemp(prefix="finance-pricewatch-"))
os.environ["DATA_DIR"] = str(_TMP)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["APP_PASSWORD"] = ""
os.environ["ANTHROPIC_API_KEY"] = "test"

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from app import db as appdb  # noqa: E402
from app import pricewatch  # noqa: E402
from app.approval import route  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    APPROVAL_REJECTED,
    VERDICT_OVER,
    Base,
    Document,
    Invoice,
    InvoiceLine,
    Job,
    Quote,
    QuoteLine,
)
from app.services import recompare_job  # noqa: E402

D = Decimal
client = TestClient(app)

YARD = "New Castle Building Products"
BOOT = '3" pipe boot'


def setup_module(_module) -> None:
    # Never drop_all: every test module shares one engine (see test_compare).
    init_db()


def _job(session, number: str) -> Job:
    job = Job(job_number=number, name="Price watch test")
    session.add(job)
    session.flush()
    return job


def _doc(session, job: Job, tag: str, kind: str) -> Document:
    doc = Document(job_id=job.id, filename=f"{tag}.pdf", sha256=tag.ljust(64, "0"),
                   stored_path=str(_TMP / f"{tag}.pdf"), kind=kind, status="ready")
    session.add(doc)
    session.flush()
    return doc


def _invoice(session, job: Job, n: int, lines, *, vendor: str = YARD, day: int = 0) -> Invoice:
    """An invoice dated 2026-09-<day or n>, lines as (description, qty, price)."""
    doc = _doc(session, job, f"pw{job.job_number}i{n}", "invoice")
    inv = Invoice(job_id=job.id, document_id=doc.id, vendor=vendor,
                  invoice_number=f"INV-{job.job_number}-{n}",
                  invoice_date=date(2026, 9, day or n),
                  total=sum((D(q) * D(p) for _, q, p in lines), D(0)))
    session.add(inv)
    session.flush()
    for i, (desc, qty, price) in enumerate(lines, 1):
        session.add(InvoiceLine(invoice_id=inv.id, line_no=i, description=desc,
                                qty=D(qty), uom="EA", unit_price=D(price),
                                extended=D(qty) * D(price)))
    session.flush()
    session.refresh(inv)
    return inv


def _boot(invoice: Invoice) -> InvoiceLine:
    return next(line for line in invoice.lines if line.description == BOOT)


def test_the_pipe_boot():
    with SessionLocal() as s:
        job = _job(s, "265101")
        first = _invoice(s, job, 1, [(BOOT, 2, "65"), ("Shingles", 10, "40")])
        second = _invoice(s, job, 2, [(BOOT, 1, "66")])
        recompare_job(s, job)

        assert first.lines_price_changed == 0
        assert second.lines_price_changed == 1
        assert _boot(second).first_billed_price == D("65")
        assert _boot(second).first_billed_invoice_id == first.id


def test_the_same_price_again_is_left_alone():
    with SessionLocal() as s:
        job = _job(s, "265102")
        _invoice(s, job, 1, [(BOOT, 2, "65")])
        second = _invoice(s, job, 2, [(BOOT, 1, "65.00")])
        recompare_job(s, job)

        assert second.lines_price_changed == 0
        assert _boot(second).first_billed_price is None


def test_cheaper_is_flagged_too():
    with SessionLocal() as s:
        job = _job(s, "265103")
        _invoice(s, job, 1, [(BOOT, 2, "65")])
        second = _invoice(s, job, 2, [(BOOT, 1, "60")])
        recompare_job(s, job)

        assert _boot(second).first_billed_price == D("65")


def test_the_first_price_stands():
    """Invoice three is held to invoice one, not to invoice two's new price."""
    with SessionLocal() as s:
        job = _job(s, "265104")
        _invoice(s, job, 1, [(BOOT, 2, "65")])
        second = _invoice(s, job, 2, [(BOOT, 1, "66")])
        third = _invoice(s, job, 3, [(BOOT, 1, "66")])
        recompare_job(s, job)

        assert second.lines_price_changed == 1
        assert third.lines_price_changed == 1
        assert _boot(third).first_billed_price == D("65")


def test_another_supplier_is_not_held_to_it():
    with SessionLocal() as s:
        job = _job(s, "265105")
        _invoice(s, job, 1, [(BOOT, 2, "65")])
        other = _invoice(s, job, 2, [(BOOT, 1, "66")], vendor="ABC Supply Co.")
        recompare_job(s, job)

        assert other.lines_price_changed == 0


def test_an_older_invoice_arriving_late_becomes_the_first():
    with SessionLocal() as s:
        job = _job(s, "265106")
        later = _invoice(s, job, 1, [(BOOT, 1, "66")], day=5)
        recompare_job(s, job)
        assert later.lines_price_changed == 0      # the only one, so the first

        earlier = _invoice(s, job, 2, [(BOOT, 2, "65")], day=2)
        recompare_job(s, job)

        assert earlier.lines_price_changed == 0
        assert _boot(later).first_billed_price == D("65")


def test_a_rejected_invoice_sets_no_price():
    with SessionLocal() as s:
        job = _job(s, "265107")
        first = _invoice(s, job, 1, [(BOOT, 2, "65")])
        second = _invoice(s, job, 2, [(BOOT, 1, "66")])
        recompare_job(s, job)
        assert second.lines_price_changed == 1

        first.approval_status = APPROVAL_REJECTED
        pricewatch.check_job(s, job)

        assert second.lines_price_changed == 0
        assert _boot(second).first_billed_price is None


def test_once_a_quote_prices_it_the_quote_decides():
    with SessionLocal() as s:
        job = _job(s, "265108")
        _invoice(s, job, 1, [(BOOT, 2, "65")])
        second = _invoice(s, job, 2, [(BOOT, 1, "66")])
        recompare_job(s, job)
        assert second.lines_price_changed == 1

        doc = _doc(s, job, f"pw{job.job_number}q", "quote")
        quote = Quote(job_id=job.id, document_id=doc.id, is_master=True, vendor=YARD)
        s.add(quote)
        s.flush()
        s.add(QuoteLine(quote_id=quote.id, line_no=1, description=BOOT, qty=D(1),
                        uom="EA", unit_price=D("65"), extended=D("65")))
        s.flush()
        recompare_job(s, job)

        assert second.lines_price_changed == 0
        assert _boot(second).verdict == VERDICT_OVER


def test_the_invoice_says_why_it_wants_a_look():
    with SessionLocal() as s:
        job = _job(s, "265109")
        _invoice(s, job, 1, [(BOOT, 2, "65")])
        second = _invoice(s, job, 2, [(BOOT, 1, "66")])
        recompare_job(s, job)

        reasons = route(second).reasons
        assert any("different price than the first invoice" in r for r in reasons)


def test_it_shows_on_the_invoice_the_job_and_the_invoices_page():
    with SessionLocal() as s:
        job = _job(s, "265110")
        _invoice(s, job, 1, [(BOOT, 2, "65")])
        second = _invoice(s, job, 2, [(BOOT, 1, "66")])
        recompare_job(s, job)
        s.commit()
        invoice_id = second.id

    page = client.get(f"/invoice/{invoice_id}").text
    assert "price changed" in page and "first billed $65" in page

    assert "price changed" in client.get("/job/265110").text
    assert "price changed" in client.get("/incoming").text


def test_rejecting_the_first_invoice_on_the_page_clears_the_flag():
    """The reject button, not just the function: the page must re-check the job."""
    with SessionLocal() as s:
        job = _job(s, "265111")
        first = _invoice(s, job, 1, [(BOOT, 2, "65")])
        second = _invoice(s, job, 2, [(BOOT, 1, "66")])
        recompare_job(s, job)
        assert second.lines_price_changed == 1
        s.commit()
        first_id, second_id = first.id, second.id

    client.post(f"/invoice/{first_id}/decide",
                data={"decision": "reject", "actor": "Zack", "note": "Wrong job"},
                follow_redirects=False)

    with SessionLocal() as s:
        assert s.get(Invoice, second_id).lines_price_changed == 0

    # Reopening takes a reason now (2026-09-12 review).
    client.post(f"/invoice/{first_id}/decide",
                data={"decision": "reopen", "actor": "Zack", "note": "Right job after all"},
                follow_redirects=False)

    with SessionLocal() as s:
        assert s.get(Invoice, second_id).lines_price_changed == 1


# A database as it exists in production before this deploy: no price-watch
# columns on either table, and a row already in each.
OLD_SCHEMA = """
CREATE TABLE invoice (
    id INTEGER NOT NULL PRIMARY KEY,
    job_id INTEGER NOT NULL,
    document_id INTEGER NOT NULL,
    vendor VARCHAR(255),
    invoice_number VARCHAR(128),
    created_at DATETIME
);
CREATE TABLE invoice_line (
    id INTEGER NOT NULL PRIMARY KEY,
    invoice_id INTEGER NOT NULL,
    description TEXT,
    unit_price NUMERIC(14, 4)
);
"""


def test_the_new_columns_reach_a_database_already_in_service(monkeypatch):
    path = Path(tempfile.mkdtemp(prefix="finance-pw-old-")) / "existing.db"
    raw = sqlite3.connect(path)
    raw.executescript(OLD_SCHEMA)
    raw.execute("INSERT INTO invoice (id, job_id, document_id, vendor, invoice_number) "
                "VALUES (1, 1, 1, 'New Castle', 'INV-1')")
    raw.execute("INSERT INTO invoice_line (id, invoice_id, description, unit_price) "
                "VALUES (1, 1, 'pipe boot', '65')")
    raw.commit()
    raw.close()

    engine = create_engine(f"sqlite:///{path}", future=True)
    monkeypatch.setattr(appdb, "engine", engine)
    try:
        Base.metadata.create_all(engine)
        appdb._add_missing_columns()
        with engine.begin() as conn:
            assert conn.execute(
                text("SELECT lines_price_changed FROM invoice WHERE id = 1")
            ).scalar() == 0
            assert conn.execute(
                text("SELECT first_billed_price, first_billed_invoice_id "
                     "FROM invoice_line WHERE id = 1")
            ).one() == (None, None)
    finally:
        engine.dispose()

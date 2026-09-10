"""The invoice says what each line was compared to, when the words differ.

Zack, on a job quoted in one shingle colour and ordered in another: the prices
still matched, and nothing on the invoice said the line had been priced
against a different colour. A part number or a close description is exactly
how a colour swap gets matched, so the quote line's wording is shown under the
invoice line's whenever the two differ.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = Path(tempfile.mkdtemp(prefix="finance-wording-"))
os.environ["DATA_DIR"] = str(_TMP)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["APP_PASSWORD"] = ""
os.environ["ANTHROPIC_API_KEY"] = "test"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.matching import worded_differently  # noqa: E402
from app.models import Document, Invoice, InvoiceLine, Job, Quote, QuoteLine  # noqa: E402
from app.services import recompare_job  # noqa: E402

D = Decimal
client = TestClient(app)

YARD = "New Castle Building Products"
QUOTED = "GAF Timberline HDZ Shingles Weathered Wood"
ORDERED = "GAF Timberline HDZ Shingles Charcoal"


def setup_module(_module) -> None:
    # Never drop_all: every test module shares one engine (see test_compare).
    init_db()


def _line(desc, quoted_desc=None):
    quote_line = None if quoted_desc is None else SimpleNamespace(description=quoted_desc)
    return SimpleNamespace(description=desc, quote_line=quote_line)


def test_a_different_colour_is_worded_differently():
    assert worded_differently(_line(ORDERED, QUOTED))


def test_the_same_words_in_other_case_and_punctuation_are_not():
    assert not worded_differently(_line("gaf timberline hdz shingles, weathered wood", QUOTED))


def test_a_line_matched_to_nothing_has_nothing_to_show():
    assert not worded_differently(_line(ORDERED))


def _doc(session, job: Job, tag: str, kind: str) -> Document:
    doc = Document(job_id=job.id, filename=f"{tag}.pdf", sha256=tag.ljust(64, "0"),
                   stored_path=str(_TMP / f"{tag}.pdf"), kind=kind, status="ready")
    session.add(doc)
    session.flush()
    return doc


def test_the_invoice_shows_the_quoted_colour_under_the_ordered_one():
    """One generic part number for every colour, so the swap matches on SKU."""
    with SessionLocal() as s:
        job = Job(job_number="265201", name="Colour swap")
        s.add(job)
        s.flush()

        quote = Quote(job_id=job.id, document_id=_doc(s, job, "qw265201q", "quote").id,
                      is_master=True, vendor=YARD)
        s.add(quote)
        s.flush()
        s.add_all([
            QuoteLine(quote_id=quote.id, line_no=1, sku="TLHDZ", description=QUOTED,
                      qty=D(30), uom="BD", unit_price=D("40"), extended=D("1200")),
            QuoteLine(quote_id=quote.id, line_no=2, sku="DE10", description="Drip edge 10ft",
                      qty=D(20), uom="EA", unit_price=D("9"), extended=D("180")),
        ])

        invoice = Invoice(job_id=job.id, document_id=_doc(s, job, "qw265201i", "invoice").id,
                          vendor=YARD, invoice_number="INV-265201",
                          invoice_date=date(2026, 9, 10), total=D("1380"))
        s.add(invoice)
        s.flush()
        s.add_all([
            InvoiceLine(invoice_id=invoice.id, line_no=1, sku="TLHDZ", description=ORDERED,
                        qty=D(30), uom="BD", unit_price=D("40"), extended=D("1200")),
            InvoiceLine(invoice_id=invoice.id, line_no=2, sku="DE10", description="Drip edge 10ft",
                        qty=D(20), uom="EA", unit_price=D("9"), extended=D("180")),
        ])
        s.flush()
        recompare_job(s, job)
        s.commit()
        invoice_id = invoice.id

    page = client.get(f"/invoice/{invoice_id}").text

    assert f"on the quote as: {QUOTED}" in page
    # The drip edge is word for word what was quoted: nothing to add.
    assert page.count("on the quote as:") == 1

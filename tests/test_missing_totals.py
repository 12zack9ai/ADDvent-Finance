"""An invoice filed with no amount.

Loughlin & Son's draws on the Mahwah roof are typed up in Word - "Deposit",
"2nd payment" - with no totals box. Two came in, Zack: "arent recorded as any
money. but i know the price is on the invoice". The first read of the same
PDFs had found $25,000 and $26,805; the second found nothing, so the Subs page
counted nothing.
"""
from __future__ import annotations

import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = Path(tempfile.mkdtemp(prefix="finance-totals-"))
os.environ["DATA_DIR"] = str(_TMP)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["APP_PASSWORD"] = ""
os.environ["ANTHROPIC_API_KEY"] = "test"

from sqlalchemy import select  # noqa: E402

from app import extract, services  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.extract import ExtractionResult  # noqa: E402
from app.models import Invoice  # noqa: E402

D = Decimal
VENDOR = "Loughlin Totals Construction"


def setup_module(_module) -> None:
    # Never drop_all: every test module shares one engine (see test_compare).
    init_db()


def _payload(**over) -> dict:
    payload = {
        "doc_type": "invoice", "vendor": VENDOR, "document_number": "",
        "document_date": "2026-09-11", "total": "", "subtotal": "", "tax": "",
        "freight": "", "job_number_hint": "",
        "lines": [{"line_no": 1, "description": "2nd payment - roof, 144 Oldwoods Court",
                   "qty": "", "unit_price": "", "extended": ""}],
    }
    payload.update(over)
    return payload


def _file(tag: str, payload: dict) -> int:
    path = _TMP / f"{tag}.pdf"
    path.write_bytes(b"%PDF-1.4\n% " + tag.encode() + b"\n%%EOF\n")
    with SessionLocal() as session:
        doc = services.ingest_file(
            session, path, f"{tag}.pdf", source="email", sender="zmabry@addventuresinc.com",
            subject="FW: 2nd payment 265740", message_id=f"<{tag}@x>",
            extraction=ExtractionResult(payload=payload, model="test"))
        session.commit()
        return session.scalar(select(Invoice.id).where(Invoice.document_id == doc.id))


def _total(invoice_id: int):
    with SessionLocal() as session:
        return session.get(Invoice, invoice_id).total


@pytest.fixture()
def reader(monkeypatch):
    """Stands in for the second read. `reader.finds[tag]` is the TOTAL it reads
    off that document. Every module shares one database, so other modules'
    documents may be read too - calls are counted per document."""
    class Reader:
        finds: dict = {}
        read: list = []

        @classmethod
        def calls(cls, tag: str) -> int:
            return sum(tag.encode() in content for content in cls.read)

    def read(path, hint=""):
        content = Path(path).read_bytes()
        Reader.read.append(content)
        total = next((t for tag, t in Reader.finds.items() if tag.encode() in content), "")
        return ExtractionResult(payload=_payload(total=total), model="reread")

    monkeypatch.setattr(services, "extract_document", read)
    return Reader


# --- what is printed is used ---------------------------------------------------------

def test_a_printed_total_is_the_total():
    assert services.printed_total(_payload(total="25000", subtotal="1")) == D("25000")


def test_no_total_but_a_subtotal_uses_the_subtotal_and_charges():
    assert services.printed_total(_payload(subtotal="25000", tax="1500", freight="")) == D("26500")


def test_no_total_but_line_amounts_uses_the_lines():
    lines = [{"description": "Deposit", "extended": "25000"},
             {"description": "Dumpster", "extended": "805"}]
    assert services.printed_total(_payload(lines=lines)) == D("25805")


def test_a_line_without_an_amount_means_the_lines_are_not_a_total():
    lines = [{"description": "Deposit", "extended": "25000"},
             {"description": "Tear off", "extended": ""}]
    assert services.printed_total(_payload(lines=lines)) is None


def test_nothing_printed_is_nothing():
    assert services.printed_total(_payload()) is None


def test_an_invoice_filed_with_only_a_line_amount_carries_that_amount():
    lines = [{"line_no": 1, "description": "2nd payment", "qty": "", "unit_price": "",
              "extended": "25000"}]
    invoice_id = _file("line-only", _payload(lines=lines))
    assert _total(invoice_id) == D("25000")


# --- and when nothing was read, it is read again - once ----------------------------------

def test_an_invoice_with_no_amount_is_read_again_and_gets_it(reader):
    invoice_id = _file("no-amount", _payload())
    assert _total(invoice_id) is None

    reader.finds = {"no-amount": "26805"}
    with SessionLocal() as session:
        found = services.reread_missing_totals(session, limit=100)

    assert _total(invoice_id) == D("26805")
    assert reader.calls("no-amount") == 1
    assert any("no-amount.pdf" in item and "26,805.00" in item for item in found)


def test_it_is_read_again_only_once_even_if_the_second_read_misses_too(reader):
    invoice_id = _file("still-nothing", _payload())
    reader.finds = {}
    with SessionLocal() as session:
        services.reread_missing_totals(session, limit=100)
        services.reread_missing_totals(session, limit=100)      # next hour
        services.reread_missing_totals(session, limit=100)

    assert _total(invoice_id) is None
    assert reader.calls("still-nothing") == 1


def test_the_reader_is_told_what_a_draw_looks_like():
    rule = extract.SYSTEM_PROMPT.split("10. TOTAL, for an invoice", 1)[1].split("Call record_document", 1)[0]
    rule = " ".join(rule.split())                  # the prompt wraps its lines
    for said in ("Amount due", "no totals box", "That printed amount is the TOTAL",
                 "exactly one amount, that amount is the TOTAL"):
        assert said in rule, said

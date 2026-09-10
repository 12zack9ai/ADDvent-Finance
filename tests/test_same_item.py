"""Colour variants pair on their own; anything else a person pairs by hand.

Zack, job 261216: quoted in one shingle colour, ordered in another, and the
invoice's shingle line was never compared - different part number, different
wording. Asked which fix, "both": teach the matcher colours, and give people a
"same item" button for whatever it still cannot pair.
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

_TMP = Path(tempfile.mkdtemp(prefix="finance-sameitem-"))
os.environ["DATA_DIR"] = str(_TMP)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["APP_PASSWORD"] = ""
os.environ["ANTHROPIC_API_KEY"] = "test"

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.matching import QuoteIndex, match_line, norm_text, strip_colors  # noqa: E402
from app.models import (  # noqa: E402
    APPROVAL_REJECTED,
    VERDICT_MATCH,
    VERDICT_NOT_ON_QUOTE,
    VERDICT_OVER,
    Document,
    Invoice,
    InvoiceLine,
    ItemMatch,
    Job,
    Quote,
    QuoteLine,
)
from app.services import recompare_job  # noqa: E402

D = Decimal
client = TestClient(app)

ABC = "ABC Supply Co. inc."
QUOTED = "GAF Timberline HDZ Shingles Weathered Wood"
ORDERED = "GAF Timberline HDZ Shingles Charcoal"


def setup_module(_module) -> None:
    # Never drop_all: every test module shares one engine (see test_compare).
    init_db()


# --- the colour rule, on its own -----------------------------------------

def _line(desc, sku="", uom="BD"):
    return SimpleNamespace(description=desc, sku=sku, uom=uom)


def _pair(invoice_desc, quote_desc, *, invoice_sku="", quote_sku="", invoice_uom="BD"):
    index = QuoteIndex.build([_line(quote_desc, quote_sku)])
    return match_line(_line(invoice_desc, invoice_sku, invoice_uom), index)


def test_colours_come_out_and_the_product_stays():
    assert strip_colors(norm_text(QUOTED)) == "gaf timberline hdz shingles"
    assert strip_colors(norm_text("Owens Corning Duration Onyx Black")) == "owens corning duration"


def test_a_material_is_not_a_colour():
    assert strip_colors("copper flashing") == "copper flashing"
    assert strip_colors("slate hammer") == "slate hammer"


def test_the_same_shingle_in_another_colour_pairs():
    quoted, method = _pair(ORDERED, QUOTED, invoice_sku="GAFHDZCH", quote_sku="GAFHDZWW")
    assert quoted is not None and method == "color"


def test_the_same_colour_on_another_brand_does_not():
    quoted, _ = _pair("Owens Corning Duration Shingles Charcoal", "GAF Timberline HDZ Shingles Charcoal")
    assert quoted is None


def test_a_short_line_does_not_pair_with_everything_containing_it():
    quoted, _ = _pair("Black nails", "Coil nails 1-1/4 galvanized", invoice_uom="BX")
    assert quoted is None


def test_a_colour_pair_still_respects_units():
    quoted, _ = _pair(ORDERED, QUOTED, invoice_uom="SQ")      # squares, not bundles
    assert quoted is None


# --- on the real pages --------------------------------------------------

def _doc(session, tag: str, kind: str) -> Document:
    doc = Document(filename=f"{tag}.pdf", sha256=tag.ljust(64, "0"),
                   stored_path=str(_TMP / f"{tag}.pdf"), kind=kind, status="ready")
    session.add(doc)
    session.flush()
    return doc


def _job(session, number: str) -> Job:
    job = Job(job_number=number, name="Same item test")
    session.add(job)
    session.flush()
    return job


def _quote(session, job: Job, lines) -> Quote:
    """lines as (sku, description, price); bundles throughout."""
    quote = Quote(job_id=job.id, document_id=_doc(session, f"si{job.job_number}q", "quote").id,
                  is_master=True, vendor="ABC Supply Co. Inc.")
    session.add(quote)
    session.flush()
    for i, (sku, desc, price) in enumerate(lines, 1):
        session.add(QuoteLine(quote_id=quote.id, line_no=i, sku=sku, description=desc,
                              qty=D(1), uom="BD", unit_price=D(price), extended=D(price)))
    session.flush()
    return quote


def _invoice(session, job: Job, n: int, lines) -> Invoice:
    inv = Invoice(job_id=job.id,
                  document_id=_doc(session, f"si{job.job_number}i{n}", "invoice").id,
                  vendor=ABC, invoice_number=f"{job.job_number}-{n}",
                  invoice_date=date(2026, 9, n), total=sum((D(p) for _, _, p in lines), D(0)))
    session.add(inv)
    session.flush()
    for i, (sku, desc, price) in enumerate(lines, 1):
        session.add(InvoiceLine(invoice_id=inv.id, line_no=i, sku=sku, description=desc,
                                qty=D(1), uom="BD", unit_price=D(price), extended=D(price)))
    session.flush()
    session.refresh(inv)
    return inv


def test_the_colour_swap_is_priced_and_the_invoice_says_so():
    with SessionLocal() as s:
        job = _job(s, "265301")
        _quote(s, job, [("GAFHDZWW", QUOTED, "40")])
        inv = _invoice(s, job, 1, [("GAFHDZCH", ORDERED, "40")])
        recompare_job(s, job)
        assert inv.lines[0].verdict == VERDICT_MATCH
        s.commit()
        invoice_id = inv.id

    page = client.get(f"/invoice/{invoice_id}").text
    assert f"on the quote as: {QUOTED}" in page
    assert "different color" in page
    assert "Same item as a quote line?" not in page       # paired: nothing to ask


def test_same_item_pairs_remembers_and_undoes():
    starter = ("GAFPROSTART", "GAF Pro-Start Starter Strip", "55")
    with SessionLocal() as s:
        job = _job(s, "265302")
        quote = _quote(s, job, [starter])
        first = _invoice(s, job, 1, [("ABC-STRT", "STARTER SHINGLE ROLL", "58")])
        recompare_job(s, job)
        assert first.lines[0].verdict == VERDICT_NOT_ON_QUOTE
        s.commit()
        first_id, line_id = first.id, first.lines[0].id
        quote_line_id = quote.lines[0].id

    assert "Same item as a quote line?" in client.get(f"/invoice/{first_id}").text

    client.post(f"/invoice/{first_id}/line/{line_id}/same",
                data={"quote_line_id": str(quote_line_id), "actor": "Zack"},
                follow_redirects=False)

    with SessionLocal() as s:
        line = s.get(InvoiceLine, line_id)
        assert line.verdict == VERDICT_OVER            # 58 against the quoted 55
        assert line.match_method.startswith("manual")
        # The next invoice billing the same part pairs without a click.
        job = s.get(Job, s.get(Invoice, first_id).job_id)
        second = _invoice(s, job, 2, [("ABC-STRT", "STARTER SHINGLE ROLL", "55")])
        recompare_job(s, job)
        assert second.lines[0].verdict == VERDICT_MATCH
        s.commit()
        second_line_id = second.lines[0].id

    page = client.get(f"/invoice/{first_id}").text
    assert "matched by hand" in page and "not the same item" in page

    client.post(f"/invoice/{first_id}/line/{line_id}/not-same", follow_redirects=False)

    with SessionLocal() as s:
        assert s.get(InvoiceLine, line_id).verdict == VERDICT_NOT_ON_QUOTE
        assert s.get(InvoiceLine, second_line_id).verdict == VERDICT_NOT_ON_QUOTE


def test_a_line_cannot_be_paired_with_another_jobs_quote():
    with SessionLocal() as s:
        mine = _job(s, "265303")
        _quote(s, mine, [("X1", "Ridge vent", "20")])
        inv = _invoice(s, mine, 1, [("Y9", "Something unquoted", "10")])
        other = _job(s, "265304")
        foreign = _quote(s, other, [("Z1", "Something unquoted", "10")])
        recompare_job(s, mine)
        s.commit()
        invoice_id, line_id, foreign_id = inv.id, inv.lines[0].id, foreign.lines[0].id
        mine_id = mine.id

    client.post(f"/invoice/{invoice_id}/line/{line_id}/same",
                data={"quote_line_id": str(foreign_id)}, follow_redirects=False)

    with SessionLocal() as s:
        assert not s.scalars(select(ItemMatch).where(ItemMatch.job_id == mine_id)).all()
        assert s.get(InvoiceLine, line_id).verdict == VERDICT_NOT_ON_QUOTE


def test_a_hold_a_person_placed_survives_a_recompare():
    with SessionLocal() as s:
        job = _job(s, "265306")
        _quote(s, job, [("GAFHDZWW", QUOTED, "40")])
        inv = _invoice(s, job, 1, [("GAFHDZCH", ORDERED, "40")])
        recompare_job(s, job)
        s.commit()
        invoice_id = inv.id

    client.post(f"/invoice/{invoice_id}/decide",
                data={"decision": "hold", "actor": "Zack", "note": "Waiting on the yard"},
                follow_redirects=False)

    with SessionLocal() as s:
        inv = s.get(Invoice, invoice_id)
        assert inv.approval_status == "held"
        recompare_job(s, inv.job)
        assert inv.approval_status == "held"
        assert inv.hold_reason == "Waiting on the yard"


def test_the_recheck_after_a_deploy_reaches_invoices_already_filed_once():
    """Job 261216's invoices arrived before colours paired. The re-check is
    what brings them in - and it runs once per matcher change, not per boot."""
    from app.config import settings
    from app.main import RECHECK_VERSION, _recheck_once

    marker = settings.data_dir / f"recheck-{RECHECK_VERSION}.done"
    marker.unlink(missing_ok=True)

    with SessionLocal() as s:
        job = _job(s, "265307")
        _quote(s, job, [("GAFHDZWW", QUOTED, "40")])
        inv = _invoice(s, job, 1, [("GAFHDZCH", ORDERED, "40")])
        inv.lines[0].verdict = VERDICT_NOT_ON_QUOTE    # as filed before this change
        s.commit()
        line_id = inv.lines[0].id

    _recheck_once()
    with SessionLocal() as s:
        line = s.get(InvoiceLine, line_id)
        assert line.verdict == VERDICT_MATCH
        line.verdict = VERDICT_NOT_ON_QUOTE
        s.commit()

    assert marker.exists()
    _recheck_once()                                    # already done: nothing moves
    with SessionLocal() as s:
        assert s.get(InvoiceLine, line_id).verdict == VERDICT_NOT_ON_QUOTE


def test_a_rejected_invoice_stays_rejected_when_the_job_is_recompared():
    """A new quote, or a "same item" click, re-compares the whole job."""
    with SessionLocal() as s:
        job = _job(s, "265305")
        inv = _invoice(s, job, 1, [("GAFHDZCH", ORDERED, "40")])
        inv.approval_status = APPROVAL_REJECTED
        s.flush()
        _quote(s, job, [("GAFHDZWW", QUOTED, "40")])
        recompare_job(s, job)
        assert inv.approval_status == APPROVAL_REJECTED

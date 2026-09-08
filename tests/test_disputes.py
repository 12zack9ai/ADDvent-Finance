"""Asking for the money back, and finding out who keeps making us ask.

Job 241640 found $2,596 billed over quote on its first day of real documents.
That figure is worth exactly nothing until somebody asks New Castle for it and
a credit memo comes back, which is why this exists and why it records the
answer as well as the question.

The letter is composed in Decimal from lines the matching engine already
priced. No number in it comes from anywhere else.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = Path(tempfile.mkdtemp(prefix="finance-dispute-"))
os.environ.setdefault("DATA_DIR", str(_TMP))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP / 'test.db'}")
os.environ["APP_PASSWORD"] = ""
os.environ["ANTHROPIC_API_KEY"] = "test"

from fastapi.testclient import TestClient  # noqa: E402

from app import disputes  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    DISPUTE_CREDITED,
    DISPUTE_OPEN,
    Dispute,
    Document,
    Invoice,
    InvoiceLine,
    Job,
    Quote,
    VERDICT_MATCH,
    VERDICT_NOT_ON_QUOTE,
    VERDICT_OVER,
)

client = TestClient(app)
_n = {"i": 0}


def setup_module(_module) -> None:
    init_db()


def _doc(session, job, tag: str) -> int:
    _n["i"] += 1
    d = Document(job_id=job.id, filename=f"{tag}.pdf",
                 # Fixed width, or "…q1" padded with zeros and "…q10" padded
                 # with zeros are the same 64 characters. That collision is
                 # the reason the seed script grew a counter of its own.
                 sha256=f"dispute-{tag}-{_n['i']:06d}".ljust(64, "0"),
                 stored_path=f"/tmp/{tag}.pdf", kind="invoice", status="ready")
    session.add(d)
    session.flush()
    return d.id


def _invoice(job_number: str, lines, *, with_quote=True, total="20000.00",
             over="2596.45") -> int:
    """One job, one quote, one invoice with the lines given."""
    with SessionLocal() as session:
        job = session.query(Job).filter_by(job_number=job_number).first()
        if job is None:
            job = Job(job_number=job_number, name="Dispute test")
            session.add(job)
            session.flush()

        quote_id = None
        if with_quote:
            quote = Quote(job_id=job.id, document_id=_doc(session, job, "q"),
                          vendor="New Castle Building Products", is_master=True,
                          quote_number="07RM-Q-1", quote_date=date(2026, 8, 1),
                          total=D("40000.00"))
            session.add(quote)
            session.flush()
            quote_id = quote.id

        invoice = Invoice(
            job_id=job.id, document_id=_doc(session, job, "i"), quote_id=quote_id,
            vendor="New Castle Building Products",
            # Distinct per job. Every module in this suite shares one
            # database, and one vendor billing the same number for the same
            # amount on several jobs is not a thing that happens - it just
            # breaks other people's tests.
            invoice_number=f"07RM0002825027-{job_number}",
            invoice_date=date(2026, 9, 2), total=D(total),
            overbilled_amount=D(over),
        )
        session.add(invoice)
        session.flush()
        for sku, qty, unit, quoted, ext_var, verdict in lines:
            session.add(InvoiceLine(
                invoice_id=invoice.id, sku=sku, description=sku,
                qty=D(qty) if qty else None,
                unit_price=D(unit) if unit else None,
                quote_unit_price=D(quoted) if quoted else None,
                extended=D(ext_var) if ext_var else None,
                extended_variance=D(ext_var) if ext_var else None,
                verdict=verdict,
            ))
        session.commit()
        return invoice.id


OVERBILLED = [
    ("SHG-TL-WW", "40", "324.50", "280.00", "1781.95", VERDICT_OVER),
    ("UND-SYN-10", "12", "92.00", "92.00", "0", VERDICT_MATCH),
    ("DELIVERY", "1", "814.50", None, "814.50", VERDICT_NOT_ON_QUOTE),
]


# --- the arithmetic --------------------------------------------------------

def test_the_draft_separates_overpricing_from_things_never_quoted():
    """Two different conversations. "You charged more than you said" and "we
    never agreed to this at all" get answered differently by a vendor."""
    invoice_id = _invoice("260960", OVERBILLED)
    with SessionLocal() as session:
        draft = disputes.build(session.get(Invoice, invoice_id))

    assert draft.priced_over == D("1781.95")
    assert draft.unquoted == D("814.50")
    assert draft.total == D("2596.45")
    assert draft.worth_sending


def test_a_line_billed_as_quoted_is_not_in_the_letter():
    invoice_id = _invoice("260961", OVERBILLED)
    with SessionLocal() as session:
        draft = disputes.build(session.get(Invoice, invoice_id))
    assert all("UND-SYN-10" not in item.description for item in draft.items)


def test_the_biggest_difference_is_argued_first():
    invoice_id = _invoice("260962", OVERBILLED)
    with SessionLocal() as session:
        draft = disputes.build(session.get(Invoice, invoice_id))
    assert [i.difference for i in draft.items] == [D("1781.95"), D("814.50")]


def test_unmatched_lines_are_not_raised_when_there_is_no_quote_at_all():
    """Without a quote every line reads as unmatched, and the letter would be
    a list of everything they sent. That is not a dispute, it is an insult."""
    invoice_id = _invoice("260963", [
        ("SHG-TL-WW", "40", "324.50", None, "12975.00", VERDICT_NOT_ON_QUOTE),
    ], with_quote=False, over="0")
    with SessionLocal() as session:
        draft = disputes.build(session.get(Invoice, invoice_id))
    assert draft.items == []
    assert not draft.worth_sending


def test_the_percentage_is_computed_not_guessed():
    invoice_id = _invoice("260964", OVERBILLED)
    with SessionLocal() as session:
        draft = disputes.build(session.get(Invoice, invoice_id))
    over = next(i for i in draft.items if i.quoted)
    # 280.00 -> 324.50 is 15.9%, which rounds to 16.
    assert over.percent == 16


# --- the letter ------------------------------------------------------------

def test_the_letter_carries_the_numbers_and_asks_for_something():
    invoice_id = _invoice("260965", OVERBILLED)
    with SessionLocal() as session:
        text = disputes.letter(disputes.build(session.get(Invoice, invoice_id)))

    assert "07RM0002825027-260965" in text
    assert "07RM-Q-1" in text          # what we are comparing against
    assert "$1,781.95" in text and "$814.50" in text
    assert "$2,596.45" in text
    assert "credit memo" in text
    # It has to leave room for "we agreed this with your foreman".
    assert "agreed with someone here" in text


# --- the pages -------------------------------------------------------------

def test_the_invoice_page_offers_to_ask_when_there_is_something_to_ask_for():
    invoice_id = _invoice("260966", OVERBILLED)
    body = client.get(f"/invoice/{invoice_id}").text
    assert f"/invoice/{invoice_id}/dispute" in body


def test_recording_that_it_was_sent_makes_it_trackable():
    invoice_id = _invoice("260967", OVERBILLED)
    r = client.post(f"/invoice/{invoice_id}/dispute",
                    data={"who": "Zack", "note": "spoke to Dave"},
                    follow_redirects=False)
    assert r.status_code == 303

    with SessionLocal() as session:
        d = session.query(Dispute).filter_by(invoice_id=invoice_id).one()
        assert d.amount == D("2596.45")
        assert d.status == DISPUTE_OPEN
        assert d.raised_by == "Zack"


def test_a_credit_is_recorded_against_what_was_asked():
    invoice_id = _invoice("260968", OVERBILLED)
    client.post(f"/invoice/{invoice_id}/dispute", data={"who": "Zack"})
    with SessionLocal() as session:
        dispute_id = session.query(Dispute).filter_by(invoice_id=invoice_id).one().id

    client.post(f"/dispute/{dispute_id}",
                data={"outcome": "credited", "credited": "1,781.95",
                      "note": "credit memo CM-4471"})
    with SessionLocal() as session:
        d = session.get(Dispute, dispute_id)
        assert d.status == DISPUTE_CREDITED
        assert d.credited == D("1781.95")
        assert d.closed_at is not None


def test_an_unreadable_credit_amount_is_refused_by_name():
    """Treating it as zero would understate what the checking recovered - the
    one number this whole page exists to report."""
    invoice_id = _invoice("260969", OVERBILLED)
    client.post(f"/invoice/{invoice_id}/dispute", data={"who": "Zack"})
    with SessionLocal() as session:
        dispute_id = session.query(Dispute).filter_by(invoice_id=invoice_id).one().id

    r = client.post(f"/dispute/{dispute_id}",
                    data={"outcome": "credited", "credited": "about half"},
                    follow_redirects=False)
    assert "Could+not+read" in r.headers["location"]
    with SessionLocal() as session:
        assert session.get(Dispute, dispute_id).status == DISPUTE_OPEN


def test_asking_on_an_invoice_with_nothing_over_is_refused():
    invoice_id = _invoice("260970", [
        ("SHG-TL-WW", "40", "280.00", "280.00", "0", VERDICT_MATCH),
    ], over="0")
    r = client.post(f"/invoice/{invoice_id}/dispute", data={"who": "Zack"},
                    follow_redirects=False)
    assert "Nothing+on+this+invoice" in r.headers["location"]


# --- the number nobody has today -------------------------------------------

def test_the_vendor_history_shows_who_keeps_doing_it():
    rows = [
        Dispute(vendor="New Castle Building Products", amount=D("2596.45"),
                status=DISPUTE_CREDITED, credited=D("1781.95")),
        Dispute(vendor="NEW CASTLE BUILDING PRODUCTS", amount=D("400.00"),
                status=DISPUTE_OPEN),
        Dispute(vendor="Bergen Dumpster", amount=D("120.00"),
                status=DISPUTE_OPEN),
    ]
    history = disputes.history(rows)
    assert [r.vendor for r in history][0].lower().startswith("new castle")

    castle = history[0]
    assert castle.raised == 2              # however they capitalise themselves
    assert castle.asked == D("2996.45")
    assert castle.recovered == D("1781.95")
    assert castle.open_count == 1
    assert castle.recovery_rate == 59


def test_a_vendor_who_has_never_been_credited_reports_no_rate_not_zero():
    history = disputes.history([
        Dispute(vendor="Bergen Dumpster", amount=D("0"), status=DISPUTE_OPEN),
    ])
    assert history[0].recovery_rate is None


def test_days_open_survives_a_round_trip_through_the_database():
    """A real 500 this caught. SQLite has no timezone type, so a timestamp
    written as aware reads back naive - and subtracting it from an aware
    "now" raises. It never shows up on the object you just built in memory,
    only once the page loads a row somebody saved earlier."""
    invoice_id = _invoice("260971", OVERBILLED)
    client.post(f"/invoice/{invoice_id}/dispute", data={"who": "Zack"})

    with SessionLocal() as session:
        stored = session.query(Dispute).filter_by(invoice_id=invoice_id).one()
        assert stored.days_open >= 0          # this is what used to raise

    assert client.get("/disputes").status_code == 200

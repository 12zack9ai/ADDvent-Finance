"""Sub or supplier: asked once per vendor, the moment it matters.

Superior Seamless Gutters' invoice landed with the supplier bills instead of on
the Subs page. Zack, 2026-09-11: "if the email doesn't know which it is it
shouldn't hesitate responding asking the question it needed."
"""
from __future__ import annotations

import os
import sys
import tempfile
from email.message import EmailMessage
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = Path(tempfile.mkdtemp(prefix="finance-roles-"))
os.environ["DATA_DIR"] = str(_TMP)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["APP_PASSWORD"] = ""
os.environ["ANTHROPIC_API_KEY"] = "test"

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import alerts, mail_imap, mail_send, services, vendor_roles  # noqa: E402
from app.approval import route  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.extract import REPLY_SENTINEL, ExtractionResult  # noqa: E402
from app.mail_types import PollResult  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Invoice, Job, Quote, VendorRole  # noqa: E402

client = TestClient(app)
ZACK = "Zack Mabry <zmabry@addventuresinc.com>"


def setup_module(_module) -> None:
    # Never drop_all: every test module shares one engine (see test_compare).
    init_db()


@pytest.fixture()
def outbox(monkeypatch):
    sent: list = []
    monkeypatch.setattr(mail_send, "send", sent.append)
    monkeypatch.setattr(settings, "can_send_mail", lambda: True)
    monkeypatch.setattr(settings, "may_email",
                        lambda address: address.lower().endswith("@addventuresinc.com"))
    monkeypatch.setattr(settings, "smtp_settings",
                        lambda: ("smtp.example", 587, "u", "p", "aifinance@addventuresinc.com"))
    monkeypatch.setattr(alerts, "where_to", lambda: "zmabry@addventuresinc.com")
    return sent


def _questions(sent):
    return [m for m in sent if str(m["Subject"]).startswith("Sub or supplier?")]


def _file(doc_type: str, job: str, vendor: str, tag: str, *, sender: str = ZACK,
          source: str = "email"):
    payload = {
        "doc_type": doc_type, "vendor": vendor, "document_number": f"{tag}",
        "document_date": "2026-09-11", "total": "5000", "subtotal": "5000",
        "job_number_hint": "",
        "lines": [{"line_no": 1, "description": "Gutters and leaders, install",
                   "qty": "1", "unit_price": "5000", "extended": "5000"}],
    }
    path = _TMP / f"{tag}.pdf"
    path.write_bytes(b"%PDF-1.4\n% " + tag.encode() + b"\n%%EOF\n")
    with SessionLocal() as session:
        doc = services.ingest_file(
            session, path, f"{tag}.pdf", source=source, sender=sender,
            subject=f"Fwd: {job} {doc_type}", message_id=f"<{tag}@icloud.com>",
            extraction=ExtractionResult(payload=payload, model="test"),
        )
        session.commit()
        return doc.id


def _invoices(vendor):
    with SessionLocal() as session:
        return [(i.id, bool(i.is_subcontract)) for i in session.scalars(
            select(Invoice).where(Invoice.vendor == vendor)).all()]


def _reply(to_question: EmailMessage, words: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = "Re: " + str(to_question["Subject"])
    msg["From"] = ZACK
    msg["Message-ID"] = f"<reply-{words.split()[0]}@icloud.com>"
    msg["In-Reply-To"] = to_question["Message-ID"]
    msg["References"] = to_question["Message-ID"]
    msg.set_content(f"{words}\n\nOn Fri, Sep 11, 2026 the app wrote:\n> {REPLY_SENTINEL}\n"
                    "> Is it a subcontractor or a supplier? Reply sub or supplier.\n")
    return msg


def _read_reply(msg):
    with SessionLocal() as session:
        result = PollResult()
        mail_imap._process(session, msg, result)
        return result


# --- asking ----------------------------------------------------------------------

def test_an_unknown_vendor_is_asked_about_once_straight_back(outbox):
    _file("invoice", "265701", "Gutter Pros LLC", "gp-1")
    _file("invoice", "265701", "Gutter Pros LLC", "gp-2")

    asked = _questions(outbox)
    assert len(asked) == 1
    assert asked[0]["To"] == "zmabry@addventuresinc.com"
    assert "Gutter Pros LLC" in asked[0]["Subject"]
    assert "sub, or supplier" in asked[0].get_content()
    assert asked[0]["Auto-Submitted"] == "auto-generated"
    assert all(not flagged for _id, flagged in _invoices("Gutter Pros LLC"))  # filed as supplier meanwhile


def test_when_the_vendor_sent_it_themselves_you_are_asked(outbox):
    _file("invoice", "265702", "Roofline Subs Inc", "rs-1",
          sender="Billing <billing@roofline.example>")

    assert _questions(outbox)[0]["To"] == "zmabry@addventuresinc.com"


def test_an_upload_is_asked_on_the_page_not_by_email(outbox):
    doc_id = _file("invoice", "265703", "Upload Only Co", "uo-1", source="upload")

    assert _questions(outbox) == []
    with SessionLocal() as session:
        invoice = session.scalar(select(Invoice).where(Invoice.document_id == doc_id))
        invoice_id = invoice.id
    assert "Is Upload Only Co a subcontractor or a supplier?" in client.get(f"/invoice/{invoice_id}").text


def test_a_vendor_holding_a_contract_is_never_asked(outbox):
    with SessionLocal() as session:
        job = Job(job_number="265704", name="Contracted")
        session.add(job)
        session.flush()
        doc_id = _doc_row(session, "hc-q")
        session.add(Quote(job_id=job.id, document_id=doc_id, vendor="Contract Gutters Inc",
                          is_master=True, is_subcontract=True, total=50000))
        session.commit()

    _file("invoice", "265704", "Contract Gutters Inc", "hc-1")

    assert _questions(outbox) == []
    assert _invoices("Contract Gutters Inc")[0][1] is True


def _doc_row(session, tag):
    from app.models import Document
    doc = Document(filename=f"{tag}.pdf", sha256=f"role-{tag}".ljust(64, "0"),
                   stored_path=str(_TMP / f"{tag}.pdf"), kind="quote", status="ready")
    session.add(doc)
    session.flush()
    return doc.id


# --- the answer ----------------------------------------------------------------------

def test_a_reply_saying_sub_moves_every_invoice_to_subs(outbox):
    _file("invoice", "265705", "Rivera Gutters Inc", "rg-1")
    _file("invoice", "265705", "Rivera Gutters Inc", "rg-2")
    question = _questions(outbox)[0]

    result = _read_reply(_reply(question, "Sub"))

    assert "Rivera Gutters Inc -> subcontractor (from reply)" in result.filed
    assert all(flagged for _id, flagged in _invoices("Rivera Gutters Inc"))
    panel = client.get("/sub-invoices?job=265705").text.split("Rivera Gutters Inc", 1)[1][:3000]
    assert "None on file" in panel
    assert "over contract" not in panel and "past the award" not in panel
    assert "Rivera Gutters Inc" in client.get("/checks").text


def test_subs_opens_on_one_folder_per_job_like_the_invoices_page(outbox):
    """Zack: "doesn't look like it's in a job folder though within subs.
    Similar to the vendor tab." """
    _file("invoice", "265714", "Folder Gutters Inc", "fg-1")
    with SessionLocal() as session:
        vendor_roles.set_role(session, "Folder Gutters Inc", vendor_roles.SUB)
        session.commit()

    body = client.get("/sub-invoices").text
    folder = body.split('href="/sub-invoices?job=265714"', 1)[1][:1500]

    assert "Folder Gutters Inc" in folder and "no contract on file" in folder
    assert "1 to look at" in folder
    assert "fg-1" not in body                      # the invoices are inside the folder

    inside = client.get("/sub-invoices?job=265714").text
    assert "Folder Gutters Inc" in inside and "fg-1" in inside


def test_a_sub_without_a_contract_is_never_blocked_as_over_it(outbox):
    _file("invoice", "265706", "No Paper Subs LLC", "np-1")
    with SessionLocal() as session:
        vendor_roles.set_role(session, "No Paper Subs LLC", vendor_roles.SUB)
        session.commit()
        invoice = session.scalar(select(Invoice).where(Invoice.vendor == "No Paper Subs LLC"))
        assert not [b for b in route(invoice).blockers if "contract" in b.lower()]


def test_a_reply_saying_supplier_keeps_them_with_the_supplier_bills(outbox):
    _file("invoice", "265707", "Shingle Yard Co", "sy-1")

    _read_reply(_reply(_questions(outbox)[0], "supplier"))

    with SessionLocal() as session:
        assert vendor_roles.role_of(session, "Shingle Yard Co") == "supplier"
    assert _invoices("Shingle Yard Co")[0][1] is False


def test_an_unclear_reply_is_not_guessed_at(outbox):
    _file("invoice", "265708", "Maybe Gutters Co", "mg-1")

    result = _read_reply(_reply(_questions(outbox)[0], "not sure, ask Todd"))

    assert not any("Maybe Gutters" in item for item in result.filed)
    with SessionLocal() as session:
        assert vendor_roles.role_of(session, "Maybe Gutters Co") == ""


def test_the_button_on_the_invoice_decides_it_too(outbox):
    doc_id = _file("invoice", "265709", "Button Gutters Inc", "bg-1")
    with SessionLocal() as session:
        invoice_id = session.scalar(select(Invoice.id).where(Invoice.document_id == doc_id))

    resp = client.post("/vendor-role", data={"vendor": "Button Gutters Inc", "role": "sub",
                                             "next": f"/invoice/{invoice_id}"},
                       follow_redirects=False)

    assert resp.status_code in (302, 303)
    assert _invoices("Button Gutters Inc")[0][1] is True
    assert "Button Gutters Inc</strong> is a subcontractor" in client.get(f"/invoice/{invoice_id}").text


def test_a_known_sub_goes_straight_to_subs_without_asking(outbox):
    with SessionLocal() as session:
        vendor_roles.set_role(session, "Known Sub Gutters Inc", vendor_roles.SUB)
        session.commit()

    _file("invoice", "265710", "Known Sub Gutters Inc", "ks-1")
    _file("quote", "265711", "Known Sub Gutters Inc", "ks-q")

    assert _questions(outbox) == []
    assert _invoices("Known Sub Gutters Inc")[0][1] is True
    with SessionLocal() as session:
        quote = session.scalar(select(Quote).where(Quote.vendor == "Known Sub Gutters Inc"))
        assert quote.is_subcontract


def test_a_question_that_could_not_be_sent_is_tried_again_next_time(outbox, monkeypatch):
    def down(_msg):
        raise mail_send.SendError("smtp down")

    monkeypatch.setattr(mail_send, "send", down)
    _file("invoice", "265712", "Retry Gutters Co", "rt-1")
    with SessionLocal() as session:
        assert session.scalar(select(VendorRole).where(VendorRole.vendor == "Retry Gutters Co")) is None

    monkeypatch.setattr(mail_send, "send", outbox.append)
    _file("invoice", "265712", "Retry Gutters Co", "rt-2")

    assert len(_questions(outbox)) == 1


def test_an_approved_invoice_from_a_sub_without_a_contract_is_not_an_overage(outbox):
    """Only approved money counts toward an overage, so this is the case that
    would show "over contract" against a $0 award if the guard were missing."""
    from app import subs

    doc_id = _file("invoice", "265713", "Approved Subs LLC", "ap-1")
    with SessionLocal() as session:
        vendor_roles.set_role(session, "Approved Subs LLC", vendor_roles.SUB)
        invoice = session.scalar(select(Invoice).where(Invoice.document_id == doc_id))
        invoice.approval_status = "approved"
        session.commit()
        position = subs.position_for(invoice.job, "Approved Subs LLC")
        assert position is not None and not position.has_contract
        assert position.overage == 0 and position.would_exceed == 0

    panel = client.get("/sub-invoices?job=265713").text.split("Approved Subs LLC", 1)[1][:3000]
    assert "over contract" not in panel

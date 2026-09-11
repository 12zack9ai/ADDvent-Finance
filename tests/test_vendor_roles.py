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
          source: str = "email", number: str | None = None,
          subject: str | None = None, body: str = "", sub_dept: bool = False,
          lines: list | None = None):
    # The job number rides at the bottom of the body, so a test can write any
    # subject and note it likes above it.
    subject = f"Fwd: {job} {doc_type}" if subject is None else subject
    body = f"{body}\n\njob {job}"
    payload = {
        "doc_type": doc_type, "vendor": vendor,
        "document_number": tag if number is None else number,
        "document_date": "2026-09-11", "total": "5000", "subtotal": "5000",
        "job_number_hint": "",
        "lines": lines if lines is not None else [
            {"line_no": 1, "description": "Gutters and leaders, install",
             "qty": "1", "unit_price": "5000", "extended": "5000"}],
    }
    path = _TMP / f"{tag}.pdf"
    path.write_bytes(b"%PDF-1.4\n% " + tag.encode() + b"\n%%EOF\n")
    with SessionLocal() as session:
        doc = services.ingest_file(
            session, path, f"{tag}.pdf", source=source, sender=sender,
            subject=subject, body=body, message_id=f"<{tag}@icloud.com>",
            is_subcontract=sub_dept,
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


def test_a_subs_next_draw_without_an_invoice_number_is_filed(outbox):
    """Loughlin & Son's "2nd payment" on the Mahwah roof had no invoice number,
    and neither did the first, so it was refused as a duplicate of it."""
    _file("invoice", "265721", "Loughlin Draws Inc", "draw-1", number="")
    _file("invoice", "265721", "Loughlin Draws Inc", "draw-2", number="")
    _file("invoice", "265721", "Loughlin Draws Inc", "draw-3", number="")

    assert len(_invoices("Loughlin Draws Inc")) == 3


def test_a_sub_billing_in_draws_against_one_quote(outbox):
    """Loughlin & Son: one quote, then a deposit and three more payments, none
    with an invoice number - "one quote sent and 4 invoices, only one made it
    in". All four land on Subs under the job, against the quote as their
    contract, each named by the email it came in on."""
    job = "265726"
    _file("quote", job, "Draw Roofing LLC", "dr-q", subject="FW: sub quote, Mahwah roof")
    for tag, subject in (("dr-1", "FW: Deposit"), ("dr-2", "FW: 2nd payment"),
                         ("dr-3", "FW: 3rd payment"), ("dr-4", "RE: FW: Final payment")):
        _file("invoice", job, "Draw Roofing LLC", tag, number="", subject=subject)

    assert _questions(outbox) == []                      # the forward said "sub"
    assert [flag for _id, flag in _invoices("Draw Roofing LLC")] == [True] * 4
    page = client.get(f"/sub-invoices?job={job}").text
    for label in ("Deposit", "2nd payment", "3rd payment", "Final payment"):
        assert f"{label}</a>" in page, label
    assert "FW:" not in page and "RE:" not in page
    assert "None on file" not in page                    # the quote is the contract


# --- told on the email, without being asked -------------------------------------------

def test_writing_sub_on_the_forward_files_it_as_a_sub_without_asking(outbox):
    """Zack: "i emailed the word sub invoice and it still put it into the
    vendor invoice"."""
    _file("invoice", "265722", "Told Sub Roofing LLC", "ts-1", subject="FW: sub invoice")

    assert _questions(outbox) == []
    assert _invoices("Told Sub Roofing LLC")[0][1] is True
    with SessionLocal() as session:
        assert vendor_roles.role_of(session, "Told Sub Roofing LLC") == vendor_roles.SUB


def test_a_note_above_the_forward_counts_and_the_forwarded_text_does_not(outbox):
    _file("invoice", "265723", "Note Sub LLC", "ns-1", subject="FW: deposit",
          body="this one is a sub\n\n-----Original Message-----\nFrom: Billing\nSee attached")
    _file("invoice", "265723", "Below The Line Co", "bl-1", subject="FW: deposit",
          body="see attached\n\n-----Original Message-----\nFrom: Billing\nSub floor repair")

    assert _invoices("Note Sub LLC")[0][1] is True
    assert _invoices("Below The Line Co")[0][1] is False
    assert [str(q["Subject"]) for q in _questions(outbox)] == ["Sub or supplier? Below The Line Co"]


def test_writing_supplier_on_the_forward_keeps_it_with_the_supplier_bills(outbox):
    _file("invoice", "265727", "Told Supplier Co", "tsu-1", subject="FW: supplier invoice")

    assert _questions(outbox) == []
    with SessionLocal() as session:
        assert vendor_roles.role_of(session, "Told Supplier Co") == vendor_roles.SUPPLIER


def test_sub_total_on_the_email_is_not_the_answer(outbox):
    _file("invoice", "265724", "Totals Only Co", "to-1", subject="FW: Sub total due")
    _file("invoice", "265724", "Hyphen Totals Co", "ht-1", subject="FW: sub-total attached")

    assert len(_questions(outbox)) == 2
    assert _invoices("Totals Only Co")[0][1] is False


def test_a_vendor_calling_itself_a_sub_is_not_our_decision(outbox):
    _file("invoice", "265725", "Self Styled Subs", "ss-1", subject="Sub invoice",
          sender="Billing <billing@selfstyled.example>")

    assert len(_questions(outbox)) == 1
    assert _invoices("Self Styled Subs")[0][1] is False


# --- two departments, kept apart -------------------------------------------------------

def test_a_subs_invoice_is_on_subs_and_never_with_the_supplier_invoices(outbox):
    """Zack: "one invoice sits in vendor still... it says i already marked it
    as a sub.... so why is it in there still" - and "they should act the same
    but are two separate entities." """
    job, sub_only = "265730", "265731"
    _file("invoice", job, "Split Supply Co", "sp-1", subject="FW: supplier invoice")
    _file("invoice", job, "Split Roofing Sub LLC", "sp-2", number="", subject="FW: sub - Deposit")
    _file("invoice", sub_only, "Split Roofing Sub LLC", "sp-3", number="", subject="FW: 2nd payment")
    (supplier_id, _), = _invoices("Split Supply Co")
    sub_ids = [i for i, flagged in _invoices("Split Roofing Sub LLC") if flagged]
    assert len(sub_ids) == 2

    supplier_side = client.get(f"/job/{job}").text
    assert f'href="/invoice/{supplier_id}"' in supplier_side
    assert not any(f'href="/invoice/{i}"' in supplier_side for i in sub_ids)
    assert f'href="/sub-invoices?job={job}"' in supplier_side      # one click across
    # Its totals are the supplier's $5,000, not $10,000 with the sub's draw in.
    assert "$10,000.00" not in supplier_side

    incoming = client.get("/incoming").text
    assert f'href="/job/{job}"' in incoming
    assert f'href="/job/{sub_only}"' not in incoming                # nothing of theirs there

    subs_side = client.get(f"/sub-invoices?job={job}").text
    assert f'href="/invoice/{sub_ids[0]}"' in subs_side
    assert f'href="/invoice/{supplier_id}"' not in subs_side


def test_back_from_a_subs_invoice_goes_to_subs_and_a_suppliers_to_the_job(outbox):
    """Zack: "when im in subs and click the back button to exit that job it
    brings me back to vendor dash instead of the sub dash." """
    job = "265732"
    _file("invoice", job, "Back Supply Co", "bk-1", subject="FW: supplier invoice")
    _file("invoice", job, "Back Roofing Sub LLC", "bk-2", subject="FW: sub invoice")
    (supplier_id, _), = _invoices("Back Supply Co")
    (sub_id, _), = _invoices("Back Roofing Sub LLC")

    assert f'<a href="/sub-invoices?job={job}">&larr; Subs' in client.get(f"/invoice/{sub_id}").text
    assert f'<a href="/job/{job}">&larr; Job' in client.get(f"/invoice/{supplier_id}").text

    one_job = client.get(f"/sub-invoices?job={job}").text
    assert '<a href="/sub-invoices">Subs</a>' in one_job
    assert f'href="/job/{job}"' not in one_job


def test_a_draw_waiting_for_approval_is_not_shown_as_nothing_billed(outbox):
    """Zack: "it counts it at zero. when invoice is for 25,000" - the folder
    said "$0 billed" because billed meant approved, and nobody had yet."""
    job = "265733"
    _file("invoice", job, "Zero Roofing Sub LLC", "zr-1", number="", subject="FW: sub - Deposit")

    folder = client.get("/sub-invoices").text.split(f'href="/sub-invoices?job={job}"', 1)[1][:1500]
    assert "$5,000.00 invoiced" in folder
    assert "1 to look at · $5,000.00" in folder
    assert "$0.00" not in folder


def test_a_contract_uploaded_after_the_invoice_takes_the_invoice_to_subs(outbox):
    """A sub's invoice filed before their contract stayed with the supplier
    invoices, marked as nobody's, until something re-read it."""
    job = "265734"
    _file("invoice", job, "Late Contract Roofing", "lc-1", subject="FW: invoice")
    assert _invoices("Late Contract Roofing")[0][1] is False

    _file("quote", job, "Late Contract Roofing", "lc-q", subject="FW: contract", sub_dept=True)

    (invoice_id, flagged), = _invoices("Late Contract Roofing")
    assert flagged is True
    assert f'href="/invoice/{invoice_id}"' not in client.get(f"/job/{job}").text
    assert f'href="/invoice/{invoice_id}"' in client.get(f"/sub-invoices?job={job}").text


# --- a sub's bill is a lump sum ------------------------------------------------------

# How a sub bills: the amount, and the scope underneath with no prices on it.
LUMP_SUM = [
    {"line_no": 1, "description": "Roof replacement, 144 Oldwoods Court - 2nd payment",
     "qty": "", "unit_price": "", "extended": ""},
    {"line_no": 2, "description": "Tear off and haul away, 2 layers",
     "qty": "", "unit_price": "", "extended": ""},
]


def test_a_subs_invoice_is_checked_as_a_lump_sum_against_the_contract(outbox):
    """Zack: "the subs are probably just gonna have one line saying the total
    of what their invoicing for" - no unit prices, so nothing for a line-by-line
    check to grade. A sub's bill is checked against the contract."""
    job = "265735"
    _file("quote", job, "Lump Sum Roofing LLC", "ls-q", subject="FW: sub contract")
    _file("invoice", job, "Lump Sum Roofing LLC", "ls-1", number="",
          subject="FW: 2nd payment", lines=LUMP_SUM)
    (invoice_id, flagged), = _invoices("Lump Sum Roofing LLC")
    assert flagged

    page = client.get(f"/invoice/{invoice_id}").text
    assert "Within the contract" in page
    assert "on the contract." in page                 # "... this leaves $X on the contract."
    assert "Every priced line matches" not in page
    assert '<span class="was">' not in page           # no "not on the quote" on scope lines
    assert "Same item as a quote line?" not in page
    # None of the supplier-bill wording about unmatched lines.
    assert "could not be matched to the master quote" not in page
    assert "those prices were not checked" not in page
    assert "Not on the master quote" not in page


def test_a_subs_invoice_with_no_contract_says_so(outbox):
    _file("invoice", "265736", "No Paper Roofing LLC", "npc-1", number="",
          subject="FW: sub - Deposit", lines=LUMP_SUM)
    (invoice_id, _), = _invoices("No Paper Roofing LLC")

    page = client.get(f"/invoice/{invoice_id}").text
    assert "No contract on file" in page
    assert '<span class="was">' not in page
    assert "No contract on file for No Paper Roofing LLC" in page
    assert "No quote on this job from" not in page       # a supplier's wording


def test_a_suppliers_invoice_is_still_checked_line_by_line(outbox):
    _file("invoice", "265737", "Line Priced Supply Co", "lp-1",
          subject="FW: supplier invoice", lines=LUMP_SUM)
    (invoice_id, _), = _invoices("Line Priced Supply Co")

    page = client.get(f"/invoice/{invoice_id}").text
    assert "No quote on this job yet" in page
    assert '<span class="was">' in page
    assert "contract" not in page.split('<div class="sheet">', 1)[1].split("vitems", 1)[0]


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

"""Provenance screening: is this bill actually ours?

The scenario these tests are written against is the one Zack described: a
stranger emails the finance mailbox a PDF of a bill with a covering note saying
it has been approved for payment. Nothing about the PDF is wrong. The price
matching finds no fault, because there is no fault - it is simply not our bill.

The tests that matter most are the negative ones. A screen that flags
everything is the same as a screen that flags nothing, because people stop
reading it. So: a known vendor at a known address, forwarded internally, must
come through completely silent.
"""
from __future__ import annotations

import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = Path(tempfile.mkdtemp(prefix="finance-trust-"))
os.environ.setdefault("DATA_DIR", str(_TMP))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP / 'trust.db'}")
os.environ.setdefault("APP_PASSWORD", "")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app import trust  # noqa: E402
from app.db import Base, SessionLocal, engine, init_db  # noqa: E402
from app.models import (  # noqa: E402
    APPROVAL_APPROVED,
    APPROVAL_PENDING,
    Document,
    Invoice,
    Job,
    Quote,
)

D = Decimal


@pytest.fixture()
def session():
    init_db()
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
    s = SessionLocal()
    yield s
    s.close()


_counter = {"n": 0}


def _doc(session, **kw) -> Document:
    _counter["n"] += 1
    n = _counter["n"]
    doc = Document(
        filename=kw.pop("filename", f"doc{n}.pdf"),
        sha256=f"{n:064d}",
        stored_path=f"/tmp/doc{n}.pdf",
        source=kw.pop("source", "email"),
        **kw,
    )
    session.add(doc)
    session.flush()
    return doc


def _job(session, number="260000") -> Job:
    job = session.query(Job).filter_by(job_number=number).one_or_none()
    if job is None:
        job = Job(job_number=number)
        session.add(job)
        session.flush()
    return job


def _known_vendor(session, vendor: str, sender: str) -> None:
    """Give the database the history a real supplier would have: a quote we
    asked for, delivered from an address they have used."""
    doc = _doc(session, sender=sender, job_id=_job(session).id)
    session.add(Quote(
        job_id=doc.job_id, document_id=doc.id, vendor=vendor, is_master=True,
    ))
    session.flush()


# --- the quiet case, which matters most -----------------------------------

def test_a_known_vendor_from_a_known_address_says_nothing(session):
    _known_vendor(session, "ABC Supply Co", "billing@abcsupply.com")

    incoming = _doc(session, sender="billing@abcsupply.com",
                    subject="Invoice 88213", body_text="Attached please find.")
    assert trust.screen(session, incoming, "ABC Supply Co") == []


def test_forwarded_from_inside_the_company_is_not_a_stranger(session):
    """Staff forward vendor mail in constantly. The sending address is then
    ours, which says nothing about the vendor - so the sender checks must not
    fire on it."""
    _known_vendor(session, "ABC Supply Co", "billing@abcsupply.com")

    forwarded = _doc(session, sender="zmabry@addventuresinc.com",
                     subject="Fwd: Invoice 88213 - job 260000")
    flags = trust.screen(session, forwarded, "ABC Supply Co",
                         own_domains={"addventuresinc.com"})
    assert flags == []


def test_a_mail_subdomain_is_the_same_supplier(session):
    _known_vendor(session, "ABC Supply Co", "billing@abcsupply.com")
    incoming = _doc(session, sender="no-reply@mail.abcsupply.com")
    assert trust.screen(session, incoming, "ABC Supply Co") == []


# --- the scam Zack described ----------------------------------------------

def test_approved_for_payment_from_a_stranger_blocks(session):
    _known_vendor(session, "ABC Supply Co", "billing@abcsupply.com")

    fake = _doc(
        session,
        sender="accounts@nationalbuildingservices-inv.com",
        subject="Invoice 4471 - approved for payment",
        body_text="This bill has been approved to be paid. Please remit.",
    )
    flags = trust.screen(session, fake, "National Building Services")
    codes = {f.code for f in flags}

    assert trust.NEW_VENDOR in codes
    assert trust.SENDER_UNKNOWN in codes
    assert trust.PRESSURE_LANGUAGE in codes
    # The combination is the point: any one alone is ordinary, together they
    # are the shape of a fake invoice.
    assert trust.UNSOLICITED_BILL in codes
    assert trust.blocking(flags)


def test_a_first_invoice_from_a_genuinely_new_vendor_only_warns(session):
    """New suppliers are normal. Being new is not an accusation - it must not
    block, or the first bill from every new vendor needs a sign-off."""
    _known_vendor(session, "ABC Supply Co", "billing@abcsupply.com")

    first = _doc(session, sender="ar@coastalroofingsupply.com",
                 subject="Invoice 1201 for job 260000")
    flags = trust.screen(session, first, "Coastal Roofing Supply")

    assert {f.code for f in flags} == {trust.NEW_VENDOR, trust.SENDER_UNKNOWN}
    assert trust.blocking(flags) == []


def test_a_known_vendor_billing_from_a_new_address_blocks(session):
    """The expensive one. Somebody registers a domain, sends an invoice under
    a supplier's name, and the price matching has nothing to complain about."""
    _known_vendor(session, "ABC Supply Co", "billing@abcsupply.com")

    spoof = _doc(session, sender="billing@abc-supply-invoices.com")
    flags = trust.screen(session, spoof, "ABC Supply Co")

    assert [f.code for f in flags] == [trust.SENDER_MISMATCH]
    assert trust.blocking(flags)


def test_a_lookalike_domain_is_called_out_as_such(session):
    _known_vendor(session, "ABC Supply Co", "billing@abcsupply.com")

    spoof = _doc(session, sender="billing@abcsuppy.com")
    flags = trust.screen(session, spoof, "Some Other Vendor")
    codes = {f.code for f in flags}

    assert trust.LOOKALIKE_SENDER in codes
    assert trust.blocking(flags)


def test_changed_bank_details_block_whoever_sent_them(session):
    """Business email compromise. The only safe response is a phone call, so
    this blocks even when everything else about the sender checks out."""
    _known_vendor(session, "ABC Supply Co", "billing@abcsupply.com")

    incoming = _doc(
        session,
        sender="billing@abcsupply.com",
        subject="Invoice 88213",
        body_text="Note our banking details have changed. Please use the account below.",
    )
    flags = trust.screen(session, incoming, "ABC Supply Co")

    assert [f.code for f in flags] == [trust.REMITTANCE_CHANGE]
    assert trust.blocking(flags)


def test_a_supply_house_does_not_bill_from_gmail(session):
    _known_vendor(session, "ABC Supply Co", "billing@abcsupply.com")
    incoming = _doc(session, sender="joesroofingsupply@gmail.com")
    codes = {f.code for f in trust.screen(session, incoming, "Joes Roofing Supply")}
    assert trust.FREEMAIL_SENDER in codes


# --- where trust comes from -----------------------------------------------

def test_an_unapproved_invoice_does_not_make_its_own_vendor_known(session):
    """If it did, the first fake invoice would whitelist the second."""
    job = _job(session)
    doc = _doc(session, sender="accounts@stranger.com", job_id=job.id)
    session.add(Invoice(
        job_id=job.id, document_id=doc.id, vendor="Stranger Supply",
        invoice_number="1", approval_status=APPROVAL_PENDING,
    ))
    session.flush()

    assert "stranger supply" not in trust.known_vendors(session)

    second = _doc(session, sender="accounts@stranger.com")
    codes = {f.code for f in trust.screen(session, second, "Stranger Supply")}
    assert trust.NEW_VENDOR in codes


def test_a_human_approval_does_make_a_vendor_known(session):
    job = _job(session)
    doc = _doc(session, sender="ar@coastalroofingsupply.com", job_id=job.id)
    session.add(Invoice(
        job_id=job.id, document_id=doc.id, vendor="Coastal Roofing Supply",
        invoice_number="1", approval_status=APPROVAL_APPROVED,
    ))
    session.flush()

    assert "coastal roofing supply" in trust.known_vendors(session)

    second = _doc(session, sender="ar@coastalroofingsupply.com")
    assert trust.screen(session, second, "Coastal Roofing Supply") == []


def test_an_upload_from_the_office_is_not_screened_on_its_sender(session):
    """A file dragged into the browser has no sender at all."""
    _known_vendor(session, "ABC Supply Co", "billing@abcsupply.com")
    uploaded = _doc(session, source="upload", sender="")
    assert trust.screen(session, uploaded, "ABC Supply Co") == []


# --- clearing a flag -------------------------------------------------------

def test_clearing_signs_the_flag_rather_than_deleting_it(session):
    _known_vendor(session, "ABC Supply Co", "billing@abcsupply.com")
    spoof = _doc(session, sender="billing@abc-supply-invoices.com")
    spoof.trust_json = trust.dump(trust.screen(session, spoof, "ABC Supply Co"))
    session.flush()

    assert trust.blocking(trust.flags_for(spoof))

    assert trust.clear(spoof, "Zack (called ABC, new billing system)", "04 Sep 2026") == 1

    after = trust.flags_for(spoof)
    assert trust.blocking(after) == []          # no longer stops approval
    assert len(after) == 1                       # but the record survives
    assert after[0].cleared
    assert "called ABC" in after[0].cleared_by

    # Clearing twice is not an error, and does not double-sign.
    assert trust.clear(spoof, "Somebody Else", "05 Sep 2026") == 0


def test_bad_stored_json_is_not_a_crash(session):
    doc = _doc(session, sender="x@y.com")
    doc.trust_json = "not json at all"
    assert trust.flags_for(doc) == []


# --- the domain helpers ----------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("billing@abcsupply.com", "abcsupply.com"),
    ("ABC Supply <Billing@ABCSupply.COM>", "abcsupply.com"),
    ("billing@abcsupply.com.", "abcsupply.com"),
    ("no-at-sign", ""),
    ("", ""),
])
def test_domain_of(raw, expected):
    assert trust.domain_of(raw) == expected


def test_edit_distance_gives_up_rather_than_grinding():
    assert trust.edit_distance("abcsupply.com", "abcsuppy.com") == 1
    assert trust.edit_distance("abcsupply.com", "newcastlebp.com", ceiling=2) == 3


@pytest.mark.parametrize("body", [
    "Hey, this bill is approved to be paid.",
    "Invoice approved for payment - please process.",
    "This has been authorized for payment by the office.",
    "Cleared for payment, remit at your earliest convenience.",
    "URGENT PAYMENT required today.",
])
def test_the_wording_these_actually_arrive_in(session, body):
    _known_vendor(session, "ABC Supply Co", "billing@abcsupply.com")
    doc = _doc(session, sender="ar@somewhere-else.com", body_text=body)
    codes = {f.code for f in trust.screen(session, doc, "Somewhere Else Inc")}
    assert trust.PRESSURE_LANGUAGE in codes
    assert trust.UNSOLICITED_BILL in codes


@pytest.mark.parametrize("body", [
    "Attached is invoice 88213 for job 260000.",
    "Please find the invoice for the Winding Ridge delivery.",
    "Here is the revised quote per our call this morning.",
])
def test_ordinary_vendor_mail_is_not_treated_as_pressure(session, body):
    _known_vendor(session, "ABC Supply Co", "billing@abcsupply.com")
    doc = _doc(session, sender="billing@abcsupply.com", body_text=body)
    assert trust.screen(session, doc, "ABC Supply Co") == []

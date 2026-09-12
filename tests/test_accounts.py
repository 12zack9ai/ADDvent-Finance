"""Everyone signs in as themselves, and their paperwork goes with the sub.

Zack, 2026-09-12: "When you first go in, you should register an account.
Should be an addventuresinc.com email. That way when someone approved something
it auto registers to their account. You don't have to manually type in who's
approving the bill." And, on the vendor file: "we should be uploading the
contract, lien waivers, and all that crap with it" - warn-only, no blocking yet.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, unquote_plus, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = Path(tempfile.mkdtemp(prefix="finance-accounts-"))
os.environ["DATA_DIR"] = str(_TMP)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["APP_PASSWORD"] = ""
os.environ["ANTHROPIC_API_KEY"] = "test"

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import auth, mail_send, services  # noqa: E402
from app import main as appmain  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.extract import ExtractionResult  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Invoice, User, VendorDocument  # noqa: E402

ZACK = "Zack Mabry <zmabry@addventuresinc.com>"
GOOD = "correct horse 1"


def setup_module(_module) -> None:
    # Never drop_all: every test module shares one engine (see test_compare).
    init_db()


@pytest.fixture()
def outbox(monkeypatch):
    sent: list = []
    monkeypatch.setattr(mail_send, "send", sent.append)
    monkeypatch.setattr(settings, "can_send_mail", lambda: True)
    monkeypatch.setattr(settings, "smtp_settings",
                        lambda: ("smtp.example", 587, "u", "p", "aifinance@addventuresinc.com"))
    monkeypatch.setattr(settings, "owner_emails_raw", "nobody-owns-this@addventuresinc.com")
    monkeypatch.setattr(appmain, "_accounts_live", False)
    auth.forget_attempts()
    return sent


def _link(outbox, path: str) -> str:
    """The newest emailed link to `path`, as a path the test client can open."""
    for msg in reversed(outbox):
        found = re.search(r"https?://\S+" + re.escape(path) + r"\?t=\S+", msg.get_content())
        if found:
            token = parse_qs(urlparse(found.group(0)).query)["t"][0]
            return f"{path}?t={token}"
    raise AssertionError(f"no {path} link was emailed")


def _register(client, email, name="Dana Office", password=GOOD):
    return client.post("/register", follow_redirects=False, data={
        "name": name, "email": email, "password": password, "confirm": password})


def _signed_up(email, outbox, name="Dana Office", password=GOOD) -> TestClient:
    client = TestClient(app)
    _register(client, email, name=name, password=password)
    client.get(_link(outbox, "/verify"), follow_redirects=False)
    return client


def _file(doc_type, job, vendor, tag, subject) -> int:
    payload = {
        "doc_type": doc_type, "vendor": vendor, "document_number": tag,
        "document_date": "2026-09-12", "total": "5000", "subtotal": "5000",
        "job_number_hint": "",
        "lines": [{"line_no": 1, "description": "Gutters and leaders, install",
                   "qty": "1", "unit_price": "5000", "extended": "5000"}],
    }
    path = _TMP / f"{tag}.pdf"
    path.write_bytes(b"%PDF-1.4\n% " + tag.encode() + b"\n%%EOF\n")
    with SessionLocal() as session:
        doc = services.ingest_file(
            session, path, f"{tag}.pdf", source="email", sender=ZACK, subject=subject,
            body=f"job {job}", message_id=f"<{tag}@x>",
            extraction=ExtractionResult(payload=payload, model="test"))
        session.commit()
        return session.scalar(select(Invoice.id).where(Invoice.document_id == doc.id)) or doc.id


def _where(response) -> str:
    # The error rides in the query string, "+" for every space.
    return unquote_plus(response.headers.get("location", ""))


# --- creating an account -------------------------------------------------------

def test_the_sign_in_page_offers_an_account_and_a_forgotten_password(outbox):
    body = TestClient(app).get("/login").text
    assert 'href="/register"' in body and 'href="/forgot"' in body


def test_only_a_company_email_can_register(outbox):
    r = _register(TestClient(app), "dana@gmail.com")
    assert "@addventuresinc.com" in _where(r)
    assert outbox == []
    with SessionLocal() as s:
        assert s.scalar(select(User).where(User.email == "dana@gmail.com")) is None


def test_an_account_signs_in_only_after_its_email_is_confirmed(outbox):
    client = TestClient(app)
    r = _register(client, "pat@addventuresinc.com", name="Pat Field")
    assert r.status_code == 200 and "pat@addventuresinc.com" in r.text      # "check your email"
    assert outbox[-1]["To"] == "pat@addventuresinc.com"

    r = client.post("/login", follow_redirects=False,
                    data={"email": "pat@addventuresinc.com", "password": GOOD})
    assert "Confirm your email" in _where(r)
    assert auth.who_from_token(client.cookies.get(auth.COOKIE_NAME)) is None

    client.get(_link(outbox, "/verify"), follow_redirects=False)
    person = auth.who_from_token(client.cookies.get(auth.COOKIE_NAME))
    assert person and person["name"] == "Pat Field"


def test_passwords_are_stored_scrambled(outbox):
    _register(TestClient(app), "lee@addventuresinc.com", password="plain text 99")
    with SessionLocal() as s:
        stored = s.scalar(select(User).where(User.email == "lee@addventuresinc.com")).password_hash
    assert stored.startswith("scrypt$") and "plain text 99" not in stored
    assert auth.check_password("plain text 99", stored)
    assert not auth.check_password("plain text 98", stored)


# --- nobody types their name -------------------------------------------------------

def test_an_approval_is_signed_by_the_account_not_a_typed_name(outbox):
    client = _signed_up("sam@addventuresinc.com", outbox, name="Sam Approver")
    invoice_id = _file("invoice", "265761", "Account Supply Co", "acc-1",
                       "FW: supplier invoice 265761")

    page = client.get(f"/invoice/{invoice_id}").text
    form = page.split(f'action="/invoice/{invoice_id}/decide"', 1)[1].split("</form>", 1)[0]
    assert 'name="actor"' not in form                        # no name box to fill in
    assert "Sam Approver" in client.get("/incoming").text   # and it says who is signed in

    client.post(f"/invoice/{invoice_id}/decide", follow_redirects=False,
                data={"decision": "approve", "actor": "Somebody Else"})
    with SessionLocal() as s:
        assert s.get(Invoice, invoice_id).approved_by == "Sam Approver"


def test_the_shared_password_stops_once_the_owner_account_is_confirmed(outbox, monkeypatch):
    monkeypatch.setattr(settings, "app_password", "shared-office-pass")
    monkeypatch.setattr(settings, "owner_emails_raw", "boss@addventuresinc.com")

    office = TestClient(app)
    r = office.post("/login", follow_redirects=False, data={"password": "shared-office-pass"})
    assert r.headers["location"] == "/"
    assert office.get("/incoming", follow_redirects=False).status_code == 200

    owner = _signed_up("boss@addventuresinc.com", outbox, name="The Boss")
    with SessionLocal() as s:
        assert s.scalar(select(User).where(User.email == "boss@addventuresinc.com")).role == "owner"

    assert office.get("/incoming", follow_redirects=False).status_code == 303   # old session ends
    r = TestClient(app).post("/login", follow_redirects=False, data={"password": "shared-office-pass"})
    assert "email and password" in _where(r)
    assert owner.get("/incoming", follow_redirects=False).status_code == 200


def test_a_forgotten_password_is_reset_by_an_emailed_link_that_works_once(outbox):
    _signed_up("rae@addventuresinc.com", outbox, name="Rae", password="first password 1")

    client = TestClient(app)
    client.post("/forgot", data={"email": "rae@addventuresinc.com"})
    token = _link(outbox, "/reset").split("t=", 1)[1]
    r = client.post("/reset", follow_redirects=False,
                    data={"t": token, "password": "second password 2", "confirm": "second password 2"})
    assert r.headers["location"].startswith("/")

    again = TestClient(app).post("/reset", follow_redirects=False,
                                 data={"t": token, "password": "third password 3",
                                       "confirm": "third password 3"})
    assert _where(again).startswith("/forgot")                       # the link is spent

    fresh = TestClient(app)
    old = fresh.post("/login", follow_redirects=False,
                     data={"email": "rae@addventuresinc.com", "password": "first password 1"})
    assert "don't match" in _where(old)
    new = fresh.post("/login", follow_redirects=False,
                     data={"email": "rae@addventuresinc.com", "password": "second password 2"})
    assert new.headers["location"] == "/"


# --- the sub's paperwork ----------------------------------------------------------

def test_paperwork_is_kept_with_the_sub_and_signed_by_the_account(outbox):
    client = _signed_up("ap@addventuresinc.com", outbox, name="Ann Payable")
    _file("quote", "265760", "Paper Roofing LLC", "acc-q", "FW: sub contract 265760")

    client.post("/paperwork", follow_redirects=False,
                data={"vendor": "Paper Roofing LLC", "job_number": "265760", "kind": "lien_waiver",
                      "scope": "job", "note": "Conditional, draw 2",
                      "next": "/sub-invoices?job=265760"},
                files={"file": ("waiver.pdf", b"%PDF-1.4 waiver", "application/pdf")})
    soon = (date.today() + timedelta(days=10)).isoformat()
    client.post("/paperwork", follow_redirects=False,
                data={"vendor": "Paper Roofing LLC", "kind": "insurance", "scope": "all",
                      "expires_on": soon, "next": "/sub-invoices?job=265760"},
                files={"file": ("coi.pdf", b"%PDF-1.4 coi", "application/pdf")})

    page = client.get("/sub-invoices?job=265760").text
    assert "Lien waiver" in page and "Insurance certificate" in page
    assert "expires in 10 days" in page and "Conditional, draw 2" in page
    assert "Ann Payable" in page

    with SessionLocal() as s:
        papers = s.scalars(select(VendorDocument).where(VendorDocument.vendor == "Paper Roofing LLC")).all()
    assert {p.uploaded_by for p in papers} == {"Ann Payable"}
    waiver = next(p for p in papers if p.kind == "lien_waiver")
    assert client.get(f"/paperwork/{waiver.id}/file").content == b"%PDF-1.4 waiver"

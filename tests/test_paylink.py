"""Invoices that arrive as a QuickBooks "View and pay" link instead of a PDF.

Zack forwarded one from Superior Seamless Gutters: "Your invoice is ready!",
a View and pay button, nothing attached - and the mailbox skipped it. Every
figure used here is invented; the real invoice is never committed.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from email.message import EmailMessage
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = Path(tempfile.mkdtemp(prefix="finance-paylink-"))
os.environ["DATA_DIR"] = str(_TMP)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["APP_PASSWORD"] = ""
os.environ["ANTHROPIC_API_KEY"] = "test"

from sqlalchemy import select  # noqa: E402

from app import mail_imap, paylink, services  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Invoice, Job  # noqa: E402

TRACK = "https://links.notification.intuit.com/ss/c/u001.abc/4ty/xyz/h1/h001.q"
PAGE = "https://connect.intuit.com/t/scs-v1-0123456789abcdef-1?cta=viewinvoicenow"
UNSUB = "https://links.notification.intuit.com/ss/c/u001.unsub/4ty/xyz/h1/h001.u"


def setup_module(_module) -> None:
    # Never drop_all: every test module shares one engine (see test_compare).
    init_db()


def page(*, sale_type="INVOICE", number="10001", vendor="Example Gutters Inc",
         description="Gutters (6in) & leaders (3x4)\nBuilding 5", amount=4200):
    """An Intuit customer page, shaped like the real one and filled with fakes."""
    data = {"props": {"initialReduxState": {
        "companyInfo": {"companyName": vendor, "sourceOffering": "QBO"},
        "sale": {
            "type": sale_type,
            "referenceNumber": number,
            "amount": amount,
            "txnDate": "09-11-2026",
            "receivable": {"dueDate": "10-11-2026", "balance": amount},
            "saleTerm": {"name": "Net 30"},
            "tax": {"totalTaxAmount": 0},
            "lines": [
                {"sequence": 1, "type": "SalesItemLineDetail", "description": description,
                 "amount": amount, "quantity": 1, "rate": {"moneyValue": amount},
                 "item": {"name": "GL Large"}},
                {"sequence": None, "type": "SubTotalLineDetail", "amount": amount},
            ],
        },
    }}}
    return ("<!DOCTYPE html><html><head><title>Intuit QuickBooks</title></head>"
            '<body><div id="__next"></div><script id="__NEXT_DATA__" type="application/json">'
            f"{json.dumps(data)}</script></body></html>")


# --- which links are opened --------------------------------------------------

def test_only_links_that_say_view_or_pay_on_intuit_are_opened():
    html = (
        f'<a href="{TRACK.replace("q", "q&amp;x=1")}">View and pay</a>'
        f'<a href="{UNSUB}">Unsubscribe</a>'
        '<a href="https://links.notification.intuit.com/ss/c/priv">Privacy</a>'
        '<a href="https://www.affirm.com/apps">Get Started</a>'
        '<a href="http://connect.intuit.com/t/scs-v1-x">View invoice</a>'
        '<a href="https://connect.intuit.com.evil.example/t/x">View and pay</a>'
        '<a href="https://evil.example/?u=https://connect.intuit.com/t/x">Pay now</a>'
    )
    assert paylink.find_links(html) == [TRACK.replace("q", "q&x=1")]


def test_a_plain_text_body_only_yields_a_direct_invoice_page():
    text = f"View and pay: {TRACK}\nOr open {PAGE}."
    assert paylink.find_links("", text) == [PAGE]


def test_an_unsubscribe_link_is_never_opened_even_if_it_says_view():
    html = f'<a href="{UNSUB}">View our unsubscribe options</a>'
    assert paylink.find_links(html) == []


# --- reading the page ----------------------------------------------------------

def test_the_invoice_is_read_straight_off_the_page():
    payload = paylink.parse(page(number="10002", amount=4200))
    assert payload["doc_type"] == "invoice"
    assert payload["vendor"] == "Example Gutters Inc"
    assert payload["document_number"] == "10002"
    assert payload["document_date"] == "2026-09-11"
    assert payload["due_date"] == "2026-10-11"
    assert payload["terms"] == "Net 30"
    assert payload["total"] == "4200" and payload["subtotal"] == "4200"
    assert payload["lines"] == [{
        "line_no": 1, "sku": "GL Large",
        "description": "Gutters (6in) & leaders (3x4)\nBuilding 5",
        "qty": "1", "unit_price": "4200", "extended": "4200",
    }]


def test_a_job_number_written_on_a_line_is_carried_as_a_hint():
    payload = paylink.parse(page(description="Gutters - job 265499"))
    assert payload["job_number_hint"] == "265499"


@pytest.mark.parametrize("html", [
    page(sale_type="ESTIMATE"),
    page(number=""),
    "<html><body>Sign in to QuickBooks</body></html>",
    '<script id="__NEXT_DATA__">{not json</script>',
])
def test_a_page_that_is_not_an_invoice_reads_as_none(html):
    assert paylink.parse(html) is None


# --- fetching ------------------------------------------------------------------

def _serve(monkeypatch, routes: dict, opened: list):
    """Answer requests from `routes` - url -> (status, headers, body)."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        opened.append(url)
        assert request.method == "GET"
        status, headers, body = routes.get(url, (404, {}, ""))
        return httpx.Response(status, headers=headers, text=body)

    monkeypatch.setattr(paylink, "_client", lambda: httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=False))


def test_intuit_redirects_are_followed_to_the_page(monkeypatch):
    opened: list = []
    _serve(monkeypatch, {TRACK: (302, {"location": PAGE}, ""), PAGE: (200, {}, page())}, opened)
    assert paylink.read(TRACK)["document_number"] == "10001"
    assert opened == [TRACK, PAGE]


def test_a_redirect_away_from_intuit_is_refused_before_it_is_taken(monkeypatch):
    opened: list = []
    _serve(monkeypatch, {TRACK: (302, {"location": "https://evil.example/pay"}, "")}, opened)
    with pytest.raises(paylink.PaylinkError) as caught:
        paylink.read(TRACK)
    assert not caught.value.retry
    assert opened == [TRACK]


def test_intuit_being_down_is_worth_trying_again(monkeypatch):
    _serve(monkeypatch, {PAGE: (503, {}, "")}, [])
    with pytest.raises(paylink.PaylinkError) as caught:
        paylink.read(PAGE)
    assert caught.value.retry


def test_a_page_far_larger_than_an_invoice_is_refused(monkeypatch):
    _serve(monkeypatch, {PAGE: (200, {}, "x" * (paylink.MAX_BYTES + 10))}, [])
    with pytest.raises(paylink.PaylinkError):
        paylink.read(PAGE)


# --- the mailbox, end to end -----------------------------------------------------

class FakeMailbox:
    """Just enough of ImapMailbox for poll_once."""

    messages: dict = {}
    filed_away: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def select_inbox(self):
        pass

    def unread_ids(self, limit):
        return list(self.messages)[:limit]

    def fetch(self, msg_id):
        return self.messages[msg_id]

    def file_away(self, msg_id, folder):
        FakeMailbox.filed_away.append(msg_id)
        FakeMailbox.messages.pop(msg_id, None)


def _forward(subject: str, links=(("View and pay", TRACK), ("Unsubscribe", UNSUB))):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "Zack Mabry <zmabry@addventuresinc.com>"
    msg["Message-ID"] = f"<{uuid.uuid4()}@addventuresinc.com>"
    msg.set_content("Forwarded invoice")
    anchors = "".join(f'<p><a href="{url}">{label}</a></p>' for label, url in links)
    msg.add_alternative(f"<html><body><h1>Your invoice is ready!</h1>{anchors}</body></html>",
                        subtype="html")
    return msg


@pytest.fixture()
def mailbox(monkeypatch):
    FakeMailbox.messages = {}
    FakeMailbox.filed_away = []
    monkeypatch.setattr(mail_imap, "ImapMailbox", FakeMailbox)

    def no_model(*_a, **_k):
        raise AssertionError("a pay-link invoice must never be sent to the model")

    monkeypatch.setattr(services, "extract_document", no_model)
    return FakeMailbox


def _poll():
    with SessionLocal() as session:
        return mail_imap.poll_once(session)


def test_a_forwarded_pay_link_is_filed_to_the_job_in_the_subject(monkeypatch, mailbox):
    opened: list = []
    _serve(monkeypatch, {
        TRACK: (302, {"location": PAGE}, ""),
        PAGE: (200, {}, page(number="10003", amount=4200)),
    }, opened)
    mailbox.messages = {b"1": _forward("Fwd: 265401 gutter invoice")}

    result = _poll()

    assert any("from the pay link" in item and "job 265401" in item for item in result.filed)
    assert UNSUB not in opened                     # never so much as requested
    assert mailbox.filed_away == [b"1"]
    with SessionLocal() as session:
        job = session.scalar(select(Job).where(Job.job_number == "265401"))
        invoice = session.scalar(select(Invoice).where(Invoice.job_id == job.id))
        assert invoice.invoice_number == "10003"
        assert invoice.vendor == "Example Gutters Inc"
        assert str(invoice.total) in ("4200", "4200.00", "4200.0000")
        assert [line.sku for line in invoice.lines] == ["GL Large"]
        assert invoice.document.mime_type == "text/html"


def test_the_same_invoice_again_is_already_received(monkeypatch, mailbox):
    routes = {TRACK: (302, {"location": PAGE}, ""), PAGE: (200, {}, page(number="10004"))}
    _serve(monkeypatch, routes, [])
    mailbox.messages = {b"1": _forward("Fwd: 265402 invoice")}
    _poll()
    mailbox.messages = {b"2": _forward("Fwd: 265402 invoice - reminder")}

    result = _poll()

    assert any("already received" in item for item in result.skipped)
    assert mailbox.filed_away == [b"1", b"2"]


def test_mail_is_left_for_the_next_poll_when_intuit_is_down(monkeypatch, mailbox):
    _serve(monkeypatch, {TRACK: (503, {}, "")}, [])
    mailbox.messages = {b"1": _forward("Fwd: 265403 invoice")}

    result = _poll()

    assert result.errors and "QuickBooks pay link" in result.errors[0]
    assert mailbox.filed_away == []                # still unread, tried again next time


def test_an_email_with_nothing_to_read_says_so(monkeypatch, mailbox):
    _serve(monkeypatch, {}, [])
    mailbox.messages = {b"1": _forward("Lunch friday?", links=())}

    result = _poll()

    assert result.skipped == ["Lunch friday? (no attachment and no invoice link)"]
    assert mailbox.filed_away == [b"1"]

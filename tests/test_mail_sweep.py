"""Nothing sent to the finance mailbox goes unread, and a miss is never silent.

Zack, 2026-09-11: "i sent an email at 7:04 new quote came in. it was not
uploaded. we need to implement some sort of background check on these so no
invoice or quote gets missed." It was read at 7:05 and skipped - his iPhone
attached the PDF "inline", and the reader took it for a signature logo - and
the only trace was "1 skipped" in a log.
"""
from __future__ import annotations

import os
import sys
import tempfile
from email.message import EmailMessage
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = Path(tempfile.mkdtemp(prefix="finance-sweep-"))
os.environ["DATA_DIR"] = str(_TMP)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["APP_PASSWORD"] = ""
os.environ["ANTHROPIC_API_KEY"] = "test"

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import alerts, mail_imap, mail_send, services  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.extract import ExtractionResult  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Document, MailSeen, Quote  # noqa: E402

client = TestClient(app)


def setup_module(_module) -> None:
    # Never drop_all: every test module shares one engine (see test_compare).
    init_db()


def pdf(tag: str) -> bytes:
    return b"%PDF-1.4\n% " + tag.encode() + b"\n" + b"0" * 2000 + b"\n%%EOF\n"


def iphone_forward(subject: str, msgid: str, attachment: bytes | None = None,
                   filename: str = "Quote.pdf") -> EmailMessage:
    """What Apple Mail sends: the PDF shown inside the message - inline, with a Content-ID."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "Zack Mabry <zmabry@addventuresinc.com>"
    msg["Message-ID"] = msgid
    msg["Date"] = "Fri, 11 Sep 2026 07:04:00 -0400"
    msg.set_content("Forwarded from my iPhone")
    if attachment is not None:
        maintype, subtype = ("application", "pdf") if filename.endswith(".pdf") else ("image", "jpeg")
        msg.add_attachment(attachment, maintype=maintype, subtype=subtype,
                           filename=filename, disposition="inline")
        for part in msg.walk():
            if part.get_filename():
                part["Content-ID"] = f"<{filename}@icloud.com>"
    return msg


# --- the reader --------------------------------------------------------------

def test_an_inline_pdf_is_read_like_any_attachment():
    msg = iphone_forward("Fwd: quote", "<a1@icloud.com>", pdf("a1"))
    assert [name for name, _ in mail_imap._attachments(msg)] == ["Quote.pdf"]


def test_a_photo_shown_inline_is_read_but_a_small_logo_is_not():
    photo = iphone_forward("receipt", "<a2@icloud.com>", b"\xff\xd8" + b"1" * 200_000,
                           filename="IMG_0412.jpeg")
    logo = iphone_forward("hello", "<a3@icloud.com>", b"\xff\xd8" + b"1" * 3_000,
                          filename="logo.jpeg")
    assert [name for name, _ in mail_imap._attachments(photo)] == ["IMG_0412.jpeg"]
    assert list(mail_imap._attachments(logo)) == []


# --- a stand-in mailbox ----------------------------------------------------------

class FakeMailbox:
    """Folders of {id: (message, seen)}; enough of ImapMailbox for poll and sweep."""

    folders: dict = {}
    current = "INBOX"
    fetched: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _box(self):
        return FakeMailbox.folders.setdefault(FakeMailbox.current, {})

    def select_inbox(self):
        FakeMailbox.current = "INBOX"

    def select_folder(self, name):
        FakeMailbox.current = name
        return name in FakeMailbox.folders

    def unread_ids(self, limit):
        return [k for k, (_m, seen) in self._box().items() if not seen][:limit]

    def ids_since(self, days, seen_only=False):
        return [k for k, (_m, seen) in self._box().items() if seen or not seen_only]

    def message_key(self, msg_id):
        return mail_imap._message_key(self._box()[msg_id][0])

    def fetch(self, msg_id):
        FakeMailbox.fetched.append((FakeMailbox.current, msg_id))
        return self._box()[msg_id][0]

    def file_away(self, msg_id, folder):
        message, _seen = self._box().pop(msg_id)
        FakeMailbox.folders.setdefault(folder, {})[b"moved-" + msg_id] = (message, True)


QUOTE = {
    "doc_type": "quote", "vendor": "Example Supply Co", "document_date": "2026-09-11",
    "total": "1200", "subtotal": "1200", "job_number_hint": "",
    "lines": [{"line_no": 1, "sku": "SH-1", "description": "Shingles", "qty": "10",
               "uom": "BD", "unit_price": "120", "extended": "1200"}],
}


@pytest.fixture()
def mailbox(monkeypatch):
    FakeMailbox.folders = {"INBOX": {}, "Processed": {}}
    FakeMailbox.current = "INBOX"
    FakeMailbox.fetched = []
    monkeypatch.setattr(mail_imap, "ImapMailbox", FakeMailbox)
    monkeypatch.setattr(settings, "mail_processed_folder", "Processed")

    counter = {"n": 0}

    def read_as_quote(stored, hint=""):
        counter["n"] += 1
        return ExtractionResult(payload=dict(QUOTE, document_number=f"Q-{counter['n']}-{hint[:12]}"),
                                model="test")

    monkeypatch.setattr(services, "extract_document", read_as_quote)
    # One document per file: segmenting a scan is not what is under test.
    monkeypatch.setattr(mail_imap, "ingest_scan",
                        lambda session, path, name, **kw: [services.ingest_file(session, path, name, **kw)])
    return FakeMailbox


@pytest.fixture()
def sent(monkeypatch):
    """Capture alert emails instead of sending them."""
    outbox: list = []
    monkeypatch.setattr(alerts, "where_to", lambda: "zmabry@addventuresinc.com")
    monkeypatch.setattr(settings, "can_send_mail", lambda: True)
    monkeypatch.setattr(settings, "smtp_settings",
                        lambda: ("smtp.example", 587, "u", "p", "aifinance@addventuresinc.com"))
    monkeypatch.setattr(mail_send, "send", outbox.append)
    return outbox


def _row(key):
    with SessionLocal() as session:
        return session.scalar(select(MailSeen).where(MailSeen.message_key == key))


def _poll():
    with SessionLocal() as session:
        return mail_imap.poll_once(session)


def _sweep():
    with SessionLocal() as session:
        return mail_imap.sweep_once(session)


# --- recording, and speaking up ----------------------------------------------------

def test_the_7_04_quote_is_filed_now(mailbox, sent):
    mailbox.folders["INBOX"][b"1"] = (
        iphone_forward("Fwd: new quote 265501", "<q1@icloud.com>", pdf("q1")), False)

    result = _poll()

    assert any("job 265501" in item for item in result.filed)
    assert _row("<q1@icloud.com>").outcome == "filed"
    # Filed, so no "not filed" alert - only the one-time question about a
    # vendor nobody has classified yet (vendor_roles.py).
    assert not [m for m in sent if "Not filed" in str(m["Subject"])]
    assert [m for m in sent if str(m["Subject"]).startswith("Sub or supplier?")]


def test_a_message_not_filed_is_recorded_and_emailed_about_once(mailbox, sent):
    mailbox.folders["INBOX"][b"1"] = (iphone_forward("Fwd: quote?", "<s1@icloud.com>"), False)

    _poll()
    _sweep()                                         # already decided by this reader

    row = _row("<s1@icloud.com>")
    assert row.outcome == "skipped" and "no attachment" in row.reason
    assert row.alerted_at is not None
    assert len(sent) == 1
    assert sent[0]["To"] == "zmabry@addventuresinc.com"
    assert "Not filed: Fwd: quote?" in sent[0]["Subject"]
    assert sent[0]["Auto-Submitted"] == "auto-generated"
    assert "forward it again" in sent[0].get_content()


def test_an_alert_is_never_sent_to_the_mailbox_itself(mailbox, sent, monkeypatch):
    monkeypatch.setattr(alerts, "where_to", lambda: "aifinance@addventuresinc.com")
    mailbox.folders["INBOX"][b"1"] = (iphone_forward("hi", "<s2@icloud.com>"), False)

    _poll()

    assert sent == []


def test_automatic_mail_is_recorded_but_never_alerted(mailbox, sent):
    newsletter = iphone_forward("September deals", "<n1@vendor.example>")
    newsletter["List-Unsubscribe"] = "<https://vendor.example/u>"
    mailbox.folders["INBOX"][b"1"] = (newsletter, False)

    _poll()

    assert _row("<n1@vendor.example>").outcome == "automatic"
    assert sent == []


# --- the background check ------------------------------------------------------------

def test_mail_a_person_opened_first_is_still_read(mailbox, sent):
    """The poll only sees unread mail; somebody reading it on a phone hid it for good."""
    mailbox.folders["INBOX"][b"1"] = (
        iphone_forward("Fwd: quote 265502", "<o1@icloud.com>", pdf("o1")), True)

    assert _poll().messages_seen == 0
    swept = _sweep()

    assert any("job 265502" in item for item in swept.filed)
    assert b"1" not in mailbox.folders["INBOX"]      # filed away like any other
    assert _row("<o1@icloud.com>").outcome == "filed"


def test_what_an_older_reader_skipped_is_read_again(mailbox, sent):
    """This morning's quote: in Processed, skipped by the reader before this one."""
    key = "<old1@icloud.com>"
    with SessionLocal() as session:
        session.add(MailSeen(message_key=key, outcome="skipped",
                             reason="no attachment", reader_version=1))
        session.commit()
    mailbox.folders["Processed"][b"7"] = (
        iphone_forward("Fwd: new quote 265503", key, pdf("old1")), True)

    swept = _sweep()

    assert any("job 265503" in item for item in swept.filed)
    assert _row(key).outcome == "filed"
    assert b"7" in mailbox.folders["Processed"]      # read where it is, not moved


def test_mail_never_logged_but_already_filed_is_left_alone(mailbox, sent):
    key = "<filed1@scanner.example>"
    with SessionLocal() as session:
        session.add(Document(filename="old.pdf", sha256="sweepfiled".ljust(64, "0"),
                             stored_path=str(_TMP / "old.pdf"), kind="invoice",
                             status="ready", email_message_id=key))
        session.commit()
    mailbox.folders["Processed"][b"8"] = (
        iphone_forward("scan", key, pdf("filed1")), True)

    _sweep()

    assert ("Processed", b"8") not in mailbox.fetched  # never downloaded again
    assert _row(key).outcome == "filed"


def test_the_sweep_does_not_read_the_same_mail_twice(mailbox, sent):
    mailbox.folders["Processed"][b"9"] = (
        iphone_forward("Fwd: quote 265504", "<t1@icloud.com>", pdf("t1")), True)

    first = _sweep()
    second = _sweep()

    assert len(first.filed) == 1 and second.messages_seen == 0
    with SessionLocal() as session:
        quotes = session.scalars(select(Quote).join(Document)
                                 .where(Document.email_message_id == "<t1@icloud.com>")).all()
        assert len(quotes) == 1


def test_a_message_that_keeps_failing_is_emailed_about_once_not_every_poll(mailbox, sent, monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("the disk is full")

    monkeypatch.setattr(mail_imap, "ingest_scan", broken)
    mailbox.folders["INBOX"][b"1"] = (
        iphone_forward("Fwd: quote 265505", "<e1@icloud.com>", pdf("e1")), False)

    _poll()
    _poll()                                          # still unread, tried again

    assert b"1" in mailbox.folders["INBOX"]
    assert _row("<e1@icloud.com>").outcome == "error"
    assert len(sent) == 1


# --- the page ------------------------------------------------------------------------

def test_the_mail_page_lists_what_was_missed(mailbox, sent):
    mailbox.folders["INBOX"][b"1"] = (iphone_forward("Fwd: page test", "<p1@icloud.com>"), False)
    _poll()

    page = client.get("/mail").text

    assert "Mail received" in page
    assert "Fwd: page test" in page and "not filed" in page

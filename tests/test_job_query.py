"""Asking the sender for a missing job number, and recognising the answer.

Vendors routinely leave the job field blank - both real quotes we have on file
do. Without this the document sits in the Inbox until somebody happens to look,
which on a busy week is nobody.

The risks worth testing are not "does the email send". They are: asking a
supplier the same question a dozen times because the poller runs every five
minutes, asking when nobody needs asking, and failing to recognise the answer
when it comes back.
"""
import os
from datetime import datetime
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATA_DIR", "/tmp/job-query-tests")

from app import mail_send                                    # noqa: E402
from app.config import settings                              # noqa: E402


def doc(**kw):
    base = dict(source="email", job_id=None, job_query_sent_at=None, job_query_to="",
                sender="Paul Cricelli <pcricelli@ncbp.com>", subject="Quote for you",
                filename="Quote07RM0002847012.pdf", email_message_id="<abc@ncbp.com>")
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def sending_enabled(monkeypatch):
    monkeypatch.setattr(settings, "ask_for_job_number", True)
    monkeypatch.setattr(settings, "smtp_host", "mail.addventuresinc.com")
    monkeypatch.setattr(settings, "smtp_user", "ap@addventuresinc.com")
    monkeypatch.setattr(settings, "smtp_password", "x")
    monkeypatch.setattr(settings, "smtp_from", "ap@addventuresinc.com")
    sent = []
    monkeypatch.setattr(mail_send, "send", lambda msg: sent.append(msg))
    return sent


# --- when NOT to ask -----------------------------------------------------

def test_disabled_by_default(monkeypatch):
    """Replying to people must never begin by surprise on a deploy."""
    monkeypatch.setattr(settings, "ask_for_job_number", False)
    assert mail_send.ask_for_job_number(doc()) is None


def test_never_asks_about_an_upload(sending_enabled):
    """Whoever uploaded it is sitting at the screen; the form asks them there."""
    assert mail_send.ask_for_job_number(doc(source="upload")) is None
    assert sending_enabled == []


def test_never_asks_when_the_document_is_already_filed(sending_enabled):
    assert mail_send.ask_for_job_number(doc(job_id=7)) is None
    assert sending_enabled == []


def test_never_asks_twice(sending_enabled):
    """The poller runs every five minutes. One missing job number is one email."""
    already = doc(job_query_sent_at=datetime(2026, 9, 4, 12, 0))
    assert mail_send.ask_for_job_number(already) is None
    assert sending_enabled == []


def test_does_not_ask_when_there_is_no_reply_address(sending_enabled):
    assert mail_send.ask_for_job_number(doc(sender="")) is None
    assert sending_enabled == []


def test_does_not_ask_when_smtp_is_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "ask_for_job_number", True)
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "imap_host", "")
    assert mail_send.ask_for_job_number(doc()) is None


# --- when to ask, and what it says ---------------------------------------

def test_asks_the_sender_and_reports_who(sending_enabled):
    assert mail_send.ask_for_job_number(doc()) == "pcricelli@ncbp.com"
    assert len(sending_enabled) == 1


def test_the_question_threads_onto_the_original_email(sending_enabled):
    """So the answer comes back attached to the document, not as a loose message."""
    mail_send.ask_for_job_number(doc())
    msg = sending_enabled[0]
    assert msg["In-Reply-To"] == "<abc@ncbp.com>"
    assert msg["References"] == "<abc@ncbp.com>"
    assert msg["Subject"] == "Re: Quote for you"


def test_the_question_names_the_document_so_the_reader_knows_which_one(sending_enabled):
    mail_send.ask_for_job_number(doc(), vendor="New Castle Building Products",
                                 document_number="07RM0002847012")
    body = sending_enabled[0].get_content()
    assert "New Castle Building Products 07RM0002847012" in body
    assert "Quote07RM0002847012.pdf" in body
    assert "job number" in body.lower()


def test_the_question_is_marked_automatic(sending_enabled):
    """So a vacation responder or bounce is not mistaken for an answer."""
    mail_send.ask_for_job_number(doc())
    assert sending_enabled[0]["Auto-Submitted"] == "auto-generated"


def test_a_subject_that_is_already_a_reply_is_not_double_prefixed(sending_enabled):
    mail_send.ask_for_job_number(doc(subject="RE: 5 Skyline Drive"))
    assert sending_enabled[0]["Subject"] == "RE: 5 Skyline Drive"


# --- reading the reply address off a From: header ------------------------

@pytest.mark.parametrize("header,expected", [
    ("Paul Cricelli <pcricelli@ncbp.com>", "pcricelli@ncbp.com"),
    ("pcricelli@ncbp.com", "pcricelli@ncbp.com"),
    ('"Cricelli, Paul" <pcricelli@ncbp.com>', "pcricelli@ncbp.com"),
    ("", ""),
    ("no address here", ""),
])
def test_reply_address_is_read_from_the_from_header(header, expected):
    assert mail_send.reply_address(header) == expected


# --- the round trip: our question, their reply ---------------------------

def test_a_real_reply_to_our_own_question_parses_to_their_number(sending_enabled):
    """End to end against the message we actually send, quoted the way a mail
    client quotes it. This is the case that decides whether the feature works.

    The question contains "Job 260000" as an example. If the sentinel or the
    quote stripping is wrong, this test reads our example back as the answer
    and the document is filed against a job the sender never named.
    """
    from app.extract import parse_job_answer, strip_quoted_reply

    mail_send.ask_for_job_number(doc())
    question = sending_enabled[0].get_content()

    quoted = "\n".join("> " + line for line in question.splitlines())
    reply = f"260123\n\nOn Thu, 4 Sep 2026, Add Ventures wrote:\n{quoted}"

    assert parse_job_answer("", reply).job_number == "260123"
    assert "260000" not in strip_quoted_reply(reply)


def test_the_sentinel_is_the_first_line_of_the_question(sending_enabled):
    """Not the last. A reply quotes the whole message beneath the sender's own
    words, so a sentinel at the bottom leaves our example above the cut."""
    from app.extract import REPLY_SENTINEL

    mail_send.ask_for_job_number(doc())
    assert sending_enabled[0].get_content().startswith(REPLY_SENTINEL)


# --- never asking about something that is not a quote or an invoice ------

def test_no_question_about_a_document_that_is_not_a_quote_or_invoice(sending_enabled):
    """A statement, a packing slip, a signed contract, somebody's screenshot.
    None of them have a job number to ask for, and emailing a stranger to ask
    which job their PDF belongs to is worse than doing nothing."""
    from app import mail_imap
    from types import SimpleNamespace as NS

    for kind in ("other", "statement", "unknown", ""):
        doc = NS(kind=kind, status="other", job_id=None, source="email",
                 job_query_sent_at=None, job_query_to="",
                 sender="stranger@example.com", subject="hi",
                 filename="whatever.pdf", email_message_id="<x@y>")
        assert mail_imap._ask_about(None, doc) == "", kind
    assert sending_enabled == []


def test_a_quote_waiting_on_a_job_number_is_still_asked_about(sending_enabled):
    """The guard must not silence the case the feature exists for."""
    from app import mail_imap
    from types import SimpleNamespace as NS

    committed = []
    doc = NS(kind="quote", status="needs_job", job_id=None, source="email",
             job_query_sent_at=None, job_query_to="",
             sender="Paul <pcricelli@ncbp.com>", subject="Quote",
             filename="q.pdf", email_message_id="<x@y>")
    session = NS(commit=lambda: committed.append(1))
    assert mail_imap._ask_about(session, doc) == "pcricelli@ncbp.com"
    assert len(sending_enabled) == 1
    assert doc.job_query_to == "pcricelli@ncbp.com"

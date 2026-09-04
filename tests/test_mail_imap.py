"""Tests for parsing real-world email.

Everything here works on constructed email messages - no mail server needed. The
parsing is where the bugs live: encoded subject lines, HTML-only bodies, inline
images that are not attachments, and filenames that arrive RFC 2047 encoded.
"""
from __future__ import annotations

import sys
import pytest
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.extract import parse_job_answer, parse_job_directive, strip_quoted_reply  # noqa: E402
from app.mail_imap import _attachments, _body_text, _decode  # noqa: E402


def build(subject="Test", body="", html=None, attachments=()):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "Paul Cricelli <pcricelli@ncbp.com>"
    msg.set_content(body or "")
    if html is not None:
        msg.add_alternative(html, subtype="html")
    for name, data, (maintype, subtype) in attachments:
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return msg


PDF = b"%PDF-1.4\ntrailer<</Root 1 0 R>>\n%%EOF\n"


# --- headers --------------------------------------------------------------

def test_encoded_subject_is_decoded():
    """Vendors' mail systems encode non-ASCII subjects; raw they are unreadable."""
    encoded = "=?utf-8?q?Quote_=E2=80=93_Job_4417?="
    assert _decode(encoded) == "Quote – Job 4417"


def test_a_malformed_header_does_not_raise():
    assert _decode("=?bogus?x?nonsense?=") != ""
    assert _decode(None) == ""
    assert _decode("") == ""


# --- bodies ---------------------------------------------------------------

def test_plain_text_body_is_read():
    msg = build(body="Please see attached for job 4417.\nThanks")
    assert "job 4417" in _body_text(msg)


def test_html_only_body_is_reduced_to_text():
    """Vendors frequently send HTML-only mail; the job number can be in it."""
    msg = EmailMessage()
    msg["Subject"] = "Invoice"
    msg.set_content("<p>Master updated to <b>job 4417</b></p>", subtype="html")
    text = _body_text(msg)
    assert "Master updated to job 4417" in text
    assert "<b>" not in text


def test_script_and_style_are_stripped_from_html():
    msg = EmailMessage()
    msg["Subject"] = "x"
    msg.set_content(
        "<style>.a{color:red}</style><script>var x=1</script><p>job 4417</p>",
        subtype="html",
    )
    text = _body_text(msg)
    assert "color:red" not in text and "var x" not in text
    assert "job 4417" in text


# --- attachments ----------------------------------------------------------

def test_pdf_attachment_is_found():
    msg = build(attachments=[("quote.pdf", PDF, ("application", "pdf"))])
    found = list(_attachments(msg))
    assert [name for name, _ in found] == ["quote.pdf"]
    assert found[0][1] == PDF


def test_unsupported_attachment_types_are_skipped():
    """A signature block or a spreadsheet must not be sent to the extractor."""
    msg = build(attachments=[
        ("quote.pdf", PDF, ("application", "pdf")),
        ("terms.docx", b"PK\x03\x04", ("application", "octet-stream")),
        ("logo.svg", b"<svg/>", ("image", "svg+xml")),
    ])
    assert [name for name, _ in _attachments(msg)] == ["quote.pdf"]


def test_scanned_images_are_accepted():
    msg = build(attachments=[("scan.jpg", b"\xff\xd8\xff", ("image", "jpeg"))])
    assert [name for name, _ in _attachments(msg)] == ["scan.jpg"]


def test_several_attachments_all_come_through():
    msg = build(attachments=[
        ("inv-1.pdf", PDF, ("application", "pdf")),
        ("inv-2.pdf", PDF + b" ", ("application", "pdf")),
    ])
    assert len(list(_attachments(msg))) == 2


def test_message_with_no_attachments_yields_nothing():
    assert list(_attachments(build(body="just a note"))) == []


def test_oversized_attachment_is_skipped():
    from app.mail_types import MAX_ATTACHMENT_BYTES

    huge = b"x" * (MAX_ATTACHMENT_BYTES + 1)
    msg = build(attachments=[("huge.pdf", huge, ("application", "pdf"))])
    assert list(_attachments(msg)) == []


# --- the whole point: subject line -> job -------------------------------

def test_forwarded_subject_lines_resolve_to_a_job():
    cases = [
        ("FW: Quote 07RM0002885432 - Job 4417", "4417", False),
        ("Invoice for job #8823", "8823", False),
        ("Job: 63 Winding Ridge", "63 WINDING RIDGE", False),
        ("master updated to job 4417", "4417", True),
        ("Re: your order", None, False),
    ]
    for subject, expected_job, expected_master in cases:
        directive = parse_job_directive(subject)
        assert directive.job_number == expected_job, subject
        assert directive.is_master_update is expected_master, subject


# --- recognising the answer to a job number we asked for -----------------
# The reply is matched on the Message-ID we asked from, carried back in
# In-Reply-To. Subject lines get edited, forwarded and reused; a message ID
# does not, so it is the only thing worth trusting to tie an answer to a
# document.

from app.mail_imap import _MSGID  # noqa: E402


def test_message_ids_are_pulled_out_of_a_references_header():
    header = "<first@ncbp.com> <second@ncbp.com>\n <third@abcsupply.com>"
    assert _MSGID.findall(header) == [
        "<first@ncbp.com>", "<second@ncbp.com>", "<third@abcsupply.com>"
    ]


def test_no_message_ids_in_an_empty_header():
    assert _MSGID.findall("") == []


@pytest.mark.parametrize("body", [
    "260000", "Job 260000", "job #260000", "That's job number 260000.",
    "260000 thanks", "260000, thanks!", "  260000  ",
    "Hi\n\n260000\n\nSent from my iPhone",
])
def test_a_bare_number_is_a_valid_answer_because_we_asked(body):
    """Asked a direct question, people reply with the number and nothing else."""
    assert parse_job_answer("", body).job_number == "260000", body


def test_a_bare_number_is_still_ignored_in_an_unsolicited_email():
    """Unasked, "260000" could be an invoice number, a quantity or an extension."""
    assert parse_job_directive("", "260000").job_number is None


def test_our_own_example_in_the_quoted_reply_is_not_read_as_the_answer():
    """The dangerous case: the question contains "Job 260000" as an example.

    Reply with the original quoted underneath and a naive parser answers the
    question with our own example, filing the document against a job the sender
    never named.
    """
    reply = ("999123\n\n"
             "----- please reply above this line -----\n"
             "Could you reply with the job number? For example:\n"
             "    Job 260000\n")
    assert parse_job_answer("", reply).job_number == "999123"


@pytest.mark.parametrize("quoted", [
    "> Could you reply with the job number? For example:\n>     Job 260000",
    "On Thu, 4 Sep 2026 at 14:02, Add Ventures wrote:\n    Job 260000",
    "-----Original Message-----\nFrom: ap@addventuresinc.com\n Job 260000",
    "From: ap@addventuresinc.com\nSent: Thursday\n Job 260000",
])
def test_quoted_originals_are_stripped_whatever_the_mail_client(quoted):
    assert parse_job_answer("", f"999123\n\n{quoted}").job_number == "999123"
    assert "260000" not in strip_quoted_reply(f"999123\n\n{quoted}")


def test_a_reply_can_also_declare_the_master_quote():
    directive = parse_job_answer("", "master updated to job 260000")
    assert directive.job_number == "260000"
    assert directive.is_master_update


@pytest.mark.parametrize("body", [
    "Thanks!", "I'll check with the office.", "See attached.", "",
    "260000 or 260001", "260000 tomorrow",
])
def test_an_ambiguous_reply_is_not_guessed_at(body):
    """Better to keep waiting than to file a document against the wrong job."""
    assert not parse_job_answer("", body).job_number, body

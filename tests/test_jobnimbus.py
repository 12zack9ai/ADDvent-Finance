"""The JobNimbus lookup, and the email it triggers.

No network anywhere here - `_request` is replaced with the shapes a real
response might arrive in. That is the point: the field names could not be
verified when this was written, so what is tested is that the reader copes with
several plausible shapes and refuses to guess when none of them fits.

The tests that matter most are the refusals. Emailing the wrong project manager
about somebody else's job is worse than emailing nobody.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = Path(tempfile.mkdtemp(prefix="finance-jn-"))
os.environ.setdefault("DATA_DIR", str(_TMP))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP / 'jn.db'}")
os.environ.setdefault("APP_PASSWORD", "")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app import jobnimbus, mail_send  # noqa: E402
from app.config import settings  # noqa: E402


@pytest.fixture()
def with_key(monkeypatch):
    monkeypatch.setattr(settings, "jobnimbus_api_key", "test-key")


def respond(monkeypatch, payload):
    monkeypatch.setattr(jobnimbus, "_request", lambda path, params: payload)


# --- reading a job record -------------------------------------------------

OWNER_SHAPE = {
    "results": [{
        "jnid": "abc123",
        "number": "260000",
        "name": "Daul Gardens - Building 3",
        "record_type_name": "Roof Replacement",
        "owners": [{"id": "u1", "name": "Mike Reilly",
                    "email": "mreilly@addventuresinc.com"}],
    }],
}

FLAT_SHAPE = {
    "results": [{
        "number": "260000",
        "display_name": "Daul Gardens",
        "sales_rep_name": "Mike Reilly",
        "sales_rep_email": "mreilly@addventuresinc.com",
    }],
}

BARE_LIST = [OWNER_SHAPE["results"][0]]


@pytest.mark.parametrize("payload", [OWNER_SHAPE, FLAT_SHAPE, BARE_LIST])
def test_the_assignee_is_found_in_several_plausible_shapes(monkeypatch, with_key, payload):
    respond(monkeypatch, payload)
    found = jobnimbus.find_job("260000")

    assert found is not None
    assert found.email == "mreilly@addventuresinc.com"
    assert found.person_name == "Mike Reilly"
    assert found.usable


def test_nothing_happens_without_an_api_key(monkeypatch):
    monkeypatch.setattr(settings, "jobnimbus_api_key", "")
    assert not jobnimbus.configured()
    # And it does not even try: _request would raise if it were called.
    assert jobnimbus.find_job("260000") is None


def test_a_job_number_that_does_not_match_exactly_is_refused(monkeypatch, with_key):
    """A search endpoint returns what it thinks is relevant. 260000 and 260001
    are different jobs with different managers."""
    respond(monkeypatch, {"results": [{
        "number": "260001", "name": "Somebody else's job",
        "owners": [{"name": "Wrong Person", "email": "wrong@addventuresinc.com"}],
    }]})
    assert jobnimbus.find_job("260000") is None


def test_two_jobs_with_the_same_number_are_not_guessed_between(monkeypatch, with_key):
    respond(monkeypatch, {"results": [
        {"number": "260000", "owners": [{"name": "A", "email": "a@addventuresinc.com"}]},
        {"number": "260000", "owners": [{"name": "B", "email": "b@addventuresinc.com"}]},
    ]})
    assert jobnimbus.find_job("260000") is None


def test_a_job_with_no_assignee_email_is_found_but_not_usable(monkeypatch, with_key):
    respond(monkeypatch, {"results": [{
        "number": "260000", "name": "Daul Gardens",
        "owners": [{"name": "Mike Reilly"}],
    }]})
    found = jobnimbus.find_job("260000")

    assert found is not None
    assert found.person_name == "Mike Reilly"
    assert not found.usable          # nothing to send to, so nothing is sent


def test_an_empty_response_is_not_an_error(monkeypatch, with_key):
    respond(monkeypatch, {"results": []})
    assert jobnimbus.find_job("260000") is None


def test_jobnimbus_being_down_never_raises_into_ingestion(monkeypatch, with_key):
    def explode(path, params):
        raise jobnimbus.JobNimbusError("Could not reach JobNimbus: timed out")
    monkeypatch.setattr(jobnimbus, "_request", explode)

    assert jobnimbus.find_job("260000") is None


def test_a_blank_job_number_asks_nothing(monkeypatch, with_key):
    respond(monkeypatch, OWNER_SHAPE)
    assert jobnimbus.find_job("") is None
    assert jobnimbus.find_job("   ") is None


# --- the email itself ------------------------------------------------------

def test_the_request_names_the_job_the_invoice_and_where_to_send(monkeypatch):
    monkeypatch.setattr(
        settings, "smtp_from", "aifinance@addventuresinc.com", raising=False)
    msg = mail_send.compose_quote_request(
        to_address="mreilly@addventuresinc.com",
        job_number="260000",
        job_name="Daul Gardens",
        person_name="Mike Reilly",
        vendor="New Castle Building Products",
        invoice_number="INV-551900",
        amount="$6,154.00",
    )
    body = msg.get_content()

    assert msg["To"] == "mreilly@addventuresinc.com"
    assert "260000" in msg["Subject"]
    assert "Hi Mike," in body
    assert "New Castle Building Products" in body
    assert "INV-551900" in body
    assert "$6,154.00" in body
    assert "aifinance@addventuresinc.com" in body      # where to send them
    # It says what is actually at stake, without inventing urgency.
    assert "unchecked" in body
    assert msg["Auto-Submitted"] == "auto-generated"


def test_it_starts_its_own_thread_rather_than_replying_to_the_vendor():
    """This goes to a colleague about a job, not to a vendor about a document."""
    msg = mail_send.compose_quote_request(
        to_address="mreilly@addventuresinc.com", job_number="260000")
    assert msg["In-Reply-To"] is None
    assert msg["References"] is None
    assert "Job 260000" in msg["Subject"]


def test_no_first_name_still_reads_as_a_sentence():
    msg = mail_send.compose_quote_request(
        to_address="pm@addventuresinc.com", job_number="260000")
    assert msg.get_content().startswith("Hi,")


# --- when a quote request is actually sent ---------------------------------

class _Job:
    def __init__(self, number="260000", chased=None):
        self.job_number = number
        self.quote_chase_sent_at = chased
        self.quote_chase_to = ""


class _Invoice:
    vendor = "New Castle Building Products"
    invoice_number = "INV-551900"
    total = None


@pytest.fixture()
def sendable(monkeypatch):
    """SMTP configured, the feature on, and nothing actually leaving."""
    sent = []
    monkeypatch.setattr(settings, "ask_for_quote", True)
    monkeypatch.setattr(settings, "can_send_mail", lambda: True)
    monkeypatch.setattr(settings, "reply_domains", lambda: {"addventuresinc.com"})
    monkeypatch.setattr(settings, "smtp_settings",
                        lambda: ("h", 587, "u", "p", "aifinance@addventuresinc.com"))
    monkeypatch.setattr(mail_send, "send", lambda msg: sent.append(msg))
    return sent


def _assignment(email="mreilly@addventuresinc.com", name="Mike Reilly"):
    return jobnimbus.Assignment(job_number="260000", job_name="Daul Gardens",
                                person_name=name, email=email)


def test_the_project_manager_is_asked(sendable):
    who = mail_send.ask_for_quote(_Job(), _Invoice(), _assignment())
    assert who == "mreilly@addventuresinc.com"
    assert len(sendable) == 1


def test_nothing_is_sent_while_the_feature_is_off(monkeypatch, sendable):
    monkeypatch.setattr(settings, "ask_for_quote", False)
    assert mail_send.ask_for_quote(_Job(), _Invoice(), _assignment()) is None
    assert sendable == []


def test_the_once_per_job_rule_lives_with_the_caller_that_records_it(sendable):
    """Deliberately NOT enforced here. services.chase_quote sets
    quote_chase_sent_at before calling, so a duplicate guard in this function
    would refuse every legitimate send - which is what it did until this test
    existed. The end-to-end version of this rule is in test_upload_flow.py."""
    from datetime import datetime, timezone
    already = _Job(chased=datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert mail_send.ask_for_quote(already, _Invoice(), _assignment()) is not None
    assert len(sendable) == 1


def test_an_outside_address_is_never_written_to(sendable):
    """Even coming from JobNimbus. If a job record has a customer's address in
    the assignee field, that is a bad record - not permission to email them."""
    outside = _assignment(email="owner@daulgardenscondo.com", name="Board President")
    assert mail_send.ask_for_quote(_Job(), _Invoice(), outside) is None
    assert sendable == []


def test_nothing_is_sent_when_jobnimbus_gave_us_nobody(sendable):
    assert mail_send.ask_for_quote(_Job(), _Invoice(), None) is None
    assert mail_send.ask_for_quote(_Job(), _Invoice(), _assignment(email="")) is None
    assert sendable == []


def test_nothing_is_sent_without_smtp(monkeypatch, sendable):
    monkeypatch.setattr(settings, "can_send_mail", lambda: False)
    assert mail_send.ask_for_quote(_Job(), _Invoice(), _assignment()) is None
    assert sendable == []

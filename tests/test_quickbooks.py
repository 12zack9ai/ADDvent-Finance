"""QuickBooks Desktop, tested without QuickBooks.

None of this can be tried against the real thing until there is a Windows
machine beside the company file, so the parts are built to be provable
without one: the conversation is a state machine with no HTTP in it, qbXML is
strings in and dataclasses out, and the test below is a fake Web Connector
that drives all eight callbacks over real SOAP with canned responses.

What that cannot prove is whether QuickBooks accepts the requests. It proves
everything up to the wire.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = Path(tempfile.mkdtemp(prefix="finance-qb-"))
os.environ.setdefault("DATA_DIR", str(_TMP))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP / 'qb.db'}")
os.environ.setdefault("APP_PASSWORD", "")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Job  # noqa: E402
from app.quickbooks import protocol, qbxml, qwc, soap, sync  # noqa: E402
from app.quickbooks.mirror import (  # noqa: E402
    OUT_CONFIRMED,
    OUT_FAILED,
    OUT_PENDING,
    QbCustomer,
    QbInvoice,
    QbOutbox,
    QbSession,
    QbSyncState,
)


# --- canned qbXML, in the shape QuickBooks actually returns ----------------

def _wrap(body: str, code: str = "0", extra: str = "") -> str:
    return (
        '<?xml version="1.0" ?><QBXML><QBXMLMsgsRs>'
        f"{body}"
        "</QBXMLMsgsRs></QBXML>"
    )


COMPANY_RS = _wrap(
    '<CompanyQueryRs requestID="1" statusCode="0" statusSeverity="Info">'
    "<CompanyRet><CompanyName>Add Ventures Inc.</CompanyName>"
    "<Country>US</Country></CompanyRet></CompanyQueryRs>"
)

CUSTOMERS_RS = _wrap(
    '<CustomerQueryRs requestID="1" statusCode="0" statusSeverity="Info" '
    'iteratorRemainingCount="0">'
    "<CustomerRet><ListID>80000001-1</ListID><EditSequence>1</EditSequence>"
    "<Name>Daul Gardens Condominium Association</Name>"
    "<FullName>Daul Gardens Condominium Association</FullName>"
    "<IsActive>true</IsActive>"
    "<TimeModified>2026-09-01T10:00:00-05:00</TimeModified></CustomerRet>"
    "<CustomerRet><ListID>80000002-1</ListID><EditSequence>1</EditSequence>"
    "<Name>260701 Building 4 reroof</Name>"
    "<FullName>Daul Gardens Condominium Association:260701 Building 4 reroof</FullName>"
    "<ParentRef><ListID>80000001-1</ListID></ParentRef>"
    "<IsActive>true</IsActive></CustomerRet>"
    "<CustomerRet><ListID>80000003-1</ListID><EditSequence>1</EditSequence>"
    "<Name>Clubhouse repairs</Name>"
    "<FullName>Winding Ridge Court HOA:Clubhouse repairs</FullName>"
    "<ParentRef><ListID>80000009-1</ListID></ParentRef>"
    "<IsActive>true</IsActive></CustomerRet>"
    "</CustomerQueryRs>"
)

INVOICES_RS = _wrap(
    '<InvoiceQueryRs requestID="1" statusCode="0" statusSeverity="Info" '
    'iteratorRemainingCount="0">'
    "<InvoiceRet><TxnID>AAA-1</TxnID><EditSequence>1</EditSequence>"
    "<RefNumber>AR-2611</RefNumber>"
    "<CustomerRef><ListID>80000002-1</ListID>"
    "<FullName>Daul Gardens Condominium Association:260701 Building 4 reroof</FullName>"
    "</CustomerRef>"
    "<TxnDate>2026-08-18</TxnDate><DueDate>2026-09-17</DueDate>"
    "<Subtotal>96400.00</Subtotal><TotalAmount>96400.00</TotalAmount>"
    "<BalanceRemaining>56400.00</BalanceRemaining><IsPaid>false</IsPaid>"
    "</InvoiceRet>"
    "<InvoiceRet><TxnID>AAA-2</TxnID><EditSequence>1</EditSequence>"
    "<RefNumber>AR-2640</RefNumber>"
    "<CustomerRef><ListID>80000002-1</ListID>"
    "<FullName>Daul Gardens Condominium Association:260701 Building 4 reroof</FullName>"
    "</CustomerRef>"
    "<TxnDate>2026-09-02</TxnDate>"
    "<TotalAmount>40000.00</TotalAmount>"
    "<BalanceRemaining>40000.00</BalanceRemaining><IsPaid>false</IsPaid>"
    "</InvoiceRet>"
    "</InvoiceQueryRs>"
)

NOTHING_FOUND_RS = _wrap(
    '<InvoiceQueryRs requestID="1" statusCode="1" statusSeverity="Info" '
    'statusMessage="A query request did not find a matching object."/>'
)

BILL_ADDED_RS = _wrap(
    '<BillAddRs requestID="1" statusCode="0" statusSeverity="Info">'
    "<BillRet><TxnID>BILL-99</TxnID><RefNumber>INV-118420-2</RefNumber>"
    "<VendorRef><FullName>ABC Supply Co.</FullName></VendorRef>"
    "<TxnDate>2026-08-18</TxnDate><AmountDue>17430.74</AmountDue></BillRet>"
    "</BillAddRs>"
)

BILL_REFUSED_RS = _wrap(
    '<BillAddRs requestID="1" statusCode="3140" statusSeverity="Error" '
    'statusMessage="There is an invalid reference to QuickBooks Vendor."/>'
)


# --- building requests -----------------------------------------------------

def test_the_envelope_carries_the_version_processing_instruction():
    """Not decoration: it tells QuickBooks which schema to validate against,
    and a wrong one is rejected outright."""
    xml = qbxml.customer_query()
    assert xml.startswith('<?xml version="1.0" encoding="utf-8"?>')
    assert '<?qbxml version="13.0"?>' in xml
    assert '<QBXMLMsgsRq onError="stopOnError">' in xml


def test_the_version_is_never_higher_than_the_file_supports():
    assert qbxml.negotiate_version(16, 0) == "13.0"     # we ask for less
    assert qbxml.negotiate_version(8, 0) == "8.0"       # an older file
    assert qbxml.negotiate_version(None, None) == "13.0"


def test_a_list_query_and_a_transaction_query_ask_for_changes_differently():
    """A ModifiedDateRangeFilter inside a CustomerQueryRq is rejected, and the
    whole message with it. The schema names them differently and qbXML has no
    forgiveness about it."""
    since = datetime(2026, 9, 1, 10, 0)
    customers = qbxml.customer_query(cursor=since)
    invoices = qbxml.invoice_query(cursor=since)

    assert "<FromModifiedDate>2026-09-01T10:00:00</FromModifiedDate>" in customers
    assert "ModifiedDateRangeFilter" not in customers
    assert "<ModifiedDateRangeFilter><FromModifiedDate>" in invoices


def test_paging_continues_where_it_left_off():
    first = qbxml.customer_query()
    assert 'iterator="Start"' in first
    more = qbxml.customer_query(page="{ABC-123}")
    assert 'iterator="Continue"' in more and 'iteratorID="{ABC-123}"' in more


def test_a_bill_can_be_linked_to_the_purchase_order_it_came_from():
    """The one place Desktop beats Online: LinkToTxnID copies the order's
    lines onto the bill instead of leaving a bare pointer."""
    xml = qbxml.bill_add({
        "vendor": "ABC Supply Co.", "ref_number": "INV-1",
        "txn_date": "2026-08-18", "link_to_txn_id": "PO-7",
        "lines": [{"amount": D("17430.74"), "account": "5120 · Materials",
                   "customer_job": "Daul Gardens:260701", "memo": "roof"}],
    })
    assert "<LinkToTxnID>PO-7</LinkToTxnID>" in xml
    assert "<Amount>17430.74</Amount>" in xml
    assert "<CustomerRef><FullName>Daul Gardens:260701</FullName></CustomerRef>" in xml


def test_an_ampersand_in_a_vendor_name_does_not_break_the_request():
    xml = qbxml.bill_add({"vendor": "Smith & Sons <Roofing>", "lines": []})
    assert "Smith &amp; Sons &lt;Roofing&gt;" in xml


# --- reading responses -----------------------------------------------------

def test_an_invoice_response_is_read_into_decimals():
    response = qbxml.parse(INVOICES_RS)
    assert response.ok and len(response.rows) == 2
    first = response.rows[0]
    assert first["total"] == D("96400.00")
    assert first["balance_remaining"] == D("56400.00")
    assert first["txn_date"].isoformat() == "2026-08-18"


def test_a_timezone_offset_is_dropped_rather_than_kept():
    """Every other datetime in this database is naive. One aware value among
    them is a comparison waiting to raise."""
    row = qbxml.parse(CUSTOMERS_RS).rows[0]
    assert row["time_modified"] == datetime(2026, 9, 1, 10, 0)
    assert row["time_modified"].tzinfo is None


def test_nothing_found_is_not_an_error():
    """QuickBooks answers an empty query with statusCode 1. Treating that as
    a failure would put the integration in a permanent error state on a quiet
    day."""
    response = qbxml.parse(NOTHING_FOUND_RS)
    assert response.ok and response.empty and response.rows == []
    assert qbxml.errors_in(NOTHING_FOUND_RS) == ""


def test_a_real_failure_says_what_it_was():
    assert "3140" in qbxml.errors_in(BILL_REFUSED_RS)
    assert "invalid reference" in qbxml.errors_in(BILL_REFUSED_RS)


def test_rubbish_is_refused_rather_than_half_read():
    with pytest.raises(qbxml.QbXmlError):
        qbxml.parse("not xml at all")
    with pytest.raises(qbxml.QbXmlError):
        qbxml.parse("")


# --- the .qwc file ---------------------------------------------------------

def test_the_qwc_file_has_what_the_connector_refuses_to_go_without():
    content = qwc.build("https://finance.addventuresinc.com", "qbwc")
    for element in ("AppName", "AppURL", "AppSupport", "UserName", "OwnerID",
                    "FileID", "QBType"):
        assert f"<{element}>" in content
    assert "<AppURL>https://finance.addventuresinc.com/qbwc</AppURL>" in content
    assert "<QBType>QBFS</QBType>" in content
    assert "<RunEveryNMinutes>30</RunEveryNMinutes>" in content


def test_the_guids_are_uppercase_and_stable():
    """The connector recognises an application by these. A new pair on the
    next deploy appears as a second, duplicate application beside the first -
    and it rejects lowercase hex without saying that is the problem."""
    first = qwc.owner_id("https://finance.addventuresinc.com")
    again = qwc.owner_id("https://finance.addventuresinc.com")
    assert first == again
    assert first.startswith("{") and first.endswith("}")
    assert first == first.upper()
    assert qwc.owner_id("https://other.example.com") != first
    assert qwc.file_id("https://finance.addventuresinc.com") != first


def test_a_plain_http_address_is_called_out_before_anybody_carries_it_over():
    assert any("https" in p for p in qwc.problems("http://finance.example.com"))
    assert qwc.problems("https://finance.addventuresinc.com") == []
    assert qwc.problems("http://localhost:8000") == []


# --- the WSDL --------------------------------------------------------------

def test_the_wsdl_declares_every_callback_the_connector_makes():
    xml = soap.wsdl("https://finance.addventuresinc.com/qbwc")
    for op in ("serverVersion", "clientVersion", "authenticate", "sendRequestXML",
               "receiveResponseXML", "connectionError", "getLastError",
               "closeConnection"):
        assert f'name="{op}"' in xml
    assert 'targetNamespace="http://developer.intuit.com/"' in xml
    assert "https://finance.addventuresinc.com/qbwc" in xml


def test_authenticate_returns_an_array_and_the_others_do_not():
    """A single string where the connector expects an array is a type
    mismatch it reports and then stops."""
    array = soap.build_response("authenticate", ["ticket-1", ""])
    assert array.count("<string>") == 2
    single = soap.build_response("closeConnection", "OK")
    assert "<string>" not in single and "<closeConnectionResult>OK<" in single


# --- the conversation, end to end -----------------------------------------

class FakeQuickBooks:
    """The Web Connector and QuickBooks, as far as our service can tell.

    Drives all eight callbacks over real SOAP envelopes and answers each
    request with canned qbXML, so the whole exchange is exercised without a
    Windows machine anywhere.
    """

    def __init__(self, service, responses):
        self.service = service
        self.responses = list(responses)
        self.sent = []
        self.progress = []

    def _call(self, operation, **args):
        # The connector sends qbXML XML-escaped inside the element, the same
        # as any other string. Sending it raw - a whole document with its own
        # declaration, nested inside another - is not XML at all, and getting
        # that wrong in the fake would have hidden whether the real thing
        # parses.
        def esc(value):
            return (str(value).replace("&", "&amp;").replace("<", "&lt;")
                    .replace(">", "&gt;"))

        body = "".join(f"<{k}>{esc(v)}</{k}>" for k, v in args.items())
        envelope = (
            '<?xml version="1.0"?>'
            '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
            f'<soap:Body><{operation} xmlns="http://developer.intuit.com/">'
            f"{body}</{operation}></soap:Body></soap:Envelope>"
        )
        op, parsed = soap.parse_request(envelope)
        return soap.dispatch(self.service, op, parsed)

    def run(self, user="qbwc", password="secret"):
        assert self._call("clientVersion", strVersion="2.3.0.36") == ""
        ticket, company = self._call("authenticate",
                                     strUserName=user, strPassword=password)
        if company in ("nvu", "none"):
            return company

        while True:
            request = self._call(
                "sendRequestXML", ticket=ticket, strHCPResponse="",
                strCompanyFileName="C:/QB/AddVentures.QBW", qbXMLCountry="US",
                qbXMLMajorVers="16", qbXMLMinorVers="0",
            )
            if not request:
                break
            self.sent.append(request)
            reply = self.responses.pop(0) if self.responses else NOTHING_FOUND_RS
            done = self._call("receiveResponseXML", ticket=ticket,
                              response=reply, hresult="", message="")
            self.progress.append(done)
            if done < 0 or done >= 100:
                break

        return self._call("closeConnection", ticket=ticket)


@pytest.fixture()
def db():
    init_db()
    session = SessionLocal()
    for model in (QbInvoice, QbCustomer, QbOutbox, QbSyncState, QbSession):
        for row in session.scalars(select(model)).all():
            session.delete(row)
    if not session.scalar(select(Job).where(Job.job_number == "260701")):
        session.add(Job(job_number="260701", name="Daul Gardens"))
    session.commit()
    yield session
    session.close()


def _service(db, write_back=False, password="secret"):
    def opener(session):
        return sync.open_session(db, session.ticket, write_back=write_back)
    return protocol.Service("qbwc", password, opener)


def test_a_whole_sync_from_first_call_to_last(db):
    fake = FakeQuickBooks(_service(db), [COMPANY_RS, CUSTOMERS_RS, INVOICES_RS])
    closing = fake.run()

    assert len(fake.sent) == 3
    assert "CompanyQueryRq" in fake.sent[0]
    assert "CustomerQueryRq" in fake.sent[1]
    assert "InvoiceQueryRq" in fake.sent[2]
    assert fake.progress[-1] == 100
    assert "Read 6 records" in closing

    assert db.scalar(select(QbCustomer).where(QbCustomer.list_id == "80000002-1")) \
        is not None
    assert len(db.scalars(select(QbInvoice)).all()) == 2


def test_the_job_gets_its_billed_and_collected_without_anybody_typing_them(db):
    """The whole point. $136,400 billed across two invoices, $40,000 of it in."""
    FakeQuickBooks(_service(db), [COMPANY_RS, CUSTOMERS_RS, INVOICES_RS]).run()

    job = db.scalar(select(Job).where(Job.job_number == "260701"))
    db.refresh(job)
    assert job.contract_amount == D("136400.00")
    assert job.collected_amount == D("40000.00")
    assert job.outstanding == D("96400.00")
    assert job.billing_source == "quickbooks"
    assert job.billing_is_synced


def test_a_quickbooks_job_with_no_job_number_is_left_for_a_person(db):
    """Matched on the six-digit number and nothing else. Two associations can
    both have a "Clubhouse repairs"."""
    FakeQuickBooks(_service(db), [COMPANY_RS, CUSTOMERS_RS, INVOICES_RS]).run()

    unmatched = db.scalar(
        select(QbCustomer).where(QbCustomer.full_name.like("%Clubhouse%"))
    )
    assert unmatched is not None and unmatched.job_id is None


def test_a_partial_payment_is_read_as_a_partial_payment(db):
    """Not the is_paid flag: on a progress-billed roof a partial payment is
    the normal case and a boolean cannot say $40,000 of $96,400."""
    FakeQuickBooks(_service(db), [COMPANY_RS, CUSTOMERS_RS, INVOICES_RS]).run()
    invoice = db.scalar(select(QbInvoice).where(QbInvoice.txn_id == "AAA-1"))
    assert not invoice.is_paid
    assert invoice.collected == D("40000.00")


def test_the_wrong_password_is_refused_and_no_session_starts(db):
    fake = FakeQuickBooks(_service(db), [])
    assert fake.run(password="wrong") == "nvu"
    assert fake.sent == []


def test_a_second_sync_only_asks_for_what_changed(db):
    """A full pull holds a lock the people working in the company file feel."""
    FakeQuickBooks(_service(db), [COMPANY_RS, CUSTOMERS_RS, INVOICES_RS]).run()
    again = FakeQuickBooks(_service(db), [COMPANY_RS, CUSTOMERS_RS, INVOICES_RS])
    again.run()

    assert "FromModifiedDate" in again.sent[1]
    assert "ModifiedDateRangeFilter" in again.sent[2]


def test_a_failed_step_does_not_move_the_cursor(db):
    """So it is asked for again next time, rather than leaving a silent hole."""
    broken = _wrap('<CustomerQueryRs requestID="1" statusCode="3000" '
                   'statusSeverity="Error" statusMessage="The company file is busy."/>')
    FakeQuickBooks(_service(db), [COMPANY_RS, broken, INVOICES_RS]).run()

    state = sync.cursor_for(db, sync.STEP_CUSTOMERS)
    assert state.cursor is None                  # not moved on
    assert "busy" in state.last_error
    # And the step after it still ran: one bad query does not lose the run.
    assert sync.cursor_for(db, sync.STEP_INVOICES).cursor is not None


def test_an_approved_bill_is_queued_and_confirmed(db):
    db.add(QbOutbox(request_id="req-1", op="BillAdd", entity_type="invoice",
                    entity_id=1, payload_json='{"vendor": "ABC Supply Co.", '
                    '"ref_number": "INV-118420-2", "lines": []}'))
    db.commit()

    fake = FakeQuickBooks(_service(db, write_back=True),
                          [COMPANY_RS, CUSTOMERS_RS, INVOICES_RS, BILL_ADDED_RS])
    fake.run()

    assert "BillAddRq" in fake.sent[-1]
    item = db.scalar(select(QbOutbox).where(QbOutbox.request_id == "req-1"))
    db.refresh(item)
    assert item.status == OUT_CONFIRMED
    assert item.qb_txn_id == "BILL-99"


def test_a_bill_quickbooks_refuses_stays_refused(db):
    """A write it rejected was rejected for a reason - a vendor that does not
    exist - and retrying it on a schedule buries that reason in a log."""
    db.add(QbOutbox(request_id="req-2", op="BillAdd", entity_type="invoice",
                    entity_id=2, payload_json='{"vendor": "Nobody", "lines": []}'))
    db.commit()

    FakeQuickBooks(_service(db, write_back=True),
                   [COMPANY_RS, CUSTOMERS_RS, INVOICES_RS, BILL_REFUSED_RS]).run()

    item = db.scalar(select(QbOutbox).where(QbOutbox.request_id == "req-2"))
    db.refresh(item)
    assert item.status == OUT_FAILED
    assert "invalid reference" in item.last_error
    assert item.attempts == 1


def test_nothing_is_written_while_write_back_is_off(db):
    db.add(QbOutbox(request_id="req-3", op="BillAdd", entity_type="invoice",
                    entity_id=3, payload_json='{"vendor": "ABC Supply Co.", "lines": []}'))
    db.commit()

    fake = FakeQuickBooks(_service(db, write_back=False),
                          [COMPANY_RS, CUSTOMERS_RS, INVOICES_RS])
    fake.run()

    assert not any("BillAddRq" in sent for sent in fake.sent)
    item = db.scalar(select(QbOutbox).where(QbOutbox.request_id == "req-3"))
    assert item.status == OUT_PENDING


def test_an_unknown_ticket_ends_the_session_rather_than_hanging(db):
    """What a restart mid-conversation looks like from the connector's side."""
    service = _service(db)
    assert service.send_request_xml("made-up", "", "", "US", 16, 0) == ""
    assert service.receive_response_xml("made-up", COMPANY_RS, "", "") == -1
    assert service.close_connection("made-up") == "OK"


def test_the_status_page_counts_what_the_last_run_brought_back(db):
    """Not a running total since the beginning of time: "400 records" on a
    page where three things changed says nothing about whether the last sync
    worked."""
    FakeQuickBooks(_service(db), [COMPANY_RS, CUSTOMERS_RS, INVOICES_RS]).run()
    assert sync.cursor_for(db, sync.STEP_CUSTOMERS).rows == 3
    assert sync.cursor_for(db, sync.STEP_INVOICES).rows == 2

    # A second run that changes nothing reports nothing, not three again.
    FakeQuickBooks(_service(db), [COMPANY_RS, NOTHING_FOUND_RS, NOTHING_FOUND_RS]).run()
    assert sync.cursor_for(db, sync.STEP_CUSTOMERS).rows == 0

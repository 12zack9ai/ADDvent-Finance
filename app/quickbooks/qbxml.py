"""Building qbXML requests and reading what comes back.

qbXML is the message format the QuickBooks SDK speaks. A request looks like
this, and the two processing instructions at the top are not decoration - the
second one tells QuickBooks which schema to validate against, and getting it
wrong is rejected outright:

    <?xml version="1.0" encoding="utf-8"?>
    <?qbxml version="13.0"?>
    <QBXML>
      <QBXMLMsgsRq onError="stopOnError">
        <CustomerQueryRq requestID="1">
          <ActiveStatus>All</ActiveStatus>
        </CustomerQueryRq>
      </QBXMLMsgsRq>
    </QBXML>

**One request per round trip.** The Web Connector hands us one request and
brings back one response, so nothing here batches.

**The version is negotiated, not chosen.** The connector tells us what the
company file supports on every call; we ask for the lowest version that has
what we need and never more, because a request built for 16.0 against a file
that speaks 13.0 fails on the whole message rather than degrading.

Parsing is stdlib ElementTree against a schema Intuit publishes and does not
change lightly. Money is pulled straight into Decimal - it arrives as a string
in the XML, and turning it into a float on the way past would be the one
unforgivable thing to do in this file.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional

# 13.0 ships with QuickBooks 2013 and everything since, and carries every
# field this application reads or writes. Asking for more buys nothing and
# excludes older company files.
DEFAULT_VERSION = "13.0"

ZERO = Decimal("0")


def negotiate_version(major: Optional[int], minor: Optional[int]) -> str:
    """The version to speak, given what the connector says the file supports.

    Never above what it offered, never above what we have tested. A file that
    speaks 16.0 is talked to in 13.0 quite deliberately: this application uses
    nothing newer, and the older schema is the one with the most QuickBooks
    versions behind it.
    """
    if not major:
        return DEFAULT_VERSION
    offered = Decimal(f"{major}.{minor or 0}")
    ours = Decimal(DEFAULT_VERSION)
    return DEFAULT_VERSION if offered >= ours else f"{major}.{minor or 0}"


def envelope(body: str, version: str = DEFAULT_VERSION,
             on_error: str = "stopOnError") -> str:
    """Wrap one request element in the qbXML envelope."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<?qbxml version="{version}"?>\n'
        "<QBXML>\n"
        f'  <QBXMLMsgsRq onError="{on_error}">\n'
        f"    {body.strip()}\n"
        "  </QBXMLMsgsRq>\n"
        "</QBXML>"
    )


def _esc(value: str) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _iterator(cursor: Optional[str], chunk: int) -> str:
    """Paging attributes.

    Chunked hard on purpose. The Web Connector times out at around two minutes
    per request, and a bulk pull holds a lock other people in the company file
    feel - so this asks for a little, often, rather than everything once.
    """
    if cursor:
        return f' iterator="Continue" iteratorID="{_esc(cursor)}" maxReturned="{chunk}"'
    return f' iterator="Start" maxReturned="{chunk}"'


def _stamp_out(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%S")


def _since_txn(cursor: Optional[datetime]) -> str:
    """"Changed since" on a transaction query - wrapped in a range filter."""
    if cursor is None:
        return ""
    return (
        "<ModifiedDateRangeFilter>"
        f"<FromModifiedDate>{_stamp_out(cursor)}</FromModifiedDate>"
        "</ModifiedDateRangeFilter>"
    )


def _since_list(cursor: Optional[datetime]) -> str:
    """The same thing on a list query, where it is a bare element instead.

    Not a tidiness point. qbXML validates against a schema that orders every
    element and names them differently on lists and transactions - a
    ModifiedDateRangeFilter inside a CustomerQueryRq is rejected outright, and
    the whole request with it.
    """
    if cursor is None:
        return ""
    return f"<FromModifiedDate>{_stamp_out(cursor)}</FromModifiedDate>"


# --- the requests we make --------------------------------------------------

def customer_query(cursor: Optional[datetime] = None, page: Optional[str] = None,
                   chunk: int = 100, version: str = DEFAULT_VERSION) -> str:
    """Customers and jobs. In QuickBooks a job is a customer one level down."""
    # Element order is the schema's, not ours: ActiveStatus then
    # FromModifiedDate. qbXML rejects the message if they are the other way
    # round, and says nothing useful about why.
    body = (
        f'<CustomerQueryRq requestID="1"{_iterator(page, chunk)}>'
        "<ActiveStatus>All</ActiveStatus>"
        f"{_since_list(cursor)}"
        "</CustomerQueryRq>"
    )
    return envelope(body, version)


def invoice_query(cursor: Optional[datetime] = None, page: Optional[str] = None,
                  chunk: int = 100, version: str = DEFAULT_VERSION) -> str:
    """Customer invoices, with the balance still outstanding on each.

    `IncludeLineItems` is deliberately off. This reads what a job was billed
    and what has come in against it; the lines are QuickBooks' business, and
    asking for them multiplies the size of every response for nothing.
    """
    body = (
        f'<InvoiceQueryRq requestID="1"{_iterator(page, chunk)}>'
        f"{_since_txn(cursor)}"
        "<IncludeLineItems>false</IncludeLineItems>"
        "</InvoiceQueryRq>"
    )
    return envelope(body, version)


def company_query(version: str = DEFAULT_VERSION) -> str:
    """Which company file is on the other end. The first thing we ask."""
    return envelope('<CompanyQueryRq requestID="1"/>', version)


def bill_add(payload: dict, version: str = DEFAULT_VERSION) -> str:
    """A vendor bill, optionally linked to the purchase order it came from.

    `LinkToTxnID` is the one place QuickBooks Desktop is better than Online:
    it genuinely copies the purchase order's lines onto the bill, where
    Online's equivalent is a bare pointer. When a PO exists this is what makes
    the match deterministic instead of a guess.
    """
    lines = []
    for line in payload.get("lines", []):
        parts = [f"<Amount>{_money_out(line.get('amount'))}</Amount>"]
        if line.get("account"):
            parts.insert(0, f"<AccountRef><FullName>{_esc(line['account'])}"
                            "</FullName></AccountRef>")
        if line.get("memo"):
            parts.append(f"<Memo>{_esc(line['memo'])}</Memo>")
        if line.get("customer_job"):
            parts.append(f"<CustomerRef><FullName>{_esc(line['customer_job'])}"
                         "</FullName></CustomerRef>")
            parts.append("<BillableStatus>NotBillable</BillableStatus>")
        lines.append(f"<ExpenseLineAdd>{''.join(parts)}</ExpenseLineAdd>")

    fields = [f"<VendorRef><FullName>{_esc(payload['vendor'])}</FullName></VendorRef>"]
    if payload.get("txn_date"):
        fields.append(f"<TxnDate>{payload['txn_date']}</TxnDate>")
    if payload.get("ref_number"):
        fields.append(f"<RefNumber>{_esc(payload['ref_number'])}</RefNumber>")
    if payload.get("due_date"):
        fields.append(f"<DueDate>{payload['due_date']}</DueDate>")
    if payload.get("memo"):
        fields.append(f"<Memo>{_esc(payload['memo'])}</Memo>")
    if payload.get("link_to_txn_id"):
        fields.append(f"<LinkToTxnID>{_esc(payload['link_to_txn_id'])}</LinkToTxnID>")
    fields.extend(lines)

    body = (
        '<BillAddRq requestID="1">'
        f"<BillAdd>{''.join(fields)}</BillAdd>"
        "</BillAddRq>"
    )
    return envelope(body, version)


def _money_out(value) -> str:
    if value is None:
        return "0.00"
    return format(Decimal(str(value)).quantize(Decimal("0.01")), "f")


# --- reading what comes back ----------------------------------------------

@dataclass
class Response:
    """One qbXML response, unpacked far enough to act on.

    `status_code` is QuickBooks' own: 0 is success, 1 means the query matched
    nothing (which is not an error and must not be treated as one), and
    anything else is a real failure with a message worth showing a person.
    """

    request_type: str = ""
    status_code: int = 0
    status_message: str = ""
    rows: list[dict] = field(default_factory=list)
    next_page: str = ""            # iteratorID, when there is more to come
    remaining: int = 0

    @property
    def ok(self) -> bool:
        return self.status_code in (0, 1)

    @property
    def empty(self) -> bool:
        return self.status_code == 1

    @property
    def has_more(self) -> bool:
        return bool(self.next_page) and self.remaining > 0


class QbXmlError(ValueError):
    """The response was not qbXML we can read."""


def _text(node: Optional[ET.Element], path: str, default: str = "") -> str:
    if node is None:
        return default
    found = node.find(path)
    return (found.text or default) if found is not None else default


def _decimal(node: Optional[ET.Element], path: str) -> Optional[Decimal]:
    raw = _text(node, path)
    if not raw.strip():
        return None
    try:
        value = Decimal(raw.strip())
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def _date(node: Optional[ET.Element], path: str) -> Optional[date]:
    raw = _text(node, path).strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _stamp(node: Optional[ET.Element], path: str) -> Optional[datetime]:
    raw = _text(node, path).strip()
    if not raw:
        return None
    # QuickBooks sends 2026-09-05T14:22:31-05:00. The offset is dropped rather
    # than converted: everything else in this database is naive UTC-ish, and a
    # single aware datetime among them is a comparison waiting to raise.
    cleaned = re.sub(r"(?:Z|[+-]\d{2}:?\d{2})$", "", raw)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


def parse(xml: str) -> Response:
    """Read one qbXML response."""
    if not (xml or "").strip():
        raise QbXmlError("empty response")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise QbXmlError(f"not XML: {exc}") from exc

    msgs = root.find("QBXMLMsgsRs")
    if msgs is None:
        raise QbXmlError("no QBXMLMsgsRs in the response")
    rs = next(iter(msgs), None)
    if rs is None:
        raise QbXmlError("no response element inside QBXMLMsgsRs")

    out = Response(
        request_type=rs.tag,
        status_code=int(rs.get("statusCode", "0") or 0),
        status_message=rs.get("statusMessage", ""),
        next_page=rs.get("iteratorID", ""),
        remaining=int(rs.get("iteratorRemainingCount", "0") or 0),
    )
    if not out.ok:
        return out

    for ret in rs:
        if ret.tag.endswith("Ret"):
            out.rows.append(_row(ret))
    return out


def _row(ret: ET.Element) -> dict:
    """One returned record, in the shape the mirror stores."""
    tag = ret.tag
    if tag == "CustomerRet":
        return {
            "kind": "customer",
            "list_id": _text(ret, "ListID"),
            "edit_sequence": _text(ret, "EditSequence"),
            "name": _text(ret, "Name"),
            "full_name": _text(ret, "FullName"),
            "parent_list_id": _text(ret, "ParentRef/ListID"),
            "is_active": _text(ret, "IsActive", "true").lower() != "false",
            "time_modified": _stamp(ret, "TimeModified"),
        }
    if tag == "InvoiceRet":
        return {
            "kind": "invoice",
            "txn_id": _text(ret, "TxnID"),
            "edit_sequence": _text(ret, "EditSequence"),
            "ref_number": _text(ret, "RefNumber"),
            "customer_list_id": _text(ret, "CustomerRef/ListID"),
            "customer_full_name": _text(ret, "CustomerRef/FullName"),
            "txn_date": _date(ret, "TxnDate"),
            "due_date": _date(ret, "DueDate"),
            "subtotal": _decimal(ret, "Subtotal"),
            "total": _decimal(ret, "TotalAmount"),
            "balance_remaining": _decimal(ret, "BalanceRemaining"),
            "is_paid": _text(ret, "IsPaid", "false").lower() == "true",
            "memo": _text(ret, "Memo"),
            "time_modified": _stamp(ret, "TimeModified"),
        }
    if tag == "CompanyRet":
        return {
            "kind": "company",
            "company_name": _text(ret, "CompanyName"),
            "legal_name": _text(ret, "LegalCompanyName"),
            "country": _text(ret, "Country"),
        }
    if tag == "BillRet":
        return {
            "kind": "bill",
            "txn_id": _text(ret, "TxnID"),
            "ref_number": _text(ret, "RefNumber"),
            "vendor": _text(ret, "VendorRef/FullName"),
            "total": _decimal(ret, "AmountDue"),
            "txn_date": _date(ret, "TxnDate"),
        }
    return {"kind": tag, "raw": ET.tostring(ret, encoding="unicode")}


def errors_in(xml: str) -> str:
    """A human-readable failure from a response, or "" if it succeeded."""
    try:
        response = parse(xml)
    except QbXmlError as exc:
        return str(exc)
    if response.ok:
        return ""
    return f"{response.request_type} failed ({response.status_code}): {response.status_message}"

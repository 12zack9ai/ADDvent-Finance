"""Where the cash flow numbers come from.

One interface, several sources, so connecting QuickBooks later is a new class
rather than a rewrite of the report.

    AccountingSource
      |- LocalSource        approved invoices this system already holds
      |- AgingCsvSource     A/P and A/R aging exported from QuickBooks Desktop
      `- QuickBooksSource   live, once the connector exists

**The CSV source is the one that makes this useful before QuickBooks is
connected.** QuickBooks Desktop has no cloud API - reaching it needs a Windows
machine running the Web Connector, which is weeks of work and someone else's
permission. But it exports "A/P Aging Detail" and "A/R Aging Detail" to CSV in
about four clicks, and those two files contain everything this forecast needs.
So the same report can be produced today, by hand-export, and identically
later from a live connection. The report never knows which it got.

Column names differ between QuickBooks versions and editions, so the CSV parser
matches headers loosely rather than by position. A file whose columns cannot be
identified is rejected with the headers it did find, rather than being read
into silently wrong numbers.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cashflow import Payable, Receivable, ZERO
from app.models import Invoice

# --- reading whatever the accounting package produced ---------------------

_MONEY_JUNK = re.compile(r"[^0-9.\-()]")
_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%d/%m/%Y", "%b %d, %Y", "%d %b %Y")


class AgingParseError(ValueError):
    """The file did not look like an aging report."""


def parse_money(value: str) -> Decimal:
    """Money as accounting packages write it: 1,234.56 / $1,234.56 / (1,234.56)."""
    text = (value or "").strip()
    if not text:
        return ZERO
    negative = text.startswith("(") and text.endswith(")")
    cleaned = _MONEY_JUNK.sub("", text).replace("(", "").replace(")", "")
    if not cleaned or cleaned in {"-", "."}:
        return ZERO
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return ZERO
    return -amount if negative else amount


def parse_date(value: str) -> Optional[date]:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _norm_header(name: str) -> str:
    return re.sub(r"[^a-z]", "", (name or "").lower())


# Header synonyms, in preference order. QuickBooks names these differently
# across versions, and a report edited in Excel first can name them anything.
_FIELDS = {
    "due_date": ("duedate", "due", "datedue"),
    "date": ("date", "trandate", "transactiondate", "billdate", "invoicedate"),
    "name": ("name", "vendor", "customer", "payee", "vendorname", "customername"),
    "amount": ("openbalance", "amount", "balance", "opinbalance", "amountdue", "total"),
    "reference": ("num", "number", "refno", "ref", "invoiceno", "billno", "docnum"),
    "terms": ("terms",),
    "job": ("job", "class", "customerjob", "project"),
}


def _map_columns(headers: list[str]) -> dict[str, int]:
    normalised = [_norm_header(h) for h in headers]
    mapping: dict[str, int] = {}
    for field, synonyms in _FIELDS.items():
        for synonym in synonyms:
            if synonym in normalised:
                mapping[field] = normalised.index(synonym)
                break
    return mapping


def _rows(text: str) -> tuple[dict[str, int], list[list[str]]]:
    """Find the header row and return it with the data rows beneath.

    Aging exports carry a title block above the table - the company name, the
    report name, the date - so the header is rarely the first line.
    """
    reader = list(csv.reader(io.StringIO(text)))
    for position, row in enumerate(reader):
        mapping = _map_columns(row)
        if "amount" in mapping and ("name" in mapping or "reference" in mapping):
            return mapping, [r for r in reader[position + 1:] if any(c.strip() for c in r)]
    found = ", ".join(sorted({c.strip() for r in reader[:12] for c in r if c.strip()})[:12])
    raise AgingParseError(
        "This does not look like an aging report - no row of column headings "
        f"with an amount could be found. Columns seen: {found or '(none)'}. "
        "Export A/P Aging Detail or A/R Aging Detail as CSV without changing "
        "the column headings."
    )


def _cell(row: list[str], mapping: dict[str, int], field: str) -> str:
    index = mapping.get(field)
    if index is None or index >= len(row):
        return ""
    return row[index].strip()


def payables_from_csv(text: str, source: str = "A/P aging") -> list[Payable]:
    mapping, rows = _rows(text)
    out: list[Payable] = []
    for row in rows:
        amount = parse_money(_cell(row, mapping, "amount"))
        if amount == ZERO:
            continue                       # subtotal, blank, or a settled line
        name = _cell(row, mapping, "name")
        if not name:
            continue                       # a total row
        out.append(Payable(
            due_date=parse_date(_cell(row, mapping, "due_date"))
                     or parse_date(_cell(row, mapping, "date")),
            vendor=name,
            amount=amount,
            reference=_cell(row, mapping, "reference"),
            job_number=_cell(row, mapping, "job"),
            source=source,
        ))
    return out


def receivables_from_csv(text: str, source: str = "A/R aging") -> list[Receivable]:
    mapping, rows = _rows(text)
    out: list[Receivable] = []
    for row in rows:
        amount = parse_money(_cell(row, mapping, "amount"))
        if amount == ZERO:
            continue
        name = _cell(row, mapping, "name")
        if not name:
            continue
        out.append(Receivable(
            due_date=parse_date(_cell(row, mapping, "due_date"))
                     or parse_date(_cell(row, mapping, "date")),
            customer=name,
            amount=amount,
            reference=_cell(row, mapping, "reference"),
            job_number=_cell(row, mapping, "job"),
            source=source,
        ))
    return out


# --- sources --------------------------------------------------------------

class AccountingSource(Protocol):
    """Everything the forecast needs, and nothing else."""

    name: str

    def payables(self) -> Iterable[Payable]: ...
    def receivables(self) -> Iterable[Receivable]: ...
    def opening_balance(self) -> Decimal: ...


class LocalSource:
    """Bills this system already knows about.

    Every invoice that has been through the three-way match is a real
    obligation, and the ones still held are shown separately rather than
    counted as leaving - which is a view QuickBooks cannot produce, because it
    does not know an invoice is disputed.
    """

    name = "This system"

    def __init__(self, session: Session, opening: Decimal = ZERO):
        self.session = session
        self._opening = opening

    def payables(self) -> list[Payable]:
        out: list[Payable] = []
        invoices = self.session.scalars(
            select(Invoice).where(Invoice.approval_status != "paid")
        ).all()
        for invoice in invoices:
            if invoice.approval_status == "rejected":
                continue
            out.append(Payable(
                due_date=_as_date(invoice.due_date),
                vendor=invoice.vendor or "Unknown vendor",
                amount=invoice.total or ZERO,
                reference=invoice.invoice_number or f"#{invoice.id}",
                job_number=invoice.job.job_number if invoice.job else "",
                source=self.name,
                on_hold=invoice.approval_status in ("held", "pending_review"),
                hold_reason=invoice.hold_reason or (
                    "Not yet approved" if invoice.approval_status == "pending_review" else ""
                ),
            ))
        return out

    def receivables(self) -> list[Receivable]:
        # This system holds no customer invoices. Returning nothing is honest;
        # inventing a receivable would make the forecast look solvent.
        return []

    def opening_balance(self) -> Decimal:
        return self._opening


class AgingCsvSource:
    """Two CSVs exported from QuickBooks Desktop, plus the bank balance."""

    name = "QuickBooks aging export"

    def __init__(self, ap_csv: str = "", ar_csv: str = "", opening: Decimal = ZERO):
        self._payables = payables_from_csv(ap_csv) if ap_csv.strip() else []
        self._receivables = receivables_from_csv(ar_csv) if ar_csv.strip() else []
        self._opening = opening

    def payables(self) -> list[Payable]:
        return self._payables

    def receivables(self) -> list[Receivable]:
        return self._receivables

    def opening_balance(self) -> Decimal:
        return self._opening


class QuickBooksSource:
    """Live QuickBooks. Deliberately not implemented yet.

    QuickBooks Desktop has no cloud API. Reaching it needs the Web Connector on
    an always-logged-in Windows machine beside the company file, or a hosted
    bridge that runs one for you. Until that exists this raises rather than
    guessing, so nobody mistakes a stub for a connection.
    """

    name = "QuickBooks (live)"

    def payables(self) -> list[Payable]:
        raise NotImplementedError(
            "QuickBooks is not connected yet. Use the A/P and A/R aging CSV "
            "export in the meantime - the report is identical."
        )

    receivables = payables

    def opening_balance(self) -> Decimal:
        raise NotImplementedError("QuickBooks is not connected yet.")


def _as_date(value) -> Optional[date]:
    if value is None or isinstance(value, date):
        return value
    return parse_date(str(value))


# --- storing a report's inputs -------------------------------------------
# The forecast is rebuilt from these on view rather than stored computed, so a
# correction to the arithmetic fixes every report ever produced.

def payable_to_dict(p: Payable) -> dict:
    return {
        "due_date": p.due_date.isoformat() if p.due_date else None,
        "vendor": p.vendor, "amount": str(p.amount), "reference": p.reference,
        "job_number": p.job_number, "source": p.source, "on_hold": p.on_hold,
        "hold_reason": p.hold_reason, "discount_amount": str(p.discount_amount),
        "discount_deadline": p.discount_deadline.isoformat() if p.discount_deadline else None,
    }


def payable_from_dict(d: dict) -> Payable:
    return Payable(
        due_date=date.fromisoformat(d["due_date"]) if d.get("due_date") else None,
        vendor=d.get("vendor", ""), amount=Decimal(d.get("amount", "0")),
        reference=d.get("reference", ""), job_number=d.get("job_number", ""),
        source=d.get("source", ""), on_hold=bool(d.get("on_hold")),
        hold_reason=d.get("hold_reason", ""),
        discount_amount=Decimal(d.get("discount_amount", "0")),
        discount_deadline=(date.fromisoformat(d["discount_deadline"])
                           if d.get("discount_deadline") else None),
    )


def receivable_to_dict(r: Receivable) -> dict:
    return {
        "due_date": r.due_date.isoformat() if r.due_date else None,
        "customer": r.customer, "amount": str(r.amount), "reference": r.reference,
        "job_number": r.job_number, "source": r.source,
        "expected_date": r.expected_date.isoformat() if r.expected_date else None,
        "days_late_typical": r.days_late_typical,
    }


def receivable_from_dict(d: dict) -> Receivable:
    return Receivable(
        due_date=date.fromisoformat(d["due_date"]) if d.get("due_date") else None,
        customer=d.get("customer", ""), amount=Decimal(d.get("amount", "0")),
        reference=d.get("reference", ""), job_number=d.get("job_number", ""),
        source=d.get("source", ""),
        expected_date=(date.fromisoformat(d["expected_date"])
                       if d.get("expected_date") else None),
        days_late_typical=int(d.get("days_late_typical", 0)),
    )

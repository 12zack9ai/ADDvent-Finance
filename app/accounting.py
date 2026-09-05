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
from dataclasses import dataclass
from typing import Iterable, Optional, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import cashflow
from app.cashflow import (
    CAT_CHECKS, CAT_INSURANCE, CAT_LOAN, CAT_OVERHEAD, CAT_PAYROLL, CAT_RENT,
    CAT_SUPPLIER, CAT_TAX, CAT_VEHICLE, CATEGORIES, Payable, Receivable, ZERO,
)
from app.models import CHECK_APPROVED, CHECK_REQUESTED, CheckRequest, Invoice

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
    "category": ("category", "account", "expenseaccount", "type", "class2"),
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




# Categorising a bill is a judgement, and the partner's draft made every one of
# them by hand. These keyword hints get most of the way there so the finance
# team is correcting a categorisation rather than doing all of it - and anything
# unrecognised falls to supplier payments, which is where a construction
# business's uncategorised spend genuinely belongs.
_CATEGORY_HINTS = (
    (CAT_PAYROLL, ("payroll", "adp", "paychex", "gusto", "wages", "941", "labor burden")),
    (CAT_INSURANCE, ("insurance", "anthem", "blue cross", "aetna", "cigna", "workers comp",
                     "liability", "hartford", "travelers")),
    (CAT_RENT, ("rent", "lease -", "landlord", "facilit", "non-finisce")),
    (CAT_VEHICLE, ("fuel", "fuelman", "motor finance", "vehicle", "truck", "auto",
                   "equipment rental", "united rentals", "hyundai", "ford credit")),
    (CAT_LOAN, ("loan", "financ", "capital", "note payable")),
    (CAT_TAX, ("irs", "tax", "treasury", "dept of revenue", "franchise")),
    (CAT_OVERHEAD, ("visa", "amex", "american express", "mastercard", "software", "office",
                    "telephone", "verizon", "comcast", "internet", "subscription",
                    "communications", "security", "advertis", "display")),
)


def categorise(vendor: str, stated: str = "") -> str:
    """Which outflow category a bill belongs to."""
    if stated:
        text = stated.strip().lower()
        for category in CATEGORIES:
            if text == category.lower() or text in category.lower():
                return category
        for category, words in _CATEGORY_HINTS:
            if any(word in text for word in words):
                return category
    name = (vendor or "").lower()
    for category, words in _CATEGORY_HINTS:
        if any(word in name for word in words):
            return category
    return CAT_SUPPLIER

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
            category=categorise(name, _cell(row, mapping, "category")),
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
        # Aging is measured from the INVOICE date, not the due date: these
        # reports frequently carry no terms at all, which is exactly why the
        # forecast collects by bucket rather than by a due date it does not have.
        out.append(Receivable(
            customer=name,
            amount=amount,
            invoice_date=parse_date(_cell(row, mapping, "date")),
            due_date=parse_date(_cell(row, mapping, "due_date")),
            reference=_cell(row, mapping, "reference"),
            job_number=_cell(row, mapping, "job"),
            memo=_cell(row, mapping, "terms"),
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
                category=categorise(invoice.vendor or ""),
                reference=invoice.invoice_number or f"#{invoice.id}",
                job_number=invoice.job.job_number if invoice.job else "",
                source=self.name,
                on_hold=invoice.approval_status in ("held", "pending_review"),
                hold_reason=invoice.hold_reason or (
                    "Not yet approved" if invoice.approval_status == "pending_review" else ""
                ),
            ))
        out.extend(self._check_requests())
        return out

    def _check_requests(self) -> list[Payable]:
        """Permits, deposits, fees - and a sub's draw somebody typed in.

        These are the one outflow in the business that arrives with no invoice
        behind it, so a forecast assembled from bills alone simply cannot see
        them. A permit fee is small; a lift deposit and a sub's draw are not,
        and a 13-week view that quietly omits them is wrong in the direction
        that matters.

        A subcontractor's *invoice* is not one of these and never reaches here
        - it is an invoice, counted above with every other bill. Only a typed
        request is a row in this table.
        """
        out: list[Payable] = []
        requests = self.session.scalars(
            select(CheckRequest).where(
                CheckRequest.status.in_((CHECK_REQUESTED, CHECK_APPROVED))
            )
        ).all()
        for request in requests:
            approved = request.status == CHECK_APPROVED
            # An approved check is due the day it was approved: that is when
            # somebody decided the money leaves. If it was approved a
            # fortnight ago and never cut, the forecast says so by placing it
            # in the past, which is the truth and is worth seeing.
            due = _as_date(request.decided_at) if approved else None
            out.append(Payable(
                due_date=due or request.requested_on,
                vendor=request.payee or "Unknown payee",
                amount=request.amount or ZERO,
                category=CAT_CHECKS,
                reference=request.reference or f"Check request #{request.id}",
                job_number=request.job.job_number if request.job else "",
                source=self.name,
                on_hold=not approved,
                hold_reason="" if approved else "Check request not approved yet",
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


@dataclass(frozen=True)
class JobBilling:
    """What a job has been billed, and what has come in against it."""

    billed: Decimal = ZERO
    collected: Decimal = ZERO

    @property
    def outstanding(self) -> Decimal:
        return self.billed - self.collected


class QuickBooksJobBilling:
    """Billed and collected per job, read from what QuickBooks last told us.

    Kept here because this is where every other accounting source lives, but
    it does no work of its own: `app.quickbooks.sync` owns the mirror and the
    arithmetic, and this is the door the rest of the application knocks on.

    Never live. QuickBooks Desktop cannot be asked a question - the Web
    Connector polls us - so the honest thing this can return is what the last
    poll brought back, with the time it arrived attached so a page can say how
    old it is instead of implying it is current.
    """

    name = "QuickBooks (via the Web Connector)"

    def __init__(self, session: Session):
        self.session = session

    def for_job(self, job) -> Optional[JobBilling]:
        """None means QuickBooks has never been asked about this job.

        Which is a different answer from being asked and told zero, and the
        costing report says which. Returning zeroes for a job that has been
        invoiced twice would be worse than saying we do not know.
        """
        from app.quickbooks import sync

        found = sync.billing_for(self.session, job)
        if found is None or found.invoices == 0:
            return None
        return JobBilling(billed=found.billed, collected=found.collected)


def _as_date(value) -> Optional[date]:
    """A plain date, whatever shape it arrived in.

    datetime is a subclass of date, so the obvious isinstance check lets one
    straight through - and a datetime in `due_date` raises the moment the
    forecast compares it to the end of the horizon. Narrowed here rather than
    at each caller, because the next column somebody reads off a timestamp
    will hit exactly the same wall.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return parse_date(str(value))


# --- storing a report's inputs -------------------------------------------
# The forecast is rebuilt from these on view rather than stored computed, so a
# correction to the arithmetic fixes every report ever produced.

def payable_to_dict(p: Payable) -> dict:
    return {
        "due_date": p.due_date.isoformat() if p.due_date else None,
        "vendor": p.vendor, "amount": str(p.amount), "category": p.category,
        "reference": p.reference, "job_number": p.job_number, "entity": p.entity,
        "source": p.source, "on_hold": p.on_hold, "hold_reason": p.hold_reason,
        "discount_amount": str(p.discount_amount),
        "discount_deadline": p.discount_deadline.isoformat() if p.discount_deadline else None,
    }


def payable_from_dict(d: dict) -> Payable:
    return Payable(
        due_date=date.fromisoformat(d["due_date"]) if d.get("due_date") else None,
        vendor=d.get("vendor", ""), amount=Decimal(d.get("amount", "0")),
        category=d.get("category", CAT_SUPPLIER), reference=d.get("reference", ""),
        job_number=d.get("job_number", ""), entity=d.get("entity", ""),
        source=d.get("source", ""), on_hold=bool(d.get("on_hold")),
        hold_reason=d.get("hold_reason", ""),
        discount_amount=Decimal(d.get("discount_amount", "0")),
        discount_deadline=(date.fromisoformat(d["discount_deadline"])
                           if d.get("discount_deadline") else None),
    )


def receivable_to_dict(r: Receivable) -> dict:
    return {
        "customer": r.customer, "amount": str(r.amount),
        "invoice_date": r.invoice_date.isoformat() if r.invoice_date else None,
        "due_date": r.due_date.isoformat() if r.due_date else None,
        "reference": r.reference, "job_number": r.job_number, "entity": r.entity,
        "source": r.source, "memo": r.memo, "is_backlog": r.is_backlog,
        "assigned_week": r.assigned_week, "collect_weeks": r.collect_weeks,
        "retainage_pct": str(r.retainage_pct), "bucket": r.bucket,
    }


def receivable_from_dict(d: dict) -> Receivable:
    return Receivable(
        customer=d.get("customer", ""), amount=Decimal(d.get("amount", "0")),
        invoice_date=(date.fromisoformat(d["invoice_date"])
                      if d.get("invoice_date") else None),
        due_date=date.fromisoformat(d["due_date"]) if d.get("due_date") else None,
        reference=d.get("reference", ""), job_number=d.get("job_number", ""),
        entity=d.get("entity", ""), source=d.get("source", ""), memo=d.get("memo", ""),
        is_backlog=bool(d.get("is_backlog")), assigned_week=d.get("assigned_week"),
        collect_weeks=d.get("collect_weeks"),
        retainage_pct=Decimal(str(d.get("retainage_pct", "0") or "0")),
        bucket=d.get("bucket", ""),
    )


def assumptions_to_dict(rules: dict) -> dict:
    return {k: {"weeks_out": v.weeks_out, "collectability": str(v.collectability)}
            for k, v in rules.items()}


def assumptions_from_dict(d: dict) -> dict:
    return {k: cashflow.CollectionAssumption(
                weeks_out=v.get("weeks_out"),
                collectability=Decimal(str(v.get("collectability", "1"))))
            for k, v in (d or {}).items()} or cashflow.default_assumptions()

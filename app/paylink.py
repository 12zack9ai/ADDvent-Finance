"""Invoices that arrive as a QuickBooks "View and pay" link instead of a PDF.

Superior Seamless Gutters bills through QuickBooks Online. Their email says
"Your invoice is ready!" with a View and pay button and nothing attached, so the
mailbox reader used to skip it without a word. The button is a tracking redirect
(links.notification.intuit.com) to Intuit's customer page
(connect.intuit.com/t/scs-v1-...), and that page arrives with the whole invoice
already inside it - the JSON it renders from, in <script id="__NEXT_DATA__">:
number, dates, terms, every line, tax and total. One GET and no model, which is
better than a PDF, because nothing has to be read off an image.

This is the one place the app follows a link out of an email, so:

  * Only links that say what they are. An Intuit email also carries
    Unsubscribe, Privacy and Security links on the same tracking host, and
    following Unsubscribe is a GET that does exactly what it says. A link is
    followed only when its text says view, pay or invoice, or it points straight
    at an invoice page - never when it reads like a preference.
  * https only, and Intuit's own hosts only, checked on every redirect hop and
    not just the first, so a link cannot bounce the app anywhere else.
  * GET only, a short timeout and a size cap. Nothing is ever posted, so there
    is no path from here to paying anything.
  * The page is data. Named fields are read out of the JSON, nothing from it is
    executed, and the copy kept as the "original" is rebuilt here from those
    fields, escaped.

Opening the link is what a person clicking it does, and QuickBooks may show the
vendor that the invoice was viewed.
"""
from __future__ import annotations

import html as htmllib
import json
import re
import tempfile
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit

import httpx
from sqlalchemy.orm import Session

from app import jobnum
from app.extract import ExtractionResult
from app.models import Document

MODEL = "quickbooks-pay-link"      # what the Extraction row records as the reader
MAX_BYTES = 3 * 1024 * 1024        # the real page is under 200 KB
MAX_HOPS = 5
MAX_LINKS = 6
TIMEOUT = 20.0

_ANCHOR_RE = re.compile(r"<a\b[^>]*?href\s*=\s*([\"'])(.*?)\1[^>]*>(.*?)</a>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_URL_RE = re.compile(r"https://[^\s\"'<>]+")
_WANTED = re.compile(r"\b(view|pay|invoice)\b", re.I)
_NEVER = re.compile(
    r"unsubscribe|privacy|security|terms|opt[\s-]?out|preference|manage|help|report|affirm",
    re.I,
)
_NEXT_DATA_RE = re.compile(r"<script[^>]*id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>", re.S)


class PaylinkError(Exception):
    """The link could not be read. `retry` says whether trying later could help."""

    def __init__(self, message: str, retry: bool = False) -> None:
        super().__init__(message)
        self.retry = retry


# --- which links -------------------------------------------------------------

def allowed(url: str) -> bool:
    """https, on intuit.com or one of its own subdomains, and nothing else."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (host == "intuit.com" or host.endswith(".intuit.com"))


def _is_invoice_page(url: str) -> bool:
    parts = urlsplit(url)
    return (parts.hostname or "").lower() == "connect.intuit.com" and parts.path.startswith("/t/")


def find_links(html: str = "", text: str = "") -> list[str]:
    """Links in an email worth opening for an invoice, most likely first."""
    found: list[str] = []
    for _quote, href, label in _ANCHOR_RE.findall(html or ""):
        url = htmllib.unescape(href).strip()
        words = " ".join(htmllib.unescape(_TAG_RE.sub(" ", label)).split())
        if not allowed(url) or _NEVER.search(words):
            continue
        if _WANTED.search(words) or _is_invoice_page(url):
            found.append(url)
    # A plain-text body has no link text to go by, so only a link that is an
    # invoice page by its own address - never a bare tracking link.
    for url in _URL_RE.findall(text or ""):
        url = htmllib.unescape(url).rstrip(".,;)>")
        if allowed(url) and _is_invoice_page(url):
            found.append(url)
    return list(dict.fromkeys(found))[:MAX_LINKS]


# --- fetching ----------------------------------------------------------------

def _client() -> httpx.Client:
    # Never follows redirects on its own: every hop is checked before it is taken.
    return httpx.Client(
        follow_redirects=False,
        timeout=TIMEOUT,
        headers={"User-Agent": "Mozilla/5.0 (ADDvent Finance invoice reader)"},
    )


def fetch(url: str) -> tuple[str, str]:
    """GET an allowed link, following allowed redirects only. Returns (url, page)."""
    if not allowed(url):
        raise PaylinkError("not an Intuit link")
    with _client() as client:
        for _hop in range(MAX_HOPS + 1):
            try:
                with client.stream("GET", url) as response:
                    if response.is_redirect:
                        target = urljoin(url, response.headers.get("location", ""))
                        if not allowed(target):
                            raise PaylinkError("the link redirected away from Intuit")
                        url = target
                        continue
                    if response.status_code == 429 or response.status_code >= 500:
                        raise PaylinkError(
                            f"Intuit answered {response.status_code}", retry=True)
                    if response.status_code != 200:
                        raise PaylinkError(f"Intuit answered {response.status_code}")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body += chunk
                        if len(body) > MAX_BYTES:
                            raise PaylinkError("the page was far larger than an invoice")
                    return url, body.decode(response.encoding or "utf-8", "replace")
            except httpx.TimeoutException as exc:
                raise PaylinkError("Intuit did not answer in time", retry=True) from exc
            except httpx.TransportError as exc:
                raise PaylinkError(
                    f"could not reach Intuit ({exc.__class__.__name__})", retry=True) from exc
    raise PaylinkError("too many redirects")


# --- reading the page --------------------------------------------------------

def _dig(node: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _iso(value: Any) -> str:
    """Intuit writes 09-11-2026; the pipeline reads ISO without guessing."""
    text = str(value or "").strip()
    m = re.fullmatch(r"(\d{2})-(\d{2})-(\d{4})", text)
    return f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else text


def _num(value: Any) -> Optional[str]:
    return None if value is None or value == "" else str(value)


def parse(page: str) -> Optional[dict]:
    """The invoice on an Intuit customer page, as an extraction payload, or None."""
    m = _NEXT_DATA_RE.search(page or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    state = _dig(data, "props", "initialReduxState")
    sale = _dig(state, "sale")
    if not isinstance(sale, dict) or str(sale.get("type") or "").upper() != "INVOICE":
        return None
    vendor = str(_dig(state, "companyInfo", "companyName") or "").strip()
    number = str(sale.get("referenceNumber") or "").strip()
    if not vendor or not number:
        return None

    lines: list[dict] = []
    subtotal = None
    for raw in sale.get("lines") or []:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("type")
        if kind == "SubTotalLineDetail":
            subtotal = _num(raw.get("amount"))
            continue
        if kind != "SalesItemLineDetail":
            continue
        lines.append({
            "line_no": raw.get("sequence") or len(lines) + 1,
            "sku": str(_dig(raw, "item", "name") or "").strip(),
            "description": str(raw.get("description") or "").strip(),
            "qty": _num(raw.get("quantity")),
            "unit_price": _num(_dig(raw, "rate", "moneyValue")),
            "extended": _num(raw.get("amount")),
        })

    return {
        "doc_type": "invoice",
        "vendor": vendor,
        "document_number": number,
        "document_date": _iso(sale.get("txnDate")),
        "due_date": _iso(_dig(sale, "receivable", "dueDate")),
        "terms": str(_dig(sale, "saleTerm", "name") or ""),
        "subtotal": subtotal,
        "tax": _num(_dig(sale, "tax", "totalTaxAmount")),
        "total": _num(sale.get("amount")),
        "balance_due": _num(_dig(sale, "receivable", "balance")),
        # A job number written into a line, e.g. "Gutters - 261216". The
        # subject line of the forward still wins, as it does for a PDF.
        "job_number_hint": jobnum.sole_job_number(
            " ".join(line["description"] for line in lines)) or "",
        "lines": lines,
        "source": "QuickBooks pay link",
    }


def read(url: str) -> Optional[dict]:
    """Open one link and return the invoice on it, or None if it is not one."""
    _final, page = fetch(url)
    return parse(page)


# --- filing ------------------------------------------------------------------

def snapshot(payload: dict) -> bytes:
    """The copy kept as this invoice's original.

    Rebuilt from the fields read, escaped, and the same bytes every time for
    the same invoice - so a reminder email for an invoice already filed is
    caught as a duplicate rather than filed twice. For that reason the pay
    link itself is not in it: the link differs between reminders.
    """
    e = htmllib.escape
    rows = "".join(
        "<tr>"
        f"<td>{e(line['sku'])}</td>"
        f"<td>{e(line['description']).replace(chr(10), '<br>')}</td>"
        f"<td class=r>{e(line['qty'] or '')}</td>"
        f"<td class=r>{e(line['unit_price'] or '')}</td>"
        f"<td class=r>{e(line['extended'] or '')}</td>"
        "</tr>"
        for line in payload["lines"]
    )
    facts = "".join(
        f"<tr><th>{label}</th><td>{e(str(payload.get(key) or ''))}</td></tr>"
        for label, key in (
            ("Invoice", "document_number"), ("Date", "document_date"),
            ("Due", "due_date"), ("Terms", "terms"), ("Subtotal", "subtotal"),
            ("Tax", "tax"), ("Total", "total"), ("Balance due", "balance_due"),
        )
    )
    page = (
        "<!doctype html><html><head><meta charset=utf-8>"
        f"<title>{e(payload['vendor'])} invoice {e(payload['document_number'])}</title>"
        "<style>body{font:14px/1.5 system-ui,sans-serif;margin:32px;color:#1d232b}"
        "table{border-collapse:collapse;margin:12px 0}th,td{border:1px solid #d6dbe1;"
        "padding:6px 10px;text-align:left;vertical-align:top}.r{text-align:right}"
        "p{color:#555;max-width:60ch}</style></head><body>"
        f"<h1>{e(payload['vendor'])}</h1>"
        f"<table>{facts}</table>"
        "<table><tr><th>Item</th><th>Description</th><th class=r>Qty</th>"
        f"<th class=r>Rate</th><th class=r>Amount</th></tr>{rows}</table>"
        "<p>This invoice came in as a QuickBooks \"View and pay\" link rather than a "
        "PDF. Every figure above was read from the vendor's own QuickBooks invoice "
        "page. To see it or pay it there, open the original email.</p>"
        "</body></html>"
    )
    return page.encode("utf-8")


def filename_for(payload: dict) -> str:
    name = f"{payload['vendor']} invoice {payload['document_number']}.html"
    return re.sub(r"[\\/:*?\"<>|]+", " ", name).strip()


def ingest(
    session: Session,
    payload: dict,
    *,
    sender: str = "",
    subject: str = "",
    body: str = "",
    message_id: str = "",
) -> Document:
    """File the invoice through the same pipeline as a PDF, minus the reading."""
    from app.services import ingest_file   # services is the heavy module; keep it lazy

    with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as tmp:
        tmp.write(snapshot(payload))
        tmp_path = Path(tmp.name)
    try:
        return ingest_file(
            session, tmp_path, filename_for(payload),
            source="email", sender=sender, subject=subject, body=body,
            message_id=message_id,
            extraction=ExtractionResult(payload=payload, model=MODEL),
        )
    finally:
        tmp_path.unlink(missing_ok=True)

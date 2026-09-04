"""Seed a realistic demo job so the UI can be exercised without the Claude API.

Builds one roofing job with a master quote and three delivery invoices that
between them produce every verdict: over quote, under quote, exactly as quoted,
and not on the quote at all.

    python scripts/seed_demo.py [--reset]

This bypasses extraction on purpose - it exists to test the comparison engine,
the marked-up invoice, and the dashboard independently of the API.
"""
from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import (  # noqa: E402
    Approval,
    ChangeOrder,
    Document,
    Extraction,
    Invoice,
    InvoiceLine,
    Job,
    JobAlias,
    Quote,
    QuoteLine,
    Receipt,
)
from app.approval import apply_routing, route  # noqa: E402
from app.services import recompare_invoice  # noqa: E402

D = Decimal

JOB_NUMBER = "4417"
JOB_NAME = "Willow Creek Apartments — Building C reroof"
VENDOR = "Baker Building Supply"

# (sku, description, qty, uom, unit_price)
QUOTE_LINES = [
    ("SHG-WW-AR", 'Architectural shingles, Weathered Wood, 30-yr', D("184"), "SQ", D("118.50")),
    ("UND-SYN-10", "Synthetic underlayment, 10 sq roll", D("20"), "RL", D("89.00")),
    ("IWS-225", "Ice & water shield, 225 sf roll", D("14"), "RL", D("112.75")),
    ("RDG-CAP-HP", "Hip & ridge cap shingles", D("22"), "BDL", D("64.25")),
    ("NL-CL-125", 'Roofing nails, 1-1/4" coil, 7200 ct', D("12"), "BX", D("52.00")),
    ("DE-10-WHT", "Drip edge, 10 ft, white", D("96"), "EA", D("12.40")),
    ("PB-153", 'Pipe boot, 1.5" - 3"', D("18"), "EA", D("18.75")),
    ("STF-BDL", "Step flashing, bundle of 100", D("9"), "BDL", D("41.00")),
]

# (invoice_no, date, [(sku, description, qty, uom, unit_price)])
INVOICES = [
    ("INV-88214", date(2026, 8, 12), [
        ("SHG-WW-AR", 'Architectural shingles, Weathered Wood, 30-yr', D("92"), "SQ", D("118.50")),
        ("UND-SYN-10", "Synthetic underlayment, 10 sq roll", D("12"), "RL", D("89.00")),
        # Billed above quote - the point of the whole system.
        ("NL-CL-125", 'Roofing nails, 1-1/4" coil, 7200 ct', D("8"), "BX", D("58.00")),
    ]),
    ("INV-88431", date(2026, 8, 19), [
        # Vendor passed on a price drop.
        ("IWS-225", "Ice & water shield, 225 sf roll", D("14"), "RL", D("108.00")),
        ("RDG-CAP-HP", "Hip & ridge cap shingles", D("22"), "BDL", D("64.25")),
        # Small per-unit increase, large because of the quantity.
        ("DE-10-WHT", "Drip edge, 10 ft, white", D("96"), "EA", D("14.90")),
    ]),
    ("INV-88677", date(2026, 8, 27), [
        ("SHG-WW-AR", 'Architectural shingles, Weathered Wood, 30-yr', D("92"), "SQ", D("118.50")),
        ("PB-153", 'Pipe boot, 1.5" - 3"', D("18"), "EA", D("18.75")),
        ("STF-BDL", "Step flashing, bundle of 100", D("9"), "BDL", D("41.00")),
        # Never quoted - should land as "not on quote", not silently accepted.
        ("", "Dumpster haul-off & disposal fee", D("1"), "EA", D("485.00")),
    ]),
]


# A second supplier on the same job, never quoted and never confirmed received.
# Demonstrates the two situations the policy routes straight to the owner.
WASTE_VENDOR = "ABC Waste Removal"
WASTE_INVOICE = ("AW-3391", date(2026, 8, 22), [
    ("", "30 yard dumpster - haul & disposal", D("2"), "EA", D("485.00")),
    ("", "Overweight tonnage surcharge", D("1.4"), "TON", D("92.50")),
])


def _doc(session, filename: str, kind: str) -> Document:
    doc = Document(
        filename=filename,
        sha256=f"demo-{filename}",
        stored_path=f"(demo) {filename}",
        kind=kind,
        source="upload",
        status="ready",
        subject=f"Job {JOB_NUMBER} — {filename}",
    )
    session.add(doc)
    session.flush()
    return doc


def reset(session) -> None:
    for model in (Approval, InvoiceLine, Invoice, QuoteLine, Quote, ChangeOrder,
                  Receipt, JobAlias, Extraction, Document, Job):
        session.execute(delete(model))
    session.commit()


def main() -> int:
    init_db()
    session = SessionLocal()

    if "--reset" in sys.argv:
        reset(session)
        print("Cleared existing data.")

    if session.query(Job).filter(Job.job_number == JOB_NUMBER).first():
        print(f"Job {JOB_NUMBER} already exists. Re-run with --reset to rebuild.")
        return 0

    job = Job(job_number=JOB_NUMBER, name=JOB_NAME)
    session.add(job)
    session.flush()

    quote_doc = _doc(session, "baker-quote-Q-20418.pdf", "quote")
    quote_doc.job_id = job.id
    quote = Quote(
        job_id=job.id, document_id=quote_doc.id, is_master=True,
        vendor=VENDOR, quote_number="Q-20418", quote_date=date(2026, 8, 4),
    )
    session.add(quote)
    session.flush()

    subtotal = Decimal("0")
    for i, (sku, desc, qty, uom, price) in enumerate(QUOTE_LINES, start=1):
        extended = (qty * price).quantize(D("0.01"))
        subtotal += extended
        session.add(QuoteLine(
            quote_id=quote.id, line_no=i, sku=sku, description=desc,
            qty=qty, uom=uom, unit_price=price, extended=extended,
        ))
    quote.subtotal = subtotal
    quote.tax = (subtotal * D("0.0625")).quantize(D("0.01"))
    quote.total = subtotal + quote.tax
    session.flush()

    for number, when, lines in INVOICES:
        inv_doc = _doc(session, f"baker-{number.lower()}.pdf", "invoice")
        inv_doc.job_id = job.id
        invoice = Invoice(
            job_id=job.id, document_id=inv_doc.id, vendor=VENDOR,
            invoice_number=number, invoice_date=when,
        )
        session.add(invoice)
        session.flush()

        inv_subtotal = Decimal("0")
        for i, (sku, desc, qty, uom, price) in enumerate(lines, start=1):
            extended = (qty * price).quantize(D("0.01"))
            inv_subtotal += extended
            session.add(InvoiceLine(
                invoice_id=invoice.id, line_no=i, sku=sku, description=desc,
                qty=qty, uom=uom, unit_price=price, extended=extended,
            ))
        invoice.subtotal = inv_subtotal
        invoice.tax = (inv_subtotal * D("0.0625")).quantize(D("0.01"))
        invoice.total = inv_subtotal + invoice.tax
        session.flush()
        session.refresh(job)
        recompare_invoice(session, job, invoice)

    # --- the receiving leg: New Castle material was confirmed delivered ----
    session.add(Receipt(
        job_id=job.id, vendor=VENDOR, kind="delivery",
        reference="PS-44718", confirmed_by="M. Alvarez (site)",
        note="Checked against PO; full pallet count received.",
    ))

    # Vendors reference this job by the site address, not our job number.
    session.add(JobAlias(job_id=job.id, alias="63 WINDING RIDGE", source="po"))

    # --- a second supplier: never quoted, never confirmed received ---------
    number, when, lines = WASTE_INVOICE
    waste_doc = _doc(session, f"abcwaste-{number.lower()}.pdf", "invoice")
    waste_doc.job_id = job.id
    waste = Invoice(
        job_id=job.id, document_id=waste_doc.id, vendor=WASTE_VENDOR,
        invoice_number=number, invoice_date=when,
    )
    session.add(waste)
    session.flush()
    sub = Decimal("0")
    for i, (sku, desc, qty, uom, price) in enumerate(lines, start=1):
        ext = (qty * price).quantize(D("0.01"))
        sub += ext
        session.add(InvoiceLine(
            invoice_id=waste.id, line_no=i, sku=sku, description=desc,
            qty=qty, uom=uom, unit_price=price, extended=ext,
        ))
    waste.subtotal = sub
    waste.total = sub
    session.flush()
    session.refresh(job)
    recompare_invoice(session, job, waste)

    # Route everything now that receipts and vendors are in place.
    for invoice in job.invoices:
        apply_routing(invoice)

    session.commit()

    print(f"\nJob {JOB_NUMBER} — {JOB_NAME}")
    print(f"Master quote {quote.quote_number}: {len(QUOTE_LINES)} lines, total ${quote.total:,}")
    print("-" * 74)
    total_over = Decimal("0")
    for invoice in sorted(job.invoices, key=lambda i: i.invoice_number):
        total_over += invoice.overbilled_amount or Decimal("0")
        print(
            f"  {invoice.invoice_number}  {invoice.invoice_date}  "
            f"total ${invoice.total:>10,}  "
            f"over={invoice.lines_over} under={invoice.lines_under} "
            f"ok={invoice.lines_match} unmatched={invoice.lines_unmatched}  "
            f"overbilled ${invoice.overbilled_amount or 0:,}"
        )
    print("-" * 74)
    print(f"  Total billed above the master quote: ${total_over:,}")
    print("\n  Approval routing:")
    for invoice in sorted(job.invoices, key=lambda i: i.invoice_number):
        r = route(invoice)
        blocked = "" if r.can_approve else "  BLOCKED: receipt not confirmed"
        print(f"    {invoice.invoice_number:11} {r.action:12} -> {r.tier:5}{blocked}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

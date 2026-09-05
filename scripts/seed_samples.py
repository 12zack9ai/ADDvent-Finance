"""Fill the whole system with sample data.

Eight jobs that between them exercise every programme the app now runs, so
somebody opening the site sees what it looks like in use rather than an empty
shell. Nothing here calls Claude - the rows are written straight into the
database, which is the point: this exists to show the comparison, the roll-up
and the queues working, not to test extraction.

    python scripts/seed_samples.py            # add the samples
    python scripts/seed_samples.py --remove   # take every one of them out
    python scripts/seed_samples.py --reset    # remove, then add again

What each job is for:

    269001  Daul Gardens        the healthy job - material quote, three
                                deliveries (as quoted, over, under plus an
                                off-quote item), a straggler from a second
                                supplier, a subcontract billed to 85%, a
                                permit and a deposit, and full costing
    269002  Winding Ridge       two live quotes from one supplier - roofing
                                material and skylights - and invoices that
                                draw from both
    269003  Oakland Commons     a corrected invoice that never cancelled the
                                one it corrected: the duplicate check
    269004  Sunrise Estates     an invoice on a job with no quote at all, and
                                the PM already asked for one
    269005  Cedar Ridge         a revised quote standing the old one down,
                                plus a signed change order and one still
                                waiting on a person
    269006  Maple Court         a subcontractor whose next invoice goes past
                                the award, and the oldest check in the queue
    269007  Bay Terrace         check requests only - every purpose, every
                                waiting band
    269008  Harborview          two bills that are not ours: a stranger
                                demanding payment today, and a real vendor's
                                name sent from the wrong domain

Built last on purpose, that one: the screening asks what we have seen before,
so it only means anything once the other seven jobs have given it a history.

**The 269xxx band is reserved for samples.** Real jobs run up from 260000, so
nothing here can ever collide with one, and `--remove` takes out exactly these
eight job numbers and the documents attached to them.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, select                                # noqa: E402

from app import accounting, cashflow, trust                          # noqa: E402
from app.approval import apply_routing                               # noqa: E402
from app.config import settings                                      # noqa: E402
from app.db import SessionLocal, init_db                             # noqa: E402
from app.models import (                                             # noqa: E402
    APPROVAL_APPROVED,
    APPROVAL_HELD,
    APPROVAL_PAID,
    APPROVAL_PENDING,
    CHECK_APPROVED,
    CHECK_DEPOSIT,
    CHECK_FEE,
    CHECK_PAID,
    CHECK_PERMIT,
    CHECK_REIMBURSEMENT,
    CHECK_REQUESTED,
    CHECK_SUBCONTRACTOR,
    CO_APPROVED,
    CO_PROPOSED,
    JOB_LOST,
    PURCHASE_EMAIL,
    PURCHASE_TEXT,
    Approval,
    CashReport,
    ChangeOrder,
    CheckRequest,
    Document,
    Invoice,
    InvoiceLine,
    Job,
    Purchase,
    Quote,
    QuoteLine,
    Receipt,
)
from app.services import recompare_invoice, set_master_quote         # noqa: E402

D = Decimal
ZERO = D("0")
TAX = D("0.06625")           # New Jersey
TODAY = date.today()

# Everything this script creates is findable by these three marks, and
# --remove uses nothing else. A seed that cannot be cleanly taken out has no
# business being run against a system somebody is about to use for real.
JOB_BAND = "269"
DOC_MARK = "sample-269-"
CASH_MARK = "sample data"

# Bump when the samples themselves change - a new department, a new job, a
# figure that was wrong. An install carrying an older set rebuilds it once on
# the next start.
#
# Rebuilding is only acceptable because of what this data is: the 269xxx band
# is reserved, disposable, and nobody's real work is in it. Anything else in
# the database is untouched. Without this, a department added after the first
# deploy shows an empty page on the live site forever, and the only fix is
# somebody manually wiping sample data on a system that by then holds real
# work too.
SAMPLE_VERSION = 2
VERSION_MARK = f"{DOC_MARK}version"


def ago(days: int) -> date:
    return TODAY - timedelta(days=days)


def at(days: int) -> datetime:
    return datetime.combine(ago(days), time(9, 30))


def money(value: Decimal) -> Decimal:
    return value.quantize(D("0.01"))


def installed_version(session) -> int:
    """Which generation of samples is in this database, or 0 for none."""
    marker = session.scalar(
        select(Document).where(Document.sha256 == VERSION_MARK)
    )
    if marker is None:
        return 0
    return int(marker.subject) if (marker.subject or "").isdigit() else 1


def _stamp_version(session) -> None:
    marker = session.scalar(
        select(Document).where(Document.sha256 == VERSION_MARK)
    )
    if marker is None:
        marker = Document(
            filename="sample-data-version",
            sha256=VERSION_MARK,
            stored_path="(sample) version marker",
            kind="other",
            status="ready",
        )
        session.add(marker)
    marker.subject = str(SAMPLE_VERSION)
    session.flush()


def _highest_document_number(session) -> int:
    """The last number used by a previous seeding run, or 0."""
    marks = session.scalars(
        select(Document.sha256).where(Document.sha256.like(f"{DOC_MARK}%"))
    ).all()
    numbers = []
    for mark in marks:
        tail = mark[len(DOC_MARK):]
        if tail.isdigit():
            numbers.append(int(tail))
    return max(numbers, default=0)


# --- the writer -----------------------------------------------------------

class Seed:
    """Writes rows the way the app would have written them."""

    def __init__(self, session):
        self.session = session
        # Carry on from whatever is already here rather than restarting at 1.
        # The document mark is unique, so a top-up run that began again at 001
        # collided with the first sample document ever written - which is a
        # thing that only happens once samples can be added to an install that
        # already has some, i.e. exactly when this became possible.
        self.n = _highest_document_number(session)

    # -- documents ---------------------------------------------------------
    def doc(self, job, filename, kind, *, sender="", subject="", body="",
            source="upload", days=0, mime_type="application/pdf"):
        self.n += 1
        doc = Document(
            job_id=job.id if job else None,
            filename=filename,
            sha256=f"{DOC_MARK}{self.n:03d}",
            stored_path=f"(sample) {filename}",
            mime_type=mime_type,
            kind=kind,
            source=source,
            sender=sender,
            subject=subject or f"Job {job.job_number if job else ''} — {filename}",
            body_text=body,
            status="ready",
            received_at=at(days),
        )
        self.session.add(doc)
        self.session.flush()
        return doc

    # -- jobs --------------------------------------------------------------
    def job(self, number, name, **kw):
        job = Job(job_number=number, name=name, **kw)
        self.session.add(job)
        self.session.flush()
        return job

    # -- quotes ------------------------------------------------------------
    def quote(self, job, vendor, number, days, lines, *, subcontract=False,
              sender="", taxed=True, replaces=False, reason=""):
        doc = self.doc(job, f"{number.lower()}.pdf", "quote",
                       sender=sender, source="email" if sender else "upload",
                       days=days)
        quote = Quote(
            job_id=job.id, document_id=doc.id, vendor=vendor,
            quote_number=number, quote_date=ago(days),
            is_subcontract=subcontract, is_master=True,
        )
        self.session.add(quote)
        self.session.flush()

        subtotal = ZERO
        for i, (sku, desc, qty, uom, price) in enumerate(lines, start=1):
            extended = money(qty * price)
            subtotal += extended
            self.session.add(QuoteLine(
                quote_id=quote.id, line_no=i, sku=sku, description=desc,
                qty=qty, uom=uom, unit_price=price, extended=extended,
            ))
        quote.subtotal = subtotal
        quote.tax = money(subtotal * TAX) if taxed else ZERO
        quote.total = subtotal + quote.tax
        self.session.flush()

        set_master_quote(self.session, job, quote, reason=reason, replaces=replaces)
        self.session.refresh(job)
        return quote

    # -- invoices ----------------------------------------------------------
    def invoice(self, job, vendor, number, days, lines, *, status=APPROVAL_PENDING,
                sender="", subject="", body="", taxed=True, terms=30, screen=False):
        doc = self.doc(job, f"{number.lower()}.pdf", "invoice",
                       sender=sender, subject=subject, body=body,
                       source="email" if sender else "upload", days=days)
        invoice = Invoice(
            job_id=job.id, document_id=doc.id, vendor=vendor,
            invoice_number=number, invoice_date=ago(days),
            due_date=ago(days) + timedelta(days=terms),
            created_at=at(days),
        )
        self.session.add(invoice)
        self.session.flush()

        subtotal = ZERO
        for i, (sku, desc, qty, uom, price) in enumerate(lines, start=1):
            extended = money(qty * price)
            subtotal += extended
            self.session.add(InvoiceLine(
                invoice_id=invoice.id, line_no=i, sku=sku, description=desc,
                qty=qty, uom=uom, unit_price=price, extended=extended,
            ))
        invoice.subtotal = subtotal
        invoice.tax = money(subtotal * TAX) if taxed else ZERO
        invoice.total = subtotal + invoice.tax
        self.session.flush()

        if screen:
            flags = trust.screen(self.session, doc, vendor,
                                 own_domains=settings.reply_domains())
            doc.trust_json = trust.dump(flags)
            self.session.flush()

        # Decide it before the matcher runs, exactly as a person would have:
        # apply_routing leaves an approved invoice alone, which is the whole
        # reason it is safe to re-run.
        if status in (APPROVAL_APPROVED, APPROVAL_PAID):
            invoice.approval_status = status
            invoice.approved_by = "Zack Mabry"
            invoice.approved_at = at(max(days - 2, 0))
            self.session.add(Approval(
                invoice_id=invoice.id, decision="approve", tier="pm",
                actor="Zack Mabry", at=at(max(days - 2, 0)),
                note="Sample data — checked against the quote and signed.",
            ))

        self.session.refresh(job)
        recompare_invoice(self.session, job, invoice)
        return invoice

    # -- everything else ---------------------------------------------------
    def check(self, job, payee, amount, purpose, *, days, status=CHECK_REQUESTED,
              reference="", description="", decided=None):
        request = CheckRequest(
            job_id=job.id, vendor=payee, amount=D(amount), purpose=purpose,
            reference=reference, description=description,
            requested_on=ago(days), received_at=at(days), status=status,
        )
        if status in (CHECK_APPROVED, CHECK_PAID):
            request.decided_by = "Zack Mabry"
            request.decided_at = at(decided if decided is not None else days - 2)
        if status == CHECK_PAID:
            request.paid_at = at(max((decided or days) - 4, 0))
        self.session.add(request)
        self.session.flush()
        return request

    def buy(self, job, merchant, total, description, *, days, who="",
            texted=False, tax=None):
        """A receipt photographed at the counter and sent in."""
        doc = self.doc(job, f"receipt-{self.n + 1:03d}.jpg", "receipt",
                       sender=who, source="email" if who else "upload",
                       subject="", days=days, mime_type="image/jpeg")
        row = Purchase(
            job_id=job.id, document_id=doc.id, merchant=merchant,
            purchased_on=ago(days), total=D(total),
            tax=D(tax) if tax else None,
            subtotal=(D(total) - D(tax)) if tax else None,
            description=description, bought_by=who,
            arrived_by=PURCHASE_TEXT if texted else PURCHASE_EMAIL,
            created_at=at(days),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def receipt(self, job, vendor, reference, who, note, *, days):
        row = Receipt(job_id=job.id, vendor=vendor, kind="delivery",
                      reference=reference, confirmed_by=who, note=note,
                      confirmed_at=at(days))
        self.session.add(row)
        self.session.flush()
        return row

    def change_order(self, job, vendor, number, amount, description, *,
                     days, status=CO_APPROVED):
        row = ChangeOrder(
            job_id=job.id, vendor=vendor, number=number, amount=D(amount),
            description=description, status=status, created_at=at(days),
            approved_at=at(days),
            approved_by="Zack Mabry" if status == CO_APPROVED else "",
        )
        self.session.add(row)
        self.session.flush()
        return row


# --- the eight jobs -------------------------------------------------------

ABC = "ABC Supply Co."
ABC_MAIL = "quotes@abcsupply.com"
NEWCASTLE = "New Castle Building Products"
NC_MAIL = "sales@newcastlebp.com"
BEACON = "Beacon Sales"
BEACON_MAIL = "orders@becn.com"
REILLY = "Reilly Roofing LLC"
VANGUARD = "Vanguard Sheet Metal Inc."


def job_269001(s: Seed) -> Job:
    """The healthy job, with every verdict on it."""
    job = s.job("269001", "Daul Gardens — Building 4 reroof",
                contract_amount=D("268000.00"), collected_amount=D("96400.00"),
                billing_source="manual", billing_synced_at=at(3),
                labour_cost=D("41500.00"), labour_hours=D("980"),
                costing_note="Crew of four, three weeks. Sample data.")

    s.quote(job, ABC, "Q-118420", 34, [
        ("SHG-TL-WW", "Timberline HDZ shingles, Weathered Wood", D("186"), "SQ", D("121.40")),
        ("UND-SYN-10", "Synthetic underlayment, 10 sq roll", D("20"), "RL", D("92.50")),
        ("IWS-225", "Ice & water shield, 225 sf roll", D("16"), "RL", D("118.00")),
        ("RDG-CAP-HP", "Hip & ridge cap, Weathered Wood", D("24"), "BDL", D("66.75")),
        ("NL-CL-125", 'Roofing nails, 1-1/4" coil, 7200 ct', D("14"), "BX", D("54.00")),
        ("DE-10-WHT", "Drip edge, 10 ft, white", D("120"), "EA", D("12.85")),
        ("PB-153", 'Pipe boot, 1.5" - 3"', D("22"), "EA", D("19.40")),
        ("STF-BDL", "Step flashing, bundle of 100", D("12"), "BDL", D("42.50")),
        ("RVT-RDG-4", "Ridge vent, 4 ft section", D("40"), "EA", D("23.75")),
    ], sender=ABC_MAIL)

    # Exactly as quoted.
    s.invoice(job, ABC, "INV-118420-1", 26, [
        ("SHG-TL-WW", "Timberline HDZ shingles, Weathered Wood", D("93"), "SQ", D("121.40")),
        ("UND-SYN-10", "Synthetic underlayment, 10 sq roll", D("10"), "RL", D("92.50")),
    ], status=APPROVAL_PAID, sender="ap@abcsupply.com")

    # The price moved after they quoted it. This is what the system is for.
    s.invoice(job, ABC, "INV-118420-2", 18, [
        ("SHG-TL-WW", "Timberline HDZ shingles, Weathered Wood", D("93"), "SQ", D("138.90")),
        ("IWS-225", "Ice & water shield, 225 sf roll", D("16"), "RL", D("118.00")),
        ("DE-10-WHT", "Drip edge, 10 ft, white", D("120"), "EA", D("12.85")),
    ], sender="ap@abcsupply.com")

    # A price they passed on, and one item nobody ever quoted.
    s.invoice(job, ABC, "INV-118420-3", 9, [
        ("RDG-CAP-HP", "Hip & ridge cap, Weathered Wood", D("24"), "BDL", D("64.00")),
        ("NL-CL-125", 'Roofing nails, 1-1/4" coil, 7200 ct', D("14"), "BX", D("54.00")),
        ("PB-153", 'Pipe boot, 1.5" - 3"', D("22"), "EA", D("19.40")),
        ("STF-BDL", "Step flashing, bundle of 100", D("12"), "BDL", D("42.50")),
        ("RVT-RDG-4", "Ridge vent, 4 ft section", D("40"), "EA", D("23.75")),
        ("", "Cricket fabrication, custom — 2 units", D("2"), "EA", D("340.00")),
    ], status=APPROVAL_APPROVED, sender="ap@abcsupply.com")

    # A last-minute run to a different supply house. Nothing about it should
    # be priced against ABC's quote, and nothing about it is wrong.
    s.invoice(job, NEWCASTLE, "NC-771204", 15, [
        ("", "Sheet lead, 4 lb, 12in x 10ft roll", D("3"), "EA", D("148.00")),
        ("", "Roof cement, 5 gal", D("4"), "PL", D("62.50")),
        ("", "Same-day pickup, will-call", D("1"), "EA", D("45.00")),
    ], sender="ap@newcastlebp.com")

    # The subcontract, and three draws against it.
    s.quote(job, REILLY, "SC-2026-14", 40, [
        ("", "Tear-off and disposal, Buildings 4 and 5", D("1"), "LS", D("32000.00")),
        ("", "Dry-in: underlayment and ice & water", D("1"), "LS", D("18000.00")),
        ("", "Shingle installation", D("1"), "LS", D("46000.00")),
        ("", "Flashing, vents and detail work", D("1"), "LS", D("14000.00")),
        ("", "Cleanup and final inspection", D("1"), "LS", D("8000.00")),
    ], subcontract=True, taxed=False)

    s.invoice(job, REILLY, "RR-4412", 37, [
        ("", "Tear-off and disposal, Buildings 4 and 5", D("1"), "LS", D("32000.00")),
        ("", "Dry-in: underlayment and ice & water", D("0.5"), "LS", D("18000.00")),
    ], status=APPROVAL_PAID, taxed=False)
    s.invoice(job, REILLY, "RR-4488", 16, [
        ("", "Dry-in: underlayment and ice & water", D("0.5"), "LS", D("18000.00")),
        ("", "Shingle installation", D("0.6"), "LS", D("46000.00")),
    ], status=APPROVAL_APPROVED, taxed=False)
    s.invoice(job, REILLY, "RR-4551", 4, [
        ("", "Shingle installation", D("0.4"), "LS", D("46000.00")),
        ("", "Flashing, vents and detail work", D("0.35"), "LS", D("14000.00")),
    ], taxed=False)

    s.buy(job, "Home Depot #4412", "84.12", "Caulk, blades, shop rags",
          days=24, who="2015550147@mms.att.net", texted=True, tax="5.57")
    s.buy(job, "Sunoco", "78.40", "Fuel, box truck", days=19,
          who="malvarez@addventuresinc.com")
    s.buy(job, "Bergen Tool Rental", "212.44", "Compactor, one day",
          days=12, who="ttorres@addventuresinc.com", tax="14.08")
    s.buy(job, "Home Depot #4412", "46.88", "Roofing nails, extra bundle",
          days=6, who="2015550147@mms.att.net", texted=True)

    s.receipt(job, ABC, "PS-118420-1", "M. Alvarez (site)",
              "Pallet count checked against the delivery ticket.", days=26)
    s.check(job, "Township of Oakland", "450.00", CHECK_PERMIT, days=26,
            status=CHECK_APPROVED, reference="Permit 2026-1184",
            description="Roofing permit, 410 Daul Avenue")
    s.check(job, "Sky Access Rentals", "2500.00", CHECK_DEPOSIT, days=9,
            reference="Order 88-2210", description="Deposit on the boom lift")
    return job


def job_269002(s: Seed) -> Job:
    """One supplier, two live quotes — material and skylights."""
    job = s.job("269002", "Winding Ridge Court — roof and skylights",
                contract_amount=D("52000.00"), collected_amount=D("38250.00"),
                billing_source="manual", billing_synced_at=at(3),
                labour_cost=D("12400.00"), labour_hours=D("310"))

    s.quote(job, ABC, "Q-118655", 30, [
        ("SHG-TL-CH", "Timberline HDZ shingles, Charcoal", D("84"), "SQ", D("121.40")),
        ("UND-SYN-10", "Synthetic underlayment, 10 sq roll", D("9"), "RL", D("92.50")),
        ("IWS-225", "Ice & water shield, 225 sf roll", D("8"), "RL", D("118.00")),
        ("DE-10-BRN", "Drip edge, 10 ft, brown", D("64"), "EA", D("12.85")),
    ], sender=ABC_MAIL)

    # Same supplier, same week, nothing in common with the first one. Both
    # stay live: an invoice is priced against the pair of them together.
    s.quote(job, ABC, "Q-118661", 30, [
        ("SKY-VS-M06", "Velux VS M06 venting skylight, curb mount", D("6"), "EA", D("742.00")),
        ("SKY-EDW-M06", "Velux EDW M06 step flashing kit", D("6"), "EA", D("168.00")),
        ("SKY-BLD-M06", "Solar blind, M06", D("6"), "EA", D("214.00")),
    ], sender=ABC_MAIL)

    s.invoice(job, ABC, "INV-118655-1", 21, [
        ("SHG-TL-CH", "Timberline HDZ shingles, Charcoal", D("84"), "SQ", D("121.40")),
        ("SKY-VS-M06", "Velux VS M06 venting skylight, curb mount", D("6"), "EA", D("742.00")),
        ("SKY-EDW-M06", "Velux EDW M06 step flashing kit", D("6"), "EA", D("168.00")),
    ], status=APPROVAL_APPROVED, sender="ap@abcsupply.com")

    s.invoice(job, ABC, "INV-118661-2", 6, [
        ("UND-SYN-10", "Synthetic underlayment, 10 sq roll", D("9"), "RL", D("92.50")),
        ("IWS-225", "Ice & water shield, 225 sf roll", D("8"), "RL", D("118.00")),
        ("DE-10-BRN", "Drip edge, 10 ft, brown", D("64"), "EA", D("12.85")),
        ("SKY-BLD-M06", "Solar blind, M06", D("6"), "EA", D("214.00")),
    ], sender="ap@abcsupply.com")

    s.buy(job, "Lowe's #1782", "119.63", "Sealant, flashing tape", days=15,
          who="ttorres@addventuresinc.com", tax="7.93")

    s.receipt(job, ABC, "PS-118655", "T. Torres (PM)",
              "Six skylights on site, none damaged.", days=21)
    return job


def job_269003(s: Seed) -> Job:
    """The corrected invoice that never cancelled the one it corrected."""
    job = s.job("269003", "Oakland Commons — Buildings 1 to 3",
                contract_amount=D("34500.00"), collected_amount=D("22780.00"),
                billing_source="manual", billing_synced_at=at(3),
                labour_cost=D("6800.00"), labour_hours=D("164"))

    s.quote(job, BEACON, "Q-BE-4471", 33, [
        ("SHG-OC-DW", "Owens Corning Duration, Driftwood", D("120"), "SQ", D("116.75")),
        ("UND-DK-10", "Deck Defense underlayment, 10 sq", D("13"), "RL", D("88.00")),
        ("IWS-OC-200", "WeatherLock G ice & water, 200 sf", D("11"), "RL", D("121.50")),
        ("RDG-DW", "ProEdge hip & ridge, Driftwood", D("18"), "BDL", D("71.00")),
        ("VNT-RM-750", "VentSure ridge vent, 4 ft", D("30"), "EA", D("26.40")),
    ], sender=BEACON_MAIL)

    s.invoice(job, BEACON, "BS-90114", 24, [
        ("SHG-OC-DW", "Owens Corning Duration, Driftwood", D("60"), "SQ", D("116.75")),
        ("UND-DK-10", "Deck Defense underlayment, 10 sq", D("7"), "RL", D("88.00")),
        ("IWS-OC-200", "WeatherLock G ice & water, 200 sf", D("6"), "RL", D("121.50")),
    ], sender="billing@becn.com")

    # Reissued nine days later with the ice & water price fixed. Nobody
    # cancelled the first one, so both are sitting here waiting to be paid.
    s.invoice(job, BEACON, "BS-90209", 15, [
        ("SHG-OC-DW", "Owens Corning Duration, Driftwood", D("60"), "SQ", D("116.75")),
        ("UND-DK-10", "Deck Defense underlayment, 10 sq", D("7"), "RL", D("88.00")),
        ("IWS-OC-200", "WeatherLock G ice & water, 200 sf", D("6"), "RL", D("128.00")),
    ], sender="billing@becn.com",
       subject="Corrected invoice BS-90209 — replaces BS-90114",
       body="Please disregard the pricing on BS-90114; the ice & water line "
            "was billed at the old rate. Corrected copy attached.")

    s.invoice(job, BEACON, "BS-90360", 7, [
        ("RDG-DW", "ProEdge hip & ridge, Driftwood", D("18"), "BDL", D("71.00")),
        ("VNT-RM-750", "VentSure ridge vent, 4 ft", D("30"), "EA", D("26.40")),
    ], status=APPROVAL_APPROVED, sender="billing@becn.com")
    return job


def job_269004(s: Seed) -> Job:
    """An invoice with nothing to price it against, and the PM already asked."""
    job = s.job("269004", "Sunrise Estates — gutter and fascia",
                quote_chase_sent_at=at(2), quote_chase_count=1,
                quote_chase_to="ttorres@addventuresinc.com")

    s.invoice(job, VANGUARD, "VSM-2214", 5, [
        ("", "6in K-style aluminium gutter, .032, white", D("340"), "LF", D("8.65")),
        ("", "3x4 downspout, white", D("120"), "LF", D("9.20")),
        ("", "Aluminium fascia wrap, 8in", D("260"), "LF", D("6.40")),
    ], sender="ap@vanguardsm.com")
    return job


def job_269005(s: Seed) -> Job:
    """A revised quote standing the old one down, and two change orders."""
    job = s.job("269005", "Cedar Ridge Village — roof replacement",
                contract_amount=D("31500.00"),
                billing_source="manual", billing_synced_at=at(3),
                labour_cost=D("9200.00"), labour_hours=D("228"))

    original = [
        ("SHG-LM-BS", "Landmark Pro shingles, Burnt Sienna", D("96"), "SQ", D("132.00")),
        ("UND-RF-10", "RoofRunner underlayment, 10 sq", D("10"), "RL", D("94.00")),
        ("IWS-WG-200", "WinterGuard ice & water, 200 sf", D("9"), "RL", D("124.00")),
    ]
    s.quote(job, NEWCASTLE, "Q-NC-3120", 45, original, sender=NC_MAIL)

    # Same items, new prices. That is what makes it a revision rather than a
    # second scope, and the old prices must stop authorising invoices.
    s.quote(job, NEWCASTLE, "Q-NC-3186", 31, [
        ("SHG-LM-BS", "Landmark Pro shingles, Burnt Sienna", D("96"), "SQ", D("138.50")),
        ("UND-RF-10", "RoofRunner underlayment, 10 sq", D("10"), "RL", D("94.00")),
        ("IWS-WG-200", "WinterGuard ice & water, 200 sf", D("9"), "RL", D("129.00")),
    ], sender=NC_MAIL, reason="Vendor sent a revised quote after the 1 August mill increase.")

    s.invoice(job, NEWCASTLE, "NC-773400", 22, [
        ("SHG-LM-BS", "Landmark Pro shingles, Burnt Sienna", D("48"), "SQ", D("138.50")),
        ("UND-RF-10", "RoofRunner underlayment, 10 sq", D("10"), "RL", D("94.00")),
    ], status=APPROVAL_APPROVED, sender="ap@newcastlebp.com")

    s.invoice(job, NEWCASTLE, "NC-774102", 8, [
        ("SHG-LM-BS", "Landmark Pro shingles, Burnt Sienna", D("48"), "SQ", D("138.50")),
        ("IWS-WG-200", "WinterGuard ice & water, 200 sf", D("9"), "RL", D("129.00")),
    ], sender="ap@newcastlebp.com")

    s.change_order(job, NEWCASTLE, "CO-1", "1840.00",
                   "Mill price increase on Landmark Pro effective 1 August. "
                   "Accepted in writing 4 August.", days=31)
    s.change_order(job, NEWCASTLE, "CO-2", "2600.00",
                   "Fuel surcharge on the remaining deliveries. Read off the "
                   "vendor's email — nobody has signed it.",
                   days=6, status=CO_PROPOSED)
    return job


def job_269006(s: Seed) -> Job:
    """A sub whose next invoice goes past the award, and the oldest check."""
    job = s.job("269006", "Maple Court Condominiums — siding and roof",
                contract_amount=D("132000.00"), collected_amount=D("74900.00"),
                billing_source="manual", billing_synced_at=at(3),
                costing_note="Fully subbed — no own crew on this one.")

    s.quote(job, VANGUARD, "SC-2026-22", 62, [
        ("", "Siding removal and disposal, Buildings A to C", D("1"), "LS", D("21000.00")),
        ("", "House wrap and trim", D("1"), "LS", D("12000.00")),
        ("", "Vinyl siding installation", D("1"), "LS", D("39000.00")),
        ("", "Gutters, downspouts and fascia", D("1"), "LS", D("12000.00")),
    ], subcontract=True, taxed=False)

    s.invoice(job, VANGUARD, "VSM-3301", 43, [
        ("", "Siding removal and disposal, Buildings A to C", D("1"), "LS", D("21000.00")),
        ("", "House wrap and trim", D("1"), "LS", D("12000.00")),
    ], status=APPROVAL_PAID, taxed=False)

    s.invoice(job, VANGUARD, "VSM-3388", 22, [
        ("", "Vinyl siding installation", D("0.7"), "LS", D("39000.00")),
    ], status=APPROVAL_APPROVED, taxed=False)

    # Every line on this one is correctly priced. Only the running total sees
    # that it takes the sub $4,000 past what they were awarded.
    s.invoice(job, VANGUARD, "VSM-3455", 3, [
        ("", "Vinyl siding installation", D("0.3"), "LS", D("39000.00")),
        ("", "Gutters, downspouts and fascia", D("1"), "LS", D("12000.00")),
        ("", "Additional dumpster pulls (3)", D("1"), "LS", D("4000.00")),
    ], taxed=False)

    s.quote(job, ABC, "Q-118902", 28, [
        ("STF-BDL", "Step flashing, bundle of 100", D("14"), "BDL", D("42.50")),
        ("DE-10-BRN", "Drip edge, 10 ft, brown", D("180"), "EA", D("12.85")),
        ("NL-CL-125", 'Roofing nails, 1-1/4" coil, 7200 ct', D("22"), "BX", D("54.00")),
    ], sender=ABC_MAIL)
    s.invoice(job, ABC, "INV-118902-1", 19, [
        ("STF-BDL", "Step flashing, bundle of 100", D("14"), "BDL", D("42.50")),
        ("DE-10-BRN", "Drip edge, 10 ft, brown", D("180"), "EA", D("12.85")),
    ], status=APPROVAL_APPROVED, sender="ap@abcsupply.com")

    s.check(job, VANGUARD, "12400.00", CHECK_SUBCONTRACTOR, days=41,
            reference="Draw request 3",
            description="Siding draw — they have been asking since July")
    return job


def job_269007(s: Seed) -> Job:
    """Checks only. Every purpose, and every waiting band."""
    job = s.job("269007", "Bay Terrace — survey and repairs")

    s.check(job, "Township of Oakland", "625.00", CHECK_PERMIT, days=17,
            reference="Permit 2026-1291", description="Repair permit, 12 Bay Terrace")
    s.check(job, "Bergen County Clerk", "180.00", CHECK_FEE, days=33,
            reference="Filing 26-4471", description="Notice of commencement filing")
    s.check(job, "Sky Access Rentals", "4000.00", CHECK_DEPOSIT, days=3,
            reference="Order 88-2318", description="Deposit on the 60ft lift")
    s.check(job, "M. Alvarez", "212.44", CHECK_REIMBURSEMENT, days=8,
            status=CHECK_APPROVED, decided=6,
            description="Fasteners and sealant bought at the counter")
    s.check(job, "Township of Oakland", "450.00", CHECK_PERMIT, days=40,
            status=CHECK_PAID, decided=36, reference="Permit 2026-1102",
            description="Original survey permit")
    return job


def job_269008(s: Seed) -> Job:
    """Two bills that are not ours.

    Built last on purpose. The screening asks what this system has seen
    before, so it only means anything once there is a history to ask about.
    """
    job = s.job("269008", "Harborview Gardens — emergency leak repairs")

    s.quote(job, NEWCASTLE, "Q-NC-8841", 12, [
        ("", "Emergency tarp, 20 x 30, heavy duty", D("4"), "EA", D("96.00")),
        ("", "Roof cement, 5 gal", D("6"), "PL", D("62.50")),
        ("", "Sheet lead, 4 lb, 12in x 10ft roll", D("2"), "EA", D("148.00")),
    ], sender=NC_MAIL)
    s.invoice(job, NEWCASTLE, "NC-772110", 10, [
        ("", "Emergency tarp, 20 x 30, heavy duty", D("4"), "EA", D("96.00")),
        ("", "Roof cement, 5 gal", D("6"), "PL", D("62.50")),
    ], status=APPROVAL_APPROVED, sender="ap@newcastlebp.com")

    # A supplier nobody here has ever dealt with, from a free mail account,
    # telling us to pay today and giving new bank details.
    s.invoice(job, "Apex Roofing Supply LLC", "APX-4471", 2, [
        ("", "Roofing materials — job site delivery", D("1"), "EA", D("14880.00")),
    ], sender="apexroofingsupply.billing@gmail.com", screen=True,
       subject="FINAL NOTICE — payment required today to avoid suspension",
       body="This invoice is past due and must be paid today. Please note our "
            "remittance details have changed — kindly update your records and "
            "wire to the new bank account shown on the attached invoice.")

    # A name we know perfectly well, sent from a domain they have never used.
    s.invoice(job, ABC, "ABC-99120", 1, [
        ("", "Materials supplied — job 269008", D("1"), "EA", D("8412.00")),
    ], sender="ap@abcsupply-billing.com", screen=True,
       subject="Updated invoice — please remit",
       body="Attached is our invoice for the above job. Payment to the account "
            "detailed on the invoice.")
    return job


def job_269009(s: Seed) -> Job:
    """A job we chased and did not get. The spend is still real."""
    job = s.job("269009", "Brookside Manor — roof replacement (not awarded)",
                outcome=JOB_LOST,
                outcome_note="Board went with another contractor, 28 August.")
    s.buy(job, "Sunoco", "64.20", "Fuel, three site visits", days=38,
          who="zmabry@addventuresinc.com")
    s.buy(job, "Bergen Tool Rental", "180.00", "Lift, half day for the survey",
          days=36, who="ttorres@addventuresinc.com")
    s.buy(job, "Staples", "41.75", "Printing and binding, three proposal copies",
          days=31, who="2015550147@mms.att.net", texted=True)
    return job


def job_269010(s: Seed) -> Job:
    """The same again, smaller. There are always several of these."""
    job = s.job("269010", "Fox Hollow — gutter replacement (not awarded)",
                outcome=JOB_LOST, outcome_note="No decision; association shelved it.")
    s.buy(job, "Sunoco", "38.90", "Fuel, measure-up", days=52,
          who="malvarez@addventuresinc.com")
    return job


BUILDERS = (job_269001, job_269002, job_269003, job_269004,
            job_269005, job_269006, job_269007, job_269008,
            job_269009, job_269010)


# --- the cash flow report -------------------------------------------------

def sample_receivables() -> list:
    """Progress billings out to the associations.

    Hand-entered on purpose. This system holds no customer invoices, and
    inventing one inside LocalSource would make the forecast look solvent on
    money nobody has billed.
    """
    rows = [
        # customer, amount, invoiced days ago, terms, reference, job, retainage
        ("Daul Gardens Condominium Association", "96400.00", 18, 30, "AR-2611", "269001", 10),
        ("Winding Ridge Court HOA", "38250.00", 41, 30, "AR-2598", "269002", 10),
        ("Oakland Commons Association", "22780.00", 63, 30, "AR-2571", "269003", 0),
        ("Maple Court Condominium Association", "74900.00", 9, 30, "AR-2620", "269006", 10),
        ("Harborview Gardens Association", "4120.00", 96, 30, "AR-2540", "269008", 0),
    ]
    out = []
    for customer, amount, days, terms, ref, job_number, retainage in rows:
        invoiced = ago(days)
        out.append(cashflow.Receivable(
            customer=customer, amount=D(amount), invoice_date=invoiced,
            due_date=invoiced + timedelta(days=terms), reference=ref,
            job_number=job_number, source=CASH_MARK,
            retainage_pct=D(retainage),
        ))

    # Contract value earned but not yet requisitioned. Invisible in
    # QuickBooks, and the reason a 13-week view is worth having at all:
    # `assigned_week` is when the draw goes out, `collect_weeks` the gap
    # before the association's board actually cuts the check.
    backlog = [
        # customer, amount, bill in week, weeks to collect, job, retainage
        ("Daul Gardens Condominium Association", "171600.00", 3, 3, "269001", 10),
        ("Winding Ridge Court HOA", "13750.00", 2, 2, "269002", 10),
        ("Oakland Commons Association", "11720.00", 4, 3, "269003", 0),
        ("Maple Court Condominium Association", "57100.00", 5, 3, "269006", 10),
        ("Cedar Ridge Village Association", "31500.00", 7, 3, "269005", 0),
    ]
    for customer, amount, week, lag, job_number, retainage in backlog:
        out.append(cashflow.Receivable(
            customer=customer, amount=D(amount), reference="Progress billing",
            job_number=job_number, source=CASH_MARK, is_backlog=True,
            assigned_week=week, collect_weeks=lag, retainage_pct=D(retainage),
            memo="Remaining contract value on a live job.",
        ))
    return out


def build_cash_report(session) -> CashReport:
    payables = accounting.LocalSource(session).payables()
    receivables = sample_receivables()
    run_rates = {
        cashflow.CAT_PAYROLL: D("14500.00"),
        cashflow.CAT_INSURANCE: D("1900.00"),
        cashflow.CAT_RENT: D("2400.00"),
        cashflow.CAT_VEHICLE: D("1600.00"),
        cashflow.CAT_LOAN: D("1750.00"),
        cashflow.CAT_OVERHEAD: D("2600.00"),
    }
    report = CashReport(
        as_of=TODAY.isoformat(),
        weeks=cashflow.DEFAULT_WEEKS,
        entity="Add Ventures Inc.",
        opening_balance=D("184500.00"),
        minimum_cash=D("50000.00"),
        run_rates_json=json.dumps({k: str(v) for k, v in run_rates.items()}),
        assumptions_json=json.dumps(
            accounting.assumptions_to_dict(cashflow.default_assumptions())
        ),
        source_label=CASH_MARK,
        created_by="Sample data",
        note="Sample forecast. The bills going out are the real ones in this "
             "system; the money coming in is made up.",
        payables_json=json.dumps([accounting.payable_to_dict(p) for p in payables]),
        receivables_json=json.dumps([accounting.receivable_to_dict(r) for r in receivables]),
    )
    session.add(report)
    session.flush()
    return report


# --- add, and take back out ------------------------------------------------

def sample_jobs(session) -> list[Job]:
    return list(session.scalars(
        select(Job).where(Job.job_number.like(f"{JOB_BAND}%"))
    ).all())


def already_seeded(session) -> bool:
    return bool(sample_jobs(session))


def remove(session) -> str:
    """Take every sample row back out, and nothing else.

    Scoped to the reserved job band and to documents this script wrote. A real
    job, a real invoice, or a forecast somebody generated is untouchable here
    even if it sits on the same page.
    """
    jobs = sample_jobs(session)
    job_ids = [j.id for j in jobs]
    docs = list(session.scalars(
        select(Document).where(Document.sha256.like(f"{DOC_MARK}%"))
    ).all())

    if job_ids:
        # Break the two references that point out of the cascade before the
        # rows they point at are deleted.
        for invoice in session.scalars(
            select(Invoice).where(Invoice.job_id.in_(job_ids))
        ).all():
            invoice.receipt_id = None
            invoice.change_order_id = None
        session.flush()
        for job in jobs:
            session.delete(job)          # cascades quotes, invoices, checks, …
        session.flush()

    for doc in docs:
        session.delete(doc)

    reports = session.scalars(
        select(CashReport).where(CashReport.source_label == CASH_MARK)
    ).all()
    for report in reports:
        session.delete(report)

    session.commit()
    return (f"removed {len(jobs)} sample job(s), {len(docs)} document(s), "
            f"{len(reports)} forecast(s)")


# The job each builder makes, so one can be recognised as already present
# without running it. Kept beside BUILDERS deliberately: a builder added
# without its number here would be rebuilt on every restart.
BUILT_JOBS = {
    job_269001: "269001", job_269002: "269002", job_269003: "269003",
    job_269004: "269004", job_269005: "269005", job_269006: "269006",
    job_269007: "269007", job_269008: "269008", job_269009: "269009",
    job_269010: "269010",
}


def seed(session) -> str:
    """Add whatever samples are not here yet, and leave the rest alone.

    Top-up rather than all-or-nothing, because the samples grow. A department
    built after the first deploy - the receipt collector was - would otherwise
    show an empty page on the live site forever, since something in the 269xxx
    band already existed and the whole seed skipped itself. Somebody would
    then have to wipe and rebuild sample data to see a feature, which is a
    silly reason to touch a database that by then holds real work too.
    """
    rebuilt = ""
    if sample_jobs(session) and installed_version(session) < SAMPLE_VERSION:
        # An older set of samples. Out with all of it and in with the new -
        # only the reserved band is touched, and topping up would leave the
        # old jobs missing whatever was added to them since.
        remove(session)
        rebuilt = " (replacing an older set)"

    existing = {job.job_number for job in sample_jobs(session)}
    todo = [build for build in BUILDERS if BUILT_JOBS[build] not in existing]
    if not todo:
        return "nothing to add"

    s = Seed(session)
    jobs = [build(s) for build in todo]

    # Routing last, once every quote, contract and change order on a job is
    # in place — a routing decision taken halfway through would be taken on
    # half the facts.
    for job in jobs:
        session.refresh(job)
        for invoice in job.invoices:
            apply_routing(invoice)

    # The forecast is built from what is here, so anything added since the
    # last one makes it stale. Replaced rather than added to - two sample
    # forecasts and no way to tell which is current is worse than one.
    for old_report in session.scalars(
        select(CashReport).where(CashReport.source_label == CASH_MARK)
    ).all():
        session.delete(old_report)
    build_cash_report(session)
    _stamp_version(session)
    session.commit()

    added = "" if len(jobs) == len(BUILDERS) else " added"
    return (f"{len(jobs)} sample job{'' if len(jobs) == 1 else 's'}{added} and a "
            f"fresh 13-week forecast{rebuilt}")


def main() -> int:
    init_db()
    session = SessionLocal()

    if "--remove" in sys.argv or "--reset" in sys.argv:
        print(remove(session))
        if "--remove" in sys.argv:
            return 0

    summary = seed(session)
    if summary == "nothing to add":
        print(f"Every sample job is already loaded ({JOB_BAND}xxx). "
              f"Re-run with --reset to rebuild, or --remove to clear them.")
        return 0

    print(summary)
    for job in sorted(sample_jobs(session), key=lambda j: j.job_number):
        print(f"  {job.job_number}  {job.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""The marked-up invoice, drawn straight to PDF.

Why not render the HTML through a headless browser, as the screen view does:
the deployment host has no Chromium, and asking the reader to use their
browser's Print / Save as PDF puts the output at the mercy of their print
settings. Most browsers - phones especially - drop background colours when
printing, and a marked-up invoice with the colours stripped out is worse than
useless: it looks checked and says nothing.

This file is going to a vendor to open a conversation about money, so it is
built with a real PDF library and the colours are part of the file.

Layout is a facsimile of the vendor's own invoice - letterhead, boxed document
number, banded header, and their column order - so it can be read side by side
with the original. Numbers come from `app.fmt`, shared with the HTML view, so
the two renderings cannot disagree.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from fpdf import FPDF

from app import fmt

# --- palette. Matches the CSS custom properties in templates/markup.html. ----
INK = (26, 31, 43)
SOFT = (74, 82, 98)
MUTE = (114, 122, 136)
RULE = (233, 235, 238)
BAND = (217, 217, 217)
BANDK = (244, 245, 247)

VERDICT_COLOURS = {
    #          background        text            left bar
    "over":  ((251, 228, 226), (155, 28, 28),  (217, 74, 61)),
    "under": ((227, 245, 234), (22, 101, 52),  (47, 158, 95)),
    "match": ((251, 239, 196), (122, 92, 0),   (217, 169, 8)),
    "none":  ((238, 240, 243), (90, 98, 112),  (168, 176, 189)),
}

# Column widths in mm, across a 190mm printable width.
W_QTY, W_UOM, W_DESC, W_PRICE, W_AMT = 20.0, 13.0, 87.0, 38.0, 32.0
LEFT = 11.0
TOP = 12.0


def _bucket(verdict: Optional[str]) -> str:
    return verdict if verdict in VERDICT_COLOURS else "none"


class _Doc(FPDF):
    """Page furniture. The footer carries the provenance line on every page."""

    def footer(self) -> None:
        self.set_y(-14)
        self.set_font("Helvetica", "", 6.5)
        self.set_text_color(*MUTE)
        self.cell(0, 4, f"Page {self.page_no()} of {{nb}}", align="R")

    # Every string on the page goes through fmt.pdf_safe on the way to the
    # font: see the note there. Overriding here rather than at each call site
    # means a new heading or column added later cannot forget to do it.
    def cell(self, w=None, h=None, text="", *args, **kwargs):
        return super().cell(w, h, fmt.pdf_safe(text), *args, **kwargs)

    def multi_cell(self, w=None, h=None, text="", *args, **kwargs):
        return super().multi_cell(w, h, fmt.pdf_safe(text), *args, **kwargs)


def _price_text(line: Any) -> str:
    price = fmt.money4(line.unit_price)
    return f"{price}/{line.price_uom}" if line.price_uom else price


def _quoted_text(line: Any) -> str:
    quoted = fmt.money4(line.quote_unit_price)
    uom = getattr(getattr(line, "quote_line", None), "price_uom", None)
    return f"quoted {quoted}/{uom}" if uom else f"quoted {quoted}"


def build(invoice: Any, job: Any, quote: Any, lines: list, out_path: Path) -> Path:
    pdf = _Doc(orientation="P", unit="mm", format="Letter")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(LEFT, TOP, LEFT)
    pdf.add_page()
    pdf.alias_nb_pages()

    _verdict_banner(pdf, invoice, quote)
    _letterhead(pdf, invoice)
    _header_band(pdf, invoice, job, quote)
    _line_items(pdf, lines, quote)
    _totals(pdf, invoice)
    _legend(pdf)
    _closing_note(pdf, invoice, job, quote)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(out_path))
    return out_path


# --------------------------------------------------------------- sections
def _verdict_banner(pdf: _Doc, invoice: Any, quote: Any) -> None:
    """The answer, before any numbers. Above the facsimile, never inside it."""
    over = invoice.overbilled_amount or Decimal(0)
    under = invoice.underbilled_amount or Decimal(0)

    if quote is None:
        key, msg, amount = "none", "No master quote on this job - nothing was checked", ""
    elif over > 0:
        key = "over"
        n = invoice.lines_over
        msg = f"Billed above the master quote on {n} line{'' if n == 1 else 's'}"
        amount = "+" + fmt.money(over)
    else:
        key = "under"
        msg = "Every line at or below the quoted price"
        amount = "-" + fmt.abs_money(under) if under > 0 else ""

    bg, ink, bar = VERDICT_COLOURS[key]
    x, y, w, h = LEFT, pdf.get_y(), 190.0, 11.0
    pdf.set_fill_color(*bg)
    pdf.rect(x, y, w, h, style="F")
    pdf.set_fill_color(*bar)
    pdf.rect(x, y, 1.6, h, style="F")

    pdf.set_xy(x + 5, y + 2.0)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(*ink)
    pdf.cell(w - 55, 6.5, msg)
    if amount:
        pdf.set_xy(x + w - 52, y + 1.6)
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(47, 7.5, amount, align="R")
    pdf.set_y(y + h + 5)


def _letterhead(pdf: _Doc, invoice: Any) -> None:
    y = pdf.get_y()
    pdf.set_xy(LEFT, y)
    pdf.set_font("Times", "B", 17)
    pdf.set_text_color(17, 17, 17)
    pdf.cell(120, 7, (invoice.vendor or "Unknown vendor")[:52])

    pdf.set_xy(LEFT, y + 7.5)
    pdf.set_font("Helvetica", "B", 6.4)
    pdf.set_text_color(*MUTE)
    pdf.cell(120, 3.5, "INVOICE  ·  REBUILT AND CHECKED AGAINST THE MASTER QUOTE")

    # Boxed document number, top right, as the vendor prints it.
    bx, bw, bh = LEFT + 137, 53.0, 13.0
    pdf.set_draw_color(0, 0, 0)
    pdf.set_line_width(0.5)
    pdf.set_fill_color(*BAND)
    pdf.rect(bx, y, bw, 5.4, style="FD")
    pdf.rect(bx, y, bw, bh, style="D")
    pdf.set_xy(bx, y + 0.7)
    pdf.set_font("Helvetica", "B", 8.5)
    pdf.set_text_color(0, 0, 0)
    pdf.cell(bw, 4, "I N V O I C E", align="C")
    pdf.set_xy(bx, y + 6.4)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(bw, 5.6, invoice.invoice_number or "-", align="C")

    pdf.set_y(y + bh + 3)


def _header_band(pdf: _Doc, invoice: Any, job: Any, quote: Any) -> None:
    if invoice.po_reference:
        pdf.set_font("Helvetica", "", 7.4)
        pdf.set_text_color(*INK)
        pdf.cell(0, 4, f"PO: {invoice.po_reference}", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1)

    quote_ref = (quote.quote_number or f"#{quote.id}") if quote else "-"
    rows = [
        [("Invoice Date:", str(invoice.invoice_date or "-"), 30.0, 34.0),
         ("Due Date:", str(invoice.due_date or "-"), 24.0, 34.0),
         ("Job:", str(job.job_number), 18.0, 50.0)],   # widths total 190mm
        [("Ship To:", (invoice.ship_to or "-")[:74], 30.0, 92.0),
         ("Quote Ref:", quote_ref, 26.0, 42.0)],
    ]
    pdf.set_line_width(0.2)
    pdf.set_draw_color(0, 0, 0)
    for row in rows:
        y = pdf.get_y()
        x = LEFT
        for label, value, lw, vw in row:
            pdf.set_xy(x, y)
            pdf.set_fill_color(*BANDK)
            pdf.set_font("Helvetica", "B", 6.6)
            pdf.set_text_color(*INK)
            pdf.cell(lw, 5.0, f" {label}", border=1, fill=True)
            pdf.set_font("Helvetica", "", 6.6)
            pdf.cell(vw, 5.0, f" {value}", border=1)
            x += lw + vw
        pdf.set_y(y + 5.0)
    pdf.ln(3)


def _line_items(pdf: _Doc, lines: list, quote: Any) -> None:
    _items_header(pdf)
    pdf.set_line_width(0.2)

    for line in lines:
        bucket = _bucket(line.verdict)
        bg, ink, bar = VERDICT_COLOURS[bucket]

        desc = line.description or ""
        pdf.set_font("Helvetica", "", 6.9)
        wrapped = pdf.multi_cell(W_DESC - 3, 3.4, desc, dry_run=True, output="LINES")
        # sku line + description lines, plus padding
        height = max(9.5, 4.2 + len(wrapped) * 3.4 + 2.0)
        # the price cell may carry a second line (the quoted price)
        if bucket in ("over", "under") or (bucket == "none" and quote is not None):
            height = max(height, 11.0)

        if pdf.get_y() + height > pdf.h - 22:
            pdf.add_page()
            _items_header(pdf)

        y = pdf.get_y()
        x = LEFT

        pdf.set_text_color(*INK)
        pdf.set_font("Helvetica", "", 7.4)
        pdf.set_xy(x, y + 1.4)
        pdf.cell(W_QTY - 2, 4, fmt.qty(line.qty), align="R")
        x += W_QTY
        pdf.set_xy(x + 1.5, y + 1.4)
        pdf.cell(W_UOM, 4, line.uom or "")
        x += W_UOM

        pdf.set_xy(x + 1.5, y + 1.2)
        pdf.set_font("Helvetica", "B", 7.2)
        pdf.cell(W_DESC - 3, 3.6, (line.sku or "-")[:30])
        pdf.set_xy(x + 1.5, y + 4.8)
        pdf.set_font("Helvetica", "", 6.9)
        pdf.multi_cell(W_DESC - 3, 3.4, desc, align="L")
        x += W_DESC

        # --- the marked-up cell. This is the whole point of the document. ---
        pdf.set_fill_color(*bg)
        pdf.rect(x, y, W_PRICE, height, style="F")
        pdf.set_fill_color(*bar)
        pdf.rect(x, y, 1.2, height, style="F")
        pdf.set_text_color(*ink)
        pdf.set_xy(x, y + 1.4)
        pdf.set_font("Helvetica", "B", 7.6)
        pdf.cell(W_PRICE - 2.5, 4, _price_text(line), align="R")

        second = ""
        if bucket in ("over", "under"):
            second = _quoted_text(line)
        elif bucket == "none":
            second = "no quote yet" if quote is None else "not on the quote"
        if second:
            pdf.set_xy(x, y + 5.3)
            pdf.set_font("Helvetica", "B", 6.2)
            pdf.cell(W_PRICE - 2.5, 3.4, second, align="R")
        if "effective" in (line.match_method or ""):
            pdf.set_xy(x, y + 8.3)
            pdf.set_font("Helvetica", "B", 5.6)
            pdf.cell(W_PRICE - 2.5, 3, "packaging adjusted", align="R")
        x += W_PRICE

        pdf.set_text_color(*INK)
        pdf.set_font("Helvetica", "", 7.4)
        pdf.set_xy(x, y + 1.4)
        pdf.cell(W_AMT - 1.5, 4, fmt.money(line.extended), align="R")
        if line.extended_variance and bucket in ("over", "under"):
            pdf.set_text_color(*ink)
            pdf.set_font("Helvetica", "B", 6.0)
            pdf.set_xy(x, y + 5.3)
            sign = "+" if bucket == "over" else "-"
            word = "over quote" if bucket == "over" else "under quote"
            pdf.cell(W_AMT - 1.5, 3.4,
                     f"{sign}{fmt.abs_money(line.extended_variance)} {word}", align="R")

        pdf.set_draw_color(*RULE)
        pdf.line(LEFT, y + height, LEFT + 190, y + height)
        pdf.set_y(y + height)

    if not lines:
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*MUTE)
        pdf.cell(190, 12, "No line items were read from this document.",
                 align="C", new_x="LMARGIN", new_y="NEXT")


def _items_header(pdf: _Doc) -> None:
    y = pdf.get_y()
    pdf.set_fill_color(*BAND)
    pdf.rect(LEFT, y, 190, 5.6, style="F")
    pdf.set_draw_color(0, 0, 0)
    pdf.set_line_width(0.4)
    pdf.line(LEFT, y, LEFT + 190, y)
    pdf.line(LEFT, y + 5.6, LEFT + 190, y + 5.6)

    pdf.set_font("Helvetica", "B", 6.4)
    pdf.set_text_color(0, 0, 0)
    for label, width, align, pad in (
        ("QUANTITY", W_QTY, "R", 2.0), ("UOM", W_UOM, "L", 1.5),
        ("ITEM/DESCRIPTION", W_DESC, "L", 1.5),
        ("PRICE/UOM", W_PRICE, "R", 2.5), ("AMOUNT", W_AMT, "R", 1.5),
    ):
        pdf.set_xy(pdf.get_x(), y + 1.2)
        pdf.cell(width - (pad if align == "R" else 0), 3.4, label, align=align)
        pdf.set_x(pdf.get_x() + (pad if align == "R" else 0))
    pdf.set_y(y + 5.6)


def _totals(pdf: _Doc, invoice: Any) -> None:
    pdf.ln(2)
    x = LEFT + 190 - 84
    rows = [("Subtotal", fmt.money(invoice.subtotal), False, False)]
    if invoice.freight:
        rows.append(("Freight / delivery", fmt.money(invoice.freight), False, False))
    if invoice.tax:
        rows.append(("Tax", fmt.money(invoice.tax), False, False))
    rows.append(("TOTAL", fmt.money(invoice.total), True, False))
    over = invoice.overbilled_amount or Decimal(0)
    if over > 0:
        rows.append(("Above quoted pricing", "+" + fmt.money(over), False, True))

    for label, value, grand, warn in rows:
        y = pdf.get_y()
        if grand:
            pdf.set_draw_color(0, 0, 0)
            pdf.set_line_width(0.4)
            pdf.line(x, y, x + 84, y)
            pdf.set_font("Helvetica", "B", 9.5)
            pdf.set_text_color(*INK)
            y += 1.0
        elif warn:
            pdf.set_font("Helvetica", "B", 7.8)
            pdf.set_text_color(*VERDICT_COLOURS["over"][1])
        else:
            pdf.set_font("Helvetica", "", 7.8)
            pdf.set_text_color(*SOFT)
        pdf.set_xy(x, y)
        pdf.cell(50, 5, label)
        pdf.cell(34, 5, value, align="R")
        pdf.set_y(y + (6.0 if grand else 5.0))
    pdf.ln(3)


def _legend(pdf: _Doc) -> None:
    y = pdf.get_y()
    pdf.set_draw_color(*RULE)
    pdf.set_line_width(0.2)
    pdf.line(LEFT, y, LEFT + 190, y)
    pdf.set_y(y + 2.5)

    items = [
        ("over", "Higher than quoted - quoted price shown beneath"),
        ("under", "Lower than quoted"),
        ("match", "Exactly as quoted"),
        ("none", "Not on the master quote"),
    ]
    x, y = LEFT, pdf.get_y()
    pdf.set_font("Helvetica", "", 6.3)
    for key, label in items:
        bg, ink, bar = VERDICT_COLOURS[key]
        pdf.set_fill_color(*bg)
        pdf.rect(x, y + 0.6, 7.0, 3.2, style="F")
        pdf.set_fill_color(*bar)
        pdf.rect(x, y + 0.6, 1.0, 3.2, style="F")
        pdf.set_xy(x + 8.2, y)
        pdf.set_text_color(*SOFT)
        width = pdf.get_string_width(label) + 5
        pdf.cell(width, 4.4, label)
        x += 8.2 + width
    pdf.set_y(y + 6)


def _closing_note(pdf: _Doc, invoice: Any, job: Any, quote: Any) -> None:
    unmatched = invoice.lines_unmatched or 0
    if unmatched:
        y = pdf.get_y()
        pdf.set_fill_color(253, 243, 224)
        pdf.rect(LEFT, y, 190, 9.5, style="F")
        pdf.set_fill_color(201, 138, 18)
        pdf.rect(LEFT, y, 1.4, 9.5, style="F")
        pdf.set_xy(LEFT + 4, y + 1.2)
        pdf.set_font("Helvetica", "", 6.6)
        pdf.set_text_color(122, 83, 16)
        pdf.multi_cell(
            183, 3.3,
            f"{unmatched} line{'' if unmatched == 1 else 's'} could not be matched to the "
            "master quote. That may be legitimate - an extra item, a delivery charge, or "
            "wording that differs from the quote - but it means those prices were not "
            "checked against anything.",
        )
        pdf.set_y(y + 11.5)

    quote_ref = (quote.quote_number or f"#{quote.id}") if quote else "none on file"
    pdf.set_font("Helvetica", "", 6.2)
    pdf.set_text_color(*MUTE)
    pdf.multi_cell(
        190, 3.2,
        f"Every unit price on this invoice was compared against the same item on the master "
        f"quote for job {job.job_number} ({quote_ref}). Figures were read from the vendor's "
        f"document; the comparison is exact decimal arithmetic. Source file: "
        f"{invoice.document.filename}.",
    )

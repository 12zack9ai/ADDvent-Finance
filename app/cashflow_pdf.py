"""The 13-day cash flow forecast as a PDF.

Same reasoning as the marked-up invoice: this goes to an owner or a bank, so
the colours are in the file rather than at the mercy of a browser's print
settings, and no Chromium is required on the host.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from fpdf import FPDF

from app import fmt
from app.cashflow import Forecast

INK = (26, 31, 43)
SOFT = (74, 82, 98)
MUTE = (114, 122, 136)
RULE = (223, 227, 233)
NAVY = (30, 58, 95)
BAND = (238, 240, 243)
RED_BG, RED_INK = (251, 228, 226), (155, 28, 28)
GREEN_BG, GREEN_INK = (227, 245, 234), (22, 101, 52)
AMBER_BG, AMBER_INK = (253, 243, 224), (122, 83, 16)

LEFT, TOP, WIDTH = 12.0, 12.0, 186.0


class _Doc(FPDF):
    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Helvetica", "", 6.5)
        self.set_text_color(*MUTE)
        self.cell(0, 4, f"Page {self.page_no()} of {{nb}}", align="R")


def build(f: Forecast, report: Any, out_path: Path) -> Path:
    pdf = _Doc(orientation="P", unit="mm", format="Letter")
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.set_margins(LEFT, TOP, LEFT)
    pdf.add_page()
    pdf.alias_nb_pages()

    _title(pdf, f, report)
    _headline(pdf, f)
    _daily(pdf, f)
    _detail(pdf, f)
    _provenance(pdf, f, report)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(out_path))
    return out_path


def _title(pdf: _Doc, f: Forecast, report: Any) -> None:
    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(*NAVY)
    pdf.cell(0, 7, "13-day cash flow", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*MUTE)
    pdf.cell(0, 4.5, f"{f.start:%d %b %Y} to {f.end:%d %b %Y}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)


def _headline(pdf: _Doc, f: Forecast) -> None:
    cells = [
        ("Opening balance", fmt.money(f.opening_balance), None),
        ("Going out", "-" + fmt.money(f.total_out), RED_INK if f.total_out else None),
        ("Coming in", "+" + fmt.money(f.total_in), GREEN_INK if f.total_in else None),
        ("Projected close", fmt.money(f.closing_balance),
         RED_INK if f.closing_balance < 0 else None),
    ]
    width = WIDTH / 4
    y = pdf.get_y()
    pdf.set_fill_color(*BAND)
    pdf.rect(LEFT, y, WIDTH, 15, style="F")
    for i, (label, value, colour) in enumerate(cells):
        x = LEFT + i * width
        pdf.set_xy(x + 3, y + 2.5)
        pdf.set_font("Helvetica", "", 6.4)
        pdf.set_text_color(*MUTE)
        pdf.cell(width - 6, 3.5, label.upper())
        pdf.set_xy(x + 3, y + 6.5)
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(*(colour or INK))
        pdf.cell(width - 6, 6, value)
    pdf.set_y(y + 18)

    low = f.low_point
    if low is not None and f.goes_negative:
        _banner(pdf, RED_BG, RED_INK,
                f"The account goes negative on {low.on:%a %d %b} - "
                f"low point {fmt.money(low.closing)}.")
    elif low is not None:
        _banner(pdf, GREEN_BG, GREEN_INK,
                f"Lowest point is {fmt.money(low.closing)} on {low.on:%a %d %b}.")


def _banner(pdf: _Doc, bg, ink, text: str) -> None:
    y = pdf.get_y()
    pdf.set_fill_color(*bg)
    pdf.rect(LEFT, y, WIDTH, 8, style="F")
    pdf.set_xy(LEFT + 4, y + 1.6)
    pdf.set_font("Helvetica", "B", 8.5)
    pdf.set_text_color(*ink)
    pdf.cell(WIDTH - 8, 5, text)
    pdf.set_y(y + 11)


def _daily(pdf: _Doc, f: Forecast) -> None:
    _heading(pdf, "Day by day")
    cols = [("DAY", 44, "L"), ("OUT", 34, "R"), ("IN", 34, "R"),
            ("NET", 34, "R"), ("BALANCE", 40, "R")]
    _row_header(pdf, cols)
    for day in f.days:
        y = pdf.get_y()
        if day.is_overdrawn:
            pdf.set_fill_color(*RED_BG)
            pdf.rect(LEFT, y, WIDTH, 5.6, style="F")
        pdf.set_xy(LEFT, y + 1.1)
        pdf.set_font("Helvetica", "B" if day.is_overdrawn else "", 7.2)
        pdf.set_text_color(*(RED_INK if day.is_overdrawn else INK))
        values = [
            f"{day.on:%a %d %b}",
            "-" + fmt.money(day.out) if day.out else "",
            "+" + fmt.money(day.incoming) if day.incoming else "",
            ("-" + fmt.abs_money(day.net) if day.net < 0 else "+" + fmt.money(day.net))
            if day.net else "",
            fmt.money(day.closing),
        ]
        for (label, width, align), value in zip(cols, values):
            pdf.cell(width - 2, 3.6, value, align=align)
            pdf.set_x(pdf.get_x() + 2)
        pdf.set_draw_color(*RULE)
        pdf.line(LEFT, y + 5.6, LEFT + WIDTH, y + 5.6)
        pdf.set_y(y + 5.6)
    pdf.ln(4)


def _detail(pdf: _Doc, f: Forecast) -> None:
    if f.overdue_payables:
        _heading(pdf, "Overdue - pay these first")
        _list(pdf, [(p.vendor, p.reference, p.job_number,
                     f"due {p.due_date:%d %b}", fmt.money(p.amount)) for p in f.overdue_payables],
              ink=RED_INK)
    if f.discounts:
        _heading(pdf, "Early-payment discounts expiring")
        _list(pdf, [(p.vendor, p.reference, "",
                     f"by {p.discount_deadline:%d %b}",
                     f"save {fmt.money(p.discount_amount)}") for p in f.discounts],
              ink=GREEN_INK)
    if f.held_payables:
        _heading(pdf, "Held - not scheduled to pay")
        _list(pdf, [(p.vendor, p.reference, p.job_number,
                     p.hold_reason or "on hold", fmt.money(p.amount)) for p in f.held_payables],
              ink=AMBER_INK)
    if f.overdue_receivables:
        _heading(pdf, "Overdue in - collections")
        _list(pdf, [(r.customer, r.reference, r.job_number,
                     f"due {r.due_date:%d %b}" if r.due_date else "",
                     fmt.money(r.amount)) for r in f.overdue_receivables], ink=AMBER_INK)


def _heading(pdf: _Doc, text: str) -> None:
    if pdf.get_y() > pdf.h - 40:
        pdf.add_page()
    pdf.set_font("Helvetica", "B", 8.5)
    pdf.set_text_color(*NAVY)
    pdf.cell(0, 5, text, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(0.5)


def _row_header(pdf: _Doc, cols) -> None:
    y = pdf.get_y()
    pdf.set_fill_color(*BAND)
    pdf.rect(LEFT, y, WIDTH, 5, style="F")
    pdf.set_xy(LEFT, y + 1.1)
    pdf.set_font("Helvetica", "B", 6.2)
    pdf.set_text_color(*SOFT)
    for label, width, align in cols:
        pdf.cell(width - 2, 3.2, label, align=align)
        pdf.set_x(pdf.get_x() + 2)
    pdf.set_y(y + 5)


def _list(pdf: _Doc, rows, ink=INK) -> None:
    for who, ref, job, when, amount in rows:
        if pdf.get_y() > pdf.h - 24:
            pdf.add_page()
        y = pdf.get_y()
        pdf.set_xy(LEFT, y + 0.8)
        pdf.set_font("Helvetica", "", 7.0)
        pdf.set_text_color(*INK)
        pdf.cell(62, 3.6, who[:44])
        pdf.set_text_color(*MUTE)
        pdf.cell(34, 3.6, ref[:22])
        pdf.cell(22, 3.6, job)
        pdf.set_text_color(*ink)
        pdf.cell(34, 3.6, when)
        pdf.set_font("Helvetica", "B", 7.0)
        pdf.set_text_color(*INK)
        pdf.cell(34, 3.6, amount, align="R")
        pdf.set_draw_color(*RULE)
        pdf.line(LEFT, y + 5.2, LEFT + WIDTH, y + 5.2)
        pdf.set_y(y + 5.2)
    pdf.ln(3)


def _provenance(pdf: _Doc, f: Forecast, report: Any) -> None:
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 6.2)
    pdf.set_text_color(*MUTE)
    sources = ", ".join(f.sources) or "none recorded"
    unscheduled = len(f.unscheduled_payables) + len(f.unscheduled_receivables)
    text = (f"Generated {report.created_at:%d %b %Y at %H:%M} from: {sources}. "
            f"Opening balance {fmt.money(f.opening_balance)} was entered by hand. ")
    if unscheduled:
        text += (f"{unscheduled} item(s) carry no date and are not in the day-by-day "
                 "figures. ")
    if f.held_payables:
        text += (f"{len(f.held_payables)} invoice(s) are held by the three-way match and "
                 "are not scheduled to pay. ")
    text += "Overdue payables are shown on day one because they still have to be paid."
    pdf.multi_cell(WIDTH, 3.2, text)

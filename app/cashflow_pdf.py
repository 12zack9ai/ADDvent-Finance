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

AMBER_CELL = (253, 243, 224)
RUNRATE = (242, 244, 248)

# Landscape: thirteen weekly columns plus a label column do not fit portrait.
LEFT, TOP, WIDTH = 10.0, 10.0, 259.0
LABEL_W = 58.0


class _Doc(FPDF):
    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Helvetica", "", 6.5)
        self.set_text_color(*MUTE)
        self.cell(0, 4, f"Page {self.page_no()} of {{nb}}", align="R")


def build(f: Forecast, report: Any, out_path: Path) -> Path:
    pdf = _Doc(orientation="L", unit="mm", format="Letter")
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.set_margins(LEFT, TOP, LEFT)
    pdf.add_page()
    pdf.alias_nb_pages()

    _title(pdf, f, report)
    _headline(pdf, f)
    _grid(pdf, f)
    _detail(pdf, f)
    _provenance(pdf, f, report)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(out_path))
    return out_path


def _title(pdf: _Doc, f: Forecast, report: Any) -> None:
    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(*NAVY)
    title = "13-week cash flow" + (f"  -  {f.entity}" if f.entity else "")
    pdf.cell(0, 7, title, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*MUTE)
    pdf.cell(0, 4.5,
             f"Rolling forecast, {f.weeks[0].starts:%d %b %Y} to {f.weeks[-1].ends:%d %b %Y}",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)


def _headline(pdf: _Doc, f: Forecast) -> None:
    cells = [
        ("Opening balance", fmt.money(f.opening_balance), None),
        ("13-week outflow", "-" + fmt.money(f.total_out), RED_INK if f.total_out else None),
        ("13-week inflow", "+" + fmt.money(f.total_in), GREEN_INK if f.total_in else None),
        ("Week 13 balance", fmt.money(f.closing_balance),
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
    first_bad = f.first_negative_week
    below = f.first_below_target
    if first_bad is not None:
        _banner(pdf, RED_BG, RED_INK,
                f"Cash runs out in week {first_bad.number}, ending "
                f"{first_bad.ends:%d %B}. Low point {fmt.money(low.closing)} "
                f"in week {low.number}.")
    elif below is not None:
        _banner(pdf, AMBER_CELL, AMBER_INK,
                f"Drops below the {fmt.money(f.minimum_cash)} floor in week "
                f"{below.number}, ending {below.ends:%d %B}.")
    elif low is not None:
        _banner(pdf, GREEN_BG, GREEN_INK,
                f"Lowest point is {fmt.money(low.closing)} in week {low.number}.")


def _banner(pdf: _Doc, bg, ink, text: str) -> None:
    y = pdf.get_y()
    pdf.set_fill_color(*bg)
    pdf.rect(LEFT, y, WIDTH, 8, style="F")
    pdf.set_xy(LEFT + 4, y + 1.6)
    pdf.set_font("Helvetica", "B", 8.5)
    pdf.set_text_color(*ink)
    pdf.cell(WIDTH - 8, 5, text)
    pdf.set_y(y + 11)


def _grid(pdf: _Doc, f: Forecast) -> None:
    """The whole quarter on one page: rows are categories, columns are weeks."""
    col = (WIDTH - LABEL_W - 22) / len(f.weeks)
    _grid_header(pdf, f, col)

    def row(label, values, *, bold=False, rule_above=False, tone=None,
            shaded=(), total=None, size=5.6):
        y = pdf.get_y()
        if rule_above:
            pdf.set_draw_color(*NAVY)
            pdf.set_line_width(0.3)
            pdf.line(LEFT, y, LEFT + WIDTH, y)
        pdf.set_xy(LEFT + 1, y + 0.9)
        pdf.set_font("Helvetica", "B" if bold else "", size)
        pdf.set_text_color(*INK)
        pdf.cell(LABEL_W - 2, 3.4, label[:46])
        for i, value in enumerate(values):
            x = LEFT + LABEL_W + i * col
            week = f.weeks[i]
            if i in shaded:
                pdf.set_fill_color(*RUNRATE)
                pdf.rect(x, y, col, 4.8, style="F")
            pdf.set_xy(x, y + 0.9)
            pdf.set_text_color(*(tone or INK))
            pdf.cell(col - 1, 3.4, value, align="R")
        if total is not None:
            pdf.set_xy(LEFT + LABEL_W + len(values) * col, y + 0.9)
            pdf.set_font("Helvetica", "B", size)
            pdf.set_text_color(*(tone or INK))
            pdf.cell(21, 3.4, total, align="R")
        pdf.set_draw_color(*RULE)
        pdf.set_line_width(0.15)
        pdf.line(LEFT, y + 4.8, LEFT + WIDTH, y + 4.8)
        pdf.set_y(y + 4.8)

    def m(value):
        return fmt.money(value) if value else ""

    row("Beginning cash", [m(w.opening) for w in f.weeks], tone=SOFT)
    _section(pdf, "CASH INFLOWS", col, f)
    row("Customer collections", [m(w.inflow) for w in f.weeks],
        tone=GREEN_INK, total=fmt.money(f.total_in))
    _section(pdf, "CASH OUTFLOWS", col, f)
    for category in f.used_categories:
        shaded = {i for i, w in enumerate(f.weeks) if category in w.run_rate_categories}
        row(category, [m(w.by_category.get(category)) for w in f.weeks],
            shaded=shaded, total=fmt.money(f.category_total(category)))
    row("Total outflows", [m(w.outflow) for w in f.weeks], bold=True,
        rule_above=True, tone=RED_INK, total=fmt.money(f.total_out))
    row("Net cash flow", [m(w.net) for w in f.weeks], bold=True,
        total=fmt.money(f.net_movement))

    # Ending cash, with the bad weeks coloured.
    y = pdf.get_y()
    pdf.set_draw_color(*NAVY)
    pdf.set_line_width(0.4)
    pdf.line(LEFT, y, LEFT + WIDTH, y)
    pdf.set_xy(LEFT + 1, y + 1.0)
    pdf.set_font("Helvetica", "B", 6.0)
    pdf.set_text_color(*INK)
    pdf.cell(LABEL_W - 2, 3.6, "Ending cash")
    for i, week in enumerate(f.weeks):
        x = LEFT + LABEL_W + i * col
        if week.closing < 0:
            pdf.set_fill_color(*RED_BG)
            pdf.rect(x, y, col, 5.4, style="F")
            ink = RED_INK
        elif week.below_target(f.minimum_cash):
            pdf.set_fill_color(*AMBER_CELL)
            pdf.rect(x, y, col, 5.4, style="F")
            ink = AMBER_INK
        else:
            ink = INK
        pdf.set_xy(x, y + 1.0)
        pdf.set_text_color(*ink)
        pdf.cell(col - 1, 3.6, fmt.money(week.closing), align="R")
    pdf.set_y(y + 6.5)

    pdf.set_font("Helvetica", "", 5.6)
    pdf.set_text_color(*MUTE)
    note = ("Shaded = weekly run-rate used, because no bill is on file for that "
            "category that week.")
    if f.minimum_cash:
        note += f" Amber = below the {fmt.money(f.minimum_cash)} floor."
    note += " Red = overdrawn."
    pdf.cell(0, 3.4, note, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)


def _grid_header(pdf: _Doc, f: Forecast, col: float) -> None:
    y = pdf.get_y()
    pdf.set_fill_color(*BAND)
    pdf.rect(LEFT, y, WIDTH, 7.2, style="F")
    pdf.set_xy(LEFT + 1, y + 1.0)
    pdf.set_font("Helvetica", "B", 5.6)
    pdf.set_text_color(*SOFT)
    pdf.cell(LABEL_W - 2, 3.0, "WEEK ENDING")
    for i, week in enumerate(f.weeks):
        x = LEFT + LABEL_W + i * col
        pdf.set_xy(x, y + 0.7)
        pdf.set_text_color(*(RED_INK if week.closing < 0 else SOFT))
        pdf.cell(col - 1, 3.0, f"{week.ends:%d %b}", align="R")
        pdf.set_xy(x, y + 3.6)
        pdf.set_font("Helvetica", "", 5.0)
        pdf.cell(col - 1, 3.0, f"Wk {week.number}", align="R")
        pdf.set_font("Helvetica", "B", 5.6)
    pdf.set_xy(LEFT + LABEL_W + len(f.weeks) * col, y + 1.0)
    pdf.set_text_color(*SOFT)
    pdf.cell(21, 3.0, "TOTAL", align="R")
    pdf.set_draw_color(*NAVY)
    pdf.set_line_width(0.4)
    pdf.line(LEFT, y + 7.2, LEFT + WIDTH, y + 7.2)
    pdf.set_y(y + 7.2)


def _section(pdf: _Doc, label: str, col: float, f: Forecast) -> None:
    y = pdf.get_y()
    pdf.set_fill_color(248, 249, 251)
    pdf.rect(LEFT, y, WIDTH, 4.6, style="F")
    pdf.set_xy(LEFT + 1, y + 0.8)
    pdf.set_font("Helvetica", "B", 5.6)
    pdf.set_text_color(*NAVY)
    pdf.cell(WIDTH - 2, 3.2, label)
    pdf.set_y(y + 4.6)


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
    if f.unscheduled_receivables:
        _heading(pdf, "Over 90 days - collections, not counted as arriving")
        _list(pdf, [(r.customer, r.reference, r.bucket,
                     f"{fmt.money(r.expected_amount)} likely",
                     fmt.money(r.amount)) for r in f.unscheduled_receivables], ink=AMBER_INK)
    if f.backlog:
        _heading(pdf, f"Backlog - real work, no assigned week ({fmt.money(f.backlog_total)})")
        _list(pdf, [(r.customer, r.memo[:30], "", "unscheduled",
                     fmt.money(r.amount)) for r in f.backlog], ink=MUTE)
    if f.beyond_horizon:
        _heading(pdf, "Due after week 13")
        _list(pdf, [(p.vendor, p.reference, p.job_number,
                     f"due {p.due_date:%d %b %Y}", fmt.money(p.amount))
                    for p in f.beyond_horizon], ink=MUTE)


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
    unscheduled = len(f.unscheduled_payables)
    text = (f"Generated {report.created_at:%d %b %Y at %H:%M} from: {sources}. "
            f"Opening balance {fmt.money(f.opening_balance)} was entered by hand. ")
    if f.run_rates:
        rates = ", ".join(f"{c.split(' (')[0]} {fmt.money(v)}/wk" for c, v in f.run_rates.items())
        text += f"Weekly run-rates where no bill was on file: {rates}. "
    text += ("Receivables are collected by aging bucket with a collectability "
             "percentage; anything over 90 days is listed rather than counted. ")
    if unscheduled:
        text += (f"{unscheduled} item(s) carry no date and are not in the day-by-day "
                 "figures. ")
    if f.held_payables:
        text += (f"{len(f.held_payables)} invoice(s) are held by the three-way match and "
                 "are not scheduled to pay. ")
    text += "Overdue payables are shown on day one because they still have to be paid."
    pdf.multi_cell(WIDTH, 3.2, text)

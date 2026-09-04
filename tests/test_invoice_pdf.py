"""The marked-up PDF.

This file is the deliverable - it gets emailed to the vendor to open a
conversation about money - so the tests are about the things that would make it
misleading rather than merely ugly: a missing variance, a colour that does not
correspond to the verdict, or a silent failure that produces an empty document.
"""
from decimal import Decimal
from types import SimpleNamespace

import pypdfium2 as pdfium
import pytest

from app import invoice_pdf


def line(**kw):
    base = dict(line_no=1, sku="GAFT3CH", description="GAF TIMBERLINE HDZ CHARCOAL 3 BN/SQ",
                qty=Decimal("48"), uom="SQ", price_uom="SQ", unit_price=Decimal("121.00"),
                extended=Decimal("5808.00"), quote_unit_price=Decimal("118.75"),
                verdict="over", unit_variance=Decimal("2.25"),
                extended_variance=Decimal("108.00"), match_method="sku",
                # a matched line always carries its quote line in production
                quote_line=SimpleNamespace(price_uom="SQ"))
    base.update(kw)
    return SimpleNamespace(**base)


def build(tmp_path, lines, **over):
    inv = SimpleNamespace(
        invoice_number="07RM0003119045", invoice_date="2026-09-18", due_date="2026-10-08",
        vendor="New Castle Building Products", po_reference="260000",
        ship_to="Add Ventures Construction Svcs", page_info="1 of 1",
        subtotal=Decimal("8126.10"), tax=None, freight=None, total=Decimal("8126.10"),
        overbilled_amount=Decimal("108.00"), underbilled_amount=Decimal("0"),
        lines_over=1, lines_under=0, lines_match=2, lines_unmatched=0,
        document=SimpleNamespace(filename="03-INVOICE.pdf"),
    )
    for k, v in over.items():
        setattr(inv, k, v)
    job = SimpleNamespace(job_number="260000", name="")
    quote = SimpleNamespace(quote_number="07RM0002891004", id=1)
    out = tmp_path / "checked.pdf"
    return invoice_pdf.build(inv, job, quote, lines, out)


def text_of(path):
    doc = pdfium.PdfDocument(str(path))
    return "\n".join(page.get_textpage().get_text_range() for page in doc)


def test_produces_a_readable_pdf(tmp_path):
    out = build(tmp_path, [line()])
    assert out.exists() and out.stat().st_size > 1000
    assert pdfium.PdfDocument(str(out))[0].get_size() == (612.0, 792.0)   # US Letter


def test_the_quoted_price_appears_next_to_the_billed_one(tmp_path):
    """The single most important thing on the page: what they said vs what they billed."""
    body = text_of(build(tmp_path, [line()]))
    assert "$121.00/SQ" in body
    assert "quoted $118.75/SQ" in body


def test_the_dollar_impact_is_stated(tmp_path):
    body = text_of(build(tmp_path, [line()]))
    assert "+$108.00 over quote" in body


def test_an_unquoted_line_says_so_rather_than_looking_checked(tmp_path):
    unquoted = line(sku="NP1-10OZ", verdict="not_on_quote", quote_unit_price=None,
                    unit_variance=None, extended_variance=None, match_method="none")
    body = text_of(build(tmp_path, [unquoted], lines_over=0, lines_unmatched=1,
                         overbilled_amount=Decimal("0")))
    assert "not on the quote" in body
    assert "were not checked against anything" in body


def test_a_line_billed_under_quote_is_shown_as_under(tmp_path):
    under = line(unit_price=Decimal("56.00"), quote_unit_price=Decimal("57.25"),
                 verdict="under", unit_variance=Decimal("1.25"),
                 extended_variance=Decimal("6.25"))
    body = text_of(build(tmp_path, [under], lines_over=0, overbilled_amount=Decimal("0"),
                         underbilled_amount=Decimal("6.25")))
    assert "quoted $57.25/SQ" in body
    assert "-$6.25 under quote" in body


def test_packaging_adjusted_lines_say_so(tmp_path):
    """8 PK at $155.00/BX is not a 400% drop, and the reader must be told why."""
    packed = line(sku="BB-SFM558", uom="PK", price_uom="BX", verdict="match",
                  unit_price=Decimal("155.00"), quote_unit_price=Decimal("155.00"),
                  unit_variance=Decimal("0"), extended_variance=None,
                  match_method="sku:effective")
    body = text_of(build(tmp_path, [packed], lines_over=0, overbilled_amount=Decimal("0")))
    assert "packaging adjusted" in body


def test_a_long_description_does_not_collide_with_the_next_row(tmp_path):
    long_line = line(description="GAF COBRA SNOW COUNTRY ADVANCED EXHAUST RIDGE VENT "
                                 "11 1/2 INCH BY 4 FOOT PIECE, 10 PC/BX, SPECIAL ORDER ITEM")
    out = build(tmp_path, [long_line, line(sku="SECOND")])
    body = text_of(out)
    assert "SECOND" in body
    assert pdfium.PdfDocument(str(out)).__len__() == 1


def test_many_lines_paginate_rather_than_overflow(tmp_path):
    out = build(tmp_path, [line(sku=f"ITEM{i:03}") for i in range(60)])
    doc = pdfium.PdfDocument(str(out))
    assert len(doc) > 1
    assert "ITEM059" in text_of(out)


def test_no_quote_on_file_is_stated_plainly(tmp_path):
    inv_lines = [line(verdict="not_on_quote", quote_unit_price=None,
                      unit_variance=None, extended_variance=None, match_method="none")]
    out = tmp_path / "noquote.pdf"
    inv = SimpleNamespace(
        invoice_number="X1", invoice_date="2026-09-18", due_date=None, vendor="Vendor",
        po_reference=None, ship_to=None, page_info=None, subtotal=Decimal("100"),
        tax=None, freight=None, total=Decimal("100"), overbilled_amount=Decimal("0"),
        underbilled_amount=Decimal("0"), lines_over=0, lines_under=0, lines_match=0,
        lines_unmatched=1, document=SimpleNamespace(filename="x.pdf"),
    )
    invoice_pdf.build(inv, SimpleNamespace(job_number="260000", name=""), None, inv_lines, out)
    body = text_of(out)
    assert "No master quote" in body
    assert "no quote yet" in body

"""Tests for the comparison engine.

This is the code that decides whether a vendor overcharged, and by how much, so
it gets the coverage. Nothing here touches the network or the Claude API - the
engine is pure arithmetic by design, which is exactly what makes it testable.

    pytest tests/ -v
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.matching import (  # noqa: E402
    QuoteIndex,
    compare_invoice,
    comparison_price,
    effective_unit_price,
    match_line,
    norm_sku,
    norm_text,
    norm_vendor,
    units_disagree,
    uom_compatible,
    vendor_matches,
)
from app.models import (  # noqa: E402
    VERDICT_MATCH,
    VERDICT_NOT_ON_QUOTE,
    VERDICT_OVER,
    VERDICT_UNDER,
    InvoiceLine,
    QuoteLine,
)

D = Decimal


def q(sku="", desc="", qty="10", uom="EA", price="10.00", line_no=1, _id=None):
    line = QuoteLine(
        line_no=line_no, sku=sku, description=desc,
        qty=D(qty), uom=uom, unit_price=D(price),
        extended=D(qty) * D(price),
    )
    line.id = _id if _id is not None else line_no
    return line


def inv(sku="", desc="", qty="10", uom="EA", price="10.00", line_no=1):
    return InvoiceLine(
        line_no=line_no, sku=sku, description=desc,
        qty=D(qty) if qty is not None else None, uom=uom,
        unit_price=D(price) if price is not None else None,
        extended=(D(qty) * D(price)) if (qty is not None and price is not None) else None,
    )


# --- verdicts -------------------------------------------------------------

def test_exact_price_is_a_match():
    quote = [q(sku="A1", desc="Shingles", price="118.50")]
    lines = [inv(sku="A1", desc="Shingles", price="118.50")]
    summary = compare_invoice(lines, quote)
    assert lines[0].verdict == VERDICT_MATCH
    assert summary.lines_match == 1
    assert summary.overbilled == 0


def test_billed_higher_is_over_and_computes_dollar_impact():
    quote = [q(sku="A1", desc="Nails", price="52.00")]
    lines = [inv(sku="A1", desc="Nails", qty="8", price="58.00")]
    summary = compare_invoice(lines, quote)
    assert lines[0].verdict == VERDICT_OVER
    assert lines[0].unit_variance == D("6.00")
    assert lines[0].extended_variance == D("48.00")   # 8 x $6
    assert summary.overbilled == D("48.00")


def test_billed_lower_is_under_and_does_not_count_as_overbilling():
    quote = [q(sku="A1", desc="Ice shield", price="112.75")]
    lines = [inv(sku="A1", desc="Ice shield", qty="14", price="108.00")]
    summary = compare_invoice(lines, quote)
    assert lines[0].verdict == VERDICT_UNDER
    assert lines[0].unit_variance == D("-4.75")
    assert summary.overbilled == 0
    assert summary.underbilled == D("66.50")          # 14 x $4.75


def test_line_absent_from_quote_is_flagged_not_silently_accepted():
    quote = [q(sku="A1", desc="Shingles", price="118.50")]
    lines = [inv(sku="", desc="Dumpster haul-off fee", qty="1", price="485.00")]
    summary = compare_invoice(lines, quote)
    assert lines[0].verdict == VERDICT_NOT_ON_QUOTE
    assert summary.lines_unmatched == 1
    assert summary.overbilled == 0


def test_a_penny_over_is_still_over():
    """No silent tolerance. The quoted price is the quoted price."""
    quote = [q(sku="A1", price="10.00")]
    lines = [inv(sku="A1", qty="100", price="10.01")]
    compare_invoice(lines, quote)
    assert lines[0].verdict == VERDICT_OVER
    assert lines[0].extended_variance == D("1.00")


def test_fractional_cent_pricing_is_not_rounded_away():
    """4-decimal unit prices are real; rounding to cents would hide the variance."""
    quote = [q(sku="A1", price="4.1025")]
    lines = [inv(sku="A1", qty="1000", price="4.1075")]
    compare_invoice(lines, quote)
    assert lines[0].verdict == VERDICT_OVER
    assert lines[0].unit_variance == D("0.0050")
    assert lines[0].extended_variance == D("5.0000")


def test_missing_unit_price_is_not_guessed():
    """A matched line with no printed price must not be scored as 'as quoted'."""
    quote = [q(sku="A1", desc="Shingles", price="118.50")]
    lines = [inv(sku="A1", desc="Shingles", price=None, qty="5")]
    summary = compare_invoice(lines, quote)
    assert lines[0].verdict == VERDICT_NOT_ON_QUOTE
    assert "no_price" in lines[0].match_method
    assert summary.overbilled == 0


def test_no_master_quote_means_everything_is_unmatched():
    lines = [inv(sku="A1", price="10.00"), inv(sku="B2", price="20.00", line_no=2)]
    summary = compare_invoice(lines, [])
    assert all(l.verdict == VERDICT_NOT_ON_QUOTE for l in lines)
    assert summary.lines_unmatched == 2


# --- line alignment -------------------------------------------------------

def test_sku_match_wins_over_a_better_looking_description():
    """A confident part-number match must not be overridden by fuzzy text."""
    quote = [
        q(sku="NL-125", desc="Completely different wording", price="52.00", line_no=1, _id=1),
        q(sku="OTHER", desc="Roofing nails coil", price="99.00", line_no=2, _id=2),
    ]
    line = inv(sku="NL-125", desc="Roofing nails coil", price="52.00")
    matched, method = match_line(line, QuoteIndex.build(quote))
    assert method == "sku"
    assert matched.sku == "NL-125"


def test_description_matches_despite_case_and_punctuation():
    quote = [q(sku="", desc="Drip Edge, 10 ft. (white)", price="12.40")]
    line = inv(sku="", desc="drip edge 10 ft white", price="12.40")
    matched, method = match_line(line, QuoteIndex.build(quote))
    assert matched is not None
    assert method in ("exact", "fuzzy")


def test_fuzzy_match_is_rejected_when_units_are_incompatible():
    """Same words, different unit of measure, is a different line item."""
    quote = [q(sku="", desc="Underlayment synthetic roll", uom="RL", price="89.00")]
    line = inv(sku="", desc="Underlayment synthetic roll", uom="SQ", price="95.00")
    matched, _ = match_line(line, QuoteIndex.build(quote))
    assert matched is None


def test_equivalent_unit_names_are_treated_as_the_same():
    quote = [q(sku="", desc="Pipe boot", uom="EA", price="18.75")]
    line = inv(sku="", desc="Pipe boot", uom="EACH", price="18.75")
    matched, _ = match_line(line, QuoteIndex.build(quote))
    assert matched is not None


def test_one_quote_line_prices_many_deliveries():
    """Materials arrive in several loads; the quote is a price list, not a budget."""
    quote = [q(sku="SHG", desc="Shingles", qty="184", price="118.50")]
    first = [inv(sku="SHG", desc="Shingles", qty="92", price="118.50")]
    second = [inv(sku="SHG", desc="Shingles", qty="92", price="118.50")]
    compare_invoice(first, quote)
    compare_invoice(second, quote)
    assert first[0].verdict == VERDICT_MATCH
    assert second[0].verdict == VERDICT_MATCH


# --- normalisation --------------------------------------------------------

def test_sku_normalisation_ignores_punctuation_and_case():
    assert norm_sku("NL-CL/125") == norm_sku("nlcl125") == "nlcl125"


def test_text_normalisation_collapses_noise():
    assert norm_text("  Drip Edge,  10 ft. (WHITE) ") == "drip edge 10 ft white"


def test_unknown_units_do_not_block_a_match():
    assert uom_compatible("", "EA")
    assert uom_compatible("EA", "")
    assert not uom_compatible("SQ", "RL")


# --- roll-up --------------------------------------------------------------

def test_summary_totals_across_a_mixed_invoice():
    quote = [
        q(sku="A", desc="Shingles", price="118.50", line_no=1, _id=1),
        q(sku="B", desc="Nails", price="52.00", line_no=2, _id=2),
        q(sku="C", desc="Ice shield", price="112.75", line_no=3, _id=3),
    ]
    lines = [
        inv(sku="A", desc="Shingles", qty="92", price="118.50", line_no=1),   # match
        inv(sku="B", desc="Nails", qty="8", price="58.00", line_no=2),        # over  $48
        inv(sku="C", desc="Ice shield", qty="14", price="108.00", line_no=3), # under $66.50
        inv(sku="", desc="Fuel surcharge", qty="1", price="75.00", line_no=4),# unmatched
    ]
    s = compare_invoice(lines, quote)
    assert (s.lines_match, s.lines_over, s.lines_under, s.lines_unmatched) == (1, 1, 1, 1)
    assert s.overbilled == D("48.00")
    assert s.underbilled == D("66.50")
    assert s.net_variance == D("-18.50")
    assert s.has_issues is True


# --- packaging / unit-of-measure mismatch --------------------------------
# Taken from a real New Castle Building Products quote:
#     QUANTITY 8  UOM PK  PRICE 155.00/BX  AMOUNT 248.00
# 5 packs make a box, so the price is per box while the count is in packs.
# 8 x 155 = 1,240, but the printed amount is 248 (1.6 boxes). Comparing the
# printed 155.00 against an invoice showing 31.00/PK would report a price
# collapse that never happened.

def q2(sku="", desc="", qty=None, uom="", price=None, price_uom="", extended=None, line_no=1, _id=1):
    line = QuoteLine(
        line_no=line_no, sku=sku, description=desc,
        qty=D(qty) if qty is not None else None, uom=uom, price_uom=price_uom,
        unit_price=D(price) if price is not None else None,
        extended=D(extended) if extended is not None else None,
    )
    line.id = _id
    return line


def inv2(sku="", desc="", qty=None, uom="", price=None, price_uom="", extended=None, line_no=1):
    return InvoiceLine(
        line_no=line_no, sku=sku, description=desc,
        qty=D(qty) if qty is not None else None, uom=uom, price_uom=price_uom,
        unit_price=D(price) if price is not None else None,
        extended=D(extended) if extended is not None else None,
    )


def test_price_per_box_against_quantity_in_packs_is_detected():
    line = q2(qty="8", uom="PK", price="155.00", price_uom="BX", extended="248.00")
    assert units_disagree(line) is True
    assert effective_unit_price(line) == D("31.00")     # 248 / 8 packs


def test_normal_line_is_not_treated_as_a_unit_mismatch():
    line = q2(qty="80", uom="SQ", price="120.50", price_uom="SQ", extended="9640.00")
    assert units_disagree(line) is False


def test_same_real_price_in_different_packaging_is_not_a_variance():
    """The false-flag case. Quoted per box, billed per pack, same money."""
    quote = [q2(sku="BB-SFM558", desc="Step flashing alum prebent",
                qty="8", uom="PK", price="155.00", price_uom="BX", extended="248.00")]
    lines = [inv2(sku="BB-SFM558", desc="Step flashing alum prebent",
                  qty="8", uom="PK", price="31.00", price_uom="PK", extended="248.00")]
    summary = compare_invoice(lines, quote)
    assert lines[0].verdict == VERDICT_MATCH
    assert summary.overbilled == 0


def test_a_real_increase_is_still_caught_through_a_packaging_difference():
    """The mismatch handling must not become a blind spot."""
    quote = [q2(sku="BB-SFM558", qty="8", uom="PK",
                price="155.00", price_uom="BX", extended="248.00")]        # $31.00/PK
    lines = [inv2(sku="BB-SFM558", qty="8", uom="PK",
                  price="160.00", price_uom="BX", extended="256.00")]      # $32.00/PK
    summary = compare_invoice(lines, quote)
    assert lines[0].verdict == VERDICT_OVER
    assert lines[0].unit_variance == D("1.00")          # per pack
    assert summary.overbilled == D("8.00")              # 8 packs x $1
    assert "effective" in lines[0].match_method


def test_line_total_that_contradicts_the_printed_unit_price_uses_the_total():
    """Trust the printed line total: it is what the vendor is actually charging."""
    line = q2(qty="8", uom="PK", price="155.00", price_uom="", extended="248.00")
    assert units_disagree(line) is True                  # 8 x 155 != 248
    price, basis = comparison_price(line)
    assert basis == "effective" and price == D("31.00")


def test_cent_rounding_is_not_mistaken_for_a_unit_mismatch():
    """66.7 x 91.25 = 6,086.375; vendors print 6,086.38. That is not a mismatch."""
    line = q2(qty="66.7", uom="LF", price="91.25", price_uom="LF", extended="6086.38")
    assert units_disagree(line) is False


def test_newcastle_quote_lines_extract_and_compare_cleanly():
    """A slice of the real quote, billed back exactly as quoted."""
    quote = [
        q2(sku="GAFT3PG", desc="GAF TIMBERLINE HDZ PEWTER GRAY 3 BN/SQ",
           qty="80", uom="SQ", price="120.50", price_uom="SQ", extended="9640.00", line_no=1, _id=1),
        q2(sku="GAFTP", desc="GAF TIGER PAW UNDERLAYMENT",
           qty="7", uom="RL", price="187.00", price_uom="RL", extended="1309.00", line_no=2, _id=2),
        q2(sku="BB-SFM558", desc="STEP FLASHING ALUM PREBENT MF",
           qty="8", uom="PK", price="155.00", price_uom="BX", extended="248.00", line_no=3, _id=3),
    ]
    lines = [
        inv2(sku="GAFT3PG", desc="GAF TIMBERLINE HDZ PEWTER GRAY 3 BN/SQ",
             qty="40", uom="SQ", price="120.50", price_uom="SQ", extended="4820.00", line_no=1),
        inv2(sku="BB-SFM558", desc="STEP FLASHING ALUM PREBENT MF",
             qty="4", uom="PK", price="155.00", price_uom="BX", extended="124.00", line_no=2),
    ]
    s = compare_invoice(lines, quote)
    assert s.lines_match == 2
    assert s.lines_over == 0
    assert s.overbilled == 0


# --- vendor identity ------------------------------------------------------
# Vendors abbreviate themselves inconsistently on their own paperwork. If the
# quote says "New Castle Building Products" and the invoice says "NEW CASTLE
# BLDG PRODUCTS", an exact comparison finds no master quote and checks nothing.

def test_vendor_name_variants_are_the_same_supplier():
    assert vendor_matches("New Castle Building Products", "NEW CASTLE BUILDING PRODUCTS")
    assert vendor_matches("Baker Supply Inc.", "BAKER SUPPLY, LLC")
    assert vendor_matches("New Castle Building Products", "New Castle Building Products Inc")


def test_genuinely_different_vendors_do_not_match():
    assert not vendor_matches("New Castle Building Products", "ABC Waste Removal")
    assert not vendor_matches("Baker Supply", "Cornerstone Lumber")


def test_unknown_vendor_does_not_assert_a_mismatch():
    """One side blank means 'we don't know', not 'these differ'."""
    assert vendor_matches("", "Baker Supply")
    assert vendor_matches("Baker Supply", "")


def test_corporate_suffixes_are_ignored():
    assert norm_vendor("Baker Supply, Inc.") == norm_vendor("BAKER SUPPLY LLC") == "baker supply"

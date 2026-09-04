"""Number formatting shared by the screen and the PDF.

The quantity case here is a real bug that reached a generated PDF: a quote line
for 20 PC rendered as "2E+1". Decimal.normalize() strips trailing zeros by
raising the exponent, so every round quantity - 10, 20, 100 - came out in
scientific notation on both the invoice and the quote.
"""
from decimal import Decimal

import pytest

from app import fmt


@pytest.mark.parametrize("raw,expected", [
    ("20.0000", "20"), ("10", "10"), ("100.00", "100"), ("2000", "2,000"),
    ("104", "104"), ("7", "7"), ("0", "0"),
])
def test_round_quantities_are_not_scientific_notation(raw, expected):
    assert fmt.qty(Decimal(raw)) == expected


@pytest.mark.parametrize("raw,expected", [
    ("12.5000", "12.5"), ("1.50", "1.5"), ("0.25", "0.25"), ("2.125", "2.125"),
])
def test_fractional_quantities_keep_their_value_without_trailing_zeros(raw, expected):
    assert fmt.qty(Decimal(raw)) == expected


def test_missing_quantity_is_a_dash_not_a_zero():
    """A quantity that was never read must not be shown as if it were zero."""
    assert fmt.qty(None) == fmt.DASH


@pytest.mark.parametrize("raw,expected", [
    ("118.75", "$118.75"), ("9.7", "$9.70"), ("155", "$155.00"),
    ("4.1025", "$4.1025"), ("4.102", "$4.102"),
])
def test_unit_prices_keep_the_decimals_they_actually_use(raw, expected):
    """Showing $4.10 when the quote says $4.1025 hides the difference we exist to find."""
    assert fmt.money4(Decimal(raw)) == expected


@pytest.mark.parametrize("raw,expected", [("8126.10", "$8,126.10"), ("0", "$0.00")])
def test_totals_always_carry_two_decimals(raw, expected):
    assert fmt.money(Decimal(raw)) == expected


def test_variance_helpers_drop_the_sign_so_the_caller_controls_it():
    assert fmt.abs_money(Decimal("-6.25")) == "$6.25"
    assert fmt.abs_money4(Decimal("-1.2500")) == "$1.25"

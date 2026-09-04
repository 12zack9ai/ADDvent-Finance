"""Reading A/P and A/R aging reports exported from QuickBooks Desktop.

This is the path that makes the forecast work before any connector exists: the
finance team exports two CSVs and the report is produced from them. So the
parser has to cope with what QuickBooks actually emits - a title block above
the table, subtotal rows, accounting-style negatives, and column names that
differ between versions - and it has to refuse rather than guess when it cannot
identify the columns, because a silently misread aging report produces a
confident wrong cash position.
"""
import pytest

from app.accounting import (
    AgingParseError, parse_date, parse_money, payables_from_csv, receivables_from_csv,
)
from decimal import Decimal as D
from datetime import date

AP_CSV = """Add Ventures Inc
A/P Aging Detail
As of September 7, 2026

Type,Date,Num,Name,Terms,Due Date,Class,Open Balance
Bill,08/12/2026,07RM0003114872,New Castle Building Products,Net 30,09/11/2026,260000,"8,411.00"
Bill,08/18/2026,07RM0003119045,New Castle Building Products,Net 30,09/17/2026,260000,"8,126.10"
Bill,07/30/2026,2014030903,ABC Supply Co. Inc.,Net 30,08/29/2026,250148,"9,624.22"
Total Current,,,,,,,"16,537.10"
TOTAL,,,,,,,"26,161.32"
"""

AR_CSV = """Add Ventures Inc
A/R Aging Detail
As of September 7, 2026

Type,Date,Num,Name,Terms,Due Date,Class,Open Balance
Invoice,08/05/2026,1041,Ridgeview Condo Association,Net 30,09/04/2026,260000,"42,500.00"
Invoice,08/22/2026,1044,Knollwoods HOA,Net 30,09/21/2026,250148,"18,750.00"
TOTAL,,,,,,,"61,250.00"
"""


# --- money and dates as accounting packages write them -------------------

@pytest.mark.parametrize("raw,expected", [
    ("8,411.00", D("8411.00")), ("$1,234.56", D("1234.56")), ("1234.56", D("1234.56")),
    ("(500.00)", D("-500.00")),          # accounting negative
    ("-500.00", D("-500.00")), ("", D("0.00")), ("   ", D("0.00")), ("-", D("0.00")),
])
def test_money_is_read_however_it_is_written(raw, expected):
    assert parse_money(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("09/11/2026", date(2026, 9, 11)), ("09/11/26", date(2026, 9, 11)),
    ("2026-09-11", date(2026, 9, 11)), ("Sep 11, 2026", date(2026, 9, 11)),
    ("", None), ("not a date", None),
])
def test_dates_are_read_in_the_formats_quickbooks_emits(raw, expected):
    assert parse_date(raw) == expected


# --- the whole file ------------------------------------------------------

def test_the_title_block_above_the_table_is_skipped():
    """Aging exports carry the company name and report name above the header."""
    payables = payables_from_csv(AP_CSV)
    assert len(payables) == 3


def test_subtotal_and_total_rows_are_not_read_as_bills():
    """They carry an amount but no name. Counted, they would double the AP."""
    payables = payables_from_csv(AP_CSV)
    assert all(p.vendor for p in payables)
    assert sum(p.amount for p in payables) == D("26161.32")


def test_every_field_the_forecast_needs_is_picked_up():
    first = payables_from_csv(AP_CSV)[0]
    assert first.vendor == "New Castle Building Products"
    assert first.amount == D("8411.00")
    assert first.due_date == date(2026, 9, 11)
    assert first.reference == "07RM0003114872"
    assert first.job_number == "260000"


def test_receivables_read_the_same_way():
    receivables = receivables_from_csv(AR_CSV)
    assert len(receivables) == 2
    assert receivables[0].customer == "Ridgeview Condo Association"
    assert receivables[0].amount == D("42500.00")
    assert receivables[0].due_date == date(2026, 9, 4)


# --- column naming varies by version ------------------------------------

def test_alternative_column_names_are_recognised():
    other = ('Vendor,Bill No.,Due,Amount Due\n'
             'New Castle Building Products,INV-1,09/11/2026,"1,000.00"\n')
    payable = payables_from_csv(other)[0]
    assert payable.vendor == "New Castle Building Products"
    assert payable.amount == D("1000.00")
    assert payable.due_date == date(2026, 9, 11)


def test_a_missing_due_date_falls_back_to_the_transaction_date():
    csv_text = 'Name,Date,Open Balance\nABC Supply,09/11/2026,"500.00"\n'
    assert payables_from_csv(csv_text)[0].due_date == date(2026, 9, 11)


# --- refusing rather than guessing --------------------------------------

def test_a_file_that_is_not_an_aging_report_is_rejected():
    """A silently misread aging report produces a confident wrong cash position,
    which is worse than no report at all."""
    with pytest.raises(AgingParseError):
        payables_from_csv("just,some,columns\n1,2,3\n")


def test_the_rejection_says_what_it_saw_and_what_to_do():
    with pytest.raises(AgingParseError) as excinfo:
        payables_from_csv("alpha,beta\n1,2\n")
    message = str(excinfo.value)
    assert "alpha" in message
    assert "A/P Aging Detail" in message


def test_an_empty_file_is_rejected_not_read_as_zero():
    with pytest.raises(AgingParseError):
        payables_from_csv("")

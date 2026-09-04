"""Add Ventures job numbers.

Six digits, the first two being the year the job was opened: 260000 is the
first job of 2026, 250148 the hundred and forty-ninth of 2025. Jobs from
earlier years stay active for a long time.

The shape matters because it makes a job number self-identifying, so it can be
picked out of a sentence without a label - which is how vendors and staff
actually write it. The counterpart risk is picking up a number that is not a
job at all, so the rejections below are taken from the real documents on file.
"""
from datetime import date

import pytest

from app import jobnum

TODAY = date(2026, 9, 4)


@pytest.mark.parametrize("value", ["260000", "260001", "269999", "250148", "240001", "200000"])
def test_a_six_digit_number_beginning_with_the_year_is_a_job(value):
    assert jobnum.is_job_number(value, TODAY)


def test_next_years_numbers_are_accepted_before_the_year_turns():
    """January's jobs get numbered in December; rejecting them would be wrong."""
    assert jobnum.is_job_number("270001", TODAY)


@pytest.mark.parametrize("value", [
    "280001",      # too far ahead to be real
    "190001",      # before the scheme began
    "26000",       # five digits
    "2600000",     # seven
    "26O000",      # letter O, not a zero
    "", "abc123",
])
def test_anything_else_is_not_a_job_number(value):
    assert not jobnum.is_job_number(value, TODAY)


# --- numbers from the real documents that must never be read as jobs ------

@pytest.mark.parametrize("text,why", [
    ("quote 2014030903 attached", "ABC Supply quote number"),
    ("Account: 2174772 0002", "ABC Supply account number"),
    ("07RM0002847012", "New Castle quote number"),
    ("Phone: (845) 357-7134", "phone number"),
    ("Valley Cottage, NY 10989-1238", "zip code"),
    ("Printed: 09/01/26 11:40:29", "a date and time"),
    ("$4,270.00", "money"),
])
def test_real_document_numbers_are_not_mistaken_for_jobs(text, why):
    assert jobnum.find_job_numbers(text, TODAY) == [], why


# --- reading one out of ordinary writing ---------------------------------

@pytest.mark.parametrize("text", [
    "260000", "Job 260000", "job #260000", "this is for 260000 thanks",
    "260000 - 118 Ridgeview Terrace", "Please price up 260000 when you can.",
    "260000\n\nSent from my iPhone",
])
def test_a_job_number_is_found_wherever_it_is_written(text):
    assert jobnum.sole_job_number(text, TODAY) == "260000"


def test_two_different_jobs_in_one_message_is_not_answered():
    """Guessing between them files the document against the wrong job, which is
    worse than leaving it in the Inbox for a person."""
    assert jobnum.sole_job_number("260000 and 250148", TODAY) is None
    assert jobnum.find_job_numbers("260000 and 250148", TODAY) == ["260000", "250148"]


def test_the_same_job_written_twice_is_still_one_answer():
    assert jobnum.sole_job_number("260000 - see attached for 260000", TODAY) == "260000"


def test_a_job_number_inside_a_longer_run_of_digits_is_not_extracted():
    """2014030903 contains "201403", which is job-shaped. It is not a job."""
    assert jobnum.find_job_numbers("2014030903", TODAY) == []


@pytest.mark.parametrize("text", [
    "GAF260000WW",   # a part number that happens to contain six digits
    "X260000Y",
    "JOB260000",     # no space - the labelled patterns handle this form
    "2600001",       # seven digits
    "12600000",      # eight
])
def test_exactly_six_digits_and_nothing_touching_them(text):
    """A job number is six digits and nothing else. Anything adjacent - a digit
    or a letter - means this is a different kind of number."""
    assert jobnum.find_job_numbers(text, TODAY) == []


@pytest.mark.parametrize("text", ["260000.", "(260000)", "260000-1", "260000/A", "-260000-"])
def test_ordinary_punctuation_around_it_is_fine(text):
    assert jobnum.find_job_numbers(text, TODAY) == ["260000"]


def test_the_year_a_job_was_opened_can_be_read_back():
    assert jobnum.year_of("250148") == 2025
    assert jobnum.year_of("260000") == 2026
    assert jobnum.year_of("not a job") is None


def test_the_year_bound_moves_with_the_calendar():
    """Hard-coding "26" would silently stop recognising jobs on 1 January."""
    assert jobnum.is_job_number("280001", date(2027, 6, 1))
    assert not jobnum.is_job_number("280001", date(2026, 6, 1))

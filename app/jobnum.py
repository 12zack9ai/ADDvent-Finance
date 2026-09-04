"""What an Add Ventures job number looks like.

Six digits. The first two are the year the job was opened, the last four are
that year's sequence: 260000 is the first job of 2026, 250148 the hundred and
forty-ninth of 2025. Work from earlier years stays active for a long time, so
older prefixes have to be recognised too.

That shape is worth encoding, because it makes a job number self-identifying.
"260000" written anywhere - a subject line, a sentence, a note scribbled in an
email - is a job number and nothing else. Without the shape the parser needs a
label ("Job 260000") or the number alone on its own line, and vendors provide
neither.

The year bound is what keeps it from matching everything else on a document.
Six digits beginning 20-27 is a job; 2014030903 is an ABC Supply quote number,
2174772 is an account number, and neither is six digits long.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

# The scheme began in 2020 and stays two-digit-year until 2030, when the first
# digit stops being a 2 and this needs revisiting.
FIRST_YEAR = 20
SCHEME_ENDS = 2030


def max_year_prefix(today: Optional[date] = None) -> int:
    """Highest plausible year prefix: this year, plus one.

    Next year's numbers start being issued before the year turns, so December
    would otherwise reject a job opened for January.
    """
    year = (today or date.today()).year
    return (year % 100) + 1


# Six digits, and exactly six - not part of a longer number, and not embedded in
# a part number. A job number is six digits and nothing else, so anything
# touching it on either side means this is something else entirely:
# 2014030903 contains "201403", and a SKU like GAF260000WW is not a job.
#
# Requiring a non-alphanumeric boundary loses nothing, because "JOB260000"
# written without a space is still caught by the labelled patterns in
# extract.py, which is where that form belongs.
#
# The year is checked in code rather than in the pattern, so the bound moves
# with the calendar instead of going stale on 1 January.
_SIX_DIGITS = re.compile(r"(?<![0-9A-Za-z])(\d{6})(?![0-9A-Za-z])")


def is_job_number(value: str, today: Optional[date] = None) -> bool:
    text = (value or "").strip()
    if len(text) != 6 or not text.isdigit():
        return False
    return FIRST_YEAR <= int(text[:2]) <= max_year_prefix(today)


def find_job_numbers(text: str, today: Optional[date] = None) -> list[str]:
    """Every job-shaped number in the text, in order, without duplicates."""
    seen: list[str] = []
    for candidate in _SIX_DIGITS.findall(text or ""):
        if is_job_number(candidate, today) and candidate not in seen:
            seen.append(candidate)
    return seen


def sole_job_number(text: str, today: Optional[date] = None) -> Optional[str]:
    """The job number, when the text names exactly one.

    Two different job numbers in one message is not an answer, it is a
    question - and guessing between them files the document against the wrong
    job, which is worse than leaving it in the Inbox.
    """
    found = find_job_numbers(text, today)
    return found[0] if len(found) == 1 else None


def year_of(job_number: str) -> Optional[int]:
    """The year a job was opened, for display. 260000 -> 2026."""
    if not is_job_number(job_number):
        return None
    return 2000 + int(job_number[:2])

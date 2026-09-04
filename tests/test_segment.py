"""Splitting one scanned file into the documents it actually contains.

A scanner produces one PDF; the pile on the glass was six invoices from four
vendors. Read as a single document that becomes one invoice carrying every
line from all six - a total that matches nothing, priced against whichever
quote the first page happened to name.

The boundary-finding itself needs the model. What is tested here is everything
around it: the validation that decides whether an answer can be trusted, and
the fallbacks when it cannot. Those are where a wrong split would come from,
and a wrong split is worse than no split.
"""
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from app.segment import Segment, _valid, page_count, split


def pdf(tmp_path, pages: int, name: str = "scan.pdf") -> Path:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    target = tmp_path / name
    with open(target, "wb") as fh:
        writer.write(fh)
    return target


# --- validation: when is an answer trustworthy? --------------------------

def test_ranges_covering_every_page_in_order_are_valid():
    assert _valid([Segment(1, 2), Segment(3, 3), Segment(4, 6)], 6)


def test_one_segment_covering_the_whole_file_is_valid():
    assert _valid([Segment(1, 5)], 5)


@pytest.mark.parametrize("segments,pages,why", [
    ([Segment(1, 3), Segment(3, 5)], 5, "overlapping - page 3 in two documents"),
    ([Segment(1, 2), Segment(4, 5)], 5, "a gap - page 3 belongs to nothing"),
    ([Segment(1, 2), Segment(3, 4)], 6, "stops short of the last page"),
    ([Segment(1, 2), Segment(3, 9)], 5, "runs past the end of the file"),
    ([Segment(3, 4), Segment(1, 2)], 4, "out of order"),
    ([Segment(2, 1)], 2, "backwards"),
    ([], 3, "no answer at all"),
])
def test_an_answer_that_does_not_add_up_is_rejected(segments, pages, why):
    """Every page must belong to exactly one document. Anything else means the
    boundaries were not understood, and six confidently mis-split invoices are
    worse than one merged document a person can see is wrong."""
    assert not _valid(segments, pages), why


# --- splitting -----------------------------------------------------------

def test_each_segment_becomes_its_own_pdf(tmp_path):
    source = pdf(tmp_path, 6)
    segments = [Segment(1, 2), Segment(3, 3), Segment(4, 6)]
    pieces = split(source, segments, tmp_path / "out")

    assert [len(PdfReader(str(p)).pages) for _, p in pieces] == [2, 1, 3]
    assert sum(len(PdfReader(str(p)).pages) for _, p in pieces) == page_count(source)


def test_a_single_segment_is_not_split_or_rewritten(tmp_path):
    """The common case. Nothing is copied, re-read or paid for twice."""
    source = pdf(tmp_path, 4)
    pieces = split(source, [Segment(1, 4)], tmp_path / "out")
    assert pieces == [(Segment(1, 4), source)]


def test_page_count_of_a_file_that_is_not_a_pdf_is_zero(tmp_path):
    """Which routes it down the ordinary single-document path rather than raising."""
    junk = tmp_path / "photo.jpg"
    junk.write_bytes(b"\xff\xd8\xff not a pdf")
    assert page_count(junk) == 0


# --- the filename a person sees -----------------------------------------

def test_a_piece_is_named_after_the_document_inside_it():
    """In a list of six, "scan - New Castle 07RM0003119045.pdf" is findable and
    "scan - part 2.pdf" is not."""
    seg = Segment(2, 3, vendor="New Castle Building Products",
                  document_number="07RM0003119045")
    assert seg.label("scan") == "scan - New Castle Building Products 07RM0003119045.pdf"


def test_a_piece_with_nothing_readable_falls_back_to_its_pages():
    assert Segment(4, 5).label("scan") == "scan - pages 4-5.pdf"


def test_characters_that_break_filenames_are_stripped():
    seg = Segment(1, 1, vendor="A/B Supply: Co.", document_number="INV/2026")
    label = seg.label("scan")
    assert "/" not in label and ":" not in label
    assert label.endswith(".pdf")


def test_pages_counts_the_inclusive_range():
    assert Segment(3, 5).pages == 3
    assert Segment(2, 2).pages == 1

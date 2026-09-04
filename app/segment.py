"""Finding the separate documents inside one scanned file.

A scanner produces one PDF. The pile on the glass was six invoices from four
vendors. Read as a single document, that becomes one invoice carrying every
line from all six - a total that matches nothing, priced against whichever
quote the first page happened to name. It is wrong in the worst way available
to this system: quietly, and with a confident number at the bottom.

So before extracting anything, a multi-page PDF is asked one question: where
does each document start and end? Then it is split and each piece goes through
the ordinary path, exactly as if it had been sent on its own.

Two deliberate conservatisms:

  * **A single-document answer costs nothing extra.** If the file turns out to
    hold one document - which is most of them - the original file is used
    unchanged and nothing is split, re-read or re-paid for.

  * **A nonsensical answer falls back to not splitting.** Page ranges that
    overlap, run backwards, or leave pages unaccounted for mean the boundaries
    were not understood, and one merged document that a person can see is wrong
    beats six confidently mis-split ones.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import anthropic

from app.config import settings
from app.extract import ExtractionError, _client, _content_block

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are looking at a PDF that may contain SEVERAL separate
documents scanned or merged into one file - for example six vendor invoices put
through a scanner in one pass.

Your only job is to say where each separate document starts and ends. Do not
transcribe line items. Do not add up anything.

How to tell one document from the next:
- A new document almost always starts with a letterhead, logo or header block.
- The vendor name, document number, or date changes.
- A page saying "Page 1 of 3" starts a document; "Page 2 of 3" continues one.
- Totals, "remit to" blocks and signature lines usually END a document.
- A continuation page repeats the same document number in a header or footer.

Rules:
1. Every page must belong to exactly one document. Do not skip pages, do not
   overlap ranges, and do not reorder them.
2. Pages are numbered from 1.
3. If the whole file is ONE document, return exactly one entry covering every
   page. This is the common case and is a perfectly good answer.
4. A blank or near-blank separator page belongs to the document BEFORE it.
5. For each document report the vendor and the document number if you can read
   them, so a person can recognise it. Empty string if you cannot - never guess.
6. doc_type is "invoice", "quote", or "other".
"""

SEGMENT_TOOL = {
    "name": "record_boundaries",
    "description": "Record where each separate document starts and ends.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["documents"],
        "properties": {
            "documents": {
                "type": "array",
                "description": "Each separate document, in page order.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["first_page", "last_page", "doc_type", "vendor",
                                 "document_number"],
                    "properties": {
                        "first_page": {"type": "integer", "description": "1-based"},
                        "last_page": {"type": "integer", "description": "1-based, inclusive"},
                        "doc_type": {"type": "string",
                                     "enum": ["invoice", "quote", "other"]},
                        "vendor": {"type": "string", "description": "As printed, else empty"},
                        "document_number": {"type": "string",
                                            "description": "As printed, else empty"},
                    },
                },
            },
        },
    },
}


@dataclass
class Segment:
    first_page: int
    last_page: int
    doc_type: str = "invoice"
    vendor: str = ""
    document_number: str = ""

    @property
    def pages(self) -> int:
        return self.last_page - self.first_page + 1

    def label(self, stem: str) -> str:
        """A filename a person can recognise in a list."""
        bits = [b for b in (self.vendor.split(",")[0].strip(), self.document_number) if b]
        name = " ".join(bits) if bits else f"pages {self.first_page}-{self.last_page}"
        safe = "".join(c if c.isalnum() or c in " -_." else "" for c in name).strip()
        return f"{stem} - {safe or f'p{self.first_page}'}.pdf"


def page_count(path: Path) -> int:
    from pypdf import PdfReader

    try:
        return len(PdfReader(str(path)).pages)
    except Exception as exc:                     # noqa: BLE001
        log.warning("could not read page count of %s: %s", path, exc)
        return 0


def _valid(segments: list[Segment], total_pages: int) -> bool:
    """Do these ranges account for every page exactly once, in order?"""
    if not segments:
        return False
    expected = 1
    for seg in segments:
        if seg.first_page != expected or seg.last_page < seg.first_page:
            return False
        expected = seg.last_page + 1
    return expected == total_pages + 1


def find_documents(path: Path, total_pages: Optional[int] = None) -> list[Segment]:
    """Where each document in this file starts and ends.

    Returns a single whole-file segment when the file holds one document, when
    it has one page, or whenever the answer cannot be trusted.
    """
    pages = total_pages if total_pages is not None else page_count(path)
    whole = [Segment(1, max(pages, 1))]
    if pages <= 1:
        return whole

    client = _client()
    try:
        message = client.messages.create(
            model=settings.extraction_model,
            max_tokens=4000,
            system=[{"type": "text", "text": SYSTEM_PROMPT}],
            tools=[SEGMENT_TOOL],
            messages=[{
                "role": "user",
                "content": [
                    _content_block(path),
                    {"type": "text", "text":
                     f"This file has {pages} pages. Using the record_boundaries "
                     "tool, say where each separate document starts and ends."},
                ],
            }],
        )
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
        # Not being able to ask is not a reason to lose the document. One
        # merged document a person can see is wrong beats nothing at all.
        log.warning("could not segment %s: %s", path.name, exc)
        return whole

    for block in message.content:
        if block.type != "tool_use" or block.name != "record_boundaries":
            continue
        payload = block.input if isinstance(block.input, dict) else json.loads(block.input)
        raw = payload.get("documents") or []
        segments = []
        for item in raw:
            try:
                segments.append(Segment(
                    first_page=int(item["first_page"]),
                    last_page=int(item["last_page"]),
                    doc_type=(item.get("doc_type") or "invoice").strip().lower(),
                    vendor=(item.get("vendor") or "").strip(),
                    document_number=(item.get("document_number") or "").strip(),
                ))
            except (KeyError, TypeError, ValueError):
                log.warning("unusable boundary entry in %s: %r", path.name, item)
                return whole
        if not _valid(segments, pages):
            log.warning(
                "boundaries for %s do not account for all %d pages (%r) - "
                "treating as one document",
                path.name, pages, [(s.first_page, s.last_page) for s in segments],
            )
            return whole
        return segments

    return whole


def split(path: Path, segments: list[Segment], into: Path) -> list[tuple[Segment, Path]]:
    """Write one PDF per segment. Returns them paired with their segment."""
    from pypdf import PdfReader, PdfWriter

    if len(segments) <= 1:
        return [(segments[0], path)] if segments else []

    into.mkdir(parents=True, exist_ok=True)
    reader = PdfReader(str(path))
    out: list[tuple[Segment, Path]] = []
    for index, seg in enumerate(segments, start=1):
        writer = PdfWriter()
        for page_no in range(seg.first_page, seg.last_page + 1):
            writer.add_page(reader.pages[page_no - 1])
        target = into / f"{index:02d}-p{seg.first_page}-{seg.last_page}.pdf"
        with open(target, "wb") as fh:
            writer.write(fh)
        out.append((seg, target))
    return out

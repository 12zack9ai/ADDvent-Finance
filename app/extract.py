"""Document extraction with Claude.

One job: turn a PDF (or image) of a quote or invoice into structured line items.
It does not compare anything and it does not do arithmetic - see matching.py for
that. Numbers come back as strings and are converted to Decimal exactly once, in
one place, so a float never touches a price.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import anthropic

from app import jobnum
from app.config import settings

# Bumped whenever the prompt or schema changes, so stored extractions stay
# traceable to how they were produced.
PROMPT_VERSION = "2026-09-04.1"

_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

SYSTEM_PROMPT = """\
You transcribe construction-supply quotes and invoices into structured data.

You are a transcriber, not an accountant. Follow these rules exactly:

1. TRANSCRIBE, DO NOT CALCULATE. Report only what is printed. If a value is not
   printed on the document, return an empty string for it. Never derive a unit
   price by dividing, never compute an extended amount by multiplying, never
   infer a subtotal. Missing is always better than guessed.

2. ONE ENTRY PER PRINTED LINE ITEM, in the order they appear. Preserve the
   vendor's own description text verbatim - do not tidy, expand, abbreviate, or
   translate it. Descriptions are how lines get matched later, so fidelity
   matters more than readability.

3. NUMBERS AS PLAIN STRINGS. Strip currency symbols and thousands separators:
   "1,234.56" becomes "1234.56". Keep every decimal place shown. A credit or
   negative amount keeps a leading minus: "-45.00".

4. UNIT PRICE is the per-unit price as printed. If the document shows only an
   extended/total amount for a line and no per-unit price, leave unit_price
   empty and fill extended. Do not divide.

4a. PRICE COLUMNS OFTEN FUSE THE PRICE AND ITS UNIT: "120.50/SQ", "9.70/EA",
   "155.00/BX". Split these - unit_price "120.50", price_uom "SQ".

4b. THE PRICE UNIT AND THE QUANTITY UNIT ARE SOMETIMES DIFFERENT ON THE SAME
   LINE, and this matters enormously. A real example:

       QUANTITY 8   UOM PK   PRICE 155.00/BX   AMOUNT 248.00

   The quantity is in packs, the price is per box (5 PK/BX), so 8 packs is
   1.6 boxes at $155 = $248. Record EXACTLY what is printed:
       qty "8", uom "PK", unit_price "155.00", price_uom "BX", extended "248.00"
   Do NOT convert, normalise, or "fix" it, and do NOT recompute the amount to
   make it consistent. Reporting both units faithfully is what lets the
   comparison handle it correctly; silently harmonising them destroys the
   information.

5. FREIGHT covers delivery, shipping, fuel surcharge, and handling charges shown
   as document-level charges. If such a charge appears as its own line item
   among the goods, ALSO include it as a normal line - do not remove it.

6. DOC_TYPE: "quote" for quotes, estimates, proposals, and bids. "invoice" for
   invoices and bills. "other" for statements, packing slips, credit memos, and
   anything else.

7. JOB_NUMBER_HINT: the job this document belongs to. Look at the "Job", "PO",
   "P.O. Number", "Project" and "Reference" fields, in that order of
   preference. IT IS OFTEN A SITE ADDRESS RATHER THAN A NUMBER - "63 winding
   ridge" is a perfectly normal value here. Report it verbatim. If no such
   field carries a value, return an empty string; never invent one.

   A job number here is SIX DIGITS, and the first two are the year the job was
   opened: 260000 is a job from 2026, 250148 from 2025. If a six-digit number
   of that shape appears anywhere on the document - in a field, in a note, in
   the reference line - report that number, because it identifies the job
   exactly. Do not confuse it with the vendor's own quote, invoice, order or
   account numbers, which are longer, shorter, or contain letters.

7a. SHIP_TO: the delivery address block, as printed, newlines replaced by
   commas. On these documents the ship-to address is frequently the same site
   as the job, so it is a useful fallback when the PO field is empty.

7b. PAGE_INFO: if the document says something like "Page 1 of 2", report it
   verbatim (e.g. "1 of 2"). Empty string if absent. This is how we detect
   that pages are missing.

7c. IGNORE HANDWRITING. These documents are often photographed after someone
   has annotated them in pen - ticks, crosses, circles, margin notes. Those
   are a person's working notes, not document data. Transcribe only what is
   printed, and never let an annotation change a value you report.

8. DATES as YYYY-MM-DD. If a date is absent or unreadable, return an empty
   string rather than a guess.

9. CONFIDENCE_NOTES: briefly note anything a human should re-check - unreadable
   figures, ambiguous columns, a total that looks inconsistent with the lines.
   Empty string if the document was clean. Never put numbers you invented here.

Call record_document exactly once."""

_LINE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "line_no", "sku", "description", "qty", "uom",
        "unit_price", "price_uom", "extended",
    ],
    "properties": {
        "line_no": {"type": "integer", "description": "1-based position on the document"},
        "sku": {"type": "string", "description": "Part/item/product code as printed, else empty"},
        "description": {"type": "string", "description": "Verbatim description text"},
        "qty": {"type": "string", "description": "Quantity as printed, else empty"},
        "uom": {"type": "string", "description": "Unit the QUANTITY is in, e.g. EA, SQ, PK, RL"},
        "unit_price": {
            "type": "string",
            "description": "Numeric part of the price only. From '155.00/BX' report '155.00'.",
        },
        "price_uom": {
            "type": "string",
            "description": (
                "Unit the PRICE is quoted per. From '155.00/BX' report 'BX'. "
                "May legitimately differ from uom - report both as printed."
            ),
        },
        "extended": {"type": "string", "description": "Line total as printed, else empty"},
    },
}

# NOTE: deliberately NOT strict.
#
# "strict": True looks like the safer choice - the schema is enforced server
# side, so tool_use.input is guaranteed to validate against it. On this schema
# it does the opposite. Measured on a real two-page New Castle quote: same
# model, same prompt, same image, back to back.
#
#     strict: True   ->   0 line items. Every value landed one key late,
#                         wrapped in raw tool-call markup that leaked into the
#                         strings, so `total` held the job number and `lines`
#                         came back empty.
#     no strict      ->  11 line items, correct, nothing malformed.
#
# Zero lines is the dangerous shape: an invoice with no lines has nothing to
# compare, so it would read as clean rather than as broken. Validation below
# turns that into a loud failure instead of a quiet pass.
RECORD_TOOL = {
    "name": "record_document",
    "description": "Record the transcribed contents of the quote or invoice.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "doc_type", "vendor", "document_number", "document_date", "due_date",
            "currency", "lines", "subtotal", "tax", "freight", "total",
            "job_number_hint", "ship_to", "page_info", "confidence_notes",
        ],
        "properties": {
            "doc_type": {"type": "string", "enum": ["quote", "invoice", "other"]},
            "vendor": {"type": "string", "description": "Company issuing the document"},
            "document_number": {"type": "string", "description": "Invoice or quote number"},
            "document_date": {"type": "string", "description": "YYYY-MM-DD or empty"},
            "due_date": {"type": "string", "description": "YYYY-MM-DD or empty"},
            "currency": {"type": "string", "description": "e.g. USD; empty if not shown"},
            "lines": {"type": "array", "items": _LINE_SCHEMA},
            "subtotal": {"type": "string"},
            "tax": {"type": "string"},
            "freight": {"type": "string", "description": "Delivery/shipping/handling total"},
            "total": {"type": "string"},
            "job_number_hint": {
                "type": "string",
                "description": "Job / PO / project reference. Often a site address.",
            },
            "ship_to": {"type": "string", "description": "Delivery address, commas for newlines"},
            "page_info": {"type": "string", "description": "e.g. '1 of 2'; empty if not shown"},
            "confidence_notes": {"type": "string"},
        },
    },
}


class ExtractionError(RuntimeError):
    pass



# Markup that must never appear inside an extracted value. When a tool call is
# serialised badly, fragments of the call format itself end up inside the field
# values - see the note above RECORD_TOOL. Detecting it is trivial; the reason
# it matters is that the result still looks like a well-formed payload.
_MARKUP_MARKERS = ("<parameter name=", "antml")


def _markup_leak(value: Any) -> bool:
    if isinstance(value, str):
        low = value.lower()
        return any(m in low for m in _MARKUP_MARKERS)
    if isinstance(value, dict):
        return any(_markup_leak(v) for v in value.values())
    if isinstance(value, list):
        return any(_markup_leak(v) for v in value)
    return False


def validate_payload(payload: Any) -> dict[str, Any]:
    """Reject an extraction that cannot be trusted, rather than storing it.

    A finance system fails safe by refusing to answer, never by answering
    emptily. Each check below corresponds to a way a bad payload would
    otherwise reach the matching engine and produce a confident wrong verdict.
    """
    if not isinstance(payload, dict):
        raise ExtractionError(
            f"Extraction returned {type(payload).__name__}, not a document."
        )

    if _markup_leak(payload):
        raise ExtractionError(
            "The document was read but the result came back malformed - field "
            "values contain fragments of the response format itself, so the "
            "numbers cannot be trusted. Nothing was saved. Try uploading it "
            "again; if it keeps happening the document needs entering by hand."
        )

    lines = payload.get("lines")
    if lines is not None and not isinstance(lines, list):
        raise ExtractionError(
            "The line items came back malformed, so nothing was saved."
        )

    doc_type = (payload.get("doc_type") or "other").strip().lower()
    if doc_type in {"quote", "invoice"} and not lines:
        raise ExtractionError(
            f"This was read as a {doc_type} but no line items came out of it. "
            "A priced document with no lines would compare as clean against "
            "anything, so it has not been saved. If the page genuinely has no "
            "line items - a covering page or a statement - upload the page "
            "that does."
        )
    return payload

@dataclass
class ExtractionResult:
    payload: dict[str, Any]
    model: str
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def raw_json(self) -> str:
        """The stored form of `payload`, derived rather than stored separately.

        Filing a document from the Inbox re-reads this JSON instead of paying
        for a second extraction, so it must always be a faithful serialisation.
        Holding it as its own field let the two drift apart, and a payload that
        no longer matched surfaced as a baffling "read as 'other'" error. A
        property cannot drift.
        """
        return json.dumps(self.payload, indent=2, default=str)

    @property
    def doc_type(self) -> str:
        return (self.payload.get("doc_type") or "other").strip().lower()

    @property
    def lines(self) -> list[dict[str, Any]]:
        return self.payload.get("lines") or []


@dataclass
class JobDirective:
    """What an email subject / upload note tells us about job assignment."""

    job_number: Optional[str] = None
    is_master_update: bool = False
    raw: str = ""
    matched_phrase: str = ""


# --- job number parsing ---------------------------------------------------
# Deliberately regex-first: this is cheap, instant, and predictable. The phrasing
# people actually use is narrow ("Job 4417", "master updated to job 4417").

# A job reference is often a SITE ADDRESS ("63 Winding Ridge"), not a number -
# that is what the office writes in the PO field on vendor paperwork. So a
# labelled reference followed by a colon captures the rest of the line (spaces
# and all), while an unlabelled one captures a single token.
_REST_OF_LINE = r"[^\n|;]{1,60}"

_JOB_PATTERNS = [
    # "master updated to job 63 winding ridge" / "...to job number 4417"
    re.compile(rf"\bmaster\s+(?:quote\s+)?(?:is\s+)?updated?\s+to\s*(?:job|po)?\s*(?:number|no\.?|#)?\s*[:\-]?\s*({_REST_OF_LINE})", re.I),
    re.compile(rf"\bnew\s+master\s+(?:for\s+)?(?:job|po)?\s*(?:number|no\.?|#)?\s*[:\-]?\s*({_REST_OF_LINE})", re.I),
    # Labelled with a colon -> take the rest of the line, so addresses survive.
    re.compile(rf"\b(?:job|po|p\.o\.|project)\s*(?:number|no\.?|#)?\s*[:\-]\s*({_REST_OF_LINE})", re.I),
    # Unlabelled -> a single token only ("Job 4417").
    re.compile(r"\bjob\s*(?:number|no\.?|#)?\s*([A-Za-z0-9][A-Za-z0-9\-_/]{1,31})\b", re.I),
    re.compile(r"#\s*(\d{2,10})\b"),
]

_MASTER_UPDATE_RE = re.compile(
    r"\b(master\s+(?:quote\s+)?(?:is\s+)?updated?|updated?\s+master|new\s+master|replace\s+master|supersedes?\s+master)\b",
    re.I,
)


# Everything below this line in a reply is the quoted original, including the
# example job number in the question we sent. Parsing that would file the
# document against the number from our own example.
REPLY_SENTINEL = "----- please reply above this line -----"

_QUOTE_MARKERS = (
    REPLY_SENTINEL,
    "-----Original Message-----",
    "________________________________",
)
_ON_WROTE = re.compile(r"^\s*On .{0,120}\bwrote:\s*$", re.M)
_OUTLOOK_HEADER = re.compile(r"^\s*From:\s.+$", re.M)
# A bare job number, alone on its line or the whole reply. Three to eight
# digits: long enough not to catch "2" or a day of the month, short enough not
# to catch a phone number or an invoice number like 07RM0002847012.
_BARE_NUMBER = re.compile(r"^\s*#?\s*(\d{3,8})\s*[.!]?\s*$", re.M)
# "260000 thanks" is how a busy person answers. Trimming a known courtesy word
# is safe; trimming arbitrary trailing text would not be, because the number
# would then be read out of sentences that were never an answer.
_COURTESY = re.compile(
    r"[\s,.-]*\b(?:thanks|thank you|thx|cheers|please|pls|ta)\b[\s,.!]*$", re.I | re.M
)


def strip_quoted_reply(body: str) -> str:
    """Just the words this person actually typed.

    A reply usually carries the whole original beneath it. Our question
    contains an example job number, so reading the quoted part would answer the
    question with our own example - filing the document against a job the
    sender never named.
    """
    text = body or ""
    for marker in _QUOTE_MARKERS:
        index = text.find(marker)
        if index != -1:
            text = text[:index]
    for pattern in (_ON_WROTE, _OUTLOOK_HEADER):
        match = pattern.search(text)
        if match:
            text = text[:match.start()]
    kept = [ln for ln in text.splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(kept).strip()


def parse_job_answer(*texts: Optional[str]) -> JobDirective:
    """Read a reply to "which job is this?".

    Looser than parse_job_directive on purpose. Asked a direct question, people
    answer with the number and nothing else - "260000" - and that is a valid
    answer here precisely because we asked. In an unsolicited email the same
    bare number could be an invoice number, a quantity or a phone extension, so
    the general parser is right to ignore it and this one is right not to.
    """
    cleaned = [strip_quoted_reply(t) for t in texts if t]

    directive = parse_job_directive(*cleaned)
    if directive.job_number:
        return directive

    # A reply that is only a number, where that number is not job-shaped. The
    # general parser is right to ignore this - unasked, "4417" could be an
    # invoice number or a quantity - but we asked a direct question, and the
    # whole answer being a number makes it the answer.
    for text in cleaned:
        found = _BARE_NUMBER.findall(_COURTESY.sub("", text))
        if len(set(found)) == 1:
            return JobDirective(
                job_number=normalize_job_number(found[0]),
                is_master_update=False,
                raw=text,
                matched_phrase=found[0],
            )
    return JobDirective(job_number=None, is_master_update=False, raw="", matched_phrase="")


def parse_job_directive(*texts: Optional[str]) -> JobDirective:
    """Find a job number, and whether this is an explicit master-quote update.

    Checks each supplied text in order (subject first, then body) and returns the
    first job number found. An explicit 'master updated to job X' phrase both
    identifies the job and marks it as a deliberate replacement.
    """
    combined = "\n".join(t for t in texts if t)
    directive = JobDirective(raw=combined)
    if not combined.strip():
        return directive

    directive.is_master_update = bool(_MASTER_UPDATE_RE.search(combined))

    for text in texts:
        if not text:
            continue
        for pattern in _JOB_PATTERNS:
            m = pattern.search(text)
            if m:
                captured = m.group(1)
                # A labelled match takes the rest of the line so that site
                # addresses survive. When that line contains one job-shaped
                # number, the number is what was meant - "Job: 260000 - 118
                # Ridgeview" is job 260000, not a job called "260000 - 118
                # Ridgeview".
                directive.job_number = (
                    jobnum.sole_job_number(captured) or normalize_job_number(captured)
                )
                directive.matched_phrase = m.group(0).strip()
                return directive

    # No label anywhere. Our job numbers are self-identifying - six digits
    # beginning with the year - so a number of that shape is a job number
    # wherever it is written, and vendors write it without a label constantly.
    # Only when the text names exactly one: two different jobs in one message
    # is a question, not an answer.
    for text in texts:
        if not text:
            continue
        found = jobnum.sole_job_number(text)
        if found:
            directive.job_number = found
            directive.matched_phrase = found
            return directive
    return directive


def normalize_job_number(value: str) -> str:
    """Canonical form of a job reference.

    Jobs are identified by whatever the office writes on the paperwork, which is
    frequently a site address ("63 Winding Ridge") rather than a number. Internal
    spacing is collapsed but preserved so the value stays readable on screen,
    while case and surrounding punctuation are normalised so the same job written
    three different ways still lands in one folder.
    """
    s = (value or "").strip()
    s = re.sub(r"^[#:\-\s]+|[.,;:\-\s]+$", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.upper()[:64]


# --- extraction -----------------------------------------------------------

def _client() -> anthropic.Anthropic:
    # An organisation-level key must say which workspace to bill; a
    # workspace-scoped key carries that already. Sending the header when we have
    # it makes both kinds of key work.
    headers = (
        {"anthropic-workspace-id": settings.anthropic_workspace_id}
        if settings.anthropic_workspace_id
        else None
    )
    if not settings.anthropic_api_key:
        # The SDK also resolves ANTHROPIC_AUTH_TOKEN and `ant auth login`
        # profiles, so an unset key is not necessarily an error.
        return anthropic.Anthropic(default_headers=headers)
    return anthropic.Anthropic(
        api_key=settings.anthropic_api_key, default_headers=headers
    )


def _content_block(path: Path) -> dict[str, Any]:
    media_type = _MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        raise ExtractionError(
            f"Unsupported file type '{path.suffix}'. Upload a PDF, PNG, or JPG."
        )
    # Base64 must contain no newlines.
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    kind = "document" if media_type == "application/pdf" else "image"
    return {"type": kind, "source": {"type": "base64", "media_type": media_type, "data": data}}


def extract_document(path: Path, hint: str = "") -> ExtractionResult:
    """Read one document and return its structured contents."""
    if not path.exists():
        raise ExtractionError(f"File not found: {path}")

    instruction = (
        "Transcribe this document using the record_document tool. "
        "Capture every line item in the order printed."
    )
    if hint:
        instruction += (
            f"\n\nContext from the person who sent it (may name the job, "
            f"and may be wrong - the document itself wins on prices):\n{hint}"
        )

    client = _client()
    try:
        with client.messages.stream(
            model=settings.extraction_model,
            max_tokens=32000,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                # Stable across every document: cache it.
                "cache_control": {"type": "ephemeral"},
            }],
            tools=[RECORD_TOOL],
            messages=[{
                "role": "user",
                "content": [_content_block(path), {"type": "text", "text": instruction}],
            }],
        ) as stream:
            message = stream.get_final_message()
    except anthropic.APIStatusError as exc:
        message = str(exc.message)
        if "anthropic-workspace-id" in message:
            raise ExtractionError(
                "This API key belongs to the organisation rather than a "
                "workspace. Either create a workspace-scoped key at "
                "console.anthropic.com (pick a workspace when creating it), or "
                "set ANTHROPIC_WORKSPACE_ID in .env."
            ) from exc
        raise ExtractionError(f"Claude API error ({exc.status_code}): {message}") from exc
    except anthropic.APIConnectionError as exc:
        raise ExtractionError("Could not reach the Claude API. Check network access.") from exc

    for block in message.content:
        if block.type == "tool_use" and block.name == "record_document":
            # Tool inputs are already parsed objects in the SDK; re-serialise for
            # the audit trail rather than string-matching anything.
            payload = block.input if isinstance(block.input, dict) else json.loads(block.input)
            validate_payload(payload)
            return ExtractionResult(
                payload=payload,
                model=message.model,
                input_tokens=message.usage.input_tokens,
                output_tokens=message.usage.output_tokens,
            )

    if message.stop_reason == "refusal":
        raise ExtractionError("Claude declined to process this document.")
    text = " ".join(b.text for b in message.content if b.type == "text")[:400]
    raise ExtractionError(f"No structured data returned. Model said: {text or '(nothing)'}")

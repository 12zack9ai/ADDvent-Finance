"""Is this document actually from someone we do business with?

Everything else in this system asks "is this invoice priced correctly?". That
question assumes the invoice is real. This module asks the question underneath
it: **did this come from a supplier we chose to deal with, sent from an address
that supplier has used before?**

The attack this exists to stop is the ordinary one. Somebody emails the finance
mailbox a PDF that looks like a bill, with a covering line saying it has already
been approved for payment. Nothing about the PDF is wrong - it is a perfectly
well-formed invoice for goods nobody ordered. The price matching will find no
fault with it, because there is no fault in it. It is simply not ours.

**Where trust comes from, and where it does not.**

A vendor becomes known by appearing on a *quote* - a document that arrives
because somebody here asked for a price - or on an invoice a person has
already approved. A vendor does **not** become known by sending an invoice.
That distinction is the whole design. If an incoming invoice could establish
its own sender as legitimate, the first fake one would whitelist the second.

Likewise a sending domain becomes known by having previously delivered a
document that was filed to a real job. Not by having sent mail.

Everything here is deterministic Python over rows already in the database. No
model call, no network, no scoring. A flag is a statement of fact - "this
domain has never sent us anything before" - and a person still decides.

The flags never approve anything. They can only make an invoice harder to
approve, never easier.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.matching import norm_vendor
from app.models import APPROVAL_APPROVED, APPROVAL_PAID, Document, Invoice, Quote

SEV_BLOCK = "block"
SEV_WARN = "warn"

# Flag codes. Kept stable - they end up in stored JSON.
NEW_VENDOR = "new_vendor"
SENDER_UNKNOWN = "sender_unknown"
SENDER_MISMATCH = "sender_mismatch"
LOOKALIKE_SENDER = "lookalike_sender"
FREEMAIL_SENDER = "freemail_sender"
PRESSURE_LANGUAGE = "pressure_language"
REMITTANCE_CHANGE = "remittance_change"
UNSOLICITED_BILL = "unsolicited_bill"


@dataclass(frozen=True)
class Flag:
    code: str
    severity: str
    message: str
    # A person who satisfied themselves this one is fine - typically by
    # picking up the phone. The flag is never deleted; it is signed for.
    cleared_by: str = ""
    cleared_at: str = ""

    @property
    def blocks(self) -> bool:
        return self.severity == SEV_BLOCK and not self.cleared_by

    @property
    def cleared(self) -> bool:
        return bool(self.cleared_by)


# --- the shapes fraud arrives in ------------------------------------------

# Free mail providers. A supply house bills from its own domain; a person
# pretending to be one usually cannot.
FREEMAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com",
    "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com", "me.com",
    "mail.com", "gmx.com", "protonmail.com", "proton.me", "zoho.com",
    "yandex.com", "inbox.com", "fastmail.com",
}

# Language that exists to stop the reader thinking. A real vendor sends an
# invoice; it does not need to tell you it has been approved.
_PRESSURE = [
    # "approved to be paid" / "approved for payment" / "approved to pay" -
    # all of it, because the endings vary and the meaning does not.
    (r"approved (?:for|to be|to)? ?(?:pay|paid|payment)\b",
     "says the bill is already approved for payment"),
    (r"authoriz(?:ed|ation) (?:for|to) (?:pay|paid|payment|be paid)\b",
     "says payment is already authorised"),
    (r"cleared for payment", "says the bill is cleared for payment"),
    (r"pay (?:this )?(?:immediately|today|asap|at once)", "demands immediate payment"),
    (r"urgent(?:ly)? (?:payment|remit|wire)", "presses for an urgent payment"),
    (r"final notice", "uses final-notice pressure"),
    (r"wire (?:the )?(?:funds|payment|money)", "asks for a wire transfer"),
    (r"do not (?:reply|respond|contact)", "tells the reader not to reply"),
]

# Anything proposing to change where money goes. In accounts payable this is
# the single most expensive sentence in the language.
_REMITTANCE = [
    (r"(?:new|updated|changed?) (?:bank|banking|account|remittance|remit|ach|wire) ",
     "proposes new banking or remittance details"),
    (r"(?:bank|banking|account|remittance) (?:details|information|info)? ?(?:has|have)? ?(?:been )?(?:changed|updated)",
     "says the banking details have changed"),
    (r"update (?:your|our) (?:records|bank|payment|remittance)",
     "asks for payment records to be updated"),
]

_COMPILED_PRESSURE = [(re.compile(p, re.I), why) for p, why in _PRESSURE]
_COMPILED_REMITTANCE = [(re.compile(p, re.I), why) for p, why in _REMITTANCE]


def domain_of(address: str) -> str:
    """The domain from an email address, however it was written."""
    text = (address or "").strip().lower()
    if "<" in text and ">" in text:
        text = text[text.rfind("<") + 1:text.rfind(">")]
    if "@" not in text:
        return ""
    domain = text.rsplit("@", 1)[1].strip().strip(">").strip()
    return domain.rstrip(".")


def _root(domain: str) -> str:
    """Strip a leading mail subdomain so mail.abcsupply.com == abcsupply.com."""
    parts = domain.split(".")
    if len(parts) > 2 and parts[0] in {"mail", "email", "smtp", "mx", "e", "send"}:
        return ".".join(parts[1:])
    return domain


def edit_distance(a: str, b: str, ceiling: int = 3) -> int:
    """Levenshtein distance, stopped once it exceeds `ceiling`.

    Used only to catch a domain typed to look like one we know - abcsuppy.com
    against abcsupply.com. Bounded because we only care about *small* distances;
    a completely different domain is not a lookalike, it is just different.
    """
    if a == b:
        return 0
    if abs(len(a) - len(b)) > ceiling:
        return ceiling + 1
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (ca != cb),
            ))
        if min(current) > ceiling:
            return ceiling + 1
        previous = current
    return previous[-1]


# --- what the database already knows --------------------------------------

def known_vendors(session: Session) -> set[str]:
    """Vendors we chose to deal with.

    Quotes count, because a quote is here because somebody asked for it.
    Approved invoices count, because a person signed for them. An unapproved
    incoming invoice does **not** count - otherwise a fake invoice would make
    its own sender trustworthy, and the second one would sail through.
    """
    names: set[str] = set()
    for vendor in session.scalars(select(Quote.vendor)).all():
        key = norm_vendor(vendor)
        if key:
            names.add(key)
    approved = session.scalars(
        select(Invoice.vendor).where(
            Invoice.approval_status.in_((APPROVAL_APPROVED, APPROVAL_PAID))
        )
    ).all()
    for vendor in approved:
        key = norm_vendor(vendor)
        if key:
            names.add(key)
    return names


def known_domains(session: Session, exclude_document_id: Optional[int] = None) -> set[str]:
    """Sending domains that have previously delivered a document we filed to a job."""
    stmt = select(Document.sender).where(
        Document.source == "email",
        Document.job_id.is_not(None),
    )
    if exclude_document_id is not None:
        stmt = stmt.where(Document.id != exclude_document_id)
    return {_root(domain_of(s)) for s in session.scalars(stmt).all() if s} - {""}


def domains_for_vendor(
    session: Session, vendor: str, exclude_document_id: Optional[int] = None
) -> set[str]:
    """Domains this particular vendor has sent from before.

    Read off documents that produced a quote or an approved invoice for that
    vendor - the same provenance rule as `known_vendors`.
    """
    key = norm_vendor(vendor)
    if not key:
        return set()

    doc_ids: set[int] = set()
    for doc_id, name in session.execute(select(Quote.document_id, Quote.vendor)).all():
        if norm_vendor(name) == key:
            doc_ids.add(doc_id)
    rows = session.execute(
        select(Invoice.document_id, Invoice.vendor).where(
            Invoice.approval_status.in_((APPROVAL_APPROVED, APPROVAL_PAID))
        )
    ).all()
    for doc_id, name in rows:
        if norm_vendor(name) == key:
            doc_ids.add(doc_id)

    doc_ids.discard(exclude_document_id)
    if not doc_ids:
        return set()

    senders = session.scalars(
        select(Document.sender).where(Document.id.in_(doc_ids))
    ).all()
    return {_root(domain_of(s)) for s in senders if s} - {""}


# --- the screen ------------------------------------------------------------

def screen(
    session: Session,
    document: Document,
    vendor: str = "",
    *,
    own_domains: Iterable[str] = (),
) -> list[Flag]:
    """Everything questionable about where this document came from.

    Returns an empty list for a document from a known vendor at a known
    address with nothing odd in the covering message - which is the normal
    case, and should stay quiet.
    """
    flags: list[Flag] = []
    text = f"{document.subject or ''}\n{document.body_text or ''}"
    sender_domain = _root(domain_of(document.sender))
    ours = {d.strip().lower() for d in own_domains if d and d.strip()}
    internal = bool(sender_domain) and sender_domain in ours

    # --- who sent it ------------------------------------------------------
    # Mail forwarded from inside the company carries our own domain, so the
    # sending address says nothing about the vendor. The document still gets
    # screened; the sender checks simply do not apply.
    if document.source == "email" and sender_domain and not internal:
        vendor_domains = domains_for_vendor(session, vendor, document.id)
        if vendor_domains and sender_domain not in vendor_domains:
            flags.append(Flag(
                SENDER_MISMATCH, SEV_BLOCK,
                f"{vendor or 'This vendor'} has always billed from "
                f"{', '.join(sorted(vendor_domains))}. This one came from "
                f"{sender_domain}.",
            ))
        elif not vendor_domains:
            seen = known_domains(session, document.id)
            if sender_domain not in seen:
                near = _nearest(sender_domain, seen | ours)
                if near:
                    flags.append(Flag(
                        LOOKALIKE_SENDER, SEV_BLOCK,
                        f"The sending domain {sender_domain} is one character or "
                        f"two away from {near}, which we do deal with. That is "
                        f"what a spoofed address looks like.",
                    ))
                else:
                    flags.append(Flag(
                        SENDER_UNKNOWN, SEV_WARN,
                        f"First document we have ever received from {sender_domain}.",
                    ))

        if sender_domain in FREEMAIL:
            flags.append(Flag(
                FREEMAIL_SENDER, SEV_WARN,
                f"Sent from {sender_domain}, a free mail account rather than a "
                f"company address.",
            ))

    # --- who it claims to be ---------------------------------------------
    if vendor and norm_vendor(vendor) not in known_vendors(session):
        flags.append(Flag(
            NEW_VENDOR, SEV_WARN,
            f"No quote on file from {vendor}, and no invoice from them has ever "
            f"been approved. This is the first time we have seen this supplier.",
        ))

    # --- what the covering message says ----------------------------------
    for pattern, why in _COMPILED_REMITTANCE:
        if pattern.search(text):
            flags.append(Flag(
                REMITTANCE_CHANGE, SEV_BLOCK,
                f"The covering message {why}. Never act on payment details that "
                f"arrive by email - phone the vendor on a number you already had.",
            ))
            break

    for pattern, why in _COMPILED_PRESSURE:
        if pattern.search(text):
            flags.append(Flag(
                PRESSURE_LANGUAGE, SEV_WARN,
                f"The covering message {why}.",
            ))
            break

    # --- the combination that is the actual scam --------------------------
    codes = {f.code for f in flags}
    if PRESSURE_LANGUAGE in codes and codes & {NEW_VENDOR, SENDER_UNKNOWN, FREEMAIL_SENDER}:
        flags.append(Flag(
            UNSOLICITED_BILL, SEV_BLOCK,
            "A bill from a supplier we have no record of, sent with a message "
            "urging payment. That is the shape of a fake invoice. Confirm with "
            "whoever placed the order before this goes any further.",
        ))

    return flags


def _nearest(domain: str, others: Iterable[str]) -> str:
    """A known domain this one is suspiciously close to, if there is one."""
    if len(domain) < 6:
        return ""
    best, best_distance = "", 3
    for other in others:
        if len(other) < 6 or other == domain:
            continue
        distance = edit_distance(domain, other, ceiling=2)
        if distance and distance < best_distance:
            best, best_distance = other, distance
    return best


# --- storage ---------------------------------------------------------------

def dump(flags: list[Flag]) -> str:
    return json.dumps([asdict(f) for f in flags])


def load(raw: str) -> list[Flag]:
    """Flags back off a document row. Never raises on bad stored JSON."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[Flag] = []
    for item in data:
        if isinstance(item, dict) and item.get("code"):
            out.append(Flag(
                str(item.get("code")),
                str(item.get("severity") or SEV_WARN),
                str(item.get("message") or ""),
                str(item.get("cleared_by") or ""),
                str(item.get("cleared_at") or ""),
            ))
    return out


def flags_for(document: Optional[Document]) -> list[Flag]:
    return load(document.trust_json) if document is not None else []


def blocking(flags: Iterable[Flag]) -> list[Flag]:
    return [f for f in flags if f.blocks]


def clear(document: Document, who: str, when: str) -> int:
    """Sign off the blocking flags on a document. Returns how many.

    Nothing is erased. The flag stays on the record with the name of whoever
    said it was fine, because in three months the only interesting question
    about a bill that turned out to be fake is who waved it through.
    """
    flags = flags_for(document)
    if not any(f.blocks for f in flags):
        return 0
    signed = [
        Flag(f.code, f.severity, f.message, who, when) if f.blocks else f
        for f in flags
    ]
    document.trust_json = dump(signed)
    return sum(1 for f in flags if f.blocks)

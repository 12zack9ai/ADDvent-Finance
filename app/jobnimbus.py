"""Looking up who owns a job, in JobNimbus.

Only one question is asked of JobNimbus, and only when the answer is needed:
**an invoice arrived on a job that has no quote — who is the project manager, so
we can ask them for it?**

Today that invoice lands in the queue with nothing to price it against, and it
stays there until somebody notices and works out whose job it is. The name is
already recorded, in the system the field staff actually use. So we go and read
it rather than making finance chase it.

**On field names, and why this file is written the way it is.**

JobNimbus publishes its API as a Postman collection rather than a specification,
and it was not reachable from the machine this was written on. The base URL and
the bearer-token header are documented and certain. The exact JSON keys on a job
object are not, and different accounts also carry different custom fields.

Guessing a key and hard-coding it would produce code that looks confident and
silently returns nothing - the worst possible failure for something whose whole
job is to notice a gap. So every field is read through a list of candidate
names, the raw payload of the first successful lookup is logged in full at DEBUG,
and `scripts/jobnimbus_probe.py` prints the keys a real response actually
contains. One run against the real account with a real job number settles it,
and the candidate lists get trimmed to what is actually there.

The key is site-wide rather than per-user (confirmed against their other
integration), which matters for one reason: a lookup that finds nothing means
the job genuinely is not there, not that this key cannot see it. So "no job
numbered 260000" is a fact worth logging and acting on, rather than an
ambiguity to work around.

Until an API key is configured this module is inert: `configured()` is False and
nothing here makes a request.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

from app import jobnum
from app.config import settings

log = logging.getLogger(__name__)

API_ROOT = "https://app.jobnimbus.com/api1"
TIMEOUT = 15


class JobNimbusError(RuntimeError):
    """The lookup could not be completed. Never fatal to ingestion."""


# --- candidate field names -------------------------------------------------
#
# Ordered most-likely first. See the module docstring: these are candidates
# precisely because the schema could not be verified, and the probe script
# exists to replace guessing with fact.

JOB_NUMBER_KEYS = ("number", "job_number", "jnid_number", "display_number")
JOB_NAME_KEYS = ("name", "display_name", "job_name")
# **Assigned to**, and nothing else. Zack: "Do not harass the sales rep. As
# they are not responsible for collecting invoices or what not. The assigned
# to is typically the project manager, who is responsible for that. Only."
#
# So the sales rep is not a fallback here and must never become one. A job
# with nobody assigned is a gap for somebody to fill in JobNimbus, not a
# licence to write to the next name on the record.
OWNER_LIST_KEYS = ("owners", "assigned_to", "assignees")
# Flat forms of the same field, for accounts that carry one named assignee
# rather than a list. Still "assigned to" - never a rep, never a salesperson.
FLAT_NAME_KEYS = ("assigned_to_name", "owner_name", "manager_name")
FLAT_EMAIL_KEYS = ("assigned_to_email", "owner_email", "manager_email")
# Named here only so the probe can show when a job has no assignee but does
# have a rep - which is the shape that must NOT be used. Nothing reads these
# to decide who to email.
NEVER_EMAIL_KEYS = ("sales_rep_name", "sales_rep_email", "sales_rep",
                    "rep_name", "rep_email", "salesperson")
# Inside an owner record.
PERSON_NAME_KEYS = ("name", "display_name", "first_name_last_name", "full_name")
PERSON_EMAIL_KEYS = ("email", "email_address", "username")
PERSON_ID_KEYS = ("id", "jnid", "user_id")


@dataclass
class Assignment:
    """Who JobNimbus says is responsible for a job."""

    job_number: str
    job_name: str = ""
    person_name: str = ""
    email: str = ""
    person_id: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        """Enough to actually send someone an email."""
        return bool(self.email and "@" in self.email)

    @property
    def who(self) -> str:
        return self.person_name or self.email or "the project manager"


def configured() -> bool:
    return bool(settings.jobnimbus_api_key)


def _first(record: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _request(path: str, params: dict[str, Any]) -> Any:
    """One GET against the JobNimbus API. Raises JobNimbusError on anything."""
    if not configured():
        raise JobNimbusError("No JobNimbus API key configured.")

    url = f"{API_ROOT}/{path.lstrip('/')}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {settings.jobnimbus_api_key}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # 401 and 403 mean the key is wrong or lacks scope, which is worth
        # saying plainly rather than reporting as "job not found".
        raise JobNimbusError(
            f"JobNimbus returned {exc.code} for {path}. "
            + ("Check JOBNIMBUS_API_KEY." if exc.code in (401, 403) else "")
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise JobNimbusError(f"Could not reach JobNimbus: {exc}") from exc

    try:
        return json.loads(body)
    except ValueError as exc:
        raise JobNimbusError("JobNimbus returned something that was not JSON.") from exc


def _records(payload: Any) -> list[dict]:
    """The job records out of whatever shape the response arrived in."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("results", "jobs", "data", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
    # A single job, returned bare.
    return [payload] if payload else []


def _assignment_from(record: dict, job_number: str) -> Assignment:
    """Pull the responsible person out of a job record."""
    assignment = Assignment(
        job_number=_first(record, JOB_NUMBER_KEYS) or job_number,
        job_name=_first(record, JOB_NAME_KEYS),
        raw=record,
    )

    for key in OWNER_LIST_KEYS:
        owners = record.get(key)
        if isinstance(owners, dict):
            owners = [owners]
        if not isinstance(owners, list):
            continue
        for owner in owners:
            if not isinstance(owner, dict):
                continue
            email = _first(owner, PERSON_EMAIL_KEYS)
            name = _first(owner, PERSON_NAME_KEYS)
            if email or name:
                assignment.email = email
                assignment.person_name = name
                assignment.person_id = _first(owner, PERSON_ID_KEYS)
                return assignment

    # No assignee record. Some accounts carry the same field flat rather than
    # as a list - but nothing beyond "assigned to" is consulted, so a job with
    # an empty Assigned box comes back unusable and nobody is written to.
    assignment.person_name = _first(record, FLAT_NAME_KEYS)
    assignment.email = _first(record, FLAT_EMAIL_KEYS)
    return assignment


def _matches(record: dict, job_number: str) -> bool:
    """Is this record actually the job we asked for?

    Checked rather than assumed. A search endpoint returns what it thinks is
    relevant, and emailing the wrong project manager about somebody else's job
    is worse than not emailing at all.
    """
    wanted = (job_number or "").strip()
    if not wanted:
        return False

    found = _first(record, JOB_NUMBER_KEYS)
    if found and found.strip() == wanted:
        return True

    # Zack: "Some jobs have words after the numbers. But every single one of
    # them has the six digit number." So a record titled
    # "241640- Mountainview Condos (due 10-30-24)" is job 241640, and refusing
    # it on a string comparison would leave the app certain the job does not
    # exist. The six digits are the identity; the words after them are a
    # label. `find_job_numbers` already knows what a job number looks like,
    # including that a date like 10-30-24 is not one.
    return wanted in _identifiers(record)


def _identifiers(record: dict) -> set[str]:
    """Every job-shaped number this record carries, wherever it is written."""
    out: set[str] = set()
    for key in JOB_NUMBER_KEYS + JOB_NAME_KEYS:
        value = record.get(key)
        if not isinstance(value, str):
            continue
        text = value.strip()
        if jobnum.is_job_number(text):
            out.add(text)
        out.update(jobnum.find_job_numbers(text))
    return out


# --- settling the field names ---------------------------------------------

@dataclass
class Finding:
    """What one lookup actually returned, for a person to read.

    The candidate lists above exist because the real key names could not be
    confirmed when this was written. Trimming them needs a real response from
    the real account - and this sandbox cannot reach JobNimbus at all, so the
    lookup has to happen somewhere that can. The app's own server can, which
    is why this is a function rather than only a script.
    """

    job_number: str
    error: str = ""
    records: int = 0
    exact: int = 0
    keys: list[tuple[str, str, str]] = field(default_factory=list)   # name, type, preview
    matched: list[tuple[str, str]] = field(default_factory=list)     # label, value
    owners: dict = field(default_factory=dict)
    assignment: Optional["Assignment"] = None
    payload_keys: list[str] = field(default_factory=list)
    # A sales rep on the record, if there is one. Never emailed - see
    # NEVER_EMAIL_KEYS. Reported only so an unassigned job is obvious.
    rep_present: str = ""

    @property
    def found(self) -> bool:
        return self.records > 0 and not self.error


def probe(job_number: str) -> Finding:
    """Ask about one job and report what came back, without interpreting it.

    Read-only: one GET, nothing written anywhere. The result carries a
    customer's name and address, so it belongs on a screen behind the login
    and not in a ticket.
    """
    number = (job_number or "").strip()
    finding = Finding(job_number=number)
    if not configured():
        finding.error = "No JOBNIMBUS_API_KEY is set."
        return finding

    try:
        payload = _request("jobs", {"filter": number, "size": 25})
    except JobNimbusError as exc:
        finding.error = str(exc)
        return finding

    records = _records(payload)
    finding.records = len(records)
    if not records:
        if isinstance(payload, dict):
            finding.payload_keys = sorted(payload)
        return finding

    exact = [r for r in records if _matches(r, number)]
    finding.exact = len(exact)
    record = (exact or records)[0]

    for key in sorted(record):
        value = record[key]
        preview = value if isinstance(value, str) else json.dumps(value)
        finding.keys.append((key, type(value).__name__, preview[:120]))

    for label, keys in (
        ("job number", JOB_NUMBER_KEYS),
        ("job name", JOB_NAME_KEYS),
        ("assigned name", FLAT_NAME_KEYS),
        ("assigned email", FLAT_EMAIL_KEYS),
    ):
        finding.matched.append((label, _first(record, keys)))

    # Shown so it is visible that a rep exists and is being left alone, not
    # because anything reads it. A job with a rep and no assignee is the case
    # to go and fix in JobNimbus.
    finding.rep_present = _first(record, NEVER_EMAIL_KEYS)

    for key in OWNER_LIST_KEYS:
        if record.get(key):
            finding.owners[key] = record[key]

    finding.assignment = _assignment_from(record, number)
    return finding


def find_job(job_number: str) -> Optional[Assignment]:
    """The JobNimbus job with this number, and who it is assigned to.

    Returns None when the job is not found, when nothing identifies it
    confidently, or when JobNimbus cannot be reached. Never raises into the
    ingestion path: an invoice still files, it just files without a name
    attached.
    """
    number = (job_number or "").strip()
    if not number or not configured():
        return None

    try:
        payload = _request("jobs", {"filter": number, "size": 25})
    except JobNimbusError as exc:
        log.warning("JobNimbus lookup for job %s failed: %s", number, exc)
        return None

    records = _records(payload)
    if not records:
        log.info("JobNimbus has no job numbered %s.", number)
        return None

    # DEBUG rather than INFO: a job record carries a customer's name and
    # address, and this is diagnostic, not something to write on every lookup.
    log.debug("JobNimbus job %s keys: %s", number, sorted(records[0].keys()))

    exact = [r for r in records if _matches(r, number)]
    if not exact:
        log.info(
            "JobNimbus returned %d result(s) for %s but none carried that exact "
            "number, so no project manager was identified.", len(records), number,
        )
        return None
    if len(exact) > 1:
        log.warning(
            "JobNimbus has %d jobs numbered %s. Not guessing which one.",
            len(exact), number,
        )
        return None

    assignment = _assignment_from(exact[0], number)
    if not assignment.usable:
        log.info(
            "JobNimbus job %s found, but no assignee email came back. Keys "
            "present: %s", number, sorted(exact[0].keys()),
        )
    return assignment

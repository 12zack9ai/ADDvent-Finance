#!/usr/bin/env python3
"""Ask JobNimbus about one job and print what actually came back.

app/jobnimbus.py reads every field through a list of candidate names, because
JobNimbus publishes its API as a Postman collection rather than a specification
and the real key names could not be confirmed when it was written. This settles
that: run it once against the real account with a real job number, and the
candidate lists can be trimmed to the keys that exist.

    JOBNIMBUS_API_KEY=... .venv/bin/python scripts/jobnimbus_probe.py 260000

Read-only. It makes one GET and writes nothing anywhere.

The output includes a customer's name and address, so it belongs in a terminal,
not in a ticket or a commit.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import jobnimbus  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    number = sys.argv[1].strip()

    if not jobnimbus.configured():
        print("No JOBNIMBUS_API_KEY set. Put it in .env or pass it inline:")
        print(f"    JOBNIMBUS_API_KEY=... .venv/bin/python {sys.argv[0]} {number}")
        return 1

    print(f"Asking JobNimbus about job {number}...\n")
    try:
        payload = jobnimbus._request("jobs", {"filter": number, "size": 25})
    except jobnimbus.JobNimbusError as exc:
        print(f"FAILED: {exc}")
        return 1

    records = jobnimbus._records(payload)
    print(f"{len(records)} record(s) came back.")
    if not records:
        print("\nTop-level keys on the response:")
        if isinstance(payload, dict):
            for key in sorted(payload):
                print(f"    {key}")
        else:
            print(f"    (response was a {type(payload).__name__})")
        return 1

    exact = [r for r in records if jobnimbus._matches(r, number)]
    print(f"{len(exact)} of them carry exactly that job number.\n")

    record = (exact or records)[0]

    print("--- every key on the first record -------------------------------")
    for key in sorted(record):
        value = record[key]
        shape = type(value).__name__
        preview = json.dumps(value)[:90] if not isinstance(value, str) else value[:90]
        print(f"  {key:<32} {shape:<8} {preview}")

    print("\n--- what the candidate lists find -------------------------------")
    for label, keys in (
        ("job number", jobnimbus.JOB_NUMBER_KEYS),
        ("job name", jobnimbus.JOB_NAME_KEYS),
        ("flat rep name", jobnimbus.FLAT_NAME_KEYS),
        ("flat rep email", jobnimbus.FLAT_EMAIL_KEYS),
    ):
        found = jobnimbus._first(record, keys)
        print(f"  {label:<16} {found or '(nothing matched)'}")

    for key in jobnimbus.OWNER_LIST_KEYS:
        owners = record.get(key)
        if owners:
            print(f"\n  '{key}' is present:")
            print("    " + json.dumps(owners, indent=2)[:1200].replace("\n", "\n    "))

    print("\n--- what the app would do with it -------------------------------")
    assignment = jobnimbus._assignment_from(record, number)
    print(f"  person : {assignment.person_name or '(none)'}")
    print(f"  email  : {assignment.email or '(none)'}")
    print(f"  usable : {assignment.usable}")
    if not assignment.usable:
        print("\n  No email came back, so no quote request would be sent.")
        print("  Find the assignee's email in the key listing above and add that")
        print("  key to PERSON_EMAIL_KEYS (or FLAT_EMAIL_KEYS) in app/jobnimbus.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

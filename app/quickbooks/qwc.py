"""The .qwc file - the one thing a person carries to the Windows machine.

The Web Connector has no configuration screen worth the name. You hand it a
small XML file and it learns the URL to poll, the username to send, and how
often to run. Everything else about the connection is in that file, which is
why generating it here - filled in with this deployment's real URL - removes
the step most likely to be got wrong by hand.

The GUIDs matter and must not change once imported. OwnerID and FileID are how
the connector recognises an application it already knows; a new pair on the
next deploy would show up as a second, duplicate application beside the first.
So they are generated once from the site's own URL and are stable for as long
as that URL is.
"""
from __future__ import annotations

import hashlib
import uuid
from urllib.parse import urljoin


def _stable_guid(seed: str, salt: str) -> str:
    """A GUID that is the same every time for the same site.

    Uppercase hex in braces, which is the only form the connector accepts -
    lowercase is rejected with a message that does not mention case.
    """
    digest = hashlib.sha256(f"{salt}:{seed}".encode()).digest()[:16]
    return "{" + str(uuid.UUID(bytes=digest)).upper() + "}"


def owner_id(base_url: str) -> str:
    return _stable_guid(base_url, "addvent-finance-owner")


def file_id(base_url: str) -> str:
    return _stable_guid(base_url, "addvent-finance-file")


def build(base_url: str, username: str, app_name: str = "ADDvent Finance",
          every_minutes: int = 30, read_only: bool = False) -> str:
    """The file to import into the Web Connector.

    `RunEveryNMinutes` is what makes this unattended. Without a scheduler
    block somebody has to press Update Selected in the connector's window,
    which on a machine nobody logs into means the sync silently never runs.
    """
    base = base_url.rstrip("/") + "/"
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<QBWCXML>\n"
        f"  <AppName>{app_name}</AppName>\n"
        f"  <AppID></AppID>\n"
        f"  <AppURL>{urljoin(base, 'qbwc')}</AppURL>\n"
        f"  <AppDescription>Reads customer invoices and payments so job "
        f"costing can show what each job was billed and collected, and files "
        f"approved vendor bills back into QuickBooks.</AppDescription>\n"
        f"  <AppSupport>{urljoin(base, 'quickbooks')}</AppSupport>\n"
        f"  <UserName>{username}</UserName>\n"
        f"  <OwnerID>{owner_id(base_url)}</OwnerID>\n"
        f"  <FileID>{file_id(base_url)}</FileID>\n"
        "  <QBType>QBFS</QBType>\n"
        "  <Scheduler>\n"
        f"    <RunEveryNMinutes>{every_minutes}</RunEveryNMinutes>\n"
        "  </Scheduler>\n"
        + ("  <IsReadOnly>true</IsReadOnly>\n" if read_only else
           "  <IsReadOnly>false</IsReadOnly>\n")
        + "</QBWCXML>\n"
    )


def problems(base_url: str) -> list[str]:
    """Reasons the connector will refuse this file, checked before it is sent.

    All three of these are rejected by the connector with messages that do not
    say which rule was broken, so they are worth catching here where we can
    say so plainly.
    """
    found = []
    if not base_url:
        found.append("No BASE_URL is set, so the file has no address to poll.")
        return found
    if base_url.startswith("http://") and "localhost" not in base_url \
            and "127.0.0.1" not in base_url:
        found.append(
            "The Web Connector refuses a plain http:// address unless it is "
            "localhost. This has to be https."
        )
    if base_url.rstrip("/").endswith("onrender.com"):
        found.append(
            "This is the default Render address. It works, but the connector "
            "stores the URL permanently - moving to finance.addventuresinc.com "
            "later means removing and re-importing the application."
        )
    return found

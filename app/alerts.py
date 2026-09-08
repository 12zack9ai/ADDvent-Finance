"""Telling somebody when a quiet thing has stopped.

Everything in this app that can fail without anyone noticing fails the same
way: the site keeps serving pages perfectly while a background job stops doing
its job. The mailbox stops being read. The nightly backup stops being written.
Nobody finds out until a vendor rings about a payment, or until the day the
backup is needed.

The signals already existed - `/healthz` has reported `mail.stale` since the
mailbox was built. Nothing was listening. This is the listener.

Two rules, both about being believed:

  * **One email per incident.** An alarm that fires every five minutes is an
    alarm people filter into a folder, and then it is not an alarm.
  * **Say when it recovers.** Otherwise the only way to know it is fixed is to
    go and check, which is the thing the alarm was supposed to save.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Optional

from app import mail_send
from app.config import settings

log = logging.getLogger("finance.alerts")

# Incidents currently open, by key. Deliberately in memory: a restart re-arms
# every alarm, which errs towards telling somebody twice rather than never.
_open: dict[str, datetime] = {}


@dataclass
class Alarm:
    """One thing that can be wrong, and the words for it."""

    key: str
    subject: str
    body: str
    fields: dict[str, str] = field(default_factory=dict)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def where_to() -> str:
    """The address alarms go to, or "" if there is nowhere to send them."""
    to = settings.alert_email
    if not to:
        return ""
    if not settings.may_email(to):
        # Same rule as everything else here: this app writes to our own domain
        # and nowhere else. A misconfigured alert address is not a licence.
        log.warning("ALERT: %s is outside %s - not sending",
                    to, ", ".join(sorted(settings.reply_domains())) or "(no domain)")
        return ""
    return to


def _compose(to_address: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    _, _, _, _, sender = settings.smtp_settings()
    msg["From"] = sender
    msg["To"] = to_address
    msg["Subject"] = subject
    msg.set_content(body)
    return msg


def raise_alarm(alarm: Alarm) -> bool:
    """Report a problem. Returns True if an email actually went.

    Calling this repeatedly for the same key while it is still broken is the
    normal case and sends nothing.
    """
    if alarm.key in _open:
        return False
    _open[alarm.key] = _now()

    log.warning("ALERT: %s", alarm.subject)
    to = where_to()
    if not to or not settings.can_send_mail():
        return False

    lines = [alarm.body, ""]
    for name, value in alarm.fields.items():
        lines.append(f"{name}: {value}")
    lines += ["", settings.base_url, "", f"-- \n{settings.site_name}"]

    try:
        mail_send.send(_compose(to, f"[{settings.site_name}] {alarm.subject}",
                                "\n".join(lines)))
        return True
    except mail_send.SendError as exc:
        # The mail path is often the very thing that is broken. Log it and
        # leave the incident open so the dashboard still shows it.
        log.warning("ALERT: could not send %r: %s", alarm.subject, exc)
        return False


def clear(key: str, subject: str, body: str = "") -> bool:
    """Say a problem has gone away. Returns True if an email actually went."""
    started = _open.pop(key, None)
    if started is None:
        return False

    hours = round((_now() - started).total_seconds() / 3600, 1)
    log.info("ALERT CLEARED: %s (after %s hour(s))", subject, hours)
    to = where_to()
    if not to or not settings.can_send_mail():
        return False

    text = body or "This has recovered on its own. No action needed."
    try:
        mail_send.send(_compose(
            to, f"[{settings.site_name}] Recovered: {subject}",
            f"{text}\n\nIt was broken for about {hours} hour(s).\n\n"
            f"{settings.base_url}\n\n-- \n{settings.site_name}",
        ))
        return True
    except mail_send.SendError as exc:
        log.warning("ALERT: could not send recovery for %r: %s", subject, exc)
        return False


# What each alarm says on the banner at the top of every page. Short, because
# it is read by somebody who came here to do something else.
LABELS = {
    "mail-stale": "Emailed documents are not arriving — the mailbox has stopped being read.",
    "backup-failed": "The nightly backup is not working.",
    "disk-low": "The disk is nearly full.",
}


def open_labels() -> list[str]:
    """Plain sentences for whatever is currently wrong."""
    return [LABELS.get(key, key) for key in sorted(_open)]


def open_incidents() -> dict[str, datetime]:
    """What is currently wrong, for the banner at the top of the site."""
    return dict(_open)


def is_open(key: str) -> bool:
    return key in _open


def forget_everything() -> None:
    """Tests only."""
    _open.clear()

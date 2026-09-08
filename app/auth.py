"""Access control.

One shared password protects the whole site, held in a signed, HTTP-only cookie.
That is deliberately modest: this is a small internal team, and a login that
everyone actually uses beats an SSO integration that stalls in IT for a month.

It is a real gate, not decoration - the cookie is cryptographically signed, so it
cannot be forged without SECRET_KEY, and comparison is constant-time.

To upgrade later (per-user accounts, or Microsoft SSO through the same Entra app
registration the mailbox already uses), replace `current_user` and `verify` -
every route goes through `require_login`, so nothing else has to change.
"""
from __future__ import annotations

import hmac
import secrets
import time
from typing import Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings

COOKIE_NAME = "fin_session"
_SALT = "finance-automation-login"

# Paths reachable without signing in.
# /qbwc has to be public: the QuickBooks Web Connector is a Windows service
# with no browser and no cookie jar. It is guarded instead by its own username
# and password, checked in constant time, and the only thing it can do is hold
# a sync conversation - there is no path from it to a page, a document or a
# decision.
PUBLIC_PATHS = {"/login", "/healthz", "/favicon.ico", "/qbwc"}

# A generated key means sessions do not survive a restart, which is a nuisance
# but never a security hole. Set SECRET_KEY in .env in production.
_secret = settings.secret_key or secrets.token_urlsafe(48)
_serializer = URLSafeTimedSerializer(_secret, salt=_SALT)


def auth_required() -> bool:
    """Auth is only skipped when no password is configured (local development)."""
    return bool(settings.app_password)


def verify(password: str) -> bool:
    if not settings.app_password:
        return True
    return hmac.compare_digest(password or "", settings.app_password)


# --- slowing down guessing -------------------------------------------------
# One shared password and an unlimited number of tries is a password that gets
# guessed eventually. This is not a lockout - locking the office out of their
# own finance system on a Friday afternoon is a worse outcome than a slow
# attacker - it is a delay that grows, per source address, and forgets itself.

_ATTEMPT_WINDOW = 900          # a wrong password is remembered for 15 minutes
_FREE_TRIES = 5                # typos, and the password manager's first guess
_MAX_WAIT = 60                 # seconds; long enough to make guessing useless

_failures: dict[str, list[float]] = {}


def _recent(who: str, now: float) -> list[float]:
    times = [t for t in _failures.get(who, []) if now - t < _ATTEMPT_WINDOW]
    if times:
        _failures[who] = times
    else:
        _failures.pop(who, None)
    return times


def wait_for(who: str, now: Optional[float] = None) -> int:
    """Seconds this address must wait before another try is accepted."""
    now = time.time() if now is None else now
    times = _recent(who, now)
    if len(times) < _FREE_TRIES:
        return 0
    # Doubling from one second after the free tries are spent.
    delay = min(_MAX_WAIT, 2 ** (len(times) - _FREE_TRIES))
    waited = now - times[-1]
    return max(0, int(delay - waited) + (1 if delay > waited else 0))


def note_failure(who: str, now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    _recent(who, now)
    _failures.setdefault(who, []).append(now)


def note_success(who: str) -> None:
    _failures.pop(who, None)


def forget_attempts() -> None:
    """Tests only."""
    _failures.clear()


def make_token() -> str:
    return _serializer.dumps({"ok": True})


def valid_token(token: Optional[str]) -> bool:
    if not token:
        return False
    try:
        _serializer.loads(token, max_age=settings.session_days * 86400)
        return True
    except (BadSignature, SignatureExpired):
        return False


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith("/static/")


def warnings() -> list[str]:
    """Configuration problems worth shouting about at startup."""
    out = []
    if not settings.app_password:
        out.append(
            "APP_PASSWORD is not set - the site is OPEN to anyone who can reach it. "
            "Set it in .env before exposing this beyond localhost."
        )
    elif len(settings.app_password) < 10:
        out.append("APP_PASSWORD is short. Use a long passphrase.")
    if not settings.secret_key:
        out.append(
            "SECRET_KEY is not set - a random one was generated, so everyone is "
            "signed out on every restart. Set it in .env."
        )
    if not settings.anthropic_api_key:
        out.append(
            "ANTHROPIC_API_KEY is not set - documents cannot be read until it is."
        )
    return out

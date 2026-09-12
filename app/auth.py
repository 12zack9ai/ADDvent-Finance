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

import hashlib
import hmac
import secrets
import time
from contextvars import ContextVar
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
PUBLIC_PATHS = {"/login", "/healthz", "/favicon.ico", "/qbwc",
                # Creating an account, confirming it, and resetting a password
                # all happen before anybody is signed in.
                "/register", "/verify", "/forgot", "/reset"}

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


def make_token(user=None) -> str:
    """A signed session. With an account it also says who - signed, so the
    name on an approval cannot be forged by editing a cookie."""
    if user is None:
        return _serializer.dumps({"ok": True})
    return _serializer.dumps({
        "ok": True, "uid": user.id, "name": user.name or user.email,
        "email": user.email, "owner": bool(getattr(user, "is_owner", False)),
    })


def token_data(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    try:
        data = _serializer.loads(token, max_age=settings.session_days * 86400)
    except (BadSignature, SignatureExpired):
        return None
    return data if isinstance(data, dict) else None


def valid_token(token: Optional[str]) -> bool:
    return token_data(token) is not None


def who_from_token(token: Optional[str]) -> Optional[dict]:
    """The signed-in person, or None for no session or the shared password."""
    data = token_data(token)
    if not data or not data.get("uid"):
        return None
    return {"uid": data["uid"], "name": data.get("name") or data.get("email") or "",
            "email": data.get("email", ""), "owner": bool(data.get("owner"))}


# Who is signed in for the request being handled - set by the login middleware,
# read by everything that records a name (main._actor). So an approval, a hold,
# a cleared warning or a check marked cut is signed by the account, and nobody
# types their name.
current: ContextVar[Optional[dict]] = ContextVar("finance_current_user", default=None)


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith("/static/")


# --- passwords ------------------------------------------------------------------
# scrypt from the standard library: slow on purpose, salted per password, and
# stored with its own settings so they can be raised later without a reset.

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt((password or "").encode(), salt=salt,
                            n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = (stored or "").split("$")
        if scheme != "scrypt":
            return False
        test = hashlib.scrypt((password or "").encode(), salt=bytes.fromhex(salt),
                              n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(digest)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(test.hex(), digest)


# --- emailed links: confirm an address, reset a password --------------------------
# Signed and timed, and tied to the password as it stands: changing the
# password spends every link issued before it, so a reset link works once.

_links = URLSafeTimedSerializer(_secret, salt="finance-account-links")


def _fingerprint(password_hash: str) -> str:
    return hashlib.sha256((password_hash or "").encode()).hexdigest()[:16]


def link_token(purpose: str, uid: int, password_hash: str) -> str:
    return _links.dumps({"p": purpose, "u": uid, "f": _fingerprint(password_hash)})


def read_link(token: str, purpose: str, max_age: int) -> Optional[dict]:
    try:
        data = _links.loads(token or "", max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict) or data.get("p") != purpose or not data.get("u"):
        return None
    return data


def link_matches(data: dict, password_hash: str) -> bool:
    return hmac.compare_digest(str(data.get("f", "")), _fingerprint(password_hash))


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

"""The Web Connector conversation, as a state machine.

QuickBooks calls us. Eight callbacks, in this order, and the whole exchange
happens across separate HTTP requests minutes apart:

    clientVersion(version)                    -> "" to accept
    authenticate(user, password)              -> [ticket, "" | "none" | "nvu"]
    sendRequestXML(ticket, ...)               -> one qbXML request, or "" when done
    receiveResponseXML(ticket, response, ..)  -> 0-100, or negative to abort
      ... sendRequestXML / receiveResponseXML repeat until we return "" ...
    closeConnection(ticket)                   -> a line for the connector's log

    connectionError(ticket, hresult, message) -> "done" to give up
    getLastError(ticket)                      -> why we stopped

The second element of what `authenticate` returns is load-bearing and easy to
get wrong:

    ""      use whatever company file is currently open - what we want
    "none"  we have nothing to do; the connector goes away quietly
    "nvu"   not a valid user; the connector shows a login failure
    a path  open that specific company file

There is no HTTP here and no SOAP. That is the point: the conversation is the
part with the states, the retries and the out-of-order calls, and it can be
driven end to end by a test that pretends to be QuickBooks.
"""
from __future__ import annotations

import hmac
import logging
import secrets
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

log = logging.getLogger(__name__)

AUTH_OK = ""
AUTH_NO_WORK = "none"
AUTH_BAD_USER = "nvu"
AUTH_BUSY = "busy"


class Conversation(Protocol):
    """What a session does between authenticate and closeConnection.

    Implemented by sync.Sync. Kept as an interface so the protocol can be
    tested against a conversation that answers "one request, then done"
    without touching the database.
    """

    def next_request(self) -> str:
        """The next qbXML to send, or "" when there is nothing left."""

    def handle_response(self, xml: str) -> None:
        """Take one qbXML response."""

    def progress(self) -> int:
        """0-100. 100 ends the session."""

    def had_work(self) -> bool:
        """Was there anything to do at all?"""

    def finish(self, error: str = "") -> str:
        """Wrap up. Returns the line the connector shows in its log."""


@dataclass
class Session:
    ticket: str
    conversation: Conversation
    company_file: str = ""
    qbxml_version: str = ""
    country: str = ""
    last_error: str = ""
    closed: bool = False


class Service:
    """The eight callbacks. Transport-free.

    `open_conversation` is called once per authenticated session and decides
    whether there is anything to do. Returning None means there is not, and
    the connector is told "none" - which is the difference between a quiet
    connector and one that wakes the office up every five minutes.
    """

    def __init__(
        self,
        username: str,
        password: str,
        open_conversation: Callable[[Session], Optional[Conversation]],
        server_version: str = "1.0",
    ) -> None:
        self._username = username
        self._password = password
        self._open = open_conversation
        self._server_version = server_version
        self._sessions: dict[str, Session] = {}

    # -- the handshake -----------------------------------------------------

    def server_version(self) -> str:
        return self._server_version

    def client_version(self, version: str) -> str:
        """Vet the connector's own version.

        "" accepts, "W:..." warns the user and carries on, "E:..." refuses.
        We accept everything: refusing on a version string would take the
        integration down for a reason nobody could act on, and every version
        of the connector speaks this protocol identically.
        """
        log.info("QBWC: connector version %s", version)
        return ""

    def authenticate(self, username: str, password: str) -> list[str]:
        """Check the credentials and decide whether there is work.

        Compared with `compare_digest` because this endpoint is on the public
        internet by necessity - the connector has to be able to reach it - and
        a byte-by-byte early exit is a timing oracle on the one secret that
        guards it.
        """
        ticket = secrets.token_urlsafe(24)

        user_ok = hmac.compare_digest(username or "", self._username)
        pass_ok = hmac.compare_digest(password or "", self._password)
        if not (self._username and self._password) or not (user_ok and pass_ok):
            log.warning("QBWC: authentication refused for %r", username)
            return [ticket, AUTH_BAD_USER]

        session = Session(ticket=ticket, conversation=None)  # type: ignore[arg-type]
        conversation = self._open(session)
        if conversation is None:
            log.info("QBWC: authenticated, nothing to do")
            return [ticket, AUTH_NO_WORK]

        session.conversation = conversation
        self._sessions[ticket] = session
        return [ticket, AUTH_OK]

    # -- the loop ----------------------------------------------------------

    def send_request_xml(
        self, ticket: str, hcp_response: str, company_file: str,
        country: str, major: Optional[int], minor: Optional[int],
    ) -> str:
        session = self._sessions.get(ticket)
        if session is None:
            log.warning("QBWC: sendRequestXML on an unknown ticket")
            return ""

        session.company_file = company_file or session.company_file
        session.country = country or session.country
        from app.quickbooks import qbxml
        session.qbxml_version = qbxml.negotiate_version(major, minor)

        try:
            return session.conversation.next_request()
        except Exception as exc:                    # noqa: BLE001
            # A failure here must not leave the connector waiting: returning ""
            # ends the session cleanly and the error is kept for getLastError.
            log.exception("QBWC: building a request failed")
            session.last_error = f"Could not build the next request: {exc}"
            return ""

    def receive_response_xml(
        self, ticket: str, response: str, hresult: str, message: str,
    ) -> int:
        """Take one response and say how far along we are.

        A negative return aborts the session. Reserved for our own failures:
        an hresult from QuickBooks is reported by the connector separately and
        aborting on it as well would lose the rest of a run over one query.
        """
        session = self._sessions.get(ticket)
        if session is None:
            return -1

        if hresult:
            session.last_error = f"QuickBooks reported {hresult}: {message}"
            log.warning("QBWC: %s", session.last_error)
            return -1

        try:
            session.conversation.handle_response(response)
        except Exception as exc:                    # noqa: BLE001
            log.exception("QBWC: handling a response failed")
            session.last_error = f"Could not read the response: {exc}"
            return -1

        return max(0, min(session.conversation.progress(), 100))

    # -- endings -----------------------------------------------------------

    def connection_error(self, ticket: str, hresult: str, message: str) -> str:
        """The connector could not reach QuickBooks at all.

        "done" tells it to stop rather than retry against a company file it
        has already failed to open.
        """
        session = self._sessions.get(ticket)
        if session is not None:
            session.last_error = f"Connection error {hresult}: {message}"
            log.warning("QBWC: %s", session.last_error)
        return "done"

    def get_last_error(self, ticket: str) -> str:
        session = self._sessions.get(ticket)
        if session is None:
            return "Unknown session."
        return session.last_error or "No error recorded."

    def close_connection(self, ticket: str) -> str:
        session = self._sessions.pop(ticket, None)
        if session is None:
            return "OK"
        session.closed = True
        try:
            return session.conversation.finish(session.last_error)
        except Exception as exc:                    # noqa: BLE001
            log.exception("QBWC: closing the session failed")
            return f"Finished with an error: {exc}"

    # -- for the status page ----------------------------------------------

    @property
    def open_tickets(self) -> int:
        return len(self._sessions)

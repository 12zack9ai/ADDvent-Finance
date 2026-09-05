"""The two URLs QuickBooks needs, and the one a person needs.

    GET  /qbwc?wsdl      the contract, fetched once by the Web Connector
    POST /qbwc           every SOAP callback, for the life of the integration
    GET  /quickbooks     what the connector has been doing
    GET  /quickbooks/qwc the file to carry to the Windows machine

`/qbwc` is the only unauthenticated route in this application, and it has to
be: the Web Connector is a Windows service that cannot log in to a website. It
is guarded by its own username and password, checked with a constant-time
compare, and it can do exactly one thing - hold a sync conversation. There is
no path from it to a page, a document or a decision.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.models import Job
from app.quickbooks import protocol, qbxml, qwc, soap, sync
from app.quickbooks.mirror import (
    OUT_PENDING,
    QbCustomer,
    QbInvoice,
    QbOutbox,
    QbSession,
    QbSyncState,
)

log = logging.getLogger(__name__)
router = APIRouter()

_service: protocol.Service | None = None


def service() -> protocol.Service:
    """The one Service for this process.

    Its open sessions live in memory on purpose. A conversation spans many HTTP
    requests, and if this process restarts in the middle of one the ticket
    stops being recognised - the connector is told the session is over and
    starts again on its next run. That is the right outcome: cursors only move
    when a step finishes cleanly, so nothing is missed, and the alternative
    (resuming a half-finished conversation against a company file that has
    moved on) is how a mirror ends up quietly wrong.
    """
    global _service
    if _service is None:
        _service = protocol.Service(
            username=settings.qbwc_username,
            password=settings.qbwc_password,
            open_conversation=_open,
        )
    return _service


def _open(session: protocol.Session):
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        return sync.open_session(db, session.ticket,
                                 write_back=settings.qbwc_write_back)
    except Exception:                                  # noqa: BLE001
        log.exception("QBWC: could not open a sync session")
        db.close()
        return None


@router.get("/qbwc")
def qbwc_wsdl(request: Request) -> Response:
    """The contract. Served at the same URL the connector posts to.

    The address inside it is the one the connector actually reached, not the
    one this application thinks it is on: behind a proxy those differ, and the
    connector posts to whatever this says rather than where it got it from.
    """
    endpoint = str(request.url).split("?")[0]
    return Response(content=soap.wsdl(endpoint), media_type="text/xml")


@router.post("/qbwc")
async def qbwc_soap(request: Request) -> Response:
    """Every callback, for the life of the integration."""
    body = (await request.body()).decode("utf-8", "replace")
    try:
        operation, args = soap.parse_request(body)
    except soap.SoapError as exc:
        log.warning("QBWC: bad SOAP request - %s", exc)
        return Response(content=soap.fault(str(exc)), media_type="text/xml",
                        status_code=400)

    if not settings.quickbooks_ready():
        # Deliberately a refusal rather than a fault: the connector shows the
        # authentication failure to whoever is standing at the machine, which
        # is exactly who needs to know the password has not been set.
        if operation == "authenticate":
            return Response(
                content=soap.build_response("authenticate", ["", protocol.AUTH_BAD_USER]),
                media_type="text/xml",
            )

    try:
        result = soap.dispatch(service(), operation, args)
    except Exception as exc:                           # noqa: BLE001
        log.exception("QBWC: %s failed", operation)
        return Response(content=soap.fault(str(exc)), media_type="text/xml",
                        status_code=500)

    return Response(content=soap.build_response(operation, result),
                    media_type="text/xml")


@router.get("/quickbooks/qwc")
def download_qwc() -> Response:
    """The file somebody carries to the Windows machine."""
    content = qwc.build(
        base_url=settings.base_url,
        username=settings.qbwc_username,
        every_minutes=settings.qbwc_minutes,
        read_only=not settings.qbwc_write_back,
    )
    return PlainTextResponse(
        content,
        media_type="application/xml",
        headers={"Content-Disposition": 'attachment; filename="addvent-finance.qwc"'},
    )


@router.get("/quickbooks", response_class=HTMLResponse)
def quickbooks_status(request: Request, session: Session = Depends(get_session)):
    """What the connector has been doing, and what is still to be set up.

    Also the AppSupport URL in the .qwc file, which the Web Connector requires
    to return a 200 - so this page has to work whether or not anybody has
    configured anything.
    """
    from app.main import _ctx, templates

    sessions = session.scalars(
        select(QbSession).order_by(desc(QbSession.started_at)).limit(8)
    ).all()
    states = session.scalars(select(QbSyncState)).all()
    unlinked = session.scalars(
        select(QbCustomer)
        .where(QbCustomer.job_id.is_(None), QbCustomer.parent_list_id != "")
        .order_by(QbCustomer.full_name)
        .limit(25)
    ).all()

    return templates.TemplateResponse(request, "quickbooks.html", _ctx(
        request, session,
        ready=settings.quickbooks_ready(),
        enabled=settings.quickbooks_enabled,
        has_password=bool(settings.qbwc_password),
        username=settings.qbwc_username,
        write_back=settings.qbwc_write_back,
        minutes=settings.qbwc_minutes,
        base_url=settings.base_url,
        qwc_problems=qwc.problems(settings.base_url),
        qbxml_version=qbxml.DEFAULT_VERSION,
        sessions=sessions,
        states={s.entity: s for s in states},
        customers=session.scalar(select(func.count(QbCustomer.id))) or 0,
        linked=session.scalar(
            select(func.count(QbCustomer.id)).where(QbCustomer.job_id.is_not(None))
        ) or 0,
        invoices=session.scalar(select(func.count(QbInvoice.id))) or 0,
        unlinked=unlinked,
        jobs_total=session.scalar(select(func.count(Job.id))) or 0,
        queued=session.scalar(
            select(func.count(QbOutbox.id)).where(QbOutbox.status == OUT_PENDING)
        ) or 0,
    ))

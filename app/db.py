"""Database engine, session, and an exact-money column type.

Money is stored as TEXT and round-tripped through decimal.Decimal. SQLite has no
native decimal type, and storing currency as a float is how you end up with
$4,182.5999999. Never use Float for money here.
"""
from __future__ import annotations

import logging

from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy import String, TypeDecorator, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

log = logging.getLogger(__name__)

TWO = Decimal("0.01")
FOUR = Decimal("0.0001")


class Money(TypeDecorator):
    """Exact decimal currency, stored as TEXT."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect) -> Optional[str]:
        if value is None or value == "":
            return None
        if not isinstance(value, Decimal):
            try:
                value = Decimal(str(value))
            except (InvalidOperation, ValueError):
                return None
        # NaN is the one that does not announce itself: quantize returns it
        # rather than raising, so it would be written as the text "NaN" and
        # read back as a Decimal every comparison in the system answers False
        # to. Screened before the arithmetic, not after.
        if not _is_sane(value):
            log.warning("MONEY: refusing to store an unusable value %r", value)
            return None
        try:
            return format(value.quantize(FOUR), "f")
        except (InvalidOperation, ValueError):
            # Belt and braces. to_decimal screens these out at the door, so
            # reaching here means a Decimal was built somewhere else - and a
            # column type is the wrong place to take a page down.
            log.warning("MONEY: refusing to store an unrepresentable value %r", value)
            return None

    def process_result_value(self, value, dialect) -> Optional[Decimal]:
        if value is None:
            return None
        try:
            parsed = Decimal(value)
        except (InvalidOperation, ValueError):
            return None
        # A row written before the guard above existed, or by hand. Reading it
        # back as None is a hole in a report; reading it back as NaN is a
        # comparison that quietly answers False everywhere it is used.
        if not _is_sane(parsed):
            log.warning("MONEY: unusable value %r in the database, read as None", value)
            return None
        return parsed


def to_decimal(value) -> Optional[Decimal]:
    """Best-effort conversion of extracted values to Decimal.

    Handles the shapes Claude returns for money and quantities: numbers,
    numeric strings, and strings carrying currency symbols, commas, or
    parenthesised negatives.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    s = str(value).strip()
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("$", "").replace(",", "").replace("USD", "").strip()
    if not s:
        return None
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    if not _is_sane(d):
        return None
    return -d if negative else d


# No line item, invoice, contract or bank balance in this business is within
# nine orders of magnitude of this. The bound is not about tidiness: a value
# past it cannot be stored (Decimal.quantize raises), and everything upstream
# of the store - a misread digit run, a fuzzed form field, a spreadsheet cell
# holding 1e999 - would take a page out with a 500 instead.
MAX_MONEY = Decimal("1e12")


def _is_sane(value: Decimal) -> bool:
    """Is this a number arithmetic can be trusted with?

    NaN is the dangerous one and the reason this exists. Every comparison
    against NaN is False, so an invoice total of NaN is not over the quote, not
    over tolerance, not over the contract ceiling, and not greater than zero -
    it passes every check in this system silently. Decimal("nan") is a value
    `Decimal(str)` accepts without complaint, from a form field or from a
    document somebody scanned badly. It must never get in.
    """
    if not value.is_finite():
        return False
    return abs(value) < MAX_MONEY


def money_str(value: Optional[Decimal], places: str = "0.01") -> str:
    if value is None:
        return "—"
    return f"${value.quantize(Decimal(places)):,}"


def qty_str(value: Optional[Decimal]) -> str:
    if value is None:
        return "—"
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        return str(normalized.to_integral_value())
    return format(normalized, "f")


class Base(DeclarativeBase):
    pass


_db_url = settings.resolved_db_url()
_connect_args = {"check_same_thread": False} if _db_url.startswith("sqlite") else {}

engine = create_engine(_db_url, connect_args=_connect_args, future=True)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record) -> None:
    """Make SQLite survive a slow write, because this application has one.

    Reading a document is a Claude call taking around twenty seconds, and the
    row is written before the call and committed after it - so a write
    transaction is held open for the whole of it. The mailbox poller does that
    from inside the web process every five minutes.

    On the stock settings that meant: the first person to approve an invoice
    while a document was being read waited five seconds and then got a 500,
    "database is locked". Nothing about the page they were on would have
    suggested why. Reproduced before changing anything, and it failed exactly
    like that.

      * WAL lets readers carry on throughout, instead of being shut out at the
        moment the writer commits. Every page in this app is a read.
      * A thirty-second busy timeout is longer than the write it has to wait
        out, so a person's approval queues behind the poller and then goes
        through, rather than failing five seconds in.
      * synchronous=NORMAL is the documented companion to WAL: still safe
        against a process crash, and it is a crash - not power loss on a
        managed host - that this needs to survive.

    The real fix is to not hold a transaction open across an API call at all.
    That is a change to the ingestion path and wants doing deliberately; this
    stops people being thrown out of the app in the meantime.
    """
    if not _db_url.startswith("sqlite"):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_session():
    """FastAPI dependency: one session per request."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def init_db() -> None:
    from app import models  # noqa: F401  (registers mappers)

    Base.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """Add columns the models declare but the existing database lacks.

    create_all() creates missing tables and nothing else, so a new column on an
    existing table is invisible to it. On a server that already holds real
    documents the next deploy would then fail on every query naming that
    column - and the fix people reach for under pressure is deleting the
    database, which is the one irreversible mistake available here.

    Only additive, only nullable, and only on SQLite. Anything else - a dropped
    column, a changed type, a backfill - needs a considered migration, and this
    deliberately will not pretend to handle it.
    """
    if engine.dialect.name != "sqlite":
        return

    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            rows = conn.execute(text(f'PRAGMA table_info("{table.name}")')).fetchall()
            if not rows:
                continue                      # table is new; create_all made it
            existing = {row[1] for row in rows}
            for column in table.columns:
                if column.name in existing or column.primary_key:
                    continue
                if not column.nullable and column.default is None:
                    log.warning(
                        "SCHEMA: %s.%s is missing and NOT NULL - needs a real "
                        "migration, skipping", table.name, column.name,
                    )
                    continue
                ddl = column.type.compile(engine.dialect)
                # A server_default is carried into the ALTER, which is what
                # backfills the rows already in the table. Without it every
                # existing row gets NULL - fine for a display field, and not
                # fine for one that decides whether something is authorised.
                if column.server_default is not None:
                    literal = getattr(column.server_default, "arg", None)
                    if literal is not None:
                        ddl += f" DEFAULT {_sql_literal(str(literal))}"
                conn.execute(
                    text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl}')
                )
                log.warning("SCHEMA: added %s.%s", table.name, column.name)


def _sql_literal(value: str) -> str:
    """Quote a default for inline DDL. Values are ours, never user input."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"

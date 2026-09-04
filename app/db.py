"""Database engine, session, and an exact-money column type.

Money is stored as TEXT and round-tripped through decimal.Decimal. SQLite has no
native decimal type, and storing currency as a float is how you end up with
$4,182.5999999. Never use Float for money here.
"""
from __future__ import annotations

import logging

from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy import String, TypeDecorator, create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

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
        return format(value.quantize(FOUR), "f")

    def process_result_value(self, value, dialect) -> Optional[Decimal]:
        if value is None:
            return None
        try:
            return Decimal(value)
        except (InvalidOperation, ValueError):
            return None


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
    return -d if negative else d


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


log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


_db_url = settings.resolved_db_url()
_connect_args = {"check_same_thread": False} if _db_url.startswith("sqlite") else {}

engine = create_engine(_db_url, connect_args=_connect_args, future=True)
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
                conn.execute(
                    text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl}')
                )
                log.warning("SCHEMA: added %s.%s", table.name, column.name)

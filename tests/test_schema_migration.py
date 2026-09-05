"""The additive migration, tested against a database that already exists.

Every deploy runs `_add_missing_columns` against a database holding real
documents. It is the riskiest code in the project by a distance: if it gets a
column wrong the app fails on every query naming it, and the fix people reach
for under pressure is deleting the database.

The specific bug these tests exist for: `ChangeOrder.approved_at` was declared
NOT NULL, and making it nullable in the model produced a table that accepted
inserts on a fresh database and rejected them on a real one - because SQLite has
no ALTER COLUMN and this migration can only add. That failure showed up as an
order-dependent test, which is the worst way to find anything.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = Path(tempfile.mkdtemp(prefix="finance-migrate-"))
os.environ.setdefault("DATA_DIR", str(_TMP))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP / 'm.db'}")
os.environ.setdefault("APP_PASSWORD", "")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session as OrmSession  # noqa: E402

from app import db as appdb  # noqa: E402
from app.models import (  # noqa: E402
    Base,
    CO_APPROVED,
    CO_PROPOSED,
    ChangeOrder,
    Job,
)

D = Decimal

# The change_order table exactly as it shipped before change orders could be
# proposed: no status, no decided_note, and approved_at NOT NULL.
OLD_SCHEMA = """
CREATE TABLE job (
    id INTEGER NOT NULL PRIMARY KEY,
    job_number VARCHAR(64) NOT NULL,
    name VARCHAR(255),
    notes TEXT,
    created_at DATETIME
);
CREATE TABLE change_order (
    id INTEGER NOT NULL PRIMARY KEY,
    job_id INTEGER NOT NULL,
    document_id INTEGER,
    number VARCHAR(64),
    vendor VARCHAR(255),
    description TEXT,
    amount NUMERIC(14, 4),
    approved_by VARCHAR(128),
    approved_at DATETIME NOT NULL,
    created_at DATETIME
);
"""


@pytest.fixture()
def old_database(monkeypatch):
    """A database as it exists in production before this deploy."""
    path = Path(tempfile.mkdtemp(prefix="finance-old-")) / "existing.db"
    raw = sqlite3.connect(path)
    raw.executescript(OLD_SCHEMA)
    raw.execute(
        "INSERT INTO job (id, job_number, name) VALUES (1, '260000', 'Daul')"
    )
    # A change order somebody typed in months ago, under the old schema.
    raw.execute(
        "INSERT INTO change_order "
        "(id, job_id, number, vendor, description, amount, approved_by, approved_at) "
        "VALUES (1, 1, 'CO-1', 'New Castle Building Products', 'Hidden rot', "
        "'2400.0000', 'Zack', '2026-06-01 10:00:00')"
    )
    raw.commit()
    raw.close()

    engine = create_engine(f"sqlite:///{path}", future=True)
    monkeypatch.setattr(appdb, "engine", engine)
    yield engine
    engine.dispose()


def _columns(engine, table: str) -> dict:
    with engine.begin() as conn:
        rows = conn.execute(text(f'PRAGMA table_info("{table}")')).fetchall()
    return {row[1]: row for row in rows}


def test_the_new_columns_are_added_to_a_table_that_already_exists(old_database):
    Base.metadata.create_all(old_database)      # creates the tables that are new
    appdb._add_missing_columns()

    cols = _columns(old_database, "change_order")
    assert "status" in cols
    assert "decided_note" in cols


def test_change_orders_already_in_service_stay_authorising(old_database):
    """The one that would have cost real money. `is_live` fails closed, so a
    backfill to NULL would silently strip authorisation from every change order
    already on the books - and re-hold every invoice they were covering."""
    Base.metadata.create_all(old_database)
    appdb._add_missing_columns()

    with OrmSession(old_database) as session:
        existing = session.get(ChangeOrder, 1)
        assert existing.status == CO_APPROVED
        assert existing.is_live


def test_a_proposed_change_order_inserts_against_the_old_table(old_database):
    """approved_at is NOT NULL on every database already in service and this
    migration cannot relax it, so a proposed change order has to be insertable
    without anyone passing one."""
    Base.metadata.create_all(old_database)
    appdb._add_missing_columns()

    with OrmSession(old_database) as session:
        session.add(ChangeOrder(
            job_id=1, vendor="New Castle Building Products", number="CO-7",
            amount=D("150.00"), description="Price increase per mill notice",
            status=CO_PROPOSED, approved_by="",
        ))
        session.commit()                        # this is the assertion

        proposed = session.query(ChangeOrder).filter_by(number="CO-7").one()
        assert proposed.status == CO_PROPOSED
        assert not proposed.is_live
        # It has a timestamp because the column demands one, and reading it as
        # an approval date would be believing something that never happened.
        assert proposed.approved_at is not None
        assert proposed.decided_on is None


def test_running_the_migration_twice_changes_nothing(old_database):
    """It runs on every single boot."""
    Base.metadata.create_all(old_database)
    appdb._add_missing_columns()
    before = _columns(old_database, "change_order")
    appdb._add_missing_columns()
    assert _columns(old_database, "change_order") == before


def test_defaults_are_quoted_rather_than_interpolated():
    assert appdb._sql_literal("approved") == "'approved'"
    assert appdb._sql_literal("it's") == "'it''s'"


# --- the job table, which every deploy adds something to --------------------

def test_the_costing_columns_land_on_a_job_table_that_predates_them(old_database):
    """The old `job` table above has five columns. Every costing figure added
    since - billed, collected, the crew's hours, where the billing came from -
    has to arrive by this migration or the app fails on its first query."""
    Base.metadata.create_all(old_database)
    appdb._add_missing_columns()

    columns = _columns(old_database, "job")
    for name in ("contract_amount", "collected_amount", "billing_source",
                 "billing_synced_at", "labour_cost", "labour_hours",
                 "costing_note"):
        assert name in columns, name


def test_a_job_already_in_service_reads_back_without_a_null_where_a_string_goes(old_database):
    """billing_source is NOT NULL. A row inserted before the column existed
    has to be backfilled by the server default, or every read of it raises -
    which is the exact shape of the change_order bug these tests exist for."""
    Base.metadata.create_all(old_database)
    appdb._add_missing_columns()

    with OrmSession(old_database) as session:
        job = session.get(Job, 1)
        assert job.billing_source == ""          # not None
        assert not job.billing_is_synced
        assert job.collected_amount is None      # genuinely unknown
        assert job.outstanding is None           # and says so rather than guessing


def test_costing_figures_can_be_written_to_the_migrated_table(old_database):
    Base.metadata.create_all(old_database)
    appdb._add_missing_columns()

    with OrmSession(old_database) as session:
        job = session.get(Job, 1)
        job.contract_amount = D("268000.00")
        job.collected_amount = D("96400.00")
        job.labour_hours = D("980")
        job.labour_cost = D("41500.00")
        job.billing_source = "manual"
        session.commit()

        job = session.get(Job, 1)
        assert job.outstanding == D("171600.00")
        assert job.labour_rate == D("42.35")

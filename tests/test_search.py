"""The search box in the header.

Zack: *"I don't think the searching for a job number works. I did it and it
brought me back to main page."* It did exactly that - the form submitted to
"/", which reads no query at all and re-renders the front door. The matching
was already written and sitting on /jobs, unreachable from the one box in the
app that looks like it should reach it.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = Path(tempfile.mkdtemp(prefix="finance-search-"))
os.environ.setdefault("DATA_DIR", str(_TMP))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP / 'test.db'}")
os.environ["APP_PASSWORD"] = ""
os.environ["ANTHROPIC_API_KEY"] = "test"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Job  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


def setup_module(_module) -> None:
    init_db()
    with SessionLocal() as session:
        if session.query(Job).filter_by(job_number="260901").first() is None:
            session.add(Job(job_number="260901", name="Winding Ridge"))
            session.commit()


def test_the_search_box_submits_somewhere_that_reads_the_query():
    """The whole bug in one assertion. A form posting to "/" cannot search."""
    page = client.get("/").text
    assert 'class="search" action="/jobs"' in page


def test_a_job_number_goes_straight_to_that_job():
    r = client.get("/jobs?q=260901", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/job/260901"


def test_a_job_number_with_a_hash_and_spaces_still_lands():
    r = client.get("/jobs?q=%20%23260901%20", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/job/260901"


def test_a_name_filters_the_list_rather_than_redirecting():
    page = client.get("/jobs?q=Winding")
    assert page.status_code == 200
    assert "260901" in page.text


def test_a_search_that_matches_nothing_says_so_instead_of_listing_everything():
    page = client.get("/jobs?q=zzzznotathing")
    assert page.status_code == 200
    assert "260901" not in page.text

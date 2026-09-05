"""The receipt department.

Zack: *"the 6th department of this is the receipt collector. We need to
implement taking pictures and emailing every receipt. Or texting. You respond
every time what job number. And you add it to a job folder in this department.
This one will have more folders than all. Because sometimes we spend money on
jobs we don't get so this will turn out to be a loss."*

Four things in that, and each of them is tested here: it arrives as a
photograph, we always ask which job, it lands in a folder, and the ones on
work we did not get are a loss rather than a cost.
"""
from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import mail_send, purchases  # noqa: E402
from app.models import JOB_ACTIVE, JOB_LOST, Job, Purchase  # noqa: E402

_ids = {"n": 0}


def _next() -> int:
    _ids["n"] += 1
    return _ids["n"]


def job(number="260014", name="Daul Gardens", outcome=JOB_ACTIVE, buys=()):
    j = Job(job_number=number, name=name, outcome=outcome)
    j.id = _next()
    j.purchases = list(buys)
    return j


def buy(total="84.12", merchant="Home Depot", on=None):
    p = Purchase(job_id=1, merchant=merchant, total=D(total),
                 purchased_on=on or date(2026, 9, 1))
    p.id = _next()
    return p


# --- folders ---------------------------------------------------------------

def test_every_job_anybody_bought_anything_for_gets_a_folder():
    """"More folders than all" - every other department only has a folder for
    a job that reached it. Counter spend touches everything."""
    s = purchases.build([
        job("260010", buys=[buy("84.12")]),
        job("260011", buys=[buy("212.44"), buy("31.80")]),
        job("260012"),                       # nothing bought: no folder
    ])
    assert [f.job.job_number for f in s.folders] == ["260011", "260010"]
    assert s.total == D("328.36")
    assert s.count == 3


def test_folders_are_ordered_by_what_was_spent():
    """With a folder for nearly every job, the job number is not what anybody
    is scanning for."""
    s = purchases.build([
        job("260010", buys=[buy("12.00")]),
        job("260011", buys=[buy("900.00")]),
        job("260012", buys=[buy("40.00")]),
    ])
    assert [f.total for f in s.folders] == [D("900.00"), D("40.00"), D("12.00")]


def test_a_folder_names_the_shops_without_repeating_them():
    """In the order the folder shows them, which is newest first."""
    f = purchases.build([job(buys=[buy(merchant="Home Depot"),
                                   buy(merchant="Home Depot"),
                                   buy(merchant="Lowe's")])]).folders[0]
    assert f.merchants == ["Lowe's", "Home Depot"]


def test_the_newest_receipt_is_first_inside_a_folder():
    f = purchases.build([job(buys=[
        buy("10.00", on=date(2026, 8, 1)),
        buy("20.00", on=date(2026, 9, 3)),
    ])]).folders[0]
    assert f.purchases[0].total == D("20.00")
    assert f.latest == date(2026, 9, 3)


# --- and some of it is a loss ---------------------------------------------

def test_spend_on_a_job_we_did_not_get_is_totalled_as_a_loss():
    """The only money in this system with nothing on the other side of it."""
    s = purchases.build([
        job("260010", buys=[buy("500.00")]),
        job("260011", outcome=JOB_LOST, buys=[buy("340.00")]),
    ])
    assert s.total == D("840.00")
    assert s.lost_total == D("340.00")
    assert s.working_total == D("500.00")
    assert [f.job.job_number for f in s.lost] == ["260011"]


def test_a_live_job_is_not_a_loss_just_because_nobody_has_said_yet():
    """Defaulting to lost would turn every job nobody has touched into a loss
    on the front page."""
    s = purchases.build([job(buys=[buy("500.00")])])
    assert s.lost_total == D("0")
    assert not s.folders[0].is_loss


def test_nothing_bought_anywhere_is_zero_rather_than_an_error():
    s = purchases.build([job(), job("260011")])
    assert s.folders == [] and s.total == D("0") and s.lost_total == D("0")


# --- how the photograph got here ------------------------------------------

def test_a_photo_texted_in_is_recognised_as_a_text():
    """A phone can send a picture to an email address on all three US
    networks. That is what makes "just text it in" work with no phone number,
    no provider and no monthly bill."""
    assert mail_send.came_from_a_phone("Mike <2015551234@mms.att.net>")
    assert mail_send.came_from_a_phone("5555551234@vzwpix.com")
    assert mail_send.came_from_a_phone("<5555551234@tmomail.net>")
    assert not mail_send.came_from_a_phone("Zack <zack@addventuresinc.com>")
    assert not mail_send.came_from_a_phone("")


def test_the_answer_to_a_text_is_one_sentence():
    """It arrives on a phone as a text. Everything the email version explains
    is true and unreadable split across six messages - somebody standing in a
    lumber yard needs the question, not the reassurance."""
    texted = mail_send.compose_job_query(
        to_address="5555551234@mms.att.net", subject="", filename="IMG_4471.jpg",
        vendor="Home Depot", texting=True,
    )
    body = texted.get_content()
    assert "Which job?" in body
    assert len(body.splitlines()) <= 4
    assert "safe in the meantime" not in body

    emailed = mail_send.compose_job_query(
        to_address="zack@addventuresinc.com", subject="Receipt",
        filename="IMG_4471.jpg", vendor="Home Depot",
    )
    assert len(emailed.get_content().splitlines()) > 10

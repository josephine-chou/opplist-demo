"""Batch T3 (2026-10-28): once a vendor of the current round has sent a
usable rate (quoted = 1), a New case's Next says "Send the quote" - in
tracking.case_nudge, the case page's Next tile and the team site's next.
A reply without a rate changes nothing; a rate from the last round of a
re-quote does not count. Tracking's cards are not touched."""

from __future__ import annotations

import re

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, firebase_sync, tracking
from opplist.web import create_app

TODAY = "2026-10-09"                     # a Friday; every date below is before it


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "next-send-quote.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    ids = {"agent": add_party(repo, "Pelorin Cargo", country="Chile")}
    for key in ("Marvane Lines", "Quillby Freight", "Ostrava Haul"):
        ids[key] = add_party(repo, key, country="USA")
    repo.commit()
    yield conn, repo, path, ids
    conn.close()


def three_asked(repo, ids, opp, first):
    """A New case, three vendors asked on Monday Oct 5; the first one's
    answer is `first` (vendor_request fields; quoted follows
    machine_status), the others silent."""
    case = add_enquiry(repo, opp, ids["agent"], received="2026-10-05", service_mode="LCL",
                       origin_country="USA", dest_country="Chile", commodity="Ceramic tiles")
    for n, key in enumerate(("Marvane Lines", "Quillby Freight", "Ostrava Haul")):
        values = {"enquiry_id": case, "vendor_party_id": ids[key], "asked_on": "2026-10-05"}
        if n == 0:
            values.update(first)
        repo.insert("vendor_request", values)
    repo.commit()
    return case


def row_of(conn, case):
    return conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (case,)).fetchone()


def next_tile(client, case):
    body = client.get("/enquiries/{}".format(case)).data.decode("utf-8")
    facts = body[body.index('<div class="facts">'):body.index('<div class="layout">')]
    labels = re.findall(r'<div class="f-l">(.*?)</div>', facts)
    values = re.findall(r'<div class="f-v[^"]*">(.*?)</div>', facts, re.S)
    return values[labels.index("Next")].strip()


def site_next(conn, opp):
    return {r["opp"]: r for r in firebase_sync.build(conn, TODAY).inquiries}[opp]["next"]


def test_one_rate_in_hand_means_send_the_quote(world):
    conn, repo, path, ids = world
    case = three_asked(repo, ids, "OPP-7201", {"replied_on": "2026-10-06", "machine_status": "Quoted"})
    assert tracking.case_nudge(conn, row_of(conn, case), TODAY) == {
        "rule": "", "text": "Send the quote", "hint": "", "days": None}
    assert site_next(conn, "OPP-7201") == "Send the quote"
    # Tracking's cards are as they were: the silent two are still chased
    silent = {i.extra["vendor_key"] for i in tracking.rule_vendor_silent(conn, TODAY, 2) if i.enquiry_id == case}
    assert silent == {"Quillby Freight", "Ostrava Haul"}
    assert case in {i.enquiry_id for i in tracking.rule_mail_activity(conn, TODAY)}
    assert tracking.new_cases_off_every_card(conn, TODAY) == []
    client = create_app(path).test_client()
    assert next_tile(client, case) == "Send the quote"


def test_a_reply_without_a_rate_still_chases_the_silent_ones(world):
    conn, repo, path, ids = world
    case = three_asked(repo, ids, "OPP-7202", {"replied_on": "2026-10-06", "machine_status": "Acknowledged"})
    nudge = tracking.case_nudge(conn, row_of(conn, case), TODAY)
    assert nudge["rule"] == "R2" and nudge["text"] == "Vendor Quillby Freight no reply, 4 days"
    assert site_next(conn, "OPP-7202") == nudge["text"]
    client = create_app(path).test_client()
    assert next_tile(client, case).startswith("Vendor Quillby Freight no reply, ")   # the page counts to today


def test_another_office_still_comes_first(world):
    conn, repo, path, ids = world
    case = three_asked(repo, ids, "OPP-7203", {"replied_on": "2026-10-06", "machine_status": "Quoted"})
    repo.update("enquiry", case, {"handled_by": "YUL"})
    repo.commit()
    assert tracking.case_nudge(conn, row_of(conn, case), TODAY)["text"] == "Handled by YUL"


@pytest.fixture
def requoted(world):
    """Quoted on Sep 24 on Marvane's rate, then an old-style re-quote (a
    requote event on New, Oct 1): a new round that needs new rates."""
    conn, repo, path, ids = world
    case = add_enquiry(repo, "OPP-7204", ids["agent"], received="2026-09-21")
    repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": ids["Marvane Lines"],
                                   "asked_on": "2026-09-22", "replied_on": "2026-09-23", "machine_status": "Quoted"})
    repo.set_status(case, "Quoted", "2026-09-24")
    repo.set_status(case, "New", "2026-10-01", note="Re-quote: two pallets more", kind="requote")
    repo.commit()
    assert row_of(conn, case)["in_requote"] == 1
    return conn, repo, case, ids


def test_last_rounds_rate_with_nothing_asked_is_r1(requoted):
    conn, repo, case, ids = requoted
    assert tracking.case_nudge(conn, row_of(conn, case), TODAY)["rule"] == "R1"


def test_last_rounds_rate_does_not_stop_the_chase(requoted):
    conn, repo, case, ids = requoted
    vr = repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": ids["Quillby Freight"],
                                        "asked_on": "2026-10-05"})
    repo.commit()
    nudge = tracking.case_nudge(conn, row_of(conn, case), TODAY)
    assert nudge["rule"] == "R2" and nudge["text"] == "Vendor Quillby Freight no reply, 4 days"
    # a rate of this round turns it
    repo.update("vendor_request", vr, {"replied_on": "2026-10-07", "machine_status": "Quoted"})
    repo.commit()
    assert tracking.case_nudge(conn, row_of(conn, case), TODAY)["text"] == "Send the quote"

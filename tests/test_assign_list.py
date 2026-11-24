"""The team site's Assign list (assign_list): which inquiries wait for our
quote and in what order, On hold, Assigned to on the case page. These were
the Google Sheet's rules (AL2, AL4, AL5); CX1 (2026-11-04) retired the sheet
and kept the lists for the site.

Every company, case number, name and date here is invented; cases from
OPP-3101."""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import assign_list, db
from opplist.web import create_app

TODAY = "2026-11-20"


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "assign.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "owners", ["Ana", "Ben", "Cara"])
    kestrel = add_party(repo, "Kestrel Forwarding", country="Chile")
    lark = add_party(repo, "Lark Imports", ptype="Direct Customer", country="USA")
    ids = {}
    for opp, received, extra in (
            ("OPP-3101", "2026-11-10", {}),
            ("OPP-3102", "2026-11-12", {"source_subject": "RE: [OPP-3102] FW: RATE REQUEST // TALCA - HOUSTON"}),
            ("OPP-3103", "2026-11-05", {"owner": "Ben"}),
            ("OPP-3104", "2026-11-11", {}),
            ("OPP-3105", "2026-11-01", {"handled_by": "YUL"}),                          # another office
            ("OPP-3106", "2026-11-02", {"legacy": 1}),
            ("OPP-3107", "2026-11-03", {"hold_reason": "waiting for the shipper", "hold_until": "2026-12-01"}),
            ("OPP-3108", "2026-11-04", {"hold_reason": "waiting", "hold_until": "2026-11-15"}),  # its Until passed
            ("OPP-3109", "2026-11-06", {"status": "Quoted"})):
        ids[opp] = add_enquiry(repo, opp, kestrel, received=received, status=extra.pop("status", "New"), **extra)
    ids["OPP-3110"] = add_enquiry(repo, "OPP-3110", lark, received="2026-11-07")           # a direct customer
    repo.commit()
    yield conn, path, ids
    conn.close()


def test_which_inquiries_are_listed_and_in_what_order(world):
    conn, path, ids = world
    rows = assign_list.listed(conn, TODAY)
    # not assigned first, oldest first; then the assigned ones
    # AL4: the cases on hold (OPP-3107, OPP-3108) are listed like any other
    # SC1: the direct customer's OPP-3110 is listed too
    assert [r.opp_no for r in rows] == ["OPP-3107", "OPP-3108", "OPP-3110", "OPP-3101", "OPP-3104", "OPP-3102",
                                        "OPP-3103"]
    three = next(r for r in rows if r.opp_no == "OPP-3102")
    assert three.subject == "RATE REQUEST // TALCA - HOUSTON" and three.requester == "Kestrel Forwarding"


def test_the_request_card_shows_assigned_to(world):
    conn, path, ids = world
    app = create_app(path)
    app.config["TESTING"] = True
    client = app.test_client()
    card = client.get("/enquiries/{}".format(ids["OPP-3103"])).data.decode("utf-8")
    card = card[card.index('id="request"'):card.index("</section>", card.index('id="request"'))]
    assert '<div class="k">Assigned to</div><div class="v">Ben</div>' in card
    card = client.get("/enquiries/{}".format(ids["OPP-3101"])).data.decode("utf-8")
    assert '<div class="k">Assigned to</div><div class="v"><span class="m">Not assigned</span></div>' in card
    # CX1: the sheet's part of Settings is gone
    assert 'id="assign-list"' not in client.get("/settings").data.decode("utf-8")


def test_a_case_on_hold_is_listed_in_its_usual_place_and_a_hold_changes_nothing(world):
    conn, path, ids = world
    repo = db.Repository(conn, source="test")
    party = conn.execute("SELECT requester_party_id FROM enquiry WHERE id = ?", (ids["OPP-3101"],)).fetchone()[0]
    ahead = add_enquiry(repo, "OPP-4201", party, received="2026-11-08",
                        hold_reason="waiting for the permit", hold_until="2026-12-15")
    no_until = add_enquiry(repo, "OPP-4202", party, received="2026-11-09", hold_reason="client travelling")
    passed = add_enquiry(repo, "OPP-4203", party, received="2026-11-13",
                         hold_reason="waiting", hold_until="2026-11-14")
    repo.commit()
    rows = assign_list.listed(conn, TODAY)
    assert [r.opp_no for r in rows] == ["OPP-3107", "OPP-3108", "OPP-3110", "OPP-4201", "OPP-4202", "OPP-3101",
                                        "OPP-3104", "OPP-3102", "OPP-4203", "OPP-3103"]          # SC1: OPP-3110
    order = [r.opp_no for r in rows]
    repo.set_hold(ids["OPP-3101"], "waiting for the shipper", "2026-11-18", until="2026-12-20")
    repo.release_hold(ahead, "2026-11-18")
    repo.commit()
    # a hold never moves a row (AL5: only its On hold text changes)
    assert [r.opp_no for r in assign_list.listed(conn, TODAY)] == order
    assert no_until and passed


def test_the_on_hold_cell_says_the_reason_and_the_until_date(world):
    conn, path, ids = world
    repo = db.Repository(conn, source="test")
    party = conn.execute("SELECT requester_party_id FROM enquiry WHERE id = ?", (ids["OPP-3101"],)).fetchone()[0]
    add_enquiry(repo, "OPP-4601", party, received="2026-11-08", hold_reason="  waiting for  the permit ",
                hold_until="2026-12-05")
    add_enquiry(repo, "OPP-4602", party, received="2026-11-09", hold_reason="client travelling")
    add_enquiry(repo, "OPP-4603", party, received="2026-11-09", hold_reason="x" * 250)
    repo.commit()
    cells = {r.opp_no: r.on_hold for r in assign_list.listed(conn, TODAY)}
    assert cells["OPP-4601"] == "waiting for the permit \u00b7 until 12/05"
    assert cells["OPP-4602"] == "client travelling"
    assert cells["OPP-4603"] == "x" * assign_list.HOLD_REASON_MAX
    assert cells["OPP-3101"] == ""

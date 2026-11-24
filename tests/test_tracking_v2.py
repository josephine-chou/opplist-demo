"""Tracking v2 (2026-09-24): the view model splits R3 by step
and its counts are the tiles and the two totals; every rule's actions
render with the routes they always posted to; the Decide batch form
carries only ticked ids; R7 renders party rows; an empty group says
Nothing here; a late row carries the due class."""

from __future__ import annotations

import datetime as dt
import re

import pytest

from conftest import add_enquiry, add_party, seed_reference, form_nesting
from opplist import db, shipments, tracking
from opplist import web as web_module
from opplist.web import create_app

TODAY = "2026-09-24"


def ago(days):
    return (dt.date.fromisoformat(TODAY) - dt.timedelta(days=days)).isoformat()


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "trk.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")                       # LATAM agent
    tsv = add_party(repo, "KRV Global", country="USA")
    office = add_party(repo, "JoJo JQN", ptype="Office", country="USA")
    seavio = add_party(repo, "Seavio", ptype="Unknown", country="USA")
    tbc = add_party(repo, "(TBC)", ptype="Unknown", country=None, placeholder=1)
    db.set_setting(conn, "closed_reasons_lost", ["Price too high", "No response from customer", "Other"])
    ids = {}
    # R3 Replied: quoted, then a reply note asking something, 3 days ago
    c = add_enquiry(repo, "OPP-1301", tolvex, received=ago(20), service_mode="FCL", origin_country="USA",
                    origin_loc="MIA", dest_country="Chile", dest_loc="SAI", commodity="Spare parts")
    repo.set_status(c, "Quoted", ago(10))
    repo.add_note(c, ago(3), "Reply from Ana (needs an answer): can you redo it with 2 x 40' please https://x.example/z [mid:<a@b>]", source="mail")
    ids["replied"] = c
    # R3 FU1 due: quoted 4 working days ago, nothing sent
    c = add_enquiry(repo, "OPP-1302", tsv, received=ago(12), service_mode="Air", origin_country="USA", origin_loc="MIA", dest_country="China", dest_loc="NGB")
    repo.set_status(c, "Quoted", ago(6))
    ids["fu1"] = c
    # R3 FU2 due: FU1 sent 9 working days ago -> late (calendar 14+ since the quote? no: since FU1) -> not late
    c = add_enquiry(repo, "OPP-1303", office, received=ago(30), service_mode="LCL", origin_country="USA", origin_loc="MIA", dest_country="Chile", dest_loc="SAI")
    repo.set_status(c, "Quoted", ago(20))
    repo.add_followup(c, ago(9))
    ids["fu2"] = c
    # R3 Decide, late: quoted 40 days ago, two follow-ups long ago
    c = add_enquiry(repo, "OPP-1304", tolvex, received=ago(45), service_mode="FCL", origin_country="USA", origin_loc="MIA", dest_country="Chile", dest_loc="SAI")
    repo.set_status(c, "Quoted", ago(40))
    repo.add_followup(c, ago(38))
    repo.add_followup(c, ago(30))
    ids["decide"] = c
    # R1: New, untouched for 10 working days, late
    ids["r1"] = add_enquiry(repo, "OPP-1305", tsv, received=ago(14), service_mode="Air", origin_country="USA", origin_loc="MIA", dest_country="Chile", dest_loc="SAI")
    # R9: a vendor replied after the last event
    c = add_enquiry(repo, "OPP-1306", tolvex, received=ago(8), service_mode="Air", origin_country="USA", origin_loc="MIA", dest_country="Chile", dest_loc="SAI")
    repo.set_status(c, "New", ago(7))
    repo.insert("vendor_request", {"enquiry_id": c, "vendor_party_id": seavio, "asked_on": ago(6), "replied_on": ago(2)})
    ids["r9"] = c
    # R2: a vendor silent for 5 working days
    c = add_enquiry(repo, "OPP-1307", tsv, received=ago(10), service_mode="FCL", origin_country="USA", origin_loc="MIA", dest_country="China", dest_loc="NGB")
    repo.set_status(c, "New", ago(9))
    repo.insert("vendor_request", {"enquiry_id": c, "vendor_party_id": seavio, "asked_on": ago(8)})
    ids["r2"] = c
    # R6: won with a vendor asked, rate_used empty
    c = add_enquiry(repo, "OPP-1308", tolvex, received=ago(20), service_mode="Air", origin_country="USA", origin_loc="MIA", dest_country="Chile", dest_loc="SAI")
    repo.insert("vendor_request", {"enquiry_id": c, "vendor_party_id": seavio, "asked_on": ago(15), "machine_status": "Quoted"})
    repo.set_status(c, "Quoted", ago(14))
    repo.set_status(c, "Won", ago(5))
    ids["r6"] = c
    # R5: parked on (TBC)
    c = add_enquiry(repo, "OPP-1309", tbc, received=ago(3), service_mode="Air", origin_country="USA", origin_loc="MIA", dest_country="Chile", dest_loc="SAI")
    # work under way: a vendor asked today (R2's in two working days), so not R1
    repo.insert("vendor_request", {"enquiry_id": c, "vendor_party_id": seavio, "asked_on": TODAY})
    ids["r5"] = c
    # R4: unconfirmed status
    ids["r4"] = add_enquiry(repo, "OPP-1310", tsv, received=ago(25), status_confirmed=0, service_mode="Air", origin_country="USA", origin_loc="MIA", dest_country="China", dest_loc="NGB")
    # R8: a country with no location
    c = add_enquiry(repo, "OPP-1311", tsv, received=ago(2), service_mode="FCL", origin_country="USA", origin_loc="MIA", dest_country="Chile")
    # work under way: a vendor asked today (R2's in two working days), so not R1
    repo.insert("vendor_request", {"enquiry_id": c, "vendor_party_id": seavio, "asked_on": TODAY})
    ids["r8"] = c
    # R7: a party quiet for longer than its cadence (no enquiries, so nothing moves its clock)
    quiet = add_party(repo, "Quiet Co", country="Chile", stage="active", cadence_days=30)
    repo.insert("party_event", {"party_id": quiet, "type": "outreach", "at": ago(90), "summary": "hello"})
    ids["tsv"] = quiet
    # R10: an active shipment with an ETA gone by
    c = add_enquiry(repo, "OPP-1312", tolvex, received=ago(30), service_mode="Air", origin_country="USA", origin_loc="MIA", dest_country="Chile", dest_loc="SAI")
    repo.set_status(c, "Quoted", ago(25))
    repo.set_status(c, "Won", ago(20), rate_used="own")
    sid = shipments.start(repo, c, "Miami to Santiago")
    shipments.edit(repo, sid, {"planned_arrival": ago(3)})
    shipments.log_milestone(repo, sid, "Departed", ago(6) + " 08:00")
    ids["sid"] = sid
    # a pending suggestion on the Replied case
    repo.insert("suggestion", {"enquiry_id": ids["replied"], "opp_no": "OPP-1301", "kind": "status", "proposed_status": "Won",
                               "proposed_date": ago(1), "confidence": "high", "source": "mail", "evidence_date": ago(1),
                               "evidence_sender": "Ana", "evidence_quote": "please proceed with the booking", "state": "pending"})
    repo.commit()
    monkeypatch.setattr(web_module, "_today", lambda: TODAY)
    return conn, path, ids


# test_page_model_splits_r3_and_counts_match_the_tiles: replaced in batch T1 by tests/test_tracking_order.py (it pinned the old card order or the Tracking suggestions card).


def _page(path):
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), app.test_client().get("/tracking").data.decode("utf-8")


# test_every_rule_renders_its_actions_with_the_existing_routes: replaced in batch T1 by tests/test_tracking_order.py (it pinned the old card order or the Tracking suggestions card).


# test_the_decide_batch_posts_only_ticked_ids_and_empty_groups_say_nothing: replaced in batch T1 by tests/test_tracking_order.py (it pinned the old card order or the Tracking suggestions card).


# test_show_all_marks_rows_past_ten_as_extra: replaced in batch T1 by tests/test_tracking_order.py (it pinned the old card order or the Tracking suggestions card).

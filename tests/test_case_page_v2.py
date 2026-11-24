"""The case page v2 (2026-09-24): the sample's layout on the real
routes - header, facts, rail, cards, bottom bar - with every form and
field name as it was."""

from __future__ import annotations

import re

import pytest

from conftest import add_enquiry, add_party, seed_reference, form_nesting
from opplist import db, suggestions, tracking
from opplist.web import create_app


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "case-v2.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    repo.insert("contact", {"party_id": tolvex, "name": "Ana Vera", "email": "ana@tolvex.example",
                            "is_primary": 1})
    seavio = add_party(repo, "Seavio", country="USA")
    # a Quoted case: received Monday Sep 7, quoted Monday Sep 14, FU1 on the 16th
    quoted = add_enquiry(repo, "OPP-1407", tolvex, received="2026-09-07", service_mode="FCL",
                         container_type="40GP", container_qty=1, incoterm="EXW",
                         origin_country="USA", origin_loc="MIA", origin_alt="1187 Juniper Crossing Road",
                         dest_country="Chile", dest_loc="SAI", commodity="Tomato Juice")
    repo.set_status(quoted, "Quoted", "2026-09-14")
    repo.add_followup(quoted, "2026-09-16")
    repo.insert("vendor_request", {"enquiry_id": quoted, "vendor_party_id": seavio,
                                   "asked_on": "2026-09-14", "replied_on": "2026-09-14",
                                   "machine_status": "Quoted", "their_ref": "KQRS64829"})
    # an earlier case of the same requester, for the context strip
    old = add_enquiry(repo, "OPP-1146", tolvex, received="2026-05-24", service_mode="Project",
                      dest_loc="SAI", dest_country="Chile")
    repo.set_status(old, "Dropped", "2026-06-01", closed_reason="Registered in error")
    # a Won case
    won = add_enquiry(repo, "OPP-1408", tolvex, received="2026-09-01", service_mode="Air",
                      origin_country="USA", dest_country="Chile", air_weight_kg=120)
    repo.set_status(won, "Quoted", "2026-09-03")
    repo.set_status(won, "Won", "2026-09-10", rate_used="own")
    # a New case with nothing on it
    fresh = add_enquiry(repo, "OPP-1409", tolvex, received="2026-09-20")
    # a case with an open suggestion
    pending = add_enquiry(repo, "OPP-1410", tolvex, received="2026-09-18", service_mode="LCL",
                          origin_country="USA", dest_country="Chile")
    repo.set_status(pending, "Quoted", "2026-09-19")
    suggestions.record(repo, pending, "OPP-1410", {
        "kind": "status", "proposed_status": "Won", "proposed_date": "2026-09-22",
        "evidence_sender": "Ana <ana@tolvex.example>", "evidence_date": "2026-09-22",
        "evidence_quote": "Please proceed with the booking.", "confidence": "high",
        "source": "mail"})
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, {"quoted": quoted, "won": won, "fresh": fresh, "pending": pending}


def page(client, case_id):
    response = client.get("/enquiries/{}".format(case_id))
    assert response.status_code == 200
    return response.data.decode("utf-8")


# test_the_quoted_case_renders_the_new_layout: replaced in 15a by tests/test_case_v3.py (case page v3).


# test_the_facts_row_counts_working_days: replaced in L2 by tests/test_fu1_wait_rewrites.py (no FU2 without an answer to FU1)


# test_the_ladder_helper_puts_dates_on_the_rungs: replaced in L2 by tests/test_fu1_wait_rewrites.py (no FU2 without an answer to FU1)


def test_rail_ticks_follow_the_required_fields(world):
    client, path, ids = world
    body = page(client, ids["quoted"])
    rail = body[body.index('<ol class="sections">'):body.index("</ol>", body.index('<ol class="sections">'))]
    ticks = re.findall(r'<li class="sec ([^"]*)"><a href="#(\w+)"', rail)
    assert dict((k, v) for v, k in ticks) == {"request": "ok", "cargo": "ok", "routes": "ok",
                                                "vendors": "", "status": "", "followups": "",
                                                "mail": ""}                      # batch 3 added Mail
    body = page(client, ids["fresh"])
    rail = body[body.index('<ol class="sections">'):body.index("</ol>", body.index('<ol class="sections">'))]
    ticks = dict((k, v) for v, k in re.findall(r'<li class="sec ([^"]*)"><a href="#(\w+)"', rail))
    assert ticks["request"] == "ok" and ticks["cargo"] == "" and ticks["routes"] == ""


def test_an_open_suggestion_sits_in_the_status_card_not_at_the_top(world):
    client, path, ids = world
    body = page(client, ids["pending"])
    assert "Suggested updates" in body
    assert body.index('id="status"') < body.index("Suggested updates") < body.index('id="followups"')
    assert "Daily scan: nothing open" not in body
    body = page(client, ids["quoted"])
    # nothing open: no daily-scan line at all (2026-09-25)
    assert "Daily scan" not in body and "no suggestion yet" not in body


def test_the_request_card_has_no_requester_context_and_no_lanes_line_on_its_own(world):
    """2026-09-25: the requester line is gone; the lanes line shows only
    when the request has other lanes; Add a lane and Same request as are
    in the bottom bar's More menu."""
    client, path, ids = world
    body = page(client, ids["quoted"])
    card = body[body.index('id="request"'):body.index('id="cargo"')]
    assert '<div class="ctx">' not in card
    for gone in ("5 enquiries", "earlier", "OPP-1146", "Ana Vera", "Lanes in this request",
                 "only this one", "Add a lane", "Same request as"):
        assert gone not in card, gone
    more = body[body.index('<details class="more-menu">'):body.index("</details>", body.index('<details class="more-menu">'))]
    # v33: no Add a lane, no Same request as - a destination is a route
    assert ">Add a lane</a>" not in more and "Same request as" not in more and "same-request-form" not in body


# test_the_vendor_row_is_read_only_with_the_edit_form_folded_under_it: replaced in 15a by tests/test_case_v3.py (case page v3).


# test_saving_the_main_form_still_writes_the_fields: replaced in 15b by tests/test_cargo_lines_only.py (cargo is lines only).


# test_the_new_enquiry_page_keeps_its_fields_in_the_same_three_groups: replaced in 15c by tests/test_register_v3.py (the register page is the case page's cards).

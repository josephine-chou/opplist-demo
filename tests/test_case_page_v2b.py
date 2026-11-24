"""Case page v2b (2026-09-25): the corrections after a day on
the v2 page - section order, no Owner, the sticky / condensed header,
facts by status, rail states, Set status above the timeline, the Route
card as two panels. Every route, form and field name is as it was."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import add_enquiry, add_party, seed_reference, form_nesting
from opplist import db, tracking
from opplist.web import create_app

STATIC = Path(__file__).resolve().parents[1] / "opplist" / "web" / "static"


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "case-v2b.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    seavio = add_party(repo, "Seavio", country="USA")
    # Quoted: received Monday Sep 7, quoted Monday Sep 14, FU1 on the 16th
    quoted = add_enquiry(repo, "OPP-1407", tolvex, received="2026-09-07", service_mode="FCL",
                         container_type="40GP", container_qty=1, incoterm="EXW",
                         origin_country="USA", origin_loc="MIA", origin_alt="1187 Juniper Crossing Road",
                         dest_country="Chile", dest_loc="SAI", commodity="Tomato Juice", owner="Iris")
    repo.set_status(quoted, "Quoted", "2026-09-14")
    repo.add_followup(quoted, "2026-09-16")
    repo.insert("vendor_request", {"enquiry_id": quoted, "vendor_party_id": seavio,
                                   "asked_on": "2026-09-14", "replied_on": "2026-09-14",
                                   "machine_status": "Quoted", "their_ref": "KQRS64829"})
    # New: nothing done, received Monday Sep 14
    fresh = add_enquiry(repo, "OPP-1409", tolvex, received="2026-09-14")
    # Sourcing: one vendor asked Monday Sep 14, silent
    sourcing = add_enquiry(repo, "OPP-1411", tolvex, received="2026-09-14", service_mode="LCL",
                           origin_country="USA", dest_country="Chile")
    repo.set_status(sourcing, "New", "2026-09-14")
    repo.insert("vendor_request", {"enquiry_id": sourcing, "vendor_party_id": seavio,
                                   "asked_on": "2026-09-14"})
    # Won: quoted Thursday Sep 3, won Thursday Sep 10, Seavio's rate
    won = add_enquiry(repo, "OPP-1408", tolvex, received="2026-09-01", service_mode="Air",
                      origin_country="USA", dest_country="Chile", air_weight_kg=120)
    repo.set_status(won, "Quoted", "2026-09-03")
    repo.set_status(won, "Won", "2026-09-10", rate_used="agent")
    repo.update("enquiry", won, {"awarded_vendor_id": seavio})
    # Lost: quoted, two follow-ups, lost on price
    lost = add_enquiry(repo, "OPP-1412", tolvex, received="2026-08-03", service_mode="FCL",
                       container_type="20GP", container_qty=2, origin_country="USA", dest_country="Chile")
    repo.set_status(lost, "Quoted", "2026-08-05")
    repo.add_followup(lost, "2026-08-07")
    repo.add_followup(lost, "2026-08-14")
    repo.set_status(lost, "Lost", "2026-08-20", closed_reason="Price too high")
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, {"quoted": quoted, "fresh": fresh, "sourcing": sourcing,
                                     "won": won, "lost": lost}


def page(client, case_id):
    response = client.get("/enquiries/{}".format(case_id))
    assert response.status_code == 200
    return response.data.decode("utf-8")


def tiles(body):
    facts = body[body.index('<div class="facts">'):body.index('<div class="layout">')]
    labels = re.findall(r'<div class="f-l">(.*?)</div>', facts)
    values = [(v.strip(), s.strip()) for v, s in
              re.findall(r'<div class="f-v[^"]*">(.*?)</div>\s*<div class="f-s">(.*?)</div>', facts, re.S)]
    return labels, values


def rail(body):
    start = body.index('<ol class="sections">')
    text = body[start:body.index("</ol>", start)]
    return re.findall(r'<li class="sec ([^"]*)"><a href="#(\w+)">.*?<span class="s-name">[^<]*</span>'
                      r'(?:<span class="state">([^<]*)</span>)?</a>', text)


# ------------------------------------------------------------- order ----
def test_the_main_column_runs_status_vendors_form_mail(world):
    client, path, ids = world
    body = page(client, ids["quoted"])
    order = [body.index('id="{}"'.format(k)) for k in ("status", "followups", "request", "routes", "cargo", "vendors", "mail")]
    assert order == sorted(order)
    # the rail lists them the same way, Mail last
    assert [key for _, key, _ in rail(body)] == ["status", "followups", "request", "routes", "cargo", "vendors", "mail"]
    deepest, ids_ = form_nesting(body)
    assert deepest <= 1, ids_


def test_the_won_card_sits_after_the_rate_requests(world):
    client, path, ids = world
    body = page(client, ids["won"])
    assert body.index('<div class="two">') < body.index('id="vendors"') < body.index('id="won"') < body.index('id="mail"')


# test_no_owner_select_on_the_case_page: DD1 (2026-11-12) puts the person back in the Request card's
# Edit; its other half (a save without the field keeps the person) is in tests/test_dd1.py.


def test_the_header_has_the_sentinel_and_the_condensed_hook(world):
    client, path, ids = world
    body = page(client, ids["quoted"])
    assert '<div class="head-sentinel" aria-hidden="true"><span id="head-sentinel"></span></div>' in body
    assert '<div class="card head" id="head">' in body
    assert body.index('id="head-sentinel"') < body.index('id="head"')
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "IntersectionObserver" in script and '"condensed"' in script and '$("head-sentinel")' in script
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert ".case-page .head { position: sticky;" in css
    assert ".case-page .head.condensed .sub" in css
    assert "var(--head-h" in css                                    # the rail's offset follows


# ------------------------------------------------------------- facts ----
# test_the_quoted_facts_are_as_before: replaced in L2 by tests/test_fu1_wait_rewrites.py (no FU2 without an answer to FU1)


def test_the_new_and_sourcing_facts_carry_age_and_the_tracking_line(world):
    client, path, ids = world
    labels, values = tiles(page(client, ids["fresh"]))
    assert labels == ["Received", "Age", "Rate requests", "Vendor quote", "Next"]
    assert values[0] == ("Sep 14", "Mon")
    assert values[1][0].endswith("working days") and values[1][1] == "since Sep 14"
    assert values[2] == ("0 asked · 0 replied", "") and values[3] == ("none yet", "")
    assert values[4][0].startswith("Nothing done yet, ") and values[4][1] == "Set a status, or ask a vendor."
    labels, values = tiles(page(client, ids["sourcing"]))
    assert labels[4] == "Next" and values[2] == ("1 asked · 0 replied", "")
    assert values[4][0].startswith("Vendor Seavio no reply, ") and values[4][1] == "Chase them, or record what came back."


def test_the_nudge_helper_follows_the_tracking_thresholds(world):
    client, path, ids = world
    conn = db.open_db(path)
    fresh = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (ids["fresh"],)).fetchone()
    assert tracking.case_nudge(conn, fresh, today="2026-09-15") == {
        "rule": "", "text": "Send the quote", "hint": "", "days": None}       # 1 working day: not yet
    assert tracking.case_nudge(conn, fresh, today="2026-09-16")["text"] == "Nothing done yet, 2 days"
    sourcing = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (ids["sourcing"],)).fetchone()
    assert tracking.case_nudge(conn, sourcing, today="2026-09-15")["text"] == "Send the quote"
    assert tracking.case_nudge(conn, sourcing, today="2026-09-18") == {
        "rule": "R2", "text": "Vendor Seavio no reply, 4 days",
        "hint": "Chase them, or record what came back.", "days": 4}
    quoted = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (ids["quoted"],)).fetchone()
    assert tracking.case_nudge(conn, quoted, today="2026-09-18")["text"] == "Send the quote"
    conn.close()


def test_the_won_facts_show_the_award(world):
    client, path, ids = world
    labels, values = tiles(page(client, ids["won"]))
    assert labels == ["Received", "Quoted", "Won", "Rate used", "Vendor quote"]
    assert values[1] == ("Sep 3", "2 working days")                      # Tue 1 to Thu 3
    assert values[2] == ("Sep 10", "5 working days after the quote")     # Thu 3 to Thu 10
    assert values[3] == ("Agent rate", "Seavio")
    assert values[4] == ("0 asked · 0 replied", "")


def test_the_lost_facts_show_the_reason_and_the_follow_ups(world):
    client, path, ids = world
    body = page(client, ids["lost"])
    labels, values = tiles(body)
    assert labels == ["Received", "Quoted", "Lost", "Follow-ups sent", "Vendor quote"]
    assert values[1] == ("Aug 5", "2 working days")
    assert values[2] == ("Aug 20", "Price too high")
    assert values[3] == ("2", "FU2 Aug 14")
    assert "none yet" not in body[body.index('<div class="facts">'):body.index('<div class="layout">')]
    # the closed Follow-ups card still says the one line (v23) after the reorder
    assert ">Closed Aug 20</p>" in body and "nothing to chase" not in body and 'class="ladder"' not in body
    assert 'id="fu-text"' not in body


# -------------------------------------------------------------- rail ----
# test_the_rail_ticks_only_the_entry_cards_and_states_the_rest: replaced in L2 by tests/test_fu1_wait_rewrites.py (no FU2 without an answer to FU1)


# ------------------------------------------------------------ status ----
# test_set_status_sits_under_the_current_status_and_the_timeline_is_newest_first: replaced in 15a by tests/test_case_v3.py (case page v3).


# ------------------------------------------------------------- route ----
# test_the_route_card_is_two_panels_with_remarks_below: replaced in 15a by tests/test_case_v3.py (case page v3).

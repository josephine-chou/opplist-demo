"""Shipments (schema v24): the milestone sets, the derived
summary, the prefill from a won case, the list, the page, the forms,
void / unvoid, and tracking rule R10.

Rewritten 2026-11-03 (TK1) on invented values;
cases OPP-1380, OPP-1470 and OPP-1471.
"""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference, form_nesting
from opplist import db, shipments, tracking
from opplist import web as web_module
from opplist.web import create_app

TODAY = "2015-09-23"


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "ships.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(repo.conn, "shipments", "1")   # FX3: switched on for these tests
    orx = add_party(repo, "Brisa Freight Services", country="USA")
    carrier = add_party(repo, "Hapag-Lloyd", ptype="Unknown", country="USA")
    klx = add_party(repo, "KLX Logistics", country="USA")
    # an ocean case, won, the carrier's rate used
    ocean = add_enquiry(repo, "OPP-1380", orx, received="2015-08-20", service_mode="FCL",
                        container_type="20GP", container_qty=1, origin_country="USA", origin_loc="MIA",
                        origin_alt="Auburn Hills, MI 48326, USA", dest_country="Chile", dest_loc="SAI",
                        dest_alt="Rancagua, Chile", commodity="Rowing machines")
    repo.set_status(ocean, "Quoted", "2015-08-22")
    repo.set_status(ocean, "Won", "2015-08-26", rate_used="carrier")
    repo.update("enquiry", ocean, {"awarded_vendor_id": carrier})
    # a truck case, won, own rate
    truck = add_enquiry(repo, "OPP-1470", orx, received="2015-09-18", service_mode="Truck",
                        origin_country="USA", dest_country="USA", origin_alt="Hazleton, PA 18201",
                        dest_alt="Plainfield, IN 46168")
    repo.set_status(truck, "Quoted", "2015-09-18")
    repo.set_status(truck, "Won", "2015-09-22", rate_used="own")
    # a quoted case: no shipment allowed
    quoted = add_enquiry(repo, "OPP-1471", orx, received="2015-09-18", service_mode="Air")
    repo.set_status(quoted, "Quoted", "2015-09-19")
    repo.commit()
    return conn, repo, path, {"ocean": ocean, "truck": truck, "quoted": quoted, "carrier": carrier, "klx": klx}


def ocean_with_events(repo, ocean):
    """The sample's ocean shipment: 16 events, ETA pushed Oct 15 to Oct 27."""
    sid = shipments.start(repo, ocean, "Auburn Hills, MI to Rancagua")
    shipments.edit(repo, sid, {"planned_departure": "2015-09-14", "planned_arrival": "2015-10-15",
                               "carrier_detail": "Cap Lindero 614S", "booking_no": "HLCU-DEMO-4412",
                               "shipper_name": "Vantroy Fitness", "consignee_name": "Club Deportivo Andalue",
                               "remarks": "Auburn Hills, MI trucked to Miami; Hapag-Lloyd via Panama to San Antonio"})
    shipments.log_milestone(repo, sid, "Booked", "2015-08-27 10:00", "space held on the first sailing")
    shipments.add_note(repo, sid, "Rebooked", "2015-09-02 09:00", "moved to the next vessel")
    shipments.log_milestone(repo, sid, "Picked up", "2015-09-03 14:00")
    for i in range(1, 10):
        shipments.add_note(repo, sid, "Note {}".format(i), "2015-09-0{} 08:00".format(min(i, 9)))
    shipments.log_milestone(repo, sid, "Departed", "2015-09-17 06:00", "sailed, papers to follow")
    issue = shipments.log_issue(repo, sid, "Delay", "2015-09-19 11:20",
                                "The carrier site now shows San Antonio on 27 Oct, not 15 Oct.")
    shipments.add_note(repo, sid, "ETA moved", "2015-09-19 11:25", "", kind="info")
    shipments.add_note(repo, sid, "Delay notice asked for", "2015-09-19 11:40", "a written notice requested")
    shipments.edit(repo, sid, {"eta": "2015-10-27"})
    repo.commit()
    return sid, issue


# ---------------------------------------------------------- milestones ----
def test_milestone_sets_per_mode():
    assert shipments.MILESTONES["ocean"] == ("Booked", "Picked up", "Departed", "Arrived", "Delivered", "Invoiced")
    assert shipments.MILESTONES["air"] == shipments.MILESTONES["ocean"]
    assert shipments.MILESTONES["truck"] == ("Booked", "Dispatched", "Picked up", "Delivered", "POD", "Invoiced")
    assert [shipments.mode_for(m) for m in ("FCL", "LCL", "RORO", "Break Bulk", "Air", "Truck", "Project", None)] == \
        ["ocean", "ocean", "ocean", "ocean", "air", "truck", "ocean", "ocean"]


# --------------------------------------------------------------- start ----
def test_start_prefills_from_the_won_case(world):
    conn, repo, path, ids = world
    values = shipments.prefill(conn, ids["ocean"])
    assert values == {"enquiry_id": ids["ocean"], "mode": "ocean", "title": "MIA to SAI",
                      "shipper_address": "Auburn Hills, MI 48326, USA", "consignee_address": "Rancagua, Chile",
                      "carrier": "Hapag-Lloyd"}
    sid = shipments.start(repo, ids["ocean"])
    ship = conn.execute("SELECT * FROM shipment WHERE id = ?", (sid,)).fetchone()
    assert (ship["mode"], ship["title"], ship["carrier"], ship["our_file"], ship["planned_arrival"], ship["closed"]) == \
        ("ocean", "MIA to SAI", "Hapag-Lloyd", None, None, 0)
    assert conn.execute("SELECT COUNT(*) FROM shipment_event WHERE shipment_id = ?", (sid,)).fetchone()[0] == 0
    assert shipments.start(repo, ids["ocean"]) == sid                        # one per case
    assert conn.execute("SELECT COUNT(*) FROM change_log WHERE table_name = 'shipment'").fetchone()[0] == 1
    # own rate: no carrier; a truck case is a truck shipment
    truck = shipments.prefill(conn, ids["truck"])
    assert (truck["mode"], truck["carrier"], truck["shipper_address"]) == ("truck", None, "Hazleton, PA 18201")
    with pytest.raises(ValueError):
        shipments.start(repo, ids["quoted"])


# ------------------------------------------------------------- summary ----
def test_summary_derives_milestone_issues_delay_and_pills(world):
    conn, repo, path, ids = world
    sid, issue = ocean_with_events(repo, ids["ocean"])
    state = shipments.summary(conn, sid, TODAY)
    assert state["current"] == "Departed" and state["current_index"] == 2
    assert [e["title"] for e in state["open_issues"]] == ["Delay"]
    assert state["delayed_days"] == 12
    assert state["pills"] == [("Ocean", "p-mode"), ("In transit", "p-transit"), ("Delayed 12 days", "p-delay"),
                              ("Delay", "p-delay")]
    assert state["active"] and not state["delivered"]
    assert [r[:2] for r in shipments.stepper(state)] == [
        ("Booked", "done"), ("Picked up", "done"), ("Departed", "current"), ("Arrived", "todo"),
        ("Delivered", "todo"), ("Invoiced", "todo")]
    assert shipments.stepper(state)[3][2] == "ETA Oct 27"
    facts = dict((f[0], (f[1], f[2])) for f in shipments.facts(state))
    assert facts["Expected arrival"] == ("Oct 27", "was Oct 15")
    assert facts["Transit"] == ("40 days", "planned 31")
    # resolving the issue clears the pill; a milestone set order wins over time order
    shipments.resolve(repo, sid, issue, "2015-09-22 09:00")
    shipments.log_milestone(repo, sid, "Arrived", "2015-10-27 08:00")
    shipments.log_milestone(repo, sid, "Delivered", "2015-10-29 08:00")
    state = shipments.summary(conn, sid, TODAY)
    assert state["open_issues"] == [] and state["current"] == "Delivered" and state["delivered"]
    assert state["pills"] == [("Ocean", "p-mode"), ("Delivered", "p-transit"), ("Delayed 12 days", "p-delay")]
    assert not state["active"]
    with pytest.raises(ValueError):
        shipments.log_milestone(repo, sid, "POD", "2015-10-30 08:00")     # not an ocean milestone


def test_truck_summary_and_facts(world):
    conn, repo, path, ids = world
    sid = shipments.start(repo, ids["truck"], "Hazleton, PA to Plainfield, IN")
    shipments.edit(repo, sid, {"planned_pickup": "2015-09-23", "planned_delivery": "2015-09-24",
                               "price_note": "$1,265", "special": "driver assist at delivery", "carrier": "KLX Logistics"})
    shipments.log_milestone(repo, sid, "Booked", "2015-09-22 16:44", "rate confirmation signed")
    shipments.log_milestone(repo, sid, "Dispatched", "2015-09-22 17:38")
    shipments.log_issue(repo, sid, "Pickup at risk", "2015-09-23 08:45", "truck broke down on the way in")
    state = shipments.summary(conn, sid, TODAY)
    assert state["current"] == "Dispatched" and state["status_text"] == "Dispatched"
    assert state["pills"] == [("Truckload", "p-mode"), ("Dispatched", "p-transit"), ("Pickup at risk", "p-delay")]
    assert [f[0] for f in shipments.facts(state)] == ["Pickup window", "Delivery window", "Price", "Load number"]
    assert shipments.facts(state)[3] == ("Load number", "none", "")
    assert shipments.references(state["ship"]) == [("Special", "driver assist at delivery")]


# ---------------------------------------------------------------- list ----
# test_the_list_filters: replaced in batch 18 by tests/test_inquiry_words.py (it read the old words).


# ---------------------------------------------------------------- page ----
def test_the_page_renders_an_ocean_and_a_truck_shipment(world, monkeypatch):
    conn, repo, path, ids = world
    sid, issue = ocean_with_events(repo, ids["ocean"])
    tid = shipments.start(repo, ids["truck"], "Hazleton, PA to Plainfield, IN")
    shipments.log_issue(repo, tid, "Pickup at risk", "2015-09-23 08:45", "truck broke down on the way in")
    repo.commit()
    conn.close()
    monkeypatch.setattr(web_module, "_today", lambda: TODAY)
    app = create_app(path)
    app.config["TESTING"] = True
    client = app.test_client()
    body = client.get("/shipments/{}".format(sid)).data.decode("utf-8")
    assert "Shipments</a> &rsaquo; <b>Auburn Hills, MI to Rancagua</b>" in body
    assert '<span class="pill p-transit">In transit</span>' in body and '<span class="pill p-delay">Delayed 12 days</span>' in body
    assert "Won <b>Aug 26</b>" in body and "Brisa Freight Services" in body and "Rowing machines" in body
    assert '<div class="who">Vantroy Fitness</div>' in body and "Cap Lindero 614S" in body
    assert body.count('<div class="step done">') == 2 and body.count('<div class="step current">') == 1
    assert "latest 10 of 16 events" in body and "6 earlier events" in body
    assert body.count('<li class="ev ') == 16
    assert '<div class="alert"><b>Delay.</b>' in body and "open since 2015-09-19 11:20" in body
    assert 'action="/shipments/{}/events/{}"'.format(sid, issue) in body and ">resolve<" in body
    assert 'name="milestone"' in body and 'type="datetime-local"' in body
    assert "Hapag-Lloyd via Panama to San Antonio" in body and "HLCU-DEMO-4412" in body
    assert 'action="/shipments/{}/update-from-outlook"'.format(sid) in body   # 5b
    deepest, ids_ = form_nesting(body)
    assert deepest <= 1, ids_
    body = client.get("/shipments/{}".format(tid)).data.decode("utf-8")
    assert '<span class="pill p-mode">Truckload</span>' in body and '<span class="pill p-delay">Pickup at risk</span>' in body
    assert "Pickup window" in body and "Load number" in body and ">POD<" in body
    deepest, ids_ = form_nesting(body)
    assert deepest <= 1, ids_
    # the case page offers Open shipment for the ocean case, Start shipment for a won case without one
    body = client.get("/enquiries/{}".format(ids["ocean"])).data.decode("utf-8")
    assert 'href="/shipments/{}"'.format(sid) in body and "Start shipment" not in body
    body = client.get("/enquiries/{}".format(ids["quoted"])).data.decode("utf-8")
    assert "Start shipment" not in body and "Open shipment" not in body


# --------------------------------------------------------------- forms ----
def test_the_forms_write_events_through_the_repository(world, monkeypatch):
    conn, repo, path, ids = world
    conn.close()
    monkeypatch.setattr(web_module, "_today", lambda: TODAY)
    app = create_app(path)
    app.config["TESTING"] = True
    client = app.test_client()
    # Start shipment from the case page
    page = client.post("/enquiries/{}/shipment/start".format(ids["truck"]), follow_redirects=True)
    body = page.data.decode("utf-8")
    assert "Shipment started from OPP-1470" in body and "Start shipment" not in body
    conn = db.open_db(path)
    sid = conn.execute("SELECT id FROM shipment WHERE enquiry_id = ?", (ids["truck"],)).fetchone()[0]
    assert conn.execute("SELECT mode, carrier FROM shipment WHERE id = ?", (sid,)).fetchone()[:] == ("truck", None)
    conn.close()
    base = "/shipments/{}".format(sid)
    client.post(base + "/milestone", data={"milestone": "Booked", "at": "2015-09-22T16:44", "detail": "rate confirmation signed"})
    client.post(base + "/issue", data={"title": "Pickup at risk", "at": "2015-09-23T08:45", "detail": "truck running late"})
    client.post(base + "/note", data={"title": "ETA requested", "at": "2015-09-23T09:25", "detail": "Paula asks"})
    client.post(base + "/edit", data={"load_no": "L-2086", "planned_pickup": "2015-09-23", "planned_delivery": "2015-09-24",
                                      "price_note": "$1,265"})
    conn = db.open_db(path)
    rows = conn.execute("SELECT kind, title, at, detail FROM shipment_event WHERE shipment_id = ? ORDER BY id",
                        (sid,)).fetchall()
    assert [tuple(r) for r in rows] == [
        ("milestone", "Booked", "2015-09-22 16:44", "rate confirmation signed"),
        ("issue", "Pickup at risk", "2015-09-23 08:45", "truck running late"),
        ("note", "ETA requested", "2015-09-23 09:25", "Paula asks")]
    issue_id = rows[1]
    issue_id = conn.execute("SELECT id FROM shipment_event WHERE kind = 'issue'").fetchone()[0]
    assert conn.execute("SELECT load_no, planned_delivery FROM shipment WHERE id = ?", (sid,)).fetchone()[:] == ("L-2086", "2015-09-24")
    assert conn.execute("SELECT COUNT(*) FROM change_log WHERE table_name = 'shipment_event'").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM change_log WHERE table_name = 'shipment' AND field = 'load_no'").fetchone()[0] == 1
    conn.close()
    # resolve, then void the resolution and the issue comes back
    client.post(base + "/events/{}".format(issue_id), data={"action": "resolve"})
    conn = db.open_db(path)
    res = conn.execute("SELECT id, title, resolves_event_id, kind FROM shipment_event WHERE resolves_event_id IS NOT NULL").fetchone()
    assert (res["title"], res["resolves_event_id"], res["kind"]) == ("Resolved: Pickup at risk", issue_id, "info")
    assert shipments.summary(conn, sid, TODAY)["open_issues"] == []
    conn.close()
    client.post(base + "/events/{}".format(res["id"]), data={"action": "void"})
    conn = db.open_db(path)
    assert [e["title"] for e in shipments.summary(conn, sid, TODAY)["open_issues"]] == ["Pickup at risk"]
    assert conn.execute("SELECT voided FROM shipment_event WHERE id = ?", (res["id"],)).fetchone()[0] == 1
    conn.close()
    client.post(base + "/events/{}".format(res["id"]), data={"action": "unvoid"})
    conn = db.open_db(path)
    assert shipments.summary(conn, sid, TODAY)["open_issues"] == []
    conn.close()
    # a milestone with a new ETA moves the shipment's ETA; close only once delivered
    body = client.post(base + "/close", data={"action": "close"}, follow_redirects=True).data.decode("utf-8")
    assert "once it is delivered" in body
    client.post(base + "/milestone", data={"milestone": "Delivered", "at": "2015-09-24T10:00", "eta": "2015-09-24"})
    conn = db.open_db(path)
    assert conn.execute("SELECT eta FROM shipment WHERE id = ?", (sid,)).fetchone()[0] == "2015-09-24"
    conn.close()
    client.post(base + "/close", data={"action": "close"})
    conn = db.open_db(path)
    assert conn.execute("SELECT closed FROM shipment WHERE id = ?", (sid,)).fetchone()[0] == 1
    conn.close()
    body = client.post(base + "/close", data={"action": "reopen"}, follow_redirects=True).data.decode("utf-8")
    assert "Shipment reopened" in body
    body = client.post(base + "/milestone", data={"milestone": "Arrived", "at": "2015-09-24T10:00"}, follow_redirects=True).data.decode("utf-8")
    assert "Arrived is not a truck milestone" in body


# ----------------------------------------------------------------- R10 ----
def test_r10_flags_issues_etas_and_silence(world):
    conn, repo, path, ids = world
    sid, issue = ocean_with_events(repo, ids["ocean"])                 # issue Sep 19, last event Sep 19, ETA Oct 27
    tid = shipments.start(repo, ids["truck"], "Hazleton, PA to Plainfield, IN")
    shipments.edit(repo, tid, {"planned_delivery": "2015-09-22"})           # ETA gone by, nothing arrived
    shipments.log_milestone(repo, tid, "Booked", "2015-09-22 16:44")
    repo.commit()
    rows = {r[0]: r for r in shipments.attention(conn, "2015-09-23")}
    assert rows[sid][3] == ["issue open 3 working days: Delay"]            # Sat Sep 19 -> Wed Sep 23
    assert rows[tid][3] == ["ETA Sep 22 passed, no arrival logged"]
    rows = {r[0]: r for r in shipments.attention(conn, "2015-09-29")}
    assert rows[sid][3] == ["issue open 7 working days: Delay", "no event for 7 working days"]
    assert rows[sid][4] == 7
    # the rule on the tracking page, snoozable like the others
    items = tracking.rule_shipment_attention(conn, "2015-09-23")
    assert [(i.rule, i.kind, i.ref_id, i.enquiry_id, i.label) for i in items] == [
        ("R10", "shipment", sid, ids["ocean"], "Auburn Hills, MI to Rancagua"),
        ("R10", "shipment", tid, ids["truck"], "Hazleton, PA to Plainfield, IN")]
    assert items[0].extra["shipment_id"] == sid
    sections = {s.rule: s for s in tracking.run(conn, "2015-09-23")}
    assert sections["R10"].title == "Shipment needs attention" and sections["R10"].count == 2
    # the page: the section, the rows linking to the shipments, a snooze
    # like any other rule's (the page reads the real today; the fixture's
    # issue and ETA stay overdue whatever day it is)
    app = create_app(path)
    app.config["TESTING"] = True
    client = app.test_client()
    body = client.get("/tracking").data.decode("utf-8")
    assert 'id="r10"' in body and 'href="/shipments/{}"'.format(sid) in body and "Open shipment" in body
    deepest, ids_ = form_nesting(body)
    assert deepest <= 1, ids_
    body = client.post("/tracking/act", data={"do": "snooze_7", "rule": "R10", "kind": "shipment", "ref_id": sid},
                       follow_redirects=True).data.decode("utf-8")
    assert "R10 snoozed until" in body and 'href="/shipments/{}"'.format(sid) not in body
    sections = {s.rule: s for s in tracking.run(conn, "2015-09-23")}
    assert [i.ref_id for i in sections["R10"].items] == [tid]
    # resolved issue, delivered shipment: nothing to flag
    shipments.resolve(repo, tid, shipments.log_issue(repo, tid, "x", "2015-09-22 10:00"), "2015-09-22 11:00")
    shipments.log_milestone(repo, tid, "Delivered", "2015-09-23 09:00")
    repo.commit()
    assert [r[0] for r in shipments.attention(conn, "2015-09-23")] == [sid]
    conn.close()

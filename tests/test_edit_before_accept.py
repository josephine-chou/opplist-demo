"""Batch E1 (schema v40): edit a suggestion before accepting it.

What Accept writes is decided in one place (suggestions.plan) and the row's
label is made from it; Edit opens the case page's own action fields; the
suggestion keeps what the app suggested and gains what she recorded. Every
company, person, place, date and quoted line here is invented; cases from
OPP-3501.
"""

from __future__ import annotations

import json
import re

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, schema_migrations, shipments, suggestions
from opplist.web import create_app

TODAY = "2026-11-20"


def suggest(repo, case, opp, kind="status", status="Quoted", on="2026-11-12", quote="Thanks for the offer.",
            reason=None, note=None, **extra):
    values = {"enquiry_id": case, "opp_no": opp, "kind": kind, "proposed_status": status, "proposed_date": on,
              "evidence_quote": quote, "evidence_sender": "Ines Calloway <ines@marrowline.example>",
              "evidence_date": on, "confidence": "high", "source": "scan", "state": "pending",
              "closed_reason": reason, "note": note}
    values.update(extra)
    return repo.insert("suggestion", {k: v for k, v in values.items() if v is not None})


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "e1.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Marrowline Cargo", country="Chile")
    ids = {"party": party, "path": path}
    ids["new"] = add_enquiry(repo, "OPP-3501", party, received="2026-11-09", service_mode="Air")
    ids["quoted"] = add_enquiry(repo, "OPP-3502", party, received="2026-11-09", service_mode="Air")
    repo.set_status(ids["quoted"], "Quoted", "2026-11-10")
    ids["won"] = add_enquiry(repo, "OPP-3503", party, received="2026-11-02", service_mode="Air")
    repo.set_status(ids["won"], "Quoted", "2026-11-03")
    repo.set_status(ids["won"], "Won", "2026-11-12")
    repo.commit()
    yield conn, repo, ids
    conn.close()


def get(conn, sid):
    return conn.execute("SELECT * FROM suggestion WHERE id = ?", (sid,)).fetchone()


def events(conn, case):
    return [tuple(r) for r in conn.execute(
        "SELECT kind, status, at, source FROM status_event WHERE enquiry_id = ? AND voided = 0 ORDER BY id", (case,))]


# ------------------------------------------------------------- schema --
def test_schema_v40_adds_the_four_columns(world):
    conn, repo, ids = world
    assert schema_migrations.CURRENT_VERSION >= 40
    cols = {r[1] for r in conn.execute("PRAGMA table_info(suggestion)")}
    assert {"decided_as", "decided_values", "edited", "event_ref"} <= cols


# ------------------------------------------ the label and the write agree --
def test_quoted_on_a_new_case_is_quoted(world):
    conn, repo, ids = world
    sid = suggest(repo, ids["new"], "OPP-3501", quote="Please find our offer below.")
    p = suggestions.plan(conn, get(conn, sid), TODAY)
    assert (p.action, p.label, p.tone) == ("quoted", "Quoted", "s-quoted")
    said = suggestions.accept(repo, sid, today=TODAY)
    assert said == "OPP-3501 set to Quoted on 2026-11-12"
    assert events(conn, ids["new"])[-1] == ("status", "Quoted", "2026-11-12", "mail")
    row = get(conn, sid)
    last = conn.execute("SELECT MAX(id) FROM status_event WHERE enquiry_id = ?", (ids["new"],)).fetchone()[0]
    assert (row["state"], row["decided_as"], row["edited"], row["event_ref"]) == \
        ("accepted", "quoted", 0, "status_event:{}".format(last))
    assert json.loads(row["decided_values"])["date"] == "2026-11-12"


def test_quoted_on_a_quoted_case_is_a_requote_with_its_round(world):
    conn, repo, ids = world
    sid = suggest(repo, ids["quoted"], "OPP-3502", on="2026-11-14", quote="Revised rate attached.")
    p = suggestions.plan(conn, get(conn, sid), TODAY)
    assert (p.action, p.label) == ("requote", "Re-quoted · round 2")
    suggestions.accept(repo, sid, today=TODAY)
    assert events(conn, ids["quoted"])[-1] == ("requote", "Quoted", "2026-11-14", "mail")
    assert conn.execute("SELECT quote_rounds FROM v_enquiry WHERE id = ?", (ids["quoted"],)).fetchone()[0] == 2
    assert get(conn, sid)["decided_as"] == "requote"


def test_won_on_a_quoted_case_and_a_note_suggestion(world):
    conn, repo, ids = world
    won = suggest(repo, ids["quoted"], "OPP-3502", status="Won", on="2026-11-15", quote="Please book it.")
    note = suggest(repo, ids["quoted"], "OPP-3502", kind="note", status=None, on="2026-11-13",
                   note="Shipper moved the pickup to Thursday.")
    assert suggestions.plan(conn, get(conn, won), TODAY).label == "Won"
    assert suggestions.plan(conn, get(conn, note), TODAY).label == "Note"
    assert suggestions.accept(repo, note, today=TODAY) == "OPP-3502: note added on 2026-11-13"
    assert suggestions.accept(repo, won, today=TODAY) == "OPP-3502 set to Won on 2026-11-15"
    assert conn.execute("SELECT status FROM enquiry WHERE id = ?", (ids["quoted"],)).fetchone()[0] == "Won"
    assert [get(conn, s)["decided_as"] for s in (won, note)] == ["won", "note"]


def test_a_closed_case_takes_note_only_and_offers_history_before_the_close(world):
    conn, repo, ids = world
    late = suggest(repo, ids["won"], "OPP-3503", on="2026-11-16", quote="Thanks, noted.")
    early = suggest(repo, ids["won"], "OPP-3503", on="2026-11-08", quote="Here is the corrected offer.")
    assert suggestions.plan(conn, get(conn, late), TODAY).label == "Note"
    assert [o[0] for o in suggestions.options(conn, get(conn, late), TODAY)] == ["cancelled", "note"]
    options = suggestions.options(conn, get(conn, early), TODAY)
    # the case was quoted on Nov 3, before Nov 8: the history is a re-quote
    assert options[0] == ("history_requote", "Re-quoted, history before Won Nov 12", False)
    assert [o for o in options if o[2]] == [("note", "Note only", True)]
    suggestions.accept(repo, late, today=TODAY)
    assert events(conn, ids["won"])[-1][:2] == ("note", "Won")


def test_the_already_quoted_line(world):
    conn, repo, ids = world
    same_day = suggest(repo, ids["quoted"], "OPP-3502", on="2026-11-10", quote="Received with thanks.")
    later = suggest(repo, ids["quoted"], "OPP-3502", on="2026-11-14", quote="New rate below.")
    assert suggestions.plan(conn, get(conn, same_day), TODAY).already == "already quoted Nov 10"
    assert suggestions.plan(conn, get(conn, later), TODAY).already == ""


# ---------------------------------------------------------------- Edit --
@pytest.fixture
def web(world):
    conn, repo, ids = world
    app = create_app(ids["path"])
    app.config["TESTING"] = True
    import opplist.web as web_module
    return app.test_client(), conn, repo, ids


def test_edit_to_note_only_on_a_quoted_case_writes_a_note_and_no_round(web):
    client, conn, repo, ids = web
    sid = suggest(repo, ids["quoted"], "OPP-3502", on="2026-11-10", quote="Received, the quotation is with our buyer.")
    repo.commit()
    page = client.get("/enquiries/{}".format(ids["quoted"])).data.decode("utf-8")
    row = page[page.index('id="sg-{}"'.format(sid)):page.index('id="sg-edit-{}"'.format(sid))]
    assert re.search(r'class="pill s-quoted">Quoted</span> <span class="arrow">&rarr;</span>\s*'
                     r'<span class="pill s-quoted">Re-quoted · round 2</span>', row)
    assert "already quoted Nov 10" in row and ">Edit</button>" in row and 'type="date" name="at"' not in row
    edit = page[page.index('id="sg-edit-{}"'.format(sid)):]
    assert '<option value="requote" selected>Re-quoted · round 2 (suggested)</option>' in edit
    client.post("/suggestions/{}/edit".format(sid), data={"next": "/enquiries/{}".format(ids["quoted"]),
                                                          "record_as": "note", "at": "2026-11-10",
                                                          "note": "Agent acknowledged our quote."})
    assert events(conn, ids["quoted"])[-1] == ("note", "Quoted", "2026-11-10", "mail")
    assert conn.execute("SELECT quote_rounds FROM v_enquiry WHERE id = ?", (ids["quoted"],)).fetchone()[0] == 1
    row = get(conn, sid)
    assert (row["state"], row["decided_as"], row["edited"]) == ("accepted", "note", 1)
    assert json.loads(row["decided_values"])["note"] == "Agent acknowledged our quote."
    assert row["proposed_status"] == "Quoted" and row["evidence_quote"].startswith("Received")   # the app's, untouched
    # the timeline marks the event edited
    page = client.get("/enquiries/{}".format(ids["quoted"])).data.decode("utf-8")
    event_id = row["event_ref"].split(":")[1]
    line = page[page.index('data-unit="event-{}"'.format(event_id)):]
    line = line[:line.index("</li>")]
    assert '<span class="pill muted-pill src">mail</span> <span class="pill tone-amber src edited">edited</span>' in line


def test_edit_to_won_with_rate_used_and_two_routes(web):
    client, conn, repo, ids = web
    b = repo.add_route(ids["quoted"], {"origin_country": "USA", "dest_country": "Chile", "service_mode": "Air"})
    a = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ? AND seq = 1", (ids["quoted"],)).fetchone()[0]
    sid = suggest(repo, ids["quoted"], "OPP-3502", status="Won", on="2026-11-15", quote="Go ahead with route A.")
    repo.commit()
    page = client.get("/enquiries/{}".format(ids["quoted"])).data.decode("utf-8")
    edit = page[page.index('id="sg-edit-{}"'.format(sid)):]
    edit = edit[:edit.index("</form>")]
    assert 'name="routes_present"' in edit and 'name="rate_used" value="own"' in edit
    client.post("/suggestions/{}/edit".format(sid), data={
        "next": "/enquiries/{}".format(ids["quoted"]), "record_as": "won", "at": "2026-11-15",
        "routes_present": "1", "won_route": str(a), "lost_reason_{}".format(b): "Price too high",
        "rate_used": "own", "awarded_note": "", "note": "Route A booked."})
    results = dict(conn.execute("SELECT id, result FROM enquiry_route WHERE enquiry_id = ?", (ids["quoted"],)).fetchall())
    assert results == {a: "Won", b: "Lost"}
    case = conn.execute("SELECT status, rate_used FROM enquiry WHERE id = ?", (ids["quoted"],)).fetchone()
    assert tuple(case) == ("Won", "own")
    row = get(conn, sid)
    values = json.loads(row["decided_values"])
    assert (row["decided_as"], row["edited"], values["rate_used"]) == ("won", 1, "own")
    assert values["routes"] == {"won": [a], "lost": {str(b): "Price too high"}, "cancelled": {}}
    assert row["event_ref"].startswith("status_event:")


def test_lost_without_a_reason_is_refused_and_stays_pending(web):
    client, conn, repo, ids = web
    sid = suggest(repo, ids["quoted"], "OPP-3502", status="Lost", on="2026-11-15", quote="We went with another offer.",
                  reason="Price too high")
    repo.commit()
    before = events(conn, ids["quoted"])
    page = client.post("/suggestions/{}/edit".format(sid), data={
        "next": "/enquiries/{}".format(ids["quoted"]), "record_as": "lost", "at": "2026-11-15", "closed_reason": "",
        "note": "x"}, follow_redirects=True).data.decode("utf-8")
    assert "Lost needs a reason." in page
    assert get(conn, sid)["state"] == "pending" and events(conn, ids["quoted"]) == before


def test_a_closing_dated_before_the_latest_event_is_refused(web):
    client, conn, repo, ids = web
    sid = suggest(repo, ids["quoted"], "OPP-3502", status="Cancelled", on="2026-11-15", quote="Order put on ice.",
                  reason="Order not confirmed")
    repo.commit()
    page = client.post("/suggestions/{}/edit".format(sid), data={
        "next": "/enquiries/{}".format(ids["quoted"]), "record_as": "cancelled", "at": "2026-11-05",
        "closed_reason": "Order not confirmed", "note": ""}, follow_redirects=True).data.decode("utf-8")
    assert "is before this case&#39;s latest event" in page or "is before this case's latest event" in page
    assert get(conn, sid)["state"] == "pending"


def test_dismiss_and_put_back(web):
    client, conn, repo, ids = web
    sid = suggest(repo, ids["new"], "OPP-3501", quote="Thanks for the offer.")
    repo.commit()
    client.post("/suggestions/{}/dismiss".format(sid), data={"next": "/enquiries/{}".format(ids["new"])})
    row = get(conn, sid)
    assert (row["state"], row["decided_as"]) == ("dismissed", "dismissed")
    page = client.get("/enquiries/{}".format(ids["new"])).data.decode("utf-8")
    block = page[page.index('id="suggested"'):]
    assert "<summary>show 1 dismissed</summary>" in block and "Suggested updates" not in block[:200]
    assert "&ldquo;Thanks for the offer.&rdquo;" in block and ">Put back</button>" in block
    client.post("/suggestions/{}/put-back".format(sid), data={"next": "/enquiries/{}".format(ids["new"])})
    row = get(conn, sid)
    assert (row["state"], row["decided_as"], row["decided_values"], row["event_ref"]) == ("pending", None, None, None)
    logged = conn.execute("SELECT old_value FROM change_log WHERE table_name = 'suggestion' AND row_id = ? "
                          "AND field = 'decided_as' ORDER BY id DESC LIMIT 1", (sid,)).fetchone()[0]
    assert logged == "dismissed"


# ----------------------------------------------------------- shipments --
@pytest.fixture
def shipped(world):
    conn, repo, ids = world
    db.set_setting(conn, "shipments", "1")             # FX3: switched on for the shipment page
    ship = shipments.start(repo, ids["won"], "Air · Brindle Field to Port Alder")
    repo.commit()
    app = create_app(ids["path"])
    app.config["TESTING"] = True
    return app.test_client(), conn, repo, ids, ship


def test_a_shipment_milestone_edited_to_another_milestone_and_time(shipped):
    client, conn, repo, ids, ship = shipped
    sid = suggest(repo, ids["won"], "OPP-3503", kind="shipment_milestone", status="Departed", on="2026-11-14 09:30",
                  quote="Flight left this morning.", shipment_id=ship)
    repo.commit()
    page = client.get("/shipments/{}".format(ship)).data.decode("utf-8")
    assert 'id="sg-edit-{}"'.format(sid) in page and '<option selected>Departed</option>' in page
    client.post("/suggestions/{}/edit".format(sid), data={"next": "/shipments/{}".format(ship), "milestone": "Picked up",
                                                          "date": "2026-11-13", "time": "16:10", "note": "Cargo collected."})
    event = conn.execute("SELECT id, milestone, at, source FROM shipment_event WHERE shipment_id = ? ORDER BY id DESC",
                         (ship,)).fetchone()
    assert tuple(event)[1:] == ("Picked up", "2026-11-13 16:10", "mail")
    row = get(conn, sid)
    assert (row["decided_as"], row["edited"], row["event_ref"]) == ("milestone", 1, "shipment_event:{}".format(event["id"]))
    page = client.get("/shipments/{}".format(ship)).data.decode("utf-8")
    assert '<span class="pill tone-amber src edited">edited</span>' in page


def test_start_shipment_saved_as_planned(world):
    conn, repo, ids = world
    sid = suggest(repo, ids["won"], "OPP-3503", kind="shipment_milestone", status="Departed", on="2026-11-14 08:00",
                  quote="Booked on the 14th.")
    repo.commit()
    item = suggestions.view(conn, [get(conn, sid)], TODAY)[0]
    assert "Invoiced" not in item["milestones"] and item["prefill"]["planned"] is False
    said = suggestions.decide_shipment(repo, get(conn, sid), {"milestone": "Picked up", "at": "2026-11-16 10:00",
                                                              "note": "", "planned": True})
    assert said.startswith("OPP-3503: shipment started (Picked up, planned - no milestone logged)")
    ship = conn.execute("SELECT id FROM shipment WHERE enquiry_id = ?", (ids["won"],)).fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM shipment_event WHERE shipment_id = ?", (ship,)).fetchone()[0] == 0
    row = get(conn, sid)
    assert (row["decided_as"], row["edited"], row["event_ref"]) == ("start", 1, "shipment:{}".format(ship))
    assert json.loads(row["decided_values"])["planned"] is True

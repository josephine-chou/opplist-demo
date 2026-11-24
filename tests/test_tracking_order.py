"""Batch T1 (2026-10-02): the order of Today's work, and no Suggested
updates card on Tracking.

The cards run FU1 due, New nothing done yet, FU2 due, then Replied,
Decide, A vendor answered, Shipment needs attention, Vendor has not
replied, Won whose rate?, Requester still (TBC); the tiles follow the same
order. A pending suggestion is worked on its case page (or its shipment
page) and is not on Tracking. Everything else about the page is as it was.

These replace older Tracking tests that pinned the old order or the card
(they were rewritten here with the
same checks rather than edited). Every company, person, place, number and
date here is invented; cases from OPP-3151.

FX4 (2026-11-12): the Replied card is Waiting on us and its question is
logged by hand; r9, decide, r2 and r6 are hidden by default, so this world
shows every card (Settings, tracking_hidden_cards empty) to keep checking
them all; test_fx4 checks the default."""

from __future__ import annotations

import datetime as dt
import re

import pytest

from conftest import add_enquiry, add_party, form_nesting, seed_reference
from opplist import db, shipments, suggestions, tracking
from opplist import web as web_module
from opplist.web import create_app

TODAY = "2026-11-19"          # a Thursday, as the page's working-day sums expect
CARD_ORDER = ["fu1", "r1", "fu2", "replied", "decide", "r9", "r10", "r2", "r6", "r5"]
TILE_ORDER = ["fu1", "r1", "fu2", "replied", "decide", "r9", "r10"]


def ago(days):
    return (dt.date.fromisoformat(TODAY) - dt.timedelta(days=days)).isoformat()


def card(body, key):
    """One card of the page, from its id to the next card (or the backlog)."""
    start = body.index('id="{}"'.format(key))
    nxt = re.compile(r'class="card group|id="backlog"').search(body, start + 1)
    return body[start:nxt.start() if nxt else len(body)]


def ids_in_order(body, keys):
    found = [(body.index('id="{}"'.format(k)), k) for k in keys if 'id="{}"'.format(k) in body]
    return [k for _, k in sorted(found)]


# ---------------------------------------------------- one row per rule --
@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "trk-order.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "shipments", "1")   # FX3: R10 and the shipment pages on
    db.set_setting(conn, tracking.HIDDEN_SETTING, "")      # FX4: every card shown
    wren = add_party(repo, "Wrenfield Cargo", country="Chile")                 # LATAM agent
    harlan = add_party(repo, "Harlan Freight", country="USA")
    office = add_party(repo, "JoJo ORF", ptype="Office", country="USA")
    pellmar = add_party(repo, "Pellmar Lines", ptype="Unknown", country="USA")
    tbc = add_party(repo, "(TBC)", ptype="Unknown", country=None, placeholder=1)
    db.set_setting(conn, "closed_reasons_lost", ["Price too high", "No response from customer", "Other"])
    ids = {}
    # R3 Replied: quoted, then a question logged by hand (FX4: Waiting on us), 3 days ago
    c = add_enquiry(repo, "OPP-3151", wren, received=ago(20), service_mode="FCL", origin_country="USA",
                    origin_loc="LAX", dest_country="Chile", dest_loc="SAI", commodity="Valve bodies")
    repo.set_status(c, "Quoted", ago(10))
    repo.add_note(c, ago(3), "Reply from Ines (needs an answer): could you re-run it for 3 x 20' please "
                             "https://p.example/r [mid:<c@d>]", source="manual")
    ids["replied"] = c
    # R3 FU1 due: quoted 4 working days ago, nothing sent
    c = add_enquiry(repo, "OPP-3152", harlan, received=ago(12), service_mode="Air", origin_country="USA",
                    origin_loc="MEM", dest_country="China", dest_loc="NGB")
    repo.set_status(c, "Quoted", ago(6))
    ids["fu1"] = c
    # R3 FU2 due: FU1 sent 9 days ago
    c = add_enquiry(repo, "OPP-3153", office, received=ago(30), service_mode="LCL", origin_country="USA",
                    origin_loc="LAX", dest_country="Chile", dest_loc="SAI")
    repo.set_status(c, "Quoted", ago(20))
    repo.add_followup(c, ago(9))
    repo.add_note(c, ago(9), "Called the ORF desk: still comparing")      # L2: an answer to FU1
    ids["fu2"] = c
    # R3 Decide, late: quoted 40 days ago, two follow-ups long ago
    c = add_enquiry(repo, "OPP-3154", wren, received=ago(45), service_mode="FCL", origin_country="USA",
                    origin_loc="LAX", dest_country="Chile", dest_loc="SAI")
    repo.set_status(c, "Quoted", ago(40))
    repo.add_followup(c, ago(38))
    repo.add_followup(c, ago(30))
    ids["decide"] = c
    # R1: New, untouched for 10 working days, late
    ids["r1"] = add_enquiry(repo, "OPP-3155", harlan, received=ago(14), service_mode="Air", origin_country="USA",
                            origin_loc="LAX", dest_country="Chile", dest_loc="SAI")
    # R9: a vendor replied after the last event
    c = add_enquiry(repo, "OPP-3156", wren, received=ago(8), service_mode="Air", origin_country="USA",
                    origin_loc="LAX", dest_country="Chile", dest_loc="SAI")
    repo.set_status(c, "New", ago(7))
    repo.insert("vendor_request", {"enquiry_id": c, "vendor_party_id": pellmar, "asked_on": ago(6), "replied_on": ago(2)})
    ids["r9"] = c
    # R2: a vendor silent for 5 working days
    c = add_enquiry(repo, "OPP-3157", harlan, received=ago(10), service_mode="FCL", origin_country="USA",
                    origin_loc="MEM", dest_country="China", dest_loc="NGB")
    repo.set_status(c, "New", ago(9))
    repo.insert("vendor_request", {"enquiry_id": c, "vendor_party_id": pellmar, "asked_on": ago(8)})
    ids["r2"] = c
    # R6: won with a vendor asked, rate_used empty
    c = add_enquiry(repo, "OPP-3158", wren, received=ago(20), service_mode="Air", origin_country="USA",
                    origin_loc="LAX", dest_country="Chile", dest_loc="SAI")
    repo.insert("vendor_request", {"enquiry_id": c, "vendor_party_id": pellmar, "asked_on": ago(15),
                                   "machine_status": "Quoted"})
    repo.set_status(c, "Quoted", ago(14))
    repo.set_status(c, "Won", ago(5))
    ids["r6"] = c
    # R5: parked on (TBC), a vendor asked today so not R1
    c = add_enquiry(repo, "OPP-3159", tbc, received=ago(3), service_mode="Air", origin_country="USA",
                    origin_loc="LAX", dest_country="Chile", dest_loc="SAI")
    repo.insert("vendor_request", {"enquiry_id": c, "vendor_party_id": pellmar, "asked_on": TODAY})
    ids["r5"] = c
    # R4: unconfirmed status
    ids["r4"] = add_enquiry(repo, "OPP-3160", harlan, received=ago(25), status_confirmed=0, service_mode="Air",
                            origin_country="USA", origin_loc="MEM", dest_country="China", dest_loc="NGB")
    # R8: a country with no location, a vendor asked today so not R1
    c = add_enquiry(repo, "OPP-3161", harlan, received=ago(2), service_mode="FCL", origin_country="USA",
                    origin_loc="LAX", dest_country="Chile")
    repo.insert("vendor_request", {"enquiry_id": c, "vendor_party_id": pellmar, "asked_on": TODAY})
    ids["r8"] = c
    # R7: a party quiet for longer than its cadence
    quiet = add_party(repo, "Stillwater Trading", country="Chile", stage="active", cadence_days=30)
    repo.insert("party_event", {"party_id": quiet, "type": "outreach", "at": ago(90), "summary": "hello"})
    ids["quiet"] = quiet
    # R10: an active shipment with an ETA gone by
    c = add_enquiry(repo, "OPP-3162", wren, received=ago(30), service_mode="Air", origin_country="USA",
                    origin_loc="LAX", dest_country="Chile", dest_loc="SAI")
    repo.set_status(c, "Quoted", ago(25))
    repo.set_status(c, "Won", ago(20), rate_used="own")
    sid = shipments.start(repo, c, "Los Angeles to Santiago")
    shipments.edit(repo, sid, {"planned_arrival": ago(3)})
    shipments.log_milestone(repo, sid, "Departed", ago(6) + " 08:00")
    ids["sid"] = sid
    # a pending suggestion on the Replied case: worked on the case page, not on Tracking
    ids["suggestion"] = repo.insert("suggestion", {
        "enquiry_id": ids["replied"], "opp_no": "OPP-3151", "kind": "status", "proposed_status": "Won",
        "proposed_date": ago(1), "confidence": "high", "source": "mail", "evidence_date": ago(1),
        "evidence_sender": "Ines", "evidence_quote": "ready to go ahead, please book", "state": "pending"})
    repo.commit()
    monkeypatch.setattr(web_module, "_today", lambda: TODAY)
    return conn, path, ids


def _page(path):
    app = create_app(path)
    app.config["TESTING"] = True
    client = app.test_client()
    return client, client.get("/tracking").data.decode("utf-8")


def test_the_cards_and_the_tiles_run_in_the_morning_order(world):
    conn, path, ids = world
    m = tracking.page_model(conn, TODAY)
    assert [c["key"] for c in m["today_cards"]] == CARD_ORDER
    assert [t["key"] for t in m["tiles"]] == TILE_ORDER
    assert [c["key"] for c in m["backlog_cards"]] == ["r4", "r8", "r7"]
    assert "suggestions" not in m
    conn.close()
    client, body = _page(path)
    page_cards = body[body.index('id="today"'):]
    assert ids_in_order(page_cards, CARD_ORDER) == CARD_ORDER
    tiles = re.findall(r'data-tile="([a-z0-9]+)"', body[:body.index('id="today"')])
    assert tiles == TILE_ORDER
    # each tile still jumps to its card
    for key in TILE_ORDER:
        assert 'href="#{}"'.format(key) in body, key


def test_a_pending_suggestion_is_on_its_case_page_and_not_on_tracking(world):
    conn, path, ids = world
    conn.close()
    client, body = _page(path)
    assert 'id="suggested"' not in body and "ready to go ahead, please book" not in body
    assert "Suggested updates" not in body
    case = client.get("/enquiries/{}".format(ids["replied"])).data.decode("utf-8")
    assert "ready to go ahead, please book" in case
    assert 'action="/suggestions/{}/record"'.format(ids["suggestion"]) in case      # SG1: one button per way
    assert 'action="/suggestions/{}/dismiss"'.format(ids["suggestion"]) in case
    # dismissed from the case page: gone, the case untouched
    client.post("/suggestions/{}/dismiss".format(ids["suggestion"]),
                data={"next": "/enquiries/{}".format(ids["replied"])})
    conn = db.open_db(path)
    assert conn.execute("SELECT state FROM suggestion WHERE id = ?", (ids["suggestion"],)).fetchone()[0] == "dismissed"
    assert conn.execute("SELECT status FROM enquiry WHERE id = ?", (ids["replied"],)).fetchone()[0] == "Quoted"
    conn.close()


def test_page_model_splits_r3_and_counts_match_the_tiles(world):
    conn, path, ids = world
    m = tracking.page_model(conn, TODAY)
    cards = {c["key"]: c for c in m["today_cards"]}
    counts = {c["key"]: c["count"] for c in m["today_cards"] + m["backlog_cards"]}
    assert counts == {"replied": 1, "fu1": 1, "fu2": 1, "decide": 1, "r1": 1, "r9": 1, "r10": 1, "r2": 1,
                      "r6": 1, "r5": 1, "r4": 1, "r8": 1, "r7": 1}
    assert {t["key"]: t["count"] for t in m["tiles"]} == {k: 1 for k in TILE_ORDER}
    assert m["today_total"] == 10 and m["backlog_total"] == 3
    replied = cards["replied"]["rows"][0]
    assert (replied["opp"], replied["requester"], replied["latam"], replied["pill"], replied["pill_cls"]) == \
        ("OPP-3151", "Wrenfield Cargo", True, "Waiting on us", "p-rose")
    assert replied["lane"] == "LAX - SAI · FCL · Valve bodies"
    assert replied["quote"] == "Ines: could you re-run it for 3 x 20' please" and replied["last"].startswith("Reply ")
    fu2 = cards["fu2"]["rows"][0]
    assert (fu2["pill"], fu2["sub"], fu2["office"], fu2["ocean"]) == ("FU2 due", "1 sent", True, True)
    assert fu2["last"].startswith("Note ")                  # L2: the answer to FU1 is the last activity
    decide = cards["decide"]["rows"][0]
    assert decide["sub"] == "2 sent" and decide["late"] is True
    r2 = cards["r2"]["rows"][0]
    assert r2["sub"] == "Pellmar Lines · no reply" and r2["last"].startswith("asked Pellmar Lines ")
    r9 = cards["r9"]["rows"][0]
    assert r9["sub"].startswith("vendor replied ") and r9["pill"] == "New"
    r7 = m["backlog_cards"][2]["rows"][0]
    assert (r7["label"], r7["pill"], r7["sub"], r7["party_id"], r7["latam"]) == \
        ("Stillwater Trading", "quiet", "cadence 30 d", ids["quiet"], True)
    assert m["backlog_cards"][0]["rows"][0]["sub"] == "unconfirmed"


def test_every_rule_renders_its_actions_with_the_existing_routes(world):
    conn, path, ids = world
    conn.close()
    client, body = _page(path)
    assert '<main class="tracking-page">' in body
    assert body.count('action="/tracking/act"') == 13                    # one form per row
    assert re.search(r'<div class="t-n" data-tile="replied">1</div>', body)
    assert 'data-tile="r10">1</div>' in body and ">Shipments<" in body
    assert 'data-total="today">10</b>' in body and 'data-total="backlog">3</b>' in body
    replied = card(body, "replied")
    for value in ("quick:requote", "quick:Won", "quick:Lost", "snooze_7"):
        assert 'value="{}"'.format(value) in replied, value
    assert "Re-quote" in replied and 'class="pill p-rose">Waiting on us' in replied \
        and "&ldquo;Ines: could you re-run" in replied
    fu1 = card(body, "fu1")
    # FX4: no Draft FU1 link (no draft any more), Log as sent stays
    assert "Draft FU1</a>" not in fu1 and 'value="followup">Log as sent' in fu1
    decide = card(body, "decide")
    assert 'class="decide-pick"' in decide and 'id="decide-R3"' in decide and 'action="/tracking/decide-lost"' in decide
    assert "select the 1 shown" in decide and "still alive: Wait 7d on the row instead" in decide
    assert 'class="days late"' in decide
    r1 = card(body, "r1")
    assert 'value="quick:Quoted"' in r1 and '/enquiries/{}#vendors">Ask vendor'.format(ids["r1"]) in r1 \
        and 'value="quick:Dropped"' in r1
    r9 = card(body, "r9")
    assert 'name="status"' in r9 and 'name="do" value="status"' in r9 and "vendor replied" in r9
    assert 'href="/shipments/{}">Open shipment'.format(ids["sid"]) in card(body, "r10")
    r2 = card(body, "r2")
    assert 'name="quoted"' in r2 and 'value="vendor_replied">Log reply' in r2 and r2.startswith('id="r2" data-rule="R2">')
    assert '/enquiries/{}#won">Record rate used'.format(ids["r6"]) in card(body, "r6")
    assert '/enquiries/{}#request">Open case'.format(ids["r5"]) in card(body, "r5")
    backlog = body[body.index('id="backlog"'):]
    assert "Backlog" in backlog and "3</span> rows that were here" in backlog
    assert 'name="do" value="status"' in backlog[backlog.index('id="r4"'):backlog.index('id="r8"')]
    assert '/enquiries/{}#route">Fix the lane'.format(ids["r8"]) in backlog
    r7 = backlog[backlog.index('id="r7"'):]
    assert "<th>Agent</th><th>State</th><th>Quiet for</th>" in r7 and 'value="touch">Log touch' in r7
    assert 'href="/parties/{}"><b>Stillwater Trading</b>'.format(ids["quiet"]) in r7 and "cadence 30 d" in r7
    assert body.count(">More &#9662;<") >= 13 and 'value="snooze_14"' in body and 'value="snooze_until"' in body
    assert 'action="/enquiries/{}/duplicate"'.format(ids["fu1"]) in body \
        and 'action="/enquiries/{}/scope"'.format(ids["fu1"]) in body
    assert 'id="suggested"' not in body                                   # T1: no Suggested updates card
    deepest, ids_ = form_nesting(body)
    assert deepest <= 1, ids_
    # the nav badge is unchanged: R1 + R3, counted as ever (on the real date)
    conn = db.open_db(path)
    badge = tracking.focus_count(conn)
    conn.close()
    assert '<span class="badge" title="R1 + R3">{}</span>'.format(badge) in body or not badge


def test_the_decide_batch_posts_only_ticked_ids_and_empty_groups_say_nothing(world):
    conn, path, ids = world
    conn.close()
    client, body = _page(path)
    page = client.post("/tracking/decide-lost", data={"ids": [str(ids["decide"])], "at": TODAY}, follow_redirects=True)
    assert b"Marked Lost" in page.data
    conn = db.open_db(path)
    assert conn.execute("SELECT status FROM enquiry WHERE id = ?", (ids["decide"],)).fetchone()[0] == "Lost"
    assert conn.execute("SELECT status FROM enquiry WHERE id = ?", (ids["fu1"],)).fetchone()[0] == "Quoted"
    conn.close()
    body = client.get("/tracking").data.decode("utf-8")
    decide = card(body, "decide")
    assert "Nothing here." in decide and 'class="decide-pick"' not in decide
    assert 'data-tile="decide">0</div>' in body


def test_show_all_marks_rows_past_ten_as_extra(world):
    conn, path, ids = world
    repo = db.Repository(conn, source="test")
    harlan = conn.execute("SELECT id FROM party WHERE key = 'Harlan Freight'").fetchone()[0]
    for n in range(13):
        c = add_enquiry(repo, "OPP-35{:02d}".format(n), harlan, received=ago(60), service_mode="Air",
                        origin_country="USA", origin_loc="MEM", dest_country="China", dest_loc="NGB")
        repo.set_status(c, "Quoted", ago(20 + n))
    repo.commit()
    conn.close()
    client, body = _page(path)
    decide = card(body, "decide")
    assert decide.count('class="decide-pick"') == 14
    assert decide.count(' extra" hidden') == 4 and 'data-show-all="decide">Show all 14</a>' in decide \
        and "(10 of 14)" in decide
    assert "select the 10 shown" in decide


def test_a_status_suggestion_on_a_case_another_office_handles_still_applies(world):
    """Replaces the old Tracking row check (it showed the steel 'Handled by'
    label on the suggestion row): the case page carries that label, the
    suggestion sits under it, and Accept still writes the status."""
    conn, path, ids = world
    repo = db.Repository(conn, source="test")
    repo.update("enquiry", ids["fu1"], {"handled_by": "JJV"})
    sid = suggestions.record(repo, ids["fu1"], "OPP-3152", {
        "kind": "status", "proposed_status": "Won", "proposed_date": ago(1),
        "evidence_sender": "Tomas Vell <tomas@harlanfreight.example>", "evidence_date": ago(1),
        "evidence_quote": "Go ahead with the Memphis pickup.", "confidence": "high", "source": "mail"})
    repo.commit()
    conn.close()
    client, body = _page(path)
    assert "Go ahead with the Memphis pickup." not in body
    case = client.get("/enquiries/{}".format(ids["fu1"])).data.decode("utf-8")
    status = case[case.index('id="status"'):]
    assert re.search(r'class="pill big tone-steel handled">Handled by JJV</span>', status)
    assert "Go ahead with the Memphis pickup." in status
    sid = sid or db.open_db(path).execute("SELECT id FROM suggestion WHERE opp_no = 'OPP-3152'").fetchone()[0]
    client.post("/suggestions/{}/accept".format(sid), data={"next": "/enquiries/{}".format(ids["fu1"]), "at": ago(1)})
    conn = db.open_db(path)
    assert conn.execute("SELECT status, handled_by FROM enquiry WHERE id = ?", (ids["fu1"],)).fetchone()[:] == ("Won", "JJV")
    conn.close()


# ------------------------------------------- a small book, by real today --
def _days_ago(n):
    return (dt.date.today() - dt.timedelta(days=n)).isoformat()


@pytest.fixture
def web(tmp_path):
    path = tmp_path / "trk-web.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "shipments", "1")   # FX3: R10 and the shipment pages on
    db.set_setting(conn, tracking.HIDDEN_SETTING, "")      # FX4: every card shown
    party_id = add_party(repo, "Wrenfield Cargo", country="Chile")
    add_party(repo, "(TBC)", ptype="Unknown", country=None, placeholder=1)
    db.set_setting(conn, "closed_reasons_lost", ["Price too high", "No response from customer", "Other"])
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, party_id


def _stale(path, party_id, opp_no="OPP-3171", status="New", received=None):
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    case = add_enquiry(repo, opp_no, party_id, status=status, received=received or _days_ago(60))
    repo.commit()
    conn.close()
    return case


def _many_quoted(path, party_id, count):
    """`count` Decide rows, quoted one day apart so the sort is unambiguous."""
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    for n in range(count):
        case = add_enquiry(repo, "OPP-32{:02d}".format(n), party_id, status="Quoted", received=_days_ago(130))
        repo.insert("status_event", {"enquiry_id": case, "status": "Quoted", "at": _days_ago(100 - n)})
    repo.commit()
    conn.close()


def _r3_world(path, party_id, replied=1, fu1=1, fu2=1, decide=1):
    """Cases sitting on each step of the ladder, dated from today."""
    from opplist import detect_followups

    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    made = {}
    for n in range(replied):
        case = add_enquiry(repo, "OPP-41{:02d}".format(n), party_id, received=_days_ago(60))
        repo.set_status(case, "Quoted", _days_ago(30))
        repo.add_note(case, _days_ago(4), detect_followups.reply_note(
            "Ines", "Could you sharpen the pencil on this one?", "<r{}@x>".format(n)), source="manual")   # FX4
        made.setdefault("Replied", []).append("OPP-41{:02d}".format(n))
    for n in range(fu1):
        case = add_enquiry(repo, "OPP-42{:02d}".format(n), party_id, received=_days_ago(20))
        repo.set_status(case, "Quoted", _days_ago(5))
        made.setdefault("FU1", []).append("OPP-42{:02d}".format(n))
    for n in range(fu2):
        case = add_enquiry(repo, "OPP-43{:02d}".format(n), party_id, received=_days_ago(40))
        repo.set_status(case, "Quoted", _days_ago(25))
        repo.add_followup(case, _days_ago(12))
        repo.add_note(case, _days_ago(12), "WhatsApp: still with the client")   # L2: an answer to FU1
        made.setdefault("FU2", []).append("OPP-43{:02d}".format(n))
    for n in range(decide):
        case = add_enquiry(repo, "OPP-44{:02d}".format(n), party_id, received=_days_ago(90))
        repo.set_status(case, "Quoted", _days_ago(60))
        made.setdefault("Decide", []).append("OPP-44{:02d}".format(n))
    repo.commit()
    conn.close()
    return made


def test_the_focus_panels_say_nothing_here_when_empty(web):
    client, path, party_id = web
    body = client.get("/tracking").data.decode("utf-8")
    for key in ("fu1", "r1", "fu2", "replied", "decide", "r9"):
        assert 'class="card group tracking-section focus" id="{}"'.format(key) in body, key
    assert body.count("Nothing here.") >= 6
    assert "Nothing to chase today." in body
    for key in ("r2", "r6", "r5", "r4", "r8", "r7"):
        assert 'id="{}"'.format(key) not in body
    order = ("fu1", "r1", "fu2", "replied", "decide", "r9", "backlog")
    positions = [body.index('id="{}"'.format(key)) for key in order]
    assert positions == sorted(positions)


def test_quick_status_writes_the_status_and_confirms_it(web):
    client, path, party_id = web
    case = _stale(path, party_id)
    response = client.post("/tracking/act", data={
        "do": "quick:Quoted", "rule": "R1", "kind": "enquiry", "ref_id": case,
        "enquiry_id": case, "at": _days_ago(1)}, follow_redirects=True)
    assert response.status_code == 200 and b"set to Quoted" in response.data
    conn = db.open_db(path)
    row = conn.execute("SELECT status, status_confirmed FROM enquiry WHERE id = ?", (case,)).fetchone()
    assert (row["status"], row["status_confirmed"]) == ("Quoted", 1)
    event = conn.execute("SELECT status, at, source FROM status_event WHERE enquiry_id = ? "
                         "ORDER BY id DESC LIMIT 1", (case,)).fetchone()
    assert (event["status"], event["at"], event["source"]) == ("Quoted", _days_ago(1), "manual")
    conn.close()
    body = client.get("/tracking").data.decode("utf-8")
    assert "OPP-3171" not in card(body, "r1")                    # dealt with, gone from R1


def test_a_focus_panel_shows_ten_rows_and_a_show_all_link(web):
    client, path, party_id = web
    _many_quoted(path, party_id, 12)
    _stale(path, party_id)                                        # one R1 row
    body = client.get("/tracking").data.decode("utf-8")
    decide = card(body, "decide")
    assert decide.count('value="quick:Won"') == 12
    assert decide.count(' extra" hidden') == 2
    assert 'data-show-all="decide">Show all 12</a>' in decide and "(10 of 12)" in decide
    assert 'data-tile="decide">12</div>' in body and 'data-count="decide">12</span>' in body
    first = decide.index("OPP-32")
    assert decide[first:first + 8] == "OPP-3200"                 # the oldest quote leads
    hidden = decide[decide.index(' extra" hidden'):]
    assert "OPP-3211" in hidden and "OPP-3210" in hidden          # the two newest are the hidden ones
    assert "Show all" not in card(body, "r1")


def test_show_all_opens_one_focus_panel_only(web):
    client, path, party_id = web
    _many_quoted(path, party_id, 12)
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    for n in range(11):
        add_enquiry(repo, "OPP-33{:02d}".format(n), party_id, received=_days_ago(60 + n))
    repo.commit()
    conn.close()
    body = client.get("/tracking").data.decode("utf-8")
    decide, r1 = card(body, "decide"), card(body, "r1")
    assert decide.count('value="quick:Won"') == 12 and decide.count(' extra" hidden') == 2
    assert r1.count('value="quick:Quoted"') == 11 and r1.count(' extra" hidden') == 1
    assert 'data-show-all="r1">Show all 11</a>' in r1
    assert client.get("/tracking?all=R4").data.decode("utf-8").count('value="quick:Won"') == 12


def test_r3_groups_the_rows_by_step_and_the_page_shows_them_in_the_morning_order(web):
    client, path, party_id = web
    made = _r3_world(path, party_id)
    conn = db.open_db(path)
    section = {s.rule: s for s in tracking.run(conn)}["R3"]
    conn.close()
    groups = tracking.r3_groups(section)                          # the old grouping itself is unchanged
    assert [g["step"] for g in groups] == ["Replied", "FU1", "FU2", "Decide"]
    assert [[i.label for i in g["rows"]] for g in groups] == \
        [made["Replied"], made["FU1"], made["FU2"], made["Decide"]]
    assert "items" not in groups[0]
    body = client.get("/tracking").data.decode("utf-8")
    cards = body[body.index('id="today"'):]                       # the tiles above say 'FU1 due' too
    assert cards.index("FU1 due") < cards.index("New, nothing done yet") < cards.index("FU2 due") \
        < cards.index("Waiting on us") < cards.index("Decide, no answer")             # FX4: the card's name
    assert 'data-total="today">4</b>' in body


def test_each_group_shows_ten_and_has_its_own_show_all(web):
    client, path, party_id = web
    _r3_world(path, party_id, replied=11, fu1=1, fu2=0, decide=12)
    body = client.get("/tracking").data.decode("utf-8")
    replied, decide = card(body, "replied"), card(body, "decide")
    assert replied.count('value="quick:requote"') == 11
    assert replied.count(' extra" hidden') == 1 and 'data-show-all="replied">Show all 11</a>' in replied
    assert decide.count('class="decide-pick"') == 12
    assert decide.count(' extra" hidden') == 2 and 'data-show-all="decide">Show all 12</a>' in decide
    assert "(10 of 12)" in decide
    assert "Show all" not in card(body, "fu1")


def test_the_batch_lost_button_sits_under_the_decide_group(web):
    client, path, party_id = web
    made = _r3_world(path, party_id, replied=1, fu1=1, fu2=0, decide=2)
    body = client.get("/tracking").data.decode("utf-8")
    decide = card(body, "decide")
    assert decide.index("Decide, no answer") < decide.index('id="decide-R3"')
    assert "select the 2 shown" in decide and "still alive: Wait 7d on the row instead" in decide
    conn = db.open_db(path)
    ids = [r[0] for r in conn.execute("SELECT id FROM enquiry WHERE opp_no IN ({})".format(
        ",".join("'{}'".format(o) for o in made["Decide"])))]
    conn.close()
    page = client.post("/tracking/decide-lost", data={"ids": [str(i) for i in ids]}, follow_redirects=True)
    assert b"Marked Lost" in page.data
    conn = db.open_db(path)
    assert conn.execute("SELECT COUNT(*) FROM enquiry WHERE status = 'Lost'").fetchone()[0] == 2
    conn.close()


def test_nothing_here_only_when_every_group_is_empty(web):
    client, path, party_id = web
    body = client.get("/tracking").data.decode("utf-8")
    assert "Nothing here." in card(body, "fu1")
    _r3_world(path, party_id, replied=0, fu1=1, fu2=0, decide=0)
    body = client.get("/tracking").data.decode("utf-8")
    fu1, replied = card(body, "fu1"), card(body, "replied")
    assert "Nothing here." not in fu1 and "FU1 due" in fu1
    assert "Nothing here." in replied


# ------------------------------------- shipment suggestions stay put --
def test_shipment_suggestions_are_on_their_pages_and_not_on_tracking(world):
    conn, path, ids = world
    repo = db.Repository(conn, source="test")
    won = conn.execute("SELECT enquiry_id FROM shipment WHERE id = ?", (ids["sid"],)).fetchone()[0]
    issue = repo.insert("suggestion", {
        "enquiry_id": won, "opp_no": "OPP-3162", "shipment_id": ids["sid"], "kind": "shipment_issue",
        "proposed_status": "ETA moved to Nov 28", "proposed_date": ago(1) + " 10:05", "proposed_eta": "2026-11-28",
        "confidence": "high", "source": "mail", "evidence_sender": "Pellmar Lines", "state": "pending"})
    start_case = add_enquiry(repo, "OPP-3163", conn.execute("SELECT id FROM party WHERE key = 'Wrenfield Cargo'")
                             .fetchone()[0], received=ago(12), service_mode="Air", origin_country="USA",
                             origin_loc="LAX", dest_country="Chile", dest_loc="SAI")
    repo.set_status(start_case, "Won", ago(3), rate_used="own")
    start = repo.insert("suggestion", {
        "enquiry_id": start_case, "opp_no": "OPP-3163", "kind": "shipment_milestone", "proposed_status": "Booked",
        "proposed_date": ago(2) + " 09:30", "confidence": "high", "source": "mail",
        "evidence_sender": "Pellmar Lines", "state": "pending"})
    repo.commit()
    conn.close()
    client, body = _page(path)
    assert "ETA moved to Nov 28" not in body and "Start shipment · Booked" not in body
    ship_page = client.get("/shipments/{}".format(ids["sid"])).data.decode("utf-8")
    events = ship_page[ship_page.index('id="events"'):ship_page.index('id="mail"')]
    assert 'id="suggested"' in events and "ETA moved to Nov 28" in events
    case_page = client.get("/enquiries/{}".format(start_case)).data.decode("utf-8")
    assert 'Start shipment</span> <span class="pill p-transit">Booked</span>' in case_page
    # Accept still does what it did
    client.post("/suggestions/{}/accept".format(start), data={"next": "/enquiries/{}".format(start_case)})
    client.post("/suggestions/{}/accept".format(issue), data={"next": "/shipments/{}".format(ids["sid"])})
    conn = db.open_db(path)
    ship = shipments.for_enquiry(conn, start_case)
    assert ship is not None and [e["milestone"] for e in shipments.events(conn, ship["id"])] == ["Booked"]
    assert conn.execute("SELECT eta FROM shipment WHERE id = ?", (ids["sid"],)).fetchone()[0] == "2026-11-28"
    assert suggestions.pending(conn, shipment_id=ids["sid"]) == []
    conn.close()

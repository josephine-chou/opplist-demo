"""Batch E2 (schema v41): striking out an event undoes the action that wrote it.

Every change_log row one user action writes carries the same action_id; a
status event struck out takes its whole action back on that case, Put back
writes it again, and a field changed since is left and named. Every
company, person, place, date and quoted line here is invented; cases from
OPP-3601. Each step commits, as each form post does.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import dates, db, schema_migrations, shipments, suggestions
from opplist.web import create_app

TODAY = "2026-12-04"


def suggest(repo, case, opp, status="Won", on="2026-11-24", quote="Go ahead and book it, please.", **extra):
    values = {"enquiry_id": case, "opp_no": opp, "kind": "status", "proposed_status": status, "proposed_date": on,
              "evidence_quote": quote, "evidence_sender": "Odile Brackwater <odile@fernhollow.example>",
              "evidence_date": on, "confidence": "high", "source": "scan", "state": "pending"}
    values.update(extra)
    sid = repo.insert("suggestion", {k: v for k, v in values.items() if v is not None})
    repo.commit()
    return sid


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "e2.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Fernhollow Freight", country="Chile")
    vendor = add_party(repo, "Quillmoor Logistics", country="USA")
    repo.commit()
    yield conn, repo, {"party": party, "vendor": vendor, "path": path}
    conn.close()


def quoted_case(repo, ids, opp, vendor_request=True):
    case = add_enquiry(repo, opp, ids["party"], received="2026-11-16", origin_loc="MIA", origin_country="USA",
                       dest_loc="SAI", dest_country="Chile", service_mode="Air")
    repo.commit()
    repo.set_status(case, "Quoted", "2026-11-18")
    repo.commit()
    request = None
    if vendor_request:
        request = repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": ids["vendor"],
                                                 "asked_on": "2026-11-17"})
        repo.commit()
    return case, request


def case_row(conn, case):
    return conn.execute("SELECT status, closed_reason, rate_used, awarded_vendor_id, awarded_note, lost_their_price, "
                        "lost_our_price, hold_reason, hold_until FROM enquiry WHERE id = ?", (case,)).fetchone()


def route_results(conn, case):
    return [tuple(r) for r in conn.execute(
        "SELECT result, result_on, lost_reason FROM enquiry_route WHERE enquiry_id = ? AND voided = 0 ORDER BY seq",
        (case,))]


def sugg(conn, sid):
    return conn.execute("SELECT state, decided_at, decided_as, decided_values, edited, event_ref FROM suggestion "
                        "WHERE id = ?", (sid,)).fetchone()


def event_of(conn, case, status):
    return conn.execute("SELECT id FROM status_event WHERE enquiry_id = ? AND status = ? AND voided = 0 "
                        "ORDER BY id DESC LIMIT 1", (case, status)).fetchone()[0]


def accept_won(repo, case, opp, request, routes=None):
    sid = suggest(repo, case, opp)
    decision = {"action": "won", "at": "2026-11-24", "note": "", "rate": ("agent", None, request, None)}
    if routes:
        decision["routes"] = routes
    suggestions.decide(repo, repo.conn.execute("SELECT * FROM suggestion WHERE id = ?", (sid,)).fetchone(),
                       decision, TODAY)
    return sid


# ------------------------------------------------------------- schema --
def test_schema_v41_and_one_accept_is_one_action(world):
    conn, repo, ids = world
    assert schema_migrations.CURRENT_VERSION >= 41
    assert "action_id" in {r[1] for r in conn.execute("PRAGMA table_info(change_log)")}
    case, request = quoted_case(repo, ids, "OPP-3601")
    sid = accept_won(repo, case, "OPP-3601", request)
    won = event_of(conn, case, "Won")
    action = conn.execute("SELECT action_id FROM change_log WHERE table_name = 'status_event' AND row_id = ? "
                          "AND field = '(insert)'", (won,)).fetchone()[0]
    tables = {r[0] for r in conn.execute("SELECT DISTINCT table_name FROM change_log WHERE action_id = ?", (action,))}
    assert {"status_event", "enquiry", "enquiry_route", "vendor_request", "suggestion"} <= tables
    # the Quoted before it was an action of its own
    quoted = event_of(conn, case, "Quoted")
    assert conn.execute("SELECT action_id FROM change_log WHERE table_name = 'status_event' AND row_id = ? "
                        "AND field = '(insert)'", (quoted,)).fetchone()[0] != action
    assert sugg(conn, sid)["state"] == "accepted"


def test_an_upgrade_leaves_the_old_rows_without_an_action(tmp_path):
    conn = db.open_db(tmp_path / "old.db")
    conn.execute("DROP INDEX ix_change_log_action")
    conn.execute("ALTER TABLE change_log DROP COLUMN action_id")
    conn.execute("INSERT INTO change_log (table_name, row_id, field, old_value, new_value) "
                 "VALUES ('enquiry', 1, 'status', 'New', 'Quoted')")
    conn.execute("UPDATE setting SET value = '40' WHERE key = 'schema_version'")
    conn.commit()
    notes = schema_migrations.upgrade(conn)
    assert any("action_id added" in n for n in notes)
    assert [tuple(r) for r in conn.execute("SELECT action_id FROM change_log")] == [(None,)]
    conn.close()


# ------------------------------------------- a Won accepted, struck out --
def test_a_won_accepted_from_a_suggestion_struck_out_and_put_back(world):
    conn, repo, ids = world
    case, request = quoted_case(repo, ids, "OPP-3602")
    sid = accept_won(repo, case, "OPP-3602", request)
    accepted = dict(sugg(conn, sid))
    assert tuple(case_row(conn, case))[:4] == ("Won", None, "agent", ids["vendor"])
    assert route_results(conn, case) == [("Won", "2026-11-24", None)]
    won = event_of(conn, case, "Won")

    notes = repo.void_status_event(won)
    repo.commit()
    assert tuple(case_row(conn, case))[:4] == ("Quoted", None, None, None)
    assert route_results(conn, case) == [(None, None, None)]
    assert conn.execute("SELECT awarded FROM vendor_request WHERE id = ?", (request,)).fetchone()[0] == 0
    assert tuple(sugg(conn, sid)) == ("pending", None, None, None, 0, None)
    assert notes == ["The suggestion is back in Suggested updates."]
    assert [s["id"] for s in suggestions.pending(conn, case)] == [sid]
    # change_log keeps what was decided
    assert conn.execute("SELECT old_value FROM change_log WHERE table_name = 'suggestion' AND row_id = ? "
                        "AND field = 'decided_as' ORDER BY id DESC LIMIT 1", (sid,)).fetchone()[0] == "won"

    notes = repo.void_status_event(won, voided=False)
    repo.commit()
    assert tuple(case_row(conn, case))[:4] == ("Won", None, "agent", ids["vendor"])
    assert route_results(conn, case) == [("Won", "2026-11-24", None)]
    assert conn.execute("SELECT awarded FROM vendor_request WHERE id = ?", (request,)).fetchone()[0] == 1
    assert dict(sugg(conn, sid)) == accepted
    assert notes == ["The suggestion is accepted again."]

    # and struck out a second time, it goes back again
    repo.void_status_event(won)
    repo.commit()
    assert tuple(case_row(conn, case))[:3] == ("Quoted", None, None) and sugg(conn, sid)["state"] == "pending"


def test_a_won_on_two_routes_picked_in_the_form_struck_out(world):
    conn, repo, ids = world
    case, request = quoted_case(repo, ids, "OPP-3603")
    b = repo.add_route(case, {"origin_loc": "LAX", "origin_country": "USA", "dest_loc": "SAI",
                              "dest_country": "Chile", "service_mode": "Air"})
    repo.commit()
    a = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ? AND seq = 1", (case,)).fetchone()[0]
    sid = accept_won(repo, case, "OPP-3603", request, routes=([a, b], {}, {}))
    assert route_results(conn, case) == [("Won", "2026-11-24", None)] * 2
    repo.void_status_event(event_of(conn, case, "Won"))
    repo.commit()
    assert route_results(conn, case) == [(None, None, None)] * 2
    assert tuple(case_row(conn, case))[:4] == ("Quoted", None, None, None)
    assert sugg(conn, sid)["state"] == "pending"


def test_a_lost_with_prices_struck_out(world):
    conn, repo, ids = world
    case, _ = quoted_case(repo, ids, "OPP-3604", vendor_request=False)
    sid = suggest(repo, case, "OPP-3604", status="Lost", quote="We went with a cheaper offer.",
                  closed_reason="Price too high")
    suggestions.decide(repo, conn.execute("SELECT * FROM suggestion WHERE id = ?", (sid,)).fetchone(),
                       {"action": "lost", "at": "2026-11-25", "note": "", "reason": "Price too high",
                        "lost_their_price": "USD 2.10/kg", "lost_our_price": "USD 2.45/kg"}, TODAY)
    row = case_row(conn, case)
    assert (row["status"], row["closed_reason"], row["lost_their_price"], row["lost_our_price"]) == \
        ("Lost", "Price too high", "USD 2.10/kg", "USD 2.45/kg")
    assert route_results(conn, case) == [("Lost", "2026-11-25", "Price too high")]
    repo.void_status_event(event_of(conn, case, "Lost"))
    repo.commit()
    row = case_row(conn, case)
    assert (row["status"], row["closed_reason"], row["lost_their_price"], row["lost_our_price"]) == \
        ("Quoted", None, None, None)
    assert route_results(conn, case) == [(None, None, None)]
    assert sugg(conn, sid)["state"] == "pending"


def test_a_won_that_released_a_hold_struck_out(world):
    conn, repo, ids = world
    case, _ = quoted_case(repo, ids, "OPP-3605", vendor_request=False)
    repo.set_hold(case, "Shipper waiting on a permit", "2026-11-19", until="2026-12-10")
    repo.commit()
    repo.set_status(case, "Won", "2026-11-26", rate_used="own")
    repo.commit()
    released = conn.execute("SELECT id FROM status_event WHERE enquiry_id = ? AND note = 'Hold released'",
                            (case,)).fetchone()[0]
    assert case_row(conn, case)["hold_reason"] is None
    repo.void_status_event(event_of(conn, case, "Won"))
    repo.commit()
    row = case_row(conn, case)
    assert (row["status"], row["hold_reason"], row["hold_until"], row["rate_used"]) == \
        ("Quoted", "Shipper waiting on a permit", "2026-12-10", None)
    assert conn.execute("SELECT voided FROM status_event WHERE id = ?", (released,)).fetchone()[0] == 1
    # the On hold note itself was another action and stays
    assert conn.execute("SELECT voided FROM status_event WHERE enquiry_id = ? AND note LIKE 'On hold:%'",
                        (case,)).fetchone()[0] == 0


def test_a_cancelled_that_closed_a_shipment_struck_out(world):
    conn, repo, ids = world
    case, _ = quoted_case(repo, ids, "OPP-3606", vendor_request=False)
    repo.set_status(case, "Won", "2026-11-20", rate_used="own")
    repo.commit()
    ship = shipments.start(repo, case, "Air · Miami to Santiago")
    repo.commit()
    repo.set_status(case, "Cancelled", "2026-11-27", closed_reason="Order not confirmed")
    repo.commit()
    assert tuple(conn.execute("SELECT closed, closed_reason FROM shipment WHERE id = ?", (ship,)).fetchone()) == \
        (1, "Not shipped")
    note = conn.execute("SELECT id FROM shipment_event WHERE shipment_id = ? AND title LIKE '%closed as Not shipped'",
                        (ship,)).fetchone()[0]
    repo.void_status_event(event_of(conn, case, "Cancelled"))
    repo.commit()
    assert tuple(conn.execute("SELECT closed, closed_reason FROM shipment WHERE id = ?", (ship,)).fetchone()) == \
        (0, None)
    assert conn.execute("SELECT voided FROM shipment_event WHERE id = ?", (note,)).fetchone()[0] == 1
    row = case_row(conn, case)
    assert (row["status"], row["closed_reason"], row["rate_used"]) == ("Won", None, "own")
    assert route_results(conn, case) == [("Won", "2026-11-20", None)]


def test_a_field_changed_after_the_action_is_left_and_named(world):
    conn, repo, ids = world
    case, request = quoted_case(repo, ids, "OPP-3607")
    accept_won(repo, case, "OPP-3607", request)
    repo.set_rate_used(case, "own")
    repo.commit()
    notes = repo.void_status_event(event_of(conn, case, "Won"))
    repo.commit()
    today = dates.month_day(dt.date.today().isoformat())
    assert "Rate used was changed after this on {}; left as it is.".format(today) in notes
    assert "The awarded vendor was changed after this on {}; left as it is.".format(today) in notes
    row = case_row(conn, case)
    assert (row["status"], row["rate_used"], row["awarded_vendor_id"]) == ("Quoted", "own", None)
    assert route_results(conn, case) == [(None, None, None)]


def test_a_manual_requote_struck_out_drops_the_round(world):
    conn, repo, ids = world
    case, _ = quoted_case(repo, ids, "OPP-3608", vendor_request=False)
    repo.requote(case, "2026-11-23", note="lower airfreight", reason="Lower rate")
    repo.commit()
    rounds = "SELECT quote_rounds FROM v_enquiry WHERE id = ?"
    assert conn.execute(rounds, (case,)).fetchone()[0] == 2
    requote = conn.execute("SELECT id FROM status_event WHERE enquiry_id = ? AND kind = 'requote'",
                           (case,)).fetchone()[0]
    repo.void_status_event(requote)
    repo.commit()
    assert conn.execute(rounds, (case,)).fetchone()[0] == 1
    assert case_row(conn, case)["status"] == "Quoted"


def test_a_won_from_before_v41_struck_out_restates_the_case(world):
    conn, repo, ids = world
    case, request = quoted_case(repo, ids, "OPP-3609")
    sid = accept_won(repo, case, "OPP-3609", request)
    won = event_of(conn, case, "Won")
    action = conn.execute("SELECT action_id FROM change_log WHERE table_name = 'status_event' AND row_id = ? "
                          "AND field = '(insert)'", (won,)).fetchone()[0]
    conn.execute("UPDATE change_log SET action_id = NULL WHERE action_id = ?", (action,))   # as written before v41
    conn.commit()
    notes = repo.void_status_event(won)
    repo.commit()
    row = case_row(conn, case)
    assert (row["status"], row["closed_reason"], row["rate_used"], row["awarded_vendor_id"]) == \
        ("Quoted", None, "agent", ids["vendor"])
    assert route_results(conn, case) == [(None, None, None)]
    assert sugg(conn, sid)["state"] == "pending"
    assert notes[0].startswith("Rate used and the awarded vendor were left as they are")
    # its Put back takes the restating back
    repo.void_status_event(won, voided=False)
    repo.commit()
    assert case_row(conn, case)["status"] == "Won" and route_results(conn, case) == [("Won", "2026-11-24", None)]
    assert sugg(conn, sid)["state"] == "accepted"


def test_the_action_that_registered_the_case_is_not_undone(world):
    conn, repo, ids = world
    case = repo.create_enquiry({"requester_party_id": ids["party"], "received_on": "2026-11-21", "status": "Quoted",
                                "origin_loc": "MIA", "origin_country": "USA", "dest_loc": "SAI",
                                "dest_country": "Chile", "service_mode": "Air"}, status_at="2026-11-22")
    repo.commit()
    repo.void_status_event(event_of(conn, case, "Quoted"))
    repo.commit()
    assert [tuple(r) for r in conn.execute("SELECT status, voided FROM status_event WHERE enquiry_id = ? ORDER BY id",
                                           (case,))] == [("New", 0), ("Quoted", 1)]
    assert case_row(conn, case)["status"] == "New"


# ------------------------------------------- shipment events, vendors --
def test_a_shipment_milestone_and_a_vendor_request_put_their_suggestions_back(world):
    conn, repo, ids = world
    case, _ = quoted_case(repo, ids, "OPP-3610", vendor_request=False)
    repo.set_status(case, "Won", "2026-11-20", rate_used="own")
    repo.commit()
    ship = shipments.start(repo, case, "Air · Miami to Santiago")
    repo.commit()
    milestone = suggest(repo, case, "OPP-3610", status="Departed", on="2026-11-28 07:40",
                        kind="shipment_milestone", shipment_id=ship, quote="The flight left on time.")
    suggestions.accept(repo, milestone, today=TODAY)
    event = int(sugg(conn, milestone)["event_ref"].split(":")[1])
    accepted = dict(sugg(conn, milestone))
    notes = shipments.void_event(repo, ship, event)
    repo.commit()
    assert sugg(conn, milestone)["state"] == "pending" and notes == ["The suggestion is back in Suggested updates."]
    shipments.void_event(repo, ship, event, voided=False)
    repo.commit()
    assert dict(sugg(conn, milestone)) == accepted

    vendor = suggest(repo, case, "OPP-3610", status=None, kind="vendor", vendor_key="Thistlebank Air Cargo",
                     vendor_domain="thistlebank.example", quote="Rate request MIA-SCL")
    suggestions.accept(repo, vendor, today=TODAY)
    request = int(sugg(conn, vendor)["event_ref"].split(":")[1])
    accepted = dict(sugg(conn, vendor))
    repo.void_vendor_request(request)
    repo.commit()
    assert sugg(conn, vendor)["state"] == "pending"
    repo.void_vendor_request(request, voided=False)
    repo.commit()
    assert dict(sugg(conn, vendor)) == accepted
    assert conn.execute("SELECT voided FROM vendor_request WHERE id = ?", (request,)).fetchone()[0] == 0


# ----------------------------------------------------------------- web --
def test_the_timeline_strike_out_says_what_it_left(world):
    conn, repo, ids = world
    case, request = quoted_case(repo, ids, "OPP-3611")
    accept_won(repo, case, "OPP-3611", request)
    repo.set_rate_used(case, "own")
    repo.commit()
    app = create_app(ids["path"])
    app.config["TESTING"] = True
    client = app.test_client()
    won = event_of(conn, case, "Won")
    page = client.post("/enquiries/{}/events/{}".format(case, won), data={"action": "void"}, follow_redirects=True)
    body = page.data.decode("utf-8")
    assert "Event struck out." in body and "Rate used was changed after this on" in body
    assert "The suggestion is back in Suggested updates." in body
    # one request, one action, its own
    actions = {r[0] for r in conn.execute("SELECT action_id FROM change_log WHERE table_name = 'status_event' "
                                          "AND row_id = ? AND field = 'voided'", (won,))}
    assert len(actions) == 1 and None not in actions
    action = actions.pop()
    assert conn.execute("SELECT COUNT(DISTINCT table_name) FROM change_log WHERE action_id = ?",
                        (action,)).fetchone()[0] >= 3
    assert conn.execute("SELECT status FROM enquiry WHERE id = ?", (case,)).fetchone()[0] == "Quoted"


def test_tracking_runs_off_the_events_that_still_count(world):
    """R3 asks how long since anything happened; a struck-out event did not
    (moved here from test_timeline in E2, invented data)."""
    from opplist import tracking
    conn, repo, ids = world
    case = add_enquiry(repo, "OPP-3612", ids["party"], received="2026-08-25", origin_loc="MIA",
                       origin_country="USA", dest_loc="SAI", dest_country="Chile", service_mode="Air")
    repo.commit()
    for at in ("2026-08-28", "2026-09-08"):
        repo.set_status(case, "Quoted", at)
        repo.commit()
    r3 = {s.rule: s for s in tracking.run(conn, today="2026-09-09")}["R3"]
    assert [(i.since, i.extra["step"]) for i in r3.items] == [("2026-09-08", "FU1")]
    latest = conn.execute("SELECT id FROM status_event WHERE enquiry_id = ? AND at = '2026-09-08'",
                          (case,)).fetchone()[0]
    repo.void_status_event(latest)
    repo.commit()
    row = conn.execute("SELECT status, last_event_on FROM v_enquiry WHERE id = ?", (case,)).fetchone()
    assert (row["status"], row["last_event_on"]) == ("Quoted", "2026-08-28")
    r3 = {s.rule: s for s in tracking.run(conn, today="2026-09-09")}["R3"]
    assert [i.since for i in r3.items] == ["2026-08-28"]

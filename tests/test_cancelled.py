"""Batch C1 (schema v38): Cancelled, a closing status of its own.

Won = we got it; Lost = the shipment went ahead without us; Cancelled =
the shipment did not go ahead at all; Dropped = the row should not exist.
The upgrade, the routes and the case status, the Cancelled action, the
closing reason as one fact, the refused early closing, Rate used kept by a
route won, reading the mail, the win rates and the old export's word.

Every company, place, date and number here is invented; cases from OPP-3101."""

from __future__ import annotations

import json

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import (dashboard, db, export_xlsx, monthly, propose, report, routes, schema_migrations,
                     shipments, status as status_mod, suggestions)
from opplist.web import create_app

TODAY = "2026-11-20"


def rows(conn, sql, *args):
    return [tuple(r) for r in conn.execute(sql, args).fetchall()]


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "c1.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    repo.insert("country", {"name": "Peru", "region": "Latin America"})
    for key, country in (("OSORNO", "Chile"), ("TEMUCO", "Chile"), ("CUSCO", "Peru")):
        repo.insert("location", {"key": key, "country": country, "type": "City"})
    agent = add_party(repo, "Fernhill Cargo", country="Chile")
    ids = {"agent": agent}

    def case(opp, received, **extra):
        return add_enquiry(repo, opp, agent, received=received, origin_loc="MIA", origin_country="USA",
                           dest_loc="OSORNO", dest_country="Chile", service_mode="Air", **extra)

    ids["new"] = case("OPP-3101", "2026-11-02")
    ids["quoted"] = case("OPP-3102", "2026-11-03")
    repo.set_status(ids["quoted"], "Quoted", "2026-11-05")
    ids["q_b"] = repo.add_route(ids["quoted"], {"origin_loc": "MIA", "origin_country": "USA", "dest_loc": "TEMUCO",
                                                "dest_country": "Chile", "service_mode": "Air"})
    ids["won"] = case("OPP-3103", "2026-11-04")
    ids["w_b"] = repo.add_route(ids["won"], {"origin_loc": "MIA", "origin_country": "USA", "dest_loc": "CUSCO",
                                             "dest_country": "Peru", "service_mode": "Air"})
    ids["w_a"] = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ? AND seq = 1", (ids["won"],)).fetchone()[0]
    repo.record_results(ids["won"], "2026-11-10", [ids["w_a"]], {}, rate_used="own")
    ids["ship"] = shipments.start(repo, ids["won"], route_id=ids["w_a"])
    db.set_setting(conn, "next_opp_number", 3110)
    repo.commit()
    yield conn, path, ids
    conn.close()


@pytest.fixture
def web(world, monkeypatch):
    conn, path, ids = world
    import opplist.web as web_module
    monkeypatch.setattr(web_module, "_today", lambda: TODAY)
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), conn, ids


def status_of(conn, enquiry_id):
    return conn.execute("SELECT status, closed_reason FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone()[:]


def route_results(conn, enquiry_id):
    return rows(conn, "SELECT seq, result, lost_reason FROM enquiry_route WHERE enquiry_id = ? AND voided = 0 "
                      "ORDER BY seq", enquiry_id)


# --------------------------------------------------------------- the upgrade --
def _back_to_v37(path):
    """The tables, views and settings as v37 had them: no Cancelled anywhere."""
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = OFF")
    import re
    from opplist import paths
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    for name in re.findall(r"CREATE VIEW\s+(\w+)\s+AS", schema_sql):
        conn.execute("DROP VIEW IF EXISTS {}".format(name))
    for table in ("enquiry", "status_event", "enquiry_route"):
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()[0]
        old = sql.replace(",'Cancelled'", "").replace("CREATE TABLE {}".format(table),
                                                      "CREATE TABLE {}_old".format(table), 1)
        conn.execute(old)
        conn.execute("INSERT INTO {0}_old SELECT * FROM {0}".format(table))
        conn.execute("DROP TABLE {}".format(table))
        conn.execute("ALTER TABLE {0}_old RENAME TO {0}".format(table))
    conn.execute("UPDATE setting SET value = ? WHERE key = 'closed_reasons_lost'", (json.dumps(
        ["Price too high", "Rate too slow", "No response from customer", "Shipment cancelled",
         "Unable to handle", "No vendor rate", "Went with another forwarder", "Other"]),))
    conn.execute("DELETE FROM setting WHERE key = 'closed_reasons_cancelled'")
    conn.execute("UPDATE setting SET value = '37' WHERE key = 'schema_version'")
    # a view schema.sql does not own, as the live database's location view (v24)
    conn.execute("CREATE VIEW location_probe AS SELECT key FROM location")
    conn.commit()
    conn.close()


def test_the_upgrade_turns_only_lost_shipment_cancelled_into_cancelled(world):
    conn, path, ids = world
    repo = db.Repository(conn, source="test")
    cancelled = ids["new"]
    repo.set_status(cancelled, "Lost", "2026-11-12", closed_reason="Shipment cancelled", note="the plant closed")
    priced = ids["quoted"]
    repo.set_status(priced, "Lost", "2026-11-12", closed_reason="Price too high")
    # a Won case with a route lost for 'Shipment cancelled' is not converted
    repo.edit_route(ids["w_b"], {"result": "Lost", "lost_reason": "Shipment cancelled"}, at="2026-11-12")
    repo.commit()
    conn.close()
    _back_to_v37(path)

    notes = []
    conn = db.open_db(path, upgrade_notes=notes)
    assert schema_migrations.current_version(conn) == schema_migrations.CURRENT_VERSION >= 38
    assert any(n.startswith("restore point opplist_before_schema") for n in notes), notes
    assert status_of(conn, cancelled) == ("Cancelled", "Shipment cancelled")
    assert rows(conn, "SELECT status, reason, note FROM status_event WHERE enquiry_id = ? AND kind = 'status' "
                      "ORDER BY id DESC LIMIT 1", cancelled) == [("Cancelled", "Shipment cancelled", "the plant closed")]
    assert route_results(conn, cancelled) == [(1, "Cancelled", "Shipment cancelled")]
    assert status_of(conn, priced) == ("Lost", "Price too high")
    assert route_results(conn, ids["won"])[1] == (2, "Lost", "Shipment cancelled")
    # every value through the repository
    logged = rows(conn, "SELECT table_name, field, old_value, new_value FROM change_log WHERE source = 'migration' "
                        "AND new_value = 'Cancelled' ORDER BY table_name")
    assert {r[0] for r in logged} == {"enquiry", "enquiry_route", "status_event"}
    # the Lost route still carries the reason, so it stays on the Lost list, and the note says which
    assert "Shipment cancelled" in status_mod.closed_reasons(conn, "Lost")
    assert any("kept on the Lost reasons: still on Lost OPP-3103" in n for n in notes), notes
    assert status_mod.closed_reasons(conn, "Cancelled") == list(status_mod.DEFAULT_REASONS_CANCELLED)
    # a view schema.sql does not own survives (the live location view, v24)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'view' AND name = 'location_probe'").fetchone()
    assert conn.execute("SELECT closed_on FROM v_enquiry WHERE id = ?", (cancelled,)).fetchone()[0] == "2026-11-12"
    conn.close()


def test_the_upgrade_takes_shipment_cancelled_off_the_lost_list_when_nothing_lost_carries_it(world):
    conn, path, ids = world
    db.Repository(conn, source="test").set_status(ids["new"], "Lost", "2026-11-12", closed_reason="Shipment cancelled")
    conn.commit()
    conn.close()
    _back_to_v37(path)
    notes = []
    conn = db.open_db(path, upgrade_notes=notes)
    assert "Shipment cancelled" not in status_mod.closed_reasons(conn, "Lost")
    assert any("Lost with 'Shipment cancelled' made Cancelled: 1 (OPP-3101)" in n for n in notes), notes
    conn.close()


# ------------------------------------------------------- routes and status --
def test_the_case_follows_its_routes(world):
    conn, path, ids = world
    repo = db.Repository(conn, source="test")
    a = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ? AND seq = 1", (ids["quoted"],)).fetchone()[0]
    b = ids["q_b"]
    repo.edit_route(b, {"result": "Cancelled", "lost_reason": "Order not confirmed"}, at=TODAY)
    assert status_of(conn, ids["quoted"])[0] == "Quoted"                   # A still open
    repo.edit_route(a, {"result": "Won"}, at=TODAY)
    assert status_of(conn, ids["quoted"])[0] == "Won"                      # won A, cancelled B
    repo.edit_route(a, {"result": "Lost", "lost_reason": "Price too high"}, at=TODAY)
    assert status_of(conn, ids["quoted"]) == ("Lost", "Price too high")    # lost A, cancelled B
    repo.edit_route(a, {"result": "Cancelled", "lost_reason": "Shipment cancelled"}, at=TODAY)
    assert status_of(conn, ids["quoted"]) == ("Cancelled", "Shipment cancelled")   # both cancelled
    assert routes.result_line(routes.routes_of(conn, ids["quoted"])) == "cancelled A, B"
    assert status_mod.to_legacy("Cancelled") == "Customer Cancelled"
    back = status_mod.from_legacy("Customer Cancelled")
    assert (back.status, back.closed_reason) == ("Cancelled", "Shipment cancelled")


# ------------------------------------------------------- the case page --
def test_cancelled_on_a_new_and_a_quoted_case(web):
    client, conn, ids = web
    body = client.get("/enquiries/{}".format(ids["new"])).data.decode("utf-8")
    acts = body[body.index('class="acts-row"'):body.index("</div>", body.index('class="acts-row"'))]
    assert acts.index('data-act="lost"') < acts.index('data-act="cancelled"') < acts.index('data-act="hold"')
    assert 'data-act-form="cancelled"' in body and "Order not confirmed" in body
    refused = client.post("/enquiries/{}/status".format(ids["new"]), data={"status": "Cancelled", "at": TODAY},
                          follow_redirects=True)
    assert "Cancelled needs a reason." in refused.data.decode("utf-8")
    client.post("/enquiries/{}/status".format(ids["new"]), data={
        "status": "Cancelled", "at": TODAY, "closed_reason": "Order not confirmed", "note": "their buyer never signed"})
    conn.rollback()
    assert status_of(conn, ids["new"]) == ("Cancelled", "Order not confirmed")
    assert route_results(conn, ids["new"]) == [(1, "Cancelled", "Order not confirmed")]
    # two routes, none with a result: both cancelled
    client.post("/enquiries/{}/status".format(ids["quoted"]), data={
        "status": "Cancelled", "at": TODAY, "closed_reason": "Shipment cancelled"})
    conn.rollback()
    assert status_of(conn, ids["quoted"]) == ("Cancelled", "Shipment cancelled")
    assert route_results(conn, ids["quoted"]) == [(1, "Cancelled", "Shipment cancelled"),
                                                  (2, "Cancelled", "Shipment cancelled")]
    # a closed Cancelled case offers Note only, its label grey
    body = client.get("/enquiries/{}".format(ids["quoted"])).data.decode("utf-8")
    acts = body[body.index('class="acts-row"'):body.index("</div>", body.index('class="acts-row"'))]
    assert 'data-act="note"' in acts and 'data-act="won"' not in acts and 'data-act="cancelled"' not in acts
    assert 'pill big s-cancelled' in body


def test_cancelled_on_a_won_case_cancels_its_won_routes_and_closes_the_shipment(web):
    client, conn, ids = web
    body = client.get("/enquiries/{}".format(ids["won"])).data.decode("utf-8")
    acts = body[body.index('class="acts-row"'):body.index("</div>", body.index('class="acts-row"'))]
    assert acts.index('data-act="cancelled"') < acts.index('data-act="note"')
    client.post("/enquiries/{}/status".format(ids["won"]), data={
        "status": "Cancelled", "at": TODAY, "closed_reason": "Shipment cancelled", "note": "consignee went bankrupt"})
    conn.rollback()
    assert status_of(conn, ids["won"]) == ("Cancelled", "Shipment cancelled")
    assert route_results(conn, ids["won"]) == [(1, "Cancelled", "Shipment cancelled"),
                                               (2, "Cancelled", "Shipment cancelled")]
    ship = conn.execute("SELECT closed, closed_reason, title FROM shipment WHERE id = ?", (ids["ship"],)).fetchone()
    assert (ship["closed"], ship["closed_reason"]) == (1, "Not shipped")
    note = conn.execute("SELECT kind, title, detail FROM shipment_event WHERE shipment_id = ? ORDER BY id DESC",
                        (ids["ship"],)).fetchone()
    assert note[:] == ("note", "OPP-3103 cancelled: closed as Not shipped", "Shipment cancelled")


def test_the_won_picker_offers_lost_or_cancelled(web):
    client, conn, ids = web
    body = client.get("/enquiries/{}".format(ids["quoted"])).data.decode("utf-8")
    assert 'value="Cancelled|Order not confirmed"' in body
    a = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ? AND seq = 1", (ids["quoted"],)).fetchone()[0]
    client.post("/enquiries/{}/status".format(ids["quoted"]), data={
        "status": "Won", "at": TODAY, "routes_present": "1", "won_route": [str(a)],
        "lost_reason_{}".format(ids["q_b"]): "Cancelled|Order not confirmed", "rate_used": "own"})
    conn.rollback()
    assert status_of(conn, ids["quoted"])[0] == "Won"
    assert route_results(conn, ids["quoted"]) == [(1, "Won", None), (2, "Cancelled", "Order not confirmed")]


# ------------------------------------------------------ one closing reason --
def test_a_reason_edited_in_the_timeline_reaches_the_case_and_its_routes(web):
    client, conn, ids = web
    repo = db.Repository(conn, source="test")
    repo.set_status(ids["quoted"], "Lost", "2026-11-12", closed_reason="Price too high")
    repo.commit()
    event = conn.execute("SELECT id FROM status_event WHERE enquiry_id = ? AND status = 'Lost'",
                         (ids["quoted"],)).fetchone()[0]
    client.post("/enquiries/{}/events/{}".format(ids["quoted"], event), data={
        "at": "2026-11-12", "status": "Cancelled", "reason": "Order not confirmed", "note": ""})
    conn.rollback()
    assert status_of(conn, ids["quoted"]) == ("Cancelled", "Order not confirmed")
    assert route_results(conn, ids["quoted"]) == [(1, "Cancelled", "Order not confirmed"),
                                                  (2, "Cancelled", "Order not confirmed")]


def test_a_reason_edited_in_the_route_editor_reaches_the_case_and_its_closing_event(world):
    conn, path, ids = world
    repo = db.Repository(conn, source="test")
    repo.set_status(ids["new"], "Lost", "2026-11-12", closed_reason="Price too high")
    a = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ?", (ids["new"],)).fetchone()[0]
    repo.edit_route(a, {"result": "Lost", "lost_reason": "Rate too slow"}, at=TODAY)
    assert status_of(conn, ids["new"]) == ("Lost", "Rate too slow")
    assert rows(conn, "SELECT reason FROM status_event WHERE enquiry_id = ? AND status = 'Lost'", ids["new"]) == \
        [("Rate too slow",)]
    # two closed routes that disagree: the case keeps its reason until they agree
    repo.set_status(ids["quoted"], "Lost", "2026-11-12", closed_reason="Price too high")
    a2 = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ? AND seq = 1", (ids["quoted"],)).fetchone()[0]
    repo.edit_route(a2, {"result": "Lost", "lost_reason": "Rate too slow"}, at=TODAY)
    assert status_of(conn, ids["quoted"]) == ("Lost", "Price too high")
    repo.edit_route(ids["q_b"], {"result": "Lost", "lost_reason": "Rate too slow"}, at=TODAY)
    assert status_of(conn, ids["quoted"]) == ("Lost", "Rate too slow")


# ------------------------------------------------------ an early closing --
def test_a_won_dated_before_the_latest_event_is_refused_everywhere(web):
    client, conn, ids = web
    events = lambda: conn.execute("SELECT COUNT(*) FROM status_event WHERE enquiry_id = ?", (ids["quoted"],)).fetchone()[0]
    before = events()
    page = client.post("/enquiries/{}/status".format(ids["quoted"]), data={
        "status": "Won", "at": "2026-11-04", "rate_used": "own"}, follow_redirects=True)
    assert ("Won on Nov 4 is before this case&#39;s latest event (Quoted, Nov 5). Use a later date, "
            "or correct the earlier event first.") in page.data.decode("utf-8")
    conn.rollback()
    assert events() == before and status_of(conn, ids["quoted"])[0] == "Quoted"
    # the route editor: a result (dated today, the editor has no date) that
    # would close the case before an event dated later
    db.Repository(conn, source="test").set_status(ids["quoted"], "Quoted", "2026-11-25", note="sent again")
    conn.commit()
    before = events()
    a = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ? AND seq = 1", (ids["quoted"],)).fetchone()[0]
    page = client.post("/enquiries/{}/routes".format(ids["quoted"]), data={
        "form": "route", "route_id": str(a), "origin_loc": "MIA", "origin_country": "USA", "dest_loc": "OSORNO",
        "dest_country": "Chile", "service_mode": "Air", "result": "Won"},
        follow_redirects=True)                                         # 24b-fix: post, redirect, get
    assert "Won on Nov 20 is before this case&#39;s latest event (Quoted, Nov 25)" in page.data.decode("utf-8")
    conn.rollback()
    assert route_results(conn, ids["quoted"])[0] == (1, None, None) and events() == before
    # Accept of a closing suggestion: refused, the suggestion stays pending
    sid = db.Repository(conn, source="test").insert("suggestion", {
        "enquiry_id": ids["quoted"], "opp_no": "OPP-3102", "kind": "status", "proposed_status": "Won", "proposed_date": "2026-11-04",
        "state": "pending", "evidence_sender": "Lia Moran", "evidence_quote": "please book"})
    conn.commit()
    with pytest.raises(ValueError, match=r"Won on Nov 4 is before this case's latest event \(Quoted, Nov 25\)"):
        suggestions.accept(db.Repository(conn, source="test"), sid)
    conn.rollback()
    assert conn.execute("SELECT state FROM suggestion WHERE id = ?", (sid,)).fetchone()[0] == "pending"
    assert events() == before


def test_a_route_won_keeps_rate_used(world):
    conn, path, ids = world
    repo = db.Repository(conn, source="test")
    repo.update("enquiry", ids["new"], {"rate_used": "own", "awarded_note": "own buy rate"})
    a = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ?", (ids["new"],)).fetchone()[0]
    repo.edit_route(a, {"result": "Won"}, at=TODAY)
    assert rows(conn, "SELECT status, rate_used, awarded_note FROM enquiry WHERE id = ?", ids["new"]) == \
        [("Won", "own", "own buy rate")]


# ------------------------------------------------------- reading the mail --
def _mail(body):
    return propose.ThreadMail(at="2026-11-15", sender_name="Lia Moran", address="lia@fernhill.example",
                              internal=False, subject="RE: RATE REQUEST // MIA - OSORNO", body=body,
                              from_requester=True)


@pytest.mark.parametrize("body, wanted", [
    ("Our importer will proceed on their own with the freight, so you can cancel the pickup request.",
     ("Lost", "Went with another forwarder")),
    ("Plans changed at the plant and the freight is cancelled.", ("Cancelled", "Shipment cancelled")),
    ("The mill tells us they no longer require the rate for this lane.", ("Cancelled", "Shipment cancelled")),
    ("The distributor did not confirm the order before the deadline.", ("Cancelled", "Order not confirmed")),
    ("Production slipped, so the move is postponed until spring.", ("", "")),
    ("Leave this one on hold while the plant decides.", ("", "")),
])
def test_the_phrases_split_by_what_they_mean(body, wanted):
    proposal = propose.classify([_mail(body)], "Fernhill Cargo", requester_known=True)
    got = (proposal.proposed_status or "", proposal.closed_reason or "")
    # nothing proposed reads 'Unknown': a postponement is not a closing
    assert (got if got[0] != "Unknown" else ("", "")) == wanted


def test_accepting_a_cancelled_suggestion_writes_what_the_case_page_writes(world):
    conn, path, ids = world
    repo = db.Repository(conn, source="test")
    sid = repo.insert("suggestion", {
        "enquiry_id": ids["quoted"], "opp_no": "OPP-3102", "kind": "status", "proposed_status": "Cancelled", "proposed_date": "2026-11-15",
        "closed_reason": "Shipment cancelled", "state": "pending", "evidence_sender": "Lia Moran",
        "evidence_quote": "plans changed and the freight is cancelled"})
    suggestions.accept(repo, sid)
    assert status_of(conn, ids["quoted"]) == ("Cancelled", "Shipment cancelled")
    assert route_results(conn, ids["quoted"]) == [(1, "Cancelled", "Shipment cancelled"),
                                                  (2, "Cancelled", "Shipment cancelled")]


# ---------------------------------------------------------------- counting --
def test_win_rates_leave_cancelled_out(world):
    conn, path, ids = world
    repo = db.Repository(conn, source="test")
    repo.set_status(ids["new"], "Cancelled", "2026-11-12", closed_reason="Shipment cancelled")
    repo.set_status(ids["quoted"], "Lost", "2026-11-12", closed_reason="Price too high")
    repo.commit()
    dash = dashboard.build(conn, today=TODAY)
    assert (dash.pipeline["won_90"], dash.pipeline["lost_90"], dash.pipeline["win_rate_90"]) == (1, 1, 50)
    assert ("Cancelled", 1) in [(name, n) for name, n, _c in dash.pipeline["closed"]]
    kpis = monthly.kpis(conn, "2026-11")
    assert kpis[3] == ("1", "Won") and kpis[4] == ("1", "Lost")
    assert monthly.kpi_notes(conn, "2026-11") == {"Lost": "1 cancelled"}
    assert "Cancelled" in report.OUTCOMES and "Lost or cancelled" not in report.OUTCOMES
    assert "Customer Cancelled" in export_xlsx.LEGACY_STATUS_LIST
    row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (ids["new"],)).fetchone()
    assert status_mod.row_to_legacy(row) == "Customer Cancelled"
    # check lists a Cancelled case with no reason like a Lost one
    repo.update("enquiry", ids["new"], {"closed_reason": None})
    from opplist import checks
    found = {f.key: f for f in checks.run(conn)}
    assert "OPP-3101" in [r["opp_no"] for r in found["closed_without_reason"].rows]

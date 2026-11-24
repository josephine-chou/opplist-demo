"""The reason on the event (schema v22): closed reasons on Lost / Dropped
events, re-quote reasons on the Quoted event that reopens a round or the
requote event that opened it, and the report table built from them."""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference, form_nesting
from opplist import db, monthly, monthly_export, schema_migrations, status as status_mod
from opplist.web import create_app


def events(conn, case):
    return [tuple(r) for r in conn.execute(
        "SELECT status, kind, reason FROM status_event WHERE enquiry_id = ? "
        "AND kind IN ('status', 'requote') AND (status <> 'New' OR kind = 'requote' OR reason IS NOT NULL "
        "OR id > (SELECT MIN(id) FROM status_event WHERE enquiry_id = ?)) ORDER BY at, id", (case, case))]


# ------------------------------------------------------------ migration --
def test_v22_adds_the_column_the_setting_and_backfills_closing_events(repo):
    conn = repo.conn
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    lost = add_enquiry(repo, "OPP-0001", party, received="2026-08-01")
    repo.set_status(lost, "Lost", "2026-08-10", closed_reason="Price too high")
    dropped = add_enquiry(repo, "OPP-0002", party, received="2026-08-02")
    repo.set_status(dropped, "Dropped", "2026-08-11", closed_reason="Registered in error")
    blank = add_enquiry(repo, "OPP-0003", party, received="2026-08-03")
    repo.set_status(blank, "Lost", "2026-08-12")
    open_case = add_enquiry(repo, "OPP-0004", party, received="2026-08-04")
    repo.set_status(open_case, "Quoted", "2026-08-13")
    # as before v22: the events carry no reason yet
    conn.execute("UPDATE status_event SET reason = NULL")
    repo.commit()

    notes = schema_migrations._to_v22(conn)
    conn.commit()
    assert "reason" in [r[1] for r in conn.execute("PRAGMA table_info(status_event)")]
    assert status_mod.requote_reasons(conn)[0] == "Lower rate" and "Other" in status_mod.requote_reasons(conn)
    assert any("copied onto their closing events: 2" in n for n in notes)
    reasons = {r["opp_no"]: r["reason"] for r in conn.execute(
        "SELECT e.opp_no, s.reason FROM status_event s JOIN enquiry e ON e.id = s.enquiry_id "
        "WHERE s.status IN ('Lost', 'Dropped', 'Quoted') AND s.kind = 'status'")}
    assert reasons == {"OPP-0001": "Price too high", "OPP-0002": "Registered in error",
                       "OPP-0003": None, "OPP-0004": None}
    assert conn.execute("SELECT closed_reason FROM enquiry WHERE id = ?", (lost,)).fetchone()[0] == "Price too high"
    logged = conn.execute("SELECT COUNT(*) FROM change_log WHERE table_name = 'status_event' "
                          "AND field = 'reason' AND source = 'migration'").fetchone()[0]
    assert logged == 2
    assert not any("events: 1" in n or "events: 2" in n for n in schema_migrations._to_v22(conn)) \
        or "events: 0" in " ".join(schema_migrations._to_v22(conn))      # a second run copies nothing


def old_requote(repo, case, at, reason=None):
    """A re-quote as v19 to v28 wrote it: a requote event on Sourcing that
    opens the round the next Quoted closes (still read that way, v30)."""
    return repo.set_status(case, "Sourcing", at, note="Re-quote", kind="requote", reason=reason)


# ----------------------------------------------------------- repository --
@pytest.fixture
def quoted(repo):
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    case = add_enquiry(repo, "OPP-1601", party, received="2026-07-01", origin_loc="LAX",
                       origin_country="USA", dest_loc="SAI", dest_country="Chile")
    repo.set_status(case, "Quoted", "2026-07-10")
    repo.commit()
    return repo, case


def test_closed_and_requote_reasons_land_on_their_events(quoted):
    repo, case = quoted
    repo.requote(case, "2026-07-20", note="3 x 40HQ", reason="Request changed")
    repo.set_status(case, "Quoted", "2026-07-22", reason="Lower rate")
    repo.set_status(case, "Lost", "2026-07-30", closed_reason="Price too high")
    repo.commit()
    assert events(repo.conn, case) == [
        ("Quoted", "status", None), ("Quoted", "requote", "Request changed"),
        ("Quoted", "status", "Lower rate"), ("Lost", "status", "Price too high")]


def test_the_reading_rule_prefers_the_quoted_events_reason(quoted):
    repo, case = quoted
    old_requote(repo, case, "2026-07-20", reason="Request changed")
    repo.commit()                                   # E2: each one its own action, as a form post is
    repo.set_status(case, "Quoted", "2026-07-22", reason="Lower rate")
    repo.commit()
    old_requote(repo, case, "2026-08-01", reason="Rates expired")
    repo.commit()
    repo.set_status(case, "Quoted", "2026-08-03")                        # no reason of its own
    repo.commit()
    old_requote(repo, case, "2026-08-10")
    repo.commit()
    repo.set_status(case, "Quoted", "2026-08-12")                        # nothing anywhere
    repo.commit()
    rounds = status_mod.quote_rounds(repo.conn, case)
    assert [(r["requote"], r["reason"]) for r in rounds] == [
        (False, None), (True, "Lower rate"), (True, "Rates expired"), (True, None)]
    assert rounds[1]["opener_reason"] == "Request changed" and rounds[1]["own_reason"] == "Lower rate"
    # a struck-out requote event no longer opens anything
    opener = rounds[2]["opener_id"]
    repo.void_status_event(opener)
    repo.commit()
    assert status_mod.quote_rounds(repo.conn, case)[2]["reason"] is None


def test_the_vocabulary_is_checked(quoted):
    repo, case = quoted
    assert status_mod.requote_reason_belongs(repo.conn, "lower rate")
    assert status_mod.requote_reason_belongs(repo.conn, None)
    assert not status_mod.requote_reason_belongs(repo.conn, "Price too high")
    assert status_mod.event_reason_list(repo.conn, "Lost") == status_mod.closed_reasons(repo.conn, "Lost")
    assert status_mod.event_reason_list(repo.conn, "Sourcing", "requote") == status_mod.requote_reasons(repo.conn)
    assert status_mod.event_reason_list(repo.conn, "Sourcing") == []
    assert "Lower rate" not in status_mod.closed_reasons(repo.conn)        # never a closed reason


# ------------------------------------------------------------- the page --
@pytest.fixture
def web(tmp_path):
    path = tmp_path / "reasons-web.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    fresh = add_enquiry(repo, "OPP-1701", party, received="2026-07-01")
    once = add_enquiry(repo, "OPP-1702", party, received="2026-07-01")
    repo.set_status(once, "Quoted", "2026-07-10")
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, fresh, once


# test_the_wrap_is_rendered_for_a_quoted_case_only: replaced in 15a by tests/test_case_v3.py (case page v3).


# Set status Quoted again: tests/test_requote_not_swallowed.py (13d).


def test_the_requote_form_and_the_event_editor_carry_the_reason(web):
    client, path, fresh, once = web
    page = client.post("/enquiries/{}/requote".format(once), data={
        "at": "2026-07-25", "requote_reason": "Request changed"}, follow_redirects=True)
    body = page.data.decode("utf-8")
    assert "re-quote &middot; Request changed" in body
    conn = db.open_db(path)
    event = conn.execute("SELECT id FROM status_event WHERE enquiry_id = ? AND kind = 'requote'",
                         (once,)).fetchone()["id"]
    lost = conn.execute("SELECT id FROM status_event WHERE enquiry_id = ? AND status = 'Quoted'",
                        (once,)).fetchone()["id"]
    conn.close()
    # the editor offers the requote list on the requote event and changes it
    assert '<option value="Request changed" selected>' in body
    client.post("/enquiries/{}/events/{}".format(once, event), data={
        "at": "2026-07-25", "status": "New", "reason": "Rates expired", "action": "save"})
    page = client.post("/enquiries/{}/events/{}".format(once, lost), data={
        "at": "2026-07-10", "status": "Lost", "reason": "Lower rate", "action": "save"},
        follow_redirects=True)
    assert b"is not a Lost reason" in page.data
    conn = db.open_db(path)
    assert conn.execute("SELECT reason FROM status_event WHERE id = ?", (event,)).fetchone()[0] == "Rates expired"
    conn.close()


def test_lost_from_the_page_puts_the_closed_reason_on_the_event(web):
    client, path, fresh, once = web
    client.post("/enquiries/{}/status".format(once), data={
        "status": "Lost", "at": "2026-07-30", "closed_reason": "Price too high"}, follow_redirects=True)
    conn = db.open_db(path)
    assert events(conn, once)[-1] == ("Lost", "status", "Price too high")
    assert conn.execute("SELECT closed_reason FROM enquiry WHERE id = ?", (once,)).fetchone()[0] == "Price too high"
    conn.close()
    body = client.get("/settings").data.decode("utf-8")
    assert 'name="requote_reasons"' in body and "Re-quote reasons" in body


# ------------------------------------------------------------ the report --

# test_requotes_by_reason_counts_rounds_after_the_first: removed in batch 17a - the month report no longer carries re-quotes by reason or the Requote reasons sheet; the reasons stay on the cases


# test_the_report_xlsx_has_the_requote_sheet: removed in batch 17a - the month report no longer carries re-quotes by reason or the Requote reasons sheet; the reasons stay on the cases

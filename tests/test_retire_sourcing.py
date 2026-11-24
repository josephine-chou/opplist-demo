"""Sourcing retired (2026-09-24): a case is New until a quote goes out, then
Quoted. No picker offers Sourcing, nothing writes it, re-quote puts a case
back on New (kind requote, schema v29), the retire-sourcing backfill moves
the cases left on it, and the history that says Sourcing stays as it was."""

from __future__ import annotations

import re

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, intake, propose, retire_sourcing, schema_migrations, suggestions, tracking
from opplist import status as status_mod
from opplist.web import create_app
from test_outbound_direction import partner_question
from test_outlook import conn_with_parties  # noqa: F401  (fixture)

TODAY = "2026-09-24"


def view(conn, enquiry_id):
    return conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (enquiry_id,)).fetchone()


def _sourcing_case(repo, opp_no, party, received, sourcing_at, **extra):
    """A case left on Sourcing the way the live ones were: a New on the
    received date, then a manual Sourcing event."""
    case = add_enquiry(repo, opp_no, party, received=received, **extra)
    repo.insert("status_event", {"enquiry_id": case, "at": sourcing_at, "status": "Sourcing",
                                 "source": "manual", "note": "by hand"})
    repo.update("enquiry", case, {"status": "Sourcing", "status_confirmed": 1})
    return case


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "retire.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    agent = add_party(repo, "Tolvex", country="Chile")
    vendor = add_party(repo, "Seavio", country="USA")
    ids = {"agent": agent, "vendor": vendor}
    # no vendor request at all
    ids["bare"] = _sourcing_case(repo, "OPP-1701", agent, "2026-09-01", "2026-09-02")
    # a vendor asked, no reply: R2 keeps chasing them
    ids["asked"] = _sourcing_case(repo, "OPP-1702", agent, "2026-09-03", "2026-09-04")
    ids["vr"] = repo.insert("vendor_request", {"enquiry_id": ids["asked"], "vendor_party_id": vendor,
                                                "asked_on": "2026-09-04"})
    # on hold and unconfirmed: both stay
    ids["held"] = _sourcing_case(repo, "OPP-1703", agent, "2026-09-05", "2026-09-08")
    repo.update("enquiry", ids["held"], {"hold_reason": "waiting on the PO", "status_confirmed": 0})
    # a quoted case with a pending suggestion that still proposes Sourcing
    ids["quoted"] = add_enquiry(repo, "OPP-1704", agent, received="2026-09-01")
    repo.set_status(ids["quoted"], "Quoted", "2026-09-10")
    ids["sugg"] = repo.insert("suggestion", {
        "enquiry_id": ids["quoted"], "opp_no": "OPP-1704", "kind": "status",
        "proposed_status": "Sourcing", "proposed_date": "2026-09-20",
        "note": propose.REQUOTE_NOTE, "state": "pending"})
    repo.commit()
    yield conn, path, ids
    conn.close()


# ------------------------------------------------------------- vocabulary --
def test_sourcing_is_no_open_status_but_still_sorts():
    assert status_mod.OPEN_STATUSES == ("New", "Quoted")
    assert "Sourcing" not in status_mod.ALL_STATUSES
    assert status_mod.STAGE_RANK["Sourcing"] == 2                       # old events still sort
    assert status_mod.from_legacy("Sent to Vendor").status == "New"
    assert status_mod.to_legacy("Sourcing") == "Sent to Vendor"          # an old row still exports


# ------------------------------------------------------ nothing writes it --
def test_a_vendor_request_we_send_leaves_the_case_new(conn_with_parties):
    conn, _ = conn_with_parties
    draft = intake.build(conn, partner_question())
    assert draft.direction == "asking" and draft.outbound is True
    assert draft.values["status"] == "New"
    assert draft.sources["status"] == intake.FROM_OUTBOUND


def test_propose_suggests_new_for_a_requote(repo):
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    case = add_enquiry(repo, "OPP-1711", party, received="2026-09-01")
    repo.set_status(case, "Quoted", "2026-09-05")
    repo.commit()
    source = open(propose.__file__, encoding="utf-8").read()
    assert 'proposed_status = "Sourcing"' not in source
    # accepting a Re-quoted suggestion writes the button's event (v30: on Quoted)
    from opplist import detect_followups
    sid = repo.insert("suggestion", {"enquiry_id": case, "opp_no": "OPP-1711", "kind": "status",
                                     "proposed_status": "Quoted", "proposed_date": "2026-09-12",
                                     "note": detect_followups.REQUOTED_NOTE, "state": "pending"})
    repo.commit()
    suggestions.accept(repo, sid)
    event = repo.conn.execute("SELECT status, kind FROM status_event WHERE enquiry_id = ? "
                              "ORDER BY id DESC LIMIT 1", (case,)).fetchone()
    assert tuple(event) == ("Quoted", "requote")
    assert view(repo.conn, case)["status"] == "Quoted"


def test_accepting_an_old_sourcing_suggestion_lands_on_new(repo):
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    case = add_enquiry(repo, "OPP-1712", party, received="2026-09-01")
    sid = repo.insert("suggestion", {"enquiry_id": case, "opp_no": "OPP-1712", "kind": "status",
                                     "proposed_status": "Sourcing", "proposed_date": "2026-09-12",
                                     "state": "pending"})
    repo.commit()
    suggestions.accept(repo, sid)
    assert repo.conn.execute("SELECT COUNT(*) FROM status_event WHERE status = 'Sourcing'").fetchone()[0] == 0


# ---------------------------------------------------------- re-quote rounds --
def test_an_old_requote_on_new_opens_a_round(repo):
    """Batch 8's shape (a requote event on New; v30 writes Quoted): read
    as an open round until the Quoted that answers it."""
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    case = add_enquiry(repo, "OPP-1721", party, received="2026-07-01")
    repo.set_status(case, "Quoted", "2026-07-10")
    repo.set_status(case, "New", "2026-07-20", note="Re-quote", kind="requote")
    repo.commit()
    row = view(repo.conn, case)
    assert (row["status"], row["in_requote"], row["quote_rounds"]) == ("New", 1, 1)
    # a later New (retire-sourcing's own, a hand-set New) does not end the round
    repo.set_status(case, "New", "2026-07-22", note=status_mod.SOURCING_RETIRED_NOTE)
    repo.add_note(case, "2026-07-23", "asked Seavio again")
    repo.commit()
    assert view(repo.conn, case)["in_requote"] == 1
    # the Quoted that answers it does
    repo.set_status(case, "Quoted", "2026-07-25")
    repo.commit()
    row = view(repo.conn, case)
    assert (row["status"], row["in_requote"], row["quote_rounds"]) == ("Quoted", 0, 2)


def test_a_same_day_old_requote_wins_and_closing_ends_the_round(repo):
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    case = add_enquiry(repo, "OPP-1722", party, received="2026-07-01")
    repo.set_status(case, "Quoted", "2026-07-10")
    repo.set_status(case, "New", "2026-07-10", note="Re-quote", kind="requote")   # batch 8's shape
    repo.commit()
    assert (view(repo.conn, case)["status"], view(repo.conn, case)["in_requote"]) == ("New", 1)
    repo.set_status(case, "Lost", "2026-07-30", closed_reason="Price too high")
    repo.commit()
    assert view(repo.conn, case)["in_requote"] == 0


def test_the_upgrade_rebuilds_v_enquiry_only(tmp_path):
    path = tmp_path / "old.db"
    conn = db.open_db(path)
    conn.execute("CREATE VIEW keep_me AS SELECT 1 AS one")               # stands in for 'location'
    conn.execute("UPDATE setting SET value = '28' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    notes = []
    conn = db.open_db(path, upgrade_notes=notes)
    try:
        assert any(n.startswith("restore point opplist_before_schema{}_".format(
            schema_migrations.CURRENT_VERSION)) for n in notes)
        assert "v_enquiry rebuilt" in " ".join(notes)
        assert conn.execute("SELECT one FROM keep_me").fetchone()[0] == 1
        assert schema_migrations.current_version(conn) == schema_migrations.CURRENT_VERSION
        assert "requote" in conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'v_enquiry'").fetchone()[0]
    finally:
        conn.close()


# ----------------------------------------------------------------- backfill --
def test_the_dry_run_lists_and_writes_nothing(world):
    conn, _, ids = world
    before = conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
    report = retire_sourcing.run(conn, dry_run=True, today=TODAY)
    assert [c[0] for c in report.cases] == ["OPP-1701", "OPP-1702", "OPP-1703"]
    assert report.cases[1][2] == "Seavio (no reply)"
    assert [s[0] for s in report.suggestions] == [ids["sugg"]]
    assert report.moved == [] and report.dismissed == []
    assert conn.execute("SELECT COUNT(*) FROM enquiry WHERE status = 'Sourcing'").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0] == before
    assert "dry run: nothing written" in report.lines()[0]


def test_apply_moves_them_to_new_and_keeps_the_history(world):
    conn, _, ids = world
    old_events = conn.execute("SELECT id, at, status, note, voided FROM status_event "
                              "WHERE status = 'Sourcing' ORDER BY id").fetchall()
    report = retire_sourcing.run(conn, dry_run=False, today=TODAY)
    assert report.moved == ["OPP-1701", "OPP-1702", "OPP-1703"] and report.dismissed == [ids["sugg"]]
    for key in ("bare", "asked", "held"):
        assert view(conn, ids[key])["status"] == "New"
        event = conn.execute("SELECT at, status, kind, note FROM status_event WHERE enquiry_id = ? "
                             "ORDER BY id DESC LIMIT 1", (ids[key],)).fetchone()
        assert tuple(event) == (TODAY, "New", "status", "Sourcing retired, back to New")
        logged = conn.execute("SELECT old_value, new_value FROM change_log WHERE table_name = 'enquiry' "
                              "AND row_id = ? AND field = 'status' ORDER BY id DESC LIMIT 1", (ids[key],)).fetchone()
        assert tuple(logged) == ("Sourcing", "New")
    # the old Sourcing events are exactly as they were
    assert conn.execute("SELECT id, at, status, note, voided FROM status_event "
                        "WHERE status = 'Sourcing' ORDER BY id").fetchall() == old_events
    # hold and unconfirmed stay; the vendor request is untouched
    held = view(conn, ids["held"])
    assert (held["hold_reason"], held["status_confirmed"]) == ("waiting on the PO", 0)
    assert conn.execute("SELECT replied_on, voided FROM vendor_request WHERE id = ?",
                        (ids["vr"],)).fetchone()[:] == (None, 0)
    # the suggestion is dismissed with a note that keeps what it said
    sugg = conn.execute("SELECT state, note FROM suggestion WHERE id = ?", (ids["sugg"],)).fetchone()
    assert sugg["state"] == "dismissed"
    assert sugg["note"] == propose.REQUOTE_NOTE + " | " + retire_sourcing.DISMISS_NOTE
    assert conn.execute("SELECT COUNT(*) FROM change_log WHERE table_name = 'suggestion' "
                        "AND row_id = ?", (ids["sugg"],)).fetchone()[0] >= 2
    # a second run finds nothing
    again = retire_sourcing.run(conn, dry_run=False, today=TODAY)
    assert again.cases == [] and again.suggestions == []


def test_a_case_sourced_today_is_left_for_another_day(world):
    conn, _, ids = world
    conn.execute("UPDATE status_event SET at = ? WHERE enquiry_id = ? AND status = 'Sourcing'",
                 (TODAY, ids["bare"]))
    conn.commit()
    report = retire_sourcing.run(conn, dry_run=False, today=TODAY)
    assert "OPP-1701" not in report.moved and report.left[0][0] == "OPP-1701"
    assert view(conn, ids["bare"])["status"] == "Sourcing"


def test_the_cli_takes_a_restore_point_before_apply(world, capsys):
    conn, path, _ = world
    conn.close()
    from opplist.__main__ import main
    assert main(["retire-sourcing", "--db", str(path)]) == 0
    out = capsys.readouterr().out
    assert "cases in Sourcing                  : 3" in out and "restore point" not in out
    restore = path.parent / "rp"
    assert main(["retire-sourcing", "--db", str(path), "--apply", "--restore-dir", str(restore)]) == 0
    out = capsys.readouterr().out
    assert "restore point" in out and "cases moved to New                 : 3" in out
    assert list(restore.glob("opplist_before_retire-sourcing_*.db"))


# ------------------------------------------------------------------ tracking --
def test_moved_cases_stay_on_tracking(world):
    """Batch 8 follow-up: a case moved back to New is our turn - R1 when
    nobody was asked, R2 while a vendor is silent; it never falls off."""
    conn, _, ids = world
    retire_sourcing.run(conn, dry_run=False, today=TODAY)
    r1 = {i.enquiry_id for i in tracking.rule_new_no_action(conn, TODAY, 1)}
    assert ids["bare"] in r1                                           # no vendor request
    assert ids["held"] not in r1                                       # left unconfirmed: R4's
    r2 = {i.enquiry_id for i in tracking.rule_vendor_silent(conn, TODAY, 2)}
    assert ids["asked"] in r2 and ids["asked"] not in r1               # R2 keeps chasing Seavio
    assert tracking.new_cases_off_every_card(conn, TODAY) == []
    # once confirmed, it is R1's like the others
    conn.execute("UPDATE enquiry SET status_confirmed = 1 WHERE id = ?", (ids["held"],))
    assert ids["held"] in {i.enquiry_id for i in tracking.rule_new_no_action(conn, TODAY, 1)}


# --------------------------------------------------------------------- pages --
def _client(path):
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client()


def _options(body):
    return re.findall(r'<option value="Sourcing"', body)


def test_no_picker_offers_sourcing(world):
    conn, path, ids = world
    retire_sourcing.run(conn, dry_run=False, today=TODAY)
    # a Replied row on Tracking needs a quoted case with a reply note
    conn.close()
    client = _client(path)
    for url in ("/enquiries/new", "/enquiries", "/enquiries?view=all", "/tracking", "/settings",
                "/enquiries/{}".format(ids["quoted"])):
        body = client.get(url).get_data(as_text=True)
        assert not _options(body), url
        assert 'value="quick:Sourcing"' not in body, url


def test_the_forms_refuse_sourcing(world):
    conn, path, ids = world
    conn.close()
    client = _client(path)
    client.post("/enquiries/{}/status".format(ids["quoted"]), data={"status": "Sourcing", "at": TODAY})
    client.post("/tracking/act", data={"do": "quick:Sourcing", "enquiry_id": ids["quoted"], "at": TODAY})
    conn = db.open_db(path)
    try:
        assert view(conn, ids["quoted"])["status"] == "Quoted"
        assert conn.execute("SELECT COUNT(*) FROM status_event WHERE enquiry_id = ? AND status = 'Sourcing'",
                            (ids["quoted"],)).fetchone()[0] == 0
    finally:
        conn.close()


def test_the_tracking_requote_button_writes_the_requote_event(world):
    conn, path, ids = world
    conn.close()
    client = _client(path)
    client.post("/tracking/act", data={"do": "quick:requote", "rule": "R3", "enquiry_id": ids["quoted"], "at": TODAY})
    conn = db.open_db(path)
    try:
        row = view(conn, ids["quoted"])
        assert (row["status"], row["in_requote"], row["quote_rounds"]) == ("Quoted", 0, 2)
    finally:
        conn.close()


def test_an_old_sourcing_event_is_still_on_the_timeline(world):
    conn, path, ids = world
    retire_sourcing.run(conn, dry_run=False, today=TODAY)
    conn.close()
    body = _client(path).get("/enquiries/{}".format(ids["bare"])).get_data(as_text=True)
    assert '<span class="pill s-sourcing">Sourcing</span>' in body
    assert "Sourcing retired, back to New" in body
    # its edit row keeps Sourcing as its own value, and only there
    assert _options(body) == ['<option value="Sourcing"']
    assert 'value="Sourcing" selected' in body


def test_the_dashboard_ring_has_no_sourcing_bucket(world):
    conn, _, ids = world
    from opplist import dashboard
    retire_sourcing.run(conn, dry_run=False, today=TODAY)
    dash = dashboard.build(conn, today=TODAY)
    assert [p[0] for p in dash.pipeline["parts"]] == ["New", "Quoted"]
    assert dash.pipeline["on_us"] == 3                                  # New counts as ours


# test_tracking_cards_have_a_plain_border_and_title_case_titles: FX4 rewrote it in
# tests/test_fx4_rewritten.py (the Replied card is called Waiting on us)

"""The two closed vocabularies and the command that moves the old cases
onto them (2026-09-18).

    Lost    = there was a shipment and we did not get it.
    Dropped = this row should not exist.
"""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, fix_closed_reasons, status as status_mod


# ------------------------------------------------------ the two lists -----
def test_the_lists_are_two_and_do_not_overlap(conn):
    lost = status_mod.closed_reasons(conn, "Lost")
    dropped = status_mod.closed_reasons(conn, "Dropped")
    cancelled = status_mod.closed_reasons(conn, "Cancelled")
    # C1 (v38): a shipment that did not go ahead is Cancelled, not Lost
    assert "Unable to handle" in lost and "Shipment cancelled" not in lost
    assert cancelled == ["Shipment cancelled", "Order not confirmed", "Postponed with no new date", "Other"]
    assert dropped == ["Duplicate of OPP-xxxx", "Registered in error"]
    assert not set(lost) & set(dropped) and not set(cancelled) & set(dropped)
    # no status given: every list, Lost first, for a form with nothing to go on
    assert status_mod.closed_reasons(conn) == lost + [r for r in cancelled if r not in lost] + dropped


def test_a_reason_belongs_to_one_status_only(conn):
    assert status_mod.reason_belongs(conn, "Lost", "Unable to handle")
    assert not status_mod.reason_belongs(conn, "Lost", "Registered in error")
    assert status_mod.reason_belongs(conn, "Dropped", "Registered in error")
    assert not status_mod.reason_belongs(conn, "Dropped", "Unable to handle")
    # a real case number is the 'OPP-xxxx' entry with the number filled in
    assert status_mod.reason_belongs(conn, "Dropped", "Duplicate of OPP-1412")
    assert not status_mod.reason_belongs(conn, "Lost", "Duplicate of OPP-1412")
    # a blank reason is the form's business, not this function's
    assert status_mod.reason_belongs(conn, "Lost", None)


def test_a_numbered_duplicate_still_exports_as_one_word():
    """The pipeline knows nine strings; the case number lives in
    the reason, not in the export."""
    assert status_mod.to_legacy("Dropped", closed_reason="Duplicate of OPP-1412") \
        == "Duplicate"
    assert status_mod.to_legacy("Dropped", closed_reason="Registered in error") \
        == "Registered in error"
    assert status_mod.to_legacy("Lost", closed_reason="Unable to handle") == "Lost"


def test_a_new_reason_joins_the_list_of_its_own_status(conn):
    status_mod.ensure_closed_reason(conn, "Never quoted", "Lost")
    assert "Never quoted" in status_mod.closed_reasons(conn, "Lost")
    assert "Never quoted" not in status_mod.closed_reasons(conn, "Dropped")
    # matching ignores case, so nothing is added twice
    assert status_mod.ensure_closed_reason(conn, "unable TO handle") == "Unable to handle"
    assert len(status_mod.closed_reasons(conn, "Lost")) == \
        len(status_mod.DEFAULT_REASONS_LOST) + 1


# ------------------------------------------------- fix-closed-reasons -----
@pytest.fixture
def cases(repo):
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    ids = {}
    ids["kept"] = add_enquiry(repo, "OPP-1237", party, received="2026-07-01")

    # closed as Dropped because we could not handle it: there was a shipment
    ids["handle"] = add_enquiry(repo, "OPP-1231", party, received="2026-07-07")
    repo.set_status(ids["handle"], "Dropped", "2026-07-07",
                    closed_reason="Unable to Handle")
    # a duplicate whose note says which case it duplicates
    ids["dupe"] = add_enquiry(repo, "OPP-1305", party, received="2026-07-02")
    repo.set_status(ids["dupe"], "Dropped", "2026-07-03",
                    note="Duplicate of OPP-1237", closed_reason="Duplicate")
    # a duplicate whose note never said
    ids["tbc"] = add_enquiry(repo, "OPP-1306", party, received="2026-07-02")
    repo.set_status(ids["tbc"], "Dropped", "2026-07-03", closed_reason="Duplicate")
    # the old wording of a Lost reason
    ids["price"] = add_enquiry(repo, "OPP-1277", party, received="2026-07-04")
    repo.set_status(ids["price"], "Lost", "2026-07-05", closed_reason="Price")
    # already right, and one nobody can judge but Iris
    ids["error"] = add_enquiry(repo, "OPP-1315", party, received="2026-07-06")
    repo.set_status(ids["error"], "Dropped", "2026-07-06",
                    closed_reason="Registered in error")
    ids["blank"] = add_enquiry(repo, "OPP-1337", party, received="2026-07-08")
    repo.set_status(ids["blank"], "Dropped", "2026-07-09")
    repo.commit()
    return repo, ids


def reasons(conn):
    return {r["opp_no"]: (r["status"], r["closed_reason"]) for r in
            conn.execute("SELECT opp_no, status, closed_reason FROM enquiry")}


def test_the_dry_run_says_what_it_would_do_and_writes_nothing(cases):
    repo, ids = cases
    before = reasons(repo.conn)
    report = fix_closed_reasons.run(repo.conn, dry_run=True)
    assert reasons(repo.conn) == before
    text = " ".join(report.lines())
    assert "OPP-1231: Dropped 'Unable to Handle' -> Lost 'Unable to handle'" in text
    assert "OPP-1305: 'Duplicate' -> 'Duplicate of OPP-1237'" in text
    assert "OPP-1277: 'Price' -> 'Price too high'" in text
    assert "dry run" in text


def test_unable_to_handle_becomes_lost_on_its_own_closing_date(cases):
    repo, ids = cases
    fix_closed_reasons.run(repo.conn, dry_run=False)
    assert reasons(repo.conn)["OPP-1231"] == ("Lost", "Unable to handle")
    event = repo.conn.execute(
        "SELECT at, status, source, note FROM status_event WHERE enquiry_id = ? "
        "ORDER BY id DESC LIMIT 1", (ids["handle"],)).fetchone()
    assert (event["at"], event["status"], event["source"]) == (
        "2026-07-07", "Lost", "import")
    assert "Reclassified" in event["note"]
    logged = repo.conn.execute(
        "SELECT source FROM change_log WHERE table_name = 'enquiry' AND row_id = ? "
        "AND field = 'status' AND new_value = 'Lost'", (ids["handle"],)).fetchone()
    assert logged["source"] == "import"


def test_a_guessed_closing_date_stays_a_guess(cases):
    """The migrated Dropped event was dated by the workbook's Date Received
    and carries approx = 1. The Lost event that replaces it must too, or the
    export gains a closing date the original file never had."""
    repo, ids = cases
    event = repo.conn.execute(
        "SELECT id FROM status_event WHERE enquiry_id = ? AND status = 'Dropped'",
        (ids["handle"],)).fetchone()
    repo.update("status_event", event["id"], {"approx": 1})
    repo.commit()

    fix_closed_reasons.run(repo.conn, dry_run=False)
    lost = repo.conn.execute(
        "SELECT at, approx FROM status_event WHERE enquiry_id = ? "
        "AND status = 'Lost'", (ids["handle"],)).fetchone()
    assert (lost["at"], lost["approx"]) == ("2026-07-07", 1)
    row = repo.conn.execute(
        "SELECT closed_on, closed_on_any FROM v_enquiry WHERE id = ?",
        (ids["handle"],)).fetchone()
    assert row["closed_on"] is None and row["closed_on_any"] == "2026-07-07"


def test_an_earlier_run_that_lost_the_flag_is_put_right(cases):
    """The first live run wrote the new event without the flag; running it
    again compares the two events and fixes it, once."""
    repo, ids = cases
    event = repo.conn.execute(
        "SELECT id FROM status_event WHERE enquiry_id = ? AND status = 'Dropped'",
        (ids["handle"],)).fetchone()
    repo.update("status_event", event["id"], {"approx": 1})
    repo.commit()
    fix_closed_reasons.run(repo.conn, dry_run=False)
    lost = repo.conn.execute(
        "SELECT id FROM status_event WHERE enquiry_id = ? AND status = 'Lost'",
        (ids["handle"],)).fetchone()
    repo.update("status_event", lost["id"], {"approx": 0})       # as it was
    repo.commit()

    report = fix_closed_reasons.run(repo.conn, dry_run=False)
    assert any("marked approximate" in line for line in report.renamed)
    assert repo.conn.execute(
        "SELECT approx FROM status_event WHERE id = ?", (lost["id"],)).fetchone()[0] == 1
    assert not fix_closed_reasons.run(repo.conn, dry_run=False).renamed


def test_a_duplicate_takes_the_number_from_its_own_history(cases):
    repo, ids = cases
    report = fix_closed_reasons.run(repo.conn, dry_run=False)
    assert reasons(repo.conn)["OPP-1305"] == ("Dropped", "Duplicate of OPP-1237")
    # and the one that never said which case is left for a person to fill in
    assert reasons(repo.conn)["OPP-1306"] == ("Dropped", "Duplicate of (TBC)")
    assert any("OPP-1306" in line for line in report.tbc)
    logged = repo.conn.execute(
        "SELECT source FROM change_log WHERE table_name = 'enquiry' "
        "AND field = 'closed_reason' AND new_value = 'Duplicate of OPP-1237' "
        "AND row_id = ?", (ids["dupe"],)).fetchall()
    assert [r["source"] for r in logged] == ["import"]


def test_the_old_wordings_move_and_the_rest_is_left_alone(cases):
    repo, ids = cases
    report = fix_closed_reasons.run(repo.conn, dry_run=False)
    rows = reasons(repo.conn)
    assert rows["OPP-1277"] == ("Lost", "Price too high")
    assert rows["OPP-1315"] == ("Dropped", "Registered in error")
    # no reason at all: nothing is invented, it is listed instead
    assert rows["OPP-1337"] == ("Dropped", None)
    assert report.no_reason == ["OPP-1337 (Dropped)"]
    # every reason now belongs to the status that carries it
    for opp_no, (status, reason) in rows.items():
        assert status_mod.reason_belongs(repo.conn, status, reason), opp_no


def test_running_it_twice_changes_nothing_the_second_time(cases):
    repo, ids = cases
    fix_closed_reasons.run(repo.conn, dry_run=False)
    after = reasons(repo.conn)
    changes = repo.conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
    again = fix_closed_reasons.run(repo.conn, dry_run=False)
    assert reasons(repo.conn) == after
    assert repo.conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0] == changes
    assert not again.moved and not again.renamed and not again.duplicates


def test_nothing_is_ever_deleted(cases):
    repo, ids = cases
    before = repo.conn.execute("SELECT COUNT(*) FROM enquiry").fetchone()[0]
    events = repo.conn.execute("SELECT COUNT(*) FROM status_event").fetchone()[0]
    fix_closed_reasons.run(repo.conn, dry_run=False)
    assert repo.conn.execute("SELECT COUNT(*) FROM enquiry").fetchone()[0] == before
    assert repo.conn.execute(
        "SELECT COUNT(*) FROM status_event").fetchone()[0] >= events

"""An editable timeline, and the two ways a case turns out not to be one.

An event that was recorded wrong is corrected or struck out, never
deleted; a duplicate keeps its number and stops counting.
"""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, report, status as status_mod


@pytest.fixture
def case(repo):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    kept = add_enquiry(repo, "OPP-1361", tolvex, received="2026-08-25",
                       origin_loc="MIA", dest_loc="SAI", service_mode="Air")
    dupe = add_enquiry(repo, "OPP-1319", tolvex, received="2026-08-26",
                       origin_loc="MIA", dest_loc="SAI", service_mode="Air")
    repo.commit()
    return repo, {"kept": kept, "dupe": dupe}


def events(conn, enquiry_id):
    return conn.execute(
        "SELECT * FROM status_event WHERE enquiry_id = ? ORDER BY id",
        (enquiry_id,)).fetchall()


# ------------------------------------------------------------- editing ----
def test_an_event_can_be_corrected_and_the_change_is_logged(case):
    repo, ids = case
    event = events(repo.conn, ids["kept"])[0]
    repo.edit_status_event(event["id"], {"at": "2026-08-20",
                                         "status": "Quoted",
                                         "note": "wrong date, corrected"})
    repo.commit()
    row = repo.conn.execute("SELECT * FROM status_event WHERE id = ?",
                            (event["id"],)).fetchone()
    assert (row["at"], row["status"]) == ("2026-08-20", "Quoted")
    logged = {r["field"] for r in repo.conn.execute(
        "SELECT field FROM change_log WHERE table_name = 'status_event' "
        "AND row_id = ?", (event["id"],))}
    assert {"at", "status", "note"} <= logged


def test_correcting_the_last_event_moves_the_case_with_it(case):
    repo, ids = case
    event = events(repo.conn, ids["kept"])[0]
    repo.edit_status_event(event["id"], {"status": "Quoted"})
    repo.commit()
    assert repo.conn.execute("SELECT status FROM enquiry WHERE id = ?",
                             (ids["kept"],)).fetchone()["status"] == "Quoted"


# ------------------------------------------------------------- voiding ----
def test_a_voided_event_is_kept_but_counts_for_nothing(case):
    repo, ids = case
    repo.set_status(ids["kept"], "Won", "2026-08-30")
    repo.commit()
    won = events(repo.conn, ids["kept"])[-1]
    before = repo.conn.execute(
        "SELECT closed_on, last_event_on, status FROM v_enquiry WHERE id = ?",
        (ids["kept"],)).fetchone()
    assert before["closed_on"] == "2026-08-30"

    repo.void_status_event(won["id"])
    repo.commit()
    after = repo.conn.execute(
        "SELECT closed_on, closed_on_any, last_event_on, status "
        "FROM v_enquiry WHERE id = ?", (ids["kept"],)).fetchone()
    assert after["closed_on"] is None and after["closed_on_any"] is None
    assert after["last_event_on"] == "2026-08-25"      # back to the New event
    assert after["status"] == "New"                    # the case follows
    # The event itself is still there, struck out.
    row = repo.conn.execute("SELECT voided FROM status_event WHERE id = ?",
                            (won["id"],)).fetchone()
    assert row["voided"] == 1


def test_a_voided_event_can_be_put_back(case):
    repo, ids = case
    repo.set_status(ids["kept"], "Won", "2026-08-30")
    repo.commit()
    won = events(repo.conn, ids["kept"])[-1]
    repo.void_status_event(won["id"])
    repo.void_status_event(won["id"], voided=False)
    repo.commit()
    row = repo.conn.execute(
        "SELECT closed_on, status FROM v_enquiry WHERE id = ?",
        (ids["kept"],)).fetchone()
    assert (row["closed_on"], row["status"]) == ("2026-08-30", "Won")


def test_nothing_is_ever_deleted(case):
    repo, ids = case
    before = repo.conn.execute(
        "SELECT COUNT(*) AS n FROM status_event").fetchone()["n"]
    event = events(repo.conn, ids["kept"])[0]
    repo.void_status_event(event["id"])
    repo.commit()
    assert repo.conn.execute(
        "SELECT COUNT(*) AS n FROM status_event").fetchone()["n"] == before


# ----------------------------------------------------------- duplicates ----
def test_marking_a_duplicate_drops_it_and_ties_it_to_the_kept_case(case):
    repo, ids = case
    repo.mark_duplicate(ids["dupe"], ids["kept"], "2026-09-09")
    repo.commit()
    row = repo.conn.execute(
        "SELECT status, closed_reason, group_id, request_group "
        "FROM v_enquiry WHERE id = ?", (ids["dupe"],)).fetchone()
    # the reason names the case it duplicates (2026-09-18)
    assert (row["status"], row["closed_reason"]) == ("Dropped",
                                                    "Duplicate of OPP-1361")
    assert row["group_id"] == ids["kept"]
    kept = repo.conn.execute("SELECT request_group FROM v_enquiry WHERE id = ?",
                             (ids["kept"],)).fetchone()
    assert row["request_group"] == kept["request_group"]
    event = events(repo.conn, ids["dupe"])[-1]
    assert "Duplicate of OPP-1361" in event["note"]
    assert event["at"] == "2026-09-09"


def test_a_duplicate_keeps_its_number(case):
    repo, ids = case
    repo.mark_duplicate(ids["dupe"], ids["kept"], "2026-09-09")
    repo.commit()
    assert repo.conn.execute("SELECT opp_no FROM enquiry WHERE id = ?",
                             (ids["dupe"],)).fetchone()["opp_no"] == "OPP-1319"


def test_registered_in_error(case):
    repo, ids = case
    repo.mark_registered_in_error(ids["dupe"], "2026-09-09")
    repo.commit()
    row = repo.conn.execute(
        "SELECT status, closed_reason FROM enquiry WHERE id = ?",
        (ids["dupe"],)).fetchone()
    assert (row["status"], row["closed_reason"]) == ("Dropped",
                                                     "Registered in error")


def test_neither_counts_in_the_month_report(case):
    repo, ids = case
    month = "2026-08"
    before = report.inbound(repo.conn, month).rows[-1]
    assert before[0] == "Total" and before[1] == 2      # two request groups

    repo.mark_duplicate(ids["dupe"], ids["kept"], "2026-09-09")
    repo.commit()
    after = report.inbound(repo.conn, month).rows[-1]
    assert after[1] == 1                                # only the kept one
    assert report.mode(repo.conn, month).rows[-1][4] == 1


def test_a_mis_registration_does_not_count_either(case):
    repo, ids = case
    repo.mark_registered_in_error(ids["dupe"], "2026-09-09")
    repo.commit()
    assert report.inbound(repo.conn, "2026-08").rows[-1][1] == 1


def test_neither_counts_as_an_outcome(case):
    repo, ids = case
    repo.set_status(ids["dupe"], "Won", "2026-08-30")
    repo.commit()
    assert report.outcome(repo.conn, "2026-08", "2026-09-09").rows[-1][1] == 1

    repo.mark_duplicate(ids["dupe"], ids["kept"], "2026-09-09")
    repo.commit()
    assert report.outcome(repo.conn, "2026-08", "2026-09-09").rows[-1][1] == 0


def test_the_export_says_which_kind_of_not_a_case_it_was(case):
    repo, ids = case
    repo.mark_duplicate(ids["dupe"], ids["kept"], "2026-09-09")
    repo.mark_registered_in_error(ids["kept"], "2026-09-09")
    repo.commit()
    rows = {r["opp_no"]: status_mod.row_to_legacy(r) for r in repo.conn.execute(
        "SELECT * FROM v_enquiry")}
    assert rows["OPP-1319"] == "Duplicate"
    assert rows["OPP-1361"] == "Registered in error"


def test_neither_is_in_the_live_set_sort_inbox_uses(case):
    """Sort-Inbox: New / Sent to Vendor / Quoted - Awaiting Reply /
    In Progress / Shipped / '' are live. These two are not, so no mail is
    proposed for them."""
    live = {"New", "Sent to Vendor", "Quoted - Awaiting Reply",
            "In Progress / Shipped", ""}
    for reason in ("Duplicate of OPP-1361", "Registered in error"):
        assert status_mod.to_legacy("Dropped", closed_reason=reason) not in live


def test_the_reasons_are_offered_on_the_form(conn):
    """Two lists since 2026-09-18: a dropped row should not exist, a lost
    one is a shipment we did not get."""
    dropped = db.get_setting_list(conn, "closed_reasons_dropped")
    lost = db.get_setting_list(conn, "closed_reasons_lost")
    assert dropped == ["Duplicate of OPP-xxxx", "Registered in error"]
    assert "Unable to handle" in lost and "Shipment cancelled" not in lost          # Cancelled since v38
    assert "Shipment cancelled" in db.get_setting_list(conn, "closed_reasons_cancelled")
    assert lost[-1] == "Other"
    assert not set(dropped) & set(lost)


# ------------------------------------------------- thread key ---
def test_a_typed_subject_gets_a_thread_key(case):
    repo, ids = case
    enquiry_id = repo.create_enquiry(
        {"opp_no": "OPP-1500", "requester_party_id": 1,
         "received_on": "2026-09-09",
         "source_subject": "RE: PRICING // MEM - GYE // FCL"},
        status_at="2026-09-09")
    repo.commit()
    assert repo.conn.execute(
        "SELECT thread_key FROM enquiry WHERE id = ?", (enquiry_id,)
    ).fetchone()["thread_key"] == "PRICINGMEMGYEFCL"


def test_a_key_that_came_from_outlook_is_left_alone(case):
    repo, ids = case
    enquiry_id = repo.create_enquiry(
        {"opp_no": "OPP-1501", "requester_party_id": 1,
         "received_on": "2026-09-09", "source_subject": "PRICING // X",
         "thread_key": "FROMTHEMAIL"}, status_at="2026-09-09")
    repo.commit()
    assert repo.conn.execute(
        "SELECT thread_key FROM enquiry WHERE id = ?", (enquiry_id,)
    ).fetchone()["thread_key"] == "FROMTHEMAIL"


def test_editing_the_subject_recalculates_the_key(case):
    repo, ids = case
    repo.save_enquiry(ids["kept"], {
        "source_subject": "PRICING // LAX - VAP"})
    repo.commit()
    first = repo.conn.execute("SELECT thread_key FROM enquiry WHERE id = ?",
                              (ids["kept"],)).fetchone()["thread_key"]
    assert first == "PRICINGLAXVAP"

    repo.save_enquiry(ids["kept"], {
        "source_subject": "RE: PRICING // ATL - SCL // AIR"})
    repo.commit()
    assert repo.conn.execute(
        "SELECT thread_key FROM enquiry WHERE id = ?", (ids["kept"],)
    ).fetchone()["thread_key"] == "PRICINGATLSCLAIR"


def test_saving_something_else_does_not_touch_the_key(case):
    repo, ids = case
    repo.save_enquiry(ids["kept"], {"source_subject": "PRICING // LAX - VAP"})
    repo.commit()
    repo.save_enquiry(ids["kept"], {"commodity": "Steel coils"})
    repo.commit()
    assert repo.conn.execute(
        "SELECT thread_key FROM enquiry WHERE id = ?", (ids["kept"],)
    ).fetchone()["thread_key"] == "PRICINGLAXVAP"


def test_a_case_with_no_subject_gets_no_key(case):
    repo, ids = case
    repo.save_enquiry(ids["kept"], {"commodity": "Steel coils"})
    repo.commit()
    assert repo.conn.execute(
        "SELECT thread_key FROM enquiry WHERE id = ?", (ids["kept"],)
    ).fetchone()["thread_key"] is None


def test_the_upgrade_fills_in_what_was_already_there(tmp_path):
    """schema v7: cases with a subject but no key get one, once."""
    from opplist import db as db_module

    path = tmp_path / "old.db"
    conn = db_module.open_db(path)
    repo = db_module.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    enquiry_id = repo.insert("enquiry", {
        "opp_no": "OPP-0001", "requester_party_id": party,
        "received_on": "2026-09-01",
        "source_subject": "RE: PRICING // BOS - BLR // DG"})
    conn.execute("UPDATE enquiry SET thread_key = NULL WHERE id = ?",
                 (enquiry_id,))
    conn.execute("UPDATE setting SET value = '6' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    notes = []
    conn = db_module.open_db(path, upgrade_notes=notes)
    assert conn.execute("SELECT thread_key FROM enquiry WHERE id = ?",
                        (enquiry_id,)).fetchone()["thread_key"] == \
        "PRICINGBOSBLRDG"
    assert any("thread_key filled in" in note for note in notes)
    conn.close()


def test_verify_agrees_with_a_struck_out_event(case):
    """The checker judges a case by the events that still count, as the app does."""
    from opplist import verify

    repo, ids = case
    repo.set_status(ids["kept"], "Won", "2026-08-30")
    repo.commit()
    won = events(repo.conn, ids["kept"])[-1]
    repo.void_status_event(won["id"])
    repo.commit()
    check = verify.check_events(repo.conn)
    assert check.ok, check.lines

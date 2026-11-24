"""On hold (schema v11): set from the Set status box, shown on the
case page and the list, released by the next status change or by hand,
skipped by tracking until the date, ignored by the report."""

from __future__ import annotations

import csv

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import export_analysis, monthly, schema_migrations, tracking


@pytest.fixture
def case(repo):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    case_id = add_enquiry(repo, "OPP-0001", tolvex, received="2026-08-01", status="Quoted")
    repo.commit()
    return repo, case_id


def events(repo, case_id):
    return [(r["kind"], r["note"]) for r in repo.conn.execute(
        "SELECT kind, note FROM status_event WHERE enquiry_id = ? ORDER BY id", (case_id,))]


def test_the_schema_has_the_two_columns(case):
    repo, _ = case
    assert schema_migrations.current_version(repo.conn) >= 12
    assert {"hold_reason", "hold_until"} <= {r[1] for r in repo.conn.execute("PRAGMA table_info(enquiry)")}


def test_a_hold_writes_the_columns_and_a_note_and_leaves_the_status(case):
    repo, case_id = case
    repo.set_hold(case_id, "customer travelling", "2026-09-10", until="2026-09-20")
    row = repo.conn.execute("SELECT status, hold_reason, hold_until FROM enquiry WHERE id = ?",
                            (case_id,)).fetchone()
    assert tuple(row) == ("Quoted", "customer travelling", "2026-09-20")
    assert events(repo, case_id)[-1] == ("note", "On hold: customer travelling, until 2026-09-20")
    repo.set_hold(case_id, "waiting on docs", "2026-09-11")
    assert repo.conn.execute("SELECT hold_until FROM enquiry WHERE id = ?", (case_id,)).fetchone()[0] is None
    assert events(repo, case_id)[-1] == ("note", "On hold: waiting on docs")
    with pytest.raises(ValueError):
        repo.set_hold(case_id, "  ", "2026-09-11")


def test_a_status_change_releases_the_hold_but_a_note_does_not(case):
    repo, case_id = case
    repo.set_hold(case_id, "customer travelling", "2026-09-10", until="2026-09-20")
    repo.add_note(case_id, "2026-09-11", "called them")
    assert repo.conn.execute("SELECT hold_reason FROM enquiry WHERE id = ?", (case_id,)).fetchone()[0]
    repo.set_status(case_id, "Won", "2026-09-12")
    row = repo.conn.execute("SELECT status, hold_reason, hold_until FROM enquiry WHERE id = ?",
                            (case_id,)).fetchone()
    assert tuple(row) == ("Won", None, None)
    assert ("note", "Hold released") in events(repo, case_id)
    assert repo.release_hold(case_id, "2026-09-13") is False        # nothing to release
    repo.set_hold(case_id, "again", "2026-09-13")
    assert repo.release_hold(case_id, "2026-09-14") is True
    assert events(repo, case_id)[-1] == ("note", "Hold released")


def test_tracking_skips_a_case_held_until_later_but_not_a_dated_out_or_undated_one(case):
    repo, case_id = case
    repo.conn.execute("UPDATE status_event SET at = '2026-07-01' WHERE enquiry_id = ?", (case_id,))
    repo.commit()

    def labels(today):
        return {item.label for s in tracking.run(repo.conn, today=today) for item in s.items}

    assert "OPP-0001" in labels("2026-09-11")               # quoted, no answer: chased
    repo.set_hold(case_id, "customer travelling", "2026-09-10", until="2026-09-20")
    repo.commit()
    assert "OPP-0001" not in labels("2026-09-11")           # held until later
    # the hold note counts as activity, so look well past the date
    assert "OPP-0001" in labels("2026-12-01")               # the date has passed
    repo.set_hold(case_id, "no date", "2026-09-10")
    repo.commit()
    assert "OPP-0001" in labels("2026-12-01")               # undated: as usual


# test_the_report_and_the_analysis_file_see_the_status_and_the_hold: moved in batch 17a to
# tests/test_monthly.py (rewritten on invented data; it read the removed 'Quoted, no decision
# yet' tile and was written on a real agent's name)

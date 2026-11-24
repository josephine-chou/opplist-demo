"""The two mappings between the workbook and the app."""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import status as status_mod

# Every Status string the workbook actually contains, and where it lands.
ROUND_TRIP = [
    ("New", "New", "New"),
    ("Sent to Vendor", "New", "New"),                  # Sourcing retired 2026-09-24
    ("Quoted - Awaiting Reply", "Quoted", "Quoted - Awaiting Reply"),
    ("Secured", "Won", "Secured"),
    ("In Progress / Shipped", "Won", "Secured"),
    ("Lost", "Lost", "Lost"),
    ("No Response", "Dropped", "No Response"),
    ("Unable to Handle", "Dropped", "Unable to Handle"),
    ("Legacy - Unknown", "Dropped", "Legacy - Unknown"),
    ("", "New", ""),
]


@pytest.mark.parametrize("old,new,exported", ROUND_TRIP)
def test_legacy_round_trip(old, new, exported):
    mapped = status_mod.from_legacy(old)
    assert mapped.status == new
    assert status_mod.to_legacy(
        mapped.status,
        confirmed=mapped.confirmed,
        legacy=mapped.legacy,
        closed_reason=mapped.closed_reason,
    ) == exported


def test_blank_status_is_unconfirmed_and_exports_blank():
    mapped = status_mod.from_legacy("")
    assert mapped.status == "New" and mapped.confirmed is False
    assert status_mod.to_legacy("New", confirmed=False) == ""
    assert status_mod.to_legacy("New", confirmed=True) == "New"


def test_legacy_flag_wins_over_status():
    for status in status_mod.ALL_STATUSES:
        assert status_mod.to_legacy(status, legacy=True) == "Legacy - Unknown"


def test_dropped_splits_on_closed_reason():
    assert status_mod.to_legacy("Dropped", closed_reason="Unable to Handle") \
        == "Unable to Handle"
    assert status_mod.to_legacy("Dropped", closed_reason="Price") == "No Response"
    assert status_mod.to_legacy("Dropped", closed_reason=None) == "No Response"


def test_open_statuses_stay_live_for_sort_inbox():
    """Sort-Inbox.ps1 treats this set as live; open cases must land inside it."""
    live = {"New", "Sent to Vendor", "Quoted - Awaiting Reply",
            "In Progress / Shipped", ""}
    for status in status_mod.OPEN_STATUSES:
        assert status_mod.to_legacy(status) in live


def test_unknown_old_status_is_treated_as_unconfirmed_new():
    mapped = status_mod.from_legacy("Something Else")
    assert mapped.status == "New" and mapped.confirmed is False


def test_won_records_the_awarded_vendor(repo):
    """Closing as Won asks whose rate was used and stores it."""
    seed_reference(repo)
    requester = add_party(repo, "Tolvex", country="Chile")
    vendor = add_party(repo, "Solstice", country="China")
    enquiry_id = repo.insert("enquiry", {
        "opp_no": "OPP-1401",
        "requester_party_id": requester,
        "received_on": "2026-09-01",
        "status": "Sourcing",
    })
    request_id = repo.insert("vendor_request", {
        "enquiry_id": enquiry_id,
        "vendor_party_id": vendor,
        "asked_on": "2026-09-02",
        "machine_status": "Quoted",
    })

    repo.update("vendor_request", request_id, {"awarded": 1})
    repo.update("enquiry", enquiry_id,
                {"status": "Won", "awarded_vendor_id": vendor})
    repo.insert("status_event", {
        "enquiry_id": enquiry_id, "at": "2026-09-05", "status": "Won"})

    row = repo.conn.execute(
        "SELECT * FROM v_enquiry WHERE id = ?", (enquiry_id,)).fetchone()
    assert row["status"] == "Won"
    assert row["awarded_vendor_id"] == vendor
    assert row["closed_on"] == "2026-09-05"
    assert status_mod.row_to_legacy(row) == "Secured"

    logged = repo.conn.execute(
        "SELECT field, old_value, new_value FROM change_log "
        "WHERE table_name = 'enquiry' AND row_id = ? AND field = 'status'",
        (enquiry_id,)).fetchone()
    assert (logged["old_value"], logged["new_value"]) == ("Sourcing", "Won")


def test_won_without_a_vendor_request_uses_a_note(repo):
    seed_reference(repo)
    requester = add_party(repo, "JoJo JJC", ptype="Office")
    enquiry_id = repo.insert("enquiry", {
        "opp_no": "OPP-1402",
        "requester_party_id": requester,
        "received_on": "2026-09-01",
        "status": "Won",
        "awarded_note": "own rate",
    })
    row = repo.conn.execute(
        "SELECT * FROM v_enquiry WHERE id = ?", (enquiry_id,)).fetchone()
    assert row["awarded_vendor_id"] is None
    assert row["awarded_note"] == "own rate"


# ------------------------------------------------ rate used (v8) --
def test_set_rate_used_takes_one_of_four_answers(repo):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    seavio = add_party(repo, "Seavio", ptype="Unknown", country="USA")
    office = add_party(repo, "JoJo JJC", ptype="Office", country="USA")
    case = add_enquiry(repo, "OPP-0001", tolvex, status="Won")
    request_id = repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": seavio,
                                                "asked_on": "2026-09-01", "machine_status": "Quoted"})
    repo.commit()

    repo.set_rate_used(case, "agent", vendor_request_id=request_id, note="best price")
    row = repo.conn.execute("SELECT rate_used, awarded_vendor_id, awarded_note FROM enquiry").fetchone()
    assert tuple(row) == ("agent", seavio, "best price")
    assert repo.conn.execute("SELECT awarded FROM vendor_request").fetchone()[0] == 1

    repo.set_rate_used(case, "office", party_id=office)
    row = repo.conn.execute("SELECT rate_used, awarded_vendor_id, awarded_note FROM enquiry").fetchone()
    assert tuple(row) == ("office", office, None)
    assert repo.conn.execute("SELECT awarded FROM vendor_request").fetchone()[0] == 0

    repo.set_rate_used(case, "own")
    assert tuple(repo.conn.execute("SELECT rate_used, awarded_vendor_id FROM enquiry").fetchone()) == ("own", None)
    repo.set_rate_used(case, "not_recorded")
    assert repo.conn.execute("SELECT rate_used FROM enquiry").fetchone()[0] == "not_recorded"
    with pytest.raises(ValueError):
        repo.set_rate_used(case, "lottery")
    with pytest.raises(ValueError):
        repo.set_rate_used(case, "agent", vendor_request_id=999)


def test_striking_out_a_vendor_request_drops_its_award(repo):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    seavio = add_party(repo, "Seavio", ptype="Unknown", country="USA")
    case = add_enquiry(repo, "OPP-0001", tolvex, status="Won")
    request_id = repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": seavio,
                                                "asked_on": "2026-09-01", "awarded": 1})
    repo.void_vendor_request(request_id)
    row = repo.conn.execute("SELECT voided, awarded FROM vendor_request").fetchone()
    assert tuple(row) == (1, 0)
    assert repo.conn.execute("SELECT asked_count FROM v_party_stats WHERE id = ?",
                             (seavio,)).fetchone()[0] == 0
    repo.void_vendor_request(request_id, voided=False)
    assert repo.conn.execute("SELECT voided FROM vendor_request").fetchone()[0] == 0

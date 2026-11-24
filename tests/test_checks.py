"""The consistency check. It lists; it never changes anything."""

from __future__ import annotations

import pytest

from conftest import add_party, seed_reference
from opplist import checks


def by_key(findings):
    return {f.key: f for f in findings}


@pytest.fixture
def clean(repo):
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    enquiry_id = repo.insert("enquiry", {
        "opp_no": "OPP-0001", "requester_party_id": party,
        "received_on": "2026-09-01", "origin_loc": "LAX", "dest_loc": "SAI",
        "status": "New"})
    repo.insert("status_event", {"enquiry_id": enquiry_id, "at": "2026-09-01",
                                 "status": "New"})
    repo.commit()
    return repo.conn


def test_a_tidy_database_reports_nothing(clean):
    findings = checks.run(clean, today="2026-09-08")
    assert [f.key for f in findings if not f.clean] == []
    assert all(f.count == 0 for f in findings)


# -------------------------------------------------- 9.5, the split you asked --
def test_closed_without_reason_separates_legacy_from_live(repo, clean):
    party = clean.execute("SELECT id FROM party LIMIT 1").fetchone()["id"]
    repo.insert("enquiry", {
        "opp_no": "OPP-0002", "requester_party_id": party, "status": "Lost",
        "received_on": "2026-09-01"})
    repo.insert("enquiry", {
        "opp_no": "OPP-0003", "requester_party_id": party, "status": "Dropped",
        "received_on": "2026-09-01", "legacy": 1})
    repo.insert("enquiry", {
        "opp_no": "OPP-0004", "requester_party_id": party, "status": "Lost",
        "received_on": "2026-09-01", "closed_reason": "Price"})
    repo.commit()

    found = by_key(checks.run(clean, today="2026-09-08"))
    live = found["closed_without_reason"]
    legacy = found["closed_without_reason_legacy"]
    assert live.count == 1
    assert [r["opp_no"] for r in live.rows] == ["OPP-0002"]
    assert legacy.count == 1
    assert [r["opp_no"] for r in legacy.rows] == ["OPP-0003"]
    assert legacy.informational, "legacy blanks are expected, not a problem"
    assert not live.informational


def test_the_check_writes_nothing(repo, clean):
    party = clean.execute("SELECT id FROM party LIMIT 1").fetchone()["id"]
    repo.insert("enquiry", {
        "opp_no": "OPP-0002", "requester_party_id": party, "status": "Lost",
        "received_on": "2026-09-01"})
    repo.commit()
    before = clean.execute(
        "SELECT closed_reason, system_notes FROM enquiry WHERE opp_no = 'OPP-0002'"
    ).fetchone()
    checks.run(clean, today="2026-09-08")
    after = clean.execute(
        "SELECT closed_reason, system_notes FROM enquiry WHERE opp_no = 'OPP-0002'"
    ).fetchone()
    assert tuple(before) == tuple(after)
    assert clean.execute(
        "SELECT COUNT(*) AS n FROM change_log").fetchone()["n"] == \
        clean.execute("SELECT COUNT(*) AS n FROM change_log").fetchone()["n"]


# ------------------------------------------------------------ the others --
def test_malformed_keys_are_listed(clean):
    # Written straight past the repository on purpose: this is the check that
    # catches a key that got in some other way.
    clean.execute("INSERT INTO location (key) VALUES (' mia ')")
    clean.execute("INSERT INTO party (key, type, country) "
                  "VALUES (' Spaced ', 'Agent', 'Chile')")
    clean.commit()
    found = by_key(checks.run(clean, today="2026-09-08"))["malformed_keys"]
    assert found.count == 2
    assert {r["table_name"] for r in found.rows} == {"location", "party"}


def test_a_duplicate_party_cannot_be_created_in_the_first_place(clean):
    """ux_party_key_ci is the real defence; the check is the second line."""
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        clean.execute("INSERT INTO party (key, type, country) "
                      "VALUES ('TOLVEX', 'Agent', 'Chile')")
    clean.rollback()
    assert by_key(checks.run(clean, today="2026-09-08"))["duplicate_parties"].count == 0


def test_duplicate_parties_are_reported_if_the_index_ever_goes(clean):
    clean.execute("DROP INDEX ux_party_key_ci")
    clean.execute("INSERT INTO party (key, type, country) "
                  "VALUES ('TOLVEX', 'Agent', 'Chile')")
    clean.commit()
    found = by_key(checks.run(clean, today="2026-09-08"))["duplicate_parties"]
    assert found.count == 1
    assert found.rows[0]["n"] == 2
    assert "Tolvex" in found.rows[0]["keys"] and "TOLVEX" in found.rows[0]["keys"]


def test_stale_open_cases_use_the_last_event(repo, clean):
    party = clean.execute("SELECT id FROM party LIMIT 1").fetchone()["id"]
    old = repo.insert("enquiry", {
        "opp_no": "OPP-0002", "requester_party_id": party, "status": "Quoted",
        "received_on": "2026-01-01"})
    repo.insert("status_event", {"enquiry_id": old, "at": "2026-01-05",
                                 "status": "Quoted"})
    repo.commit()
    found = by_key(checks.run(clean, today="2026-09-08"))["stale_open_cases"]
    assert [r["opp_no"] for r in found.rows] == ["OPP-0002"]


def test_a_legacy_case_is_never_called_stale(repo, clean):
    party = clean.execute("SELECT id FROM party LIMIT 1").fetchone()["id"]
    repo.insert("enquiry", {
        "opp_no": "OPP-0003", "requester_party_id": party, "status": "New",
        "received_on": "2025-01-01", "legacy": 1})
    repo.commit()
    assert by_key(checks.run(clean, today="2026-09-08"))["stale_open_cases"].count == 0


def test_won_without_an_award_only_counts_cases_that_asked_a_vendor(repo, clean):
    party = clean.execute("SELECT id FROM party LIMIT 1").fetchone()["id"]
    asked = repo.insert("enquiry", {
        "opp_no": "OPP-0002", "requester_party_id": party, "status": "Won",
        "received_on": "2026-09-01"})
    repo.insert("vendor_request", {"enquiry_id": asked, "vendor_party_id": party,
                                   "asked_on": "2026-09-02"})
    repo.insert("enquiry", {
        "opp_no": "OPP-0003", "requester_party_id": party, "status": "Won",
        "received_on": "2026-09-01"})
    repo.commit()
    found = by_key(checks.run(clean, today="2026-09-08"))["won_without_award"]
    assert [r["opp_no"] for r in found.rows] == ["OPP-0002"]


def test_to_confirm_locations_and_placeholder_parties(repo, clean):
    party = clean.execute("SELECT id FROM party LIMIT 1").fetchone()["id"]
    repo.add_location("SAN VICENTE", country="Chile")
    ghost = add_party(repo, "(TBC)", ptype="Unknown", country="Unknown",
                      placeholder=1)
    case = repo.insert("enquiry", {
        "opp_no": "OPP-0002", "requester_party_id": ghost, "status": "New",
        "received_on": "2026-09-01", "origin_loc": "SAN VICENTE"})
    repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": ghost,
                                   "asked_on": "2026-09-02"})
    repo.commit()
    found = by_key(checks.run(clean, today="2026-09-08"))
    assert found["to_confirm_locations"].count == 1
    assert found["placeholder_vendors"].count == 1
    # '(TBC)' is the requester we are still looking for: R5 chases it, so it
    # is listed as work, not as background (section 6 R5).
    assert found["requesters_to_be_confirmed"].count == 1
    assert found["requesters_to_be_confirmed"].informational is False
    assert found["requesters_given_up_on"].count == 0


def test_unconfirmed_status_is_informational(repo, clean):
    party = clean.execute("SELECT id FROM party LIMIT 1").fetchone()["id"]
    repo.insert("enquiry", {
        "opp_no": "OPP-0002", "requester_party_id": party, "status": "New",
        "received_on": "2026-09-01", "status_confirmed": 0})
    repo.commit()
    found = by_key(checks.run(clean, today="2026-09-08"))["unconfirmed_status"]
    assert found.count == 1 and found.informational


def test_render_marks_problems_and_notes_differently(repo, clean):
    party = clean.execute("SELECT id FROM party LIMIT 1").fetchone()["id"]
    repo.insert("enquiry", {
        "opp_no": "OPP-0002", "requester_party_id": party, "status": "Lost",
        "received_on": "2026-09-01"})
    repo.insert("enquiry", {
        "opp_no": "OPP-0003", "requester_party_id": party, "status": "New",
        "received_on": "2026-09-01", "status_confirmed": 0})
    repo.commit()
    text = "\n".join(checks.render(checks.run(clean, today="2026-09-08")))
    assert "[LOOK]" in text and "[note]" in text and "[ok  ]" in text
    assert "1 check(s) worth looking at" in text


def test_a_requester_given_up_on_is_not_chased(repo, clean):
    """'Unknown' is a decision to stop looking; '(TBC)' is work in progress."""
    tbc = add_party(repo, "(TBC)", ptype="Unknown", country="Unknown",
                    placeholder=1)
    unknown = add_party(repo, "Unknown", ptype="Unknown", country="Unknown",
                        placeholder=1)
    repo.insert("enquiry", {"opp_no": "OPP-0002", "requester_party_id": tbc,
                            "status": "Sourcing", "received_on": "2026-09-01"})
    for opp_no in ("OPP-0003", "OPP-0004"):
        repo.insert("enquiry", {"opp_no": opp_no, "requester_party_id": unknown,
                                "status": "Lost", "received_on": "2026-08-01",
                                "closed_reason": "Price"})
    repo.commit()
    found = by_key(checks.run(clean, today="2026-09-08"))
    assert found["requesters_to_be_confirmed"].count == 1
    assert [r["opp_no"] for r in found["requesters_to_be_confirmed"].rows] == \
        ["OPP-0002"]
    assert found["requesters_given_up_on"].count == 2
    assert found["requesters_given_up_on"].informational


def test_a_legacy_case_on_the_placeholder_is_not_chased(repo, clean):
    """Nothing the previous owner left behind is going to be identified now."""
    tbc = add_party(repo, "(TBC)", ptype="Unknown", country="Unknown",
                    placeholder=1)
    repo.insert("enquiry", {"opp_no": "OPP-0002", "requester_party_id": tbc,
                            "status": "Dropped", "received_on": "2025-01-01",
                            "legacy": 1})
    repo.commit()
    assert by_key(checks.run(clean, today="2026-09-08"))[
        "requesters_to_be_confirmed"].count == 0


def test_a_vendor_that_is_the_requester_is_an_error(repo, clean):
    party = clean.execute("SELECT id FROM party LIMIT 1").fetchone()["id"]
    other = add_party(repo, "Seavio", ptype="Unknown", country="USA")
    case = repo.insert("enquiry", {
        "opp_no": "OPP-0002", "requester_party_id": party, "status": "Sourcing",
        "received_on": "2026-09-01"})
    repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": party,
                                   "asked_on": "2026-09-02", "source": "tracker"})
    repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": other,
                                   "asked_on": "2026-09-02", "source": "tracker"})
    repo.commit()
    found = by_key(checks.run(clean, today="2026-09-08"))["vendor_is_requester"]
    assert found.count == 1 and not found.informational
    assert found.rows[0]["opp_no"] == "OPP-0002"

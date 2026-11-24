"""import-tracker: the buy side of a case.

Pinned on a handful of rows shaped like the real file, because the real file
changes every time the PowerShell scan runs.
"""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, import_tracker, intake


def row(**overrides):
    values = {
        "RowKey": "OPP-0001|seavio.example", "OppNo": "OPP-0001",
        "Subject": "PRICING // HOU - GYE", "Lane": "HOU - GYE",
        "Mode": "FCL", "VendorDomain": "seavio.example", "Vendor": "Seavio",
        "Contacts": "Roberto Marino; Luis Peralta",
        "Emails": "rmarino@seavio.example; lperalta@seavio.example",
        "DateAsked": "2026-08-25 09:35", "DateReplied": "2026-08-25 19:33",
        "HoursToReply": "10", "TheirRef": "", "MachineStatus": "Acknowledged",
        "ReplyGist": "Received, will revert shortly.",
    }
    values.update(overrides)
    return values


@pytest.fixture
def ready(repo):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    add_enquiry(repo, "OPP-0001", tolvex, received="2026-08-20",
                status="Sourcing")
    add_enquiry(repo, "OPP-0002", tolvex, received="2026-08-21")
    repo.commit()
    return repo


def run(repo, rows, thread_map=()):
    """The same three steps import_tracker.run() takes, on an open repo."""
    report = import_tracker.TrackerReport()
    import_tracker.import_rows(repo, rows, report)
    import_tracker.clear_unearned_awards(repo, report)
    import_tracker.apply_thread_map(repo, list(thread_map), report)
    repo.commit()
    return report


# ------------------------------------------------------------- the rows ----
def test_a_row_becomes_a_vendor_request_on_its_case(ready):
    repo = ready
    report = run(repo, [row()])
    assert report.inserted == 1 and report.parties_created == ["Seavio"]
    request = repo.conn.execute(
        "SELECT v.*, p.key FROM vendor_request v JOIN party p "
        "ON p.id = v.vendor_party_id").fetchone()
    assert request["asked_on"] == "2026-08-25"          # the date, not the time
    assert request["replied_on"] == "2026-08-25"
    assert request["machine_status"] == "Acknowledged"
    assert request["gist"] == "Received, will revert shortly."
    assert request["quoted"] == 0 and request["source"] == "tracker"
    assert request["row_key"] == "OPP-0001|seavio.example"
    assert request["key"] == "Seavio"


def test_quoted_is_read_off_the_machine_status(ready):
    run(ready, [row(MachineStatus="Quoted")])
    assert ready.conn.execute("SELECT quoted FROM vendor_request").fetchone()[0] == 1


def test_a_row_without_a_case_number_is_counted_and_skipped(ready):
    report = run(ready, [row(OppNo="", RowKey="thread|seavio.example")])
    assert report.no_opp_no == 1 and report.inserted == 0
    assert ready.conn.execute("SELECT COUNT(*) FROM vendor_request").fetchone()[0] == 0
    assert report.parties_created == []                  # no case, no vendor made


def test_a_case_number_that_does_not_exist_is_named(ready):
    report = run(ready, [row(OppNo="OPP-1267")])
    assert report.unknown_opp_no == ["OPP-1267"]
    assert report.inserted == 0


def test_running_twice_updates_what_the_tracker_knows_and_nothing_else(ready):
    repo = ready
    run(repo, [row(DateReplied="", MachineStatus="Asked, no reply", ReplyGist="")])
    request = repo.conn.execute(
        "SELECT id, vendor_party_id FROM vendor_request").fetchone()
    repo.set_status(1, "Won", "2026-08-28",
                    awarded_vendor_id=request["vendor_party_id"])
    repo.award_vendor(1, request["id"])
    repo.update("vendor_request", request["id"], {"notes": "ours"})
    repo.commit()

    report = run(repo, [row(DateReplied="2026-08-27 10:00",
                            MachineStatus="Quoted", ReplyGist="USD 2,615")])
    assert (report.inserted, report.updated) == (0, 1)
    after = repo.conn.execute("SELECT * FROM vendor_request").fetchone()
    assert after["replied_on"] == "2026-08-27" and after["quoted"] == 1
    assert after["gist"] == "USD 2,615"
    assert after["awarded"] == 1 and after["notes"] == "ours"   # the app's, kept
    assert repo.conn.execute("SELECT COUNT(*) FROM vendor_request").fetchone()[0] == 1


def test_a_third_identical_run_changes_nothing(ready):
    run(ready, [row()])
    report = run(ready, [row()])
    assert (report.inserted, report.updated, report.unchanged) == (0, 0, 1)


# ------------------------------------------------------------- vendors -----
def test_the_domain_finds_a_party_the_app_already_knows(ready):
    repo = ready
    seavio = add_party(repo, "Seavio Transport", ptype="Unknown", country="USA")
    repo.insert("party_domain", {"domain": "seavio.example", "party_id": seavio})
    repo.commit()
    report = run(repo, [row(Vendor="Seavio (tracker spelling)")])
    assert report.parties_created == []
    assert repo.conn.execute("SELECT vendor_party_id FROM vendor_request"
                             ).fetchone()[0] == seavio


def test_the_name_finds_a_party_and_teaches_it_the_domain(ready):
    repo = ready
    seavio = add_party(repo, "Seavio", ptype="Unknown", country="USA")
    repo.commit()
    report = run(repo, [row()])
    assert report.parties_created == [] and report.domains_learned >= 1
    assert repo.conn.execute("SELECT party_id FROM party_domain "
                             "WHERE domain = 'seavio.example'").fetchone()[0] == seavio


def test_a_bare_domain_in_the_vendor_column_gets_a_readable_key(ready):
    report = run(ready, [row(Vendor="bluefin-usa.example", VendorDomain="bluefin-usa.example",
                             RowKey="OPP-0001|bluefin-usa.example")])
    assert report.parties_created == ["Bluefin-Usa"]


def test_two_subdomains_of_one_company_are_one_party(ready):
    report = run(ready, [
        row(Vendor="us.harborline.example", VendorDomain="us.harborline.example",
            RowKey="OPP-0001|us.harborline.example", Emails=""),
        row(Vendor="gsc.harborline.example", VendorDomain="gsc.harborline.example",
            RowKey="OPP-0002|gsc.harborline.example", OppNo="OPP-0002", Emails="")])
    assert report.parties_created == ["Harborline"]
    assert ready.conn.execute("SELECT COUNT(*) FROM party WHERE key = 'Harborline'"
                              ).fetchone()[0] == 1
    assert ready.conn.execute("SELECT COUNT(*) FROM party_domain "
                              "WHERE domain LIKE '%harborline.example'").fetchone()[0] == 2


def test_free_mail_is_nobody(ready):
    report = run(ready, [row(Vendor="gmail.com", VendorDomain="gmail.com",
                             RowKey="OPP-0001|gmail.com",
                             Emails="somebody@gmail.com")])
    assert report.no_vendor == 1 and report.parties_created == []
    assert ready.conn.execute("SELECT COUNT(*) FROM party WHERE key = 'Gmail'"
                              ).fetchone()[0] == 0


def test_contacts_come_along_once(ready):
    run(ready, [row()])
    run(ready, [row()])
    contacts = ready.conn.execute(
        "SELECT name, email FROM contact ORDER BY email").fetchall()
    assert [(c["name"], c["email"]) for c in contacts] == [
        ("Luis Peralta", "lperalta@seavio.example"),
        ("Roberto Marino", "rmarino@seavio.example")]


# -------------------------------------------------- the same key twice ----
def test_the_same_row_key_twice_in_one_file_is_one_ask(ready):
    report = run(ready, [
        row(DateAsked="2026-08-20 09:00", DateReplied="", MachineStatus="Asked, no reply"),
        row(DateAsked="2026-08-22 09:00", DateReplied="2026-08-23 10:00",
            MachineStatus="Quoted", ReplyGist="USD 1,745")])
    assert report.duplicate_row_keys == 1 and report.inserted == 1
    request = ready.conn.execute("SELECT * FROM vendor_request").fetchone()
    assert request["asked_on"] == "2026-08-20"          # the first ask
    assert request["replied_on"] == "2026-08-23"        # the last answer
    assert request["quoted"] == 1 and request["gist"] == "USD 1,745"


# ------------------------------------------------------ the thread map -----
def test_the_map_fills_a_missing_thread_key_when_it_is_unambiguous(ready):
    report = run(ready, [], thread_map=[
        {"ThreadKey": "PRICINGHOUGYE", "OppNo": "OPP-0001"},
        {"ThreadKey": "ONE", "OppNo": "OPP-0002"},
        {"ThreadKey": "TWO", "OppNo": "OPP-0002"}])
    assert report.thread_keys_set == 1 and report.thread_map_skipped == 1
    keys = {r["opp_no"]: r["thread_key"] for r in ready.conn.execute(
        "SELECT opp_no, thread_key FROM enquiry")}
    assert keys == {"OPP-0001": "PRICINGHOUGYE", "OPP-0002": None}


def test_the_map_never_overwrites_a_key_the_case_has(ready):
    ready.update("enquiry", 1, {"thread_key": "FROMTHEMAIL"})
    ready.commit()
    run(ready, [], thread_map=[{"ThreadKey": "OTHER", "OppNo": "OPP-0001"}])
    assert ready.conn.execute("SELECT thread_key FROM enquiry WHERE id = 1"
                              ).fetchone()[0] == "FROMTHEMAIL"


# --------------------------------------------------------------- keys ------
def test_suggested_keys():
    assert intake.suggested_party_key("seavio.example") == "Seavio"
    assert intake.suggested_party_key("bluefin-usa.example") == "Bluefin-Usa"
    assert intake.suggested_party_key("us.harborline.example") == "Harborline"
    assert intake.suggested_party_key("www.skyreach.example") == "Skyreach"
    assert intake.suggested_party_key("kestrellogistics-ph.example") == "Kestrellogistics-Ph"


# ------------------------------------------- the customer side of a thread --
def test_the_requesters_own_domain_is_not_a_vendor(ready):
    """The scan lists every outside party; the one who asked us is one of them."""
    repo = ready
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": 1})
    repo.commit()
    report = run(repo, [row(Vendor="Tolvex", VendorDomain="tolvex.example",
                            RowKey="OPP-0001|tolvex.example")])
    assert report.inserted == 0
    assert report.requester_side == [("OPP-0001", "Tolvex")]
    assert repo.conn.execute("SELECT COUNT(*) FROM vendor_request").fetchone()[0] == 0


def test_a_requester_contacts_domain_counts_too(ready):
    repo = ready
    repo.insert("contact", {"party_id": 1, "name": "Ana",
                            "email": "ana@tolvex-mail.example"})
    repo.commit()
    report = run(repo, [row(Vendor="Somebody", VendorDomain="tolvex-mail.example",
                            RowKey="OPP-0001|tolvex-mail.example")])
    assert report.requester_side == [("OPP-0001", "Somebody")]
    assert report.parties_created == []          # and no party is invented


def test_the_same_domain_on_another_case_is_still_a_vendor(ready):
    """Tolvex can be a vendor on a case somebody else asked about."""
    repo = ready
    other = add_party(repo, "QFS", country="Chile")      # a seeded country
    add_enquiry(repo, "OPP-0003", other, received="2026-08-22")
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": 1})
    repo.commit()
    report = run(repo, [row(OppNo="OPP-0003", Vendor="Tolvex",
                            VendorDomain="tolvex.example", RowKey="OPP-0003|tolvex.example")])
    assert report.inserted == 1 and report.requester_side == []


# ------------------------------------------------------- awarded is ours ---
def test_the_import_never_writes_awarded(ready):
    run(ready, [row(MachineStatus="Quoted")])
    assert ready.conn.execute("SELECT awarded FROM vendor_request").fetchone()[0] == 0
    assert "awarded" not in import_tracker.TRACKER_FIELDS


def test_an_unearned_award_on_a_tracker_row_is_cleared(ready):
    repo = ready
    run(repo, [row()])
    request_id = repo.conn.execute("SELECT id FROM vendor_request").fetchone()[0]
    repo.conn.execute("UPDATE vendor_request SET awarded = 1 WHERE id = ?",
                      (request_id,))
    repo.commit()
    report = run(repo, [row()])
    assert report.awarded_cleared == 1
    assert repo.conn.execute("SELECT awarded FROM vendor_request").fetchone()[0] == 0


def test_an_award_the_app_made_on_a_won_case_is_kept(ready):
    repo = ready
    run(repo, [row()])
    request = repo.conn.execute(
        "SELECT id, vendor_party_id FROM vendor_request").fetchone()
    repo.set_status(1, "Won", "2026-08-28", awarded_vendor_id=request["vendor_party_id"])
    repo.award_vendor(1, request["id"])
    repo.commit()
    report = run(repo, [row()])
    assert report.awarded_cleared == 0
    assert repo.conn.execute("SELECT awarded FROM vendor_request").fetchone()[0] == 1


# --------------------------------------- rows with no case number --
def test_a_row_with_no_case_number_finds_its_case_by_thread_key(ready):
    repo = ready
    repo.conn.execute("UPDATE enquiry SET thread_key = 'PRICINGHOUGYE' "
                      "WHERE opp_no = 'OPP-0002'")
    repo.commit()
    report = run(repo, [row(OppNo="", RowKey="PRICINGHOUGYE|seavio.example")])
    assert (report.matched_by_thread, report.no_opp_no) == (1, 0)
    got = repo.conn.execute(
        "SELECT e.opp_no FROM vendor_request v JOIN enquiry e ON e.id = v.enquiry_id"
    ).fetchone()
    assert got["opp_no"] == "OPP-0002"


def test_the_rowkey_prefix_finds_the_case_when_the_subject_was_cut(ready):
    """The scan cuts the key at 24 characters; the subject column is cut
    too. The prefix is enough when only one case starts that way."""
    repo = ready
    repo.conn.execute("UPDATE enquiry SET thread_key = "
                      "'W7730QRT229103BVXFCAAIRUSATOCEBUAIRPORT' "
                      "WHERE opp_no = 'OPP-0002'")
    repo.commit()
    report = run(repo, [row(OppNo="", RowKey="W7730QRT229103BVXFCAAIRU|seavio.example",
                            Subject="W7730 / QRT-2291- 03 - BVX - FCA AIR - USA TO CEBU AIRPO")])
    assert report.matched_by_thread == 1
    assert repo.conn.execute("SELECT enquiry_id FROM vendor_request").fetchone()[0] == 2


def test_a_row_with_no_case_number_finds_its_case_by_the_requester_domain(ready):
    """An outside address on the row that is Tolvex's, asked inside
    Tolvex's one case window: that is the case."""
    repo = ready
    tolvex = repo.conn.execute("SELECT id FROM party WHERE key = 'Tolvex'").fetchone()[0]
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": tolvex})
    repo.conn.execute("UPDATE enquiry SET received_on = '2026-07-01' WHERE opp_no = 'OPP-0001'")
    repo.commit()
    report = run(repo, [row(OppNo="", RowKey="SOMETHINGELSE|seavio.example",
                            Subject="Something else entirely",
                            Emails="rmarino@seavio.example; renata@tolvex.example",
                            DateAsked="2026-08-25 09:35")])
    assert (report.matched_by_requester, report.no_opp_no) == (1, 0)
    assert repo.conn.execute(
        "SELECT e.opp_no FROM vendor_request v JOIN enquiry e ON e.id = v.enquiry_id"
    ).fetchone()[0] == "OPP-0002"                      # received 08-21, in the window


def test_two_cases_in_the_window_leave_the_row_without_a_case(ready):
    repo = ready
    tolvex = repo.conn.execute("SELECT id FROM party WHERE key = 'Tolvex'").fetchone()[0]
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": tolvex})
    repo.commit()
    report = run(repo, [row(OppNo="", RowKey="SOMETHINGELSE|seavio.example",
                            Subject="Something else entirely",
                            Emails="rmarino@seavio.example; renata@tolvex.example",
                            DateAsked="2026-08-25 09:35")])
    assert (report.matched_by_requester, report.no_opp_no) == (0, 1)
    assert report.unmatched == [("Something else entirely", "2026-08-25", "Seavio")]
    assert repo.conn.execute("SELECT COUNT(*) FROM vendor_request").fetchone()[0] == 0
    assert any("still without a case (1)" in line for line in report.lines())


def test_a_thread_key_shared_by_two_cases_is_not_enough(ready):
    repo = ready
    repo.conn.execute("UPDATE enquiry SET thread_key = 'PRICINGHOUGYE'")
    repo.commit()
    report = run(repo, [row(OppNo="", RowKey="PRICINGHOUGYE|seavio.example")])
    assert (report.matched_by_thread, report.no_opp_no) == (0, 1)

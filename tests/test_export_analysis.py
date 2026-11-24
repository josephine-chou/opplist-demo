"""export-analysis: three flat CSV files, the app's own definitions."""

from __future__ import annotations

import csv

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import export_analysis

TODAY = "2026-09-10"


@pytest.fixture
def book(repo):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile", mfn_member=1)
    seavio = add_party(repo, "Seavio", ptype="Unknown", country="USA")
    office = add_party(repo, "JoJo JJC", ptype="Office", country="USA")
    directory = add_party(repo, "Belrix", country="Chile", source="directory", mfn_member=1,
                     notes="Agent directory (Chile)")
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": tolvex})
    won = add_enquiry(repo, "OPP-0001", tolvex, received="2026-08-03", status="Won",
                      origin_country="USA", dest_country="Chile", service_mode="FCL",
                      end_customer="Vidrios", commodity="Glass", rate_used="agent",
                      awarded_vendor_id=seavio)
    # add_enquiry opens the case with its final status; the timeline is New,
    # Quoted, Won so quoted_on / closed_on have something to read
    repo.conn.execute("UPDATE status_event SET status = 'New' WHERE enquiry_id = ?", (won,))
    repo.insert("status_event", {"enquiry_id": won, "at": "2026-08-10", "status": "Quoted"})
    repo.insert("status_event", {"enquiry_id": won, "at": "2026-08-20", "status": "Won"})
    repo.insert("vendor_request", {"enquiry_id": won, "vendor_party_id": seavio,
                                   "asked_on": "2026-08-05", "replied_on": "2026-08-06",
                                   "quoted": 1, "awarded": 1, "machine_status": "Quoted"})
    repo.insert("vendor_request", {"enquiry_id": won, "vendor_party_id": office,
                                   "asked_on": "2026-08-05", "voided": 1})
    add_enquiry(repo, "OPP-0002", office, received="2026-09-01", status="Sourcing",
                origin_country="China", dest_country="USA", service_mode="Air")
    dupe = add_enquiry(repo, "OPP-0003", tolvex, received="2026-09-02", status="Dropped",
                       closed_reason="Duplicate")
    repo.insert("party_event", {"party_id": directory, "at": "2026-01-05", "type": "outreach",
                                "summary": "Email sent", "source": "directory"})
    repo.insert("party_event", {"party_id": directory, "at": "2026-02-24", "type": "reply",
                                "summary": "replied (agent directory)", "source": "directory"})
    repo.commit()
    return repo


def read(path):
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")                 # UTF-8 BOM
    return list(csv.DictReader(raw.decode("utf-8-sig").splitlines()))


def test_the_three_files_are_written_with_their_columns(book, tmp_path):
    report = export_analysis.run(book.conn, out_dir=tmp_path, today=TODAY)
    assert (report.enquiries, report.vendor_requests, report.parties) == (2, 2, 4)
    enquiries = read(tmp_path / "enquiries.csv")
    assert list(enquiries[0].keys()) == list(export_analysis.ENQUIRY_COLUMNS)
    assert list(read(tmp_path / "vendor_requests.csv")[0].keys()) == \
        list(export_analysis.VENDOR_COLUMNS)
    assert list(read(tmp_path / "parties.csv")[0].keys()) == list(export_analysis.PARTY_COLUMNS)


def test_enquiries_carry_the_reports_definitions(book, tmp_path):
    export_analysis.run(book.conn, out_dir=tmp_path, today=TODAY)
    rows = {r["opp_no"]: r for r in read(tmp_path / "enquiries.csv")}
    assert "OPP-0003" not in rows                            # a duplicate is no case
    won = rows["OPP-0001"]
    assert (won["quoted_on"], won["closed_on"], won["days_to_quote"]) == \
        ("2026-08-10", "2026-08-20", "7")
    assert (won["outcome"], won["requester_type"], won["lane_latam"]) == \
        ("Won", "LATAM agent MFN", "Y")
    assert (won["direction"], won["mode"], won["rate_used"], won["awarded_vendor"]) == \
        ("US Export", "FCL", "Agent", "Seavio")
    assert (won["vendor_requests_sent"], won["vendor_replied"]) == ("1", "1")   # voided one out
    assert won["end_customer"] == "Vidrios" and won["requester_country"] == "Chile"
    sourcing = rows["OPP-0002"]
    assert (sourcing["outcome"], sourcing["requester_type"], sourcing["lane_latam"]) == \
        ("In progress", "own office", "N")
    assert sourcing["rate_used"] == "" and sourcing["days_to_quote"] == ""


def test_vendor_requests_one_row_each_including_the_struck_out(book, tmp_path):
    export_analysis.run(book.conn, out_dir=tmp_path, today=TODAY)
    rows = read(tmp_path / "vendor_requests.csv")
    seavio = next(r for r in rows if r["vendor"] == "Seavio")
    assert (seavio["opp_no"], seavio["vendor_type"], seavio["vendor_region"],
            seavio["vendor_mfn"], seavio["status"], seavio["awarded"], seavio["voided"]) == \
        ("OPP-0001", "Unknown", "North America", "N", "Quoted", "Y", "N")
    office = next(r for r in rows if r["vendor"] == "JoJo JJC")
    assert (office["voided"], office["replied_on"]) == ("Y", "")


def test_parties_count_both_directions_and_the_directory_trail(book, tmp_path):
    export_analysis.run(book.conn, out_dir=tmp_path, today=TODAY)
    rows = {r["party"]: r for r in read(tmp_path / "parties.csv")}
    tolvex = rows["Tolvex"]
    assert (tolvex["mfn"], tolvex["on_agent_directory"], tolvex["inbound_requests"],
            tolvex["inbound_last_on"], tolvex["inbound_won"], tolvex["first_seen_on"]) == \
        ("Y", "N", "2", "2026-09-02", "1", "2026-08-03")
    seavio = rows["Seavio"]
    assert (seavio["outbound_requests"], seavio["outbound_last_on"], seavio["outbound_replied"],
            seavio["outbound_awarded"]) == ("1", "2026-08-05", "1", "1")
    belrix = rows["Belrix"]
    assert (belrix["on_agent_directory"], belrix["directory_outreach_events"], belrix["directory_last_event_on"],
            belrix["first_seen_on"]) == ("Y", "2", "2026-02-24", "2026-01-05")
    assert rows["JoJo JJC"]["outbound_requests"] == "0"   # the struck-out one

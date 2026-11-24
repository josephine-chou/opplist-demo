"""The exact / approximate date split (schema v2) and the month report (7.3).

The report is pinned on a small book of cases: one per group, one per
outcome, so every bucket and every exclusion has a row that proves it.
"""

from __future__ import annotations

import datetime as dt

import openpyxl
import pytest

from conftest import add_party, seed_reference
from opplist import db, export_xlsx, report

TODAY = "2026-09-09"


@pytest.fixture
def cases(repo):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile", mfn_member=1)
    qfs = add_party(repo, "QFS", country="Chile", mfn_member=0)
    chi = add_party(repo, "JoJo JJC", ptype="Office", country="USA")
    klx = add_party(repo, "KLX", country="USA")
    seavio = add_party(repo, "Seavio", ptype="Unknown", country="USA")

    def case(opp_no, party, status, events, **extra):
        values = {"opp_no": opp_no, "requester_party_id": party,
                  "status": status, "received_on": "2026-08-03"}
        values.update(extra)
        enquiry_id = repo.insert("enquiry", values)
        for at, event_status, approx in events:
            repo.insert("status_event", {
                "enquiry_id": enquiry_id, "at": at, "status": event_status,
                "approx": approx, "source": "migration"})
        return enquiry_id

    # MFN agent, won with a real close date; two vendor quotes, one used.
    won = case("OPP-0001", tolvex, "Won",
               [("2026-08-03", "New", 0), ("2026-08-10", "Quoted", 0),
                ("2026-08-20", "Won", 0)], service_mode="FCL",
               origin_country="USA", dest_country="Chile", dest_loc="SAI",
               rate_used="agent", awarded_vendor_id=seavio)
    used = repo.insert("vendor_request", {
        "enquiry_id": won, "vendor_party_id": seavio, "asked_on": "2026-08-05",
        "replied_on": "2026-08-06", "machine_status": "Quoted", "awarded": 1})
    repo.insert("vendor_request", {
        "enquiry_id": won, "vendor_party_id": klx, "asked_on": "2026-08-05",
        "replied_on": "2026-08-07", "machine_status": "Quoted"})
    repo.update("enquiry", won, {"awarded_vendor_id": seavio})
    # own office, won but the close date was a guess.
    case("OPP-0002", chi, "Won",
         [("2026-08-03", "New", 0), ("2026-08-03", "Won", 1)],
         service_mode="Air", origin_country="China", dest_country="USA")
    # LATAM (non-MFN) agent, quoted a month ago and silent since.
    case("OPP-0003", qfs, "Quoted",
         [("2026-08-05", "New", 0), ("2026-08-05", "Quoted", 0)],
         service_mode="LCL", received_on="2026-08-05",
         origin_country="Brazil", dest_country="USA")
    # Other, still moving (an event last week).
    case("OPP-0004", klx, "New",
         [("2026-08-06", "New", 0), ("2026-09-05", "New", 0)],
         service_mode="Air", origin_country="USA", dest_country="Chile")
    # Other, never confirmed.
    case("OPP-0005", klx, "New", [("2026-08-07", "New", 1)],
         status_confirmed=0, service_mode="RORO")
    # Lost on price.
    case("OPP-0006", tolvex, "Lost",
         [("2026-08-08", "New", 0), ("2026-08-15", "Lost", 0)],
         closed_reason="Price too high", service_mode="FCL")
    # The shipment was cancelled: a shipment we did not get, so Lost.
    case("OPP-0007", tolvex, "Lost",
         [("2026-08-09", "New", 0), ("2026-08-16", "Lost", 0)],
         closed_reason="Shipment cancelled", service_mode="Air")
    # A duplicate: kept, numbered, and counted nowhere.
    case("OPP-0008", tolvex, "Dropped",
         [("2026-08-09", "New", 0), ("2026-09-01", "Dropped", 0)],
         closed_reason="Duplicate of OPP-0001", service_mode="Air")
    # Legacy: counted nowhere either.
    case("OPP-0009", qfs, "Dropped", [("2026-08-01", "Dropped", 1)],
         legacy=1, service_mode="FCL")
    # New with no event for six weeks: stale.
    case("OPP-0010", chi, "New",
         [("2026-08-01", "New", 0), ("2026-08-02", "New", 0)],
         received_on="2026-08-01", service_mode="FCL")
    # Another month entirely.
    case("OPP-0011", tolvex, "New", [("2026-07-20", "New", 0)],
         received_on="2026-07-20", service_mode="Air")
    repo.commit()
    return repo.conn


def section(built, prefix):
    return next(b for b in built.sections if b.title.startswith(prefix))


def rows_by_first(block):
    return {r[0]: r for r in block.rows}


# ------------------------------------------------ the date split (v2) -----
def test_exact_dates_ignore_the_approximate_events(cases):
    rows = {r["opp_no"]: r for r in cases.execute(
        "SELECT opp_no, closed_on, closed_on_any FROM v_enquiry")}
    assert rows["OPP-0001"]["closed_on"] == "2026-08-20"
    assert rows["OPP-0002"]["closed_on"] is None
    assert rows["OPP-0002"]["closed_on_any"] == "2026-08-03"


# ------------------------------------------------------------- groups -----
# test_the_four_groups_and_the_exclusions: removed in batch SC1; replaced by test_sc1_the_groups_and_the_exclusions below.


def test_a_own_office_in_latin_america_is_still_an_office(repo, conn):
    seed_reference(repo)
    scl = add_party(repo, "JoJo JQS", ptype="Office",
                    country="Chile", mfn_member=1)
    repo.insert("enquiry", {"opp_no": "OPP-0001", "requester_party_id": scl,
                            "received_on": "2026-08-03", "status": "New"})
    repo.commit()
    rows = rows_by_first(section(report.build(conn, "2026-08", TODAY), "1."))
    assert rows["Our offices"][1] == 1 and rows["MFN agents"][1] == 0


# ------------------------------------------------------------- top 5 ------
def test_top_lists_are_per_group_and_capped_at_five(cases):
    block = section(report.build(cases, "2026-08", TODAY), "2b.")
    mfn = [r for r in block.rows if r[0] == "MFN agents"]
    assert ("MFN agents", "Chile", 1) in mfn
    assert len(mfn) <= 5
    assert all(r[0] in report.GROUPS or r[0] == "All" for r in block.rows)
    # The month as a whole comes first: Chile 2 (0001, 0004), USA 2 (0002, 0003).
    overall = [r for r in block.rows if r[0] == "All"]
    assert overall[0][0] == "All" and block.rows[0][0] == "All"
    assert dict((r[1], r[2]) for r in overall) == {"Chile": 2, "USA": 2}


# --------------------------------------------------------------- mode -----
# test_mode_splits_ocean_from_air_and_other: removed in batch SC1; replaced by test_sc1_mode_counts_roro_as_ocean below.


# ------------------------------------------------------------ outcome -----
# test_every_outcome_bucket_has_its_case: replaced in batch C1 by tests/test_cancelled_counts.py (it pinned the old Lost / cancelled words).


def test_the_outcome_is_judged_on_the_date_given(cases):
    """Run in mid-August, the silent quote is still open."""
    rows = rows_by_first(section(report.build(cases, "2026-08", "2026-08-20"), "4."))
    total = dict(zip(report.OUTCOMES, rows["Total"][1:-1]))
    assert total["No response"] == 0
    assert total["Quoted, awaiting customer"] == 1      # OPP-0003, then
    assert total["In progress (not quoted)"] == 2         # 0004 and 0010


# ----------------------------------------------------------- outbound -----
# test_outbound_counts_the_months_vendor_requests: removed in batch SC1; replaced by test_sc1_outbound_counts_the_months_vendor_requests below.


# ----------------------------------------------------------- adoption -----
def test_adoption_is_awarded_over_quoted_on_won_cases(cases):
    built = report.build(cases, "2026-08", TODAY)
    rows = rows_by_first(section(built, "6a."))
    assert rows["MFN agents"][1:] == (2, 1, "50%")
    assert rows["Total"][1:] == (2, 1, "50%")
    used = dict((r[0], r[2]) for r in section(built, "6b.").rows)
    assert used == {"OPP-0001": "Agent rate: Seavio", "OPP-0002": "not answered yet"}


def test_only_an_agent_rate_counts_as_adopted(cases):
    """Won on our own rate: the vendor quotes on the case were not used."""
    cases.execute("UPDATE enquiry SET rate_used = 'own', awarded_vendor_id = NULL "
                  "WHERE opp_no = 'OPP-0001'")
    cases.commit()
    built = report.build(cases, "2026-08", TODAY)
    assert rows_by_first(section(built, "6a."))["Total"][1:] == (2, 0, "0%")
    used = dict((r[0], r[2]) for r in section(built, "6b.").rows)
    assert used["OPP-0001"] == "Own rate"
    cases.execute("UPDATE enquiry SET rate_used = 'office', awarded_vendor_id = "
                  "(SELECT id FROM party WHERE key = 'JoJo JJC') WHERE opp_no = 'OPP-0001'")
    cases.commit()
    built = report.build(cases, "2026-08", TODAY)
    assert dict((r[0], r[2]) for r in section(built, "6b.").rows)["OPP-0001"] == \
        "Office rate: JoJo JJC"


def test_a_struck_out_vendor_request_counts_for_nothing(cases):
    cases.execute("UPDATE vendor_request SET voided = 1 WHERE quoted = 1 AND awarded = 0")
    cases.commit()
    built = report.build(cases, "2026-08", TODAY)
    assert rows_by_first(section(built, "6a."))["Total"][1:] == (1, 1, "100%")
    assert rows_by_first(section(built, "5a."))["Total"][1] == 1


# -------------------------------------------------------------- trend -----
# test_the_trend_runs_from_january_and_outcomes_from_august: replaced in batch C1 by tests/test_cancelled_counts.py (it pinned the old Lost / cancelled words).


# test_the_trend_volume_includes_legacy_but_the_outcomes_do_not: replaced in batch C1 by tests/test_cancelled_counts.py (it pinned the old Lost / cancelled words).


# ------------------------------------------------------------ summary -----
def test_the_summary_is_built_from_the_numbers(cases):
    built = report.build(cases, "2026-08", TODAY)
    assert built.summary.startswith("2026-08: 8 inbound requests (8 rows)")
    assert "MFN agents 3" in built.summary
    assert "2 vendor requests sent" in built.summary
    assert "adoption 1 of 2 quotes (50%)" in built.summary


def test_definitions_say_what_the_numbers_mean(cases):
    built = report.build(cases, "2026-08", TODAY)
    text = " ".join(built.definitions)
    assert "Dropped" in text and "the row should not exist" in text
    assert "30 days" in text and TODAY in text


# ------------------------------------------------------------- export -----
def test_the_export_writes_the_three_sheets_from_the_same_report(cases, tmp_path):
    path = export_xlsx.export(cases, tmp_path / "out.xlsx",
                              months=["2026-07", "2026-08"])
    wb = openpyxl.load_workbook(path, read_only=True)
    try:
        assert {"Report", "Trend", "Definitions"} <= set(wb.sheetnames)
        first_column = [row[0] for row in wb["Report"].iter_rows(values_only=True)]
        assert "Month 2026-08" in first_column
        assert "1. Inbound requests" in first_column
        trend = [row[0] for row in wb["Trend"].iter_rows(values_only=True)]
        assert "2026-01" in trend and "2026-08" in trend
        definitions = [row[0] for row in wb["Definitions"].iter_rows(values_only=True)]
        assert any(line and "MFN agents" in str(line) for line in definitions)
    finally:
        wb.close()


# ------------------------------------------------------------ helpers -----
def test_month_helpers():
    assert report.month_bounds("2026-02") == ("2026-02-01", "2026-02-28")
    assert report.previous_month("2026-01") == "2025-12"
    assert report.next_month("2026-12") == "2027-01"
    assert report.months_between("2026-11", "2027-01") == [
        "2026-11", "2026-12", "2027-01"]
    assert report.default_months(dt.date(2026, 9, 8)) == ["2026-08", "2026-09"]


# ---------------------------------------- SC1: invented replacements -----
# Every company, place, date and number below is invented; cases from
# OPP-2961. SC1: report.py counts the report rule's cases (an Agent, a
# Office or a Direct Customer asked; Unidentified stays a line at
# 0), 'Non-LATAM agents' is 'Other agents', and RORO is Ocean.
@pytest.fixture
def book(repo):
    seed_reference(repo)
    harrow = add_party(repo, "Harrowgate Lines", country="Chile", mfn_member=1)
    pell = add_party(repo, "Pellbrook Freight", country="Chile", mfn_member=0)
    office = add_party(repo, "JoJo QRV", ptype="Office", country="USA")
    tannoch = add_party(repo, "Tannoch Cargo", country="USA")
    mire = add_party(repo, "Mirefield Haulage", ptype="Unknown", country="USA")
    n = [2961]

    def case(party, mode, status="New", **extra):
        values = {"opp_no": "OPP-{}".format(n[0]), "requester_party_id": party, "status": status,
                  "received_on": "2026-08-04", "service_mode": mode}
        n[0] += 1
        values.update(extra)
        return repo.insert("enquiry", values)

    won = case(harrow, "FCL", "Won", origin_country="USA", dest_country="Chile")
    case(harrow, "Air")
    case(harrow, "Truck")
    case(harrow, "Air", "Dropped", closed_reason="Duplicate of OPP-2961")       # counted nowhere
    case(pell, "LCL")
    case(pell, "FCL", "Dropped", legacy=1)                                      # legacy: counted nowhere
    case(office, "Air")
    case(office, "FCL")
    case(tannoch, "Air")
    case(tannoch, "RORO")
    case(mire, "Air")                                                           # Unknown: not counted
    for vendor in (tannoch, mire):
        repo.insert("vendor_request", {"enquiry_id": won, "vendor_party_id": vendor, "asked_on": "2026-08-05"})
    repo.commit()
    return repo.conn


def test_sc1_the_groups_and_the_exclusions(book):
    rows = rows_by_first(section(report.build(book, "2026-08", TODAY), "1."))
    assert rows["MFN agents"][1:] == (3, 3)
    assert rows["LATAM agents"][1:] == (1, 1)
    assert rows["Our offices"][1:] == (2, 2)
    assert rows["Other agents"][1:] == (2, 2)
    assert rows["Direct customers"][1:] == (0, 0)
    assert rows["Unidentified"][1:] == (0, 0)          # the Unknown requester is not counted
    assert rows["Other"][1:] == (2, 2)
    assert rows["Total"][1:] == (8, 8)


def test_sc1_mode_counts_roro_as_ocean(book):
    rows = rows_by_first(section(report.build(book, "2026-08", TODAY), "3."))
    # Air 3; Ocean FCL 2 + LCL 1 + RORO 1 = 4; Other the Truck = 1
    assert rows["Total"][1:] == (3, 4, 1, 8)


def test_sc1_outbound_counts_the_months_vendor_requests(book):
    built = report.build(book, "2026-08", TODAY)
    sent = rows_by_first(section(built, "5a."))
    assert sent["Total"][1] == 2
    assert sent["Other agents"][1] == 1
    assert sent["Unidentified"][1] == 1               # a vendor of type Unknown still counts here
    assert sent["Other"][1] == 2
    assert dict(section(built, "5b.").rows) == {"Mirefield Haulage": 1, "Tannoch Cargo": 1}
    assert dict(section(built, "5c.").rows) == {"Chile": 2}

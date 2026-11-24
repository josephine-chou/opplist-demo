"""The migration, exercised on a small workbook built to the same shape.

The real workbook is 386 cases and changes daily, so the rules are pinned
here on a sample that carries one row for every rule that matters.
"""

from __future__ import annotations

import openpyxl
import pytest

from opplist import db, export_csv, migrate_xlsx, verify
from opplist.export_xlsx import (AGENT_COLUMNS, COUNTRY_COLUMNS,
                                 LOCATION_COLUMNS, OPPORTUNITY_COLUMNS)

COUNTRIES = [
    ("Unknown", None, None, "Undefined"),
    ("USA", "US", "USA", "North America"),
    ("Chile", "CL", "CHL", "Latin America"),
    ("China", "CN", "CHN", "Asia"),
]

LOCATIONS = [
    # Entry, Official, Type, Country, Region(f), Status, Notes, CodeSys,
    # CodeOfficial, ParentCity, ISO2(f), Seaport, Airport
    ("LAX", "Los Angeles", "Airport", "USA", None, "OK", None, "IATA", "LAX",
     None, None, "N", "Y"),
    ("SAI", "San Antonio", "Seaport", "Chile", None, "OK", None, "UN/LOCODE",
     "CLSAI", None, None, "Y", "N"),
    ("NGB", "Ningbo", "Seaport", "China", None, "To Confirm", None,
     "UN/LOCODE", "CNNGB", None, None, "Y", "N"),
]

AGENTS = [
    # Agent, Full Name, Source Type, Country, Region(f), City, MFN No.,
    # Contact, Email, ...metrics..., Notes, Rank(f), MFN Member
    ("Tolvex", "Tolvex Chile SpA", "Agent", "Chile", None, "Santiago",
     "12345", "Ana; Luis", "ana@tolvex.example; luis@tolvex.example", "See tolvex.example", "Y"),
    ("JoJo JJZ", "JoJo Freight (JJZ)", "Office", "China", None,
     "Shenzhen", None, "Wei", "wei@jojofreight-cn.example", None, None),
    ("(TBC)", "To be confirmed", "Agent", "Unknown", None, None, None, None,
     None, "Placeholder - source not yet received.", None),
    ("COLIN", "direct client via Colin", None, "Unknown", None, None, None,
     None, None, "0901: placeholder for a direct client.", None),
    ("Ridgeway Parts", "Ridgeway Parts", "Agent", "USA", None, "Memphis",
     None, "Wade; Marisa",
     "wade@ridgewayparts.example; marisa@ridgewayparts.example", None, None),
    ("A Plus Kestrel", "A Plus Kestrel", "Agent", "Atlantis", None, None,
     None, "Supervisor / Tovin",
     "a@kestrellogistics-ph.example; b@kestrellogistics-ph.example", None, None),
    ("Free Mail Co", "Free Mail Co", "Agent", "USA", None, None, None,
     "Sam", "someone@gmail.com", None, None),
]

# Enquiry No., Date Received, Agent, Handled By, Service Mode, Container Type,
# Container Qty, Air kg, LCL cbm, Origin Country, Origin Detail, Origin Alt,
# Dest Country, Dest Detail, Dest Alt, Incoterms, Commodity, Special Handling,
# Status, Date Quoted, Quote Ref, Date Closed, Loss Reason, Qty raw, Remarks,
# System Notes
ENQUIRIES = [
    ("OPP-0001", "2026-08-03", "Tolvex", None, "FCL", "40HC", 3, None, None,
     "USA", "LAX", None, "Chile", "SAI", None, "EXW", "PET flakes",
     "Transload / Flatbed", "Secured", None, "JJQ-2026-311", "2026-08-20",
     None, "FCL:3 x 40'HQ", None, None),
    ("OPP-0002", "2026-08-05", "JoJo JJZ", None, "Air", None, None, 216.0,
     None, "China", "NGB", None, "USA", None, None, "FOB", "Machinery",
     "Client: Flexmold", "Quoted - Awaiting Reply", "2026-08-08", None,
     None, None, "Air:216", None, None),
    ("OPP-0003", "2026-08-06", "(TBC)", None, "LCL", None, None, None, None,
     "USA", "LAX", None, "Chile", "SAI", None, None, "Toys", "4 TEUs/month",
     "", None, None, None, None, "LCL:6.6", "watch this one", None),
    ("OPP-0004", "2026-08-07", "Ridgeway Parts", None, "FCL", None, None,
     None, None, "USA", None, None, "USA", "LAX", None, "FOB", "Oil filters",
     None, "Lost", None, None, None, None, None, None, None),
    ("OPP-0005", "2026-08-08", "COLIN", None, None, None, None, None, None,
     None, None, None, None, None, None, None, "PVC flooring", None,
     "Legacy - Unknown", None, None, None, None, None, None, None),
    ("OPP-0006", None, "Tolvex", None, "FCL", None, None, None, None, "China",
     "NGB", None, "USA", "LAX", None, None, "Steel", None, "Sent to Vendor",
     "2026-08-11", None, None, None, None, None, None),
    ("OPP-0007", "2026-08-09", "A Plus Kestrel", None, "Not specified", None,
     None, None, None, "USA", "LAX", None, "Chile", "SAI", None, None,
     "Samples", "DG", "Unable to Handle", None, None, None, None, None,
     None, None),
]

LISTS_ROWS = [
    ("Agent", "Air", "New", "Price", None, "20GP", 1, "Country", "Chile",
     "OOG", "EXW", None),
    ("Office", "LCL", "Sent to Vendor", "Transit Time", None, "40GP", 2,
     "City", "Peru", "Transload", "FOB", None),
    (None, "FCL", "Secured", "Brand new reason", None, "40HQ", 2, "Seaport",
     "Brazil", "Flatbed", "DAP", None),
]


def _build_workbook(path, enquiries=None):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    ws = wb.create_sheet("Opportunities")
    ws.append(OPPORTUNITY_COLUMNS)
    for row in (ENQUIRIES if enquiries is None else enquiries):
        # Lay the sample values out on the 41 real columns.
        line = [None] * 41
        (line[0], line[1], line[4], line[7], line[8], line[9], line[10],
         line[12], line[13], line[14], line[15], line[16], line[18], line[19],
         line[20], line[24], line[25], line[26], line[27], line[28], line[29],
         line[30], line[32], line[36], line[37], line[38]) = row
        ws.append(line)

    ws = wb.create_sheet("Lookup_Location")
    ws.append(LOCATION_COLUMNS)
    for row in LOCATIONS:
        ws.append(list(row))

    ws = wb.create_sheet("Agent_Master")
    ws.append(AGENT_COLUMNS)
    for row in AGENTS:
        line = [None] * 22
        (line[0], line[1], line[2], line[3], line[4], line[5], line[6],
         line[7], line[8], line[19], line[21]) = row
        ws.append(line)

    ws = wb.create_sheet("Country_List")
    ws.append(COUNTRY_COLUMNS)
    for row in COUNTRIES:
        ws.append(list(row))

    ws = wb.create_sheet("Lists")
    ws.append([
        "Source Type", "Service Mode", "Status", "Loss Reason", "",
        "Container Type", "TEU Factor", "Location Type", "LATAM Countries",
        "Special Handling", "Incoterms", "",
    ])
    for row in LISTS_ROWS:
        ws.append(list(row))

    wb.save(path)
    return path


@pytest.fixture
def workbook(tmp_path):
    return _build_workbook(tmp_path / "sample.xlsx")


@pytest.fixture
def migrated(conn, workbook, tmp_path):
    report = migrate_xlsx.migrate(conn, workbook=workbook,
                                  thread_map=tmp_path / "no-map.csv",
                                  today="2026-09-08",
                                  dedupe_report=tmp_path / "dedupe.csv")
    return conn, report


# ------------------------------------------------------------------ counts --
def test_counts_match_the_workbook(migrated):
    conn, report = migrated
    assert report.countries_inserted == len(COUNTRIES)
    assert report.locations_inserted == len(LOCATIONS)
    assert report.parties_inserted == len(AGENTS)
    assert report.enquiries_inserted == len(ENQUIRIES)
    for table, expected in (("country", 4), ("location", 3), ("party", 7),
                            ("enquiry", 7)):
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM {}".format(table)).fetchone()["n"] == expected


def test_no_placeholder_or_location_invented(migrated):
    conn, report = migrated
    assert report.parties_created_placeholder == []
    assert report.locations_created_to_confirm == []


def test_next_opp_number_continues_from_the_highest(migrated):
    conn, report = migrated
    assert report.next_opp_number == 8
    assert db.peek_opp_no(conn) == "OPP-0008"
    assert db.next_opp_no(conn) == "OPP-0008"
    assert db.peek_opp_no(conn) == "OPP-0009"


# ------------------------------------------------------------------ parties --
def test_party_types_follow_the_key_rules(migrated):
    conn, _ = migrated
    rows = {r["key"]: r for r in conn.execute("SELECT * FROM party")}
    assert rows["Tolvex"]["type"] == "Agent"
    assert rows["JoJo JJZ"]["type"] == "Office"
    assert (rows["(TBC)"]["type"], rows["(TBC)"]["placeholder"]) == ("Unknown", 1)
    assert (rows["COLIN"]["type"], rows["COLIN"]["placeholder"]) \
        == ("Direct Customer", 1)
    assert rows["Ridgeway Parts"]["type"] == "Direct Customer"
    assert rows["Tolvex"]["mfn_member"] == 1
    assert rows["JoJo JJZ"]["mfn_member"] is None


def test_contacts_pair_up_when_the_counts_agree(migrated):
    conn, _ = migrated
    rows = conn.execute(
        "SELECT c.name, c.email, c.is_primary FROM contact c "
        "JOIN party p ON p.id = c.party_id WHERE p.key = 'Tolvex' ORDER BY c.id"
    ).fetchall()
    assert [(r["name"], r["email"]) for r in rows] == [
        ("Ana", "ana@tolvex.example"), ("Luis", "luis@tolvex.example")]
    assert rows[0]["is_primary"] == 1 and rows[1]["is_primary"] == 0


def test_contacts_split_when_the_counts_disagree(migrated):
    conn, _ = migrated
    rows = conn.execute(
        "SELECT c.name, c.email FROM contact c JOIN party p ON p.id = c.party_id "
        "WHERE p.key = 'A Plus Kestrel' ORDER BY c.id").fetchall()
    assert [(r["name"], r["email"]) for r in rows] == [
        (None, "a@kestrellogistics-ph.example"),
        (None, "b@kestrellogistics-ph.example"),
        ("Supervisor / Tovin", None),
    ]


def test_domains_come_from_emails_and_notes_but_not_free_mail(migrated):
    conn, _ = migrated
    rows = {r["domain"]: r["key"] for r in conn.execute(
        "SELECT d.domain, p.key FROM party_domain d JOIN party p ON p.id = d.party_id")}
    assert rows["tolvex.example"] == "Tolvex"
    assert rows["ridgewayparts.example"] == "Ridgeway Parts"
    assert rows["kestrellogistics-ph.example"] == "A Plus Kestrel"
    assert "gmail.com" not in rows


def test_stage_reflects_whether_the_party_ever_asked(migrated):
    conn, _ = migrated
    stages = {r["key"]: r["stage"] for r in conn.execute(
        "SELECT key, stage FROM party")}
    assert stages["Tolvex"] == "active"
    assert stages["Free Mail Co"] == "dormant"


# ---------------------------------------------------------------- enquiries --
def test_status_mapping_and_flags(migrated):
    conn, _ = migrated
    rows = {r["opp_no"]: r for r in conn.execute("SELECT * FROM enquiry")}
    assert rows["OPP-0001"]["status"] == "Won"
    assert rows["OPP-0002"]["status"] == "Quoted"
    assert (rows["OPP-0003"]["status"], rows["OPP-0003"]["status_confirmed"]) \
        == ("New", 0)
    # a blank Loss Reason stays blank: 'Other' would read as a choice someone made
    assert (rows["OPP-0004"]["status"], rows["OPP-0004"]["closed_reason"]) \
        == ("Lost", None)
    assert (rows["OPP-0005"]["status"], rows["OPP-0005"]["legacy"],
            rows["OPP-0005"]["closed_reason"]) == ("Dropped", 1, None)
    assert rows["OPP-0006"]["status"] == "New"             # 'Sent to Vendor': Sourcing retired 2026-09-24
    assert (rows["OPP-0007"]["status"], rows["OPP-0007"]["closed_reason"]) \
        == ("Dropped", "Unable to Handle")


def test_every_enquiry_ends_on_its_own_status(migrated):
    conn, _ = migrated
    rows = conn.execute(
        "SELECT e.opp_no, e.status, "
        "(SELECT s.status FROM status_event s WHERE s.enquiry_id = e.id "
        " ORDER BY s.at DESC, s.id DESC LIMIT 1) AS last FROM enquiry e"
    ).fetchall()
    # OPP-0006 says 'Sent to Vendor' (New since Sourcing was retired) but
    # carries a Date Quoted on its received day: the workbook's New and the
    # Quoted share a date, so the New is the opening event and the quote is
    # the last one. verify says so (check 3); the migration keeps the
    # workbook's answer (README decision 2026-09-14).
    assert [r["opp_no"] for r in rows if r["last"] != r["status"]] == ["OPP-0006"]
    assert all(conn.execute(
        "SELECT COUNT(*) AS n FROM status_event WHERE enquiry_id = "
        "(SELECT id FROM enquiry WHERE opp_no = ?)", (opp,)).fetchone()["n"] > 0
        for opp in [r["opp_no"] for r in rows])


def test_missing_date_received_falls_back_to_the_earliest_known_date(migrated):
    """OPP-0006 was quoted on 2026-08-11, so it cannot have arrived today."""
    conn, _ = migrated
    row = conn.execute(
        "SELECT * FROM status_event WHERE enquiry_id = "
        "(SELECT id FROM enquiry WHERE opp_no = 'OPP-0006') "
        "AND status = 'New'").fetchone()
    assert row["at"] == "2026-08-11" and row["approx"] == 1


def test_a_row_with_no_dates_at_all_falls_back_to_today():
    from opplist.status import from_legacy
    events = migrate_xlsx._build_events(
        from_legacy("Legacy - Unknown"), None, None, None, None, "2026-09-08")
    assert [(e.status, e.at, e.approx) for e in events] == [
        ("New", "2026-09-08", 1), ("Dropped", "2026-09-08", 1)]


def test_sent_to_vendor_with_a_quote_date_keeps_the_workbook_status(migrated):
    """OPP-0006 says 'Sent to Vendor' (New since Sourcing was retired) but
    carries a Date Quoted: the case keeps the workbook's New, the quote is
    on the timeline."""
    conn, _ = migrated
    rows = conn.execute(
        "SELECT status, at, approx FROM status_event WHERE enquiry_id = "
        "(SELECT id FROM enquiry WHERE opp_no = 'OPP-0006') ORDER BY at, id"
    ).fetchall()
    assert [r["status"] for r in rows] == ["New", "Quoted"]
    assert rows[0]["approx"] == 1
    assert conn.execute("SELECT status FROM enquiry WHERE opp_no = 'OPP-0006'").fetchone()[0] == "New"


def test_closed_event_without_a_close_date_is_approximate(migrated):
    conn, _ = migrated
    row = conn.execute(
        "SELECT * FROM status_event WHERE enquiry_id = "
        "(SELECT id FROM enquiry WHERE opp_no = 'OPP-0004') "
        "AND status = 'Lost'").fetchone()
    assert row["at"] == "2026-08-07" and row["approx"] == 1


def test_quote_ref_is_kept_even_without_a_quoted_event(migrated):
    conn, _ = migrated
    row = conn.execute(
        "SELECT system_notes FROM enquiry WHERE opp_no = 'OPP-0001'").fetchone()
    assert "quote ref: JJQ-2026-311" in row["system_notes"]


def test_a_date_quoted_becomes_an_exact_quoted_event(migrated):
    conn, _ = migrated
    row = conn.execute(
        "SELECT at, approx FROM status_event WHERE status = 'Quoted' "
        "AND enquiry_id = (SELECT id FROM enquiry WHERE opp_no = 'OPP-0002')"
    ).fetchone()
    assert (row["at"], row["approx"]) == ("2026-08-08", 0)


def test_country_outside_country_list_becomes_unknown(migrated):
    conn, report = migrated
    row = conn.execute(
        "SELECT country FROM party WHERE key = 'A Plus Kestrel'").fetchone()
    assert row["country"] == "Unknown"
    assert any("Atlantis" in note for note in report.notes)


def test_container_type_is_renamed(migrated):
    conn, _ = migrated
    row = conn.execute(
        "SELECT container_type FROM enquiry WHERE opp_no = 'OPP-0001'").fetchone()
    assert row["container_type"] == "40HQ"


def test_quantity_is_parsed_only_when_the_columns_are_empty(migrated):
    conn, _ = migrated
    row = conn.execute(
        "SELECT lcl_cbm, system_notes FROM enquiry WHERE opp_no = 'OPP-0003'"
    ).fetchone()
    assert row["lcl_cbm"] == 6.6
    assert "qty parsed from raw (migration)" in row["system_notes"]
    kept = conn.execute(
        "SELECT container_qty FROM enquiry WHERE opp_no = 'OPP-0001'").fetchone()
    assert kept["container_qty"] == 3


def test_special_handling_splits_into_tags_client_and_remarks(migrated):
    conn, _ = migrated
    rows = {r["opp_no"]: r for r in conn.execute(
        "SELECT opp_no, special_handling, end_customer, remarks FROM enquiry")}
    assert rows["OPP-0001"]["special_handling"] == "Transload;Flatbed"
    assert rows["OPP-0002"]["end_customer"] == "Flexmold"
    assert rows["OPP-0002"]["special_handling"] is None
    assert rows["OPP-0003"]["special_handling"] is None
    assert "4 TEUs/month" in rows["OPP-0003"]["remarks"]
    assert "watch this one" in rows["OPP-0003"]["remarks"]


def test_country_is_filled_in_from_the_location(migrated):
    conn, _ = migrated
    row = conn.execute(
        "SELECT dest_country FROM enquiry WHERE opp_no = 'OPP-0002'").fetchone()
    assert row["dest_country"] == "USA"


def test_lists_sheet_adds_new_values_to_settings(migrated):
    conn, report = migrated
    reasons = db.get_setting_list(conn, "closed_reasons")
    assert "Brand new reason" in reasons
    assert "Price" in reasons                  # nothing is removed
    assert any("Brand new reason" in s for s in report.settings_added)


# ---------------------------------------------------------------- idempotent --
def test_rerunning_the_migration_changes_nothing(conn, workbook, tmp_path):
    first = migrate_xlsx.migrate(conn, workbook=workbook,
                                 thread_map=tmp_path / "none.csv",
                                 today="2026-09-08", dedupe_report=False)
    counts_before = {
        table: conn.execute(
            "SELECT COUNT(*) AS n FROM {}".format(table)).fetchone()["n"]
        for table in ("enquiry", "party", "contact", "party_domain",
                      "status_event", "location", "country")
    }
    second = migrate_xlsx.migrate(conn, workbook=workbook,
                                  thread_map=tmp_path / "none.csv",
                                  today="2026-09-08", dedupe_report=False)
    counts_after = {
        table: conn.execute(
            "SELECT COUNT(*) AS n FROM {}".format(table)).fetchone()["n"]
        for table in counts_before
    }
    assert counts_before == counts_after
    assert first.enquiries_inserted == 7 and second.enquiries_inserted == 0
    assert second.enquiries_updated == 0, "a re-run of the same workbook changes nothing"
    assert second.parties_updated == 0
    assert second.status_events_inserted == 0


def test_rerunning_writes_no_change_log_rows(conn, workbook, tmp_path):
    migrate_xlsx.migrate(conn, workbook=workbook,
                         thread_map=tmp_path / "none.csv", today="2026-09-08",
                         dedupe_report=False)
    before = conn.execute("SELECT COUNT(*) AS n FROM change_log").fetchone()["n"]
    migrate_xlsx.migrate(conn, workbook=workbook,
                         thread_map=tmp_path / "none.csv", today="2026-09-08",
                         dedupe_report=False)
    after = conn.execute("SELECT COUNT(*) AS n FROM change_log").fetchone()["n"]
    assert before == after and before > 0


def test_thread_map_fills_unambiguous_thread_keys(conn, workbook, tmp_path):
    map_path = tmp_path / "thread-opp-map.csv"
    map_path.write_text(
        "ThreadKey,OppNo,Source,AddedOn\n"
        "RATEREQUESTMIALIM,OPP-0001,test,2026-09-08\n"
        "OTHERKEY,OPP-0002,test,2026-09-08\n"
        "SECONDKEY,OPP-0002,test,2026-09-08\n",
        encoding="utf-8")
    report = migrate_xlsx.migrate(conn, workbook=workbook, thread_map=map_path,
                                  today="2026-09-08", dedupe_report=False)
    assert report.thread_keys_set == 1
    rows = {r["opp_no"]: r["thread_key"] for r in conn.execute(
        "SELECT opp_no, thread_key FROM enquiry")}
    assert rows["OPP-0001"] == "RATEREQUESTMIALIM"
    assert rows["OPP-0002"] is None       # two threads: left for a human


# ------------------------------------------------------------- change_log ----
def test_every_write_is_recorded(migrated):
    conn, _ = migrated
    inserts = conn.execute(
        "SELECT COUNT(*) AS n FROM change_log WHERE field = '(insert)' "
        "AND table_name = 'enquiry'").fetchone()["n"]
    assert inserts == len(ENQUIRIES)
    assert conn.execute(
        "SELECT COUNT(DISTINCT source) AS n FROM change_log").fetchone()["n"] == 1
    assert conn.execute(
        "SELECT source FROM change_log LIMIT 1").fetchone()["source"] == "migration"


# ----------------------------------------------------------------- verify ----
def test_verify_reports_matching_counts(migrated, workbook, tmp_path):
    conn, _ = migrated
    from opplist import export_xlsx
    export_path = export_xlsx.export(conn, tmp_path / "out.xlsx",
                                     months=["2026-08"])
    csv_path = export_csv.export_opp_index(conn, tmp_path / "opp-index.csv")
    result = verify.run(conn, workbook=workbook, export_path=export_path,
                        csv_path=csv_path)
    by_name = {c.name: c for c in result.checks}
    assert by_name["1. row counts match the workbook"].ok
    # OPP-0006 carries New and Quoted on one day with status New ('Sent to
    # Vendor', Sourcing until it was retired): under the same-day rule
    # (New < Quoted) the case is Quoted,
    # and verify says so rather than the migration changing the workbook's
    # answer for her (README decision 2026-09-14).
    events_check = by_name["3. every enquiry has events and ends on its own status"]
    assert not events_check.ok
    assert [l for l in events_check.lines if "but last event" in l] ==         ["   OPP-0006 status New but last event Quoted"]
    assert any(l.startswith("latest day has several events") for l in events_check.lines)
    assert by_name["4. no requester or location invented by the migration"].ok
    assert by_name["6. opp-index.csv equals the Export-OppIndex output"].ok
    assert by_name["7. contact emails match the workbook"].ok


# ------------------------------------ rows the sheet never numbered --------
def _sheet_with_half_registered_rows(tmp_path, extra_rows):
    """The sample workbook plus rows that have data but no Enquiry No."""
    return _build_workbook(tmp_path / "with-blanks.xlsx",
                           enquiries=list(ENQUIRIES) + list(extra_rows))


def test_a_row_with_no_number_gets_one_and_says_so(tmp_path):
    blank = list(ENQUIRIES[0])
    blank[0] = None                       # no Enquiry No.
    blank[2] = "Tolvex"
    workbook = _sheet_with_half_registered_rows(tmp_path, [blank])
    conn = db.open_db(tmp_path / "numbered.db")
    report = migrate_xlsx.migrate(conn, workbook=workbook,
                                  thread_map=tmp_path / "no-map.csv",
                                  today="2026-09-09",
                                  dedupe_report=tmp_path / "dedupe.csv")
    assert len(report.enquiries_numbered) == 1
    sheet_row, opp_no, agent = report.enquiries_numbered[0]
    assert opp_no == "OPP-0008"           # after the highest in the sheet
    assert agent == "Tolvex"
    row = conn.execute("SELECT system_notes, status, status_confirmed "
                       "FROM enquiry WHERE opp_no = ?", (opp_no,)).fetchone()
    assert "numbered at migration 2026-09-09" in row["system_notes"]
    assert "Opportunities row {}".format(sheet_row) in row["system_notes"]
    assert report.next_opp_number == 9
    conn.close()


def test_the_numbers_follow_the_order_of_the_rows(tmp_path):
    blanks = []
    for agent in ("Tolvex", "Ridgeway Parts", "Free Mail Co"):
        blank = list(ENQUIRIES[0])
        blank[0] = None
        blank[2] = agent
        blanks.append(blank)
    workbook = _sheet_with_half_registered_rows(tmp_path, blanks)
    conn = db.open_db(tmp_path / "order.db")
    report = migrate_xlsx.migrate(conn, workbook=workbook,
                                  thread_map=tmp_path / "no-map.csv",
                                  today="2026-09-09",
                                  dedupe_report=tmp_path / "dedupe.csv")
    assert [(o, a) for _, o, a in report.enquiries_numbered] == [
        ("OPP-0008", "Tolvex"), ("OPP-0009", "Ridgeway Parts"),
        ("OPP-0010", "Free Mail Co")]
    conn.close()


def test_running_it_twice_does_not_number_the_row_twice(tmp_path):
    """Numbers are never reused, re-runs never double."""
    blank = list(ENQUIRIES[0])
    blank[0] = None
    workbook = _sheet_with_half_registered_rows(tmp_path, [blank])
    path = tmp_path / "twice.db"
    conn = db.open_db(path)
    first = migrate_xlsx.migrate(conn, workbook=workbook,
                                 thread_map=tmp_path / "no-map.csv",
                                 today="2026-09-09",
                                 dedupe_report=tmp_path / "dedupe.csv")
    second = migrate_xlsx.migrate(conn, workbook=workbook,
                                  thread_map=tmp_path / "no-map.csv",
                                  today="2026-09-09",
                                  dedupe_report=tmp_path / "dedupe.csv")
    assert first.enquiries_inserted == len(ENQUIRIES) + 1
    assert second.enquiries_inserted == 0
    assert second.enquiries_numbered == []
    assert conn.execute("SELECT COUNT(*) AS n FROM enquiry").fetchone()["n"] == \
        len(ENQUIRIES) + 1
    assert second.next_opp_number == first.next_opp_number
    conn.close()


def test_a_numbered_row_does_not_fail_the_verify_checks(tmp_path):
    """Its agent and its ports may be new; anything else still fails."""
    blank = list(ENQUIRIES[0])
    blank[0] = None
    blank[2] = "Brand New Agent"          # not in Agent_Master
    blank[10] = "ZZZ"                     # not in Lookup_Location
    workbook = _sheet_with_half_registered_rows(tmp_path, [blank])
    conn = db.open_db(tmp_path / "verify.db")
    migrate_xlsx.migrate(conn, workbook=workbook,
                         thread_map=tmp_path / "no-map.csv",
                         today="2026-09-09",
                         dedupe_report=tmp_path / "dedupe.csv")
    import openpyxl
    wb = openpyxl.load_workbook(workbook, read_only=True, data_only=True)
    try:
        counts = verify.check_counts(conn, wb)
        placeholders = verify.check_placeholders(conn)
    finally:
        wb.close()
    assert counts.ok, counts.lines
    assert placeholders.ok, placeholders.lines
    assert any("Brand New Agent" in line for line in placeholders.lines)
    conn.close()

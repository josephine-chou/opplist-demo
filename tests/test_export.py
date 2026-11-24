"""The export byte format and the workbook column contract."""

from __future__ import annotations

import openpyxl
import pytest

from conftest import add_party, seed_reference
from opplist import export_csv, export_xlsx


def read_raw(path):
    """Read without newline translation; CRLF is part of the contract."""
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return fh.read()


@pytest.fixture
def populated(repo):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile",
                       full_name='3QV Group Chile SpA (aka "Tolvex")')
    chi = add_party(repo, "JoJo JJC", ptype="Office")
    repo.insert("contact", {"party_id": tolvex, "name": "Ana",
                            "email": "ana@tolvex.example", "is_primary": 1})
    repo.insert("contact", {"party_id": tolvex, "name": "Luis",
                            "email": "luis@tolvex.example"})

    first = repo.insert("enquiry", {
        "opp_no": "OPP-0001", "requester_party_id": tolvex,
        "received_on": "2026-08-03", "service_mode": "FCL",
        "container_type": "40HQ", "container_qty": 3,
        "origin_country": "USA", "origin_loc": "LAX",
        "dest_country": "Chile", "dest_loc": "SAI",
        "commodity": 'PET flakes, "clear"', "status": "Won",
        "special_handling": "Transload;Flatbed", "qty_raw": "FCL:3 x 40'HQ",
    })
    repo.insert("status_event", {"enquiry_id": first, "at": "2026-08-03",
                                 "status": "New"})
    repo.insert("status_event", {"enquiry_id": first, "at": "2026-08-06",
                                 "status": "Quoted",
                                 "note": "quote ref: COT-2026-118"})
    repo.insert("status_event", {"enquiry_id": first, "at": "2026-08-20",
                                 "status": "Won"})

    second = repo.insert("enquiry", {
        "opp_no": "OPP-0002", "requester_party_id": chi,
        "received_on": "2026-08-11", "service_mode": "Air",
        "air_weight_kg": 216.0, "origin_country": "China",
        "origin_loc": "NGB", "dest_country": "USA", "dest_loc": "MEM",
        "status": "New", "status_confirmed": 0,
    })
    repo.insert("status_event", {"enquiry_id": second, "at": "2026-08-11",
                                 "status": "New"})

    third = repo.insert("enquiry", {
        "opp_no": "OPP-0003", "requester_party_id": tolvex,
        "received_on": "2026-08-15", "service_mode": "LCL", "lcl_cbm": 6.6,
        "origin_country": "USA", "origin_loc": "MIA",
        "dest_country": "Brazil", "status": "Dropped", "legacy": 1,
    })
    repo.insert("status_event", {"enquiry_id": third, "at": "2026-08-15",
                                 "status": "New"})
    repo.insert("status_event", {"enquiry_id": third, "at": "2026-08-15",
                                 "status": "Dropped"})
    repo.commit()
    return repo.conn


# ------------------------------------------------------------------- CSV ----
def test_opp_index_byte_format(populated, tmp_path):
    path = export_csv.export_opp_index(populated, tmp_path / "opp-index.csv")
    raw = path.read_bytes()

    assert raw.startswith(b"\xef\xbb\xbf"), "UTF-8 BOM, as Export-Csv writes"
    assert raw.endswith(b"\r\n")
    assert b"\n" not in raw.replace(b"\r\n", b"")
    text = raw.decode("utf-8-sig")
    lines = text.split("\r\n")[:-1]
    assert lines[0] == ('"OppNo","DateReceived","Agent","Mode","OriginDetail",'
                        '"DestDetail","Commodity","Status"')
    for line in lines:
        assert line.startswith('"') and line.endswith('"')
    assert len(lines) == 4


def test_opp_index_quotes_are_doubled(populated, tmp_path):
    path = export_csv.export_opp_index(populated, tmp_path / "opp-index.csv")
    text = read_raw(path)
    assert '"PET flakes, ""clear"""' in text


def test_opp_index_uses_the_legacy_status_strings(populated, tmp_path):
    path = export_csv.export_opp_index(populated, tmp_path / "opp-index.csv")
    rows = read_raw(path).split("\r\n")
    assert rows[1].endswith('"Secured"')
    assert rows[2].endswith('""')                    # unconfirmed New -> blank
    assert rows[3].endswith('"Legacy - Unknown"')


def test_opp_index_is_ordered_by_case_number(populated, tmp_path):
    path = export_csv.export_opp_index(populated, tmp_path / "opp-index.csv")
    numbers = [line.split(",")[0] for line in
               read_raw(path).split("\r\n")[1:-1]]
    assert numbers == ['"OPP-0001"', '"OPP-0002"', '"OPP-0003"']


def test_agents_csv_format(populated, tmp_path):
    path = export_csv.export_agents(populated, tmp_path / "agents.csv")
    text = read_raw(path)
    lines = text.split("\r\n")[:-1]
    assert lines[0] == '"Agent","FullName","Country","TotalEnquiries","KnownEmail"'
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    # one cell holds every address, as the old file did
    assert '"ana@tolvex.example; luis@tolvex.example"' in text
    assert '"Tolvex","3QV Group Chile SpA (aka ""Tolvex"")","Chile","2"' in text


def test_a_dropped_case_is_still_exported(populated, tmp_path):
    """Hiding dropped rows is a screen filter: the files the
    PowerShell pipeline reads carry every case, as they always did."""
    from opplist import db
    repo = db.Repository(populated, source="test")
    case = repo.insert("enquiry", {
        "opp_no": "OPP-0004", "requester_party_id": 1,
        "received_on": "2026-08-20", "service_mode": "Air",
        "status": "New"})
    repo.insert("status_event", {"enquiry_id": case, "at": "2026-08-20",
                                 "status": "New"})
    repo.set_status(case, "Dropped", "2026-08-21",
                    closed_reason="Registered in error")
    repo.commit()

    path = export_csv.export_opp_index(populated, tmp_path / "opp-index.csv")
    lines = read_raw(path).split("\r\n")[:-1]
    assert len(lines) == 5                                  # header + four cases
    assert lines[0].count(",") == 7                         # the eight columns
    assert lines[4].startswith('"OPP-0004"')
    assert lines[4].endswith('"Registered in error"')

    book = export_xlsx.export(populated, tmp_path / "out.xlsx")
    wb = openpyxl.load_workbook(book)
    try:
        sheet = wb["Opportunities"]
        assert len(export_xlsx.OPPORTUNITY_COLUMNS) == 41
        assert [c.value for c in sheet[1]] == export_xlsx.OPPORTUNITY_COLUMNS
        assert sheet.max_row == 5
        assert [r[0].value for r in sheet.iter_rows(min_row=2)] == [
            "OPP-0001", "OPP-0002", "OPP-0003", "OPP-0004"]
    finally:
        wb.close()


# ------------------------------------------------------------------ xlsx ----
def test_workbook_columns_are_the_contract(populated, tmp_path):
    path = export_xlsx.export(populated, tmp_path / "out.xlsx")
    wb = openpyxl.load_workbook(path)
    try:
        headers = [c.value for c in wb["Opportunities"][1]]
        assert len(headers) == 41
        assert headers == export_xlsx.OPPORTUNITY_COLUMNS
        assert headers[0] == "Enquiry No." and headers[40] == "Mode/Location Check"
        assert [c.value for c in wb["Agent_Master"][1]] == export_xlsx.AGENT_COLUMNS
        for sheet in ("Opportunities", "Lookup_Location", "Agent_Master",
                      "Country_List", "Lists", "Report", "Export Info"):
            assert sheet in wb.sheetnames
    finally:
        wb.close()


def test_workbook_writes_derived_values_not_formulas(populated, tmp_path):
    path = export_xlsx.export(populated, tmp_path / "out.xlsx")
    wb = openpyxl.load_workbook(path)
    try:
        ws = wb["Opportunities"]
        header = {c.value: i for i, c in enumerate(ws[1])}
        row = [c.value for c in ws[2]]
        assert row[header["Enquiry No."]] == "OPP-0001"
        assert row[header["Month"]] == "2026-08"
        assert row[header["Source Type"]] == "Agent"
        assert row[header["TEU"]] == 6
        assert row[header["LATAM"]] == "Y"
        assert row[header["Direction"]] == "US Export"
        assert row[header["Status"]] == "Secured"
        assert row[header["Quote Ref"]] == "COT-2026-118"
        assert row[header["Days to Quote"]] == 3
        assert row[header["Special Handling"]] == "Transload; Flatbed"
        assert row[header["Date Received"]].date().isoformat() == "2026-08-03"
        assert row[header["Date Closed"]].date().isoformat() == "2026-08-20"
        for blank in ("Overdue Flag", "Location Check", "FMS Job No.",
                      "Country/Location Check", "Mode/Location Check"):
            assert row[header[blank]] is None
        assert all(not (isinstance(c.value, str) and c.value.startswith("="))
                   for r in ws.iter_rows(min_row=2) for c in r)
    finally:
        wb.close()


def test_legacy_row_exports_as_legacy_unknown(populated, tmp_path):
    path = export_xlsx.export(populated, tmp_path / "out.xlsx")
    wb = openpyxl.load_workbook(path)
    try:
        ws = wb["Opportunities"]
        header = {c.value: i for i, c in enumerate(ws[1])}
        row = [c.value for c in ws[4]]
        assert row[header["Enquiry No."]] == "OPP-0003"
        assert row[header["Status"]] == "Legacy - Unknown"
        # the workbook had no reason and none was invented
        assert row[header["Loss Reason"]] is None
    finally:
        wb.close()


def test_export_is_reproducible(populated, tmp_path):
    """Two exports of unchanged data agree cell for cell on Opportunities."""
    a = export_xlsx.export(populated, tmp_path / "a.xlsx")
    b = export_xlsx.export(populated, tmp_path / "b.xlsx")
    wa = openpyxl.load_workbook(a)
    wb_ = openpyxl.load_workbook(b)
    try:
        rows_a = [[c.value for c in r] for r in wa["Opportunities"].iter_rows()]
        rows_b = [[c.value for c in r] for r in wb_["Opportunities"].iter_rows()]
        assert rows_a == rows_b
    finally:
        wa.close()
        wb_.close()

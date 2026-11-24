"""Batch 19 (2026-11-12): the month's inquiries as an xlsx for the sales
manager, in her v3 layout: a download on the Report page and the list of
its TBC cells above the button.

Every company, place, number and date here is invented; cases from
OPP-9991, domains .test.
"""

from __future__ import annotations

import datetime as dt
import io

import openpyxl
import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, inquiries_xlsx as ix
from opplist import web as web_module
from opplist.web import create_app

MONTH = "2026-12"
TODAY = "2027-01-04"


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "b19.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    agent = add_party(repo, "Quillmoor Cargo", country="Chile")
    office = add_party(repo, "JoJo QZX", ptype="Office", country="USA")
    direct = add_party(repo, "Pembrook Valves", ptype="Direct Customer", country="USA")
    ids = {"agent": agent, "office": office, "direct": direct}

    def case(opp, received, party=agent, **extra):
        extra.setdefault("scope", "In")
        return add_enquiry(repo, opp, party, received=received, **extra)

    # FCL MIA -> SAI, two box types, quoted then won, a follow-up between
    ids["fcl"] = case("OPP-9993", "2026-12-03", origin_loc="MIA", origin_country="USA", dest_loc="SAI",
                      dest_country="Chile", service_mode="FCL", incoterm="FOB", commodity="  Copper   rods ",
                      special_handling="DG, OOG")
    repo.add_cargo_line(ids["fcl"], {"container_qty": 1, "container_type": "20'", "commodity": "Copper rods"})
    repo.add_cargo_line(ids["fcl"], {"container_qty": 2, "container_type": "40 HC", "commodity": "Copper rods"})
    repo.set_status(ids["fcl"], "Quoted", "2026-12-05")
    repo.add_followup(ids["fcl"], "2026-12-08")
    repo.set_status(ids["fcl"], "Won", "2026-12-11")
    # LCL then Air, China -> Chile (a triangle), lost; an address with a US city on route B
    ids["two"] = case("OPP-9992", "2026-12-03", origin_loc="NGB", origin_country="China", dest_loc="SAI",
                      dest_country="Chile", service_mode="LCL", commodity="Lamp shades", lcl_cbm=4.5)
    repo.add_route(ids["two"], {"origin_country": "USA", "origin_alt": "Larkfield Works, 12 Orchard Way, Brambleton, VA 20148",
                                "dest_country": "Brazil", "dest_loc": None, "service_mode": "Air"})
    repo.set_status(ids["two"], "Quoted", "2026-12-04")
    repo.set_status(ids["two"], "Lost", "2026-12-20", closed_reason="Price")
    # Air, the office's, nothing filled: every always-filled column TBC, Air Weight TBC
    ids["bare"] = case("OPP-9991", "2026-12-02", party=office, service_mode="Air")
    # a customs-only route: the mode is the word; no air weight asked
    ids["customs"] = case("OPP-9994", "2026-12-09", party=direct, origin_loc="LAX", origin_country="USA",
                          dest_loc="MEM", dest_country="USA", service_mode="Air", incoterm="DDP", commodity="Seed trays")
    route_a = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ? AND seq = 1", (ids["customs"],)).fetchone()[0]
    repo.update("enquiry_route", route_a, {"service_scope": "Customs only"})
    # Cancelled: closed, no loss reason asked
    ids["cancel"] = case("OPP-9995", "2026-12-09", origin_loc="MIA", origin_country="USA", dest_loc="SAI",
                         dest_country="Chile", service_mode="Air", incoterm="EXW", commodity="Valves",
                         air_weight_kg=166.6666)
    repo.set_status(ids["cancel"], "Cancelled", "2026-12-15", closed_reason="Shipment cancelled")
    # not in the file: scope Out, registered in error, a duplicate, another month
    case("OPP-9996", "2026-12-10", scope="Out", service_mode="Air")
    err = case("OPP-9997", "2026-12-10", service_mode="Air")
    repo.set_status(err, "Dropped", "2026-12-11", closed_reason="Registered in error")
    dup = case("OPP-9998", "2026-12-10", service_mode="Air")
    repo.set_status(dup, "Dropped", "2026-12-11", closed_reason="Duplicate of OPP-9995")
    case("OPP-9999", "2026-11-30", service_mode="Air")
    repo.commit()
    monkeypatch.setattr(web_module, "_today", lambda: TODAY)
    app = create_app(path)
    app.config["TESTING"] = True
    yield {"conn": conn, "repo": repo, "ids": ids, "client": app.test_client(), "path": path}
    conn.close()


def by_opp(w):
    return {r.values["Inquiry No."]: r for r in ix.rows(w["conn"], MONTH)}


# ------------------------------------------------------------------ rows --
def test_the_rows_are_the_months_counted_inquiries_by_number(world):
    got = [r.values["Inquiry No."] for r in ix.rows(world["conn"], MONTH)]
    assert got == ["OPP-9991", "OPP-9992", "OPP-9993", "OPP-9994", "OPP-9995"]


# ---------------------------------------------------------------- values --
def test_fcl_containers_by_type_teu_and_the_dates(world):
    v = by_opp(world)["OPP-9993"].values
    assert (v["Container Type"], v["Qty"], v["TEU"]) == ("1x20GP+2x40HQ", 3, 5)
    assert (v["Service Mode"], v["Service Type"], v["Incoterms"]) == ("FCL", "Export", "FOB")
    assert (v["Origin Country"], v["Origin Detail"], v["Dest. Country"], v["Dest. Detail"]) == ("USA", "MIA", "Chile", "SAI")
    assert v["Commodity"] == "Copper rods" and v["Special Handling"] == "DG; OOG"
    assert (v["Status"], v["Date Quoted"], v["FU Date"], v["Date Closed"]) == \
        ("Won", dt.date(2026, 12, 5), dt.date(2026, 12, 8), dt.date(2026, 12, 11))
    assert v["Air Weight (kgs)"] is None and v["LCL Volume (cbm)"] is None and v["Loss Reason"] is None
    assert v["Agent"] == "Quillmoor Cargo"


def test_container_words_are_read_as_opplist_boxes():
    assert ix.container_type("40' HC DV SOC") == "40HQ" and ix.container_type("20 DC") == "20GP"
    assert ix.container_type("40\u2019RF") == "40RF" and ix.container_type("20FT DRY") == "20GP"
    assert ix.container_type("Mixed") == "Mixed"                     # no size: as written, TEU to fill


def test_a_line_on_every_route_counts_once_per_route(world):
    repo = world["repo"]
    case = add_enquiry(repo, "OPP-9989", world["ids"]["agent"], received="2026-12-14", scope="In",
                       origin_loc="MIA", origin_country="USA", dest_loc="SAI", dest_country="Chile",
                       service_mode="FCL", incoterm="FOB", commodity="Tiles")
    repo.add_route(case, {"origin_loc": "LAX", "origin_country": "USA", "dest_loc": "SAI", "dest_country": "Chile",
                          "service_mode": "FCL"})
    repo.add_cargo_line(case, {"container_qty": 1, "container_type": "40HQ"})
    repo.commit()
    v = by_opp(world)["OPP-9989"].values
    assert (v["Container Type"], v["Qty"], v["TEU"]) == ("40HQ", 2, 4)
    assert v["Origin Detail"] == "MIA / LAX" and v["Origin Country"] == "USA"     # one country once


def test_routes_join_modes_countries_places_and_types(world):
    v = by_opp(world)["OPP-9992"].values
    assert v["Service Mode"] == "Air & LCL"                          # FX5: alphabetical, as her files
    assert v["Origin Country"] == "China & USA" and v["Dest. Country"] == "Chile & Brazil"
    assert v["Origin Detail"] == "NGB / BRAMBLETON, VA"               # a US address's city
    assert v["Dest. Detail"] == "SAI"
    assert v["Service Type"] == "Triangle & Export"
    assert v["LCL Volume (cbm)"] == 4.5
    assert (v["Status"], v["Loss Reason"]) == ("Lost", "Price")


def test_customs_only_is_the_mode_and_asks_no_weight(world):
    row = by_opp(world)["OPP-9994"]
    assert row.values["Service Mode"] == "Customs only"
    assert row.values["Air Weight (kgs)"] is None
    assert row.values["Service Type"] == "Domestic"
    assert row.values["Agent"] == "Pembrook Valves"                  # a direct customer is in the file


def test_air_weight_to_one_decimal_and_a_cancelled_case_asks_no_loss_reason(world):
    row = by_opp(world)["OPP-9995"]
    assert row.values["Air Weight (kgs)"] == 166.7
    assert row.values["Status"] == "Cancelled" and row.values["Date Closed"] == dt.date(2026, 12, 15)
    assert row.values["Loss Reason"] == "Shipment cancelled"
    assert row.values["Date Quoted"] is None                          # never quoted: blank, not TBC
    assert row.tbc == []


# ------------------------------------------------------------------- TBC --
def test_an_empty_case_is_tbc_in_every_column_that_applies(world):
    row = by_opp(world)["OPP-9991"]
    cols = [c for c, _ in row.tbc]
    assert cols == ["Air Weight (kgs)", "Origin Country", "Origin Detail", "Dest. Country", "Dest. Detail",
                    "Service Type", "Incoterms", "Commodity"]                  # in column order
    assert dict(row.tbc)["Incoterms"] == "the incoterm"
    assert row.values["Container Type"] is None and row.values["LCL Volume (cbm)"] is None
    assert row.values["Date Quoted"] is None and row.values["Date Closed"] is None
    assert all(row.values[c] == "TBC" for c in cols)


def test_quoted_without_a_date_and_lost_without_a_reason_are_tbc(world):
    repo = world["repo"]
    case = add_enquiry(repo, "OPP-9990", world["ids"]["agent"], received="2026-12-12", scope="In",
                       origin_loc="MIA", origin_country="USA", dest_loc="SAI", dest_country="Chile",
                       service_mode="FCL", incoterm="FOB", commodity="Tiles")
    repo.set_status(case, "Lost", "2026-12-14")
    repo.commit()
    row = by_opp(world)["OPP-9990"]
    cols = {c for c, _ in row.tbc}
    assert {"Container Type", "Qty", "TEU", "Loss Reason"} <= cols
    assert "Date Quoted" not in cols                                  # it was never quoted
    assert row.values["Date Closed"] == dt.date(2026, 12, 14)


# ---------------------------------------------------------------- layout --
def test_the_file_is_her_layout(world):
    data = ix.build(world["conn"], MONTH, dt.date(2027, 1, 4))
    ws = openpyxl.load_workbook(io.BytesIO(data)).active
    assert ws.title == "Inquiries"
    assert ws["A1"].value == "Inquiries, December 2026" and ws["E1"].value == "As of 01/04/2027"
    assert ws["A2"].value == "Total" and ws["B2"].value == "=SUBTOTAL(103,A4:A8)"
    assert [ws.cell(2, c).value for c in range(6, 10)] == ["=SUBTOTAL(109,{0}4:{0}8)".format(x) for x in "FGHI"]
    assert [ws.cell(3, c).value for c in range(1, 23)] == list(ix.HEADERS) and len(ix.HEADERS) == 22
    assert ws["A4"].value == "OPP-9991" and ws["A8"].value == "OPP-9995"
    assert ws.freeze_panes == "B4" and ws.auto_filter.ref == "A3:V8"
    assert ws["B4"].number_format == "mm/dd/yyyy"
    grey = {ws.cell(r, c).coordinate for r in range(1, 9) for c in range(1, 23)
            if ws.cell(r, c).fill.fill_type == "solid" and ws.cell(r, c).fill.fgColor.rgb == "FFEDEDED"}
    assert grey == {ws.cell(2, c).coordinate for c in range(1, 23)} | {"G{}".format(r) for r in range(4, 9)}
    assert all(ws.cell(r, c).border.left.style == "thin" for r in range(2, 9) for c in range(1, 23))


def test_the_file_name_is_todays():
    assert ix.file_name(dt.date(2027, 1, 4)) == "OppList_Inquiries_2027-01-04.xlsx"


# ------------------------------------------------------------------- web --
def test_the_report_page_lists_the_tbcs_above_the_button(world):
    html = world["client"].get("/report?month=" + MONTH).data.decode("utf-8")
    block = html[html.index('id="inquiry-tbc"'):]
    assert block.index("OPP-9991") < block.index('href="/report/inquiries.xlsx')
    assert 'href="/enquiries/{}"'.format(world["ids"]["bare"]) in block
    assert "the incoterm" in block and "Incoterms" in block
    assert "/report/inquiries.xlsx?month=" + MONTH in block


def test_a_month_with_nothing_to_fill_says_so(world):
    html = world["client"].get("/report?month=2026-10").data.decode("utf-8")
    assert "No TBC" in html


def test_the_download_is_the_months_file_and_nothing_is_written(world, tmp_path):
    before = sorted(p.name for p in tmp_path.rglob("*"))
    got = world["client"].get("/report/inquiries.xlsx?month=" + MONTH)
    assert got.status_code == 200
    assert "attachment" in got.headers["Content-Disposition"]
    assert "OppList_Inquiries_2027-01-04.xlsx" in got.headers["Content-Disposition"]
    ws = openpyxl.load_workbook(io.BytesIO(got.data)).active
    assert ws["A4"].value == "OPP-9991"
    assert sorted(p.name for p in tmp_path.rglob("*")) == before

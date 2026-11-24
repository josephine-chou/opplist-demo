"""Batch 17a-2: the month report with direct customers and the other
services (Customs only, Warehousing, Truck, ...); section 12 goes. (TK1: the mode
Domestic is Truck, and a Truck case needs both countries.)

Every company, place, date and number here is invented; cases from
OPP-5401. The byte-for-byte check reuses test_monthly's fixture month with
its one direct customer's case switched Out, so the month has neither
direct customers nor other services; BEFORE is the SHA-256 and length of
pages 1 to 3 as the code before 17a-2 rendered them (taken 2026-10-12).
"""

from __future__ import annotations

import hashlib
import re

import openpyxl
import pytest

from conftest import add_enquiry, add_party
from opplist import cheatsheet, monthly, monthly_export
from opplist.web import create_app
from test_monthly import book, cells  # noqa: F401  (the 17a fixture)

# 17i: the month has an office request, so section 9 gained the JoJo
# offices group (and page 3 the class dc, section 10 the 180 high chart);
# nothing else on pages 1 to 3 changed (diffed against the 7b2a67e output).
# 17k: section 4's heading states the rule and its win rate is won / quoted
# (50% / 40% / -10 pts became 33% / 25% / -8 pts); nothing else changed
# (diffed against the 5064296 output)
# 17l: the first tile reads "8 / 1" "Agents / Our offices" for "9"
# "Rate requests received"; nothing else changed (diffed against the
# 1f5dbed output)
# SC1: "Non-LATAM agents" reads "Other agents" (five times on pages 1 to 3)
# and the note under section 1 reads "Of which handled by other JoJo
# offices: 0" for "LATAM inquiries handled by ..."; with those two put back
# the pages are byte for byte the 1f5dbed output (checked 2026-11-10)
# This edition: the pages carry this edition's invented company and party
# names, so the bytes are not the ones the pin was first taken with. The
# value below is the same render on this edition's names, taken once the
# test had passed on the names it was written with (2026-11-23).
BEFORE = ("1c28f9318dcfd5abcd74e27219cf8713433b5e711e06197bf9377ef56881c6da", 380292)
MONTH = "2026-09"
NOV = "2026-11"


def pages(html, count=3) -> str:
    found = re.findall(r'<section class="page[^"]*">.*?</section>', html, re.S)
    return "\n".join(found[:count])


def test_pages_1_to_3_of_a_month_without_them_are_byte_for_byte_as_before(book):
    conn, repo, ids, p, case, path = book
    repo.update("enquiry", ids["farm"], {"scope": "Out"})
    repo.commit()
    html = pages(monthly_export.render_html(monthly.build(conn, MONTH))).encode("utf-8")
    assert (hashlib.sha256(html).hexdigest(), len(html)) == BEFORE


@pytest.fixture
def november(book):
    """November, fourteen cases: two warehousing, a customs-only, a Truck
    and a LATAM RORO case beside plain ones; four direct customers."""
    conn, repo, ids, p, case, path = book
    p["hollis"] = add_party(repo, "Hollis Bakery", ptype="Direct Customer", country="USA")
    p["tamsin"] = add_party(repo, "Tamsin Looms", ptype="Direct Customer", country="USA")
    p["orrin"] = add_party(repo, "Orrin Tiles", ptype="Direct Customer", country="USA")
    n = [5401]
    got = {}

    def add(key, party, mode, origin, dest, status="Quoted", service=None, day=3, **extra):
        opp = "OPP-{}".format(n[0])
        n[0] += 1
        got[key] = add_enquiry(repo, opp, p[party], received="{}-{:02d}".format(NOV, day), status=status,
                               service_mode=mode, origin_country=origin, dest_country=dest, **extra)
        if service:
            route = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ?", (got[key],)).fetchone()[0]
            repo.update("enquiry_route", route, {"service_scope": service})

    add("air_won", "quill", "Air", "USA", "Chile", "Won")
    add("air_lost", "lark", "Air", "USA", "China", "Lost", closed_reason="Price too high")
    add("customs", "lark", None, None, "USA", service="Customs only")           # no mode, one place
    add("ware1", "lark", "Not specified", "USA", None, service="Warehousing")
    # Air mode, still Other; SC1: an office's case from October that is not
    # LATAM, so the report leaves it out (it stays on file, scope In)
    add("ware2", "office", "Air", "USA", "USA", service="Warehousing")
    add("truck", "lark", "Truck", "USA", "USA")                                 # TK1: both countries
    add("roro", "quill", "RORO", "USA", "Chile")                                # a LATAM route
    add("no_dest", "lark", "Air", "USA", None)                                  # still a finding
    add("no_mode", "lark", None, "USA", "China")                                # still a finding
    # direct customers: Brindle Farm twice (one Won), three others once.
    # SC1: the three to Colombia - from October the report counts a direct
    # customer's case only when it is LATAM (they went to China before)
    add("farm_won", "farm", "FCL", "USA", "Chile", "Won", day=4)
    add("farm", "farm", "Air", "USA", "Chile", day=5)
    add("hollis", "hollis", "Air", "USA", "Colombia", day=6)
    add("tamsin", "tamsin", "Air", "USA", "Colombia", day=7)
    add("orrin", "orrin", "Air", "USA", "Colombia", day=8)
    repo.commit()
    yield conn, repo, got, p, path


def test_the_third_kpi_number_its_small_text_and_the_other_column(november):
    conn = november[0]
    r = monthly.build(conn, NOV)
    # SC1: Air 7, Ocean 2 (FCL and the RORO), Other 3 (the Truck and the two
    # services; ware2, the office's, is not counted); no_mode in Total only
    assert ("7 / 2 / 3", "Air / Ocean / Other") in r.kpis
    assert "Air / Ocean / Other" not in r.kpi_notes                 # 17l: no small line
    # SC1: RORO and Truck have their own columns; Other is the services
    assert r.by_scope.columns == ["Scope / requester", "Total", "Air", "FCL", "LCL", "RORO", "Truck", "Other",
                                  "Won", "Lost"]
    assert cells(r.by_scope)["Total"] == ["13", "7", "1", "0", "1", "1", "2", "2", "1"]
    # section 6: SC1 - the RORO export to Chile is Ocean, so no Other row
    assert [row.label for row in r.direction.rows] == ["Total", "Air", "Ocean"]
    assert cells(r.direction)["Ocean"] == ["2", "0", "0", "2"]
    html = monthly_export.render_html(r)
    assert "<b>7 / 2 / 3</b><span>Air / Ocean / Other</span>" in html and "warehousing</small>" not in html


def test_the_check_box_leaves_the_services_and_a_truck_case_with_both_countries_alone(november):
    conn, repo, got, p, path = november
    found = {g["title"]: [i["ids"][0] for i in g["items"]] for g in monthly.checks(conn, NOV)}
    place = found["No origin or destination country"]
    assert got["no_dest"] in place
    assert not {got["customs"], got["ware1"], got["truck"]} & set(place)
    assert found["No mode"] == [got["no_mode"]]          # not customs (blank mode), not ware1 (Not specified)


def test_section_4_rows_and_the_agents_and_offices_win_rate(november):
    conn, repo, got, p, path = november
    r = monthly.build(conn, NOV)
    rows = [(row.label, row.cells, row.cls) for row in r.compare.rows]
    assert [row[0] for row in rows] == [
        "Requests", "of which direct customers", "LATAM share", "Won", "of which direct customers",
        "Win rate, agents and offices", "Active LATAM agents", "New LATAM agents"]
    assert rows[1] == ("of which direct customers", ["", "5", ""], "indent")       # no October: no change
    assert rows[4][1] == ["", "1", ""]
    # agents and offices: Won 1, Lost 1, 6 open quotes (17k: won / quoted;
    # SC1: ware2, the office's case that is not LATAM, is not counted)
    assert rows[5][1] == ["", "12%", ""]
    wb = openpyxl.load_workbook(monthly_export.write_xlsx(r, path.parent))
    try:
        first = [row[0] for row in wb["Report"].iter_rows(values_only=True)]
    finally:
        wb.close()
    assert first.count("of which direct customers") == 2 and "Win rate, agents and offices" in first
    assert "  Win rate, agents and offices |  | 12% | " in monthly.render_text(r)


def test_section_4_changes_with_a_direct_customer_in_the_previous_month(november):
    conn, repo, got, p, path = november
    add_enquiry(repo, "OPP-5421", p["quill"], received="2026-12-02", status="Quoted", service_mode="Air",
                origin_country="USA", dest_country="Chile")
    repo.commit()
    rows = {row.label: row.cells for row in monthly.build(conn, "2026-12").compare.rows}
    assert "Win rate, agents and offices" in rows and "Win rate" not in rows


def test_section_4_is_17a_s_without_a_direct_customer_this_month_or_the_last(book):
    conn, repo, ids, p, case, path = book
    repo.update("enquiry", ids["farm"], {"scope": "Out"})
    repo.commit()
    labels = [row.label for row in monthly.build(conn, MONTH).compare.rows]
    assert labels == ["Requests", "LATAM share", "Won", "Win rate", "Active LATAM agents", "New LATAM agents"]


def test_section_9_has_the_three_direct_customers_and_page_3_is_tightened(november):
    conn, repo, got = november[:3]
    # 17i: without the office's request, so the direct customers are the one extra group
    repo.update("enquiry", got["ware2"], {"scope": "Out"})
    repo.commit()
    r = monthly.build(conn, NOV)
    heads = [row.label for row in r.top_agents.rows if row.cls == "head"]
    assert heads == ["LATAM agents", "Other agents", "Direct customers"]          # SC1: the label
    at = [row.label for row in r.top_agents.rows].index("Direct customers")
    # Brindle Farm 2 (1 won), then the first two of the three with one each
    assert [(row.label, row.cells[0], row.cells[5]) for row in r.top_agents.rows[at + 1:]] == [
        ("Brindle Farm", "2", "1"), ("Hollis Bakery", "1", ""), ("Tamsin Looms", "1", "")]
    assert r.direct_group
    html = monthly_export.render_html(r)
    assert '<section class="page dc"><h1>NOVEMBER REPORT <small>Agents</small>' in html
    assert ".page.dc .p3low{margin-top:20px}" in html
    page3 = pages(html)[pages(html).index("Requests per week"):]
    assert 'viewBox="0 0 640 180"' in page3
    # a month without a direct customer keeps the 200 high chart
    assert 'viewBox="0 0 640 200"' in monthly_export.render_html(monthly.build(conn, "2026-08"))


def test_the_cheat_sheet_glance_names_the_services(november):
    conn, repo, got, p, path = november
    g = cheatsheet.build(conn, NOV).glance
    # CS1: Other is every mode but Air, FCL and LCL (customs, ware1, truck,
    # roro; SC1: ware2 is not counted); the blank mode is no mode; the parts
    # add up to the inquiries
    assert (g["air"], g["fcl"], g["lcl"], g["other"], g["no_mode"]) == (7, 1, 0, 4, 1)
    assert g["air"] + g["fcl"] + g["lcl"] + g["other"] + g["no_mode"] == g["n"] == 13
    app = create_app(path)
    app.config["TESTING"] = True
    html = app.test_client().get("/report", query_string={"month": NOV, "view": "cheat"}).data.decode("utf-8")
    assert ("Air 7 &middot; FCL 1 &middot; LCL 0 &middot; Other 4 (Customs only 1, Warehousing 1, Truck 1, RORO 1)"
            ' &middot; <span class="nomode">no mode 1</span></span>') in html
    # the By who asked table's Other is the report's; the blank mode is No mode
    rows = {grp["name"]: grp["row"] for grp in cheatsheet.build(conn, NOV).groups}
    assert (rows["Other agents"]["other"], rows["Other agents"]["no_mode"]) == (3, 1)     # SC1: the label
    assert "Our offices" not in rows               # SC1: ware2, the one office case, is not counted


def test_no_no_response_section(november, tmp_path):
    conn = november[0]
    r = monthly.build(conn, NOV)
    # 17f, 17g: sections 12 to 18 and pages 4 and 5 are the analysis now
    assert [number for number, _, _ in monthly.tables(r)] == [str(i) for i in range(1, 19)]
    assert [title for number, title, _ in monthly.tables(r)][11] == "LATAM agent base"
    html = monthly_export.render_html(r)
    assert html.count('<section class="page') == 5
    assert "Follow-up</small>" not in html and "No response by country" not in html
    assert not any("No response" in line for line in monthly.render_text(r))
    wb = openpyxl.load_workbook(monthly_export.write_xlsx(r, tmp_path))
    try:
        first = [str(row[0] or "") for row in wb["Report"].iter_rows(values_only=True)]
    finally:
        wb.close()
    assert not any("No response" in v for v in first)

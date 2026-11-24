"""Batch 17l: the month report's first tile is the base by requester, no
small line under Air / Ocean / Other, and section 12's black line per
month.

Every company, place, date and number here is invented; cases from
OPP-7901. The report month is March 2027.
"""

from __future__ import annotations

import datetime as dt

import openpyxl
import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, monthly, monthly_analysis, monthly_export

FEB, MAR = "2027-02", "2027-03"
AFTER = dt.date(2027, 4, 15)


@pytest.fixture
def world(tmp_path):
    """Two LATAM agents (Larchmere and Wrenford, Chile), a Non-LATAM agent
    (Quenby, China), a direct customer and an office; January to March."""
    conn = db.open_db(tmp_path / "l.db")
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    p = {
        "larch": add_party(repo, "Larchmere Cargo", country="Chile"),
        "wren": add_party(repo, "Wrenford Freight", country="Chile"),
        "quen": add_party(repo, "Quenby Forwarding", country="China"),
        "direct": add_party(repo, "Ottery Ceramics", ptype="Direct Customer", country="USA"),
        "office": add_party(repo, "JoJo Saltmarsh", ptype="Office", country="USA"),
    }
    n = [7901]
    ids = {}

    def case(key, party, day, mode="Air", dest="Chile"):
        opp = "OPP-{}".format(n[0])
        n[0] += 1
        ids[key] = add_enquiry(repo, opp, p[party], received=day, status="Quoted", service_mode=mode,
                               origin_country="USA", dest_country=dest)
        repo.set_status(ids[key], "Quoted", day)

    case("larch_jan", "larch", "2027-01-11")
    repo.set_status(ids["larch_jan"], "Won", "2027-01-20")        # January: Larchmere awarded us
    case("larch_feb", "larch", "2027-02-02")                       # February: only the older Won
    case("wren_feb", "wren", "2027-02-03")
    repo.set_status(ids["wren_feb"], "Won", "2027-03-05")          # won after February's closing day
    # an Other case (SC1: a Truck - RORO counts as Ocean since SC1)
    case("quen_roro", "quen", "2027-02-09", mode="Truck", dest="China")
    case("larch_mar", "larch", "2027-03-02")
    repo.set_status(ids["larch_mar"], "Won", "2027-03-18")
    case("quen_mar", "quen", "2027-03-04", dest="China")
    case("quen_cancel", "quen", "2027-03-08", dest="China")
    repo.set_status(ids["quen_cancel"], "Cancelled", "2027-03-12", closed_reason="Shipment cancelled")
    # SC1: to Chile - a direct customer's case from October that is not LATAM is not counted
    case("direct_mar", "direct", "2027-03-09", dest="Chile")
    case("office_mar", "office", "2027-03-10")
    repo.commit()
    yield conn, repo, ids
    conn.close()


# ------------------------------------------------------- the first tile --
def test_the_first_tile_with_all_three_groups(world, tmp_path):
    conn = world[0]
    r = monthly.build(conn, MAR, AFTER)
    assert r.kpis[0] == ("3 / 1 / 1", "Agents / Direct customers / Our offices")
    assert sum(int(n) for n in r.kpis[0][0].split(" / ")) == 5          # the base's total
    assert {row.label: row.cells for row in r.by_scope.rows}["Total"][0] == "5"
    html = monthly_export.render_html(r)
    assert "<b>3 / 1 / 1</b><span>Agents / Direct customers / Our offices</span>" in html
    assert "Rate requests received" not in html
    assert "Agents / Direct customers / Our offices 3 / 1 / 1" in monthly.render_text(r)
    wb = openpyxl.load_workbook(monthly_export.write_xlsx(r, tmp_path))
    try:
        got = [list(row[:2]) for row in wb["Report"].iter_rows(values_only=True)]
    finally:
        wb.close()
    assert ["Agents / Direct customers / Our offices", "3 / 1 / 1"] in got


def test_a_month_with_no_direct_customer_has_two_numbers(world):
    r = monthly.build(world[0], FEB, AFTER)
    assert r.kpis[0] == ("3 / 0", "Agents / Our offices")


# ------------------------------------- no small line under Air / Ocean --
def test_no_small_line_under_air_ocean_other_and_the_cancelled_note_stays(world):
    conn = world[0]
    feb = monthly.build(conn, FEB, AFTER)                   # an Other case
    assert ("2 / 0 / 1", "Air / Ocean / Other") in feb.kpis and feb.kpi_notes == {}
    html = monthly_export.render_html(feb)
    assert "<b>2 / 0 / 1</b><span>Air / Ocean / Other</span>" in html and " truck</small>" not in html
    assert not hasattr(feb, "other_kinds")
    mar = monthly.build(conn, MAR, AFTER)                   # none, one cancelled
    assert ("5 / 0", "Air / Ocean") in mar.kpis and mar.kpi_notes == {"Lost": "1 cancelled"}
    assert "<small>1 cancelled</small><span>Lost</span>" in monthly_export.render_html(mar)
    assert "(1 cancelled)" in "\n".join(monthly.render_text(mar))


# ----------------------------------------------- section 12 per month --
def section_12(conn, month=MAR, today=AFTER):
    return monthly.build(conn, month, today).analysis[0]


def test_the_line_counts_the_month_s_own_requests_won_by_its_closing_day(world):
    conn = world[0]
    base = section_12(conn)
    # November, December: nobody; January: Larchmere; February: Larchmere's
    # Won is on January's request, Wrenford's came on 5 March; March: Larchmere
    assert [row.cells[4] for row in base.table.rows] == ["0", "0", "1", "0", "1"]
    assert base.chart["awarded"] == [0, 0, 1, 0, 1]
    assert base.table.columns[-1] == "Awarded us that month"
    assert (monthly_analysis.INK, "Awarded us that month") in base.legend
    # the bullet stays cumulative: Wrenford's February Won (5 March) is new in March
    assert monthly_analysis.plain(base.bullets[2]) == (
        "Agents that have ever awarded us: 2 (Larchmere Cargo, Wrenford Freight); new in March: Wrenford Freight")


def test_the_running_month_counts_to_today(world):
    conn = world[0]
    assert section_12(conn, today=dt.date(2027, 3, 10)).table.rows[-1].cells[4] == "0"     # Won on the 18th
    assert section_12(conn, today=dt.date(2027, 3, 20)).table.rows[-1].cells[4] == "1"

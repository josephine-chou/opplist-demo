"""Batch 17e: section 1 of the month report reads By direction under its
Total - Export, Import, Triangle always, Domestic only in a month that has
one - in section 1's columns; page 1 gets a dense class when sections 1 and
2 run long. Every company, place and number here is invented; cases from
OPP-5901.
"""

from __future__ import annotations

import openpyxl
import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, monthly, monthly_export


@pytest.fixture
def book(tmp_path):
    conn = db.open_db(tmp_path / "dir.db")
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    p = {"wren": add_party(repo, "Wrenhollow Cargo", country="Chile"),
         "pike": add_party(repo, "Pikestaff Freight", country="USA"),
         "dale": add_party(repo, "Dalewick Mills", ptype="Direct Customer", country="USA")}
    n = [5901]

    def case(party, month, status="Quoted", mode="Air", origin="USA", dest="Chile", **extra):
        opp = "OPP-{}".format(n[0])
        n[0] += 1
        values = dict(received=month + "-10", status=status, service_mode=mode, origin_country=origin,
                      dest_country=dest)
        values.update(extra)
        return add_enquiry(repo, opp, p[party], **values)

    # September: two air exports (one Won), an FCL import (Lost), an LCL
    # triangle, a RORO export, and a case with no destination country
    case("wren", "2026-09", "Won", "Air", "USA", "Chile")
    case("pike", "2026-09", "Quoted", "Air", "USA", "China")
    case("wren", "2026-09", "Lost", "FCL", "Chile", "USA", closed_reason="Price too high")
    case("pike", "2026-09", "Quoted", "LCL", "China", "Chile")
    case("wren", "2026-09", "Quoted", "RORO", "USA", "Chile")
    case("pike", "2026-09", "Quoted", "Air", "USA", None)
    # October: one Domestic case (SC1: an agent's - a direct customer's
    # domestic case from October is not LATAM, so the report leaves it out)
    case("pike", "2026-10", "Quoted", "FCL", "USA", "USA")
    case("wren", "2026-10", "Quoted", "Air", "USA", "Chile")
    repo.commit()
    yield conn, repo, p, case
    conn.close()


def rows(table):
    return [(r.label, r.cls, r.cells) for r in table.rows]


def test_the_head_row_and_the_three_directions_in_order_with_their_counts(book):
    conn = book[0]
    r = monthly.build(conn, "2026-09")
    got = rows(r.by_scope)
    total = [i for i, row in enumerate(got) if row[0] == "Total"][0]
    # SC1: the RORO export has its own column, where Other was
    assert got[total] == ("Total", "total", ["6", "3", "1", "1", "1", "1", "1"])
    assert got[total + 1:] == [
        ("By direction", "head", []),
        ("Export", "indent", ["3", "2", "0", "0", "1", "1", "0"]),       # an air export lands in Air, Won in Won
        ("Import", "indent", ["1", "0", "1", "0", "0", "0", "1"]),       # the FCL import, and its Lost
        ("Triangle", "indent", ["1", "0", "0", "1", "0", "0", "0"]),
        ("Of which handled by our other offices: 0", "note", [])]           # SC1
    # the case with no destination country is in Total and in no direction row
    directions = got[total + 2:total + 5]
    assert sum(int(row[2][0]) for row in directions) == 5
    assert "Domestic" not in [row[0] for row in got]


def test_a_domestic_row_only_in_a_month_that_has_one(book):
    conn = book[0]
    got = rows(monthly.build(conn, "2026-10").by_scope)
    labels = [row[0] for row in got]
    assert labels[labels.index("By direction"):-1] == ["By direction", "Export", "Import", "Triangle", "Domestic"]
    assert ("Domestic", "indent", ["1", "0", "1", "0", "0", "0"]) in got
    assert ("Import", "indent", ["0", "0", "0", "0", "0", "0"]) in got     # always, even at zero


def test_the_title_the_xlsx_and_the_text_carry_the_rows(book, tmp_path):
    conn = book[0]
    r = monthly.build(conn, "2026-09")
    assert monthly.tables(r)[0][:2] == ("1", "By scope, requester and direction")
    html = monthly_export.render_html(r)
    assert '<h2><span class="sn">1.</span>By scope, requester and direction</h2>' in html
    assert '<tr class="head"><td colspan="8">By direction</td></tr>' in html
    wb = openpyxl.load_workbook(monthly_export.write_xlsx(r, tmp_path))
    try:
        sheet = [row for row in wb["Report"].iter_rows(values_only=True)]
        first = [row[0] for row in sheet]
        assert "1. By scope, requester and direction" in first
        at = first.index("By direction")
        assert [first[at + 1], first[at + 2], first[at + 3]] == ["Export", "Import", "Triangle"]
        assert [str(v) for v in sheet[at + 2][1:8]] == ["1", "0", "1", "0", "0", "0", "1"]
    finally:
        wb.close()
    text = monthly.render_text(r)
    assert "1. By scope, requester and direction" in text
    assert text[text.index("  By direction") + 1] == "  Export | 3 | 2 | 0 | 0 | 1 | 1 | 0"


def test_page_one_is_dense_only_when_sections_one_and_two_run_long(book):
    conn, repo, p, case = book
    r = monthly.build(conn, "2026-09")
    assert not r.dense_overview
    assert '<section class="page"><h1>SEPTEMBER REPORT <small>Overview</small>' in monthly_export.render_html(r)
    # a heavy month: direct customers under both scopes, a Domestic row,
    # six lost reasons, three cancelled cases
    month = "2026-11"
    case("dale", month, "Quoted", "Air", "USA", "Chile")
    case("dale", month, "Quoted", "Air", "USA", "China")
    case("pike", month, "Quoted", "FCL", "USA", "USA")
    for reason in ("Price too high", "Rate too slow", "Went with another forwarder", "Customer arranged it",
                   "No vendor rate", "No response"):
        case("wren", month, "Lost", "Air", "USA", "Chile", closed_reason=reason)
    for _ in range(3):
        case("pike", month, "Cancelled", "Air", "USA", "China", closed_reason="Shipment cancelled")
    repo.commit()
    heavy = monthly.build(conn, month)
    assert monthly.overview_rows(heavy) > monthly.DENSE_OVERVIEW_ROWS and heavy.dense_overview
    assert '<section class="page dense"><h1>NOVEMBER REPORT <small>Overview</small>' in \
        monthly_export.render_html(heavy)

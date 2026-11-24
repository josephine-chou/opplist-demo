"""Batch 17k: the month report's win rate is won / quoted on the closing
day, a quote still without an answer counted as not won (Cancelled stays
out; New and Dropped are not quoted).

Every company, place, date and number here is invented; cases from
OPP-7101. The report month is March 2027 (February before it).
"""

from __future__ import annotations

import datetime as dt

import openpyxl
import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, monthly, monthly_analysis, monthly_export, monthly_requests

FEB, MAR = "2027-02", "2027-03"
LATER = dt.date(2027, 4, 20)          # well after March closed


@pytest.fixture
def world(tmp_path):
    conn = db.open_db(tmp_path / "rate.db")
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    p = {"pell": add_party(repo, "Pellmoor Cargo", country="Chile"),
         "vask": add_party(repo, "Vaskel Logistics", country="China"),
         "wren": add_party(repo, "Wrenfield Ceramics", ptype="Direct Customer", country="USA")}
    db.set_setting(conn, "closed_reasons_lost", ["Price too high", "No response from agent", "Other"])
    n = [7101]

    def case(day, who="pell", quoted=None, **extra):
        """A case received on `day`, quoted on `quoted` (default: the same
        day; False: never)."""
        got = add_enquiry(repo, "OPP-{}".format(n[0]), p[who], received=day, service_mode="Air",
                          # SC1: the direct customer's lane touches LATAM, so the report counts it
                          origin_country="USA", dest_country="China" if who == "vask" else "Chile",
                          air_weight_kg=180, **extra)
        n[0] += 1
        if quoted is not False:
            repo.set_status(got, "Quoted", quoted or day)
        repo.commit()
        return got

    return conn, repo, case, tmp_path


def section_4(report):
    return {r.label: r.cells for r in report.compare.rows}


def bullets(card):
    return [monthly_analysis.plain(b) for b in card.bullets]


# ---------------------------------------------------------------- the rule --
def test_won_over_won_lost_and_quoted_on_the_closing_day(world):
    conn, repo, case, _ = world
    for day in ("2027-03-02", "2027-03-03"):
        repo.set_status(case(day), "Won", "2027-03-20")
    repo.set_status(case("2027-03-04"), "Lost", "2027-03-21", closed_reason="Price too high")
    case("2027-03-05")                                                        # still Quoted
    repo.set_status(case("2027-03-06"), "Cancelled", "2027-03-22", closed_reason="Order not confirmed")
    case("2027-03-07", quoted=False)                                          # New: not quoted
    repo.set_status(case("2027-03-08"), "Dropped", "2027-03-23", closed_reason="Duplicate of OPP-7101")
    repo.commit()
    report = monthly.build(conn, MAR, LATER)
    # 2 won of 4 quoted (won, won, lost, quoted); cancelled, new and dropped out
    assert section_4(report)["Win rate"][1] == "50%"
    assert monthly_requests.win_rate_on(conn, MAR, "2027-03-31") == (2, 1, 50)


def test_a_quote_after_the_closing_day_is_out_and_a_later_win_is_quoted_on_it(world):
    conn, repo, case, _ = world
    repo.set_status(case("2027-03-09"), "Won", "2027-03-25")
    case("2027-03-10", quoted="2027-04-02")                                   # New on 31 March
    late_win = case("2027-03-11")
    repo.set_status(late_win, "Won", "2027-04-05")                            # Quoted on 31 March
    repo.commit()
    report = monthly.build(conn, MAR, LATER)
    assert section_4(report)["Won"][1] == "1"
    assert section_4(report)["Win rate"][1] == "50%"                          # 1 of 2 quoted
    # read on 10 April the same cases give 2 of 3
    assert monthly_requests.win_rate_on(conn, MAR, "2027-04-10")[2] == 67


def test_the_agents_and_offices_row_on_those_cases_only_and_a_dash_when_nothing_is_quoted(world):
    conn, repo, case, _ = world
    repo.set_status(case("2027-03-12"), "Won", "2027-03-18")
    case("2027-03-13", who="vask")                                            # an agent's open quote
    repo.set_status(case("2027-03-14", who="wren"), "Won", "2027-03-19")      # a direct customer
    case("2027-03-15", who="wren")
    case("2027-02-20", quoted=False)                                          # February: nothing quoted
    repo.commit()
    report = monthly.build(conn, MAR, LATER)
    rows = section_4(report)
    assert "Win rate" not in rows
    assert rows["Win rate, agents and offices"] == ["", "50%", ""]            # 1 of 2, Wrenfield left out
    assert monthly_requests.win_rate_on(conn, FEB, "2027-02-28") == (0, 0, None)


# -------------------------------------------------------------- section 18 --
def test_section_18_reads_the_rule_on_both_days(world):
    conn, repo, case, _ = world
    repo.set_status(case("2027-02-03"), "Won", "2027-02-15")
    a, b, c = case("2027-02-04"), case("2027-02-05"), case("2027-02-06")    # open on 28 Feb
    case("2027-02-07", quoted="2027-03-04")                                   # New on 28 Feb
    repo.set_status(a, "Lost", "2027-03-08", closed_reason="No response from agent")
    repo.set_status(b, "Won", "2027-03-09")
    repo.commit()
    got = bullets(monthly.build(conn, MAR, LATER).requests[3])
    # 28 Feb: 1 won of 4 quoted (25%); 31 March: 2 won of 5 quoted (40%)
    assert "February win rate: 25% on Feb 28, 40% on Mar 31; 1 lost since, 1 of them No response" in got


def test_section_18_shows_a_dash_for_a_day_with_nothing_quoted(world):
    conn, repo, case, _ = world
    x = case("2027-02-10", quoted="2027-03-02")
    repo.set_status(x, "Won", "2027-03-12")
    repo.commit()
    got = bullets(monthly.build(conn, MAR, LATER).requests[3])
    assert "February win rate: - on Feb 28, 100% on Mar 31" in got


# ------------------------------------------------------- where it is stated --
def test_the_heading_states_the_rule_and_the_xlsx_and_the_text_carry_the_note(world):
    conn, repo, case, path = world
    repo.set_status(case("2027-03-16"), "Won", "2027-03-26")
    case("2027-02-11")
    repo.commit()
    report = monthly.build(conn, MAR, LATER)
    html = monthly_export.render_html(report)
    assert ('<h2><span class="sn">4.</span>March vs February '
            '<small>win rate = won ÷ quoted, open quotes included</small></h2>') in html
    assert all(r.cls != "note" for r in report.compare.rows)
    assert report.compare.rows[-1].label == "New LATAM agents"

    text = monthly.render_text(report)
    at = text.index("4. March vs February")
    assert text[at + 1:at + 2] == ["   | February | March | Change"]
    assert text[at + 2 + len(report.compare.rows)] == "  " + monthly.WIN_RATE_NOTE
    assert monthly.WIN_RATE_NOTE == ("Win rate = won ÷ quoted on each month's closing day; "
                                     "a quote still without an answer counts as not won.")

    wb = openpyxl.load_workbook(monthly_export.write_xlsx(report, path))
    try:
        first = [row[0] for row in wb["Report"].iter_rows(values_only=True)]
    finally:
        wb.close()
    assert first.count(monthly.WIN_RATE_NOTE) == 1
    assert first[first.index(monthly.WIN_RATE_NOTE) - 1] == "New LATAM agents"

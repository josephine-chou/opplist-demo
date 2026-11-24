"""Batch 17j: a month's report closes on its last day. Every outcome in it
is the case's state on that day, read from its timeline; corrections
(a duplicate dropped, scope, the requester...) apply whenever they are
made; section 18 shows how the previous month's win rate moved since.

Every company, place, date and number here is invented; cases from
OPP-7001. The report month is March 2027 (February before it).
"""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, monthly, monthly_analysis

FEB, MAR = "2027-02", "2027-03"
LATER = dt.date(2027, 4, 20)          # well after March closed


@pytest.fixture
def world(tmp_path):
    conn = db.open_db(tmp_path / "close.db")
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    p = {"tarn": add_party(repo, "Tarnwick Freight", country="Chile"),
         "ober": add_party(repo, "Oberly Transit", country="China"),
         "hale": add_party(repo, "Halesworth Mills", ptype="Unknown", country="USA")}
    db.set_setting(conn, "closed_reasons_lost", ["Price too high", "No response from agent",
                                                 "No response from customer", "Other"])
    n = [7001]

    def case(day, who="tarn", quoted=True, **extra):
        got = add_enquiry(repo, "OPP-{}".format(n[0]), p[who], received=day, service_mode="Air",
                          origin_country="USA", dest_country="Chile" if who == "tarn" else "China",
                          air_weight_kg=250, **extra)
        n[0] += 1
        if quoted:
            repo.set_status(got, "Quoted", day)
        repo.commit()
        return got

    return conn, repo, case


def kpi(report, label):
    return next(v for v, name in report.kpis if name == label)


def requests(report) -> int:
    """17l: the month's total, the first tile's numbers added up."""
    return sum(int(n) for n in report.kpis[0][0].split(" / "))


def section_4(report):
    return {r.label: r.cells for r in report.compare.rows}


# --------------------------------------------------------- the closing day --
def test_a_case_won_after_its_month_is_open_in_its_months_report(world):
    conn, repo, case = world
    won = case("2027-03-10")
    repo.set_status(won, "Won", "2027-04-03")
    repo.commit()
    report = monthly.build(conn, MAR, LATER)
    assert report.close_day == "2027-03-31"
    assert kpi(report, "Won") == "0" and kpi(report, "Lost") == "0"
    total = next(r for r in report.by_scope.rows if r.label == "Total")
    assert total.cells[-2:] == ["0", "0"]                                  # section 1: Won, Lost
    assert section_4(report)["Won"][1] == "0"
    agent = next(r for r in report.top_agents.rows if r.label == "Tarnwick Freight")
    assert agent.cells[5] == ""                                           # section 9's Won
    sizes = monthly.build(conn, MAR, LATER).requests[0]
    assert [r.cells for r in sizes.table.rows if r.label == "100-499"] == [["0", "0", "1"]]   # 15: open
    # the same month re-exported later reads the same
    again = monthly.build(conn, MAR, dt.date(2027, 9, 1))
    assert kpi(again, "Won") == "0"
    # while March runs, it closes on today: on 20 March nothing is won either
    assert monthly.build(conn, MAR, dt.date(2027, 3, 20)).close_day == "2027-03-20"


def test_a_case_lost_on_the_20th_and_reopened_next_month_is_lost_in_its_month(world):
    conn, repo, case = world
    lost = case("2027-03-05")
    repo.set_status(lost, "Lost", "2027-03-20", closed_reason="Price too high")
    repo.set_status(lost, "Quoted", "2027-04-03")                       # reopened
    repo.commit()
    assert conn.execute("SELECT status FROM enquiry WHERE id = ?", (lost,)).fetchone()[0] == "Quoted"
    report = monthly.build(conn, MAR, LATER)
    assert kpi(report, "Lost") == "1"
    assert report.lost_bars == [("Price too high", 1)]                   # the reason on the Lost event
    lost16 = report.requests[1]
    assert (lost16.big, lost16.caption) == ("1 of 1", "March losses on price")
    sizes = report.requests[0]
    assert [r.cells for r in sizes.table.rows if r.label == "100-499"] == [["0", "1", "0"]]   # 15: lost


def test_a_lost_event_dated_the_30th_and_entered_next_month_counts_in_the_month(world):
    conn, repo, case = world
    lost = case("2027-03-08")
    repo.set_status(lost, "Lost", "2027-03-30", closed_reason="No response from agent")
    conn.execute("UPDATE status_event SET created_at = '2027-04-02 09:15:00' WHERE enquiry_id = ? AND status = 'Lost'",
                 (lost,))
    late = case("2027-03-09")
    repo.set_status(late, "Lost", "2027-04-01", closed_reason="Price too high")   # dated after the month
    repo.commit()
    report = monthly.build(conn, MAR, LATER)
    assert kpi(report, "Lost") == "1"
    assert report.lost_bars == [("No response from agent", 1)]


def test_a_cancelled_case_and_its_reason_on_the_day(world):
    conn, repo, case = world
    gone = case("2027-03-11")
    repo.set_status(gone, "Cancelled", "2027-03-25", closed_reason="Order not confirmed")
    after = case("2027-03-12")
    repo.set_status(after, "Cancelled", "2027-04-04", closed_reason="Shipment cancelled")
    repo.commit()
    report = monthly.build(conn, MAR, LATER)
    assert [(c["opp_no"], c["reason"]) for c in report.cancelled] == [("OPP-7001", "Order not confirmed")]
    assert report.kpi_notes["Lost"] == "1 cancelled"


def test_a_duplicate_dropped_later_leaves_the_months_count(world):
    conn, repo, case = world
    case("2027-03-02")
    twin = case("2027-03-03")
    assert requests(monthly.build(conn, MAR, LATER)) == 2
    # a correction, whenever it is made, applies to the month
    repo.set_status(twin, "Dropped", "2027-04-12", closed_reason="Duplicate of OPP-7001")
    repo.commit()
    report = monthly.build(conn, MAR, LATER)
    assert requests(report) == 1
    assert section_4(report)["Requests"][1] == "1"


def test_the_check_box_stays_on_the_live_data(world):
    conn, repo, case = world
    lost = case("2027-03-04")
    repo.set_status(lost, "Lost", "2027-04-06")                           # no reason, after March
    repo.commit()
    report = monthly.build(conn, MAR, LATER)
    assert kpi(report, "Lost") == "0"
    groups = {g["title"]: g for g in report.checks}
    assert [i["opp_nos"] for i in groups["Lost with no reason"]["items"]] == [["OPP-7001"]]


# --------------------------------------------------------------- section 4 --
def test_section_4s_previous_month_is_read_on_its_own_last_day(world):
    conn, repo, case = world
    feb_won = case("2027-02-15")
    feb_lost = case("2027-02-16")
    repo.set_status(feb_lost, "Lost", "2027-02-26", closed_reason="Price too high")
    repo.set_status(feb_won, "Won", "2027-03-05")                         # decided in March
    mar = case("2027-03-15")
    repo.set_status(mar, "Won", "2027-03-20")
    repo.commit()
    rows = section_4(monthly.build(conn, MAR, LATER))
    # February on 28 Feb: nothing won, one lost; March on 31 March: one won
    assert rows["Won"][:2] == ["0", "1"]
    assert rows["Win rate"][:2] == ["0%", "100%"]
    # February's own report says the same
    feb = section_4(monthly.build(conn, FEB, LATER))
    assert feb["Won"][1] == "0" and feb["Win rate"][1] == "0%"


# -------------------------------------------------------------- section 18 --
def bullets(card):
    return [monthly_analysis.plain(b) for b in card.bullets]


def test_section_18_has_the_previous_months_win_rate_and_the_no_response_count(world):
    conn, repo, case = world
    won, lost = case("2027-02-03"), case("2027-02-04")
    repo.set_status(won, "Won", "2027-02-20")
    repo.set_status(lost, "Lost", "2027-02-22", closed_reason="Price too high")
    a, b, c, d = (case("2027-02-1{}".format(i)) for i in range(4))
    repo.set_status(a, "Lost", "2027-03-08", closed_reason="No response from agent")
    repo.set_status(b, "Lost", "2027-03-09", closed_reason="No response from customer")
    repo.set_status(c, "Lost", "2027-03-10", closed_reason="Price too high")
    repo.set_status(d, "Won", "2027-03-11")
    repo.commit()
    moved = monthly.build(conn, MAR, LATER).requests[3]
    got = bullets(moved)
    # 17k: won / quoted. February on 28 Feb: 1 won, 1 lost, 4 open (17%); on 31 March: 2 won, 4 lost (33%)
    assert got[1] == "February win rate: 17% on Feb 28, 33% on Mar 31; 3 lost since, 2 of them No response"
    assert [t for t, bold in moved.bullets[1] if bold] == ["17%", "33%", "3", "2"]


def test_section_18_without_a_decision_since_has_no_win_rate_line(world):
    conn, repo, case = world
    won = case("2027-02-03")
    repo.set_status(won, "Won", "2027-02-20")
    case("2027-02-05")                                                    # still open
    repo.commit()
    got = bullets(monthly.build(conn, MAR, LATER).requests[3])
    assert not any("win rate" in b for b in got)
    # a win since, and no loss: the rates and nothing about losses
    late = case("2027-02-06")
    repo.set_status(late, "Won", "2027-03-02")
    repo.commit()
    got = bullets(monthly.build(conn, MAR, LATER).requests[3])
    assert "February win rate: 33% on Feb 28, 67% on Mar 31" in got           # 17k: 1 of 3, 2 of 3

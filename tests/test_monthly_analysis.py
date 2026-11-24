"""Batch 17f: the month report's page 4, Analysis · Agents - 12 LATAM agent
base, 13 LATAM requests by agent, 14 Where the growth came from.

Every company, place, date and number here is invented; cases from
OPP-6501. The report month is May 2027; today is 15 June 2027 unless a
test says otherwise.
"""

from __future__ import annotations

import datetime as dt
import re

import openpyxl
import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, monthly, monthly_analysis, monthly_export

MAY = "2027-05"
TODAY = dt.date(2027, 6, 15)


@pytest.fixture
def world(tmp_path):
    """LATAM agents (Amberline and Dunmore in Chile, Bramblewick and Foxglove
    in Brazil, Cinderford in Peru, Elmstead in Colombia), a Non-LATAM agent,
    an office; January to July 2027."""
    path = tmp_path / "a.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    for name in ("Peru", "Colombia"):
        repo.insert("country", {"name": name, "region": "Latin America"})
    p = {
        "amber": add_party(repo, "Amberline Cargo", country="Chile"),
        "bramble": add_party(repo, "Bramblewick Freight", country="Brazil"),
        "cinder": add_party(repo, "Cinderford Agents", country="Peru"),
        "dun": add_party(repo, "Dunmore Logistics", country="Chile"),
        "elm": add_party(repo, "Elmstead Shipping", country="Colombia"),
        "fox": add_party(repo, "Foxglove Transit", country="Brazil"),
        "gorse": add_party(repo, "Gorsebrook Freight", country="China"),
        "office": add_party(repo, "JoJo Harbour", ptype="Office", country="USA"),
    }
    n = [6501]
    ids = {}

    def case(party, day, status="Quoted", key=None, dest="Chile", **extra):
        opp = "OPP-{}".format(n[0])
        n[0] += 1
        got = add_enquiry(repo, opp, p[party], received=day, status=status, service_mode="Air",
                          origin_country="USA", dest_country=dest, **extra)
        if key:
            ids[key] = got
        return got

    case("amber", "2027-01-10", "Won")                    # Amberline has awarded us since January
    case("amber", "2027-02-03")
    case("fox", "2027-02-05", "Dropped", closed_reason="Registered in error")   # not active; first ever
    case("dun", "2027-03-04", key="dun_march")            # won in May (below)
    case("amber", "2027-04-06")
    for day in ("2027-04-07", "2027-04-08", "2027-04-09"):
        case("cinder", day, dest="Peru")
    case("gorse", "2027-04-12", dest="China")
    case("gorse", "2027-04-13", dest="China")
    case("office", "2027-04-14")
    # May: Amberline 4 (one won), Bramblewick 2 and Elmstead 2 (new), Dunmore and Foxglove 1
    case("amber", "2027-05-03", "Won")
    for day in ("2027-05-04", "2027-05-05", "2027-05-06"):
        case("amber", day)
    case("bramble", "2027-05-07")
    case("bramble", "2027-05-08")
    case("dun", "2027-05-09")
    case("elm", "2027-05-10", key="elm_won")
    case("elm", "2027-05-11")
    case("fox", "2027-05-12")
    for day in ("2027-05-13", "2027-05-14", "2027-05-17"):
        case("gorse", day, dest="China")
    repo.set_status(ids["dun_march"], "Won", "2027-05-20")
    repo.set_status(ids["elm_won"], "Won", "2027-06-02")    # won now, after the month's end
    # June: no LATAM agent request
    case("gorse", "2027-06-03", dest="China")
    case("office", "2027-06-04")
    repo.commit()
    yield conn, repo, p, case, path
    conn.close()


def cards(conn, month=MAY, today=TODAY):
    base, agents, growth = monthly.build(conn, month, today).analysis
    return base, agents, growth


def bullets(card):
    return [monthly_analysis.plain(b) for b in card.bullets]


def rows(table):
    return {r.label: r.cells for r in table.rows}


# ------------------------------------------------- 12. LATAM agent base --
def test_section_12_five_months_new_returning_lapsed_and_awarded(world):
    conn = world[0]
    base = cards(conn)[0]
    assert (base.big, base.caption, base.pill) == ("5", "LATAM agents sent a request in May", ("▲ 3 vs Apr", "up"))
    # Active, Returning, New, Lapsed, Awarded us that month (17l: per month)
    assert rows(base.table) == {
        "January": ["1", "0", "1", "0", "1"],
        "February": ["1", "1", "0", "0", "0"],      # Foxglove's Dropped case is not a request
        "March": ["1", "0", "1", "1", "0"],         # Dunmore new; Amberline lapsed
        "April": ["2", "1", "1", "1", "0"],         # Cinderford new; Dunmore lapsed
        "May": ["5", "3", "2", "1", "1"],           # Foxglove returning: its first case was in February
    }
    assert bullets(base) == [
        "2 first-time agents: Bramblewick Freight, Elmstead Shipping",
        "1 April agent sent nothing in May: Cinderford Agents",
        "Agents that have ever awarded us: 2 (Amberline Cargo, Dunmore Logistics); new in May: Dunmore Logistics",
    ]
    svg = base.svg
    assert svg.count(">1 lapsed</text>") == 3 and ">May</text>" in svg and ">Jan</text>" in svg
    assert 'fill="#e8456c"' in svg and 'fill="#f6b3c2"' in svg
    assert re.search(r'font-weight="600" [^>]*>1</text>\n</svg>', svg)      # the line's last value


def test_the_awarded_line_is_per_month_and_the_bullet_cumulative(world):
    conn = world[0]
    # 17l: Dunmore's March case was won on 20 May (after March's closing
    # day, and not one of May's requests); Elmstead's May case on 2 June
    assert [r.cells[4] for r in cards(conn)[0].table.rows] == ["1", "0", "0", "0", "1"]
    # a struck-out Won event counts for nothing; the bullet stays cumulative
    ev = conn.execute("SELECT id FROM status_event WHERE status = 'Won' AND at = '2027-05-20'").fetchone()[0]
    world[1].update("status_event", ev, {"voided": 1})
    world[1].commit()
    base = cards(conn)[0]
    assert base.table.rows[-1].cells[4] == "1"
    assert bullets(base)[2] == "Agents that have ever awarded us: 1 (Amberline Cargo); no new one in May"


def test_section_12_s_bullets_with_nobody_new_or_lapsed_and_the_name_caps(world):
    conn, repo, p, case, path = world
    june = cards(conn, "2027-06")[0]
    assert bullets(june)[:2] == [
        "No first-time agent in June",
        "5 May agents sent nothing in June: Amberline Cargo, Bramblewick Freight, Dunmore Logistics, "
        "Elmstead Shipping, Foxglove Transit"]
    assert june.pill == ("▼ 5 vs May", "down")
    # July: Amberline and seven more; August: none of the eight
    case("amber", "2027-07-01")
    for i, name in enumerate(("Hartwell", "Ivybridge", "Juniper", "Kestwick", "Larchmont", "Mossgate", "Northby")):
        add_enquiry(repo, "OPP-{}".format(6600 + i), add_party(repo, name + " Cargo", country="Chile"),
                    received="2027-07-0{}".format(2 + i % 5), status="Quoted", service_mode="Air",
                    origin_country="USA", dest_country="Chile")
    repo.commit()
    july = cards(conn, "2027-07")[0]
    assert bullets(july)[0] == ("7 first-time agents: Hartwell Cargo, Ivybridge Cargo, Juniper Cargo, Kestwick Cargo, "
                                "Larchmont Cargo, Mossgate Cargo and 1 more")
    assert bullets(july)[1] == "Every June agent sent a request again"
    # eight lapsed names: six, then 'and 2 more' (Iris, 10/12)
    august = cards(conn, "2027-08")[0]
    assert bullets(august)[1] == ("8 July agents sent nothing in August: Amberline Cargo, Hartwell Cargo, "
                                  "Ivybridge Cargo, Juniper Cargo, Kestwick Cargo, Larchmont Cargo and 2 more")
    assert august.pill == ("▼ 8 vs Jul", "down")


def test_a_month_with_no_change_has_a_grey_pill(world):
    conn, repo, p, case, path = world
    case("amber", "2027-07-05")
    case("cinder", "2027-08-05")
    repo.commit()
    assert cards(conn, "2027-08")[0].pill == ("0 vs Jul", "flat")


# --------------------------------------------- 13. LATAM requests by agent --
def test_section_13_bars_the_one_each_bar_and_count_only_labels(world):
    conn = world[0]
    agents = cards(conn)[1]
    assert (agents.big, agents.caption) == ("40%", "of May’s 10 LATAM requests from one agent, Amberline Cargo")
    labels = re.findall(r'<text x="120.0"[^>]*>([^<]*)</text>', agents.svg)
    assert labels == ["Amberline Cargo", "Bramblewick Freight", "Elmstead Shipping", "2 more, one each"]
    counts = re.findall(r'text-anchor="start"[^>]*>([^<]*)</text>', agents.svg)
    assert counts == ["4", "2", "2", "2"] and "%" not in agents.svg
    # 17j: Elmstead's win is dated 2 June, after May closed, so May has one win
    assert bullets(agents) == [
        "Amberline Cargo: 4 of 10 requests, and all 1 LATAM agent win in May",
        "The next 4: 6 requests (60%)",
        "2 agents sent one each: Dunmore Logistics, Foxglove Transit",
    ]
    # every agent of the month in the table, with its country
    assert rows(agents.table) == {"Amberline Cargo": ["Chile", "4"], "Bramblewick Freight": ["Brazil", "2"],
                                  "Elmstead Shipping": ["Colombia", "2"], "Dunmore Logistics": ["Chile", "1"],
                                  "Foxglove Transit": ["Brazil", "1"]}


def test_section_13_colours_its_countries_over_three_months(world):
    conn = world[0]
    agents = cards(conn)[1]
    fills = re.findall(r'<path [^>]*fill="([^"]+)"', agents.svg)
    # March to May: Chile 7, Brazil 3, Peru 3 (April), Colombia 2 - so Elmstead's Colombia is Others
    assert fills == ["#2a78d6", "#eb6834", "#c3c2b7", "#c3c2b7"]
    assert agents.legend == [("#2a78d6", "Chile"), ("#eb6834", "Brazil"), ("#c3c2b7", "Others")]


def test_section_13_all_the_wins_more_agents_and_a_long_name(world):
    conn, repo, p, case, path = world
    names = ["Westerholm Andina International Freight", "Oakridge", "Pinecrest", "Quarrytown", "Rookwood",
             "Sedgefield", "Thornbury", "Upton Vale"]
    for i, name in enumerate(names):
        pid = add_party(repo, name, country="Chile")
        for k in range(3 if i == 0 else 2):
            add_enquiry(repo, "OPP-{}".format(6700 + 3 * i + k), pid, received="2027-07-{:02d}".format(5 + k),
                        status="Won" if (i, k) == (0, 0) else "Quoted", service_mode="Air", origin_country="USA",
                        dest_country="Chile")
    repo.commit()
    agents = cards(conn, "2027-07", dt.date(2027, 8, 10))[1]         # 17j: July closed
    labels = re.findall(r'<text x="120.0"[^>]*>([^<]*)</text>', agents.svg)
    assert labels[-1] == "2 more agents" and len(labels) == 7
    assert labels[0].endswith("…") and labels[0].startswith("Westerholm")
    assert bullets(agents)[0] == "Westerholm Andina International Freight: 3 of 17 requests, and all 1 LATAM agent win in July"
    assert bullets(agents)[1] == "The next five: 10 requests (59%)"
    assert len(bullets(agents)) == 2                     # nobody sent one


def test_a_month_with_no_latam_agent_request(world):
    conn = world[0]
    report = monthly.build(conn, "2027-06", TODAY)
    agents = report.analysis[1]
    assert agents.empty == "No LATAM agent request in June." and not agents.svg and not agents.bullets
    html = monthly_export.render_html(report)
    assert '<span class="cn">13</span>LATAM requests by agent</h3><p class="none">No LATAM agent request in June.</p>' \
        in html
    assert "  No LATAM agent request in June." in monthly.render_text(report)


# ------------------------------------------- 14. where the growth came from --
def test_section_14_in_a_month_that_grew(world):
    conn = world[0]
    growth = cards(conn)[2]
    assert (growth.big, growth.caption, growth.pill) == ("13", "requests in May", ("▲ 86% vs Apr (7)", "up"))
    # Direct customers had no case in either month: left out; 17i: the offices after the groups
    assert [(r.label, r.cells, r.cls) for r in growth.table.rows] == [
        ("LATAM agents", ["4", "10", "+6"], ""), ("Other agents", ["2", "3", "+1"], ""),
        ("Our offices", ["1", "0", "-1"], ""), ("Our offices", [], "head"),
        ("JoJo Harbour", ["1", "0", "-1"], "")]
    assert "Direct customers" not in growth.svg
    assert re.findall(r'<text x="396.0"[^>]*>([^<]*)</text>', growth.svg) == ["+6", "+1", "-1"]
    assert bullets(growth) == [
        "LATAM agents 4 → 10, 100% of the +6",
        "Chile +4, Brazil +3, Peru -3",
        "Other agents +1",
        "Our offices 1 → 0: Harbour -1",              # 17i: a bullet of their own
    ]
    assert growth.legend == [("#c9cdd2", "April"), ("#2a78d6", "May")]


def test_section_14_in_a_month_that_fell(world):
    conn = world[0]
    growth = cards(conn, "2027-06")[2]
    assert (growth.big, growth.pill) == ("2", ("▼ 85% vs May (13)", "down"))
    assert bullets(growth) == [
        "LATAM agents 10 → 0",
        "Chile -5, Brazil -3, Colombia -2",
        "Other agents -2",
        "Our offices 0 → 1: Harbour +1; 1 on LATAM lanes",
    ]


def test_section_14_without_latam_agent_requests_or_a_month_before(world):
    conn, repo, p, case, path = world
    case("office", "2027-09-06")
    repo.commit()
    growth = cards(conn, "2027-09")[2]
    assert growth.pill is None                            # August had nothing
    # 17i: the offices lead the card, so their own bullet leaves out 0 -> 1
    assert bullets(growth) == ["Our offices 0 → 1, 100% of the +1", "Our offices: Harbour +1; 1 on LATAM lanes"]


# ------------------------------------------------------ the files --
def test_page_4_in_the_html(world):
    conn = world[0]
    html = monthly_export.render_html(monthly.build(conn, MAY, TODAY))
    page4 = html[html.index("<small>Analysis &middot; Agents</small>"):]
    assert html.count('<section class="page') == 5
    assert '<div class="g g5"><div class="card"><h3><span class="cn">12</span>LATAM agent base</h3>' in page4
    assert '<div class="g g6"><div class="card"><h3><span class="cn">13</span>' in page4
    assert '<span class="pill up">▲ 86% vs Apr (7)</span>' in page4
    assert "<li><b>2</b> first-time agents: Bramblewick Freight, Elmstead Shipping</li>" in page4
    assert "<li>LATAM agents <b>4</b> → <b>10</b>, <b>100%</b> of the +6</li>" in page4
    assert ".card{border:1px solid #E1E0D9;" in html and ".g6{grid-template-columns:1.08fr 0.92fr}" in html
    assert '<section class="page dense">' not in page4


def test_the_three_sections_in_the_xlsx_and_the_text(world, tmp_path):
    conn = world[0]
    report = monthly.build(conn, MAY, TODAY)
    numbers = [(n, t) for n, t, _ in monthly.tables(report)][11:14]
    assert numbers == [("12", "LATAM agent base"), ("13", "LATAM requests by agent"),
                       ("14", "Where the growth came from")]
    wb = openpyxl.load_workbook(monthly_export.write_xlsx(report, tmp_path))
    try:
        got = [list(r) for r in wb["Report"].iter_rows(values_only=True)]
    finally:
        wb.close()
    first = [r[0] for r in got]
    at = first.index("12. LATAM agent base")
    assert got[at + 1][:6] == ["Month", "Active", "Returning", "New", "Lapsed", "Awarded us that month"]
    assert got[at + 6][:6] == ["May", 5, 3, 2, 1, 1]
    assert first[at + 7] == "2 first-time agents: Bramblewick Freight, Elmstead Shipping"
    at = first.index("13. LATAM requests by agent")
    assert got[at + 2][:3] == ["Amberline Cargo", "Chile", 4]
    at = first.index("14. Where the growth came from")
    assert got[at + 1][:4] == ["Group", "April", "May", "Change"]
    assert got[at + 2][:4] == ["LATAM agents", 4, 10, 6]
    assert "Chile +4, Brazil +3, Peru -3" in first
    text = monthly.render_text(report)
    assert "14. Where the growth came from" in text
    assert "  LATAM agents | 4 | 10 | +6" in text
    assert "  LATAM agents 4 → 10, 100% of the +6" in text


def test_long_bullets_tighten_page_4(world, monkeypatch):
    conn = world[0]
    assert not monthly.build(conn, MAY, TODAY).dense_analysis
    # the estimate of the cards' text height past DENSE_PX (measured in Edge:
    # September 2026 on the live copy is 245 and its page 762 px of 794)
    assert monthly_analysis.text_px(monthly.build(conn, MAY, TODAY).analysis) < monthly_analysis.DENSE_PX
    monkeypatch.setattr(monthly_analysis, "DENSE_PX", 100)
    report = monthly.build(conn, MAY, TODAY)
    assert report.dense_analysis
    assert 'viewBox="0 0 979 120"' in report.analysis[0].svg
    html = monthly_export.render_html(report)
    assert '<section class="page dense"><h1>MAY REPORT <small>Analysis &middot; Agents</small>' in html
    assert ".page.dense .card{padding:6px 12px 4px}" in html

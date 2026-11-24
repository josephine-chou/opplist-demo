"""Batch 17g: the month report's page 5, Analysis · Requests - 15 What we
win, by shipment size; 16 Why we lost; 17 LATAM requests by country; 18 How
the previous month's requests moved. Also 17g's two fixes to earlier
sections: 13's first bullet when the first agent has no win, and 9's quiet
row on the page. 17h: no foot line under page 5; the cases section 15
leaves out are in the check box.

Every company, place, date and number here is invented; cases from
OPP-6601. Today is 20 July 2027 unless a test says otherwise.
"""

from __future__ import annotations

import datetime as dt
import re

import openpyxl
import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, monthly, monthly_analysis, monthly_export, monthly_requests
from opplist.web import create_app

TODAY = dt.date(2027, 7, 20)
WON, LOST, OPEN = "#4f9a24", "#b3405a", "#eda100"


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "r.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    for name in ("Peru", "Colombia", "Uruguay"):
        repo.insert("country", {"name": name, "region": "Latin America"})
    p = {
        "amber": add_party(repo, "Amberline Cargo", country="Chile"),
        "bramble": add_party(repo, "Bramblewick Freight", country="Brazil"),
        "cinder": add_party(repo, "Cinderford Agents", country="Peru"),
        "elm": add_party(repo, "Elmstead Shipping", country="Colombia"),
        "fox": add_party(repo, "Foxglove Transit", country="Uruguay"),
        "gorse": add_party(repo, "Gorsebrook Freight", country="China"),
    }
    n = [6601]
    ids = {}

    def case(party, day, status="Quoted", key=None, mode="Not specified", origin="USA", dest="Chile", **extra):
        opp = "OPP-{}".format(n[0])
        n[0] += 1
        got = add_enquiry(repo, opp, p[party], received=day, status=status, service_mode=mode,
                          origin_country=origin, dest_country=dest, **extra)
        if key:
            ids[key] = got
        return got

    def close(key_or_id, status, day, reason=None):
        repo.set_status(ids.get(key_or_id, key_or_id), status, day, closed_reason=reason)

    # ---- 15: February and March, a Non-LATAM agent's cases of every size
    for i, (status, qty, ctype) in enumerate([("Won", 1, "40HQ"), ("Lost", 3, "20GP"), ("Quoted", 7, "20GP"),
                                              ("Won", 4, "40HQ"), ("Quoted", 15, "20GP")]):
        case("gorse", "2027-03-{:02d}".format(2 + i), status, mode="FCL", dest="China", container_type=ctype,
             container_qty=qty)                                       # 2, 3, 7, 8, 15 TEU
    case("gorse", "2027-02-10", "Cancelled", mode="FCL", dest="China", container_type="40HQ", container_qty=8)
    case("gorse", "2027-03-09", mode="FCL", dest="China")              # no container: left out
    for i, (status, cbm) in enumerate([("Quoted", 0.99), ("Won", 1), ("Quoted", 4.9), ("Won", 5), ("Quoted", 9.9),
                                       ("Lost", 10)]):
        case("gorse", "2027-03-{:02d}".format(10 + i), status, mode="LCL", dest="China", lcl_cbm=cbm)
    case("gorse", "2027-02-11", mode="LCL", dest="China")              # no cbm: left out
    for i, (status, kg) in enumerate([("Quoted", 99), ("Won", 100), ("Quoted", 999), ("Won", 1000), ("Lost", 2999),
                                      ("Won", 3000)]):
        case("gorse", "2027-03-{:02d}".format(17 + i), status, mode="Air", dest="China", air_weight_kg=kg)
    # ---- 17: April
    case("amber", "2027-04-05")
    # ---- 18 (and 17): May, seven cases
    case("amber", "2027-05-05", key="m1")                  # quoted, won on 10 June
    case("amber", "2027-05-06", key="m2")                  # quoted, lost on 12 June
    case("bramble", "2027-05-07", key="m3", origin="Brazil", dest="USA")   # quoted, still open
    case("gorse", "2027-05-08", "New", key="m4", dest="China")             # never quoted
    case("gorse", "2027-05-09", "Won", key="m5", dest="China")             # won on arrival
    case("gorse", "2027-05-10", key="m6", dest="China")                    # won on 5 July, after the month
    case("gorse", "2027-05-11", key="m7", dest="China")                    # cancelled on 15 June
    close("m1", "Won", "2027-06-10")
    close("m2", "Lost", "2027-06-12", "Price too high")
    close("m6", "Won", "2027-07-05")
    close("m7", "Cancelled", "2027-06-15", "Project postponed")
    # ---- 16 and 17: June, eleven cases, seven lost with a reason
    for key, party, dest in (("p1", "amber", "Chile"), ("p2", "amber", "Chile"), ("p3", "gorse", "China")):
        case(party, "2027-06-0{}".format(len(ids) % 9 + 1), key=key, mode="Air", dest=dest, air_weight_kg=500)
        close(key, "Lost", "2027-06-20", "Price too high")
    case("bramble", "2027-06-11", "Lost", key="r1", origin="Brazil", dest="USA", closed_reason="Rate too slow")
    case("cinder", "2027-06-12", "Lost", key="r2", dest="Peru", closed_reason="Rate too slow")
    for key, reason in (("w1", "Went with another forwarder"), ("o1", "Other")):
        case("gorse", "2027-06-13", key=key, dest="China")
        close(key, "Lost", "2027-06-21", reason)
    case("amber", "2027-06-14")
    case("bramble", "2027-06-15", origin="Brazil", dest="USA")
    case("elm", "2027-06-16", dest="Colombia")
    case("fox", "2027-06-17", dest="Uruguay")
    repo.commit()
    yield conn, repo, p, case, close, ids, path
    conn.close()


def build(conn, month, today=TODAY):
    return monthly.build(conn, month, today)


def cards(conn, month, today=TODAY):
    return dict(zip(("15", "16", "17", "18"), build(conn, month, today).requests))


def bullets(card):
    return [monthly_analysis.plain(b) for b in card.bullets]


def rows(table):
    return {r.label: r.cells for r in table.rows}


# ------------------------------------------------ 15. what we win, by size --
def test_section_15_columns_at_their_edges_and_the_colours(world):
    conn = world[0]
    size = cards(conn, "2027-03")["15"]
    got = rows(size.table)            # label -> won, lost or cancelled, open
    assert [got[k] for k in ("1", "2", "3-7", "8-15", "16+")] == [
        ["0", "0", "0"], ["1", "0", "0"], ["0", "1", "1"], ["1", "0", "1"], ["0", "1", "0"]]
    assert [got[k] for k in ("<1", "1-4.9", "5-9.9", "10+")] == [
        ["0", "0", "1"], ["1", "0", "1"], ["1", "0", "1"], ["0", "1", "0"]]
    assert [got[k] for k in ("<100 kg", "100-499", "500-999", "1-2.9 t", "3 t+")] == [
        ["0", "0", "1"], ["1", "0", "0"], ["0", "0", "1"], ["1", "1", "0"], ["1", "0", "0"]]
    # each column stacks won, then lost or cancelled, then open
    fcl = size.svg.split("</svg>")[0]
    assert re.findall(r'fill="(#[0-9a-f]+)"/>', fcl) == [WON, LOST, OPEN, WON, OPEN, LOST]
    assert ">FCL, TEU per request</text>" in size.svg and "&lt;100 kg" in size.svg
    assert (size.big, size.caption, size.pill) == (
        "1 of 3", "FCL requests of 8 TEU or more won (39 TEU)", ("February and March", "flat"))
    assert bullets(size) == [
        "FCL: every win is 2 or 8 TEU (2 wins); the 3 requests of 8 TEU or more: 1 won, 1 still open",
        "LCL: 2 wins, 1 to 5 cbm",
        "Air: wins up to 3 t; 1 of the 1 request of 3 t or more won",
    ]


def test_page_5_has_no_foot_line(world):
    conn = world[0]
    for month in ("2026-12", "2027-03"):
        report = build(conn, month)
        assert not hasattr(report, "foot")
        html = monthly_export.render_html(report)
        assert 'class="fn"' not in html and "p.fn" not in html and "Status as of" not in html
    assert not hasattr(monthly_requests, "foot")
    # a month with no FCL, LCL or air case at all: no chart and no bullets
    size = cards(conn, "2027-01")["15"]
    assert size.empty and not size.bullets


def size_group(conn, month):
    return [g for g in monthly.checks(conn, month) if g["title"] == "No size for section 15"]


def test_the_check_box_lists_the_cases_section_15_leaves_out(world):
    conn, repo, p, case, close, ids, path = world
    # a Customs only case marked FCL with no size: not in section 15, not listed
    customs = case("gorse", "2027-03-25", mode="FCL", dest="China")
    route = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ?", (customs,)).fetchone()[0]
    repo.update("enquiry_route", route, {"service_scope": "Customs only"})
    repo.commit()
    order = ["No origin or destination country", "Country unknown", "No mode", "No size for section 15",
             "Air with containers", "Lost with no reason"]
    titles = [g["title"] for g in monthly.checks(conn, "2027-03")]
    assert "No size for section 15" in titles and [t for t in order if t in titles] == [t for t in titles if t in order]
    [group] = size_group(conn, "2027-03")
    # March's FCL case without containers, February's LCL case without cbm
    # (marked with its month); the air cases all have their weight
    assert [(i["opp_nos"], i["who"], i["text"]) for i in group["items"]] == [
        (["OPP-6607"], "Gorsebrook Freight", "USA to China, FCL, no container count or type"),
        (["OPP-6614"], "Gorsebrook Freight", "(February) USA to China, LCL, no cbm"),
    ]
    assert sum(m["missing"] for m in monthly_requests.sizes(conn, "2027-03").values()) == 2
    # April shows March and April: only the FCL case, now without a month
    [group] = size_group(conn, "2027-04")
    assert [i["text"] for i in group["items"]] == ["(March) USA to China, FCL, no container count or type"]
    assert size_group(conn, "2027-06") == []           # the group only when it has something
    # on the Report page, linked to the case
    page = create_app(path).test_client().get("/report?month=2027-03").data.decode("utf-8")
    assert "<b>No size for section 15</b>" in page
    assert '<a href="/enquiries/{}">OPP-6614</a> Gorsebrook Freight <em>(February) USA to China, LCL, no cbm</em>'.format(
        ids_of(conn, "OPP-6614")) in page
    assert "No size for section 15" not in monthly_export.render_html(build(conn, "2027-03"))


def ids_of(conn, opp):
    return conn.execute("SELECT id FROM enquiry WHERE opp_no = ?", (opp,)).fetchone()[0]


# ------------------------------------------------------- 16. why we lost --
def test_section_16_the_donut_the_caption_and_the_bullets(world):
    conn = world[0]
    lost = cards(conn, "2027-06")["16"]
    assert (lost.big, lost.caption) == ("3 of 7", "June losses on price")
    assert rows(lost.table) == {"Price too high": ["3"], "Rate too slow": ["2"], "Other": ["1"],
                                "Went with another forwarder": ["1"]}
    assert re.findall(r'stroke="(#[0-9a-f]+)"', lost.svg) == ["#b3405a", "#9aa3ad", "#d1d5da", "#e1e4e7"]
    assert 'class="dn">7</text>' in lost.svg and 'class="dl">lost</text>' in lost.svg
    assert "<span><i style=\"background:#9aa3ad\"></i>Rate too slow</span><b>2</b>" in lost.svg
    assert bullets(lost) == [
        "Price too high: 3; all air exports; 2 are Amberline Cargo, USA to Chile",
        "Rate too slow: 2; never quoted: Bramblewick Freight (Brazil to USA), Cinderford Agents (USA to Peru)",
    ]


def test_section_16_the_other_caption_some_of_them_and_one_case(world):
    conn, repo, p, case, close, ids, path = world
    for i, (mode, origin, dest) in enumerate([("FCL", "China", "USA"), ("FCL", "Chile", "USA"), ("Air", "USA", "Chile")]):
        got = case("gorse", "2027-07-0{}".format(i + 1), mode=mode, origin=origin, dest=dest)
        close(got, "Lost", "2027-07-10", "Rate too slow")
    got = case("gorse", "2027-07-05", mode="Air", dest="China")
    close(got, "Lost", "2027-07-11", "Price too high")
    repo.commit()
    lost = cards(conn, "2027-07")["16"]
    assert (lost.big, lost.caption) == ("3 of 4", "July losses: Rate too slow")
    assert bullets(lost) == ["Rate too slow: 3; 2 of them FCL imports", "Price too high: 1; an air export"]


def test_section_16_names_four_never_quoted_cases_then_more(world):
    conn, repo, p, case, close, ids, path = world
    for i in range(5):
        case("gorse", "2027-08-0{}".format(i + 1), "Lost", dest="China", closed_reason="Rate too slow")
    repo.commit()
    assert bullets(cards(conn, "2027-08", dt.date(2027, 9, 10))["16"]) == [      # 17j: August closed
        "Rate too slow: 5; all exports; 5 are Gorsebrook Freight, USA to China; never quoted: "
        "Gorsebrook Freight (USA to China), Gorsebrook Freight (USA to China), Gorsebrook Freight (USA to China), "
        "Gorsebrook Freight (USA to China) and 1 more"]


def test_section_16_with_no_lost_case(world):
    conn = world[0]
    report = build(conn, "2027-04")
    assert report.requests[1].empty == "No lost cases in April."
    assert '<span class="cn">16</span>Why we lost</h3><p class="none">No lost cases in April.</p>' in \
        monthly_export.render_html(report)


# --------------------------------------------- 17. LATAM requests by country --
def test_section_17_columns_colours_and_bullets(world):
    conn = world[0]
    country = cards(conn, "2027-06")["17"]
    assert (country.big, country.caption, country.pill) == (
        "8", "LATAM agent requests in June", ("▲ 167% vs May (3)", "up"))
    # April to June: Chile 6, Brazil 3, then Colombia, Peru, Uruguay 1 each - Colombia by name
    assert country.legend == [("#2a78d6", "Chile"), ("#eb6834", "Brazil"), ("#1baf7a", "Colombia"),
                              ("#c3c2b7", "Others")]
    assert rows(country.table)["Total"] == ["1", "3", "8"]
    assert re.findall(r'font-weight="600"[^>]*>(\d+)</text>', country.svg) == ["1", "3", "8"]
    assert bullets(country) == [
        "Brazil 1 → 2, Colombia 0 → 1",
        "Chile 2 → 3, 3 of them Amberline Cargo",
        "Others: Peru, Uruguay, one request each",
    ]


def _events(conn, lines):
    db.set_setting(conn, monthly_requests.EVENTS_SETTING, lines)
    conn.commit()


def test_section_17_events_in_a_month_across_two_and_more_than_two(world):
    conn = world[0]
    _events(conn, ["2027-04-10 to 2027-04-12 Santiago visit", "2027-06-15 Lima fair",
                   "2027-05-28 to 2027-06-03 Andes trip"])
    svg = cards(conn, "2027-06")["17"].svg
    notes = re.findall(r'fill="#898781"[^>]*>([^<]*)</text>', svg)
    assert notes == ["Santiago visit, Apr 10-12", "Andes trip, May 28 to Jun 3", "Andes trip, May 28 to Jun 3",
                     "Lima fair, Jun 15"]
    _events(conn, ["2027-06-15 Lima fair", "2027-05-28 to 2027-06-03 Andes trip", "2027-06-20 Call series"])
    notes = re.findall(r'fill="#898781"[^>]*>([^<]*)</text>', cards(conn, "2027-06")["17"].svg)
    assert notes[-2:] == ["Andes trip, May 28 to Jun 3", "+2 more"]
    table = cards(conn, "2027-06")["17"].table
    assert ("Event: Call series, Jun 20", ["June"]) in [(r.label, r.cells) for r in table.rows]


def test_settings_shows_the_events_and_refuses_a_line_it_cannot_read(world):
    conn, repo, p, case, close, ids, path = world
    conn.commit()
    app = create_app(path)
    app.config["TESTING"] = True
    client = app.test_client()
    assert "Report events (month report, section 17)" in client.get("/settings").data.decode("utf-8")
    page = client.post("/settings", data={"report_events": "2027-06-15 Lima fair\nJune 20 calls\n2027-06-31 Bad day"},
                       follow_redirects=True).data.decode("utf-8")
    assert "Report events, line 2: &#34;June 20 calls&#34; is not" in page or \
        'Report events, line 2: "June 20 calls" is not' in page
    assert "line 3" in page and "Nothing was saved." in page
    assert "June 20 calls" in page                         # her text stays in the box
    check = db.open_db(path)
    try:
        assert db.get_setting_list(check, "report_events") == []
    finally:
        check.close()
    client.post("/settings", data={"report_events": "2027-06-15 Lima fair\n\n2027-05-28 to 2027-06-03 Andes trip"})
    check = db.open_db(path)
    try:
        assert db.get_setting_list(check, "report_events") == ["2027-06-15 Lima fair",
                                                                "2027-05-28 to 2027-06-03 Andes trip"]
    finally:
        check.close()
    assert monthly_requests.parse_event("2027-06-03 to 2027-05-28 Backwards") is None


# ------------------------------------------- 18. how the previous month moved --
def test_section_18_states_on_both_days_bands_and_the_big_number(world):
    conn = world[0]
    moved = cards(conn, "2027-06")["18"]
    assert moved.title == "How May’s requests moved in June"
    assert (moved.big, moved.caption, moved.pill) == (
        "3 of 5", "open May quotes were decided in June", ("1 won, 2 lost or cancelled", "flat"))
    # from May 31 (rows) to June 30 (columns): won, lost or cancelled, no decision yet, not quoted, total
    assert rows(moved.table) == {
        "Won": ["1", "0", "0", "0", "1"],
        "Lost or cancelled": ["0", "0", "0", "0", "0"],
        "No decision yet": ["1", "2", "2", "0", "5"],      # OPP won after the month is still open on June 30
        "Not quoted": ["0", "0", "0", "1", "1"],
        "June’s 11 requests on Jun 30": [],
        "June": ["0", "7", "4", "0", "11"],
    }
    assert bullets(moved) == [
        "May’s 7 requests on Jun 30: 2 won, 2 lost or cancelled, 2 still without a decision",
        "May win rate: 17% on May 31, 40% on Jun 30; 1 lost since, 0 of them No response",      # 17j, 17k
        "1 May request still not quoted after a month",
        "Open going into July: 2 May and 4 June quotes with no decision",
    ]
    bands = re.findall(r'fill="(#[0-9a-f]+)" fill-opacity="([0-9.]+)"', moved.svg)
    assert (WON, "0.16") in bands and (WON, "0.42") in bands and (LOST, "0.42") in bands and (OPEN, "0.16") in bands
    assert len(bands) == 5                                 # won-won, open-won, open-lost, open-open, new-new
    assert ">May 31</text>" in moved.svg and ">Jun 30</text>" in moved.svg
    assert '<div class="sub">June’s 11 requests on Jun 30</div>' in moved.svg


def test_section_18_on_a_month_not_over(world):
    conn = world[0]
    moved = cards(conn, "2027-06", today=dt.date(2027, 6, 13))["18"]
    # on June 13: the 10 June win and the 12 June loss, not the 15 June cancellation
    assert (moved.big, moved.pill) == ("2 of 5", ("1 won, 1 lost", "flat"))
    assert bullets(moved)[0] == "May’s 7 requests on Jun 13: 2 won, 1 lost or cancelled, 3 still without a decision"


# ------------------------------------------------------------ the files --
def test_page_5_and_the_four_sections_in_the_xlsx_and_the_text(world, tmp_path):
    conn = world[0]
    report = build(conn, "2027-06")
    html = monthly_export.render_html(report)
    assert html.count('<section class="page') == 5
    page5 = html[html.index("<small>Analysis &middot; Requests</small>"):]
    assert '<div class="g g7"><div class="card"><h3><span class="cn">15</span>' in page5
    assert '<div class="g g1"><div class="card"><h3><span class="cn">17</span>' in page5
    assert '<span class="pill flat">1 won, 2 lost or cancelled</span>' in page5
    assert [(n, t) for n, t, _ in monthly.tables(report)][14:] == [
        ("15", "What we win, by shipment size"), ("16", "Why we lost"), ("17", "LATAM requests by country"),
        ("18", "How May’s requests moved in June")]
    wb = openpyxl.load_workbook(monthly_export.write_xlsx(report, tmp_path))
    try:
        got = [list(r) for r in wb["Report"].iter_rows(values_only=True)]
    finally:
        wb.close()
    first = [r[0] for r in got]
    at = first.index("16. Why we lost")
    assert got[at + 2][:2] == ["Price too high", 3]
    assert "Price too high: 3; all air exports; 2 are Amberline Cargo, USA to Chile" in first
    at = first.index("18. How May’s requests moved in June")
    assert got[at + 1][:6] == ["May 31 \\ Jun 30", "Won", "Lost or cancelled", "No decision yet", "Not quoted", "Total"]
    assert "Others: Peru, Uruguay, one request each" in first
    text = monthly.render_text(report)
    assert "17. LATAM requests by country" in text
    assert "  Chile | 1 | 2 | 3" in text


def test_page_5_tightens_when_its_text_runs_long(world, monkeypatch):
    conn = world[0]
    # 17j: June's section 18 has the win-rate line now, a line past DENSE_PX
    # (442 measured 787 px of 794; 16 px more does not fit)
    assert build(conn, "2027-06").dense_requests
    monkeypatch.setattr(monthly_requests, "DENSE_PX", 10 ** 6)
    assert not build(conn, "2027-06").dense_requests
    monkeypatch.setattr(monthly_requests, "DENSE_PX", 100)
    html = monthly_export.render_html(build(conn, "2027-06"))
    assert '<section class="page dense"><h1>JUNE REPORT <small>Analysis &middot; Requests</small>' in html


# ----------------------------------------------- the two fixes to 13 and 9 --
def test_section_13_when_the_first_agent_has_none_of_the_wins(world):
    conn, repo, p, case, close, ids, path = world
    case("amber", "2027-09-02")
    case("amber", "2027-09-03")
    case("elm", "2027-09-04", "Won", dest="Colombia")
    repo.commit()
    agents = build(conn, "2027-09", dt.date(2027, 10, 10)).analysis[1]     # 17j: September closed
    assert monthly_analysis.plain(agents.bullets[0]) == (
        "Amberline Cargo: 2 of 3 requests; the 1 LATAM agent win in September went to another agent")
    case("cinder", "2027-09-05", "Won", dest="Peru")
    repo.commit()
    agents = build(conn, "2027-09", dt.date(2027, 10, 10)).analysis[1]     # 17j: September closed
    assert monthly_analysis.plain(agents.bullets[0]) == (
        "Amberline Cargo: 2 of 4 requests; the 2 LATAM agent wins in September went to other agents")


def test_section_9_names_twelve_quiet_agents_on_the_page_and_all_in_the_xlsx(world, tmp_path):
    conn, repo, p, case, close, ids, path = world
    names = ["Quayside {}".format(w) for w in ("Alder", "Birch", "Cedar", "Dogwood", "Elder", "Fir", "Gum", "Hazel",
                                               "Ilex", "Juniper", "Kowhai", "Larch", "Maple")]
    for i, name in enumerate(names):
        add_enquiry(repo, "OPP-{}".format(6801 + i), add_party(repo, name, country="Chile"),
                    received="2027-10-{:02d}".format(i + 1), status="Quoted", service_mode="Air",
                    origin_country="USA", dest_country="Chile")
    repo.commit()
    report = build(conn, "2027-11")
    html = monthly_export.render_html(report)
    shown = "Requested in October, not in November: " + ", ".join(names[:12]) + " and 1 more"
    assert shown in html and "Quayside Maple" not in html.split("<section", 1)[1].split("Analysis &middot;")[0]
    full = "Requested in October, not in November: " + ", ".join(names)
    wb = openpyxl.load_workbook(monthly_export.write_xlsx(report, tmp_path))
    try:
        assert full in [r[0] for r in wb["Report"].iter_rows(values_only=True)]
    finally:
        wb.close()
    assert "  " + full in monthly.render_text(report)

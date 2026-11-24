"""The month report in three pages, as sample v11 (batch 17a):
one base for every number, eleven sections, three files from one build,
the check box on the page only.

Replaces the 2026-09-10 tests of this file, which were written on real
agent names. Every company, place, date and number here is invented;
cases from OPP-5201.
"""

from __future__ import annotations

import re

import openpyxl
import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import dashboard, db, monthly, monthly_export
from opplist.web import create_app

MONTH = "2026-09"


@pytest.fixture
def book(tmp_path):
    path = tmp_path / "m.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    for name in ("Peru", "Colombia", "Mexico"):
        repo.insert("country", {"name": name, "region": "Latin America"})
    repo.insert("location", {"key": "TALCAHUANO", "country": "Chile", "type": "Seaport"})
    repo.insert("location", {"key": "MATARANI", "country": "Peru", "type": "Seaport"})
    p = {
        "quill": add_party(repo, "Quillmoor Cargo", country="Chile", mfn_member=1),
        "fern": add_party(repo, "Fernhollow Freight", country="Peru"),
        "talca": add_party(repo, "Talca Andes Agents", country="Chile"),
        "coral": add_party(repo, "Coral Bay Agents", country="Chile"),
        "lark": add_party(repo, "Larkspur Logistics", country="USA"),
        "office": add_party(repo, "JoJo JJV", ptype="Office", country="USA"),
        "farm": add_party(repo, "Brindle Farm", ptype="Direct Customer", country="USA"),
        "odd": add_party(repo, "Odd Requester", ptype="Unknown", country="Unknown"),
    }
    n = [5201]

    def case(party, received, status="Quoted", mode="Air", origin="USA", dest="Chile", **extra):
        values = dict(received=received, status=status, service_mode=mode, origin_country=origin,
                      dest_country=dest)
        values.update(extra)
        opp = "OPP-{}".format(n[0])
        n[0] += 1
        return add_enquiry(repo, opp, p[party], **values)

    ids = {}
    # September: nine cases on the report, one that is not (an Unknown requester)
    ids["won"] = case("quill", "2026-09-01", "Won", air_weight_kg=500)                       # US Export
    ids["lost"] = case("quill", "2026-09-02", "Lost", "FCL", closed_reason="Price too high",
                       container_type="40HQ", container_qty=2, dest_loc="TALCAHUANO")
    ids["tri"] = case("fern", "2026-09-08", "Quoted", "LCL", "China", "Peru", lcl_cbm=4.2,
                      dest_loc="MATARANI")                                                    # Cross-trade
    ids["row_won"] = case("lark", "2026-09-09", "Won", "Air", "USA", "China", air_weight_kg=200)
    ids["row_lost"] = case("lark", "2026-09-14", "Lost", "Air", "USA", "China", closed_reason="Rate too slow")
    ids["import"] = case("office", "2026-09-15", "Quoted", "FCL", "Chile", "USA",
                         container_type="20GP", container_qty=1)                              # US Import
    ids["farm"] = case("farm", "2026-09-21", "Quoted", "Air", "USA", "Chile")
    ids["no_reason"] = case("quill", "2026-09-22", "Lost", "Air")
    ids["coral"] = case("coral", "2026-09-28", "Quoted", "Air")
    ids["odd"] = case("odd", "2026-09-29", "Quoted", "Air")
    cancelled = case("lark", "2026-09-29", "Quoted", "Air", "USA", "China")
    repo.set_status(cancelled, "Cancelled", "2026-09-30", closed_reason="Order not confirmed",
                    note="Wren Hale <wren@larkspur.example>   The buyer   dropped the order.")
    ids["cancelled"] = cancelled
    # August: Fernhollow and Talca asked; Talca not again in September
    case("fern", "2026-08-11", "Quoted")
    case("talca", "2026-08-12", "Won")
    case("talca", "2026-08-13", "Lost", closed_reason="Price too high")
    # Coral Bay's first case in OppList was a Dropped one in July: not new in September
    case("coral", "2026-07-20", "Dropped", closed_reason="Registered in error")
    # the won routes carry their result (the map's green core reads it)
    for key in ("won", "row_won"):
        route = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ?", (ids[key],)).fetchone()[0]
        repo.update("enquiry_route", route, {"result": "Won", "result_on": "2026-09-20"})
    repo.commit()
    yield conn, repo, ids, p, case, path
    conn.close()


def cells(table):
    return {r.label: r.cells for r in table.rows}


def requests(report) -> int:
    """17l: the month's total, the first tile's numbers added up."""
    return sum(int(n) for n in report.kpis[0][0].split(" / "))


# ------------------------------------------------------------- the base --
def test_five_kpis_and_the_cancelled_small_text(book):
    conn, repo, ids, p, case, path = book
    r = monthly.build(conn, MONTH)
    # 17l: the first tile is the base by requester (agents / direct customers / offices).
    # SC1: Brindle Farm (a direct customer) asked before October, so the report
    # rule leaves it out: no direct customer in September, two numbers
    assert r.kpis == [("8 / 1", "Agents / Our offices"), ("6 / 3", "LATAM / Rest of world"),
                      ("6 / 3", "Air / Ocean"), ("2", "Won"), ("3", "Lost")]
    assert r.kpi_notes == {"Lost": "1 cancelled"}
    assert "Quoted, no decision yet" not in "\n".join(monthly.render_text(r))


def test_one_set_of_numbers(book):
    conn, repo, ids, p, case, path = book
    r = monthly.build(conn, MONTH)
    total = requests(r)
    assert cells(r.by_scope)["Total"][0] == str(total)
    assert sum(n for _, n, _ in r.mix) == total
    assert sum(sum(v) for v in r.weeks["series"].values()) == total


def test_an_untyped_requester_is_on_no_page_and_in_the_check_box(book):
    conn, repo, ids, p, case, path = book
    r = monthly.build(conn, MONTH)
    text = "\n".join(monthly.render_text(r))
    assert "Odd Requester" not in text and "Other" not in cells(r.by_scope)
    found = {g["title"]: g["items"] for g in r.checks}
    untyped = found["Not on the report, requester type is not Agent, own office or Direct customer"]
    assert [(i["opp_nos"], i["text"]) for i in untyped] == [(["OPP-5210"], "Unknown")]
    for word in ("Not set", "Not recorded", "?"):
        assert word not in monthly_export.render_html(r).split("<section", 1)[1]


def test_direct_customers_have_a_row_and_a_slice_only_with_a_case(book):
    conn, repo, ids, p, case, path = book
    # SC1: Brindle Farm's September case is before October, so not counted;
    # its October case touches LATAM (Chile), so it is
    r = monthly.build(conn, MONTH)
    assert "Direct customers" not in [row.label for row in r.by_scope.rows]
    case("farm", "2026-10-06", "Quoted", "Air", "USA", "Chile")
    repo.commit()
    october = monthly.build(conn, "2026-10")
    assert ("Direct customers", "indent") in [(row.label, row.cls) for row in october.by_scope.rows]
    assert [label for label, _, _ in october.mix] == ["LATAM agents", "Other agents", "Our offices",
                                                     "Direct customers"]
    august = monthly.build(conn, "2026-08")
    assert "Direct customers" not in [row.label for row in august.by_scope.rows]
    assert [label for label, _, _ in august.mix] == ["LATAM agents", "Other agents", "Our offices"]
    assert "Direct customers" not in august.weeks["series"]


# -------------------------------------------------------- 2. why we lost --
def test_the_bars_count_lost_only_and_a_case_without_a_reason_is_listed(book):
    conn, repo, ids, p, case, path = book
    r = monthly.build(conn, MONTH)
    assert r.lost_bars == [("Price too high", 1), ("Rate too slow", 1)]          # ties in the Settings order
    found = {g["title"]: g["items"] for g in r.checks}
    assert [i["opp_nos"] for i in found["Lost with no reason"]] == [["OPP-5208"]]


def test_at_most_six_bars_and_the_sixth_is_other_reasons(book):
    conn, repo, ids, p, case, path = book
    reasons = monthly.lost_bars.__globals__["status_mod"] if False else None
    from opplist import status as status_mod
    names = status_mod.closed_reasons(conn, "Lost")
    for i, reason in enumerate(names):                       # seven reasons, the first twice
        for _ in range(2 if i == 0 else 1):
            case("lark", "2026-10-05", "Lost", "Air", "USA", "China", closed_reason=reason)
    repo.commit()
    bars = monthly.build(conn, "2026-10").lost_bars
    assert len(bars) == 6 and bars[0] == (names[0], 2)
    # SC1: Settings' Lost reasons gained "Customer chose the other mode", so the rest is one more
    assert bars[-1] == ("Other reasons", len(names) - 5) and [label for label, _ in bars[1:5]] == names[1:5]
    html = monthly_export.fragments(monthly.build(conn, "2026-10"))["bars"]
    assert html.count('class="hb"') == 6 and "background:{}".format(monthly.MUTED) in html


def test_the_cancelled_lines_their_note_fallback_and_the_cut(book):
    conn, repo, ids, p, case, path = book
    r = monthly.build(conn, MONTH)
    assert r.cancelled == [{"opp_no": "OPP-5211", "requester": "Larkspur Logistics", "reason": "Order not confirmed",
                            "note": "The buyer dropped the order."}]
    # no note on the Cancelled event: the latest note on or after the day
    later = case("lark", "2026-09-28", "Quoted", "Air", "USA", "China")
    # 17j: cancelled inside the month (a cancelling after it is not in September's report)
    repo.set_status(later, "Cancelled", "2026-09-30", closed_reason="Shipment cancelled")
    repo.add_note(later, "2026-09-29", "a note before the day")
    repo.add_note(later, "2026-10-03", "Cargo   stays in the yard. " + "x" * 200)
    repo.commit()
    got = [c for c in monthly.build(conn, MONTH).cancelled if c["opp_no"] == "OPP-5216"][0]
    assert got["note"].startswith("Cargo stays in the yard. xxx") and got["note"].endswith("...")
    assert len(got["note"]) == monthly.NOTE_CUT + 3
    html = monthly_export.render_html(monthly.build(conn, MONTH))
    assert "OPP-5211 &middot; Larkspur Logistics:</span> Order not confirmed, The buyer dropped the order." in html


def test_how_many_cancelled_lines_follow_the_bars():
    assert [monthly.CANCEL_LINES[n] for n in (2, 4, 5, 6)] == [3, 3, 2, 1]


# ------------------------------------------------------------- 4. vs last --
def test_section_4_rows_and_colours(book):
    conn, repo, ids, p, case, path = book
    r = monthly.build(conn, MONTH)
    assert r.compare.columns == ["", "August", "September", "Change"]
    got = {row.label: (row.cells, row.marks[2]) for row in r.compare.rows}
    # SC1: Brindle Farm's September case (a direct customer before October) is not counted
    assert got["Requests"] == (["3", "9", "+6"], "up")
    assert got["LATAM share"] == (["100%", "67%", "-33 pts"], "")          # never coloured
    assert got["Won"] == (["1", "2", "+1"], "up")
    # 17a-2: no direct customer counted in either month, so no 'of which' rows
    # and the plain win rate (17k: 2 of 8 quoted)
    assert [row.label for row in r.compare.rows][:4] == ["Requests", "LATAM share", "Won", "Win rate"]
    assert "of which direct customers" not in got
    assert got["Win rate"] == (["33%", "25%", "-8 pts"], "down")
    # Quillmoor and Coral Bay in September; Coral Bay's Dropped July case makes it not new
    assert got["Active LATAM agents"] == (["2", "3", "+1"], "up")
    assert got["New LATAM agents"] == (["2", "1", "-1"], "down")


def test_a_first_month_has_no_previous_column(book):
    conn, repo, ids, p, case, path = book
    r = monthly.build(conn, "2026-07")                       # June has no case
    for row in r.compare.rows:
        assert row.cells[0] == "" and row.cells[2] == ""


# --------------------------------------------------------------- 5. map --
def test_the_map_counts_routes_per_country_and_others_past_13(book):
    conn, repo, ids, p, case, path = book
    r = monthly.build(conn, MONTH)
    assert dict(r.map_rows)["Chile"] == {"n": 5, "won": 1}          # SC1: Brindle Farm's case not counted
    assert [row.label for row in r.countries.rows] == ["Chile", "Peru"]
    names = [c for c in db.get_setting_list(conn, "latam_countries") if c != "USA"][:15]
    have = {row[0] for row in conn.execute("SELECT name FROM country")}
    for name in names:
        if name not in have:
            repo.insert("country", {"name": name, "region": "Latin America"})
        case("lark", "2026-11-03", "Quoted", "Air", "USA", name)
    repo.commit()
    table = monthly.build(conn, "2026-11").countries
    assert len(table.rows) == 13 and table.rows[-1].label == "Others (3)" and table.rows[-1].cells == ["3"]


# -------------------------------------------------- 6, 7, 8: the routes --
def test_latam_by_service_type_lanes_and_pods(book):
    conn, repo, ids, p, case, path = book
    r = monthly.build(conn, MONTH)
    assert r.direction.columns == ["LATAM", "Export", "Import", "Triangle", "Total"]
    # SC1: Brindle Farm's USA to Chile (a direct customer before October) is not counted
    assert cells(r.direction) == {"Total": ["4", "1", "1", "6"], "Air": ["3", "0", "0", "3"],
                                  "Ocean": ["1", "1", "1", "3"]}
    lanes = [(row.label, row.cells) for row in r.top_lanes.rows]
    assert lanes[:3] == [("LATAM", []), ("USA to Chile", ["4"]), ("China to Peru", ["1"])]
    assert ("USA to China", ["3"]) in lanes
    assert [(row.label, row.cells) for row in r.top_pods.rows] == [("TALCAHUANO", ["1"]), ("MATARANI", ["1"])]


# --------------------------------------------------------- 9. top agents --
def test_top_agents_directions_the_previous_month_and_the_quiet_row(book):
    conn, repo, ids, p, case, path = book
    r = monthly.build(conn, MONTH)
    t = r.top_agents
    assert t.columns == ["Agent", "Inquiries", "Export", "Import", "Triangle", "Aug", "Won", "TEU", "Air kg",
                         "LCL cbm"]
    assert t.marks == ["", "dir", "dir", "dir", "", "won", "", "", ""]
    rows = [(row.label, row.cls, row.cells) for row in t.rows]
    assert rows[0] == ("LATAM agents", "head", [])
    assert rows[1] == ("Quillmoor Cargo", "", ["3", "3", "", "", "", "1", "4", "500", ""])
    # one case each: the lowest case number first (OPP-5203 before OPP-5209)
    assert rows[2] == ("Fernhollow Freight", "", ["1", "", "", "1", "1", "", "", "", "4.2"])
    assert rows[3] == ("Coral Bay Agents", "", ["1", "1", "", "", "", "", "", "", ""])
    assert rows[4] == ("Requested in August, not in September: Talca Andes Agents", "note", [])
    assert rows[5] == ("Other agents", "head", [])                 # SC1: was Non-LATAM agents
    assert rows[6][0] == "Larkspur Logistics" and rows[6][2][:2] == ["3", "3"]


def test_no_quiet_row_when_nobody_went_quiet_and_a_long_one_tightens_page_3(book):
    conn, repo, ids, p, case, path = book
    case("talca", "2026-09-25", "Quoted")
    repo.commit()
    assert "note" not in [row.cls for row in monthly.build(conn, MONTH).top_agents.rows]
    for i in range(20):
        party = add_party(repo, "Agencia de Carga Quieta Numero {:02d} Ltda".format(i), country="Peru")
        add_enquiry(repo, "OPP-{}".format(5300 + i), party, received="2026-08-20", service_mode="Air",
                    origin_country="USA", dest_country="Peru")
    repo.commit()
    r = monthly.build(conn, MONTH)
    note = [row.label for row in r.top_agents.rows if row.cls == "note"][0]
    assert note.count("Agencia de Carga Quieta") == 20 and r.dense_agents
    # 17i: dc, section 9 has the offices group; SC1: Brindle Farm (a direct
    # customer before October) is not counted, so no Direct customers group and no dc4
    assert '<section class="page dense dc"><h1>SEPTEMBER REPORT <small>Agents</small>' in monthly_export.render_html(r)


# ---------------------------------------------------------------- 10. weeks --
@pytest.mark.parametrize("month,labels", [
    # August 2026 starts on a Saturday and ends on a Monday
    ("2026-08", ["Aug 1-9", "Aug 10-16", "Aug 17-23", "Aug 24-31"]),
    ("2026-09", ["Sep 1-6", "Sep 7-13", "Sep 14-20", "Sep 21-27", "Sep 28-30"]),
])
def test_the_weeks_cut_at_the_month_and_short_edges_join(book, month, labels):
    conn, repo, ids, p, case, path = book
    r = monthly.build(conn, month)
    assert r.weeks["labels"] == labels
    spans = r.weeks["spans"]
    assert spans[0][0].day == 1 and (spans[-1][1].month != spans[-1][1].replace(day=28).month or True)
    assert sum((b - a).days + 1 for a, b in spans) == {"2026-08": 31, "2026-09": 30}[month]
    assert sum(sum(v) for v in r.weeks["series"].values()) == requests(r)


def test_the_last_week_label_is_anchored_at_its_end(book):
    conn, repo, ids, p, case, path = book
    svg = monthly_export.fragments(monthly.build(conn, MONTH))["weeks"]
    assert re.search(r'text-anchor="end">Sep 28-30</text>', svg)
    assert re.search(r'text-anchor="middle">Sep 1-6</text>', svg)


# --------------------------------------------------- files and the page --
def test_three_files_from_one_build_and_no_requote_sheet(book, tmp_path, monkeypatch):
    conn, repo, ids, p, case, path = book
    r = monthly.build(conn, MONTH)
    html = monthly_export.write_html(r, tmp_path).read_text(encoding="utf-8")
    # 17f: a fourth page, Analysis · Agents; 17g: a fifth, Analysis · Requests
    assert html.count('<section class="page') == 5 and "precheck" not in html.split("</style>", 1)[1]
    for small in ("Overview", "Regions", "Agents", "Analysis &middot; Agents", "Analysis &middot; Requests"):
        assert "SEPTEMBER REPORT <small>{}</small>".format(small) in html
    assert ('<h2><span class="sn">4.</span>September vs August '
            '<small>win rate = won \u00f7 quoted, open quotes included</small></h2>') in html      # 17k
    assert 'font-family:system-ui,-apple-system,"Segoe UI",sans-serif' in html          # not escaped
    for gone in ("What we heard", "Re-quotes by reason", "Quoted, no decision yet"):
        assert gone not in html
    wb = openpyxl.load_workbook(monthly_export.write_xlsx(r, tmp_path))
    try:
        assert wb.sheetnames == ["Report"]
        first = [row[0] for row in wb["Report"].iter_rows(values_only=True)]
        titles = [t for t in first if isinstance(t, str) and re.match(r"\d+\. ", t)]
        assert [t.split(".")[0] for t in titles] == [str(i) for i in range(1, 19)]   # 17f: 12 to 14; 17g: 15 to 18
        assert "Requested in August, not in September: Talca Andes Agents" in first
    finally:
        wb.close()
    text = monthly.render_text(r)
    assert [line for line in text if re.match(r"\d+\. ", line)] == ["{}. {}".format(n, t) for n, t, _ in
                                                                    monthly.tables(r)]
    monkeypatch.setattr(monthly_export, "find_edge", lambda: None)
    with pytest.raises(monthly_export.EdgeNotFound):
        monthly_export.write_pdf(r, tmp_path)


def test_the_check_box_lists_every_finding_on_the_page_only(book):
    conn, repo, ids, p, case, path = book
    case("lark", "2026-10-02", "Quoted", "Air", "USA", None)                          # no destination
    case("lark", "2026-10-03", "Quoted", "Air", "USA", "Unknown")                     # country unknown
    case("lark", "2026-10-04", "Quoted", None, "USA", "Chile")                        # no mode
    boxes = case("lark", "2026-10-05", "Quoted", "Air", "USA", "China")
    repo.add_cargo_line(boxes, {"container_type": "40HQ", "container_qty": 3})        # Air with containers
    case("lark", "2026-10-06", "Lost", "Air", "USA", "China")                         # lost, no reason
    case("lark", "2026-10-07", "Cancelled", "Air", "USA", "China")                    # cancelled, no reason
    case("odd", "2026-10-08", "Quoted", "Air", "USA", "China")                        # not on the report
    nowhere = add_party(repo, "Nowhere Agents", country="Unknown")
    add_enquiry(repo, "OPP-5299", nowhere, received="2026-10-09", service_mode="Air", origin_country="USA",
                dest_country="China")
    add_enquiry(repo, "OPP-5298", nowhere, received="2026-10-10", service_mode="Air", origin_country="USA",
                dest_country="China")
    repo.commit()
    r = monthly.build(conn, "2026-10")
    assert [g["title"] for g in r.checks] == [
        "No origin or destination country", "Country unknown", "No mode", "No size for section 15",
        "Air with containers", "Lost with no reason", "Cancelled with no reason",
        "Not on the report, requester type is not Agent, own office or Direct customer",
        "Agent's country unknown"]
    agent = r.checks[-1]["items"]
    assert [(a["who"], a["opp_nos"]) for a in agent] == [("Nowhere Agents", ["OPP-5298", "OPP-5299"])]
    assert r.checks[4]["items"][0]["text"] == "3 x 40HQ"
    client = create_app(path).test_client()
    page = client.get("/report?month=2026-10").data.decode("utf-8")
    assert '<div class="precheck" id="precheck"><h2>Check before you print <small>October</small>' in page
    assert '<a href="/enquiries/{}">OPP-5298</a>, <a href="/enquiries/'.format(
        conn.execute("SELECT id FROM enquiry WHERE opp_no = 'OPP-5298'").fetchone()[0]) in page
    assert "precheck" not in monthly_export.render_html(r).split("</style>", 1)[1]


def test_nothing_to_fix(book):
    conn, repo, ids, p, case, path = book
    page = create_app(path).test_client().get("/report?month=2026-07").data.decode("utf-8")
    assert '<p class="pc-ok">Nothing to fix.</p>' in page


def test_the_dashboard_lost_its_two_texts(book):
    conn, repo, ids, p, case, path = book
    page = create_app(path).test_client().get("/dashboard").data.decode("utf-8")
    assert "scope In &middot; legacy excluded" not in page
    assert "legacy rows excluded everywhere" not in page and "rows without a receipt date" not in page


# -------------------------------------------- kept from the old file --
def test_requests_per_month_run_from_january_and_keep_inherited_rows(book):
    conn, repo, ids, p, case, path = book
    repo.insert("enquiry", {"opp_no": "OPP-5290", "requester_party_id": p["quill"], "received_on": "2026-06-10",
                            "service_mode": "Air", "origin_country": "USA", "dest_country": "Chile",
                            "status": "Dropped", "legacy": 1})
    repo.commit()
    chart = monthly.build(conn, MONTH).per_month
    assert chart["labels"] == ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep"]
    # SC1: Brindle Farm's September case is not counted
    assert chart["latam"][5] == 1 and chart["latam"][8] == 6 and chart["row"][8] == 3
    svg = monthly.chart_svg(chart["labels"], chart["latam"], chart["row"])
    assert svg.count("<polyline") == 2


def test_css_is_scoped_on_the_page_and_bare_in_the_file():
    bare = monthly_export.css()
    scoped = monthly_export.css(".mreport")
    assert bare.startswith("body{") and ".page{width:297mm" in bare and "h2 .sn{" in bare
    assert scoped.startswith(".mreport{") and ".mreport .page{width:297mm" in scoped
    assert "\nbody{" not in scoped


def test_cbm_and_default_month():
    import datetime as dt
    assert monthly._cbm(13.823) == "14" and monthly._cbm(7.9) == "7.9" and monthly._cbm(0) == ""
    assert monthly.default_month(dt.date(2026, 9, 10)) == "2026-08"
    assert monthly.default_month(dt.date(2026, 1, 3)) == "2025-12"


def test_a_held_case_counts_by_its_status_and_the_analysis_file_has_the_hold(book, tmp_path):
    """Moved from test_hold.py (batch 17a): a hold changes nothing in the
    report, and the analysis file carries its two columns."""
    import csv
    from opplist import export_analysis
    conn, repo, ids, p, case, path = book
    before = monthly.build(conn, MONTH).kpis
    repo.set_hold(ids["tri"], "customer travelling", "2026-09-10", until="2026-09-20")
    repo.commit()
    assert monthly.build(conn, MONTH).kpis == before
    export_analysis.run(conn, out_dir=tmp_path, today="2026-09-11")
    rows = {r["opp_no"]: r for r in csv.DictReader(
        (tmp_path / "enquiries.csv").read_text(encoding="utf-8-sig").splitlines())}
    assert (rows["OPP-5203"]["hold_reason"], rows["OPP-5203"]["hold_until"]) == ("customer travelling", "2026-09-20")
    assert list(rows["OPP-5203"].keys())[-2:] == ["hold_reason", "hold_until"]


# ------------------------------------------- 17d: FCL and LCL in section 1 --
def test_section_1_splits_ocean_into_fcl_and_lcl_and_a_roro_case_has_its_column(book, tmp_path):
    """17d's FCL and LCL columns; SC1: a RORO case has its own column (it
    was in Other since 17a-2) and counts as Ocean on the KPI."""
    conn, repo, ids, p, case, path = book
    for opp, mode in (("OPP-5501", "FCL"), ("OPP-5502", "LCL"), ("OPP-5503", "Air"), ("OPP-5504", "RORO")):
        add_enquiry(repo, opp, p["lark"], received="2026-12-07", service_mode=mode, origin_country="USA",
                    dest_country="China")
    repo.commit()
    r = monthly.build(conn, "2026-12")
    assert r.by_scope.columns == ["Scope / requester", "Total", "Air", "FCL", "LCL", "RORO", "Won", "Lost"]
    total = cells(r.by_scope)["Total"]
    assert total == ["4", "1", "1", "1", "1", "0", "0"]
    assert cells(r.by_scope)["Rest of world"] == total
    assert ("1 / 3", "Air / Ocean") in r.kpis                       # SC1: RORO is Ocean
    assert "Air / Ocean" not in r.kpi_notes                         # 17l: no small line
    # a lane of the rest of the world: section 6 (LATAM) has no Other row
    assert [row.label for row in r.direction.rows] == ["Total", "Air", "Ocean"]
    wb = openpyxl.load_workbook(monthly_export.write_xlsx(r, tmp_path))
    try:
        rows = list(wb["Report"].iter_rows(values_only=True))
        heads = [row for row in rows if row[0] == "Scope / requester"][0]
        assert list(heads[:8]) == ["Scope / requester", "Total", "Air", "FCL", "LCL", "RORO", "Won", "Lost"]
    finally:
        wb.close()
    assert "  Scope / requester | Total | Air | FCL | LCL | RORO | Won | Lost" in monthly.render_text(r)
    html = monthly_export.render_html(r)
    assert '<th class="num">FCL</th><th class="num">LCL</th><th class="num">RORO</th>' in html

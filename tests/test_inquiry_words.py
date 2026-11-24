"""Batch 18 (2026-10-01): every word Iris reads says inquiry, never
enquiry, and Triangle, never Cross-trade. Code names stay - tables, views,
routes and URLs (/enquiries), identifiers, stored values, settings keys -
and so do the header rows the scripts read: the Opportunities and Routes
sheets of the auto-export workbook, its Agent_Master sheet (Export-AgentSeed.ps1
reads row 1 by name), opp-index.csv and agents.csv.

Every company, case number, date, lane and mail here is invented; cases
from OPP-3001."""

from __future__ import annotations

import csv
import html as html_mod
import re
from html.parser import HTMLParser

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, export_csv, export_xlsx, monthly, monthly_export
from opplist.web import create_app

BANNED = re.compile(r"enquir|cross-trade", re.IGNORECASE)
#: attributes a person reads (or that the page's script shows as text)
VISIBLE_ATTRS = ("title", "placeholder", "aria-label", "alt", "data-show", "data-hide", "data-saving",
                 "data-unsaved", "data-saved", "data-empty", "data-label", "data-confirm")


class _Visible(HTMLParser):
    """The text of a page as it reads: text nodes (not scripts or styles),
    the attributes a person sees, and the value of a button-like input."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        a = dict(attrs)
        for name in VISIBLE_ATTRS:
            if a.get(name):
                self.out.append(a[name])
        if tag == "input" and a.get("type") in ("submit", "button") and a.get("value"):
            self.out.append(a["value"])

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.out.append(data.strip())


def visible(page: str) -> list:
    parser = _Visible()
    parser.feed(page)
    return parser.out


def hits(texts) -> list:
    return [t for t in texts if BANNED.search(t or "")]


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "words.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "shipments", "1")             # FX3: the shipment pages are read too
    kestrel = add_party(repo, "Kestrel Forwarding", country="Chile")
    pelican = add_party(repo, "Pelican Lines", ptype="Carrier", country="USA")
    ids = {"kestrel": kestrel, "pelican": pelican}
    ids["air"] = add_enquiry(repo, "OPP-3001", kestrel, received="2026-11-03", origin_country="USA", origin_loc="MIA",
                             dest_country="Chile", dest_loc="SAI", service_mode="Air", source_subject="RFQ AIR MIA - SAI")
    ids["triangle"] = add_enquiry(repo, "OPP-3002", kestrel, received="2026-11-04", origin_country="China",
                                  origin_loc="NGB", dest_country="Brazil", service_mode="FCL",
                                  source_subject="RFQ FCL NINGBO - SANTOS")
    repo.add_cargo_line(ids["triangle"], {"container_qty": 2, "container_type": "40HQ", "commodity": "ceramic tiles"})
    repo.set_status(ids["triangle"], "Quoted", "2026-11-05")
    ids["won"] = add_enquiry(repo, "OPP-3003", kestrel, received="2026-11-02", origin_country="USA", origin_loc="LAX",
                             dest_country="Chile", dest_loc="SAI", service_mode="Air")
    repo.set_status(ids["won"], "Quoted", "2026-11-03")
    repo.set_status(ids["won"], "Won", "2026-11-06")
    repo.commit()
    yield conn, path, ids
    conn.close()


def pages(ids):
    return ["/enquiries", "/enquiries?view=all", "/enquiries?view=closed", "/enquiries/new",
            "/enquiries/{}".format(ids["air"]), "/enquiries/{}".format(ids["triangle"]),
            "/enquiries/{}".format(ids["won"]), "/tracking", "/dashboard", "/report?month=2026-11", "/export",
            "/settings", "/parties", "/parties/{}".format(ids["kestrel"]), "/parties/{}".format(ids["pelican"]),
            "/parties/duplicates", "/locations", "/locations/new", "/shipments", "/check", "/search?q=kiln"]


def test_no_page_says_the_old_words(world):
    conn, path, ids = world
    app = create_app(path)
    app.config["TESTING"] = True
    client = app.test_client()
    found = {}
    for url in pages(ids):
        page = client.get(url, follow_redirects=True)
        assert page.status_code == 200, url
        bad = hits(visible(page.data.decode("utf-8")))
        if bad:
            found[url] = bad
    assert found == {}, found


def test_the_words_she_reads_are_inquiry_and_triangle(world):
    conn, path, ids = world
    app = create_app(path)
    app.config["TESTING"] = True
    client = app.test_client()
    body = client.get("/enquiries/{}".format(ids["triangle"])).data.decode("utf-8")
    assert "Triangle" in visible(body) or any("Triangle" in t for t in visible(body))
    listing = " ".join(visible(client.get("/enquiries?view=all").data.decode("utf-8")))
    assert "Inquiries" in listing and "New inquiry" in listing and "Triangle" in listing
    # the code names stay: the URL, the filter value the page's script reads
    assert 'data-filter="direction" data-value="Cross-trade"' in client.get("/enquiries").data.decode("utf-8")


def test_the_month_report_and_its_exports_say_inquiry_and_triangle(world, tmp_path):
    conn, path, ids = world
    report = monthly.build(conn, "2026-11")
    html = monthly_export.render_html(report)
    assert hits(visible(html)) == []
    assert "Triangle" in html
    from openpyxl import load_workbook
    book = load_workbook(monthly_export.write_xlsx(report, out_dir=tmp_path))
    cells = [str(c.value) for ws in book.worksheets for row in ws.iter_rows() for c in row if c.value is not None]
    assert hits(cells) == []


# The header rows the scripts and the schedule read, copied from the exports
# made on 2026-10-01 before batch 18: they never change.
OPPORTUNITIES_HEAD = [
    "Enquiry No.", "Date Received", "Month", "Source Type", "Agent", "Agent Country", "Agent Region", "Handled By",
    "Service Mode", "Container Type", "Container Qty", "TEU", "Air Weight (kgs)", "LCL Volume (cbm)",
    "Origin Country", "Origin Detail", "Origin Alt", "Origin Region", "Dest. Country", "Dest. Detail", "Dest. Alt",
    "Dest. Region", "LATAM", "Direction", "Incoterms", "Commodity", "Special Handling", "Status", "Date Quoted",
    "Quote Ref", "Date Closed", "Days to Quote", "Loss Reason", "Overdue Flag", "Location Check", "FMS Job No.",
    "Source Qty (raw)", "Remarks", "System Notes", "Country/Location Check", "Mode/Location Check"]
ROUTES_HEAD = ["Enquiry No.", "Route", "Origin Country", "Origin Location", "Origin Alt", "Dest. Country",
               "Dest. Location", "Dest. Alt", "Service Mode", "Incoterm", "Service", "Port of Discharge", "Result",
               "Result Date", "Lost Reason", "Direction", "LATAM", "Notes"]
AGENT_MASTER_HEAD = ["Agent", "Full Name", "Source Type", "Country", "Region", "City", "MFN No.", "Contact",
                     "Email", "First Enquiry", "Last Enquiry", "Last Enquiry No.", "Total Enquiries", "Secured",
                     "Conversion Rate", "Win Rate", "Enquiry Share", "Secured Share", "Days Since Last", "Notes",
                     "Rank Helper", "MFN Member"]
OPP_INDEX_HEAD = b'\xef\xbb\xbf"OppNo","DateReceived","Agent","Mode","OriginDetail","DestDetail","Commodity","Status"\r\n'
AGENTS_HEAD = b'\xef\xbb\xbf"Agent","FullName","Country","TotalEnquiries","KnownEmail"\r\n'


def test_the_headers_the_scripts_read_are_unchanged(world, tmp_path):
    conn, path, ids = world
    from openpyxl import load_workbook
    book = load_workbook(export_xlsx.export(conn, tmp_path / "book.xlsx", db_path=""))
    assert [c.value for c in book["Opportunities"][1]] == OPPORTUNITIES_HEAD
    assert [c.value for c in book["Routes"][1]] == ROUTES_HEAD
    assert [c.value for c in book["Agent_Master"][1]] == AGENT_MASTER_HEAD
    opp_index = export_csv.export_opp_index(conn, tmp_path / "opp-index.csv").read_bytes()
    agents = export_csv.export_agents(conn, tmp_path / "agents.csv").read_bytes()
    assert opp_index.startswith(OPP_INDEX_HEAD) and agents.startswith(AGENTS_HEAD)


def test_the_workbook_s_own_sheets_say_inquiry_and_triangle(world, tmp_path):
    conn, path, ids = world
    from openpyxl import load_workbook
    book = load_workbook(export_xlsx.export(conn, tmp_path / "book.xlsx", db_path=""))
    for ws in book.worksheets:
        if ws.title in ("Opportunities", "Routes", "Agent_Master"):
            continue                                   # the scripts' contract sheets: kept as they were
        cells = [str(c.value) for row in ws.iter_rows() for c in row if c.value is not None]
        assert hits(cells) == [], ws.title


def test_the_dashboard_export_and_the_month_report_text_say_inquiry(world):
    conn, path, ids = world
    from opplist import dashboard, report
    page = dashboard.render_standalone(dashboard.build(conn, today="2026-11-20"))
    assert hits(visible(page)) == []
    assert hits(monthly.render_text(monthly.build(conn, "2026-11"))) == []          # a list of lines
    built = report.build(conn, "2026-11")
    assert hits([str(x) for x in report.render(built)]) == []



# ------------------------------------------------- the replaced old tests --
def others(world_conn):
    """Three LATAM cases another office handled (JJV twice, JQM once)."""
    conn = world_conn
    repo = db.Repository(conn, source="test")
    party = conn.execute("SELECT id FROM party WHERE key = 'Kestrel Forwarding'").fetchone()[0]
    for opp, office in (("OPP-3011", "JJV"), ("OPP-3012", "JJV"), ("OPP-3013", "JQM")):
        add_enquiry(repo, opp, party, received="2026-11-05", origin_country="USA", dest_country="Chile",
                    service_mode="Air", handled_by=office)
    repo.commit()


def test_the_handled_elsewhere_line_and_the_cases_it_names_are_counted(world):
    conn, path, ids = world
    from opplist import dashboard
    others(conn)
    # SC1: the cases another office handles count like any other; the line
    # under section 1 says how many of them are in the total
    line = "Of which handled by our other offices: 3 (JJV 2, JQM 1)"
    dash = dashboard.build(conn, "2026-11-20", "month")
    assert dash.others_line == line
    assert {t["label"]: t["big"] for t in dash.tiles}["Requests, November to date"] == "6"
    report = monthly.build(conn, "2026-11")
    assert report.kpis[0] == ("6 / 0", "Agents / Our offices")          # 17l
    assert (report.by_scope.rows[-1].label, report.by_scope.rows[-1].cls) == (line, "note")
    text = "\n".join(monthly.render_text(report))
    section = text[text.index("1. By scope, requester and direction"):]
    assert "  " + line in section[:section.index("2. Why we lost")]
    app = create_app(path)
    app.config["TESTING"] = True
    client = app.test_client()
    assert line in client.get("/report?month=2026-11").data.decode("utf-8")
    assert line in dashboard.render_standalone(dash)          # the page itself reads today's month


def test_the_month_report_s_scope_rows_and_its_direction_heads(world):
    conn, path, ids = world
    report = monthly.build(conn, "2026-11")
    assert [(r.label, r.cls, r.cells) for r in report.by_scope.rows] == [
        ("LATAM", "sub", ["3", "2", "1", "0", "1", "0"]),            # 17d: FCL and LCL
        ("LATAM agents, not MFN", "indent", ["3", "2", "1", "0", "1", "0"]),
        ("Rest of world", "sub", ["0", "0", "0", "0", "0", "0"]),
        ("Total", "total", ["3", "2", "1", "0", "1", "0"]),
        ("By direction", "head", []),                                   # 17e
        ("Export", "indent", ["2", "2", "0", "0", "1", "0"]),
        ("Import", "indent", ["0", "0", "0", "0", "0", "0"]),
        ("Triangle", "indent", ["1", "0", "1", "0", "0", "0"]),
        ("Of which handled by our other offices: 0", "note", [])]           # SC1
    # 17a: section 6 reads Export, Import, Triangle; a route without a
    # direction is in none of them (no Not set column)
    assert report.direction.columns == ["LATAM", "Export", "Import", "Triangle", "Total"]
    repo = db.Repository(conn, source="test")
    repo.update("enquiry", ids["triangle"], {"origin_country": None})
    repo.commit()
    assert monthly.build(conn, "2026-11").direction.columns == ["LATAM", "Export", "Import", "Triangle", "Total"]


def test_the_shipments_list_filters_and_the_nav_reads_inquiries_first(world):
    conn, path, ids = world
    from opplist import shipments
    repo = db.Repository(conn, source="test")
    party = conn.execute("SELECT id FROM party WHERE key = 'Kestrel Forwarding'").fetchone()[0]
    done = add_enquiry(repo, "OPP-3021", party, received="2026-11-01", origin_country="USA", origin_loc="LAX",
                       dest_country="Chile", dest_loc="SAI", service_mode="Air")
    repo.set_status(done, "Quoted", "2026-11-02")
    repo.set_status(done, "Won", "2026-11-03")
    moving = shipments.start(repo, ids["won"], "Lakeview, OH to Talca")
    shipments.log_milestone(repo, moving, "Picked up", "2026-11-08 10:00")
    delivered = shipments.start(repo, done, "Ridgeway, PA to Curico")
    shipments.log_milestone(repo, delivered, "Delivered", "2026-11-12 09:00")
    repo.commit()
    assert [r["title"] for r in shipments.list_rows(conn, "active", "2026-11-20")] == ["Lakeview, OH to Talca"]
    assert [r["title"] for r in shipments.list_rows(conn, "delivered", "2026-11-20")] == ["Ridgeway, PA to Curico"]
    assert len(shipments.list_rows(conn, "all", "2026-11-20")) == 2
    app = create_app(path)
    app.config["TESTING"] = True
    body = app.test_client().get("/shipments?show=all").data.decode("utf-8")
    assert "Ridgeway, PA to Curico" in body and 'href="/shipments?show=delivered"' in body
    nav = body[body.index("<nav>"):body.index("</nav>")]
    assert nav.index("Inquiries") < nav.index("Shipments") < nav.index("Parties")


def test_the_party_list_and_page_open(world):
    conn, path, ids = world
    app = create_app(path)
    app.config["TESTING"] = True
    client = app.test_client()
    assert b"Kestrel Forwarding" in client.get("/parties").data
    page = client.get("/parties/{}".format(ids["kestrel"]))
    assert page.status_code == 200
    assert b"Inquiries received" in page.data and b"Timeline" in page.data
    assert b'<span class="pill">inquiry</span>' in page.data         # the timeline's kind, as she reads it

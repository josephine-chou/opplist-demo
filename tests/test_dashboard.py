"""The /dashboard page (2026-09-25): every tile and chart on a
fixture with known numbers, the grouping rule, the working-day rule, the
map's LATAM filter, and an export with nothing fetched from anywhere."""

from __future__ import annotations

import json
import re

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import dashboard, db, favicon, paths
from opplist.web import create_app

TODAY = "2026-09-23"                                          # a Wednesday


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "dash.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    for name, region in (("Peru", "Latin America"), ("Mexico", "Latin America")):
        repo.insert("country", {"name": name, "region": region})
    db.set_setting(conn, "latam_countries", json.dumps(["Chile", "Peru", "Mexico"]))
    tolvex = add_party(repo, "Tolvex", country="Chile")                          # a LATAM agent
    tsv = add_party(repo, "KRV Global", country="USA")                           # another agent
    chi = add_party(repo, "JoJo JJC", ptype="Office", country="USA")
    harlow = add_party(repo, "Harlow", ptype="Direct Customer", country="USA")

    a = add_enquiry(repo, "OPP-0001", tolvex, received="2026-09-14", service_mode="Air",
                    origin_country="USA", dest_country="Chile")                  # Mon, quoted Wed: 2 days
    repo.set_status(a, "Quoted", "2026-09-16")
    repo.add_followup(a, "2026-09-22")                                           # this week
    b = add_enquiry(repo, "OPP-0002", tolvex, received="2026-09-11", service_mode="FCL",
                    origin_country="USA", dest_country="Chile")                  # Fri, quoted Tue: 2 days
    repo.set_status(b, "Quoted", "2026-09-15")
    repo.set_status(b, "Won", "2026-09-18", rate_used="own")
    c = add_enquiry(repo, "OPP-0003", tsv, received="2026-09-10", service_mode="Air",
                    origin_country="USA", dest_country="Peru")                   # Thu, quoted Tue: 3 days
    repo.set_status(c, "Quoted", "2026-09-15")
    repo.set_status(c, "Lost", "2026-09-17", closed_reason="Price too high")
    d = add_enquiry(repo, "OPP-0004", chi, received="2026-09-21", service_mode="Air",
                    origin_country="USA", dest_country="China")                  # an office, New
    e = add_enquiry(repo, "OPP-0005", tolvex, received="2026-08-20", service_mode="LCL",
                    origin_country="USA", dest_country="Chile")                  # August, quoted next day, stale
    repo.set_status(e, "Quoted", "2026-08-21")
    f = add_enquiry(repo, "OPP-0006", tsv, received="2026-08-12", service_mode="FCL",
                    origin_country="China", dest_country="USA")                  # lost without a reason
    repo.set_status(f, "Quoted", "2026-08-13")
    repo.set_status(f, "Lost", "2026-08-25")
    g = add_enquiry(repo, "OPP-0007", harlow, received="2026-09-15", scope="Out",
                    service_mode="Air", origin_country="USA", dest_country="Chile")   # out of scope
    repo.set_status(g, "Won", "2026-09-16", rate_used="own")
    h = add_enquiry(repo, "OPP-0008", tolvex, received="2026-09-15", legacy=1,
                    service_mode="Air", origin_country="USA", dest_country="Chile")   # legacy
    i = add_enquiry(repo, "OPP-0009", tolvex, received="2026-09-02", service_mode="Air",
                    origin_country="USA", dest_country="Chile")
    repo.set_status(i, "Dropped", "2026-09-05", closed_reason="Registered in error")
    repo.commit()
    return conn, path


def test_working_days_follow_the_rule():
    assert dashboard.bdays("2026-09-14", "2026-09-16") == 2       # Monday to Wednesday
    assert dashboard.bdays("2026-09-11", "2026-09-15") == 2       # Friday to Tuesday
    assert dashboard.bdays("2026-09-10", "2026-09-15") == 3       # Thursday to Tuesday
    assert dashboard.bdays("2026-09-14", "2026-09-14") == 0
    assert dashboard.bdays("", "2026-09-14") is None and dashboard.bdays("2026-09-16", "2026-09-14") is None


def test_group_of_follows_the_reference():
    assert dashboard.group_of({"requester_type": "Office", "requester_region": "North America"}) == "Our offices"
    # v36: a LATAM agent is an Agent in a country of the Settings list (v_enquiry.latam_agent), not the region
    assert dashboard.group_of({"requester_type": "Agent", "requester_region": "Latin America", "latam_agent": "Y"}) == "LATAM agents"
    assert dashboard.group_of({"requester_type": "Agent", "requester_region": "Latin America", "latam_agent": "N"}) == "Other agents"
    assert dashboard.group_of({"requester_type": "Agent", "requester_region": "Asia", "latam_agent": "N"}) == "Other agents"
    assert dashboard.group_of({"requester_type": None, "requester_region": None}) == "Other agents"


def test_the_six_tiles(world):
    conn, path = world
    dash = dashboard.build(conn, TODAY)
    tiles = {t["label"]: t for t in dash.tiles}
    assert [t["label"] for t in dash.tiles] == [
        "Requests, September to date", "Open now", "Quoted within 2 business days",
        "Won this month", "Quotes awaiting an answer", "LATAM share of requests"]
    assert tiles["Requests, September to date"]["big"] == "5"                    # a b c d i
    assert tiles["Requests, September to date"]["sub"] == "August whole month <b>2</b>"
    assert '<svg class="spark"' in tiles["Requests, September to date"]["spark"]
    assert tiles["Open now"]["big"] == "3" and tiles["Open now"]["sub"] == "on us <b>1</b> &middot; on them <b>2</b>"
    assert tiles["Quoted within 2 business days"]["big"] == "67%"                 # a, b within; c not
    assert tiles["Quoted within 2 business days"]["sub"] == "median 2 days from receipt &middot; August <b>100%</b>"
    assert tiles["Won this month"]["big"] == "1"
    assert tiles["Won this month"]["sub"] == "lost <b>1</b> &middot; 90-day win rate <b>33%</b>"   # 1 won, 2 lost
    assert tiles["Quotes awaiting an answer"]["big"] == "2"
    assert tiles["Quotes awaiting an answer"]["sub"] == \
        "follow-ups sent this week <b>1</b> &middot; older than 10 business days <b>1</b>"
    assert tiles["LATAM share of requests"]["big"] == "80%"                       # d goes to China
    assert tiles["LATAM share of requests"]["sub"] == "Chile is <b>100%</b> of LATAM wins (90 days)"


def test_the_tables_and_the_map(world):
    conn, path = world
    dash = dashboard.build(conn, TODAY)
    assert [(r["key"], r["country"], r["n"], r["quoted"], r["won"]) for r in dash.requesters] == [
        ("Tolvex", "Chile", 4, 3, 1), ("KRV Global", "USA", 2, 2, 0), ("JoJo JJC", "USA", 1, 0, 0)]
    assert dash.lost == [("Not recorded", 1), ("Price too high", 1)]
    # the map: LATAM countries only, USA and China never get a bubble
    assert dash.map_since == "Aug 1"
    assert [(n, t["n"], t["quoted"], t["won"]) for n, t in dash.map_rows] == [("Chile", 4, 3, 1), ("Peru", 1, 1, 0)]
    svg = dashboard.map_svg(dash.map_rows)
    assert svg.count("<circle") == 3                                            # Chile bubble + won core, Peru bubble
    assert "Chile <tspan" in svg and "Peru <tspan" not in svg                     # labels from n >= 2
    assert "United States" not in svg.split("</path>")[-1]
    assert [(l["origin"], l["dest"], l["n"], l["won"]) for l in dash.lanes] == [
        ("USA", "Chile", 4, 1), ("China", "USA", 1, 0), ("USA", "China", 1, 0), ("USA", "Peru", 1, 0)]


def test_the_90_day_tab_moves_the_window(world):
    conn, path = world
    dash = dashboard.build(conn, TODAY, "90")
    assert dash.period["key"] == "90"
    tiles = {t["label"]: t for t in dash.tiles}
    assert tiles["Requests, last 90 days"]["big"] == "7" and "previous 90 days <b>0</b>" in tiles["Requests, last 90 days"]["sub"]
    assert tiles["Won last 90 days"]["big"] == "1" and "lost <b>2</b>" in tiles["Won last 90 days"]["sub"]
    assert dash.pipeline["closed_title"] == "Closed in the last 90 days"
    assert dash.mix[0] == ("LATAM agents", 4, dashboard.C["latam"])


def test_the_page_and_the_nav(world):
    conn, path = world
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    client = app.test_client()
    body = client.get("/dashboard").data.decode("utf-8")
    nav = body[body.index("<nav>"):body.index("</nav>")]
    assert nav.index("Tracking") < nav.index("Dashboard") < nav.index("Report")
    assert "OPP-" not in body                                                   # never an OPP number
    assert 'class="tab on" href="/dashboard?period=month"' in body
    assert 'href="/dashboard?period=90"' in body
    assert body.count('<div class="tile">') == 6 and "Where the LATAM traffic is" in body
    assert 'action="/export/dashboard"' in body and ">Export HTML<" in body
    assert "Tolvex" in body and "Price too high" in body
    body90 = client.get("/dashboard?period=90").data.decode("utf-8")
    assert "Requests, last 90 days" in body90


def test_the_export_is_self_contained(world, tmp_path, monkeypatch):
    conn, path = world
    dash = dashboard.build(conn, TODAY)
    written = dashboard.write_html(dash, out_dir=tmp_path / "reports")
    assert written.name == "DASHBOARD 2026-09-23.html"
    text = written.read_text(encoding="utf-8")
    # I1: the one link is the tab icon, a data: URI - nothing fetched
    icon = '<link rel="icon" type="image/png" sizes="32x32" href="{}">'.format(favicon.data_uri())
    assert text.count(icon) == 1
    text = text.replace(icon, "")
    assert "http" not in text and "<script" not in text and "<link" not in text
    assert text.count("<svg") >= 5 and "OPP-" not in text and ".dash .tiles" in text
    assert "Export HTML" not in text                                            # the file has no button
    # the route writes to docs\\reports; here that folder is a temp one
    conn.close()
    monkeypatch.setattr(paths, "REPORTS_DIR", tmp_path / "docs-reports")
    app = create_app(path)
    app.config["TESTING"] = True
    client = app.test_client()
    body = client.post("/export/dashboard", data={"period": "month"}, follow_redirects=True).data.decode("utf-8")
    assert "Dashboard written:" in body
    assert (tmp_path / "docs-reports" / "DASHBOARD {}.html".format(dash.today[:10])).exists() or \
        re.search(r"DASHBOARD \d{4}-\d{2}-\d{2}\.html", body)

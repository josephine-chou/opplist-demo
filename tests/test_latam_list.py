"""Batch 20 (schema v36): LATAM is the latam_countries list in Settings,
everywhere - the views, the month report, the Dashboard, the Enquiries
list's LATAM filter. The country table's region is geography only: a
country whose region is Latin America but which is not on the list is not
LATAM, as a route end or as an agent's country. Editing the list changes
LATAM at once.

Every company, case number, date and lane here is invented; cases from
OPP-2901."""

from __future__ import annotations

import re

import pytest

from conftest import add_enquiry, add_party
from opplist import dashboard, db, monthly
from opplist.web import create_app

MONTH = "2026-11"


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "latam.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    for name, region in (("USA", "North America"), ("China", "Asia"), ("Chile", "Latin America"),
                         ("Barbados", "Latin America"), ("Unknown", "Undefined")):
        repo.insert("country", {"name": name, "region": region})
    db.set_setting(conn, "latam_countries", ["Chile"])
    kestrel = add_party(repo, "Kestrel Forwarding", country="USA")
    tern = add_party(repo, "Tern Cargo", country="Barbados")            # an agent in a region-only country
    wren = add_party(repo, "Wren Logistics", country="Chile")           # an agent in a list country
    lark = add_party(repo, "Lark Imports", ptype="Direct Customer", country="Chile")
    ids = {}
    for key, opp, party, origin, dest in (
            ("to_barbados", "OPP-2901", kestrel, "USA", "Barbados"),
            ("barbados_agent", "OPP-2902", tern, "USA", "China"),
            ("to_chile", "OPP-2903", kestrel, "USA", "Chile"),
            ("chile_agent", "OPP-2904", wren, "USA", "China"),
            ("chile_customer", "OPP-2905", lark, "USA", "China")):
        ids[key] = add_enquiry(repo, opp, party, received="2026-11-03", origin_country=origin, dest_country=dest,
                               service_mode="Air")
    # a second route of the Barbados case ends in Chile: that route is LATAM (the case's own
    # flag reads route A, the list's LATAM filter reads the case or any of its routes)
    repo.add_route(ids["to_barbados"], {"origin_country": "USA", "dest_country": "Chile", "service_mode": "Air"})
    repo.commit()
    yield conn, path, ids
    conn.close()


def flags(conn):
    return {r["opp_no"]: (r["latam"], r["latam_lane"], r["latam_agent"]) for r in conn.execute(
        "SELECT opp_no, latam, latam_lane, latam_agent FROM v_enquiry ORDER BY opp_no")}


def route_flags(conn):
    return {(r["opp_no"], r["letter"]): r["latam"] for r in conn.execute(
        "SELECT opp_no, letter, latam FROM v_route ORDER BY opp_no, seq")}


def test_the_list_not_the_region_decides_latam(world):
    conn, path, ids = world
    assert flags(conn) == {
        "OPP-2901": ("N", "N", "N"),        # route A ends in Barbados (region Latin America, not on the list)
        "OPP-2902": ("N", "N", "N"),        # an agent in Barbados (region Latin America, not on the list)
        "OPP-2903": ("Y", "Y", "N"),
        "OPP-2904": ("Y", "N", "Y"),        # a LATAM agent
        "OPP-2905": ("Y", "N", "N"),        # DC1: a direct customer in Chile is LATAM, not a LATAM agent
    }
    assert route_flags(conn)[("OPP-2901", "A")] == "N" and route_flags(conn)[("OPP-2901", "B")] == "Y"
    parties = {r["key"]: (r["region"], r["latam"], r["latam_agent"]) for r in conn.execute(
        "SELECT key, region, latam, latam_agent FROM v_party_stats")}
    assert parties["Tern Cargo"] == ("Latin America", "N", "N")          # the region stays, as geography
    assert parties["Wren Logistics"] == ("Latin America", "Y", "Y")
    assert parties["Lark Imports"] == ("Latin America", "Y", "N")


def test_the_month_report_reads_the_list(world):
    conn, path, ids = world
    report = monthly.build(conn, MONTH)
    assert ("3 / 2", "LATAM / Rest of world") in report.kpis          # DC1: with the Chile customer
    labels = [row.label for row in report.top_agents.rows]
    latam_part = labels[labels.index("LATAM agents") + 1:labels.index("Other agents")]
    assert latam_part == ["Wren Logistics"] and "Tern Cargo" in labels[labels.index("Other agents"):]


def test_adding_a_country_in_settings_makes_its_inquiries_latam_everywhere_at_once(world):
    conn, path, ids = world
    app = create_app(path)
    app.config["TESTING"] = True
    client = app.test_client()

    def listed():
        body = client.get("/enquiries?view=all").data.decode("utf-8")
        return {m.group(1): m.group(2) for m in re.finditer(
            r'<tr class="enq-row" data-id="(\d+)".*?data-latam="([YN])"', body, re.S)}

    before = listed()
    assert before[str(ids["barbados_agent"])] == "N" and before[str(ids["chile_agent"])] == "Y"
    assert before[str(ids["to_barbados"])] == "Y"                 # its route B ends in Chile
    share = dashboard.build(conn, today="2026-11-20")
    assert any(t["label"] == "LATAM share of requests" and t["big"] == "60%" for t in share.tiles)

    page = client.post("/settings", data={"latam_countries": "Chile\nBarbados"}, follow_redirects=True)
    assert page.status_code == 200
    assert db.get_setting_list(conn, "latam_countries") == ["Chile", "Barbados"]

    # the views, at once - nothing cached
    assert flags(conn)["OPP-2902"] == ("Y", "N", "Y") and flags(conn)["OPP-2901"] == ("Y", "Y", "N")
    assert route_flags(conn)[("OPP-2901", "A")] == "Y"
    # the month report
    assert ("5 / 0", "LATAM / Rest of world") in monthly.build(conn, MONTH).kpis
    # the Dashboard: the share, the LATAM agents group, the map
    share = dashboard.build(conn, today="2026-11-20")
    assert any(t["label"] == "LATAM share of requests" and t["big"] == "100%" for t in share.tiles)
    assert dashboard.group_of({"requester_type": "Agent", "latam_agent": "Y"}) == "LATAM agents"
    assert "Barbados" in {country for country, _counts in share.map_rows}
    # the Enquiries list's LATAM filter
    after = listed()
    assert after[str(ids["barbados_agent"])] == "Y" and after[str(ids["chile_customer"])] == "Y"


def test_an_upgraded_database_gets_the_list_rule(tmp_path):
    from opplist import schema_migrations
    path = tmp_path / "v35.db"
    conn = db.open_db(path)
    conn.execute("DROP VIEW v_latam_country")
    conn.execute("UPDATE setting SET value = '35' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    notes = []
    conn = db.open_db(path, upgrade_notes=notes)
    assert schema_migrations.current_version(conn) == schema_migrations.CURRENT_VERSION >= 36
    assert any("v_latam_country" in n for n in notes)
    assert conn.execute("SELECT COUNT(*) FROM v_latam_country").fetchone()[0] == len(
        db.get_setting_list(conn, "latam_countries"))
    conn.close()

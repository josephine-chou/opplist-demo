"""RD1c (2026-10-20): a place the register page would create is pending until
Register. The picker's Add and the old inline 'Add
location...' are kept in the form ("new: KEY"), asked of the naming rules at
once, and written with the case in one transaction; a refused save, the
duplicate panel or leaving the page writes nothing. check lists the To
confirm places from intake that nothing points at.

Every company, place, code, case and date here is invented; cases from
OPP-2001."""

from __future__ import annotations

import json

import pytest

from conftest import add_party
from opplist import checks, db, migrate_master
from opplist.web import create_app

TODAY = "2026-11-12"


def seed(repo):
    for name, region, iso2 in (("USA", "North America", "US"), ("Chile", "Latin America", "CL"),
                               ("Unknown", "Undefined", None)):
        repo.insert("country", {"name": name, "region": region, "iso2": iso2})
    db.set_setting(repo.conn, "next_opp_number", 2001)
    repo.add_master_location("MERRIDALE, OH", "USA", "city", name="Merridale", subdivision="OH", source="phase1")
    repo.add_master_location("PUERTO ALVERO", "Chile", "city", name="Puerto Alvero", source="phase1")
    repo.conn.execute("ALTER TABLE location RENAME TO location_old")
    repo.conn.execute(migrate_master.LOCATION_VIEW_SQL)


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "pending.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed(repo)
    agent = add_party(repo, "Brookvale Freight", country="Chile")
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    return {"client": app.test_client(), "path": path, "agent": agent}


def count(path, sql, args=()):
    conn = db.open_db(path)
    try:
        return conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()


def places_named(path, key):
    return count(path, "SELECT COUNT(*) FROM master_location WHERE key = ?", (key,))


def pending(key, country="Chile", name=""):
    return json.dumps([{"key": key, "name": name, "country": country, "port": 0, "airport": 0}])


def register(client, agent, **extra):
    values = {"received_on": TODAY, "requester_party_id": str(agent), "service_mode": "FCL",
              "container_type": "40HQ", "container_qty": "1", "origin_country": "USA",
              "dest_country": "Chile", "commodity": "Garden benches", "decision": "new",
              "origin_loc": "MERRIDALE, OH", "dest_loc": "PUERTO ALVERO"}
    values.update(extra)
    return client.post("/enquiries/new", data=values, follow_redirects=True)


def test_register_writes_the_pending_place_and_the_case_together(world):
    r = register(world["client"], world["agent"], dest_loc="LAGUNA TERRAZ", dest_loc_new=pending("LAGUNA TERRAZ"))
    assert r.status_code == 200
    conn = db.open_db(world["path"])
    place = conn.execute("SELECT * FROM master_location WHERE key = 'LAGUNA TERRAZ'").fetchone()
    case = conn.execute("SELECT * FROM enquiry WHERE opp_no = 'OPP-2001'").fetchone()
    assert (place["status"], place["source"], place["country"]) == ("to_confirm", "intake", "Chile")
    assert case["dest_loc_id"] == place["id"]
    # one transaction: the place and the case carry the same action
    acts = {r[0] for r in conn.execute(
        "SELECT action_id FROM change_log WHERE (table_name = 'master_location' AND row_id = ?) "
        "OR (table_name = 'enquiry' AND row_id = ?)", (place["id"], case["id"]))}
    conn.close()
    assert len(acts) == 1


def test_the_old_inline_add_location_is_pending_too(world):
    r = register(world["client"], world["agent"], origin_country="Chile", origin_loc="",
                 new_origin_loc="Quebrada Selva", dest_loc="", new_dest_loc="  Peñasco Álamo ")
    assert r.status_code == 200
    conn = db.open_db(world["path"])
    case = conn.execute("SELECT origin_loc, dest_loc FROM enquiry WHERE opp_no = 'OPP-2001'").fetchone()
    alias = conn.execute("SELECT alias FROM master_location_alias a JOIN master_location l ON l.id = a.location_id "
                         "WHERE l.key = 'PENASCO ALAMO'").fetchone()
    conn.close()
    assert tuple(case) == ("QUEBRADA SELVA", "PENASCO ALAMO") and alias[0] == "PEÑASCO ÁLAMO"


def test_the_duplicate_panel_writes_neither(world):
    client, agent = world["client"], world["agent"]
    first = dict(created_by="outlook", entry_id="FAKE-ENTRY-0001", conversation_id="FAKE-CONV-0001",
                 source_subject="RFQ Merridale to Puerto Alvero // garden benches")
    register(client, agent, **first)
    assert count(world["path"], "SELECT COUNT(*) FROM enquiry") == 1
    before = count(world["path"], "SELECT COUNT(*) FROM master_location")
    parties = count(world["path"], "SELECT COUNT(*) FROM party")
    r = register(client, agent, decision="", dest_loc="LAGUNA TERRAZ", dest_loc_new=pending("LAGUNA TERRAZ"),
                 origin_loc="", new_origin_loc="Ashby Crossing, OH",
                 new_party_key="Kestrel Lane Cargo", new_party_type="Agent", new_party_country="Chile", **first)
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and "OPP-2001" in page
    assert count(world["path"], "SELECT COUNT(*) FROM enquiry") == 1
    assert count(world["path"], "SELECT COUNT(*) FROM master_location") == before
    assert count(world["path"], "SELECT COUNT(*) FROM party") == parties
    # the form keeps them pending for Register
    assert "LAGUNA TERRAZ" in page and "ASHBY CROSSING, OH" in page and "Kestrel Lane Cargo" in page
    # 'Open the existing case' instead: nothing either
    register(client, agent, decision="duplicate", decision_target="1", dest_loc="LAGUNA TERRAZ",
             dest_loc_new=pending("LAGUNA TERRAZ"), origin_loc="", new_origin_loc="Ashby Crossing, OH", **first)
    assert count(world["path"], "SELECT COUNT(*) FROM master_location") == before
    # then Register: both, once
    register(client, agent, decision="new", dest_loc="LAGUNA TERRAZ", dest_loc_new=pending("LAGUNA TERRAZ"),
             origin_loc="", new_origin_loc="Ashby Crossing, OH", **first)
    assert count(world["path"], "SELECT COUNT(*) FROM enquiry") == 2
    assert places_named(world["path"], "LAGUNA TERRAZ") == 1 and places_named(world["path"], "ASHBY CROSSING, OH") == 1


def test_a_refused_save_writes_neither(world):
    before = count(world["path"], "SELECT COUNT(*) FROM master_location")
    # a pending place that is fine, beside a field that is not (no containers)
    r = register(world["client"], world["agent"], container_qty="", container_type="",
                 dest_loc="LAGUNA TERRAZ", dest_loc_new=pending("LAGUNA TERRAZ"),
                 origin_loc="", new_origin_loc="Ashby Crossing, OH")
    assert r.status_code == 400
    # a pending place the rules refuse: said in the form, nothing written
    r = register(world["client"], world["agent"], origin_loc="", new_origin_loc="Ashby Crossing")
    assert r.status_code == 400 and "without its state" in r.get_data(as_text=True)
    assert count(world["path"], "SELECT COUNT(*) FROM master_location") == before
    assert count(world["path"], "SELECT COUNT(*) FROM enquiry") == 0


def test_two_pending_entries_with_one_key_are_one_place(world):
    client = world["client"]
    # route A's destination and route B's, each added in its own picker,
    # one spelled the rules' way and one not
    r = register(client, world["agent"], origin_country="USA",
                 dest_loc="LAGUNA TERRAZ", dest_loc_new=pending("LAGUNA TERRAZ"),
                 route_2_origin_country="USA", route_2_origin_loc="MERRIDALE, OH", route_2_dest_country="Chile",
                 route_2_dest_loc="laguna  terraz", route_2_dest_loc_new=pending("laguna  terraz"),
                 route_2_service_mode="FCL", route_2_incoterm="")
    assert r.status_code == 200
    assert places_named(world["path"], "LAGUNA TERRAZ") == 1
    assert count(world["path"], "SELECT COUNT(DISTINCT dest_loc_id) || '/' || COUNT(*) FROM enquiry_route") == "1/2"
    # a pending key created meanwhile (another tab) is that row, not a second
    conn = db.open_db(world["path"])
    repo = db.Repository(conn, source="test")
    made = repo.add_master_location("RIO VERDALE", "Chile", status="to_confirm", source="intake")["id"]
    repo.commit()
    conn.close()
    register(client, world["agent"], dest_loc="RIO VERDALE", dest_loc_new=pending("RIO VERDALE"))
    assert places_named(world["path"], "RIO VERDALE") == 1
    assert count(world["path"], "SELECT dest_loc_id FROM enquiry WHERE opp_no = 'OPP-2002'") == made


def test_the_rules_answer_at_once(world):
    client = world["client"]
    got = client.get("/api/place-rules?text=Ashby+Crossing&country=USA").get_json()
    assert "without its state" in got["error"]
    got = client.get("/api/place-rules?text=FOB+Ashby+Crossing+oh&country=USA").get_json()
    assert (got["key"], got["error"], got["found"]) == ("ASHBY CROSSING, OH", None, None)
    assert got["notes"][0].startswith("'FOB' taken off")
    got = client.get("/api/place-rules?text=Merridale+OH&country=USA").get_json()
    assert (got["key"], got["found"]) == ("MERRIDALE, OH", "MERRIDALE, OH")
    assert "This is a country" in client.get("/api/place-rules?text=Chile&country=Chile").get_json()["error"]
    assert "two-letter" in client.get("/api/place-rules?text=WA&country=USA").get_json()["error"]
    # asking writes nothing
    assert places_named(world["path"], "ASHBY CROSSING, OH") == 0


def test_check_lists_intake_places_nothing_uses(world):
    register(world["client"], world["agent"], dest_loc="LAGUNA TERRAZ", dest_loc_new=pending("LAGUNA TERRAZ"))
    conn = db.open_db(world["path"])
    repo = db.Repository(conn, source="test")
    repo.pin_action()
    stray = repo.add_master_location("SAN TELMO ALTO", "Chile", status="to_confirm", source="intake",
                                     note="Added from the inquiry form.")
    act = repo.action_id
    # pointed at only by a route's place, a shipment's via points, a gateway's parent: used
    repo.add_master_location("VALLE OSCURO", "Chile", status="to_confirm", source="intake")
    city = repo.add_master_location("CERRO MANSO", "Chile", status="to_confirm", source="intake")
    repo.add_master_location("QZM", "Chile", "airport", parent_id=city["id"], is_airport=1)
    case = conn.execute("SELECT id FROM enquiry WHERE opp_no = 'OPP-2001'").fetchone()[0]
    route = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ?", (case,)).fetchone()[0]
    repo.insert("shipment", {"enquiry_id": case, "route_id": route, "mode": "ocean",
                             "title": "Merridale > Laguna Terraz", "via_points": "Valle Oscuro"})
    # a to confirm place from a person (not intake), and an active one: not asked
    repo.add_master_location("PLAYA SECA", "Chile", status="to_confirm", source="manual")
    repo.commit()
    found = {f.key: f for f in checks.run(conn, today=TODAY)}["unused_intake_locations"]
    conn.close()
    assert [r["key"] for r in found.rows] == ["SAN TELMO ALTO"] and found.count == 1
    row = found.rows[0]
    assert (row["action_id"], row["logged_by"], row["created_at"]) == (act, "test", stray["created_at"])
    assert not found.informational and "Locations page" in found.note

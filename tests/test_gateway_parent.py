"""RD1 (2026-10-19, schema v42): an airport, seaport or ramp names the
market city it serves (its parent), or carries 'No parent city' with a note
that says why. The Locations page refuses it otherwise, in the form; a city
has neither; check lists the gateways still without one. Also the Locations
page's create / edit / alias round trip, rewritten on invented data from the
M2 test it replaces.

Every company, place, code, case and date here is invented; cases from
OPP-2001."""

from __future__ import annotations

import pytest

from conftest import add_party
from opplist import checks, db, migrate_master, schema_migrations
from opplist.web import create_app

TODAY = "2026-11-12"


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "gateways.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    for name, region, iso2 in (("USA", "North America", "US"), ("Chile", "Latin America", "CL"),
                               ("Unknown", "Undefined", None)):
        repo.insert("country", {"name": name, "region": region, "iso2": iso2})
    db.set_setting(conn, "next_opp_number", 2001)
    city = repo.add_master_location("HARROWGATE, OH", "USA", "city", name="Harrowgate", subdivision="OH",
                                    source="phase1")
    repo.add_master_location("QHW", "USA", "airport", name="Harrowgate Field", iata="QHW",
                             parent_id=city["id"], is_airport=1, source="phase1")
    repo.add_master_location("LINDENPORT", "Chile", "city", name="Lindenport", source="phase1")
    repo.add_master_location("PUERTO VELADO", "Chile", "seaport", name="Puerto Velado",
                             status="to_confirm", source="intake")
    repo.add_master_location("QXR", "Chile", "airport", name="Old Strip", status="retired", source="phase1")
    agent = add_party(repo, "Fenmore Cargo", country="Chile")
    conn.execute("ALTER TABLE location RENAME TO location_old")
    conn.execute(migrate_master.LOCATION_VIEW_SQL)
    repo.commit()
    ids = {r["key"]: r["id"] for r in conn.execute("SELECT id, key FROM master_location")}
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    return {"client": app.test_client(), "path": path, "ids": ids, "agent": agent}


def row_of(world, key):
    conn = db.open_db(world["path"])
    try:
        return conn.execute("SELECT * FROM master_location WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()


def new_place(**extra):
    values = {"key": "Bracken Quay", "name": "Bracken Quay", "type": "seaport", "country": "Chile",
              "unlocode": "", "iata": "", "is_seaport": "1", "status": "active", "parent_key": "",
              "area_key": "", "note": ""}
    values.update(extra)
    return values


# ------------------------------------------------------------ the rule --
def test_the_rule_itself():
    problem = db.gateway_parent_problem
    assert problem({"type": "city", "status": "active"}) is None
    assert "needs its parent city" in problem({"type": "seaport", "status": "active"})
    assert problem({"type": "airport", "status": "active"}).startswith("An airport")
    assert problem({"type": "ramp", "status": "to_confirm"}).startswith("A ramp")
    assert problem({"type": "seaport", "status": "active", "parent_id": 4}) is None
    assert "say why in the note" in problem({"type": "seaport", "status": "active", "no_parent": 1})
    assert problem({"type": "seaport", "status": "active", "no_parent": 1, "note": "  "}) is not None
    assert problem({"type": "seaport", "status": "active", "no_parent": 1, "note": "serves two cities"}) is None
    # retiring a wrong row never asks for a parent it does not have
    assert problem({"type": "airport", "status": "retired"}) is None


def test_a_seaport_without_a_parent_is_refused_in_the_form_with_what_was_typed(world):
    r = world["client"].post("/locations/new", data=new_place(note="typed before the refusal"))
    assert r.status_code == 200
    page = r.get_data(as_text=True)
    assert row_of(world, "BRACKEN QUAY") is None
    assert '<em class="err">A seaport needs its parent city' in page
    assert 'value="Bracken Quay"' in page and "typed before the refusal" in page
    assert 'name="no_parent"' in page
    # ticked, but nothing in the note: refused again, and the box stays ticked
    r = world["client"].post("/locations/new", data=new_place(no_parent="1"))
    page = r.get_data(as_text=True)
    assert row_of(world, "BRACKEN QUAY") is None
    assert "&#39;No parent city&#39; is ticked: say why in the note." in page
    assert 'name="no_parent" checked' in page
    # a parent typed that names nothing on file is not taken as 'none'
    r = world["client"].post("/locations/new", data=new_place(parent_key="Nowhere Flats"))
    assert row_of(world, "BRACKEN QUAY") is None
    assert "No location NOWHERE FLATS on file" in r.get_data(as_text=True)


def test_a_seaport_is_saved_with_the_box_and_a_note_or_with_a_parent(world):
    r = world["client"].post("/locations/new", data=new_place(no_parent="1", note="serves the whole coast"),
                             follow_redirects=True)
    assert r.status_code == 200
    row = row_of(world, "BRACKEN QUAY")
    assert (row["type"], row["parent_id"], row["no_parent"], row["note"]) == \
        ("seaport", None, 1, "serves the whole coast")
    world["client"].post("/locations/new", data=new_place(key="Dunmere Pier", name="Dunmere Pier",
                                                          parent_key="lindenport", no_parent="1"))
    row = row_of(world, "DUNMERE PIER")
    # a parent wins over the box: it is not 'no parent' any more
    assert (row["parent_id"], row["no_parent"]) == (world["ids"]["LINDENPORT"], 0)


def test_editing_a_gateway_without_a_parent_asks_for_one_and_changes_nothing(world):
    port = world["ids"]["PUERTO VELADO"]
    data = new_place(key="PUERTO VELADO", name="Puerto Velado", status="to_confirm", note="checked the map")
    r = world["client"].post("/locations/{}".format(port), data=data)
    assert r.status_code == 200
    assert "A seaport needs its parent city" in r.get_data(as_text=True)
    assert row_of(world, "PUERTO VELADO")["note"] is None
    r = world["client"].post("/locations/{}".format(port), data=dict(data, parent_key="LINDENPORT"),
                             follow_redirects=True)
    row = row_of(world, "PUERTO VELADO")
    assert (row["parent_id"], row["note"]) == (world["ids"]["LINDENPORT"], "checked the map")
    # a retired gateway saves without one
    old = world["ids"]["QXR"]
    world["client"].post("/locations/{}".format(old), data=new_place(
        key="QXR", name="Old Strip", type="airport", status="retired", note="closed strip"))
    assert row_of(world, "QXR")["note"] == "closed strip"


def test_a_city_shows_no_box_and_never_keeps_a_parent(world):
    page = world["client"].get("/locations/{}".format(world["ids"]["LINDENPORT"])).get_data(as_text=True)
    # the parent and the box are there for the type switch, hidden for a city
    assert page.count('class="f gateway-only" hidden') == 2
    page = world["client"].get("/locations/{}".format(world["ids"]["QHW"])).get_data(as_text=True)
    assert 'class="f gateway-only" hidden' not in page and 'name="no_parent"' in page
    world["client"].post("/locations/{}".format(world["ids"]["LINDENPORT"]), data=new_place(
        key="LINDENPORT", name="Lindenport", type="city", is_seaport="", parent_key="HARROWGATE, OH",
        no_parent="1"))
    row = row_of(world, "LINDENPORT")
    assert (row["parent_id"], row["no_parent"]) == (None, 0)


def test_check_lists_the_gateways_without_a_parent_until_she_decides(world):
    conn = db.open_db(world["path"])
    finding = checks.gateways_without_parent(conn)
    assert (finding.count, finding.rows) == (1, [{"key": "PUERTO VELADO", "type": "seaport",
                                                  "country": "Chile", "status": "to_confirm"}])
    assert not finding.informational
    assert "gateways_without_parent" in [f.key for f in checks.run(conn, today=TODAY)]
    text = "\n".join(checks.render([finding]))
    assert "[LOOK] 1     Gateways without a parent city" in text and "key=PUERTO VELADO" in text
    conn.close()
    world["client"].post("/locations/{}".format(world["ids"]["PUERTO VELADO"]), data=new_place(
        key="PUERTO VELADO", name="Puerto Velado", status="to_confirm", no_parent="1",
        note="a terminal between two towns"))
    conn = db.open_db(world["path"])
    assert checks.gateways_without_parent(conn).count == 0
    conn.close()


def test_the_v42_upgrade_adds_no_parent_and_the_route_views(tmp_path):
    conn = db.open_db(tmp_path / "old.db")
    conn.execute("DROP VIEW v_master_route_lane")
    conn.execute("DROP VIEW v_master_location")
    conn.execute("CREATE VIEW v_master_location AS SELECT id, key FROM master_location")
    conn.execute("ALTER TABLE master_location DROP COLUMN no_parent")
    conn.execute("UPDATE setting SET value = '41' WHERE key = 'schema_version'")
    conn.commit()
    notes = schema_migrations.upgrade(conn)
    assert "master_location: no_parent added (0 on every row)" in notes
    assert any(n.startswith("contract views rebuilt: v_master_location") and "v_master_route_lane" in n
               for n in notes)
    assert schema_migrations.current_version(conn) == schema_migrations.CURRENT_VERSION >= 42
    cols = [r[1] for r in conn.execute("PRAGMA table_info(v_master_location)")]
    assert cols[-1] == "no_parent"
    assert conn.execute("SELECT COUNT(*) FROM v_master_route_lane").fetchone()[0] == 0
    # run again: nothing to add, nothing broken
    assert schema_migrations._to_v42(conn)[0].startswith("contract views rebuilt")
    conn.close()


# ------------------------------------- the Locations page round trip --
def test_locations_page_creates_edits_and_aliases_master_rows(world):
    client = world["client"]
    r = client.post("/locations/new", data=new_place(key="Ashgrove Wharf", name="Ashgrove Wharf",
                                                     unlocode="clzaw", parent_key="LINDENPORT",
                                                     note="the town's port"), follow_redirects=True)
    assert r.status_code == 200
    row = row_of(world, "ASHGROVE WHARF")
    assert (row["type"], row["unlocode"], row["is_seaport"], row["source"], row["note"], row["parent_id"]) == \
        ("seaport", "CLZAW", 1, "manual", "the town's port", world["ids"]["LINDENPORT"])
    # the same name again goes to the existing row
    r = client.post("/locations/new", data={"key": "ashgrove wharf", "country": "Chile", "type": "city"},
                    follow_redirects=False)
    assert r.headers["Location"].endswith("/locations/{}".format(row["id"]))
    # edit: parent by key, status, and a key change that follows the cases
    conn = db.open_db(world["path"])
    case = db.Repository(conn, "test").create_enquiry(
        {"requester_party_id": world["agent"], "received_on": TODAY, "origin_loc": "HARROWGATE, OH",
         "origin_country": "USA", "dest_loc": "ASHGROVE WHARF", "dest_country": "Chile"}, TODAY)
    conn.commit()
    conn.close()
    r = client.post("/locations/{}".format(row["id"]), data=new_place(
        key="ASHGROVE PIER", name="Ashgrove Wharf", unlocode="CLZAW", status="to_confirm",
        parent_key="harrowgate, oh"), follow_redirects=True)
    assert r.status_code == 200
    conn = db.open_db(world["path"])
    row = conn.execute("SELECT * FROM master_location WHERE id = ?", (row["id"],)).fetchone()
    assert (row["key"], row["status"], row["note"]) == ("ASHGROVE PIER", "to_confirm", None)
    assert row["parent_id"] == world["ids"]["HARROWGATE, OH"]
    assert conn.execute("SELECT dest_loc FROM enquiry WHERE id = ?", (case,)).fetchone()[0] == "ASHGROVE PIER"
    assert conn.execute("SELECT COUNT(*) FROM change_log WHERE table_name = 'master_location' "
                        "AND row_id = ? AND field = 'key'", (row["id"],)).fetchone()[0] == 1
    conn.close()
    # aliases: add, refuse one that is another place's key, remove
    client.post("/locations/{}/aliases".format(row["id"]), data={"alias": "ashgrove wharf"})
    client.post("/locations/{}/aliases".format(row["id"]), data={"alias": "QHW"})
    conn = db.open_db(world["path"])
    aliases = [r[0] for r in conn.execute("SELECT alias FROM master_location_alias WHERE location_id = ?",
                                          (row["id"],))]
    assert aliases == ["ASHGROVE WHARF"]
    conn.close()
    client.post("/locations/{}/aliases".format(row["id"]), data={"remove": "ASHGROVE WHARF"})
    conn = db.open_db(world["path"])
    assert conn.execute("SELECT COUNT(*) FROM master_location_alias WHERE location_id = ?",
                        (row["id"],)).fetchone()[0] == 0
    assert conn.execute("SELECT old_value FROM change_log WHERE table_name = 'master_location_alias' "
                        "AND field = '(delete)'").fetchone()[0] == "ASHGROVE WHARF -> {} (manual)".format(row["id"])
    conn.close()
    page = client.get("/locations/{}".format(row["id"])).get_data(as_text=True)
    assert "ASHGROVE PIER" in page and "OPP-2001" in page

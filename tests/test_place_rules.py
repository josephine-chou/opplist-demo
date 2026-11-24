"""RD1b (2026-10-20): the master's naming rules of 16 Sep, applied by every
path that creates a master_location - a US or Canadian city is 'NAME, ST'
(D1), a country is not a location (D3), and a mode or incoterm word glued to
the front of a place is taken off with a note and the rest looked up again.
Rows already on file are not changed.

Every company, place, code, case and date here is invented; cases from
OPP-2001."""

from __future__ import annotations

import pytest

from conftest import add_party
from opplist import db, migrate_master
from opplist.web import create_app

TODAY = "2026-11-12"


def seed(repo):
    for name, region, iso2 in (("USA", "North America", "US"), ("Canada", "North America", "CA"),
                               ("Chile", "Latin America", "CL"), ("Taiwan", "Asia", "TW"),
                               ("Singapore", "Asia", "SG"), ("Unknown", "Undefined", None)):
        repo.insert("country", {"name": name, "region": region, "iso2": iso2})
    db.set_setting(repo.conn, "next_opp_number", 2001)
    repo.add_master_location("QXT", "Taiwan", "airport", name="Quillton Field", iata="QXT", is_airport=1,
                             source="phase1")
    repo.add_master_location("MERRIDALE, OH", "USA", "city", name="Merridale", subdivision="OH", source="phase1")
    repo.add_master_location("PUERTO ALVERO", "Chile", "city", name="Puerto Alvero", source="phase1")
    # a row that breaks the rules, written before them: left as it is
    repo.insert("master_location", {"key": "OLDHAVEN IL", "name": "OLDHAVEN IL", "type": "city",
                                    "country": "USA", "status": "to_confirm", "source": "intake"})
    repo.conn.execute("ALTER TABLE location RENAME TO location_old")
    repo.conn.execute(migrate_master.LOCATION_VIEW_SQL)


@pytest.fixture
def repo(tmp_path):
    conn = db.open_db(tmp_path / "rules.db")
    repo = db.Repository(conn, source="test")
    seed(repo)
    yield repo
    conn.close()


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "rules-web.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed(repo)
    agent = add_party(repo, "Brookvale Freight", country="Chile")
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    return {"client": app.test_client(), "path": path, "agent": agent}


def keys(path):
    conn = db.open_db(path)
    try:
        return {r[0] for r in conn.execute("SELECT key FROM master_location")}
    finally:
        conn.close()


# ------------------------------------------------------------- the rules --
def test_a_mode_or_incoterm_word_is_taken_off_and_the_rest_looked_up_again(repo):
    rules = db.place_rules(repo.conn, "AIR QXT", "Taiwan")
    assert rules["found"]["key"] == "QXT" and rules["error"] is None
    assert rules["notes"] == ["'AIR' taken off 'AIR QXT': a mode or incoterm word is not part of the place"]
    for word in ("OCEAN", "FCL", "LCL", "TRUCK", "EXW", "FCA", "FOB", "CFR", "CIF", "CPT", "CIP", "DAP", "DPU", "DDP"):
        assert db.place_rules(repo.conn, word + " Puerto Alvero", "Chile")["found"]["key"] == "PUERTO ALVERO", word
    rules = db.place_rules(repo.conn, "exw fob: Puerto Alvero", "Chile")
    assert rules["found"]["key"] == "PUERTO ALVERO" and len(rules["notes"]) == 2
    # a word alone is not taken off: there is nothing after it
    assert db.place_rules(repo.conn, "AIR", "Taiwan")["text"] == "AIR"


def test_sea_is_seattles_code_and_is_left_as_it_is(repo):
    for text in ("SEA", "SEA/TAC"):
        rules = db.place_rules(repo.conn, text, "USA")
        assert (rules["text"], rules["notes"], rules["error"]) == (text, [], None)


def test_a_country_is_refused(repo):
    for text, country in (("USA", "USA"), ("chile", "Unknown"), ("United States", "USA"), ("AIR Taiwan", "Taiwan")):
        rules = db.place_rules(repo.conn, text, country)
        assert rules["error"].endswith("This is a country: leave the location empty and set the country."), text
    # where the city is the country, it is a place
    assert db.place_rules(repo.conn, "Singapore", "Singapore")["error"] is None


def test_a_us_or_canadian_city_is_name_comma_state(repo):
    rules = db.place_rules(repo.conn, "Ashby Crossing il", "USA")
    assert (rules["text"], rules["error"]) == ("Ashby Crossing, IL", None)
    assert rules["notes"] == ["'ASHBY CROSSING IL' written 'ASHBY CROSSING, IL': a US city is NAME, ST"]
    assert db.place_rules(repo.conn, "Merridale OH", "USA")["found"]["key"] == "MERRIDALE, OH"
    assert db.place_rules(repo.conn, "Ashby Crossing, il", "USA")["text"] == "Ashby Crossing, IL"
    assert db.place_rules(repo.conn, "Pellham Ridge ON", "Canada")["text"] == "Pellham Ridge, ON"
    for text, country in (("Ashby Crossing", "USA"), ("Ashby Crossing, XQ", "USA"), ("Pellham Ridge", "Canada"),
                          ("Pellham Ridge TX", "Canada")):
        assert "city without its" in db.place_rules(repo.conn, text, country)["error"], text
    assert "write it NAME, ST (SPRINGFIELD, IL)" in db.place_rules(repo.conn, "Ashby Crossing", "USA")["error"]
    # not a city, or not in the US / Canada: no state asked
    assert db.place_rules(repo.conn, "Ashby Crossing", "USA", "seaport")["error"] is None
    assert db.place_rules(repo.conn, "Ashby Crossing", "Unknown")["error"] is None
    # a glued word first, then the state
    rules = db.place_rules(repo.conn, "EXW Ashby Crossing", "USA")
    assert rules["notes"][0].startswith("'EXW' taken off") and "without its state" in rules["error"]


def test_every_creator_goes_through_the_rules_and_old_rows_stay(repo):
    with pytest.raises(ValueError, match="This is a country"):
        repo.add_master_location("Chile", "Chile")
    with pytest.raises(ValueError, match="without its state"):
        repo.add_location("Ashby Crossing", country="USA")      # the intake path
    row = repo.add_master_location("TRUCK Ashby Crossing il", "USA", status="to_confirm", source="intake",
                                   note="Added when registering a case from a mail subject.")
    assert (row["key"], row["name"]) == ("ASHBY CROSSING, IL", "Ashby Crossing, IL")
    assert row["note"].startswith("Added when registering a case from a mail subject.; 'TRUCK' taken off")
    assert repo.add_location("AIR QXT", country="Taiwan") == "QXT"
    assert len(repo.place_notes) == 3
    old = repo.conn.execute("SELECT * FROM master_location WHERE key = 'OLDHAVEN IL'").fetchone()
    assert (old["name"], old["status"]) == ("OLDHAVEN IL", "to_confirm")
    # a text already on file, even one breaking the rules, is that row
    assert repo.add_master_location("Oldhaven IL", "USA")["id"] == old["id"]


# ------------------------------------------------------------ the pages --
def new_place(**extra):
    values = {"key": "", "name": "", "type": "city", "country": "USA", "status": "active",
              "parent_key": "", "area_key": "", "note": ""}
    values.update(extra)
    return values


def test_the_locations_page_refuses_in_the_form(world):
    client = world["client"]
    r = client.post("/locations/new", data=new_place(key="Ashby Crossing"))
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and "ASHBY CROSSING" not in keys(world["path"])
    assert "is a US city without its state" in page and 'value="Ashby Crossing"' in page
    r = client.post("/locations/new", data=new_place(key="Chile", country="Chile"))
    assert "This is a country: leave the location empty and set the country" in r.get_data(as_text=True)
    assert "CHILE" not in keys(world["path"])
    r = client.post("/locations/new", data=new_place(key="FCL Ashby Crossing il"), follow_redirects=True)
    page = r.get_data(as_text=True)
    assert "ASHBY CROSSING, IL" in keys(world["path"]) and "FCL ASHBY CROSSING IL" not in keys(world["path"])
    assert "&#39;FCL&#39; taken off" in page and "a US city is NAME, ST" in page


def register(client, agent, **extra):
    values = {"received_on": TODAY, "requester_party_id": str(agent), "service_mode": "FCL",
              "container_type": "40HQ", "container_qty": "1", "origin_country": "USA",
              "dest_country": "Chile", "commodity": "Garden benches", "decision": "new",
              "origin_loc": "MERRIDALE, OH", "dest_loc": "PUERTO ALVERO"}
    values.update(extra)
    return client.post("/enquiries/new", data=values, follow_redirects=True)


def test_registering_refuses_a_country_and_a_city_without_its_state(world):
    before = keys(world["path"])
    r = register(world["client"], world["agent"], origin_loc="USA")
    assert r.status_code == 400 and "This is a country" in r.get_data(as_text=True)
    r = register(world["client"], world["agent"], origin_loc="EXW Ashby Crossing")
    page = r.get_data(as_text=True)
    assert r.status_code == 400 and "without its state" in page and "&#39;EXW&#39; taken off" in page
    assert keys(world["path"]) == before
    conn = db.open_db(world["path"])
    assert conn.execute("SELECT COUNT(*) FROM enquiry").fetchone()[0] == 0
    conn.close()


def test_registering_takes_the_word_off_and_writes_the_state(world):
    r = register(world["client"], world["agent"], origin_loc="EXW Ashby Crossing il",
                 dest_country="Taiwan", dest_loc="AIR QXT")
    assert r.status_code == 200
    page = r.get_data(as_text=True)
    conn = db.open_db(world["path"])
    case = conn.execute("SELECT origin_loc, dest_loc FROM enquiry WHERE opp_no = 'OPP-2001'").fetchone()
    conn.close()
    assert tuple(case) == ("ASHBY CROSSING, IL", "QXT")
    assert not {"AIR QXT", "EXW ASHBY CROSSING IL", "ASHBY CROSSING IL"} & keys(world["path"])
    assert "&#39;AIR&#39; taken off" in page and "&#39;EXW&#39; taken off" in page


def test_a_pickers_add_form_follows_the_rules(world):
    import json
    from opplist import places

    conn = db.open_db(world["path"])
    form = {"dest_loc": "AIR QXT",
            "dest_loc_new": json.dumps([{"key": "AIR QXT", "name": "AIR QXT", "country": "Taiwan"}])}
    errors = {}
    got = places.parse(form, "dest_loc", "Dest. location", errors, 4, conn=conn, country="Taiwan")
    assert (errors, got["keys"], got["new"]) == ({}, ["QXT"], [])
    assert got["notes"][0].startswith("'AIR' taken off")
    form = {"dest_loc": "Ashby Crossing",
            "dest_loc_new": json.dumps([{"key": "Ashby Crossing", "name": "", "country": ""}])}
    places.parse(form, "dest_loc", "Dest. location", errors, 4, conn=conn, country="USA")
    assert "without its state" in errors["dest_loc"]
    errors = {}
    form = {"dest_loc": "Chile", "dest_loc_new": json.dumps([{"key": "Chile", "country": "Chile"}])}
    places.parse(form, "dest_loc", "Dest. location", errors, 4, conn=conn)
    assert "This is a country" in errors["dest_loc"]
    errors = {}
    form = {"dest_loc": "Ashby Crossing il",
            "dest_loc_new": json.dumps([{"key": "Ashby Crossing il", "name": "Ashby Crossing il", "country": "USA"}])}
    got = places.parse(form, "dest_loc", "Dest. location", errors, 4, conn=conn)
    assert errors == {} and got["keys"] == ["ASHBY CROSSING, IL"]
    assert (got["new"][0]["key"], got["new"][0]["written"], got["new"][0]["name"]) == \
        ("ASHBY CROSSING, IL", "ASHBY CROSSING, IL", "")
    conn.close()


def test_add_location_inline_writes_master_and_the_case_gets_the_id(world):
    """The register page's inline 'Add location...' (replaces the M2 test of
    the same name): the place is created To confirm, the accented spelling
    kept as an alias, the case points at it; an alias as the destination
    lands on its place; a country typed there is refused, nothing saved."""
    conn = db.open_db(world["path"])
    repo = db.Repository(conn, "test")
    repo.add_master_alias("MRD", conn.execute("SELECT id FROM master_location WHERE key = 'MERRIDALE, OH'")
                          .fetchone()[0], "code")
    repo.commit()
    conn.close()
    r = register(world["client"], world["agent"], origin_country="USA", origin_loc="", new_origin_loc="USA",
                 dest_country="USA", dest_loc="MRD")
    assert r.status_code == 400 and "This is a country" in r.get_data(as_text=True)
    assert "USA" not in keys(world["path"])
    register(world["client"], world["agent"], origin_country="Chile", origin_loc="",
             new_origin_loc="  Peñasco Álamo ", dest_country="USA", dest_loc="MRD")
    conn = db.open_db(world["path"])
    row = conn.execute("SELECT * FROM master_location WHERE key = 'PENASCO ALAMO'").fetchone()
    assert (row["status"], row["source"], row["type"], row["country"]) == ("to_confirm", "intake", "city", "Chile")
    assert conn.execute("SELECT alias FROM master_location_alias WHERE location_id = ?",
                        (row["id"],)).fetchone()[0] == "PEÑASCO ÁLAMO"
    case = conn.execute("SELECT * FROM enquiry WHERE opp_no = 'OPP-2001'").fetchone()
    assert (case["origin_loc"], case["origin_loc_id"]) == ("PENASCO ALAMO", row["id"])
    assert (case["dest_loc"], case["dest_loc_id"]) == (
        "MERRIDALE, OH", conn.execute("SELECT id FROM master_location WHERE key = 'MERRIDALE, OH'").fetchone()[0])
    conn.close()

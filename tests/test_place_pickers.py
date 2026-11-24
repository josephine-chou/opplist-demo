"""Batch 23 (schema v39): place pickers, a Dest. location and a Port of
discharge of up to four places ('or'), the Port of discharge from the master
(Mark as a port, Air's Airport of destination), Packing from a Settings list,
a route added to a closed case following the case, and the v39 upgrade with
its lists. What the server renders and writes; the pickers' typing, keys and
lists in the browser are in tests/test_place_pickers_browser.py.

Every company, case, place, code, number, date and mail here is invented;
cases from OPP-3301."""

from __future__ import annotations

import json
import re

import pytest

from conftest import add_party
from opplist import (cargo, cargo_card, db, export_csv, export_xlsx, intake, migrate_master, outlook_bridge,
                     packing, parsing, paths, places, routes, schema_migrations)
from opplist.web import create_app

TODAY = "2026-11-20"


def build_world(repo):
    """A small master: invented places in three countries, with the flags
    the pickers read (port, airport, both, neither), one To confirm, one
    retired, aliases and codes."""
    conn = repo.conn
    for name, region, iso2 in (("USA", "North America", "US"), ("Chile", "Latin America", "CL"),
                               ("Peru", "Latin America", "PE"), ("Unknown", "Undefined", None)):
        repo.insert("country", {"name": name, "region": region, "iso2": iso2})
    add = repo.add_master_location
    add("WILLOW BEND, TX", "USA", "city", name="Willow Bend", subdivision="TX", source="phase1")
    add("PORT ALDER", "Chile", "city", name="Port Alder", unlocode="CLZPA", is_seaport=1, source="phase1")
    add("QUILLMOUTH", "Chile", "city", name="Quillmouth", unlocode="CLZQM", is_seaport=1, source="phase1")
    add("BRAMBLE BAY", "Chile", "city", name="Bramble Bay", is_seaport=1, is_airport=1, source="phase1")
    add("FENWICK", "Chile", "city", name="Fenwick", source="phase1")
    add("CRESTON QUAY", "Chile", "city", name="Creston Quay", source="phase1")
    add("ALDER FIELD", "Chile", "airport", name="Alder Field", iata="QZA", is_airport=1, source="phase1")
    add("PORT HOLLIS", "Peru", "city", name="Port Hollis", is_seaport=1, source="phase1")
    add("MARROW POINT", "Chile", "city", name="Marrow Point", is_seaport=1, status="to_confirm", source="intake")
    add("OLD WHARF", "Chile", "city", name="Old Wharf", is_seaport=1, status="retired", source="phase1")
    repo.add_master_alias("ALDER HARBOUR", conn.execute("SELECT id FROM master_location WHERE key = 'PORT ALDER'")
                          .fetchone()[0], "manual")
    conn.execute("ALTER TABLE location RENAME TO location_old")
    conn.execute(migrate_master.LOCATION_VIEW_SQL)
    agent = add_party(repo, "Tidewell Forwarding", country="Chile")
    repo.insert("party_domain", {"domain": "tidewell.example", "party_id": agent})
    add_party(repo, "(TBC)", ptype="Unknown", country="Unknown", placeholder=1)
    db.set_setting(conn, "next_opp_number", 3320)
    return agent


def loc(conn, key):
    return conn.execute("SELECT id FROM master_location WHERE key = ?", (key,)).fetchone()[0]


def new_case(repo, opp, agent, **extra):
    values = {"opp_no": opp, "requester_party_id": agent, "received_on": "2026-11-02", "status": "New",
              "origin_loc": "WILLOW BEND, TX", "origin_country": "USA", "dest_loc": "QUILLMOUTH",
              "dest_country": "Chile", "service_mode": "FCL", "incoterm": "FOB"}
    values.update(extra)
    case = repo.insert("enquiry", values)
    repo.insert("status_event", {"enquiry_id": case, "at": values["received_on"], "status": "New"})
    return case


def route_a(conn, case):
    return conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ? AND seq = 1", (case,)).fetchone()[0]


def place_keys(conn, route_id, side):
    return [r[0] for r in conn.execute(
        "SELECT l.key FROM enquiry_route_place p JOIN master_location l ON l.id = p.location_id "
        "WHERE p.route_id = ? AND p.side = ? AND p.voided = 0 ORDER BY p.seq", (route_id, side))]


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "pickers.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    agent = build_world(repo)
    ids = {"agent": agent}
    ids["open"] = new_case(repo, "OPP-3301", agent)
    ids["a"] = route_a(conn, ids["open"])
    repo.commit()
    conn.close()
    import opplist.web as web_module
    monkeypatch.setattr(web_module, "_today", lambda: TODAY)
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, ids


def page(client, case):
    return client.get("/enquiries/{}".format(case)).get_data(as_text=True)


def save_route(client, case, route_id=None, **fields):
    data = {"form": "route", "route_id": route_id or "", "origin_country": "USA", "origin_loc": "WILLOW BEND, TX",
            "dest_country": "Chile", "service_mode": "FCL", "incoterm": "FOB"}
    data.update(fields)
    return client.post("/enquiries/{}/routes".format(case), data=data, headers={"X-Save-First": "1"})


def place_data(body):
    found = re.search(r'<script type="application/json" id="place-data">(.*?)</script>', body, re.S)
    return json.loads(found.group(1))


# ------------------------------------------------------------- the pickers --
def test_the_page_lists_every_live_place_with_what_the_search_and_the_rows_read(world):
    client, path, ids = world
    for body in (page(client, ids["open"]), client.get("/enquiries/new").get_data(as_text=True)):
        data = place_data(body)
        by_key = {p["key"]: p for p in data["places"]}
        assert "OLD WHARF" not in by_key                                     # retired: left out
        assert by_key["MARROW POINT"]["status"] == "to_confirm"              # To confirm: marked
        alder = by_key["PORT ALDER"]
        assert (alder["name"], alder["country"], alder["unlocode"], alder["port"], alder["airport"]) == \
            ("Port Alder", "Chile", "CLZPA", 1, 0)
        assert "ALDER HARBOUR" in alder["aliases"]
        assert by_key["ALDER FIELD"]["iata"] == "QZA" and by_key["ALDER FIELD"]["airport"] == 1
        assert data["max"] == 4 and "Peru" in data["countries"]
        # 1f: never inside a <label>; the old dropdown and its search box are gone
        assert 'class="f pk-f" data-pk-field="dest_loc"' in body and 'id="dest_loc_search"' not in body
        for picker in re.findall(r'<div class="f pk-f".*?</div>\s*</div>', body, re.S):
            assert "<label" not in picker
    # /api/locations is unchanged (its areas, its fields)
    conn = db.open_db(path)
    repo = db.Repository(conn, "test")
    area = repo.add_master_area("ALDER COAST", "Alder Coast", "CL")
    repo.update("master_location", loc(conn, "PORT ALDER"), {"area_id": area})
    conn.commit()
    conn.close()
    api = client.get("/api/locations?country=Chile").get_json()
    assert next(r for r in api if r["key"] == "PORT ALDER")["area"] == "ALDER COAST"


def test_each_place_says_what_it_is_from_its_flags():
    assert places.kind({"is_seaport": 1, "is_airport": 0, "type": "city"}) == "port"
    assert places.kind({"is_seaport": 0, "is_airport": 1, "type": "airport"}) == "airport"
    assert places.kind({"is_seaport": 1, "is_airport": 1, "type": "city"}) == "port, airport"
    assert places.kind({"is_seaport": 0, "is_airport": 0, "type": "city"}) == "city"


def test_two_destinations_are_one_route_shown_with_or_and_the_first_is_dest_loc(world):
    client, path, ids = world
    res = save_route(client, ids["open"], ids["a"], dest_loc="QUILLMOUTH|BRAMBLE BAY")
    assert res.get_json()["ok"]
    conn = db.open_db(path)
    assert place_keys(conn, ids["a"], "dest") == ["QUILLMOUTH", "BRAMBLE BAY"]
    route = conn.execute("SELECT dest_loc, dest_loc_id FROM enquiry_route WHERE id = ?", (ids["a"],)).fetchone()
    assert tuple(route) == ("QUILLMOUTH", loc(conn, "QUILLMOUTH"))
    case = conn.execute("SELECT dest_loc, dest_loc_id FROM v_enquiry WHERE id = ?", (ids["open"],)).fetchone()
    assert tuple(case) == ("QUILLMOUTH", loc(conn, "QUILLMOUTH"))
    assert conn.execute("SELECT COUNT(*) FROM enquiry_route WHERE enquiry_id = ? AND voided = 0",
                        (ids["open"],)).fetchone()[0] == 1                   # one route, one quote
    assert conn.execute("SELECT COUNT(*) FROM change_log WHERE table_name = 'enquiry_route_place' "
                        "AND field = '(insert)'").fetchone()[0] >= 2
    conn.close()
    body = page(client, ids["open"])
    row = body[body.index('id="route-row-{}"'.format(ids["a"])):]
    row = row[:row.index("</tr>")]
    assert re.search(r'to <b>Quillmouth</b> <span class="r-or">or</span> <b>Bramble Bay</b>, Chile', row)
    assert "Willow Bend, TX to Quillmouth or Bramble Bay" in body          # the header


def test_a_destination_takes_four_places_at_most_and_the_order_is_kept(world):
    client, path, ids = world
    res = save_route(client, ids["open"], ids["a"], dest_loc="QUILLMOUTH|PORT ALDER|BRAMBLE BAY|FENWICK|CRESTON QUAY")
    assert res.status_code == 422 and "4 places at most" in json.dumps(res.get_json())
    assert save_route(client, ids["open"], ids["a"], dest_loc="FENWICK|PORT ALDER|QUILLMOUTH").get_json()["ok"]
    conn = db.open_db(path)
    assert place_keys(conn, ids["a"], "dest") == ["FENWICK", "PORT ALDER", "QUILLMOUTH"]
    conn.close()
    # one taken off: struck out, never deleted; the first follows
    assert save_route(client, ids["open"], ids["a"], dest_loc="PORT ALDER").get_json()["ok"]
    conn = db.open_db(path)
    assert place_keys(conn, ids["a"], "dest") == ["PORT ALDER"]
    assert conn.execute("SELECT COUNT(*) FROM enquiry_route_place WHERE route_id = ? AND voided = 1",
                        (ids["a"],)).fetchone()[0] == 2
    assert conn.execute("SELECT dest_loc FROM enquiry WHERE id = ?", (ids["open"],)).fetchone()[0] == "PORT ALDER"
    conn.close()


def test_the_port_of_discharge_is_places_with_a_readable_copy(world):
    client, path, ids = world
    assert save_route(client, ids["open"], ids["a"], dest_loc="FENWICK", pod="PORT ALDER|QUILLMOUTH").get_json()["ok"]
    conn = db.open_db(path)
    assert place_keys(conn, ids["a"], "pod") == ["PORT ALDER", "QUILLMOUTH"]
    assert conn.execute("SELECT pod FROM enquiry_route WHERE id = ?", (ids["a"],)).fetchone()[0] == \
        "PORT ALDER / QUILLMOUTH"
    conn.close()
    body = page(client, ids["open"])
    row = body[body.index('id="route-row-{}"'.format(ids["a"])):]
    assert "<td>Port Alder or Quillmouth</td>" in row[:row.index("</tr>")]


def test_the_readers_of_dest_loc_keep_the_first_place_and_pod_its_copy(world, tmp_path):
    client, path, ids = world
    save_route(client, ids["open"], ids["a"], dest_loc="BRAMBLE BAY|QUILLMOUTH", pod="PORT ALDER|QUILLMOUTH")
    conn = db.open_db(path)
    assert conn.execute("SELECT dest_loc FROM v_enquiry WHERE id = ?", (ids["open"],)).fetchone()[0] == "BRAMBLE BAY"
    live = routes.routes_of(conn, ids["open"])
    assert [p["key"] for p in live[0]["dest_places"]] == ["BRAMBLE BAY", "QUILLMOUTH"]
    assert routes.place(live[0], "dest") == "BRAMBLE BAY or QUILLMOUTH"
    assert routes.pod_text(live[0]) == "PORT ALDER or QUILLMOUTH"
    # opp-index.csv (8 columns) and the workbook keep the first place; Routes reads the pod copy
    row = next(r for r in export_csv.opp_index_rows(conn) if r[0] == "OPP-3301")
    assert len(row) == 8 and row[5] == "BRAMBLE BAY"
    opp = next(r for r in export_xlsx.opportunities_rows(conn) if r[0] == "OPP-3301")
    assert len(opp) == 41 and opp[export_xlsx.OPPORTUNITY_COLUMNS.index("Dest. Detail")] == "BRAMBLE BAY"
    out = export_xlsx.export(conn, tmp_path / "out" / "export.xlsx", db_path=str(path))
    from openpyxl import load_workbook
    book = load_workbook(out, read_only=True)
    sheet = [r for r in book["Routes"].iter_rows(values_only=True)]
    mine = [r for r in sheet[1:] if r[0] == "OPP-3301"]
    assert [(r[6], r[11]) for r in mine] == [("BRAMBLE BAY", "PORT ALDER / QUILLMOUTH")]
    book.close()
    conn.close()


def test_a_route_holding_text_that_names_no_place_keeps_it(world):
    client, path, ids = world
    conn = db.open_db(path)
    repo = db.Repository(conn, "test")
    repo.update("enquiry_route", ids["a"], {"pod": "Somewhere Upriver"})
    conn.commit()
    conn.close()
    body = page(client, ids["open"])
    editor = body[body.index('id="r{}-form"'.format(ids["a"])):]
    editor = editor[:editor.index("</form>")]
    assert 'name="pod" value="Somewhere Upriver"' in editor
    assert '<span class="pk-chip legacy"><b>Somewhere Upriver</b></span>' in editor
    assert save_route(client, ids["open"], ids["a"], dest_loc="QUILLMOUTH", pod="Somewhere Upriver").get_json()["ok"]
    conn = db.open_db(path)
    assert conn.execute("SELECT pod FROM enquiry_route WHERE id = ?", (ids["a"],)).fetchone()[0] == "Somewhere Upriver"
    assert place_keys(conn, ids["a"], "pod") == []
    conn.close()
    # beside a place it is refused (pick it from the list or add it)
    res = save_route(client, ids["open"], ids["a"], dest_loc="QUILLMOUTH", pod="Somewhere Upriver|PORT ALDER")
    assert res.status_code == 422 and "Somewhere Upriver is not in the location list" in json.dumps(res.get_json())


def test_add_location_creates_a_to_confirm_place_when_the_route_is_saved(world):
    client, path, ids = world
    new = [{"key": "DUNMÓRROW", "name": "Dunmórrow", "country": "Chile", "port": 1, "airport": 0},
           {"key": "TAKEN OFF AGAIN", "name": "Taken Off Again", "country": "Chile", "port": 0, "airport": 0}]
    res = save_route(client, ids["open"], ids["a"], dest_loc="QUILLMOUTH|DUNMÓRROW", dest_loc_new=json.dumps(new))
    assert res.get_json()["ok"]
    conn = db.open_db(path)
    row = conn.execute("SELECT * FROM master_location WHERE key = 'DUNMORROW'").fetchone()
    assert (row["status"], row["source"], row["type"], row["country"], row["is_seaport"], row["is_airport"]) == \
        ("to_confirm", "intake", "city", "Chile", 1, 0)
    assert row["name"] == "Dunmórrow"
    assert conn.execute("SELECT alias FROM master_location_alias WHERE location_id = ?",
                        (row["id"],)).fetchone()[0] == "DUNMÓRROW"          # as typed, like a place from a mail
    assert conn.execute("SELECT 1 FROM master_location WHERE key = 'TAKEN OFF AGAIN'").fetchone() is None
    assert place_keys(conn, ids["a"], "dest") == ["QUILLMOUTH", "DUNMORROW"]
    conn.close()
    # a two-letter key is refused, nothing written
    res = save_route(client, ids["open"], ids["a"], dest_loc="ZQ",
                     dest_loc_new=json.dumps([{"key": "ZQ", "name": "Zq", "country": "Chile"}]))
    assert res.status_code == 422 and "two-letter code" in json.dumps(res.get_json())


def test_mark_as_a_port_sets_the_flag_on_the_master_when_the_route_is_saved(world):
    client, path, ids = world
    mark = [{"key": "CRESTON QUAY", "kind": "port"}]
    assert save_route(client, ids["open"], ids["a"], dest_loc="FENWICK", pod="CRESTON QUAY",
                      pod_mark=json.dumps(mark)).get_json()["ok"]
    conn = db.open_db(path)
    row = conn.execute("SELECT is_seaport, is_airport FROM master_location WHERE key = 'CRESTON QUAY'").fetchone()
    assert tuple(row) == (1, 0)
    assert conn.execute("SELECT new_value FROM change_log WHERE table_name = 'master_location' AND field = 'is_seaport' "
                        "AND row_id = ?", (loc(conn, "CRESTON QUAY"),)).fetchone()[0] == "1"
    assert place_keys(conn, ids["a"], "pod") == ["CRESTON QUAY"]
    conn.close()
    # Air: the box is the Airport of destination, the mark sets the airport flag
    assert save_route(client, ids["open"], ids["a"], dest_loc="FENWICK", service_mode="Air", pod="FENWICK",
                      pod_mark=json.dumps([{"key": "FENWICK", "kind": "airport"}])).get_json()["ok"]
    conn = db.open_db(path)
    assert conn.execute("SELECT is_airport FROM master_location WHERE key = 'FENWICK'").fetchone()[0] == 1
    conn.close()
    body = page(client, ids["open"])
    editor = body[body.index('id="r{}-form"'.format(ids["a"])):]
    editor = editor[:editor.index("</form>")]
    assert '<span class="pk-label" data-sea="Port of discharge" data-air="Airport of destination">' \
           'Airport of destination</span>' in editor


def test_merging_or_renaming_a_place_moves_the_routes_places(world):
    client, path, ids = world
    save_route(client, ids["open"], ids["a"], dest_loc="QUILLMOUTH|FENWICK", pod="PORT ALDER|CRESTON QUAY")
    conn = db.open_db(path)
    repo = db.Repository(conn, "test")
    b = repo.add_route(ids["open"], {"origin_loc": "WILLOW BEND, TX", "origin_country": "USA",
                                     "dest_places": [loc(conn, "FENWICK"), loc(conn, "QUILLMOUTH")],
                                     "dest_country": "Chile", "pod_places": [loc(conn, "CRESTON QUAY")]})
    repo.merge_master_location(loc(conn, "QUILLMOUTH"), loc(conn, "FENWICK"), today=TODAY)
    conn.commit()
    assert place_keys(conn, ids["a"], "dest") == ["QUILLMOUTH"]
    assert place_keys(conn, b, "dest") == ["QUILLMOUTH"]
    assert conn.execute("SELECT dest_loc FROM enquiry_route WHERE id = ?", (b,)).fetchone()[0] == "QUILLMOUTH"
    conn.close()
    # a new key on the Locations page: the readable copy follows
    client.post("/locations/{}".format(loc(db.open_db(path), "CRESTON QUAY")),
                data={"key": "CRESTON NEW QUAY", "name": "Creston New Quay", "type": "city", "country": "Chile",
                      "status": "active"})
    conn = db.open_db(path)
    assert conn.execute("SELECT pod FROM enquiry_route WHERE id = ?", (ids["a"],)).fetchone()[0] == \
        "PORT ALDER / CRESTON NEW QUAY"
    assert conn.execute("SELECT pod FROM enquiry_route WHERE id = ?", (b,)).fetchone()[0] == "CRESTON NEW QUAY"
    conn.close()


# --------------------------------------------- 7. a route follows its case --
def closed_world(repo, agent):
    conn = repo.conn
    out = {}
    out["won"] = new_case(repo, "OPP-3302", agent)
    repo.set_status(out["won"], "Quoted", "2026-11-04")
    repo.set_status(out["won"], "Won", "2026-11-09", rate_used="own")
    out["lost"] = new_case(repo, "OPP-3303", agent)
    repo.set_status(out["lost"], "Lost", "2026-11-10", closed_reason="Price too high")
    out["cancelled"] = new_case(repo, "OPP-3304", agent)
    repo.set_status(out["cancelled"], "Cancelled", "2026-11-11", closed_reason="Shipment cancelled")
    out["dropped"] = new_case(repo, "OPP-3305", agent)
    repo.set_status(out["dropped"], "Dropped", "2026-11-12", closed_reason="Registered in error")
    # a later note is not the closing event
    repo.add_note(out["lost"], "2026-11-14", "they wrote again")
    return out


def test_a_route_added_to_a_closed_case_starts_with_the_case_result(tmp_path):
    conn = db.open_db(tmp_path / "follow.db")
    repo = db.Repository(conn, "test")
    agent = build_world(repo)
    cases = closed_world(repo, agent)
    lane = {"origin_loc": "WILLOW BEND, TX", "origin_country": "USA", "dest_loc": "PORT ALDER", "dest_country": "Chile"}
    got = {}
    for name, case in cases.items():
        route_id = repo.add_route(case, dict(lane))
        got[name] = tuple(conn.execute("SELECT result, lost_reason, result_on FROM enquiry_route WHERE id = ?",
                                       (route_id,)).fetchone())
    assert got["won"] == ("Won", None, "2026-11-09")
    assert got["lost"] == ("Lost", "Price too high", "2026-11-10")
    assert got["cancelled"] == ("Cancelled", "Shipment cancelled", "2026-11-11")
    assert got["dropped"] == (None, None, None)
    # the cases do not move
    assert [conn.execute("SELECT status FROM enquiry WHERE id = ?", (cases[n],)).fetchone()[0]
            for n in ("won", "lost", "cancelled", "dropped")] == ["Won", "Lost", "Cancelled", "Dropped"]
    # an open case's new route has no result; a result given as empty is kept empty
    open_case = new_case(repo, "OPP-3306", agent)
    assert conn.execute("SELECT result FROM enquiry_route WHERE id = ?",
                        (repo.add_route(open_case, dict(lane)),)).fetchone()[0] is None
    empty = repo.add_route(cases["lost"], dict(lane, result=None))
    assert conn.execute("SELECT result FROM enquiry_route WHERE id = ?", (empty,)).fetchone()[0] is None
    conn.close()


def test_the_route_editor_starts_a_new_route_of_a_closed_case_with_its_result(tmp_path, monkeypatch):
    path = tmp_path / "follow-web.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, "test")
    agent = build_world(repo)
    cases = closed_world(repo, agent)
    repo.commit()
    conn.close()
    import opplist.web as web_module
    monkeypatch.setattr(web_module, "_today", lambda: TODAY)
    app = create_app(path)
    app.config["TESTING"] = True
    client = app.test_client()
    body = page(client, cases["cancelled"])
    editor = body[body.index('id="rnew-form"'):]
    editor = editor[:editor.index("</form>")]
    assert '<option value="Cancelled" selected>' in editor
    assert re.search(r'<option value="Shipment cancelled"\s+selected>', editor)
    # saved as it stands: the case's result, reason and closing date
    res = save_route(client, cases["cancelled"], dest_loc="PORT ALDER", result="Cancelled",
                     lost_reason="Shipment cancelled")
    assert res.get_json()["ok"]
    conn = db.open_db(path)
    assert tuple(conn.execute("SELECT result, lost_reason, result_on FROM enquiry_route WHERE enquiry_id = ? "
                              "AND seq = 2", (cases["cancelled"],)).fetchone()) == \
        ("Cancelled", "Shipment cancelled", "2026-11-11")
    conn.close()
    # she set this one to no result before saving: it has none
    assert save_route(client, cases["lost"], dest_loc="QUILLMOUTH", result="").get_json()["ok"]
    conn = db.open_db(path)
    assert conn.execute("SELECT result FROM enquiry_route WHERE enquiry_id = ? AND seq = 2",
                        (cases["lost"],)).fetchone()[0] is None
    conn.close()


# ----------------------------------------------------------------- packing --
def test_written_packing_words_map_to_the_list():
    expect = {"pallet": "Pallets", "plt": "Pallets", "PALLETS": "Pallets", "euro pallet": "Pallets",
              "skd": "Skids", "skid": "Skids", "case": "Cases", "wooden case": "Cases", "box": "Boxes",
              "ctn": "Cartons", "carton": "Cartons", "pkg": "Packages", "package": "Packages",
              "bultos": "Packages", "volumes": "Packages", "pc": "Pieces", "pcs": "Pieces", "piece": "Pieces",
              "crate": "Crates", "drum": "Drums", "bag": "Bags", "big bags": "Big bags", "roll": "Rolls",
              "bundle": "Bundles", "unit": "Units", "pallets in 3 x 40' containers": "Pallets"}
    for word, unit in expect.items():
        assert packing.unit_for(word) == unit, word
    for word in ("machinery", "plastic suitcase", "bulk piece", "carton on skid", ""):
        assert packing.unit_for(word) is None, word
    # her own list: a unit she took off maps to nothing
    assert packing.unit_for("pallet", ["Skids", "Crates"]) is None
    assert packing.unit_for("skid", ["Skids", "Crates"]) == "Skids"


def test_one_piece_reads_singular():
    assert packing.label(1, "Pallets") == "1 pallet"
    assert packing.label(1, "Boxes") == "1 box"
    assert packing.label(25, "Big bags") == "25 big bags"
    assert packing.label(1, "Big bags") == "1 big bag"
    assert packing.label(3, "Cases") == "3 cases"
    assert packing.label(1, "Units") == "1 unit"
    line = cargo.normalise({"id": 1, "pieces": 1, "pack_word": "plt", "pack_unit": "Pallets",
                            "weight_value": 410, "weight_unit": "kg"})
    assert cargo_card.row(line, 1, "LCL", [])["what"] == "1 pallet"
    word_only = cargo.normalise({"id": 2, "pieces": 4, "pack_word": "machinery"})
    assert cargo_card.row(word_only, 1, "LCL", [])["what"] == "4 machinery"


def test_a_cargo_line_keeps_the_word_and_stores_the_choice(world):
    client, path, ids = world
    conn = db.open_db(path)
    repo = db.Repository(conn, "test")
    line = repo.add_cargo_line(ids["open"], {"pieces": 6, "pack_word": "plts", "weight_value": 900, "weight_unit": "kg"})
    odd = repo.add_cargo_line(ids["open"], {"pieces": 2, "pack_word": "carton on skid"})
    conn.commit()
    assert tuple(conn.execute("SELECT pack_word, pack_unit FROM enquiry_cargo WHERE id = ?", (line,)).fetchone()) == \
        ("plts", "Pallets")
    assert tuple(conn.execute("SELECT pack_word, pack_unit FROM enquiry_cargo WHERE id = ?", (odd,)).fetchone()) == \
        ("carton on skid", None)
    conn.close()
    body = page(client, ids["open"])
    form = body[body.index('id="cargo-line-{}"'.format(odd)):]
    form = form[:form.index("</form>")]
    assert '<select name="pack_unit" data-f="pack_unit" class="pack-unit"' in form
    assert '<option value="">-</option>' in form and "<option>Big bags</option>" in form
    assert 'name="pack_word" data-f="pack_word" value="carton on skid"' in form
    # she picks Crates: the word stays
    res = client.post("/enquiries/{}/cargo".format(ids["open"]), headers={"X-Save-First": "1"},
                      data={"line_id": odd, "action": "save", "pieces": "2", "pack_word": "carton on skid",
                            "pack_unit": "Crates"})
    assert res.get_json()["ok"]
    conn = db.open_db(path)
    assert tuple(conn.execute("SELECT pack_word, pack_unit FROM enquiry_cargo WHERE id = ?", (odd,)).fetchone()) == \
        ("carton on skid", "Crates")
    conn.close()
    assert "2 crates" in page(client, ids["open"])


def test_the_packing_list_is_a_setting_edited_like_container_types(world):
    client, path, ids = world
    body = client.get("/settings").get_data(as_text=True)
    assert 'name="packing_units"' in body and "Big bags" in body
    form = {"packing_units": "Pallets\nSkids\nTotes"}
    client.post("/settings", data=form)
    conn = db.open_db(path)
    assert db.get_setting_list(conn, "packing_units") == ["Pallets", "Skids", "Totes"]
    assert packing.units_of(conn) == ["Pallets", "Skids", "Totes"]
    conn.close()


def test_reading_a_mail_maps_its_packing_word(world, monkeypatch):
    client, path, ids = world
    conn = db.open_db(path)
    mail = outlook_bridge.MailInfo(
        subject="Rate request WILLOW BEND - QUILLMOUTH", received_on="2026-11-03",
        sender_address="ana@tidewell.example", sender_name="Ana",
        body_excerpt="Hello,\n3 bultos 120 x 80 x 90 cm, 640 kg total\n2 crates 300 x 200 x 180 cm, 4100 kg\n")
    draft = intake.build(conn, mail)
    got = [(l.get("pack_word"), l.get("pack_unit")) for l in draft.cargo_lines]
    assert ("bultos", "Packages") in got and ("crates", "Crates") in got
    conn.close()
    # the register page: the dropdown shows the Packing, the word rides along as written
    monkeypatch.setattr(outlook_bridge, "read_selection", lambda: mail)
    body = client.post("/enquiries/from-outlook").get_data(as_text=True)
    line = body[body.index('name="cargo_0_pack_unit"'):]
    line = line[:line.index("</select>")]
    assert "<option selected>Packages</option>" in line
    assert 'name="cargo_0_pack_word" data-f="pack_word" value="bultos"' in body


# ---------------------------------------- 4. 'X or Y' read from the mail --
def mail(subject, body=""):
    return outlook_bridge.MailInfo(subject=subject, received_on="2026-11-03", sender_address="ana@tidewell.example",
                                   sender_name="Ana", body_excerpt=body)


def test_a_destination_written_x_or_y_is_one_route_with_both_places(world):
    client, path, ids = world
    conn = db.open_db(path)
    for subject, body in (("RFQ // WILLOW BEND - QUILLMOUTH OR BRAMBLE BAY // FCL 1X40HC", ""),
                          ("RFQ // WILLOW BEND - QUILLMOUTH / BRAMBLE BAY // FCL 1X40HC", ""),
                          ("Quotation request", "Hello,\nPOL: Willow Bend\nPOD: Quillmouth or Bramble Bay\n1x40HC\n"),
                          ("Quotation request", "Hello,\nPlease quote from Willow Bend to Quillmouth or Bramble Bay, "
                                                "1x40HC.\n")):
        draft = intake.build(conn, mail(subject, body))
        assert [(l["origin"], l["dest"], l.get("dest_more")) for l in draft.lanes] == \
            [("WILLOW BEND, TX", "QUILLMOUTH", ["BRAMBLE BAY"])], (subject, body)
        assert draft.values["dest_loc"] == "QUILLMOUTH" and draft.dest_more == ["BRAMBLE BAY"]
    # separate destinations, each its own quote, stay separate routes (13 item 17)
    for subject, body in (("Quotation request", "Hello,\nPOL: Willow Bend\nPOD: Quillmouth and Bramble Bay\n"),
                          ("RFQ // WILLOW BEND - QUILLMOUTH / DUNMORROW // FCL", "")):
        draft = intake.build(conn, mail(subject, body))
        assert len(draft.lanes) == 2 and not any(l.get("dest_more") for l in draft.lanes), (subject, body)
    assert parsing.written_as_alternatives("Quillmouth or Bramble Bay", ["QUILLMOUTH", "BRAMBLE BAY"])
    assert not parsing.written_as_alternatives("Quillmouth & Bramble Bay", ["QUILLMOUTH", "BRAMBLE BAY"])
    conn.close()


def test_a_state_written_after_a_comma_is_not_a_choice(world):
    # 'HARLOW, OR' is Oregon: the lane reads as before, one destination
    client, path, ids = world
    conn = db.open_db(path)
    repo = db.Repository(conn, "test")
    repo.add_master_location("HARLOW, OR", "USA", "city", name="Harlow", subdivision="OR", source="phase1")
    repo.commit()
    for subject, body, dest in (("RFQ // QUILLMOUTH - HARLOW, OR // FCL 1X40HC", "", "HARLOW, OR"),
                                ("Quotation request", "Hello,\nPOL: Quillmouth\nPOD: Harlow, OR USA\n1x40HC\n",
                                 "HARLOW, OR")):
        draft = intake.build(conn, mail(subject, body))
        assert [(l["dest"], l.get("dest_more")) for l in draft.lanes] == [(dest, None)], (subject, body)
        assert not draft.dest_more
    assert not parsing.written_as_alternatives("Harlow, OR USA", ["HARLOW", "USA"])
    assert parsing._endpoints("HARLOW, OR FCL") == ["HARLOW, OR FCL"]
    assert parsing._endpoints("QUILLMOUTH OR BRAMBLE BAY") == ["QUILLMOUTH", "BRAMBLE BAY"]
    conn.close()


def test_a_garbled_picker_post_is_refused_or_ignored_never_an_error(world):
    client, path, ids = world
    # a country that is not on file in the small form: the side's country is used
    new = [{"key": "GRIMSBY REACH", "name": "Grimsby Reach", "country": ["a"], "port": 1}]
    res = save_route(client, ids["open"], ids["a"], dest_loc="QUILLMOUTH|GRIMSBY REACH", dest_loc_new=json.dumps(new))
    assert res.status_code == 200 and res.get_json()["ok"]
    conn = db.open_db(path)
    assert conn.execute("SELECT country FROM master_location WHERE key = 'GRIMSBY REACH'").fetchone()[0] == "Chile"
    conn.close()
    new = [{"key": "WREN HOLLOW", "name": "Wren Hollow", "country": "<script>alert(1)</script>"}]
    res = save_route(client, ids["open"], ids["a"], dest_loc="WREN HOLLOW", dest_loc_new=json.dumps(new),
                     dest_country="")
    assert res.status_code == 200 and res.get_json()["ok"]
    conn = db.open_db(path)
    assert conn.execute("SELECT country FROM master_location WHERE key = 'WREN HOLLOW'").fetchone()[0] == "Unknown"
    conn.close()
    # a key longer than 80 characters is refused, nothing written
    new = [{"key": "Q" * 120, "name": "Long", "country": "Chile"}]
    res = save_route(client, ids["open"], ids["a"], dest_loc="Q" * 120, dest_loc_new=json.dumps(new))
    assert res.status_code == 422 and "80 characters" in json.dumps(res.get_json())
    conn = db.open_db(path)
    assert conn.execute("SELECT count(*) FROM master_location WHERE length(key) > 80").fetchone()[0] == 0
    conn.close()
    # a retired place is never offered: posted by hand it is not a pick on the case page
    res = save_route(client, ids["open"], ids["a"], dest_loc="QUILLMOUTH|OLD WHARF")
    assert res.status_code == 422 and "OLD WHARF is not in the location list" in json.dumps(res.get_json())
    # but a route that already holds one keeps it
    conn = db.open_db(path)
    repo = db.Repository(conn, "test")
    repo.edit_route(ids["a"], {"dest_places": [loc(conn, "OLD WHARF")]}, at=TODAY)
    repo.commit()
    conn.close()
    res = save_route(client, ids["open"], ids["a"], dest_loc="OLD WHARF", pod="PORT ALDER")
    assert res.status_code == 200 and res.get_json()["ok"]
    conn = db.open_db(path)
    assert place_keys(conn, ids["a"], "dest") == ["OLD WHARF"]
    conn.close()


def test_a_state_written_after_a_comma_is_not_a_choice(world):
    # 'HARLOW, OR' is Oregon: the lane reads as before, one destination
    client, path, ids = world
    conn = db.open_db(path)
    repo = db.Repository(conn, "test")
    repo.add_master_location("HARLOW, OR", "USA", "city", name="Harlow", subdivision="OR", source="phase1")
    repo.commit()
    for subject, body, dest in (("RFQ // QUILLMOUTH - HARLOW, OR // FCL 1X40HC", "", "HARLOW, OR"),
                                ("Quotation request", "Hello,\nPOL: Quillmouth\nPOD: Harlow, OR USA\n1x40HC\n",
                                 "HARLOW, OR")):
        draft = intake.build(conn, mail(subject, body))
        assert [(l["dest"], l.get("dest_more")) for l in draft.lanes] == [(dest, None)], (subject, body)
        assert not draft.dest_more
    assert not parsing.written_as_alternatives("Harlow, OR USA", ["HARLOW", "USA"])
    assert parsing._endpoints("HARLOW, OR FCL") == ["HARLOW, OR FCL"]
    assert parsing._endpoints("QUILLMOUTH OR BRAMBLE BAY") == ["QUILLMOUTH", "BRAMBLE BAY"]
    conn.close()


def test_a_garbled_picker_post_is_refused_or_ignored_never_an_error(world):
    client, path, ids = world
    # a country that is not on file in the small form: the side's country is used
    new = [{"key": "GRIMSBY REACH", "name": "Grimsby Reach", "country": ["a"], "port": 1}]
    res = save_route(client, ids["open"], ids["a"], dest_loc="QUILLMOUTH|GRIMSBY REACH", dest_loc_new=json.dumps(new))
    assert res.status_code == 200 and res.get_json()["ok"]
    conn = db.open_db(path)
    assert conn.execute("SELECT country FROM master_location WHERE key = 'GRIMSBY REACH'").fetchone()[0] == "Chile"
    conn.close()
    new = [{"key": "WREN HOLLOW", "name": "Wren Hollow", "country": "<script>alert(1)</script>"}]
    res = save_route(client, ids["open"], ids["a"], dest_loc="WREN HOLLOW", dest_loc_new=json.dumps(new),
                     dest_country="")
    assert res.status_code == 200 and res.get_json()["ok"]
    conn = db.open_db(path)
    assert conn.execute("SELECT country FROM master_location WHERE key = 'WREN HOLLOW'").fetchone()[0] == "Unknown"
    conn.close()
    # a key longer than 80 characters is refused, nothing written
    new = [{"key": "Q" * 120, "name": "Long", "country": "Chile"}]
    res = save_route(client, ids["open"], ids["a"], dest_loc="Q" * 120, dest_loc_new=json.dumps(new))
    assert res.status_code == 422 and "80 characters" in json.dumps(res.get_json())
    conn = db.open_db(path)
    assert conn.execute("SELECT count(*) FROM master_location WHERE length(key) > 80").fetchone()[0] == 0
    conn.close()
    # a retired place is never offered: posted by hand it is not a pick on the case page
    res = save_route(client, ids["open"], ids["a"], dest_loc="QUILLMOUTH|OLD WHARF")
    assert res.status_code == 422 and "OLD WHARF is not in the location list" in json.dumps(res.get_json())
    # but a route that already holds one keeps it
    conn = db.open_db(path)
    repo = db.Repository(conn, "test")
    repo.edit_route(ids["a"], {"dest_places": [loc(conn, "OLD WHARF")]}, at=TODAY)
    repo.commit()
    conn.close()
    res = save_route(client, ids["open"], ids["a"], dest_loc="OLD WHARF", pod="PORT ALDER")
    assert res.status_code == 200 and res.get_json()["ok"]
    conn = db.open_db(path)
    assert place_keys(conn, ids["a"], "dest") == ["OLD WHARF"]
    conn.close()


def test_registering_an_x_or_y_mail_makes_one_route_with_both_places(world, monkeypatch):
    client, path, ids = world
    monkeypatch.setattr(outlook_bridge, "read_selection", lambda: mail(
        "RFQ // WILLOW BEND - QUILLMOUTH OR BRAMBLE BAY // FCL 1X40HC", "Hello,\nPlease quote 1x40HC, FOB.\n"))
    body = client.post("/enquiries/from-outlook").get_data(as_text=True)
    assert '<input type="hidden" class="pk-value" name="dest_loc" value="QUILLMOUTH|BRAMBLE BAY">' in body
    assert "Willow Bend, TX to Quillmouth or Bramble Bay" in body or "WILLOW BEND, TX to QUILLMOUTH or BRAMBLE BAY" in body
    form = {"received_on": "2026-11-03", "requester_party_id": str(ids["agent"]), "service_mode": "FCL",
            "incoterm": "FOB", "origin_country": "USA", "origin_loc": "WILLOW BEND, TX", "dest_country": "Chile",
            "dest_loc": "QUILLMOUTH|BRAMBLE BAY", "pod": "", "decision": "new",
            "cargo_0_pieces": "1", "cargo_0_container_type": "40HQ", "cargo_0_container_qty": "1",
            "cargo_0_commodity": "garden tools"}
    res = client.post("/enquiries/new", data=form)
    assert res.status_code == 302, res.get_data(as_text=True)[:3000]
    conn = db.open_db(path)
    case = conn.execute("SELECT id, dest_loc FROM enquiry WHERE opp_no = 'OPP-3320'").fetchone()
    assert case["dest_loc"] == "QUILLMOUTH"
    live = routes.routes_of(conn, case["id"])
    assert len(live) == 1 and [p["key"] for p in live[0]["dest_places"]] == ["QUILLMOUTH", "BRAMBLE BAY"]
    conn.close()


def test_a_refused_registration_keeps_the_pickers_as_they_were(world):
    client, path, ids = world
    new = [{"key": "DUNMORROW", "name": "Dunmorrow", "country": "Chile", "port": 1, "airport": 0}]
    form = {"received_on": "2026-11-03", "requester_party_id": "", "service_mode": "FCL",
            "origin_country": "USA", "origin_loc": "WILLOW BEND, TX", "dest_country": "Chile",
            "dest_loc": "QUILLMOUTH|DUNMORROW", "dest_loc_new": json.dumps(new), "pod": "PORT ALDER|BRAMBLE BAY",
            "service_scope": "Door to port", "decision": "new"}
    res = client.post("/enquiries/new", data=form)
    assert res.status_code == 400
    body = res.get_data(as_text=True)
    assert 'name="dest_loc" value="QUILLMOUTH|DUNMORROW"' in body
    assert 'name="pod" value="PORT ALDER|BRAMBLE BAY"' in body
    assert "DUNMORROW" in body[body.index('name="dest_loc_new"'):][:300]
    conn = db.open_db(path)
    assert conn.execute("SELECT 1 FROM master_location WHERE key = 'DUNMORROW'").fetchone() is None
    conn.close()


# ------------------------------------------------------- 6. the upgrade --
def _back_to_v38(path):
    """The schema as v38 had it: no enquiry_route_place, enquiry_cargo
    without pack_unit (rebuilt: the column is the table's last, and SQLite's
    DROP COLUMN trips over the comments after it), no packing_units."""
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = OFF")
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    for name in re.findall(r"CREATE VIEW\s+(\w+)\s+AS", schema_sql):
        conn.execute("DROP VIEW IF EXISTS {}".format(name))
    conn.execute("DROP TABLE enquiry_route_place")
    sql = re.sub(r"--[^\n]*", "", conn.execute("SELECT sql FROM sqlite_master WHERE name = 'enquiry_cargo'")
                 .fetchone()[0])
    sql = re.sub(r",\s*pack_unit\s+TEXT\s*", "\n", sql).replace("CREATE TABLE enquiry_cargo", "CREATE TABLE cargo_v38", 1)
    conn.execute(sql)
    columns = [r[1] for r in conn.execute("PRAGMA table_info(cargo_v38)")]
    conn.execute("INSERT INTO cargo_v38 ({0}) SELECT {0} FROM enquiry_cargo".format(", ".join(columns)))
    conn.execute("DROP TABLE enquiry_cargo")
    conn.execute("ALTER TABLE cargo_v38 RENAME TO enquiry_cargo")
    conn.execute("CREATE INDEX ix_enquiry_cargo_enquiry ON enquiry_cargo (enquiry_id, seq)")
    for statement in re.findall(r"CREATE VIEW\s+\w+\s+AS.*?;", schema_sql, re.S):
        conn.execute(statement)
    conn.execute("DELETE FROM setting WHERE key = 'packing_units'")
    conn.execute("UPDATE setting SET value = '38' WHERE key = 'schema_version'")
    conn.commit()
    assert "pack_unit" not in [r[1] for r in conn.execute("PRAGMA table_info(enquiry_cargo)")]
    conn.close()


def test_the_upgrade_maps_packing_turns_pods_into_places_and_lists_the_rest(tmp_path):
    path = tmp_path / "v38.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, "test")
    agent = build_world(repo)
    words = ["pallet", "pallet", "plt", "bultos", "Big bags", "machinery", "carton on skid",
             "pallets in 3 x 40' containers"]
    case = new_case(repo, "OPP-3307", agent)
    for word in words:
        repo.add_cargo_line(case, {"pieces": 2, "pack_word": word})

    def pod_case(opp, pod, **extra):
        c = new_case(repo, opp, agent, **extra)
        repo.update("enquiry_route", route_a(conn, c), {"pod": pod})
        return c
    pod_case("OPP-3308", "PORT ALDER", dest_loc="FENWICK")              # a place: becomes it
    pod_case("OPP-3309", "alder harbour", dest_loc="FENWICK")           # its alias: the place
    pod_case("OPP-3310", "QUILLMOUTH OR PORT ALDER", dest_loc="FENWICK")  # kept, listed: OR, not a port
    pod_case("OPP-3311", "Creston Quay", dest_loc="FENWICK")            # a place that is no port: listed
    pod_case("OPP-3312", "PORT ALDER", dest_loc="PORT ALDER")           # the destination itself, a port
    pod_case("OPP-3313", "Nowhere Flats", dest_loc="FENWICK")           # no such place: kept, listed
    pod_case("OPP-3314", "ALDER FIELD", dest_loc="FENWICK", service_mode="Air")  # an airport on Air: fine
    repo.commit()
    conn.close()
    _back_to_v38(path)

    notes = []
    conn = db.open_db(path, upgrade_notes=notes)
    assert schema_migrations.current_version(conn) == schema_migrations.CURRENT_VERSION >= 39
    assert any(n.startswith("restore point opplist_before_schema{}_".format(schema_migrations.CURRENT_VERSION))
               for n in notes), notes
    text = "\n".join(notes)
    assert "Packing mapped to the list: 5 word(s), 6 line(s): 'pallet' 2 -> Pallets" in text
    assert "'plt' 1 -> Pallets" in text and "'bultos' 1 -> Packages" in text and "'Big bags' 1 -> Big bags" in text
    assert "'pallets in 3 x 40' containers' 1 -> Pallets" in text
    assert "Packing not mapped (the word kept, the dropdown empty): 2 word(s), 2 line(s): " \
           "'carton on skid' 1; 'machinery' 1" in text
    assert conn.execute("SELECT COUNT(*) FROM enquiry_cargo WHERE pack_word = 'machinery' AND pack_unit IS NULL"
                        ).fetchone()[0] == 1
    assert "Port of discharge turned into a place: 5 (OPP-3308 A 'PORT ALDER' -> PORT ALDER; " \
           "OPP-3309 A 'alder harbour' -> PORT ALDER; OPP-3311 A 'Creston Quay' -> CRESTON QUAY; " \
           "OPP-3312 A 'PORT ALDER' -> PORT ALDER; OPP-3314 A 'ALDER FIELD' -> ALDER FIELD)" in text
    assert "Port of discharge kept as text: 2 (OPP-3310 A 'QUILLMOUTH OR PORT ALDER'; OPP-3313 A 'Nowhere Flats')" in text
    assert "POD not a port: 3 (OPP-3310 A (QUILLMOUTH OR PORT ALDER: not a port); " \
           "OPP-3311 A (CRESTON QUAY: not a port); OPP-3313 A (Nowhere Flats: not a port))" in text
    assert "POD with OR: 1 (OPP-3310 A (QUILLMOUTH OR PORT ALDER))" in text
    assert "POD the same place as the Dest. location: 1 (OPP-3312 A (PORT ALDER))" in text
    assert "Dest. location a port with a POD as well: 1 (OPP-3312 A (PORT ALDER and POD PORT ALDER))" in text
    # every dest_loc_id is the first place now; the pod copy is the master key
    assert conn.execute("SELECT COUNT(*) FROM enquiry_route WHERE dest_loc_id IS NOT NULL").fetchone()[0] == \
        conn.execute("SELECT COUNT(*) FROM enquiry_route_place WHERE side = 'dest' AND seq = 1").fetchone()[0] == 8
    assert "dest_loc_id made the first place on 8 route(s)" in text
    a = route_a(conn, conn.execute("SELECT id FROM enquiry WHERE opp_no = 'OPP-3309'").fetchone()[0])
    assert conn.execute("SELECT pod FROM enquiry_route WHERE id = ?", (a,)).fetchone()[0] == "PORT ALDER"
    assert conn.execute("SELECT COUNT(*) FROM change_log WHERE source = 'migration' AND table_name = 'enquiry_route' "
                        "AND field = 'pod'").fetchone()[0] == 2                 # 'alder harbour', 'Creston Quay'
    assert "Open routes of Lost / Cancelled cases given the case's result: 0" in text
    # a second run finds nothing more to do
    conn.execute("UPDATE setting SET value = '38' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    again = []
    conn = db.open_db(path, upgrade_notes=again)
    text = "\n".join(again)
    assert "Port of discharge turned into a place: 0" in text and "dest_loc_id made the first place on 0" in text
    conn.close()


def test_the_upgrade_gives_open_routes_of_lost_and_cancelled_cases_the_case_result(tmp_path):
    path = tmp_path / "follow-v38.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, "test")
    agent = build_world(repo)
    cases = closed_world(repo, agent)
    lane = {"origin_loc": "WILLOW BEND, TX", "origin_country": "USA", "dest_loc": "PORT ALDER", "dest_country": "Chile"}
    # what a script did before this batch: a route added after the case closed, with no result
    added = {name: repo.add_route(case, dict(lane), follow_case=False) for name, case in cases.items()}
    # and one she already set by hand (no reason, her own date): left alone
    hand = repo.add_route(cases["lost"], dict(lane), follow_case=False)
    repo.update("enquiry_route", hand, {"result": "Lost", "result_on": "2026-11-15"})
    repo.commit()
    conn.close()
    _back_to_v38(path)
    notes = []
    conn = db.open_db(path, upgrade_notes=notes)
    got = {name: tuple(conn.execute("SELECT result, lost_reason, result_on FROM enquiry_route WHERE id = ?",
                                    (route_id,)).fetchone()) for name, route_id in added.items()}
    assert got["lost"] == ("Lost", "Price too high", "2026-11-10")
    assert got["cancelled"] == ("Cancelled", "Shipment cancelled", "2026-11-11")
    assert got["won"] == (None, None, None) and got["dropped"] == (None, None, None)
    assert tuple(conn.execute("SELECT result, lost_reason, result_on FROM enquiry_route WHERE id = ?",
                              (hand,)).fetchone()) == ("Lost", None, "2026-11-15")
    assert "Open routes of Lost / Cancelled cases given the case's result: 2 (OPP-3303 B: Lost, Price too high, " \
           "2026-11-10; OPP-3304 B: Cancelled, Shipment cancelled, 2026-11-11)" in "\n".join(notes)
    assert conn.execute("SELECT COUNT(*) FROM change_log WHERE source = 'migration' AND table_name = 'enquiry_route' "
                        "AND field = 'result'").fetchone()[0] == 2
    conn.close()

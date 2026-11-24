"""Shipments 5c (schema v27): coordinates from the two
reference files, the map and its fallback strip, the marker per
milestone, carrier links, and the plan-not-event refinement.

Rewritten 2026-11-03 (TK1) on invented values;
cases OPP-1380, OPP-1470 and OPP-1471.
"""

from __future__ import annotations

import csv

import pytest

from conftest import add_enquiry, add_party, seed_reference, form_nesting
from opplist import coordinates, db, migrate_master, paths, shipments
from opplist.outlook_bridge import read_fixture
from opplist.web import create_app


# --------------------------------------------------------- refinement ----
def test_a_plan_is_not_a_pickup():
    mail = read_fixture(paths.FIXTURES_DIR / "agent_relays_pickup_deadline.json")
    assert shipments.detect(mail, mode="air") is None                       # 'have to be picked up by Thursday'
    mail.body_excerpt = "Hi,\r\n\r\nCollection set for Thu 10 Sep, the pallets will be picked up at 10 am.\r\n"
    got = shipments.detect(mail, mode="air")
    assert (got.kind, got.name, got.at) == ("note", "Picked up planned Sep 10", "2026-09-03 11:25")
    for text in ("The skids need to be picked up tomorrow.", "Freight is going to be delivered after the weekend.",
                 "It should be delivered by Tuesday.", "The box must be picked up ahead of the last free day."):
        mail.body_excerpt = "Hi,\r\n\r\n{}\r\n".format(text)
        assert shipments.detect(mail, mode="ocean") is None, text
    mail.body_excerpt = "Hi,\r\n\r\nThe drums were picked up at noon.\r\n"
    assert shipments.detect(mail, mode="air").name == "Picked up"


# -------------------------------------------------------- coordinates ----
def test_dms_and_the_matching_rules(tmp_path):
    assert coordinates.dms_to_decimal("4047N 07400W") == (40.7833, -74.0)
    assert coordinates.dms_to_decimal("0117N 10351E") == (1.2833, 103.85)
    assert coordinates.dms_to_decimal("3327S 07040W") == (-33.45, -70.6667)
    assert coordinates.dms_to_decimal("") is None
    # a small UN/LOCODE list (no header, latin-1) and a small airports.csv
    unlocode = tmp_path / "UNLOCODE CodeListPart1.csv"
    unlocode.write_bytes(
        b',US,,.UNITED STATES,,,,,,,,\r\n'
        b',US,NYC,New York,New York,NY,12345---,AI,0507,,4042N 07400W,\r\n'
        b',US,MIA,Miami,Miami,FL,12345---,AI,0507,MIA,2547N 08013W,\r\n'
        b',CL,SAI,San Antonio,San Antonio,VS,1-------,AI,0507,,3335S 07138W,\r\n'
        b',CL,SCL,Santiago,Santiago,RM,--34----,AI,0507,SCL,3327S 07040W,\r\n'
        b',CL,ZSA,Santiago del Estero Fake,Santiago,RM,--3-----,RL,0507,,3300S 07100W,\r\n'
        b',CL,ZSB,Santiago Fake Two,Santiago,RM,--3-----,RL,0507,,3301S 07101W,\r\n'
        b',BR,SSZ,Santos,Santos,SP,1-------,AI,0507,,2357S 04620W,\r\n'
        b',BR,STS,Santos Rail,Santos,SP,-23-----,AI,0507,,,\r\n'
        b',AR,BUE,Buenos Aires,Buenos Aires,C,12345---,AI,0507,,3436S 05823W,\r\n'
        b',AR,LCU,Luj\xe1n de Cuyo,Lujan de Cuyo,M,--3-----,AI,0507,,3302S 06855W,\r\n'
        b',US,CHI,Chicago,Chicago,IL,1--45---,AI,0507,,,\r\n')
    airports = tmp_path / "airports.csv"
    with airports.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "ident", "type", "name", "latitude_deg", "longitude_deg", "elevation_ft", "continent",
                         "iso_country", "iso_region", "municipality", "scheduled_service", "icao_code", "iata_code"])
        writer.writerow([1, "KMIA", "large_airport", "Miami International Airport", 25.7932, -80.2906, 8, "NA",
                         "US", "US-FL", "Miami", "yes", "KMIA", "MIA"])
        writer.writerow([2, "SCEL", "large_airport", "Arturo Merino Benitez", -33.393, -70.7858, 1555, "SA",
                         "CL", "CL-RM", "Santiago", "yes", "SCEL", "SCL"])
        writer.writerow([3, "XXXX", "heliport", "Some Heliport", 1.0, 1.0, 0, "NA", "US", "US-FL", "", "no", "", "HEL"])
        writer.writerow([4, "KORD", "large_airport", "O'Hare", 41.9786, -87.9048, 672, "NA", "US", "US-IL", "Chicago", "yes", "KORD", "ORD"])
        writer.writerow([5, "KMDW", "medium_airport", "Midway", 41.786, -87.7524, 620, "NA", "US", "US-IL", "Chicago", "yes", "KMDW", "MDW"])
        writer.writerow([6, "KNBC", "large_airport", "Beaufort MCAS", 32.4774, -80.7232, 37, "NA", "US", "US-SC", "Beaufort", "yes", "KNBC", ""])
    rows = coordinates.read_unlocode(unlocode)
    assert len(rows) == 11 and rows[0]["code"] == "USNYC" and rows[0]["lat"] == 40.7 and rows[-2]["name"] == "Luján de Cuyo"
    assert rows[0]["function"] == "12345---" and rows[0]["status"] == "AI"
    airports_rows = coordinates.read_airports(airports)
    assert [a["iata"] for a in airports_rows] == ["MIA", "SCL", "ORD", "MDW", ""]   # the heliport is out
    # the folder works too (the zip's csv folder), by the file's name
    assert len(coordinates.read_unlocode(tmp_path)) == 11

    path = tmp_path / "coords.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(repo.conn, "shipments", "1")   # FX3: switched on for these tests
    for values in (
            {"key": "NEW YORK, NY", "name": "New York", "type": "city", "country": "USA", "country_code": "US", "unlocode": "USNYC"},
            {"key": "MIA", "name": "Miami", "type": "airport", "country": "USA", "country_code": "US", "iata": "MIA"},
            {"key": "SAN ANTONIO", "name": "San Antonio", "type": "seaport", "country": "Chile", "country_code": "CL"},
            {"key": "SANTIAGO", "name": "Santiago", "type": "city", "country": "Chile", "country_code": "CL"},
            {"key": "SANTIAGO FAKE", "name": "Santiago Fake Two", "type": "city", "country": "Chile", "country_code": "CL"},
            {"key": "SANTOS", "name": "Santos", "type": "seaport", "country": "Brazil", "country_code": "BR"},
            {"key": "LUJAN DE CUYO", "name": "Lujan de Cuyo", "type": "city", "country": "Brazil", "country_code": "AR"},
            {"key": "NOWHERE", "name": "Nowhere", "type": "city", "country": "Chile", "country_code": "CL"},
            {"key": "CHICAGO, IL", "name": "Chicago", "type": "city", "country": "USA", "country_code": "US", "unlocode": "USCHI"},
            {"key": "BEAUFORT, NC", "name": "Beaufort", "type": "city", "country": "USA", "country_code": "US"},
            {"key": "DONE", "name": "Done", "type": "city", "country": "Chile", "country_code": "CL", "lat": 1.0, "lon": 2.0}):
        values.setdefault("source", "manual")
        repo.insert("master_location", values)
    repo.commit()
    proposals = coordinates.propose(conn, rows, airports_rows)
    by_key = {p.key: p for p in proposals}
    assert "DONE" not in by_key                                                # already has coordinates
    assert (by_key["NEW YORK, NY"].source, by_key["NEW YORK, NY"].lat, by_key["NEW YORK, NY"].lon) == ("unlocode", 40.7, -74.0)
    assert (by_key["MIA"].source, by_key["MIA"].lat) == ("iata", 25.7932)      # airports.csv, not the UN/LOCODE row
    assert (by_key["SAN ANTONIO"].source, by_key["SAN ANTONIO"].matched) == ("name", "San Antonio (CLSAI)")
    assert by_key["SANTOS"].source == "name" and by_key["SANTOS"].lat == -23.95   # the rail row has no coordinates
    assert (by_key["SANTIAGO"].source, by_key["SANTIAGO"].matched) == ("name", "Santiago (CLSCL)")   # the one port / airport of three
    assert by_key["SANTIAGO FAKE"].source == "unmatched"
    # two road-only entries of one name, neither a port nor approved alone: ambiguous
    tied = [r for r in rows if r["location"] in ("ZSA", "ZSB")]
    assert coordinates._narrow(tied, "") == tied
    assert by_key["LUJAN DE CUYO"].source == "name"                             # NameWoDiacritics
    assert by_key["NOWHERE"].source == "unmatched"
    # USCHI is in the code list without coordinates: the town's one large airport stands in
    assert (by_key["CHICAGO, IL"].source, by_key["CHICAGO, IL"].matched, by_key["CHICAGO, IL"].lat) == ("city-airport", "O'Hare (ORD)", 41.9786)
    assert by_key["BEAUFORT, NC"].source == "unmatched"                       # the SC airfield is another state
    assert coordinates.state_of("BEAUFORT, NC", "US") == "NC" and coordinates.state_of("MADRID", "ES") is None
    assert {k: v for k, v in coordinates.counts(proposals).items() if v} == {"unlocode": 1, "iata": 1, "name": 4, "city-airport": 1, "unmatched": 3}
    out = coordinates.write_csv(proposals, tmp_path / "proposed.csv")
    text = out.read_bytes().decode("utf-8-sig")
    assert text.startswith("key,source,matched,lat,lon,candidates,note\r\n") and "NOWHERE,unmatched" in text
    # apply writes only empty coordinates, through the repository
    conn.execute("UPDATE master_location SET lat = 9, lon = 9 WHERE key = 'SANTOS'")
    conn.commit()
    assert coordinates.apply(repo, proposals) == 6
    repo.commit()
    assert conn.execute("SELECT lat, lon FROM master_location WHERE key = 'SANTOS'").fetchone()[:] == (9.0, 9.0)
    assert conn.execute("SELECT lat, lon FROM master_location WHERE key = 'NEW YORK, NY'").fetchone()[:] == (40.7, -74.0)
    assert conn.execute("SELECT COUNT(*) FROM change_log WHERE table_name = 'master_location' AND field = 'lat'").fetchone()[0] == 6
    conn.close()


# ---------------------------------------------------------------- map ----
@pytest.fixture
def world(tmp_path):
    path = tmp_path / "map.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(repo.conn, "shipments", "1")   # FX3: switched on for these tests
    locs = {}
    for key, name, type_, country, lat, lon in (
            ("GREENVILLE, SC", "Greenville", "city", "USA", 34.85, -82.39), ("SAVANNAH, GA", "Savannah", "seaport", "USA", 32.08, -81.09),
            ("CARTAGENA", "Cartagena", "seaport", "Unknown", 10.39, -75.51), ("SANTOS", "Santos", "seaport", "Brazil", -23.96, -46.33),
            ("DAYTON, OH", "Dayton", "city", "USA", 39.76, -84.19), ("KNOXVILLE, TN", "Knoxville", "city", "USA", 35.96, -83.92),
            ("NOCOORDS", "Nowhere", "city", "USA", None, None)):
        locs[key] = repo.insert("master_location", {"key": key, "name": name, "type": type_, "country": country,
                                                    "lat": lat, "lon": lon, "source": "manual"})
    agent = add_party(repo, "Costa Verde Cargo", country="USA")
    ocean = add_enquiry(repo, "OPP-1380", agent, received="2015-08-17", service_mode="FCL",
                        origin_country="USA", dest_country="Brazil")
    repo.update("enquiry", ocean, {"origin_loc_id": locs["GREENVILLE, SC"], "dest_loc_id": locs["SANTOS"],
                                   "origin_loc": "GREENVILLE, SC", "dest_loc": "SANTOS"})
    repo.set_status(ocean, "Won", "2015-08-21", rate_used="own")
    sid = shipments.start(repo, ocean, "Greenville, SC to Santos")
    shipments.edit(repo, sid, {"via_points": "Savannah; Cartagena", "planned_departure": "2015-09-09",
                               "planned_arrival": "2015-10-06", "carrier": "Hapag-Lloyd",
                               "booking_no": "HLCUSAV3Q418", "awb": "016-12345678"})
    truck = add_enquiry(repo, "OPP-1470", agent, received="2015-09-18", service_mode="Truck",
                        origin_country="USA", dest_country="USA")
    repo.update("enquiry", truck, {"origin_loc_id": locs["DAYTON, OH"], "dest_loc_id": locs["KNOXVILLE, TN"],
                                   "origin_loc": "DAYTON, OH", "dest_loc": "KNOXVILLE, TN"})
    repo.set_status(truck, "Won", "2015-09-22", rate_used="own")
    tid = shipments.start(repo, truck, "Dayton, OH to Knoxville, TN")
    shipments.edit(repo, tid, {"planned_pickup": "2015-09-23", "planned_delivery": "2015-09-24", "carrier": "Ridgeback Transport"})
    broken = add_enquiry(repo, "OPP-1471", agent, received="2015-09-18", service_mode="Air",
                         origin_country="USA", dest_country="Unknown")
    repo.update("enquiry", broken, {"origin_loc_id": locs["NOCOORDS"], "dest_loc_id": locs["SANTOS"],
                                    "origin_loc": "NOCOORDS", "dest_loc": "SANTOS"})
    repo.set_status(broken, "Won", "2015-09-22", rate_used="own")
    bid = shipments.start(repo, broken, "Nowhere to Santos")
    shipments.edit(repo, bid, {"via_points": "Atlantis"})
    repo.commit()
    return conn, repo, path, {"sid": sid, "tid": tid, "bid": bid, "locs": locs}


def _route(conn, sid, today="2015-09-24"):
    ship = conn.execute("SELECT * FROM shipment WHERE id = ?", (sid,)).fetchone()
    case = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (ship["enquiry_id"],)).fetchone()
    return shipments.route_map(conn, ship, case, shipments.summary(conn, sid, today))


def test_the_world_map_and_the_marker_per_milestone(world):
    conn, repo, path, ids = world
    sid = ids["sid"]
    route = _route(conn, sid)
    assert route["kind"] == "world" and [p["name"] for p in route["points"]] == ["Greenville", "Savannah", "Cartagena", "Santos"]
    assert route["marker"] is None and route["missing"] == []
    svg = route["svg"]
    assert svg.startswith('<svg viewBox="0 68 640 284"') and 'stroke-dasharray="5 4"' in svg     # cropped to the land
    assert 'cx="182.9" cy="162.2"' in svg and 'cx="240.3" cy="282.7"' in svg      # the sample's own points
    assert ">Greenville  ETS Sep 9<" in svg and ">Santos  ETA Oct 6<" in svg
    # the marker moves with the last confirmed milestone
    shipments.log_milestone(repo, sid, "Booked", "2015-08-24 08:40")
    assert _route(conn, sid)["marker"] is None
    shipments.log_milestone(repo, sid, "Picked up", "2015-09-01 13:30")
    assert _route(conn, sid)["marker"] == 0
    shipments.log_milestone(repo, sid, "Departed", "2015-09-09 16:20")
    route = _route(conn, sid)
    assert route["marker"] == 1 and 'cx="183.7" cy="167.9" r="3.5" fill="#e8456c"' in route["svg"]
    shipments.log_milestone(repo, sid, "Arrived", "2015-10-19 07:15", eta="2015-10-19")
    route = _route(conn, sid)
    assert route["marker"] == 2 and ">Santos  arrived Oct 19<" in route["svg"]
    shipments.log_milestone(repo, sid, "Delivered", "2015-10-22 11:00")
    assert _route(conn, sid)["marker"] == 3


def test_the_us_map_for_a_truck_inside_the_usa(world):
    conn, repo, path, ids = world
    route = _route(conn, ids["tid"])
    assert route["kind"] == "us" and route["svg"].startswith('<svg viewBox="0 0 640 400"')
    assert 'cx="420.1" cy="200.9"' in route["svg"] and 'cx="428.5" cy="245.2"' in route["svg"]
    assert ">Dayton  pickup Sep 23<" in route["svg"] and ">Knoxville  due Sep 24<" in route["svg"]
    shipments.log_milestone(repo, ids["tid"], "Delivered", "2015-09-24 10:00")
    assert _route(conn, ids["tid"])["marker"] == 1


def test_the_strip_when_a_point_has_no_coordinates(world):
    conn, repo, path, ids = world
    route = _route(conn, ids["bid"])
    assert route["kind"] == "strip" and [m["name"] for m in route["missing"]] == ["Nowhere", "Atlantis"]
    assert route["svg"].startswith('<svg viewBox="0 0 640 90"') and "Nowhere (no coordinates)" in route["svg"]
    assert "Atlantis (no coordinates)" in route["svg"] and ">Santos<" in route["svg"]
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    client = app.test_client()
    body = client.get("/shipments/{}".format(ids["bid"])).data.decode("utf-8")
    assert "No coordinates yet for" in body
    assert 'href="/locations/{}">Nowhere</a>'.format(ids["locs"]["NOCOORDS"]) in body and "<b>Atlantis</b> (not in Locations)" in body
    body = client.get("/shipments/{}".format(ids["sid"])).data.decode("utf-8")
    assert 'class="wmap"' in body and "No coordinates yet" not in body and "via Savannah; Cartagena" in body
    assert 'name="via_points" value="Savannah; Cartagena"' in body
    deepest, ids_ = form_nesting(body)
    assert deepest <= 1, ids_


# ------------------------------------------------------ carrier links ----
class Row(dict):
    def __getitem__(self, key):
        return self.get(key)


def test_carrier_links_for_carriers_and_awb_prefixes(world):
    zim = Row(carrier="ZIM Integrated Shipping", booking_no="ZIMUSAV4Q27", mbl=None, awb=None)
    assert shipments.carrier_links(zim) == [("Track at ZIM", "https://www.zim.com/tools/track-a-shipment?consnumber=ZIMUSAV4Q27", "booking_no")]
    maersk = Row(carrier="Maersk Line", booking_no=None, mbl="MAEU123456789", awb=None)
    assert shipments.carrier_links(maersk)[0][:2] == ("Track at Maersk", "https://www.maersk.com/tracking/MAEU123456789")
    hapag = Row(carrier="Hapag-Lloyd AG", booking_no="HLCU1234", mbl=None, awb=None)
    assert shipments.carrier_links(hapag)[0][0] == "Track at Hapag-Lloyd"
    united = Row(carrier="United Airlines", booking_no=None, mbl=None, awb="016-12345678")
    assert shipments.carrier_links(united) == [("Track at United Cargo", "https://www.unitedcargo.com/en/us/track/track-shipment.html?awb=016-12345678", "awb")]
    cathay = Row(carrier="Cathay Pacific", booking_no=None, mbl=None, awb="16012345678")
    assert shipments.carrier_links(cathay)[0][:2] == ("Track at Cathay Cargo", "https://www.cathaycargo.com/en-us/track-and-trace.html?awb=160-12345678")
    assert shipments.carrier_links(Row(carrier="Vanta Multimodal", booking_no="X1", mbl=None, awb=None)) == []
    assert shipments.carrier_links(Row(carrier="ZIM", booking_no=None, mbl=None, awb=None)) == []       # no reference
    assert shipments.carrier_links(Row(carrier="One Planet Logistics", booking_no="B1", mbl=None, awb="999-1234567")) == []
    conn, repo, path, ids = world
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    body = app.test_client().get("/shipments/{}".format(ids["sid"])).data.decode("utf-8")
    assert 'target="_blank" rel="noopener">Track at Hapag-Lloyd</a>' in body and ">Track at United Cargo</a>" in body


# ------------------------------------------------------ manual + page ----
def test_manual_rows_come_first_and_never_overwrite_by_default(tmp_path):
    manual = tmp_path / "coordinates-manual.csv"
    manual.write_text("key,lat,lon,note\r\n"
                      '"NEW YORK, NY",40.71,-74.01,by hand\r\n'
                      "DONE,5.5,6.6,already set\r\n"
                      "GHOST,1,2,not on file\r\n"
                      "OLD,1,2,retired row\r\n"
                      "BADNUM,abc,2,does not read\r\n"
                      "RANGE,95,2,out of range\r\n", encoding="utf-8")
    rows = coordinates.read_manual(manual)
    assert [(r["key"], r["lat"]) for r in rows] == [("NEW YORK, NY", 40.71), ("DONE", 5.5), ("GHOST", 1.0),
                                                     ("OLD", 1.0), ("BADNUM", None), ("RANGE", None)]
    path = tmp_path / "manual.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(repo.conn, "shipments", "1")   # FX3: switched on for these tests
    for values in (
            {"key": "NEW YORK, NY", "name": "New York", "type": "city", "country": "USA", "country_code": "US", "unlocode": "USNYC"},
            {"key": "DONE", "name": "Done", "type": "city", "country": "Chile", "country_code": "CL", "lat": 1.0, "lon": 2.0},
            {"key": "OLD", "name": "Old", "type": "city", "country": "Chile", "country_code": "CL", "status": "retired"},
            {"key": "BADNUM", "name": "Badnum", "type": "city", "country": "Chile", "country_code": "CL"},
            {"key": "RANGE", "name": "Range", "type": "city", "country": "Chile", "country_code": "CL"},
            {"key": "GAP", "name": "Gap", "type": "city", "country": "Chile", "country_code": "CL"}):
        values.setdefault("source", "manual")
        repo.insert("master_location", values)
    repo.commit()
    unlocode = [{"code": "USNYC", "country": "US", "location": "NYC", "name": "New York", "name_wo_diacritics": "New York",
                 "subdivision": "NY", "function": "1-------", "status": "AI", "iata": "", "lat": 40.7, "lon": -74.0}]
    proposals = coordinates.propose(conn, unlocode, [], manual=rows)
    by_key = {p.key: p for p in proposals}
    assert (by_key["NEW YORK, NY"].source, by_key["NEW YORK, NY"].lat) == ("manual", 40.71)       # manual beats the code list
    assert by_key["DONE"].source == "kept" and by_key["GHOST"].source == "unknown-key"
    assert by_key["OLD"].source == "retired-key"                                                  # retired: never matched
    assert by_key["BADNUM"].source == "unmatched" and by_key["RANGE"].source == "unmatched"
    assert by_key["GAP"].source == "unmatched"
    assert coordinates.counts(proposals)["manual"] == 1 and coordinates.counts(proposals)["kept"] == 1
    text = coordinates.write_csv(proposals, tmp_path / "p.csv").read_bytes().decode("utf-8-sig")
    assert text.startswith("key,source,matched,lat,lon,candidates,note\r\n") and '"NEW YORK, NY",manual,by hand,40.71,-74.01,,by hand' in text
    assert coordinates.apply(repo, proposals) == 1
    repo.commit()
    assert conn.execute("SELECT lat, lon FROM master_location WHERE key = 'DONE'").fetchone()[:] == (1.0, 2.0)
    assert conn.execute("SELECT new_value FROM change_log WHERE table_name = 'master_location' AND field = 'coordinates'"
                        ).fetchone()[0] == "fill-coordinates manual: by hand"
    # --manual-wins replaces a set coordinate
    proposals = coordinates.propose(conn, unlocode, [], manual=rows, manual_wins=True, only_empty=False)
    assert {p.key: p.source for p in proposals}["DONE"] == "manual"
    assert coordinates.apply(repo, proposals) == 1
    repo.commit()
    assert conn.execute("SELECT lat, lon FROM master_location WHERE key = 'DONE'").fetchone()[:] == (5.5, 6.6)
    conn.execute("ALTER TABLE location RENAME TO location_old")
    conn.execute(migrate_master.LOCATION_VIEW_SQL)                    # the page lists rows once master is active
    conn.commit()
    conn.close()
    # the Locations page: lat / lon in the row, the 'No coordinates' filter without retired rows
    app = create_app(path)
    app.config["TESTING"] = True
    client = app.test_client()
    body = client.get("/locations").data.decode("utf-8")
    assert "<th>Lat</th><th>Lon</th>" in body and "40.71" in body and "-74.01" in body
    assert 'href="/locations?missing=1">No coordinates <span class="count">3</span>' in body      # BADNUM, RANGE, GAP
    body = client.get("/locations?missing=1").data.decode("utf-8")
    assert ">GAP<" in body and ">OLD<" not in body and ">NEW YORK, NY<" not in body


def test_the_edit_form_validates_lat_and_lon(tmp_path):
    path = tmp_path / "edit.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(repo.conn, "shipments", "1")   # FX3: switched on for these tests
    loc = repo.insert("master_location", {"key": "GAP", "name": "Gap", "type": "city", "country": "Chile",
                                          "country_code": "CL", "source": "manual"})
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    client = app.test_client()
    base = {"key": "GAP", "name": "Gap", "type": "city", "country": "Chile", "status": "active"}
    body = client.post("/locations/{}".format(loc), data=dict(base, lat="12.5"), follow_redirects=True).data.decode("utf-8")
    assert "fill both or neither" in body
    body = client.post("/locations/{}".format(loc), data=dict(base, lat="95", lon="10"), follow_redirects=True).data.decode("utf-8")
    assert "Latitude runs -90 to 90" in body
    body = client.post("/locations/{}".format(loc), data=dict(base, lat="x", lon="10"), follow_redirects=True).data.decode("utf-8")
    assert "Latitude runs -90 to 90" in body
    client.post("/locations/{}".format(loc), data=dict(base, lat="-33.45", lon="-70.6667"), follow_redirects=True)
    conn = db.open_db(path)
    assert conn.execute("SELECT lat, lon FROM master_location WHERE id = ?", (loc,)).fetchone()[:] == (-33.45, -70.6667)
    assert conn.execute("SELECT COUNT(*) FROM change_log WHERE table_name = 'master_location' AND field IN ('lat', 'lon')").fetchone()[0] == 2
    conn.close()
    body = client.get("/locations/{}".format(loc)).data.decode("utf-8")
    assert 'name="lat" value="-33.45"' in body
    client.post("/locations/{}".format(loc), data=dict(base, lat="", lon=""), follow_redirects=True)       # both empty clears
    conn = db.open_db(path)
    assert conn.execute("SELECT lat, lon FROM master_location WHERE id = ?", (loc,)).fetchone()[:] == (None, None)
    conn.close()

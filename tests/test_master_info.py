"""The master contract views, read the way another app reads them
. RD1 (2026-10-19, schema v42): the
route, route place and cargo line views, route_ids on the vendor request
view and no_parent on the location view.

Every company, place, code, case, number and date here is invented; cases
from OPP-2001."""

from __future__ import annotations

import json
import subprocess
import sys

from conftest import add_party
from opplist import db, master_info, migrate_master, paths


def build(tmp_path):
    path = tmp_path / "m.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, "test")
    for name, region in (("USA", "North America"), ("Chile", "Latin America"), ("Unknown", "Undefined")):
        repo.insert("country", {"name": name, "region": region})
    db.set_setting(conn, "next_opp_number", 2001)
    city = repo.add_master_location("BRISTOW FALLS, IL", "USA", "city", name="Bristow Falls", source="phase1")
    repo.add_master_location("QBF", "USA", "airport", iata="QBF", parent_id=city["id"], source="phase1")
    repo.add_master_alias("BRF", city["id"], "code")
    port_a = repo.add_master_location("PUERTO CALDO", "Chile", "city", name="Puerto Caldo", is_seaport=1,
                                      source="phase1")
    port_b = repo.add_master_location("BAHIA LUMEN", "Chile", "city", name="Bahia Lumen", is_seaport=1,
                                      source="phase1")
    inland = repo.add_master_location("VILLA ROBLEDO", "Chile", "city", name="Villa Robledo", source="phase1")
    a = add_party(repo, "Corvane Chile", country="Chile")
    b = add_party(repo, "Corvane Peru", country="Chile")
    gone = add_party(repo, "Old Corvane", country="Chile")
    group = repo.add_party_group("CORVANE", "Corvane")
    repo.update("party", a, {"group_id": group, "roles": "agent"})
    repo.update("party", b, {"group_id": group, "roles": "agent|warehouse"})
    repo.merge_party(a, gone, today="2026-11-03")
    case = repo.create_enquiry({"requester_party_id": a, "received_on": "2026-11-02",
                                "origin_loc_id": city["id"], "origin_country": "USA",
                                "dest_country": "Chile", "service_mode": "Air", "incoterm": "EXW"},
                               "2026-11-02")
    # route B: by sea to an inland city, discharged at either of two ports
    route_b = repo.add_route(case, {"origin_country": "USA", "origin_loc_id": city["id"],
                                    "dest_country": "Chile", "dest_places": [inland["id"]],
                                    "pod_places": [port_a["id"], port_b["id"]],
                                    "service_mode": "LCL", "incoterm": "FOB",
                                    "service_scope": "Port to door"})
    repo.add_cargo_line(case, {"pieces": 3, "pack_unit": "Pallets", "weight_kg": 412.5})
    repo.add_cargo_line(case, {"pieces": 2, "cbm": 1.75, "route_ids": str(route_b)})
    repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": b, "asked_on": "2026-11-03",
                                   "machine_status": "Quoted"})
    repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": b, "asked_on": "2026-11-04",
                                   "voided": 1})
    repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": b, "asked_on": "2026-11-05",
                                   "route_ids": str(route_b)})
    conn.execute("ALTER TABLE location RENAME TO location_old")
    conn.execute(migrate_master.LOCATION_VIEW_SQL)
    repo.commit()
    conn.close()
    return path


def rows_of(conn, view, where=""):
    return [dict(zip(master_info.CONTRACT_V1[view], r))
            for r in conn.execute("SELECT * FROM opp.{} {}".format(view, where))]


def test_the_views_carry_exactly_the_contract_columns(tmp_path):
    path = build(tmp_path)
    conn = master_info.open_attached(path)
    info = master_info.describe(conn, "opp")
    assert info["version"] == 1
    assert set(master_info.CONTRACT_VIEWS) == set(master_info.CONTRACT_V1)
    assert len(master_info.CONTRACT_VIEWS) == 8
    for name, columns in master_info.CONTRACT_V1.items():
        assert info["views"][name]["columns"] == columns, name
    assert info["views"]["v_master_location"]["rows"] == 5
    assert info["views"]["v_master_alias"]["rows"] == 1          # BRF
    assert info["views"]["v_master_party"]["rows"] == 2          # the merged row is out
    assert info["views"]["v_master_enquiry_lane"]["rows"] == 1
    assert info["views"]["v_master_vendor_request"]["rows"] == 3  # the voided one is listed, flagged
    assert info["views"]["v_master_route_lane"]["rows"] == 2
    assert info["views"]["v_master_route_place"]["rows"] == 3     # B's destination and its two ports
    assert info["views"]["v_master_enquiry_cargo"]["rows"] == 2
    lane = rows_of(conn, "v_master_enquiry_lane")[0]
    assert (lane["opp_no"], lane["origin_loc_id"], lane["dest_loc_id"], lane["service_mode"],
            lane["incoterm"], lane["requester_party_id"], lane["status"], lane["scope"]) == \
        ("OPP-2001", 1, None, "Air", "EXW", 1, "New", "In")
    vrs = rows_of(conn, "v_master_vendor_request", "ORDER BY id")
    assert [(v["opp_no"], v["vendor_party_id"], v["quoted"], v["voided"]) for v in vrs] == \
        [("OPP-2001", 2, 1, 0), ("OPP-2001", 2, 0, 1), ("OPP-2001", 2, 0, 0)]
    # the old columns kept their places; no_parent / route_ids came at the end
    assert master_info.CONTRACT_V1["v_master_location"][-1] == "no_parent"
    assert master_info.CONTRACT_V1["v_master_vendor_request"][-1] == "route_ids"
    conn.close()


def test_the_route_views_carry_route_b_its_ports_and_its_cargo(tmp_path):
    path = build(tmp_path)
    conn = master_info.open_attached(path)
    ids = {k: conn.execute("SELECT id FROM opp.v_master_location WHERE key = ?", (k,)).fetchone()[0]
           for k in ("BRISTOW FALLS, IL", "PUERTO CALDO", "BAHIA LUMEN", "VILLA ROBLEDO")}
    routes = rows_of(conn, "v_master_route_lane", "ORDER BY seq")
    assert [(r["opp_no"], r["seq"], r["letter"]) for r in routes] == [("OPP-2001", 1, "A"), ("OPP-2001", 2, "B")]
    b = routes[1]
    assert (b["service_mode"], b["incoterm"], b["service_scope"], b["origin_loc_id"], b["dest_loc_id"],
            b["pod"], b["received_on"], b["status"], b["scope"], b["requester_party_id"], b["voided"]) == \
        ("LCL", "FOB", "Port to door", ids["BRISTOW FALLS, IL"], ids["VILLA ROBLEDO"],
         "PUERTO CALDO / BAHIA LUMEN", "2026-11-02", "New", "In", 1, 0)
    places = rows_of(conn, "v_master_route_place", "WHERE route_id = {} ORDER BY side, seq".format(b["route_id"]))
    assert [(p["side"], p["seq"], p["location_id"], p["voided"]) for p in places] == \
        [("dest", 1, ids["VILLA ROBLEDO"], 0), ("pod", 1, ids["PUERTO CALDO"], 0), ("pod", 2, ids["BAHIA LUMEN"], 0)]
    lines = rows_of(conn, "v_master_enquiry_cargo", "ORDER BY seq")
    assert [(c["opp_no"], c["seq"], c["route_ids"], c["counted"], c["voided"]) for c in lines] == \
        [("OPP-2001", 1, None, 1, 0), ("OPP-2001", 2, str(b["route_id"]), 1, 0)]
    assert (lines[0]["pieces"], lines[0]["pack_unit"], lines[0]["weight_kg"]) == (3, "Pallets", 412.5)
    assert (lines[1]["pieces"], lines[1]["cbm"]) == (2, 1.75)
    vrs = rows_of(conn, "v_master_vendor_request", "ORDER BY id")
    assert [v["route_ids"] for v in vrs] == [None, None, str(b["route_id"])]
    assert {r["no_parent"] for r in rows_of(conn, "v_master_location")} == {0}
    conn.close()


def test_the_attached_connection_cannot_write_and_reads_the_same_numbers_as_the_cli(tmp_path):
    path = build(tmp_path)
    conn = master_info.open_attached(path)
    row = conn.execute("SELECT key, parent_key FROM opp.v_master_location WHERE key = 'QBF'").fetchone()
    assert tuple(row) == ("QBF", "BRISTOW FALLS, IL")
    party = dict(zip(master_info.CONTRACT_V1["v_master_party"],
                     conn.execute("SELECT * FROM opp.v_master_party WHERE key = 'Corvane Peru'").fetchone()))
    assert (party["roles"], party["group_key"], party["group_name"], party["name"]) == \
        ("agent|warehouse", "CORVANE", "Corvane", "Corvane Peru")
    try:
        conn.execute("UPDATE opp.master_location SET name = 'x'")
        wrote = True
    except Exception:
        wrote = False
    assert not wrote
    conn.close()
    # a separate python process, plain sqlite3, mode=ro ATTACH: what the Rate Desk would do
    script = (
        "import sqlite3, json, sys\n"
        "c = sqlite3.connect('file::memory:', uri=True)\n"
        "c.execute('ATTACH DATABASE ? AS opp', ('file:' + sys.argv[1].replace(chr(92), '/') + '?mode=ro',))\n"
        "out = {}\n"
        "for v in sys.argv[2].split(','):\n"
        "    cols = [r[1] for r in c.execute('PRAGMA opp.table_info(' + v + ')')]\n"
        "    out[v] = [cols, c.execute('SELECT COUNT(*) FROM opp.' + v).fetchone()[0]]\n"
        "q = lambda sql: c.execute(sql).fetchone()\n"
        "out['_values'] = {\n"
        "    'route_b': q(\"SELECT opp_no || ' ' || letter, service_mode FROM opp.v_master_route_lane WHERE seq = 2\"),\n"
        "    'pod': q(\"SELECT l.key FROM opp.v_master_route_place p JOIN opp.v_master_location l \"\n"
        "             \"ON l.id = p.location_id WHERE p.side = 'pod' AND p.seq = 2\"),\n"
        "    'cargo': q('SELECT seq, cbm FROM opp.v_master_enquiry_cargo WHERE route_ids IS NOT NULL'),\n"
        "    'vendor': q('SELECT asked_on FROM opp.v_master_vendor_request WHERE route_ids IS NOT NULL'),\n"
        "    'route_ids': q('SELECT c.route_ids = CAST(r.route_id AS TEXT) FROM opp.v_master_enquiry_cargo c, '\n"
        "                   'opp.v_master_route_lane r WHERE c.route_ids IS NOT NULL AND r.letter = \\'B\\''),\n"
        "}\n"
        "print(json.dumps(out))\n")
    other = subprocess.run([sys.executable, "-c", script, str(path), ",".join(master_info.CONTRACT_VIEWS)],
                           capture_output=True, text=True)
    assert other.returncode == 0, other.stderr
    seen = json.loads(other.stdout)
    values = seen.pop("_values")
    assert values == {"route_b": ["OPP-2001 B", "LCL"], "pod": ["BAHIA LUMEN"], "cargo": [2, 1.75],
                      "vendor": ["2026-11-05"], "route_ids": [1]}
    cli = subprocess.run([sys.executable, "-m", "opplist", "master-info", "--db", str(path)],
                         capture_output=True, text=True, cwd=str(paths.PROJECT_DIR))
    assert cli.returncode == 0, cli.stderr
    assert set(seen) == set(master_info.CONTRACT_VIEWS)
    for name, (columns, rows) in seen.items():
        assert columns == master_info.CONTRACT_V1[name]
        assert "{:<24} {:>5} rows  {:>2} columns: {}".format(
            name, rows, len(columns), ", ".join(columns)) in cli.stdout
    assert "master_contract_version 1" in cli.stdout


def test_master_info_says_when_a_database_has_no_master_yet(tmp_path):
    conn = db.open_db(tmp_path / "plain.db")
    conn.close()
    conn = master_info.open_attached(tmp_path / "plain.db")
    info = master_info.describe(conn, "opp")
    conn.close()
    # a fresh database has the views (schema.sql) but no rows
    assert info["version"] == 1 and info["views"]["v_master_location"]["rows"] == 0
    assert info["views"]["v_master_route_lane"]["rows"] == 0
    assert master_info.lines(info)[1].startswith("v_master_location            0 rows")

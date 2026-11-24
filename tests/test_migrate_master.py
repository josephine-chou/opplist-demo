"""Master phase 2: migrate-master on a small Phase 1 proposal set
."""

from __future__ import annotations

import csv

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, export_csv, migrate_master, schema_migrations

V2 = [
    # id, key, name, type, country, country_code, subdivision, parent_key, area, unlocode, iata, is_seaport, is_airport, status, note
    ("L0001", "CHICAGO, IL", "Chicago", "city", "USA", "US", "IL", "", "CHICAGO", "USCHI", "", "0", "0", "active", ""),
    ("L0002", "ORD", "Chicago O'Hare", "airport", "USA", "US", "IL", "CHICAGO, IL", "CHICAGO", "USORD", "ORD", "0", "1", "active", ""),
    ("L0003", "SAN ANTONIO", "San Antonio", "city", "Chile", "CL", "", "", "", "CLSAI", "", "1", "0", "active", ""),
    ("L0004", "NINGBO", "Ningbo", "city", "China", "CN", "", "", "", "CNNGB", "", "1", "0", "active", ""),
    ("L0005", "MIA", "Miami International", "airport", "USA", "US", "FL", "", "", "", "MIA", "0", "1", "to_confirm", "check"),
]
ALIASES = [
    ("CHICAGO", "CHICAGO, IL", "legacy_key"), ("CHI", "CHICAGO, IL", "code"),
    ("USORD", "ORD", "unlocode"), ("SAI", "SAN ANTONIO", "legacy_key"),
    ("NGB", "NINGBO", "legacy_key"),
]
AREAS = [("CHICAGO", "USA", "US", "CHICAGO, IL | ORD", "")]
MAP = [
    # old_key, action, new_key, evidence
    ("CHICAGO", "map", "CHICAGO, IL", "US city key ends in the state"),
    ("SAI", "alias", "SAN ANTONIO", "port that is its own town"),
    ("NGB", "alias", "NINGBO", "UN/LOCODE CNNGB"),
    ("MIA", "keep", "MIA", "IATA airport"),
    ("LAX", "keep", "LAX", "not in this sample's v2 on purpose"),
    ("USA", "drop", "", "country roll-up, not a place; the enquiry keeps its country field"),
    ("ZZZ", "decide", "", "no source"),
]
V2_COLUMNS = ["id", "key", "name", "type", "country", "country_code", "subdivision", "parent_key",
              "area", "unlocode", "iata", "is_seaport", "is_airport", "status", "usage_origin",
              "usage_dest", "created", "old_keys", "confidence", "evidence", "note"]
PARTY_COLUMNS = ["party_id", "key", "type", "source", "proposed_name", "merge_into", "roles", "country",
                 "proposed_country", "proposed_group", "group_confidence", "domains", "requester_n",
                 "vendor_asked", "vendor_quoted", "awarded", "events", "contacts", "evidence",
                 "confidence", "flags", "question"]


def _write(path, columns, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL)
        w.writerow(columns)
        for row in rows:
            w.writerow(row)


def make_prep(folder, party_rows, decisions, extra_map=()):
    folder.mkdir(parents=True, exist_ok=True)
    rows = []
    for r in V2:
        rows.append(list(r[:14]) + ["0", "0", "no", "", "high", "", r[14]])
    _write(folder / "location-v2-proposed.csv", V2_COLUMNS, rows)
    _write(folder / "location-alias-proposed.csv", ["alias", "location_key", "kind", "evidence"],
           [list(a) + [""] for a in ALIASES])
    _write(folder / "area-proposed.csv", ["area", "country", "country_code", "members", "missing_members"], AREAS)
    _write(folder / "location-map-proposed.csv",
           ["old_key", "usage_origin", "usage_dest", "action", "new_key", "new_name", "new_type",
            "country_code", "subdivision", "parent_city", "area", "alias_kind", "confidence", "evidence", "question"],
           [[m[0], "0", "0", m[1], m[2], "", "", "", "", "", "", "", "high", m[3], ""] for m in list(MAP) + list(extra_map)])
    _write(folder / "party-clean-proposed.csv", PARTY_COLUMNS, party_rows)
    _write(folder / "party-merge-candidates.csv",
           ["party_id_a", "key_a", "country_a", "party_id_b", "key_b", "country_b", "why", "verdict",
            "keep_id", "merge_id", "group_key", "note"], [])
    _write(folder / "party-decisions.csv",
           ["party_id", "proposed_name", "merge_into", "roles", "country", "group", "note"], decisions)
    return folder


def party_row(pid, key, ptype="Agent", roles="agent", **extra):
    row = {c: "" for c in PARTY_COLUMNS}
    row.update({"party_id": str(pid), "key": key, "type": ptype, "source": "xlsx", "roles": roles,
                "country": "USA", "confidence": "high"})
    row.update(extra)
    return [row[c] for c in PARTY_COLUMNS]


@pytest.fixture
def world(repo, tmp_path):
    """A database the way it is before the migration, and the proposals."""
    seed_reference(repo)
    conn = repo.conn
    repo.insert("location", {"key": "CHICAGO", "country": "USA", "type": "City"})
    repo.insert("location", {"key": "USA", "country": "USA", "type": "Country"})
    repo.insert("location", {"key": "ZZZ", "country": "Unknown", "type": "City", "status": "To Confirm"})
    tolvex = add_party(repo, "Tolvex", country="Chile")                       # 1
    dup = add_party(repo, "Tolvex Chile", country="Chile", placeholder=1, ptype="Unknown")   # 2
    kul = add_party(repo, "JOJO JJK", ptype="Unknown", country="Unknown")   # 3
    minted = add_party(repo, "Qatarairways", ptype="Carrier", country="Unknown")   # 4
    marvis_cl = add_party(repo, "Marvis Chile", country="Chile")                # 5
    marvis_pe = add_party(repo, "Marvis Peru", country="Chile")                 # 6
    late = add_party(repo, "Kvintertrans", country="USA")                     # 7: not in party-clean
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": dup})
    e1 = add_enquiry(repo, "OPP-0001", tolvex, received="2026-08-01",
                     origin_loc="CHICAGO", origin_country="USA", dest_loc="SAI", dest_country="Chile")
    e2 = add_enquiry(repo, "OPP-0002", dup, received="2026-08-02",
                     origin_loc="USA", origin_country="USA", dest_loc="ZZZ", dest_country="Unknown",
                     system_notes="registered from mail")
    e3 = add_enquiry(repo, "OPP-0003", tolvex, received="2026-08-03",
                     origin_loc="NGB", origin_country="China", dest_loc="MIA", dest_country="USA")
    e4 = add_enquiry(repo, "OPP-0004", tolvex, received="2026-08-04", origin_country="USA")
    repo.commit()
    party_rows = [
        party_row(tolvex, "Tolvex"),
        party_row(dup, "Tolvex Chile", "Unknown", "unknown"),
        party_row(kul, "JOJO JJK", "Unknown", "own_office|unknown", flags="type_fix:Office"),
        party_row(minted, "Qatarairways", "Carrier", "carrier_air", proposed_name="Qatar Airways",
                  proposed_country="Qatar", country="Unknown"),
        party_row(marvis_cl, "Marvis Chile", roles="agent", proposed_group="MARVIS", group_confidence="high"),
        party_row(marvis_pe, "Marvis Peru", roles="agent", proposed_group="MARVIS", group_confidence="high"),
    ]
    decisions = [[str(dup), "", str(tolvex), "", "", "", "same company; test"]]
    # Qatar is not in the sample's country table until it is added
    repo.insert("country", {"name": "Qatar", "region": "Middle East & Africa"})
    repo.commit()
    prep = make_prep(tmp_path / "master-prep", party_rows, decisions)
    return {"repo": repo, "conn": conn, "db": tmp_path / "test.db", "prep": prep,
            "ids": {"tolvex": tolvex, "dup": dup, "kul": kul, "minted": minted,
                    "marvis_cl": marvis_cl, "marvis_pe": marvis_pe, "late": late,
                    "e1": e1, "e2": e2, "e3": e3, "e4": e4}}


def test_the_schema_is_v14_with_the_master_tables(conn):
    assert schema_migrations.current_version(conn) == schema_migrations.CURRENT_VERSION >= 14
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"master_area", "master_location", "master_location_alias", "party_group"} <= tables
    assert {"origin_loc_id", "dest_loc_id", "origin_loc_legacy", "dest_loc_legacy"} <= {
        r[1] for r in conn.execute("PRAGMA table_info(enquiry)")}
    assert {"group_id", "roles"} <= {r[1] for r in conn.execute("PRAGMA table_info(party)")}
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'enquiry'").fetchone()[0]
    assert "REFERENCES location" not in sql
    assert db.get_setting(conn, "master_contract_version") == "1"
    views = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'")}
    assert {"v_master_location", "v_master_alias", "v_master_party"} <= views


def test_dry_run_plans_and_writes_nothing(world):
    world["conn"].close()
    plan, report = migrate_master.dry_run(world["db"], world["prep"])
    assert report.exists() and "nothing was written" in report.read_text(encoding="utf-8")
    c = plan.counts()
    assert c["master_location"] == 5 and c["master_location_alias"] == 5 and c["master_area"] == 1
    assert c["actions"] == {"map": 1, "alias": 2, "keep": 1, "drop": 1, "decide": 1}
    assert c["resolved"] == 4 and c["dropped"] == 1 and c["pending"] == 1
    assert plan.opp_index_changed == 3 and plan.opp_index_same == 1     # OPP-0004 has no place
    assert c["party"] == {"roles": 5, "type": 1, "merge": 1, "group": 2, "full_name": 1,
                          "country": 1, "default_roles": 1}
    assert not plan.problems
    conn = db.connect(world["db"])
    assert conn.execute("SELECT type FROM sqlite_master WHERE name = 'location'").fetchone()[0] == "table"
    assert conn.execute("SELECT COUNT(*) FROM master_location").fetchone()[0] == 0
    conn.close()


def test_a_bad_proposal_blocks_the_apply(world):
    world["conn"].close()
    # a map row pointing at a key that is not in v2
    make_prep(world["prep"], [], [], extra_map=[("MIA", "map", "NOWHERE", "typo")])
    plan, _ = migrate_master.dry_run(world["db"], world["prep"])
    assert any("NOWHERE" in p for p in plan.problems)
    with pytest.raises(FileNotFoundError):
        migrate_master.read_inputs(world["prep"].parent / "missing")


@pytest.fixture
def applied(world, tmp_path):
    world["conn"].close()
    plan, report, result, checks = migrate_master.apply(
        world["db"], world["prep"], restore_dir=tmp_path / "rp", export=False, today="2026-09-16")
    conn = db.connect(world["db"])
    world.update({"plan": plan, "report": report, "result": result, "checks": checks, "conn": conn})
    yield world
    conn.close()


def test_apply_moves_the_locations_and_points_the_enquiries_at_them(applied):
    conn, ids = applied["conn"], applied["ids"]
    rows = {r["key"]: r for r in conn.execute("SELECT * FROM master_location")}
    assert [rows[k]["id"] for k in ("CHICAGO, IL", "ORD", "SAN ANTONIO", "NINGBO", "MIA")] == [1, 2, 3, 4, 5]
    assert rows["ORD"]["parent_id"] == 1 and rows["ORD"]["area_id"] == 1 and rows["MIA"]["status"] == "to_confirm"
    assert conn.execute("SELECT name, country_code FROM master_area").fetchone()[:] == ("Chicago", "US")
    assert conn.execute("SELECT COUNT(*) FROM master_location_alias").fetchone()[0] == 5

    e1 = conn.execute("SELECT * FROM enquiry WHERE id = ?", (ids["e1"],)).fetchone()
    assert (e1["origin_loc_legacy"], e1["origin_loc"], e1["origin_loc_id"]) == ("CHICAGO", "CHICAGO, IL", 1)
    assert (e1["dest_loc_legacy"], e1["dest_loc"], e1["dest_loc_id"]) == ("SAI", "SAN ANTONIO", 3)
    # dropped: country stays, text cleared, note says why; decide: text kept
    e2 = conn.execute("SELECT * FROM enquiry WHERE id = ?", (ids["e2"],)).fetchone()
    assert e2["origin_loc"] is None and e2["origin_loc_id"] is None and e2["origin_loc_legacy"] == "USA"
    assert e2["origin_country"] == "USA"
    assert e2["dest_loc"] == "ZZZ" and e2["dest_loc_id"] is None
    assert e2["system_notes"].splitlines() == [
        "registered from mail",
        "master: USA dropped ({})".format(MAP[5][3][:migrate_master.EVIDENCE_CHARS]),
        "master: ZZZ pending decision"]
    e3 = conn.execute("SELECT * FROM enquiry WHERE id = ?", (ids["e3"],)).fetchone()
    assert (e3["origin_loc"], e3["origin_loc_id"], e3["dest_loc"], e3["dest_loc_id"]) == ("NINGBO", 4, "MIA", 5)
    e4 = conn.execute("SELECT * FROM enquiry WHERE id = ?", (ids["e4"],)).fetchone()
    assert e4["origin_loc_legacy"] is None and e4["origin_loc_id"] is None
    # every write went through change_log
    logged = conn.execute("SELECT COUNT(*) FROM change_log WHERE table_name = 'master_location' "
                          "AND field = '(insert)'").fetchone()[0]
    assert logged == 5
    assert conn.execute("SELECT COUNT(*) FROM change_log WHERE table_name = 'enquiry' "
                        "AND field = 'origin_loc_id'").fetchone()[0] == 2      # OPP-0002 stays NULL


def test_the_old_table_stays_and_location_becomes_a_view(applied):
    conn = applied["conn"]
    kinds = dict(conn.execute("SELECT name, type FROM sqlite_master WHERE name IN ('location','location_old')"))
    assert kinds == {"location": "view", "location_old": "table"}
    assert conn.execute("SELECT COUNT(*) FROM location_old").fetchone()[0] == 9   # the six seeded + three
    assert conn.execute("SELECT COUNT(*) FROM location").fetchone()[0] == 5
    assert conn.execute("SELECT * FROM location WHERE key = 'CHICAGO'").fetchone() is None
    row = conn.execute("SELECT * FROM location WHERE key = 'CHICAGO, IL'").fetchone()
    assert dict(row) == {
        "key": "CHICAGO, IL", "official_name": "Chicago", "type": "City", "country": "USA",
        "status": "OK", "notes": None, "code_system": "UN/LOCODE", "code_official": "USCHI",
        "parent_city": None, "is_seaport": 0, "is_airport": 0, "aliases": "CHI, CHICAGO",
        "created_at": row["created_at"]}
    ord_ = conn.execute("SELECT * FROM location WHERE key = 'ORD'").fetchone()
    assert (ord_["type"], ord_["code_system"], ord_["code_official"], ord_["parent_city"]) == \
        ("Airport", "IATA", "ORD", "Chicago")
    assert conn.execute("SELECT status FROM location WHERE key = 'MIA'").fetchone()[0] == "To Confirm"
    assert conn.execute("SELECT location_id FROM master_location_alias WHERE alias = 'CHICAGO'").fetchone()[0] == \
        conn.execute("SELECT id FROM master_location WHERE key = 'CHICAGO, IL'").fetchone()[0]
    backup = applied["prep"] / "location_old_backup.csv"
    text = backup.read_text(encoding="utf-8-sig")
    assert text.startswith('"key","official_name"') and '"ZZZ"' in text


def test_apply_changes_the_party_rows_as_decided(applied):
    conn, ids = applied["conn"], applied["ids"]
    party = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM party")}
    assert party[ids["tolvex"]]["roles"] == "agent"
    assert party[ids["kul"]]["roles"] == "own_office"          # 'unknown' dropped
    assert party[ids["kul"]]["type"] == "Office"
    assert party[ids["late"]]["roles"] == "agent"                   # default from the type
    assert party[ids["minted"]]["full_name"] == "Qatar Airways"     # was equal to the key
    assert party[ids["minted"]]["country"] == "Qatar"
    assert party[ids["dup"]]["merged_into"] == ids["tolvex"]
    assert conn.execute("SELECT requester_party_id FROM enquiry WHERE id = ?", (ids["e2"],)).fetchone()[0] == ids["tolvex"]
    assert conn.execute("SELECT party_id FROM party_domain WHERE domain = 'tolvex.example'").fetchone()[0] == ids["tolvex"]
    group = conn.execute("SELECT id, key, name FROM party_group").fetchone()
    assert tuple(group) == (1, "MARVIS", "MARVIS")
    assert party[ids["marvis_cl"]]["group_id"] == 1 and party[ids["marvis_pe"]]["group_id"] == 1
    assert party[ids["tolvex"]]["group_id"] is None
    contract = {r["key"]: dict(r) for r in conn.execute("SELECT * FROM v_master_party")}
    assert "Tolvex Chile" not in contract and contract["Marvis Chile"]["group_key"] == "MARVIS"
    assert contract["Qatarairways"]["name"] == "Qatar Airways"


def test_the_exports_only_change_in_the_text_columns(applied):
    conn = applied["conn"]
    rows = {r[0]: r for r in export_csv.opp_index_rows(conn)}
    assert rows["OPP-0001"][4:6] == ("CHICAGO, IL", "SAN ANTONIO")
    assert rows["OPP-0002"][4:6] == ("", "ZZZ")
    assert rows["OPP-0003"][4:6] == ("NINGBO", "MIA")
    assert applied["plan"].opp_index_changed == 3
    assert applied["plan"].agent_changes == [("OPP-0002", "Tolvex Chile", "Tolvex")]
    assert rows["OPP-0002"][2] == "Tolvex"
    checks = applied["checks"]
    failed = [c.text for c in checks if not c.ok]
    assert failed == ["8.1 values still pending a decision: 1"]      # ZZZ, by design
    assert any("8.4 opp-index.csv: 4 rows, 3 changed" in c.text and
               "merge: OPP-0002 (Tolvex Chile -> Tolvex)" in c.text for c in checks)
    text = applied["report"].read_text(encoding="utf-8")
    assert "## Result" in text and "PASS 8.7 restore point" in text
    assert applied["result"]["restore point"].endswith(".db")


def test_a_second_apply_is_a_no_op(applied, tmp_path):
    conn = applied["conn"]
    before = conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
    conn.close()
    plan, report, result, checks = migrate_master.apply(
        applied["db"], applied["prep"], restore_dir=tmp_path / "rp2", export=False)
    conn2 = db.connect(applied["db"])
    applied["conn"] = conn2
    assert plan.nothing_to_do and not plan.problems
    assert conn2.execute("SELECT COUNT(*) FROM change_log").fetchone()[0] == before
    assert conn2.execute("SELECT COUNT(*) FROM master_location").fetchone()[0] == 5
    assert plan.enquiries_done == 6 and plan.enquiries == []
    assert [c.text for c in checks if not c.ok] == ["8.1 values still pending a decision: 1"]


def test_after_the_switch_the_repository_writes_master_locations(applied):
    conn = applied["conn"]
    repo = db.Repository(conn, source="test")
    assert db.master_active(conn)
    # a string that already names a place, by key or by alias
    assert repo.add_location("chicago") == "CHICAGO, IL"
    assert repo.add_location("Ningbo", country="China") == "NINGBO"
    # a new one lands in master_location, to_confirm, from intake, and not in location_old
    key = repo.add_location("Cuautitlán Izcalli", country="Unknown", notes="from a subject")
    assert key == "CUAUTITLAN IZCALLI"
    row = conn.execute("SELECT * FROM master_location WHERE key = ?", (key,)).fetchone()
    assert (row["status"], row["source"], row["type"], row["name"], row["note"]) == \
        ("to_confirm", "intake", "city", "Cuautitlán Izcalli", "from a subject")
    assert conn.execute("SELECT alias, kind FROM master_location_alias WHERE location_id = ?",
                        (row["id"],)).fetchone()[:] == ("CUAUTITLÁN IZCALLI", "manual")
    assert conn.execute("SELECT COUNT(*) FROM location_old WHERE key = ?", (key,)).fetchone()[0] == 0
    assert conn.execute("SELECT status FROM location WHERE key = ?", (key,)).fetchone()[0] == "To Confirm"
    # an enquiry written with text gets the id, and an id writes the text
    party = conn.execute("SELECT id FROM party WHERE key = 'Tolvex'").fetchone()[0]
    case = repo.create_enquiry({"requester_party_id": party, "received_on": "2026-09-16",
                                "origin_loc": "CHI", "origin_country": "USA",
                                "dest_loc": "ZZZ", "dest_country": "Unknown"}, "2026-09-16")
    row = conn.execute("SELECT * FROM enquiry WHERE id = ?", (case,)).fetchone()
    assert (row["origin_loc"], row["origin_loc_id"], row["dest_loc"], row["dest_loc_id"]) == \
        ("CHICAGO, IL", 1, "ZZZ", None)
    repo.save_enquiry(case, {"dest_loc_id": 3})
    row = conn.execute("SELECT dest_loc, dest_loc_id FROM enquiry WHERE id = ?", (case,)).fetchone()
    assert tuple(row) == ("SAN ANTONIO", 3)
    repo.save_enquiry(case, {"dest_loc_id": None})
    assert tuple(conn.execute("SELECT dest_loc, dest_loc_id FROM enquiry WHERE id = ?", (case,)).fetchone()) == (None, None)
    with pytest.raises(ValueError):
        repo.save_enquiry(case, {"dest_loc_id": 999})


def test_verify_reads_the_applied_database(applied, tmp_path):
    applied["conn"].close()
    checks = migrate_master.verify(applied["db"], applied["prep"], restore_dir=tmp_path / "rp")
    applied["conn"] = db.connect(applied["db"])
    assert [c.text for c in checks if not c.ok] == ["8.1 values still pending a decision: 1"]
    assert any(c.text.startswith("8.7 restore point opplist_before_master_") and c.ok for c in checks)


def test_verify_before_apply_says_so(world):
    world["conn"].close()
    checks = migrate_master.verify(world["db"], world["prep"])
    assert len(checks) == 1 and not checks[0].ok

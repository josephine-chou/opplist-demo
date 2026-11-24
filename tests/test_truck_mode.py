"""Batch TK1 (schema v46): the mode Domestic becomes Truck, for trucking
inside the US and across a border; whether a move is domestic is its
direction, from its countries.

Every company, place, date and number here is invented; cases from
OPP-8301.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import ack, cheatsheet, db, monthly, paths, schema_migrations, shipments
from opplist.parsing import parse_subject
from opplist.web import forms

MONTH = "2027-02"


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "truck.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    if not conn.execute("SELECT 1 FROM country WHERE name = 'Mexico'").fetchone():
        repo.insert("country", {"name": "Mexico", "region": "Latin America"})
    p = add_party(repo, "Dunsford Haulage", country="USA")
    yield conn, repo, p, path
    conn.close()


def case(repo, opp, party, mode="Truck", origin="USA", dest="Mexico", **extra):
    got = add_enquiry(repo, opp, party, received="2027-02-04", service_mode=mode, origin_country=origin,
                      dest_country=dest, **extra)
    repo.commit()
    return got


# ---------------------------------------------------------- the upgrade --
def _back_to_v45(path):
    """enquiry and enquiry_route as v45 had them: Domestic, not Truck."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA ignore_check_constraints = ON")          # the Truck rows copy, then become Domestic
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    for name in re.findall(r"CREATE VIEW\s+(\w+)\s+AS", schema_sql):
        conn.execute("DROP VIEW IF EXISTS {}".format(name))
    for table in ("enquiry", "enquiry_route"):
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()[0]
        conn.execute(sql.replace("'Truck'", "'Domestic'").replace("CREATE TABLE {}".format(table),
                                                                   "CREATE TABLE {}_old".format(table), 1))
        conn.execute("INSERT INTO {0}_old SELECT * FROM {0}".format(table))
        conn.execute("DROP TABLE {}".format(table))
        conn.execute("ALTER TABLE {0}_old RENAME TO {0}".format(table))
    conn.execute("UPDATE enquiry SET service_mode = 'Domestic' WHERE service_mode = 'Truck'")
    conn.execute("UPDATE enquiry_route SET service_mode = 'Domestic' WHERE service_mode = 'Truck'")
    conn.execute("UPDATE setting SET value = '45' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()


def test_the_upgrade_from_v45_turns_domestic_into_truck_and_keeps_every_row(world):
    conn, repo, p, path = world
    truck = case(repo, "OPP-8301", p)
    air = case(repo, "OPP-8302", p, mode="Air")
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    conn.close()
    _back_to_v45(path)
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT service_mode FROM enquiry WHERE id = ?", (truck,)).fetchone()[0] == "Domestic"
    before = {t: raw.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0] for t in tables}
    last = raw.execute("SELECT MAX(id) FROM change_log").fetchone()[0]
    raw.close()
    conn = db.open_db(path)
    try:
        assert schema_migrations.current_version(conn) == schema_migrations.CURRENT_VERSION
        after = {t: conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0] for t in tables}
        assert after["change_log"] == before["change_log"] + 2                 # the two rows below, nothing else
        assert {t: n for t, n in after.items() if t != "change_log"} == \
            {t: n for t, n in before.items() if t != "change_log"}
        assert conn.execute("SELECT service_mode FROM enquiry WHERE id = ?", (truck,)).fetchone()[0] == "Truck"
        assert conn.execute("SELECT service_mode FROM enquiry_route WHERE enquiry_id = ?", (truck,)).fetchone()[0] \
            == "Truck"
        assert conn.execute("SELECT service_mode FROM enquiry WHERE id = ?", (air,)).fetchone()[0] == "Air"
        logged = conn.execute("SELECT table_name, field, old_value, new_value, source, action_id FROM change_log "
                              "WHERE id > ? ORDER BY id", (last,)).fetchall()
        assert [tuple(r)[:5] for r in logged] == [
            ("enquiry", "service_mode", "Domestic", "Truck", "migration"),
            ("enquiry_route", "service_mode", "Domestic", "Truck", "migration")]
        assert len({r["action_id"] for r in logged}) == 1 and logged[0]["action_id"] is not None
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'enquiry'").fetchone()[0]
        assert "'Truck'" in sql and "'Domestic'" not in sql
    finally:
        conn.close()


# ------------------------------------------------------------- the mode --
def test_a_new_case_saves_as_truck_and_is_refused_as_domestic(world):
    conn, repo, p, _ = world
    assert conn.execute("SELECT service_mode FROM v_enquiry WHERE id = ?",
                        (case(repo, "OPP-8303", p),)).fetchone()[0] == "Truck"
    with pytest.raises(sqlite3.IntegrityError):
        case(repo, "OPP-8304", p, mode="Domestic")
    assert "Truck" in forms.SERVICE_MODES and "Domestic" not in forms.SERVICE_MODES
    assert "Truck" in forms.FREE_TEXT_QTY_MODES and "Domestic" not in forms.FREE_TEXT_QTY_MODES


@pytest.mark.parametrize("subject", ["TRUCKING RATE REQUEST // HOUSTON PORT", "RATE REQUEST // LTL // DENTON - OAKWOOD",
                                     "TRUCK LOAD // DENTON - OAKWOOD", "DRAYAGE // PORT OF ELLMOOR"])
def test_the_subject_suggests_truck(subject):
    assert parse_subject(subject).service_mode == "Truck"


# test_the_acknowledgement_says_trucking: FX4 (2026-11-12) took out the acknowledgment's words
# (ack.request_phrase) with its draft; nothing else of it was left to check.


# ----------------------------------------------------------- the report --
def test_a_truck_case_is_other_on_the_report_and_the_cheat_sheet(world):
    conn, repo, p, _ = world
    case(repo, "OPP-8306", p)
    case(repo, "OPP-8307", p, mode="Air")
    assert ("1 / 0 / 1", "Air / Ocean / Other") in monthly.build(conn, MONTH).kpis
    assert cheatsheet.build(conn, MONTH).glance["other"] == 1


def test_a_truck_case_without_its_destination_country_is_on_the_check_list(world):
    conn, repo, p, _ = world
    got = case(repo, "OPP-8308", p, dest=None)
    found = {g["title"]: [i["ids"][0] for i in g["items"]] for g in monthly.checks(conn, MONTH)}
    assert got in found["No origin or destination country"]


# --------------------------------------------------------- the shipment --
def test_start_shipment_from_a_truck_case_makes_a_truck_shipment(world):
    conn, repo, p, _ = world
    got = case(repo, "OPP-8309", p)
    repo.set_status(got, "Won", "2027-02-10", rate_used="own")
    sid = shipments.start(repo, got, "Denton, TX to Monterrey")
    repo.commit()
    assert conn.execute("SELECT mode FROM shipment WHERE id = ?", (sid,)).fetchone()[0] == "truck"
    assert shipments.mode_for("Truck") == "truck" and shipments.mode_for("Domestic") == "ocean"

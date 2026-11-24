"""A schema upgrade may add objects and never lose one (hotfix v25,
2026-09-25): v24 dropped every view before a table rebuild and put back
only the seven schema.sql knows, so the `location` compatibility view
migrate-master left over master_location vanished and every page that
reads `location` failed. This test takes a v23-shaped, master-migrated
file through v24 and v25 and compares sqlite_master before and after:
additions only, and the readers that broke work again."""

from __future__ import annotations

import sqlite3

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, export_xlsx, migrate_master, schema_migrations
from opplist.web import reference


def objects(conn) -> dict:
    return {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}


def as_v23_master_file(path):
    """A fresh file, then shaped the way the live one was on 25 Sep before
    the v24 upgrade: master-migrated (location renamed, the view in
    place), the shipment tables absent, the snooze CHECK without
    'shipment', schema_version 23."""
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Tolvex", country="Chile")
    add_enquiry(repo, "OPP-0001", party, received="2026-09-01", service_mode="Air",
                origin_country="USA", dest_country="Chile")
    repo.commit()
    conn.execute("INSERT INTO master_location (key, name, type, country, iata, is_airport, source) "
                 "VALUES ('MIA', 'Miami', 'airport', 'USA', 'MIA', 1, 'manual')")
    conn.execute("ALTER TABLE location RENAME TO location_old")
    conn.execute(migrate_master.LOCATION_VIEW_SQL)
    conn.execute("DROP INDEX IF EXISTS ix_shipment_event_shipment")
    conn.execute("DROP TABLE IF EXISTS shipment_event")
    conn.execute("DROP TABLE IF EXISTS shipment")
    conn.execute("CREATE TABLE tracking_snooze_v23 (id INTEGER PRIMARY KEY, rule TEXT NOT NULL, "
                 "ref_kind TEXT NOT NULL CHECK (ref_kind IN ('enquiry','party','vendor_request')), "
                 "ref_id INTEGER NOT NULL, until TEXT NOT NULL, UNIQUE (rule, ref_kind, ref_id))")
    conn.execute("INSERT INTO tracking_snooze_v23 SELECT * FROM tracking_snooze")
    conn.execute("DROP TABLE tracking_snooze")
    conn.execute("ALTER TABLE tracking_snooze_v23 RENAME TO tracking_snooze")
    conn.execute("INSERT INTO tracking_snooze (rule, ref_kind, ref_id, until) VALUES ('R1', 'enquiry', 1, '2026-10-01')")
    conn.execute("UPDATE setting SET value = '23' WHERE key = 'schema_version'")
    conn.commit()
    before = objects(conn)
    conn.close()
    return before


def test_upgrading_through_v24_and_v25_keeps_every_object(tmp_path):
    path = tmp_path / "v23.db"
    before = as_v23_master_file(path)
    assert ("view", "location") in before and ("table", "shipment") not in before
    notes = []
    conn = db.open_db(path, upgrade_notes=notes)
    assert schema_migrations.current_version(conn) == schema_migrations.CURRENT_VERSION
    after = objects(conn)
    lost = sorted(set(before) - set(after))
    assert lost == [], "the upgrade dropped {}".format(lost)
    added = sorted(set(after) - set(before))
    assert ("table", "shipment") in added and ("table", "shipment_event") in added
    # the compatibility view is there with migrate-master's definition
    assert after[("view", "location")].strip().rstrip(";") == migrate_master.LOCATION_VIEW_SQL.strip().rstrip(";")
    assert "'shipment'" in after[("table", "tracking_snooze")]
    assert conn.execute("SELECT COUNT(*) FROM tracking_snooze").fetchone()[0] == 1   # rows kept
    # the readers that failed on 25 Sep
    lists = reference(conn)
    assert lists["locations"] and lists["countries"]
    assert conn.execute("SELECT COUNT(*) FROM location").fetchone()[0] > 0
    written = export_xlsx.export(conn, tmp_path / "out" / "export.xlsx", db_path=str(path))
    assert written.exists() and written.stat().st_size > 0
    conn.close()


def test_v25_alone_recreates_the_view_v24_dropped(tmp_path):
    """The live file's exact state at 17:07: v24 applied, the view gone."""
    path = tmp_path / "broken.db"
    as_v23_master_file(path)
    conn = sqlite3.connect(path)
    conn.execute("DROP VIEW location")
    conn.execute("UPDATE setting SET value = '24' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    notes = []
    conn = db.open_db(path, upgrade_notes=notes)
    assert any("location view recreated" in n for n in notes), notes
    assert conn.execute("SELECT type FROM sqlite_master WHERE name = 'location'").fetchone()[0] == "view"
    assert reference(conn)["locations"]
    conn.close()
    # and a file that never went through migrate-master keeps its table
    fresh = tmp_path / "fresh.db"
    conn = db.open_db(fresh)
    assert conn.execute("SELECT type FROM sqlite_master WHERE name = 'location'").fetchone()[0] == "table"
    assert schema_migrations._to_v25(conn) == ["location table already there"]
    conn.close()

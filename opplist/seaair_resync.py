"""OP1 (2026-11-10): the Sea-Air cases' stored totals, re-synced once.

SA1's v48 upgrade set OPP-1554 to Sea-Air without re-syncing its totals, so
its chargeable weight (Sea-Air is charged on air weight, cargo.AIR_MODES)
was never written. On the first start of the app after OP1, every case
whose service_mode, or a live route's mode, is Sea-Air goes through
Repository.sync_cargo_totals (source migration, one action, change_log);
setting seaair_totals_resynced records that it ran, so it never runs again.
"""

from __future__ import annotations

import datetime as dt

from . import db

SETTING = "seaair_totals_resynced"


def cases(conn) -> list:
    """(id, opp_no) of every case that is Sea-Air, or has a live Sea-Air route."""
    return conn.execute(
        "SELECT id, opp_no FROM enquiry WHERE service_mode = 'Sea-Air' "
        "OR id IN (SELECT enquiry_id FROM enquiry_route WHERE service_mode = 'Sea-Air' AND voided = 0) "
        "ORDER BY opp_no").fetchall()


def run_once(conn) -> list:
    """The startup notes' line, or [] when it ran before. Commits."""
    if db.get_setting(conn, SETTING):
        return []
    repo = db.Repository(conn, source="migration")
    repo.pin_action()
    changed = []
    for row in cases(conn):
        if repo.sync_cargo_totals(row["id"]):
            after = conn.execute("SELECT air_weight_kg FROM enquiry WHERE id = ?", (row["id"],)).fetchone()
            changed.append("{} (chargeable {} kg)".format(row["opp_no"], "{:,.1f}".format(after["air_weight_kg"]))
                           if after and after["air_weight_kg"] is not None else row["opp_no"])
    db.set_setting(conn, SETTING, dt.date.today().isoformat())
    conn.commit()
    if not changed:
        return ["Sea-Air totals re-synced: no case changed."]
    return ["Sea-Air totals re-synced: {}.".format(", ".join(changed))]

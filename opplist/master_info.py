r"""The master contract: the three
v_master_* views the Rate Desk reads through a read-only ATTACH of
data\opplist.db. Columns are only ever added; a change bumps
setting.master_contract_version and the written contract.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

CONTRACT_VIEWS = ("v_master_location", "v_master_alias", "v_master_party",
                  "v_master_enquiry_lane", "v_master_vendor_request",
                  "v_master_route_lane", "v_master_route_place", "v_master_enquiry_cargo")

#: The columns each view carries at contract version 1, in order. A test
#: pins the live views to this list, so a change here is a deliberate one.
CONTRACT_V1 = {
    "v_master_location": ["id", "key", "name", "type", "country", "country_code", "subdivision",
                          "parent_id", "parent_key", "area_id", "area_key", "unlocode", "iata",
                          "is_seaport", "is_airport", "status",
                          "no_parent"],                                   # RD1 (v42)
    "v_master_alias": ["alias", "location_id", "kind"],
    "v_master_party": ["id", "key", "name", "type", "roles", "country", "city", "mfn_member",
                       "stage", "placeholder", "merged_into", "group_id", "group_key", "group_name"],
    # R0 (2026-09-17): added under version 1 - views only, nothing removed
    "v_master_enquiry_lane": ["id", "opp_no", "group_id", "received_on", "status", "scope",
                              "service_mode", "origin_loc_id", "dest_loc_id", "origin_country",
                              "dest_country", "incoterm", "requester_party_id", "awarded_vendor_id",
                              "rate_used"],
    "v_master_vendor_request": ["id", "enquiry_id", "opp_no", "vendor_party_id", "asked_on",
                                "replied_on", "quoted", "awarded", "voided",
                                "route_ids"],                             # RD1 (v42)
    # RD1 (2026-10-19, schema v42): the demand side per route, added under
    # version 1. voided rows are listed; the reader filters.
    "v_master_route_lane": ["route_id", "enquiry_id", "opp_no", "seq", "letter", "received_on",
                            "status", "scope", "service_mode", "origin_loc_id", "dest_loc_id",
                            "origin_country", "dest_country", "incoterm", "service_scope", "pod",
                            "result", "result_on", "requester_party_id", "awarded_vendor_id",
                            "voided"],
    "v_master_route_place": ["route_id", "side", "seq", "location_id", "voided"],
    "v_master_enquiry_cargo": ["id", "enquiry_id", "opp_no", "seq", "route_ids", "counted", "voided",
                               "pieces", "pack_unit", "weight_kg", "cbm", "container_type",
                               "container_qty", "per_month", "months"],
}


def describe(conn: sqlite3.Connection, schema: str = "main") -> dict:
    """{'version': n, 'views': {name: {'columns': [...], 'rows': n}}} for the
    database behind `schema` ('main', or the name it was ATTACHed as)."""
    prefix = "{}.".format(schema) if schema and schema != "main" else ""
    row = conn.execute(
        "SELECT value FROM {}setting WHERE key = 'master_contract_version'".format(prefix)).fetchone()
    out = {"version": int(row[0]) if row else None, "views": {}}
    for name in CONTRACT_VIEWS:
        exists = conn.execute(
            "SELECT 1 FROM {}sqlite_master WHERE type = 'view' AND name = ?".format(prefix), (name,)).fetchone()
        if exists is None:
            out["views"][name] = None
            continue
        columns = [r[1] for r in conn.execute("PRAGMA {}table_info({})".format(prefix, name))]
        rows = conn.execute("SELECT COUNT(*) FROM {}{}".format(prefix, name)).fetchone()[0]
        out["views"][name] = {"columns": columns, "rows": rows}
    return out


def lines(info: dict, db_path=None) -> list:
    out = []
    if db_path:
        out.append("database : {}".format(db_path))
    out.append("contract : master_contract_version {}".format(info["version"]))
    for name, view in info["views"].items():
        if view is None:
            out.append("{:<24} MISSING".format(name))
            continue
        out.append("{:<24} {:>5} rows  {:>2} columns: {}".format(
            name, view["rows"], len(view["columns"]), ", ".join(view["columns"])))
    return out


def open_attached(db_path) -> sqlite3.Connection:
    """The way another app reads the master: an in-memory connection with
    opplist.db ATTACHed read only as `opp`. Nothing here can write."""
    # uri=True on the connection is what lets ATTACH take a 'file:...?mode=ro'
    # name; without it the URI is read as a plain file name and refused.
    conn = sqlite3.connect("file::memory:", uri=True)
    uri = "file:{}?mode=ro".format(Path(db_path).resolve().as_posix())
    conn.execute("ATTACH DATABASE ? AS opp", (uri,))
    return conn

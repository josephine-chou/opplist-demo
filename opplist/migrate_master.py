r"""The master data migration: the master_* tables, the
location v2 data, enquiry location ids, and the party roles / groups / merges.

`--dry-run` reads the database read only and the Phase 1 proposals in
data\master-prep\, and writes migrate-master-report.md next to them. The CSV
actions are the decisions: nothing here re-derives them (section 4).
`--apply` takes a restore point, brings the schema to v14, and runs the same
plan in one transaction (section 4.1); `--verify` prints the section 8 checks.

Decisions taken on 2026-09-16:
proposed_name also overwrites a full_name that merely repeats the key; the
roles token 'unknown' is dropped (an empty roles stays empty); a party that
is not in party-clean gets a default role from its type (Agent -> agent,
Office -> own_office, Direct Customer -> direct_customer;
Carrier and Unknown stay empty).
"""

from __future__ import annotations

import csv
import datetime as dt
import sqlite3
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from . import export_csv, paths

PREP_DIR = paths.DATA_DIR / "master-prep"
REPORT_NAME = "migrate-master-report.md"
LOCATION_OLD_BACKUP = "location_old_backup.csv"

#: The Phase 1 files the migration reads (section 4). The two decision
#: files are read when they exist.
INPUTS = OrderedDict((
    ("v2", "location-v2-proposed.csv"),
    ("alias", "location-alias-proposed.csv"),
    ("area", "area-proposed.csv"),
    ("map", "location-map-proposed.csv"),
    ("party", "party-clean-proposed.csv"),
    ("merge", "party-merge-candidates.csv"),
))
OPTIONAL_INPUTS = OrderedDict((
    ("party_decisions", "party-decisions.csv"),
    ("location_decisions", "location-decisions.csv"),
))

#: Phase 1 summary numbers the report is checked against (section 8).
PHASE1_EXPECTED = {"master_location": 264, "master_location_alias": 239,
                   "master_area": 14, "resolved": 746, "dropped": 38, "pending": 0}

LOCATION_TYPES = ("city", "airport", "seaport", "ramp")
ALIAS_KINDS = ("code", "iata_city", "unlocode", "legacy_key", "manual")
ROLES = ("agent", "carrier_air", "carrier_ocean", "coloader", "trucker", "broker",   # Q2c: coloader
         "warehouse", "direct_customer", "own_office")
DROPPED_ROLE_TOKENS = ("unknown",)
DEFAULT_ROLE_BY_TYPE = {"Agent": "agent", "Office": "own_office",
                        "Direct Customer": "direct_customer"}
TYPE_FIX_FLAG = "type_fix:Office"
OWN_OFFICE = "Office"
RESOLVING_ACTIONS = ("keep", "map", "alias", "create")
EVIDENCE_CHARS = 60
DROPPED_NOTE = "master: {} dropped ({})"
PENDING_NOTE = "master: {} pending decision"

#: Rows of the dropped party that a merge moves (Repository.merge_party).
MERGE_MOVES = (("enquiry", "requester_party_id"), ("enquiry", "awarded_vendor_id"),
               ("vendor_request", "vendor_party_id"), ("contact", "party_id"),
               ("party_event", "party_id"), ("party_domain", "party_id"))

#: The compatibility view (section 4.3): the old location table's 13 columns
#: read off master_location, so every old reader keeps working. Airports show
#: their IATA code, everything else its UN/LOCODE; a retired place reads 'OK'
#: (nothing is retired yet; the Locations page decides how to show them).
LOCATION_VIEW_SQL = """
CREATE VIEW location AS
SELECT l.key                                                        AS key,
       l.name                                                       AS official_name,
       CASE l.type WHEN 'airport' THEN 'Airport' WHEN 'seaport' THEN 'Seaport'
                   ELSE 'City' END                                  AS type,
       l.country                                                    AS country,
       CASE l.status WHEN 'to_confirm' THEN 'To Confirm' ELSE 'OK' END AS status,
       l.note                                                       AS notes,
       CASE WHEN l.type = 'airport' AND COALESCE(l.iata, '') <> '' THEN 'IATA'
            WHEN COALESCE(l.unlocode, '') <> '' THEN 'UN/LOCODE'
            WHEN COALESCE(l.iata, '') <> '' THEN 'IATA'
            ELSE 'Unresolved' END                                   AS code_system,
       CASE WHEN l.type = 'airport' AND COALESCE(l.iata, '') <> '' THEN l.iata
            WHEN COALESCE(l.unlocode, '') <> '' THEN l.unlocode
            WHEN COALESCE(l.iata, '') <> '' THEN l.iata
            ELSE NULL END                                           AS code_official,
       p.name                                                       AS parent_city,
       l.is_seaport                                                 AS is_seaport,
       l.is_airport                                                 AS is_airport,
       (SELECT group_concat(alias, ', ') FROM (
            SELECT alias FROM master_location_alias a
            WHERE a.location_id = l.id ORDER BY alias))             AS aliases,
       l.created_at                                                 AS created_at
FROM master_location l
LEFT JOIN master_location p ON p.id = l.parent_id;
"""


# ------------------------------------------------------------------ inputs --
def read_csv(path: Path) -> list:
    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(fh)]


def read_inputs(prep_dir=None) -> dict:
    """Every proposal file, keyed by its short name. Missing required files
    are reported together rather than one at a time."""
    prep_dir = Path(prep_dir or PREP_DIR)
    missing = [name for name in INPUTS.values() if not (prep_dir / name).exists()]
    if missing:
        raise FileNotFoundError("missing in {}: {}".format(prep_dir, ", ".join(missing)))
    inputs = {key: read_csv(prep_dir / name) for key, name in INPUTS.items()}
    for key, name in OPTIONAL_INPUTS.items():
        inputs[key] = read_csv(prep_dir / name) if (prep_dir / name).exists() else []
    inputs["_dir"] = prep_dir
    return inputs


def v2_id(text: str) -> int:
    """'L0001' -> 1: the CSV order becomes the master_location id."""
    text = (text or "").strip()
    digits = text[1:] if text[:1].upper() == "L" else text
    return int(digits)


def clean_roles(text: str) -> str:
    """The roles column as it is written: the CSV's tokens minus 'unknown'."""
    return "|".join(t for t in (text or "").split("|") if t and t not in DROPPED_ROLE_TOKENS)


# -------------------------------------------------------------------- plan --
@dataclass
class LocationRow:
    id: int
    key: str
    name: str
    type: str
    country: str
    country_code: str | None
    subdivision: str | None
    parent_key: str | None
    area_key: str | None
    unlocode: str | None
    iata: str | None
    is_seaport: int
    is_airport: int
    status: str
    note: str | None
    exists: bool = False          # already in master_location (re-run)


@dataclass
class EnquiryChange:
    enquiry_id: int
    opp_no: str
    side: str                     # origin / dest
    old_key: str
    action: str                   # keep / map / alias / create / decided / drop / decide / unmapped
    new_key: str | None           # what the text column will hold (None = cleared)
    new_id: int | None
    note: str | None              # line appended to system_notes
    country: str | None
    evidence: str = ""

    @property
    def text_changed(self) -> bool:
        return (self.new_key or "") != (self.old_key or "")

    @property
    def resolved(self) -> bool:
        return self.new_id is not None


@dataclass
class PartyChange:
    item: str                     # roles / type / merge / group / full_name / country / default_roles
    party_id: int
    key: str
    old: str | None
    new: str | None
    detail: str = ""
    target_id: int | None = None  # merge: the surviving party


@dataclass
class Plan:
    db_path: Path
    prep_dir: Path
    schema_version: int
    master_active: bool           # 'location' is already the compatibility view
    areas: list = field(default_factory=list)
    locations: list = field(default_factory=list)
    aliases: list = field(default_factory=list)          # (alias, location_key, kind, exists)
    enquiries: list = field(default_factory=list)
    enquiries_done: int = 0                              # sides already carrying a legacy value
    party: list = field(default_factory=list)
    party_groups: list = field(default_factory=list)     # (key, name, exists)
    agent_changes: list = field(default_factory=list)    # (opp_no, old requester key, new key): a merge moved the case
    full_name_skipped: list = field(default_factory=list)
    problems: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    opp_index_changed: int = 0
    opp_index_same: int = 0

    # -- counts the report and the tests read ----------------------------
    def counts(self) -> dict:
        actions = Counter(c.action for c in self.enquiries)
        return {
            "master_area": sum(1 for a in self.areas if not a["exists"]),
            "master_location": sum(1 for l in self.locations if not l.exists),
            "master_location_alias": sum(1 for a in self.aliases if not a[3]),
            "party_group": sum(1 for g in self.party_groups if not g[2]),
            "enquiry_values": len(self.enquiries),
            "resolved": sum(1 for c in self.enquiries if c.resolved),
            "dropped": sum(1 for c in self.enquiries if c.action in ("drop", "decided") and not c.resolved),
            "pending": sum(1 for c in self.enquiries if c.action in ("decide", "unmapped")),
            "text_changed": sum(1 for c in self.enquiries if c.text_changed),
            "actions": dict(actions),
            "party": dict(Counter(p.item for p in self.party)),
        }

    @property
    def blocked(self) -> bool:
        return bool(self.problems)

    @property
    def nothing_to_do(self) -> bool:
        c = self.counts()
        return (c["master_area"] == 0 and c["master_location"] == 0
                and c["master_location_alias"] == 0 and not self.enquiries
                and not self.party and c["party_group"] == 0 and self.master_active)


def _columns(conn, table) -> set:
    return {r[1] for r in conn.execute("PRAGMA table_info({})".format(table))}


def _tables(conn) -> dict:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT name, type FROM sqlite_master WHERE type IN ('table','view')")}


def _none(value: str) -> str | None:
    return value if value not in (None, "") else None


def build_plan(conn: sqlite3.Connection, inputs: dict, db_path=None) -> Plan:
    """What apply would write, from the database as it is and the CSVs."""
    objects = _tables(conn)
    row = conn.execute("SELECT value FROM setting WHERE key = 'schema_version'").fetchone()
    plan = Plan(db_path=Path(db_path or ""), prep_dir=Path(inputs.get("_dir", "")),
                schema_version=int(row[0]) if row else 1,
                master_active=objects.get("location") == "view")
    countries = {r[0] for r in conn.execute("SELECT name FROM country")}

    _plan_locations(conn, inputs, plan, objects, countries)
    _plan_enquiries(conn, inputs, plan)
    _plan_party(conn, inputs, plan, objects, countries)
    _plan_opp_index(conn, plan)
    return plan


def _plan_locations(conn, inputs, plan, objects, countries) -> None:
    existing_locations = set()
    if "master_location" in objects:
        existing_locations = {r[0] for r in conn.execute("SELECT key FROM master_location")}
    existing_areas = set()
    if "master_area" in objects:
        existing_areas = {r[0] for r in conn.execute("SELECT key FROM master_area")}
    existing_aliases = set()
    if "master_location_alias" in objects:
        existing_aliases = {(r[0], r[1]) for r in conn.execute(
            "SELECT a.alias, l.key FROM master_location_alias a "
            "JOIN master_location l ON l.id = a.location_id")}

    rows = sorted(inputs["v2"], key=lambda r: v2_id(r["id"]))
    keys = {}
    for r in rows:
        key = r["key"].strip().upper()
        if key in keys:
            plan.problems.append("location-v2: key {} appears twice".format(key))
            continue
        if r["type"] not in LOCATION_TYPES:
            plan.problems.append("location-v2 {}: type {!r} is not one of {}".format(
                key, r["type"], "/".join(LOCATION_TYPES)))
        if r["country"] not in countries:
            plan.problems.append("location-v2 {}: country {!r} is not in the country table".format(
                key, r["country"]))
        loc = LocationRow(
            id=v2_id(r["id"]), key=key, name=r["name"] or key, type=r["type"],
            country=r["country"], country_code=_none(r["country_code"]),
            subdivision=_none(r["subdivision"]), parent_key=_none(r["parent_key"]),
            area_key=_none(r["area"]), unlocode=_none(r["unlocode"]), iata=_none(r["iata"]),
            is_seaport=1 if r["is_seaport"] == "1" else 0,
            is_airport=1 if r["is_airport"] == "1" else 0,
            status="to_confirm" if r["status"] == "to_confirm" else "active",
            note=_none(r["note"]), exists=key in existing_locations)
        keys[key] = loc
        plan.locations.append(loc)
    for loc in plan.locations:
        if loc.parent_key and loc.parent_key not in keys:
            plan.problems.append("location-v2 {}: parent {} is not a v2 key".format(loc.key, loc.parent_key))

    names = {loc.key: loc.name for loc in plan.locations}
    area_keys = set()
    for r in inputs["area"]:
        key = r["area"].strip().upper()
        area_keys.add(key)
        plan.areas.append({"key": key, "name": names.get(key, key.title()),
                           "country_code": r["country_code"], "exists": key in existing_areas})
    for loc in plan.locations:
        if loc.area_key and loc.area_key not in area_keys:
            plan.problems.append("location-v2 {}: area {} is not in area-proposed".format(loc.key, loc.area_key))

    seen = set()
    for r in inputs["alias"]:
        alias, target, kind = r["alias"].strip().upper(), r["location_key"].strip().upper(), r["kind"]
        if (alias, target) in seen:
            continue
        seen.add((alias, target))
        if target not in keys:
            plan.problems.append("location-alias {} -> {}: not a v2 key".format(alias, target))
            continue
        if kind not in ALIAS_KINDS:
            plan.problems.append("location-alias {} -> {}: kind {!r} unknown".format(alias, target, kind))
        plan.aliases.append((alias, target, kind, (alias, target) in existing_aliases))
    plan._v2_ids = {loc.key: loc.id for loc in plan.locations}


def _plan_enquiries(conn, inputs, plan) -> None:
    mapping = {r["old_key"].strip().upper(): r for r in inputs["map"]}
    decided = {r["old_key"].strip().upper(): r for r in inputs["location_decisions"]
               if r.get("old_key") and r.get("action")}
    has_legacy = "origin_loc_legacy" in _columns(conn, "enquiry")
    v2 = plan._v2_ids
    select = "SELECT id, opp_no, origin_loc, dest_loc, origin_country, dest_country{} FROM enquiry " \
             "ORDER BY CAST(substr(opp_no, 5) AS INTEGER)".format(
                 ", origin_loc_legacy, dest_loc_legacy" if has_legacy else "")
    for e in conn.execute(select):
        for side in ("origin", "dest"):
            if has_legacy and e["{}_loc_legacy".format(side)] is not None:
                plan.enquiries_done += 1
                continue
            old = e["{}_loc".format(side)]
            if old is None or old.strip() == "":
                continue
            key = old.strip().upper()
            country = e["{}_country".format(side)]
            m = mapping.get(key)
            d = decided.get(key)
            if m is None:
                plan.enquiries.append(EnquiryChange(
                    e["id"], e["opp_no"], side, old, "unmapped", old, None,
                    "master: {} not in location-map (pending decision)".format(key), country))
                continue
            action, new_key, evidence = m["action"], m["new_key"].strip().upper(), m["evidence"]
            if action == "decide" and d is not None:
                # A decision written after the last propose run counts as decided.
                action, new_key = "decided", d.get("new_key", "").strip().upper()
                evidence = "decided: {} -> {}".format(d["action"], new_key or "(drop)")
            if action in RESOLVING_ACTIONS or (action == "decided" and new_key):
                if new_key not in v2:
                    plan.problems.append("{} {}: {} -> {} but {} is not a v2 key".format(
                        e["opp_no"], side, key, new_key, new_key))
                    continue
                plan.enquiries.append(EnquiryChange(
                    e["id"], e["opp_no"], side, old, action, new_key, v2[new_key], None, country, evidence))
            elif action == "drop" or action == "decided":
                plan.enquiries.append(EnquiryChange(
                    e["id"], e["opp_no"], side, old, action, None, None,
                    DROPPED_NOTE.format(key, evidence[:EVIDENCE_CHARS]), country, evidence))
            elif action == "decide":
                plan.enquiries.append(EnquiryChange(
                    e["id"], e["opp_no"], side, old, action, old, None,
                    PENDING_NOTE.format(key), country, evidence))
            else:
                plan.problems.append("{} {}: location-map action {!r} for {} is unknown".format(
                    e["opp_no"], side, action, key))


def _plan_party(conn, inputs, plan, objects, countries) -> None:
    cols = _columns(conn, "party")
    has_roles, has_group = "roles" in cols, "group_id" in cols
    parties = {}
    for r in conn.execute("SELECT * FROM party"):
        parties[r["id"]] = dict(r)
    clean = {}
    for r in inputs["party"]:
        try:
            clean[int(r["party_id"])] = r
        except ValueError:
            plan.problems.append("party-clean: party_id {!r} is not a number".format(r["party_id"]))
    decisions = {}
    for r in inputs["party_decisions"]:
        try:
            decisions[int(r["party_id"])] = r
        except ValueError:
            plan.problems.append("party-decisions: party_id {!r} is not a number".format(r["party_id"]))

    existing_groups = {}
    if "party_group" in objects:
        existing_groups = {r[0]: r[1] for r in conn.execute("SELECT key, id FROM party_group")}
    group_of = {}                                        # party_id -> group key

    for pid, r in clean.items():
        p = parties.get(pid)
        if p is None:
            plan.problems.append("party-clean: party {} ({}) is not in the database".format(pid, r["key"]))
            continue
        if p["merged_into"]:
            continue
        # 6.1 roles, as proposed, minus the 'unknown' token (decision 2)
        want = clean_roles(r["roles"])
        have = (p.get("roles") or "") if has_roles else ""
        odd = [x for x in want.split("|") if x and x not in ROLES]
        if odd:
            plan.warnings.append("party {} ({}): role {} is not in the spec list; written as proposed".format(
                pid, p["key"], "/".join(odd)))
        if want != have:
            plan.party.append(PartyChange("roles", pid, p["key"], have or None, want or None))
        # 6.2 type fix
        if TYPE_FIX_FLAG in r["flags"].split("|") and p["type"] != OWN_OFFICE:
            plan.party.append(PartyChange("type", pid, p["key"], p["type"], OWN_OFFICE, "flag " + TYPE_FIX_FLAG))
        # 6.4 groups: high confidence only
        if r["group_confidence"] == "high" and r["proposed_group"]:
            group_of[pid] = r["proposed_group"].strip().upper()
        # 6.5 display name: into an empty full_name, or one that only repeats the key (decision 1)
        if r["proposed_name"]:
            have_name = (p["full_name"] or "").strip()
            if not have_name or have_name.casefold() == (p["key"] or "").strip().casefold():
                if have_name != r["proposed_name"]:
                    plan.party.append(PartyChange("full_name", pid, p["key"], p["full_name"], r["proposed_name"]))
            else:
                plan.full_name_skipped.append((pid, p["key"], p["full_name"], r["proposed_name"]))
        # 6.6 country, only over Unknown
        if r["proposed_country"] and p["country"] == "Unknown":
            if r["proposed_country"] not in countries:
                plan.problems.append("party {} ({}): proposed country {!r} is not in the country table".format(
                    pid, p["key"], r["proposed_country"]))
            else:
                plan.party.append(PartyChange("country", pid, p["key"], p["country"], r["proposed_country"]))

    # parties created after Phase 1: a default role from the type (decision 3)
    for pid, p in sorted(parties.items()):
        if pid in clean or p["merged_into"]:
            continue
        want = DEFAULT_ROLE_BY_TYPE.get(p["type"], "")
        have = (p.get("roles") or "") if has_roles else ""
        detail = "not in party-clean; type {}".format(p["type"])
        if want and want != have:
            plan.party.append(PartyChange("default_roles", pid, p["key"], have or None, want, detail))
        elif not want:
            plan.warnings.append("party {} ({}): not in party-clean and type {} has no default role; roles stay empty".format(
                pid, p["key"], p["type"]))

    # 6.3 merges: only what party-decisions says
    for pid, d in decisions.items():
        if d.get("group"):
            group_of[pid] = d["group"].strip().upper()
        if not d.get("merge_into"):
            continue
        try:
            keep_id = int(d["merge_into"])
        except ValueError:
            plan.problems.append("party-decisions: merge_into {!r} for party {} is not a number".format(d["merge_into"], pid))
            continue
        drop, keep = parties.get(pid), parties.get(keep_id)
        if drop is None or keep is None:
            plan.problems.append("party-decisions: merge {} -> {}: one of them is not in the database".format(pid, keep_id))
            continue
        if drop["merged_into"] == keep_id:
            continue                                     # done on an earlier run
        if drop["merged_into"] or keep["merged_into"]:
            plan.problems.append("party-decisions: merge {} ({}) -> {} ({}): one of them was merged already".format(
                pid, drop["key"], keep_id, keep["key"]))
            continue
        moves = []
        for table, column in MERGE_MOVES:
            n = conn.execute("SELECT COUNT(*) FROM {} WHERE {} = ?".format(table, column), (pid,)).fetchone()[0]
            if n:
                moves.append("{} {}.{}".format(n, table, column))
        for r in conn.execute("SELECT opp_no FROM enquiry WHERE requester_party_id = ? "
                              "ORDER BY CAST(substr(opp_no, 5) AS INTEGER)", (pid,)):
            plan.agent_changes.append((r[0], drop["key"], keep["key"]))
        plan.party.append(PartyChange(
            "merge", pid, drop["key"], drop["key"], "{} ({})".format(keep["key"], keep_id),
            "moves " + (", ".join(moves) if moves else "nothing") + "; " + (d.get("note") or ""),
            target_id=keep_id))

    for key in sorted(set(group_of.values())):
        plan.party_groups.append((key, key, key in existing_groups))
    for pid, key in sorted(group_of.items()):
        p = parties[pid]
        current = None
        if has_group and p.get("group_id"):
            current = next((k for k, gid in existing_groups.items() if gid == p["group_id"]), str(p["group_id"]))
        if current != key:
            plan.party.append(PartyChange("group", pid, p["key"], current, key))


def _plan_opp_index(conn, plan) -> None:
    """How many opp-index.csv rows change: the text columns only (section 8)."""
    changes = {}
    for c in plan.enquiries:
        if c.text_changed:
            changes.setdefault(c.opp_no, {})[c.side] = c.new_key or ""
    agents = {opp: new for opp, _, new in plan.agent_changes}
    rows = export_csv.opp_index_rows(conn)
    changed = 0
    for row in rows:
        new = list(row)
        change = changes.get(row[0])
        if change:
            if "origin" in change:
                new[4] = change["origin"]
            if "dest" in change:
                new[5] = change["dest"]
        if row[0] in agents:
            new[2] = agents[row[0]]
        if tuple(new) != tuple(row):
            changed += 1
    plan.opp_index_changed = changed
    plan.opp_index_same = len(rows) - changed


# ------------------------------------------------------------------- apply --
def backup_location_old(conn, prep_dir) -> Path | None:
    """The old location table, every column, as CSV (section 1). None when
    the table is already the view (an earlier run took the copy)."""
    if _tables(conn).get("location") != "table":
        return None
    cur = conn.execute("SELECT * FROM location ORDER BY key")
    columns = [d[0] for d in cur.description]
    rows = [tuple(r) for r in cur.fetchall()]
    path = Path(prep_dir) / LOCATION_OLD_BACKUP
    export_csv.write(path, columns, rows)
    return path


def apply_plan(conn: sqlite3.Connection, plan: Plan, today: str | None = None) -> OrderedDict:
    """Steps 3 to 8 of section 4.1 on an open connection, inside whatever
    transaction the caller holds. Returns what was written."""
    from . import db

    repo = db.Repository(conn, source="migration")
    done = OrderedDict()

    # 3 areas
    area_ids = {}
    for a in plan.areas:
        area_ids[a["key"]] = repo.add_master_area(a["key"], a["name"], a["country_code"])
    done["master_area inserted"] = sum(1 for a in plan.areas if not a["exists"])

    # 4 locations, keys first, then the parents and areas as ids
    loc_ids = {}
    for loc in plan.locations:
        if loc.exists:
            loc_ids[loc.key] = conn.execute(
                "SELECT id FROM master_location WHERE key = ?", (loc.key,)).fetchone()[0]
            continue
        taken = conn.execute("SELECT key FROM master_location WHERE id = ?", (loc.id,)).fetchone()
        if taken is not None:
            raise RuntimeError("master_location id {} is already used by {}".format(loc.id, taken[0]))
        repo.insert("master_location", {
            "id": loc.id, "key": loc.key, "name": loc.name, "type": loc.type,
            "country": loc.country, "country_code": loc.country_code,
            "subdivision": loc.subdivision, "unlocode": loc.unlocode, "iata": loc.iata,
            "is_seaport": loc.is_seaport, "is_airport": loc.is_airport,
            "status": loc.status, "note": loc.note, "source": "phase1"})
        loc_ids[loc.key] = loc.id
    for loc in plan.locations:
        if loc.exists:
            continue
        values = {}
        if loc.parent_key:
            values["parent_id"] = loc_ids[loc.parent_key]
        if loc.area_key:
            values["area_id"] = area_ids[loc.area_key]
        if values:
            repo.update("master_location", loc.id, values)
    done["master_location inserted"] = sum(1 for l in plan.locations if not l.exists)

    # 5 aliases
    n = 0
    for alias, target, kind, exists in plan.aliases:
        if not exists and repo.add_master_alias(alias, loc_ids[target], kind):
            n += 1
    done["master_location_alias inserted"] = n

    # 6 enquiry
    by_enquiry = OrderedDict()
    for c in plan.enquiries:
        by_enquiry.setdefault(c.enquiry_id, []).append(c)
    notes_added = 0
    for enquiry_id, changes in by_enquiry.items():
        values, notes = {}, []
        for c in changes:
            values["{}_loc_legacy".format(c.side)] = c.old_key
            values["{}_loc_id".format(c.side)] = c.new_id
            values["{}_loc".format(c.side)] = c.new_key
            if c.note:
                notes.append(c.note)
        if notes:
            current = conn.execute(
                "SELECT system_notes FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone()[0]
            values["system_notes"] = ((current or "").rstrip() + "\n" + "\n".join(notes)).strip()
            notes_added += len(notes)
        repo.update("enquiry", enquiry_id, values)
    done["enquiry cases updated"] = len(by_enquiry)
    done["enquiry values: resolved / cleared / pending"] = "{} / {} / {}".format(
        plan.counts()["resolved"], plan.counts()["dropped"], plan.counts()["pending"])

    # 7 the old table steps aside, the view takes its name
    objects = _tables(conn)
    if objects.get("location") == "table":
        conn.execute("ALTER TABLE location RENAME TO location_old")
        conn.execute(LOCATION_VIEW_SQL)
        done["location"] = "renamed location_old; view location created"
    elif "location" not in objects:
        conn.execute(LOCATION_VIEW_SQL)
        done["location"] = "view location created"
    else:
        done["location"] = "already the view"

    # 8 party (section 6), in the spec's order
    today = today or dt.date.today().isoformat()
    counts = Counter()
    for item in ("roles", "default_roles", "type", "merge", "group", "full_name", "country"):
        for p in plan.party:
            if p.item != item:
                continue
            if item in ("roles", "default_roles"):
                repo.update("party", p.party_id, {"roles": p.new or ""})
            elif item == "type":
                repo.update("party", p.party_id, {"type": p.new})
            elif item == "merge":
                repo.merge_party(p.target_id, p.party_id, today=today)
            elif item == "group":
                gid = repo.add_party_group(p.new)
                repo.update("party", p.party_id, {"group_id": gid})
            elif item == "full_name":
                repo.update("party", p.party_id, {"full_name": p.new})
            elif item == "country":
                repo.update("party", p.party_id, {"country": p.new})
            counts[item] += 1
    done["party_group inserted"] = sum(1 for g in plan.party_groups if not g[2])
    for item in ("roles", "default_roles", "type", "merge", "group", "full_name", "country"):
        done["party {}".format(item)] = counts[item]
    return done


def apply(db_path=None, prep_dir=None, restore_dir=None, report_path=None,
          export: bool = True, today: str | None = None) -> tuple:
    """Section 4.1: restore point, schema v14, the plan in one transaction,
    the exports, the report with the section 8 checks. Returns
    (plan, report path, result dict, checks)."""
    from . import autoexport, backup, db, schema_migrations

    db_path = Path(db_path or paths.DB_PATH)
    inputs = read_inputs(prep_dir)
    prep = inputs["_dir"]
    result = OrderedDict()

    # 1 restore point and the old table's copy
    restore = backup.before_task(db_path, "master", restore_dir=restore_dir)
    result["restore point"] = str(restore)
    conn = db.connect(db_path)
    try:
        old_csv = backup_location_old(conn, prep)
        result["location_old_backup.csv"] = str(old_csv) if old_csv else "(taken on an earlier run)"
        before_rows = export_csv.opp_index_rows(conn)

        # 2 schema
        notes = schema_migrations.upgrade(conn)
        result["schema"] = "version {}{}".format(
            schema_migrations.current_version(conn),
            " (" + "; ".join(notes) + ")" if notes else " (already current)")

        plan = build_plan(conn, inputs, db_path)
        if plan.blocked:
            result["outcome"] = "BLOCKED - nothing written after the schema step"
            report = write_report(plan, report_path, "apply-blocked", result)
            return plan, report, result, []

        # 3-8 one transaction
        conn.commit()
        conn.isolation_level = None
        conn.execute("BEGIN")
        try:
            done = apply_plan(conn, plan, today)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.isolation_level = ""
        result.update(done)
        after_rows = export_csv.opp_index_rows(conn)
        checks = acceptance(conn, inputs, db_path, plan=plan, before_rows=before_rows,
                            after_rows=after_rows, restore_point=restore)
    finally:
        conn.close()

    # 9 the exports
    if export:
        written = autoexport.export_now(db_path)
        result["exports ({})".format(written["mode"])] = "; ".join(
            str(written[k]) for k in ("workbook", "opp_index", "agents"))
    result["outcome"] = "applied"
    report = write_report(plan, report_path, "apply", result, checks)
    return plan, report, result, checks


# -------------------------------------------------------------- acceptance --
@dataclass
class Check:
    ok: bool
    text: str


def _changed_columns(before, after, conn) -> tuple:
    """(rows that differ, rows differing outside OriginDetail / DestDetail,
    rows whose Agent changed because the requester was merged)."""
    import json

    before_by = {r[0]: r for r in before}
    differ, elsewhere, merged = [], [], []
    for row in after:
        old = before_by.get(row[0])
        if old is None or tuple(old) == tuple(row):
            continue
        differ.append(row[0])
        other = [i for i in range(len(row)) if i not in (4, 5) and old[i] != row[i]]
        if other == [2]:
            # the Agent column follows a party merge when the new key's party
            # lists the old key among its aliases
            party = conn.execute("SELECT aliases FROM party WHERE key = ?", (row[2],)).fetchone()
            try:
                aliases = json.loads(party["aliases"] or "[]") if party else []
            except ValueError:
                aliases = []
            if old[2] in aliases:
                merged.append("{} ({} -> {})".format(row[0], old[2], row[2]))
                continue
        if other:
            elsewhere.append(row[0])
    return differ, elsewhere, merged


def acceptance(conn, inputs, db_path, plan: Plan | None = None, before_rows=None,
               after_rows=None, restore_point=None) -> list:
    """Section 8, as numbers: the first four items (tables and locations) and
    the party items, each PASS or FAIL. pytest and `check` run separately."""
    from .export_xlsx import OPPORTUNITY_COLUMNS

    checks = []
    objects = _tables(conn)
    if "master_location" not in objects or objects.get("location") != "view":
        return [Check(False, "migrate-master has not been applied to this database")]

    # 8.1 counts against Phase 1
    n_loc = conn.execute("SELECT COUNT(*) FROM master_location WHERE source = 'phase1'").fetchone()[0]
    n_alias = conn.execute("SELECT COUNT(*) FROM master_location_alias WHERE kind <> 'manual'").fetchone()[0]
    n_area = conn.execute("SELECT COUNT(*) FROM master_area").fetchone()[0]
    expected = {"master_location": len(inputs["v2"]), "master_location_alias": len(inputs["alias"]),
                "master_area": len(inputs["area"])}
    checks.append(Check(
        (n_loc, n_alias, n_area) == (expected["master_location"], expected["master_location_alias"], expected["master_area"]),
        "8.1 tables: master_location {} (CSV {}, Phase 1 {}), alias {} (CSV {}, Phase 1 {}), area {} (CSV {}, Phase 1 {})".format(
            n_loc, expected["master_location"], PHASE1_EXPECTED["master_location"],
            n_alias, expected["master_location_alias"], PHASE1_EXPECTED["master_location_alias"],
            n_area, expected["master_area"], PHASE1_EXPECTED["master_area"])))
    total = resolved = dropped = pending = 0
    for side in ("origin", "dest"):
        total += conn.execute(
            "SELECT COUNT(*) FROM enquiry WHERE COALESCE({0}_loc_legacy, '') <> ''".format(side)).fetchone()[0]
        resolved += conn.execute(
            "SELECT COUNT(*) FROM enquiry WHERE COALESCE({0}_loc_legacy, '') <> '' AND {0}_loc_id IS NOT NULL".format(side)).fetchone()[0]
        dropped += conn.execute(
            "SELECT COUNT(*) FROM enquiry WHERE COALESCE({0}_loc_legacy, '') <> '' AND {0}_loc_id IS NULL "
            "AND system_notes LIKE '%master: ' || upper(trim({0}_loc_legacy)) || ' dropped%'".format(side)).fetchone()[0]
        pending += conn.execute(
            "SELECT COUNT(*) FROM enquiry WHERE COALESCE({0}_loc_legacy, '') <> '' AND {0}_loc_id IS NULL "
            "AND (system_notes LIKE '%master: ' || upper(trim({0}_loc_legacy)) || ' pending decision%' "
            "  OR system_notes LIKE '%master: ' || upper(trim({0}_loc_legacy)) || ' not in location-map%')".format(side)).fetchone()[0]
    checks.append(Check(
        resolved + dropped + pending == total,
        "8.1 enquiry values: {} with a legacy value -> {} resolved to an id, {} cleared, {} pending "
        "(Phase 1: 784 -> 746 / 38 / 0)".format(total, resolved, dropped, pending)))
    checks.append(Check(pending == 0, "8.1 values still pending a decision: {}".format(pending)))

    # 8.2 every legacy value has an id or a dropped note (a pending one is listed above)
    orphans = 0
    for side in ("origin", "dest"):
        orphans += conn.execute(
            "SELECT COUNT(*) FROM enquiry WHERE COALESCE({0}_loc_legacy, '') <> '' AND {0}_loc_id IS NULL "
            "AND NOT (system_notes LIKE '%master: ' || upper(trim({0}_loc_legacy)) || ' dropped%' "
            "      OR system_notes LIKE '%master: ' || upper(trim({0}_loc_legacy)) || ' pending decision%' "
            "      OR system_notes LIKE '%master: ' || upper(trim({0}_loc_legacy)) || ' not in location-map%')".format(side)).fetchone()[0]
    checks.append(Check(orphans == 0,
                        "8.2 legacy values with neither an id nor a system_notes line: {}".format(orphans)))

    # 8.3 the compatibility view
    n_view = conn.execute("SELECT COUNT(*) FROM location").fetchone()[0]
    n_master = conn.execute("SELECT COUNT(*) FROM master_location").fetchone()[0]
    checks.append(Check(n_view == n_master, "8.3 location view rows {} = master_location rows {}".format(n_view, n_master)))
    sample = [(a[0], a[1]) for a in (plan.aliases if plan else []) if a[2] == "legacy_key"][:3]
    if not sample:
        sample = [(r[0], r[1]) for r in conn.execute(
            "SELECT a.alias, l.key FROM master_location_alias a JOIN master_location l ON l.id = a.location_id "
            "WHERE a.kind = 'legacy_key' ORDER BY a.alias LIMIT 3")]
    if any(a == "CHICAGO" for a, _ in sample) is False:
        chicago = conn.execute(
            "SELECT a.alias, l.key FROM master_location_alias a JOIN master_location l ON l.id = a.location_id "
            "WHERE a.alias = 'CHICAGO'").fetchone()
        if chicago:
            sample.insert(0, (chicago[0], chicago[1]))
    for alias, target in sample:
        old_gone = conn.execute("SELECT COUNT(*) FROM location WHERE key = ?", (alias,)).fetchone()[0] == 0
        new_there = conn.execute("SELECT COUNT(*) FROM location WHERE key = ?", (target,)).fetchone()[0] == 1
        via_alias = conn.execute(
            "SELECT l.key FROM master_location_alias a JOIN master_location l ON l.id = a.location_id "
            "WHERE a.alias = ?", (alias,)).fetchone()
        ok = old_gone and new_there and via_alias is not None and via_alias[0] == target
        checks.append(Check(ok, "8.3 key {!r}: not in view {}, {!r} in view {}, alias -> {}".format(
            alias, old_gone, target, new_there, via_alias[0] if via_alias else None)))

    # 8.4 exports
    if before_rows is not None and after_rows is not None:
        differ, elsewhere, merged = _changed_columns(before_rows, after_rows, conn)
        planned = {c.opp_no for c in (plan.enquiries if plan else []) if c.text_changed}
        planned |= {opp for opp, _, _ in (plan.agent_changes if plan else [])}
        ok = not elsewhere and len(before_rows) == len(after_rows) and set(differ) <= planned
        checks.append(Check(ok, "8.4 opp-index.csv: {} rows, {} changed (all in the report's list: {}), "
                                "{} changed outside OriginDetail / DestDetail, {} identical; Agent column "
                                "changed by a party merge: {}".format(
                                    len(after_rows), len(differ), set(differ) <= planned, len(elsewhere),
                                    len(after_rows) - len(differ), ", ".join(merged) or "none")))
    checks.append(Check(len(OPPORTUNITY_COLUMNS) == 41,
                        "8.4 xlsx Opportunities columns: {}".format(len(OPPORTUNITY_COLUMNS))))

    # 8.5 party
    for r in inputs["party_decisions"]:
        if not r.get("merge_into"):
            continue
        pid = int(r["party_id"])
        left = {}
        for table, column in MERGE_MOVES:
            n = conn.execute("SELECT COUNT(*) FROM {} WHERE {} = ?".format(table, column), (pid,)).fetchone()[0]
            if n:
                left["{}.{}".format(table, column)] = n
        merged = conn.execute("SELECT merged_into FROM party WHERE id = ?", (pid,)).fetchone()
        ok = not left and merged is not None and str(merged[0]) == r["merge_into"]
        checks.append(Check(ok, "8.5 merge {} -> {}: merged_into = {}, rows still on {}: {}".format(
            pid, r["merge_into"], merged[0] if merged else None, pid, left or "none")))
    with_roles = conn.execute("SELECT COUNT(*) FROM party WHERE roles <> '' AND merged_into IS NULL").fetchone()[0]
    live = conn.execute("SELECT COUNT(*) FROM party WHERE merged_into IS NULL").fetchone()[0]
    empty = [r[0] for r in conn.execute(
        "SELECT key FROM party WHERE roles = '' AND merged_into IS NULL ORDER BY key")]
    merged_ids = {r[0] for r in conn.execute("SELECT id FROM party WHERE merged_into IS NOT NULL")}
    in_clean = {int(r["party_id"]) for r in inputs["party"] if r["party_id"].isdigit()}
    expected_roles = sum(1 for r in inputs["party"]
                         if clean_roles(r["roles"]) and int(r["party_id"]) not in merged_ids)
    defaulted = conn.execute(
        "SELECT COUNT(*) FROM party WHERE merged_into IS NULL AND roles <> '' AND id NOT IN ({})".format(
            ",".join(str(i) for i in in_clean) or "0")).fetchone()[0]
    checks.append(Check(with_roles == expected_roles + defaulted,
                        "8.5 parties with roles: {} of {} live = {} from party-clean (rows with a role after "
                        "dropping 'unknown', merged ones excluded) + {} defaulted from the type; empty: {}".format(
                            with_roles, live, expected_roles, defaulted, ", ".join(empty) or "none")))
    fixes = [int(r["party_id"]) for r in inputs["party"] if TYPE_FIX_FLAG in r["flags"].split("|")]
    if fixes:
        wrong = [r[0] for r in conn.execute(
            "SELECT key FROM party WHERE id IN ({}) AND type <> ?".format(",".join("?" * len(fixes))),
            fixes + [OWN_OFFICE])]
        checks.append(Check(not wrong, "8.5 type Office on {} flagged parties; still wrong: {}".format(
            len(fixes), ", ".join(wrong) or "none")))

    # 8.7 the restore point
    if restore_point is not None:
        rp = Path(restore_point)
        try:
            other = sqlite3.connect("file:{}?mode=ro".format(rp.resolve().as_posix()), uri=True)
            n_before = other.execute("SELECT COUNT(*) FROM enquiry").fetchone()[0]
            other.close()
        except sqlite3.Error as exc:
            n_before = "unreadable ({})".format(exc)
        n_now = conn.execute("SELECT COUNT(*) FROM enquiry").fetchone()[0]
        checks.append(Check(rp.exists() and n_before == n_now,
                            "8.7 restore point {}: exists {}, enquiry rows {} (now {})".format(
                                rp.name, rp.exists(), n_before, n_now)))
    return checks


def verify(db_path=None, prep_dir=None, restore_dir=None) -> list:
    """The section 8 checks on the database as it is (read only)."""
    from . import backup

    db_path = Path(db_path or paths.DB_PATH)
    inputs = read_inputs(prep_dir)
    restore = None
    folder = Path(restore_dir or paths.RESTORE_POINTS_DIR)
    if folder.exists():
        candidates = sorted(folder.glob("opplist_before_master_*.db"))
        restore = candidates[-1] if candidates else None
    conn = open_readonly(db_path)
    try:
        return acceptance(conn, inputs, db_path, restore_point=restore)
    finally:
        conn.close()


# ------------------------------------------------------------------ report --
def _md_row(*cells) -> str:
    return "| " + " | ".join(str(c).replace("|", "\\|") for c in cells) + " |"


def report_lines(plan: Plan, mode: str = "dry-run", result: dict | None = None,
                 checks: list | None = None) -> list:
    """The markdown report of section 4.2. `result` (after apply) adds the
    real outcome and the restore point; `checks` the section 8 numbers."""
    c = plan.counts()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = ["# migrate-master report ({}) - {}".format(mode, now), ""]
    lines.append("- database: `{}` (schema version {}, {})".format(
        plan.db_path, plan.schema_version,
        "master tables already active" if plan.master_active else "not migrated yet"))
    lines.append("- inputs: `{}`".format(plan.prep_dir))
    if mode == "dry-run":
        lines.append("- **nothing was written**: this is the plan, `--apply` does it")
    if plan.blocked:
        lines.append("- **apply is blocked**: {} problem(s), see the last section".format(len(plan.problems)))
    elif plan.nothing_to_do:
        lines.append("- nothing to do: every step was applied on an earlier run")
    lines.append("")

    if result:
        lines += ["## Result", ""]
        for key, value in result.items():
            lines.append("- {}: {}".format(key, value))
        lines.append("")
    if checks:
        lines += ["## Section 8 checks", ""]
        for ch in checks:
            lines.append("- {} {}".format("PASS" if ch.ok else "FAIL", ch.text))
        lines.append("")

    # -- section 8 check
    lines += ["## Against the Phase 1 summary (section 8)", "",
              _md_row("", "Phase 1", "this run"), _md_row("---", "---", "---")]
    for label, key in (("master_location rows", "master_location"),
                       ("master_location_alias rows", "master_location_alias"),
                       ("master_area rows", "master_area")):
        lines.append(_md_row(label, PHASE1_EXPECTED[key], c[key]))
    lines.append(_md_row("enquiry origin / dest values", "784", c["enquiry_values"] + plan.enquiries_done))
    lines.append(_md_row("...resolved to a master id", PHASE1_EXPECTED["resolved"], c["resolved"]))
    lines.append(_md_row("...cleared (drop)", PHASE1_EXPECTED["dropped"], c["dropped"]))
    lines.append(_md_row("...pending a decision", PHASE1_EXPECTED["pending"], c["pending"]))
    if plan.enquiries_done:
        lines.append(_md_row("...already carrying a legacy value (earlier run)", "", plan.enquiries_done))
    lines.append("")

    # -- 1 tables
    lines += ["## 1. Master tables", ""]
    lines.append("- master_area: {} to insert{}".format(
        c["master_area"], _already(sum(1 for a in plan.areas if a["exists"]))))
    lines.append("- master_location: {} to insert{}; by type: {}; status to_confirm: {}".format(
        c["master_location"], _already(sum(1 for l in plan.locations if l.exists)),
        ", ".join("{} {}".format(n, t) for t, n in sorted(Counter(l.type for l in plan.locations).items())),
        sum(1 for l in plan.locations if l.status == "to_confirm")))
    lines.append("- master_location_alias: {} to insert{}; by kind: {}".format(
        c["master_location_alias"], _already(sum(1 for a in plan.aliases if a[3])),
        ", ".join("{} {}".format(n, k) for k, n in sorted(Counter(a[2] for a in plan.aliases).items()))))
    lines.append("- party_group: {} to insert{} ({})".format(
        c["party_group"], _already(sum(1 for g in plan.party_groups if g[2])),
        ", ".join(g[0] for g in plan.party_groups) or "none"))
    lines.append("- ids: master_location.id = the number in the CSV id (L0001 -> 1), so the "
                 "Rate Desk sees the same ids as the proposal")
    lines.append("")

    # -- 2 enquiry
    lines += ["## 2. Enquiry origin / dest", ""]
    lines += [_md_row("action", "values", "meaning"), _md_row("---", "---", "---")]
    meaning = {"keep": "same key, id set", "map": "new key, id set", "alias": "new key, id set (old key kept as alias)",
               "create": "new key, id set (place created in v2)", "decided": "as decided",
               "drop": "id NULL, text cleared, country kept, system_notes line",
               "decide": "id NULL, text kept, system_notes line - needs a decision",
               "unmapped": "old key not in location-map - needs a decision"}
    for action in ("keep", "map", "alias", "create", "decided", "drop", "decide", "unmapped"):
        if c["actions"].get(action):
            lines.append(_md_row(action, c["actions"][action], meaning[action]))
    lines.append("")

    dropped = [x for x in plan.enquiries if x.action in ("drop", "decided") and not x.resolved]
    lines += ["### Cleared (drop): {} value(s) on {} case(s)".format(len(dropped), len({x.opp_no for x in dropped})), ""]
    if dropped:
        lines += [_md_row("case", "side", "old key", "country stays", "why"), _md_row("---", "---", "---", "---", "---")]
        for x in dropped:
            lines.append(_md_row(x.opp_no, x.side, x.old_key, x.country or "", x.evidence[:EVIDENCE_CHARS]))
    lines.append("")

    pending = [x for x in plan.enquiries if x.action in ("decide", "unmapped")]
    lines += ["### Pending a decision: {}".format(len(pending)), ""]
    if pending:
        lines += [_md_row("case", "side", "old key", "note"), _md_row("---", "---", "---", "---")]
        for x in pending:
            lines.append(_md_row(x.opp_no, x.side, x.old_key, x.note))
    else:
        lines.append("- none")
    lines.append("")

    changed = [x for x in plan.enquiries if x.text_changed and x.resolved]
    lines += ["### Text column changes: {} value(s) on {} case(s)".format(len(changed), len({x.opp_no for x in changed})), "",
              "These, plus the cleared ones above, are exactly what changes in `opp-index.csv` "
              "(OriginDetail / DestDetail) and in the xlsx Origin Detail / Dest Detail columns. "
              "opp-index.csv: **{} rows change, {} rows identical**.".format(plan.opp_index_changed, plan.opp_index_same), ""]
    if plan.agent_changes:
        lines += ["Agent column changed by a party merge (section 3): " + "; ".join(
            "{} `{}` -> `{}`".format(opp, old, new) for opp, old, new in plan.agent_changes), ""]
    if changed:
        by_pair = Counter((x.old_key, x.new_key) for x in changed)
        lines += ["Distinct old -> new keys: {}".format(len(by_pair)), "",
                  _md_row("old key", "new key", "values"), _md_row("---", "---", "---")]
        for (old, new), n in sorted(by_pair.items()):
            lines.append(_md_row(old, new, n))
        lines += ["", "Per case:", "", _md_row("case", "side", "old", "new", "action"), _md_row("---", "---", "---", "---", "---")]
        for x in changed:
            lines.append(_md_row(x.opp_no, x.side, x.old_key, x.new_key, x.action))
    lines.append("")

    # -- 3 party
    lines += ["## 3. Party (section 6)", ""]
    groups = {"roles": "6.1 roles (from party-clean, 'unknown' dropped)",
              "default_roles": "6.1b default roles for parties not in party-clean (from the type)",
              "type": "6.2 type -> Office", "merge": "6.3 merges",
              "group": "6.4 group membership",
              "full_name": "6.5 full_name from proposed_name (empty, or equal to the key)",
              "country": "6.6 country from proposed_country"}
    for item, title in groups.items():
        rows = [p for p in plan.party if p.item == item]
        lines += ["### {}: {}".format(title, len(rows)), ""]
        if item == "roles" and rows:
            lines.append("by value: " + ", ".join("{} `{}`".format(n, v) for v, n in Counter(p.new for p in rows).most_common()))
            lines.append("")
            lines.append("(one line per party would be {} lines; the values above are the whole change)".format(len(rows)))
        elif rows:
            lines += [_md_row("party", "key", "old", "new", "detail"), _md_row("---", "---", "---", "---", "---")]
            for p in rows:
                lines.append(_md_row(p.party_id, p.key, p.old or "", p.new or "", p.detail))
        else:
            lines.append("- none")
        if item == "full_name" and plan.full_name_skipped:
            lines += ["", "Skipped because full_name already differs from the key ({}): ".format(len(plan.full_name_skipped)) +
                      "; ".join("{} `{}` has `{}`, proposed `{}`".format(pid, key, have, want)
                                for pid, key, have, want in plan.full_name_skipped)]
        lines.append("")

    # -- 4 what apply does
    lines += ["## 4. What apply does, in one transaction", "",
              "1. restore point `data\\restore-points\\opplist_before_master_<stamp>.db` and "
              "`data\\master-prep\\location_old_backup.csv` (the old location table, every column)",
              "2. schema v14: master tables, the new enquiry / party columns, enquiry's foreign key to "
              "location(key) removed (table rebuilt, rows copied), the v_master_* views",
              "3. the inserts of section 1, then enquiry: old text -> `*_loc_legacy`, id and text as in section 2",
              "4. `location` renamed `location_old`; view `location` over master_location (same 13 columns)",
              "5. party changes of section 3 (merges through Repository.merge_party)",
              "6. the three export files, so the section 8 comparison can be run", ""]

    # -- 5 problems
    lines += ["## 5. Problems ({}) and warnings ({})".format(len(plan.problems), len(plan.warnings)), ""]
    for p in plan.problems:
        lines.append("- PROBLEM: {}".format(p))
    for w in plan.warnings:
        lines.append("- warning: {}".format(w))
    if not plan.problems and not plan.warnings:
        lines.append("- none")
    return lines


def _already(n: int) -> str:
    return " ({} already there)".format(n) if n else ""


def write_report(plan: Plan, path=None, mode: str = "dry-run", result: dict | None = None,
                 checks: list | None = None) -> Path:
    path = Path(path or plan.prep_dir / REPORT_NAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(report_lines(plan, mode, result, checks)) + "\n", encoding="utf-8")
    return path


# ----------------------------------------------------------------- dry run --
def open_readonly(db_path) -> sqlite3.Connection:
    """The dry run must not touch the file: a read-only connection, and no
    schema upgrade (db.open_db would run one)."""
    uri = "file:{}?mode=ro".format(Path(db_path).resolve().as_posix())
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def dry_run(db_path=None, prep_dir=None, report_path=None) -> tuple:
    db_path = Path(db_path or paths.DB_PATH)
    inputs = read_inputs(prep_dir)
    conn = open_readonly(db_path)
    try:
        plan = build_plan(conn, inputs, db_path)
    finally:
        conn.close()
    return plan, write_report(plan, report_path)


def summary_lines(plan: Plan, report: Path) -> list:
    c = plan.counts()
    lines = ["report   : {}".format(report),
             "database : {} (schema {})".format(plan.db_path, plan.schema_version),
             "tables   : master_location {}  alias {}  area {}  party_group {}".format(
                 c["master_location"], c["master_location_alias"], c["master_area"], c["party_group"]),
             "enquiry  : {} values -> {} resolved, {} cleared, {} pending; {} text values change "
             "({} opp-index rows change, {} identical)".format(
                 c["enquiry_values"], c["resolved"], c["dropped"], c["pending"], c["text_changed"],
                 plan.opp_index_changed, plan.opp_index_same),
             "party    : " + (", ".join("{} {}".format(n, k) for k, n in sorted(c["party"].items())) or "nothing")]
    if plan.problems:
        lines.append("BLOCKED  : {} problem(s) - see the report".format(len(plan.problems)))
    if plan.warnings:
        lines.append("warnings : {}".format(len(plan.warnings)))
    return lines


def check_lines(checks) -> list:
    return ["{} {}".format("PASS" if ch.ok else "FAIL", ch.text) for ch in checks]

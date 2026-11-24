"""Connection handling, schema initialisation and the repository layer.

Every write to business data goes through Repository so that change_log is
always complete. Business data is never deleted; the only
delete in the whole app is of backup files the app itself produced.

Insert logging: an insert has no old value for each field, so it is recorded
as one change_log row with field '(insert)' and the row's natural key. Updates
are logged field by field with old and new values.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import branding, paths, status as status_mod


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def schema_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'enquiry'"
    ).fetchone()
    return row is not None


def init_schema(conn: sqlite3.Connection, schema_sql: str | Path | None = None) -> None:
    """Create the schema. Does nothing if the tables are already there."""
    if schema_exists(conn):
        return
    sql = Path(schema_sql or paths.SCHEMA_SQL).read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.commit()


def open_db(db_path: str | Path, schema_sql: str | Path | None = None,
            upgrade_notes: list | None = None) -> sqlite3.Connection:
    """Open, create the schema if needed, and bring an older file forward."""
    from . import schema_migrations

    conn = connect(db_path)
    fresh = not schema_exists(conn)
    init_schema(conn, schema_sql)
    if not fresh and schema_migrations.current_version(conn) <             schema_migrations.CURRENT_VERSION:
        # An upgrade rewrites the live database, so it is a migration like any
        # other and gets a restore point first.
        from . import backup
        try:
            # Next to the database being upgraded, not the standard folder: a
            # --db pointed somewhere else must not write into the live one.
            copy = backup.before_task(
                db_path, "schema{}".format(schema_migrations.CURRENT_VERSION),
                restore_dir=Path(db_path).parent / "restore-points")
            if upgrade_notes is not None:
                upgrade_notes.append("restore point {}".format(copy.name))
        except OSError as exc:                # a read-only or missing folder
            if upgrade_notes is not None:
                upgrade_notes.append("restore point skipped: {}".format(exc))
    notes = schema_migrations.upgrade(conn)
    if upgrade_notes is not None:
        upgrade_notes.extend(notes)
    healed = _ensure_route_a(conn)
    if healed and upgrade_notes is not None:
        upgrade_notes.append("route A added to {} case(s) that had none".format(healed))
    return conn


def _ensure_route_a(conn: sqlite3.Connection) -> int:
    """Every case has route A (v33). A case written without one - by an app
    still on older code, or a raw insert - gets it from its own lane the next
    time the file is opened. One cheap query when there is nothing to do."""
    if not _has_table(conn, "enquiry_route"):
        return 0
    missing = [r[0] for r in conn.execute(
        "SELECT e.id FROM enquiry e WHERE NOT EXISTS (SELECT 1 FROM enquiry_route r WHERE r.enquiry_id = e.id)")]
    if not missing:
        return 0
    repo = Repository(conn, source="migration")
    for enquiry_id in missing:
        case = conn.execute("SELECT * FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone()
        repo.add_route(enquiry_id, {k: case[k] for k in ROUTE_LANE_FIELDS}, source="migration", sync=False)
    conn.commit()
    return len(missing)


# ----------------------------------------------------------- our offices
#: The office this app works for (branding.HOME_OFFICE): a case handled
#: here stores NULL.
HOME_OFFICE = branding.HOME_OFFICE


def office_codes(conn: sqlite3.Connection) -> list:
    """The codes of our other offices, from the Office parties
    ('JoJo JQM' -> 'JQM'), sorted; the home office left out."""
    pattern = branding.office_key_pattern(conn)
    codes = set()
    for row in conn.execute("SELECT key FROM party WHERE type = 'Office' AND merged_into IS NULL"):
        found = pattern.match(row[0] or "")
        if found and found.group(1).upper() != HOME_OFFICE:
            codes.add(found.group(1).upper())
    return sorted(codes)


def handled_elsewhere_line(codes) -> str:
    """'Of which handled by our other offices: 3 (JQM 2, JJH 1)' from
    the office codes of the cases counted, one per case (v31; SC1: they are
    counted in the total above it, LATAM or not); the dashboard and the
    monthly report print the same sentence."""
    counts = {}
    for code in codes:
        counts[code] = counts.get(code, 0) + 1
    text = "Of which handled by our other offices: {}".format(sum(counts.values()))
    if counts:
        text += " ({})".format(", ".join("{} {}".format(code, n) for code, n in
                                          sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))))
    return text


# ------------------------------------------------------------------ settings
def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return default if row is None else row["value"]


def internal_domains(conn: sqlite3.Connection) -> set:
    """Our own mail domains (setting internal_domains; jojofreight.example when
    the setting is empty)."""
    found = {str(d).strip().lower() for d in get_setting_list(conn, "internal_domains") if str(d).strip()}
    return found or {"jojofreight.example"}


def is_internal_domain(conn: sqlite3.Connection, domain) -> bool:
    """True for one of our own domains or a subdomain of one (13g): such a
    domain never belongs to a party - an office is known by its prefix."""
    domain = (domain or "").strip().lower().lstrip("@")
    return bool(domain) and any(domain == d or domain.endswith("." + d)
                                for d in internal_domains(conn))


def get_setting_list(conn: sqlite3.Connection, key: str) -> list:
    raw = get_setting(conn, key)
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False)
    conn.execute(
        "INSERT INTO setting (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


# ---------------------------------------------------------------- repository
#: The lane fields a route owns and the enquiry copies from route A (v33).
ROUTE_LANE_FIELDS = ("origin_country", "origin_loc", "origin_loc_id", "origin_alt",
                     "dest_country", "dest_loc", "dest_loc_id", "dest_alt", "service_mode", "incoterm")

#: v39 (batch 23): what a route write may carry besides its columns - the
#: master ids of its Dest. location and of its Port of discharge, in order
#: (enquiry_route_place). The repository makes dest_loc / dest_loc_id the
#: first destination and pod the readable copy of the ports.
ROUTE_PLACE_FIELDS = ("dest_places", "pod_places")
#: A Dest. location or a Port of discharge holds at most this many places.
MAX_ROUTE_PLACES = 4

#: Tables whose writes are recorded in change_log. Housekeeping tables
#: (change_log itself, tracking_snooze, setting) are not business data.
LOGGED_TABLES = {
    "enquiry",
    "party",
    "party_domain",
    "contact",
    "party_event",
    "status_event",
    "vendor_request",
    "location",
    "country",
    "suggestion",
    "enquiry_thread",
    "enquiry_cargo",
    "enquiry_cargo_stated",
    "enquiry_route",
    "enquiry_route_place",
    "case_note",
    "shipment",
    "shipment_event",
    "master_area",
    "master_location",
    "master_location_alias",
    "party_group",
    "vendor_rate",          # v51 (Q2a)
    "vendor_rate_line",
    "quote_read",           # v53 (Q2d)
    "quote_read_draft",
    "quote",                # v56 (Q3)
    "quote_line",
    "register_read",        # v57 (RG2a)
}

#: Natural key used to label an insert in change_log.
_INSERT_LABEL = {
    "enquiry": "opp_no",
    "party": "key",
    "location": "key",
    "country": "name",
    "party_domain": "domain",
    "contact": "email",
    "status_event": "status",
    "enquiry_thread": "value",
    "enquiry_cargo": "label",
    "enquiry_cargo_stated": "enquiry_id",
    "enquiry_route": "seq",
    "enquiry_route_place": "location_id",
    "case_note": "enquiry_id",
    "shipment": "title",
    "shipment_event": "title",
    "vendor_request": "enquiry_id",
    "party_event": "type",
    "suggestion": "opp_no",
    "master_area": "key",
    "master_location": "key",
    "master_location_alias": "alias",
    "party_group": "key",
}


# ------------------------------------------------------------- master layer
# After migrate-master the old location table is
# location_old, 'location' is a view over master_location, and every place
# an enquiry points at is a master_location id. Until then the app keeps
# writing the old table, so the switch is a fact of the database, not of
# the code.
def _has_table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (name,)).fetchone() is not None


def thread_values(conn, enquiry_id: int) -> dict:
    """What identifies a case's mail thread: {'conversation': set,
    'thread_key': set} - the case's own ids plus every thread attached to it
    by hand (enquiry_thread, v20). One place for the three readers
    (membership, the register banner, propose)."""
    out = {"conversation": set(), "thread_key": set()}
    row = conn.execute("SELECT conversation_id, thread_key FROM enquiry WHERE id = ?",
                       (enquiry_id,)).fetchone()
    if row is None:
        return out
    if row["conversation_id"]:
        out["conversation"].add(row["conversation_id"])
    if row["thread_key"]:
        out["thread_key"].add(row["thread_key"])
    if _has_table(conn, "enquiry_thread"):
        for extra in conn.execute(
                "SELECT kind, value FROM enquiry_thread WHERE enquiry_id = ? AND voided = 0",
                (enquiry_id,)):
            if extra["kind"] in out:          # v37: a single mail ('message') is case_mail's to read
                out[extra["kind"]].add(extra["value"])
    return out


def thread_cases(conn, conversation: str | None = None, key: str | None = None,
                 legacy: bool = True) -> list:
    """The cases a conversation id or a thread key belongs to - by the
    case's own ids or by a thread attached to it - as rows (id, opp_no,
    status, legacy) in case order. `legacy=False` leaves inherited rows out."""
    ids = set()
    for column, kind, value in (("conversation_id", "conversation", conversation),
                                ("thread_key", "thread_key", key)):
        if not value:
            continue
        ids.update(r["id"] for r in conn.execute(
            "SELECT id FROM enquiry WHERE {} = ?".format(column), (value,)))
        if _has_table(conn, "enquiry_thread"):
            ids.update(r["enquiry_id"] for r in conn.execute(
                "SELECT enquiry_id FROM enquiry_thread WHERE kind = ? AND value = ? AND voided = 0",
                (kind, value)))
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    return conn.execute(
        "SELECT id, opp_no, status, legacy FROM enquiry WHERE id IN ({}){} ORDER BY id".format(
            marks, "" if legacy else " AND legacy = 0"), tuple(ids)).fetchall()


def master_active(conn: sqlite3.Connection) -> bool:
    """True once migrate-master has run on this database."""
    row = conn.execute(
        "SELECT type FROM sqlite_master WHERE name = 'location'").fetchone()
    return row is not None and row[0] == "view"


#: A state or a country code; never created as a place from the form.
TWO_LETTER_CODE = re.compile(r"^[A-Z]{2}$")

#: Q2b (v52): the party types, in the order every type menu offers them -
#: one list. Carrier = an airline or a shipping line; Coloader = a
#: consolidator selling space (an LCL or air consol, an NVOCC); Trucker =
#: truck or rail inland, IPI included.
PARTY_TYPES = ("Agent", "Office", "Direct Customer", "Carrier", "Coloader", "Trucker", "Unknown")
#: wherever Carrier is treated on its own, a Coloader and a Trucker are too
CARRIER_TYPES = ("Carrier", "Coloader", "Trucker")

#: A gateway serves a market city: its parent (RD1, decided 2026-09-17).
GATEWAY_TYPES = ("airport", "seaport", "ramp")


def gateway_parent_problem(values: dict):
    """Why a place cannot be saved as it stands, or None (RD1). An airport,
    seaport or ramp names its parent city, or carries no_parent = 1 with a
    note that says why. A retired row is not held to it: retiring a wrong
    row never asks for a parent it does not have. The messages are the
    form's; the check is the same wherever a place is saved by hand."""
    if values.get("type") not in GATEWAY_TYPES or values.get("status") == "retired":
        return None
    if values.get("parent_id"):
        return None
    if not values.get("no_parent"):
        return ("A{} {} needs its parent city, the market city it serves. Pick it, or tick "
                "'No parent city' and say why in the note.").format(
                    "n" if values["type"] == "airport" else "", values["type"])
    if not (values.get("note") or "").strip():
        return "'No parent city' is ticked: say why in the note."
    return None


# -- the master's naming rules (16 Sep, D1 and D3; RD1b, 2026-10-20) --------
#: A mode or incoterm word a subject glues to a place ('AIR TPE', 'EXW
#: KENNEWICK'). Not SEA: that is Seattle's code.
MODE_WORDS = ("AIR", "OCEAN", "FCL", "LCL", "TRUCK", "EXW", "FCA", "FOB", "CFR", "CIF", "CPT", "CIP",
              "DAP", "DPU", "DDP")
_MODE_PREFIX = re.compile(r"^({})(?:\s+|\s*[:/-]\s*)(\S.*)$".format("|".join(MODE_WORDS)))
#: D1: a US or Canadian city is written 'NAME, ST'.
STATE_CODES = {
    "USA": frozenset("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE "
                     "NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()),
    "Canada": frozenset("AB BC MB NB NL NS NT NU ON PE QC SK YT".split()),
}
#: D3: a country is not a location - except where the city is the country.
CITY_STATES = frozenset(("SINGAPORE", "HONG KONG", "MACAU", "MONACO", "GIBRALTAR"))
_COUNTRY_SPELLINGS = ("UNITED STATES", "UNITED STATES OF AMERICA", "U.S.A.", "UNITED KINGDOM",
                      "GREAT BRITAIN", "UNITED ARAB EMIRATES")
COUNTRY_REFUSED = "This is a country: leave the location empty and set the country"
_CODE_LIKE = re.compile(r"^[A-Z]{3}(?:/[A-Z]{3})*$")


def _country_keys(conn) -> set:
    keys = {location_key(r[0]) for r in conn.execute("SELECT name FROM country WHERE name <> 'Unknown'")}
    return keys | {location_key(s) for s in _COUNTRY_SPELLINGS}


def place_rules(conn, text: str, country=None, type: str = "city") -> dict:
    """What a new place's text becomes before anything is created (RD1b).
    {'text', 'notes', 'error', 'found'}: a leading mode or incoterm word
    taken off (a note says so) and the rest looked up again; a country
    name refused; a US or Canadian city written 'NAME, ST' - a trailing
    state without its comma rewritten (a note), none at all refused. A
    text that already names a place is left as it is. Every path that
    creates a master_location goes through it: add_master_location checks
    it, and the forms ask it first so a refusal stops the save."""
    written = " ".join((text or "").split())
    out = {"text": written, "notes": [], "error": None,
           "found": find_master_location(conn, written) if written else None}
    if not written or out["found"] is not None:
        return out
    while True:
        m = _MODE_PREFIX.match(written.upper())
        if not m:
            break
        rest = written[m.start(2):] if len(written.upper()) == len(written) else m.group(2)
        out["notes"].append("'{}' taken off '{}': a mode or incoterm word is not part of the place".format(
            m.group(1), written.upper()))
        written = rest.strip()
        out["text"] = written
        out["found"] = find_master_location(conn, written)
        if out["found"] is not None:
            return out
    key = location_key(written)
    if key not in CITY_STATES and key in _country_keys(conn):
        out["error"] = "'{}': {}.".format(key, COUNTRY_REFUSED)
        return out
    codes = STATE_CODES.get(country or "")
    # a code (SEA, SEA/TAC) is not a city name: no state asked for it
    if type == "city" and codes and not _CODE_LIKE.match(written):
        us = country == "USA"
        with_comma = re.match(r"^(.*\S)\s*,\s*([A-Za-z]{2})$", written)
        without = re.match(r"^(.*\S)\s+([A-Za-z]{2})$", written)
        if with_comma and with_comma.group(2).upper() in codes:
            fixed = "{}, {}".format(with_comma.group(1), with_comma.group(2).upper())
        elif not with_comma and without and without.group(2).upper() in codes:
            fixed = "{}, {}".format(without.group(1), without.group(2).upper())
            out["notes"].append("'{}' written '{}': a {} city is NAME, ST".format(
                written.upper(), fixed.upper(), "US" if us else "Canadian"))
        else:
            out["error"] = "'{}' is a {} city without its {}: write it NAME, ST ({}).".format(
                key, "US" if us else "Canadian", "state" if us else "province",
                "SPRINGFIELD, IL" if us else "KINGSTON, ON")
            return out
        if fixed != written:
            written = fixed
            out["text"] = written
            out["found"] = find_master_location(conn, written)
    return out


def location_key(text: str) -> str:
    """master_location.key: upper case, trimmed, accents dropped."""
    import unicodedata

    text = unicodedata.normalize("NFKD", (text or "").strip())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.upper().split())


#: Readers that must see a case's place through its id: the master row's key, or the text column where no id is set (a
#: database still on the old location table, or text nothing resolved).
LOCATION_JOIN = ("LEFT JOIN master_location mo ON mo.id = e.origin_loc_id "
                 "LEFT JOIN master_location md ON md.id = e.dest_loc_id")
ORIGIN_KEY = "COALESCE(mo.key, e.origin_loc)"
DEST_KEY = "COALESCE(md.key, e.dest_loc)"


def find_master_location(conn: sqlite3.Connection, text: str):
    """The master_location row a string names: by key first, then by alias
    None when nothing matches."""
    key = location_key(text)
    if not key:
        return None
    # A retired row still owns its key, but a live place of that key (or an
    # alias pointing at the survivor of a merge) comes first.
    row = conn.execute("SELECT * FROM master_location WHERE key = ? AND status <> 'retired'",
                       (key,)).fetchone()
    if row is not None:
        return row
    written = " ".join((text or "").upper().split())
    return conn.execute(
        "SELECT l.* FROM master_location_alias a JOIN master_location l ON l.id = a.location_id "
        "WHERE a.alias IN (?, ?) ORDER BY (l.status = 'retired'), l.id LIMIT 1", (key, written)).fetchone()


#: The two Dropped reasons, which the app writes itself rather than offering
#: as opinions. Dropped means the row should not exist, so the month report
#: and the trend leave every dropped case out entirely.
#: Since 2026-09-18 a duplicate names the case it duplicates, so the reason
#: reads 'Duplicate of OPP-1412' and the settings list holds the shape.
CLOSED_DUPLICATE = "Duplicate of {}"
CLOSED_DUPLICATE_SHAPE = "Duplicate of OPP-xxxx"

#: Parties that stay placeholders however they are edited: the parking
#: places for a requester nobody has named yet.
RESERVED_PLACEHOLDERS = {"(tbc)", "unknown", "colin"}
CLOSED_IN_ERROR = "Registered in error"
NOT_REAL_CASES = (CLOSED_DUPLICATE_SHAPE, CLOSED_IN_ERROR)


def _same_text(a, b, blank_is_same: bool = False) -> bool:
    """Two free-text values are the same after trim and casefold; with
    blank_is_same a blank `a` says nothing new, so it matches anything."""
    a = (a or "").strip().casefold()
    if blank_is_same and not a:
        return True
    return a == (b or "").strip().casefold()


def _norm(value: Any) -> str | None:
    """change_log stores text; keep NULL distinguishable from ''."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _derive_vendor_quoted(values: Mapping[str, Any], current=None) -> dict:
    """One vendor status, not two (2026-09-25): `quoted` follows
    `machine_status` - 1 when it is 'Quoted', 0 otherwise - on every
    insert and update, whatever the caller passed. The column stays for
    the views, the report and the analysis export, which read it."""
    values = dict(values)
    if "machine_status" in values:
        status = values["machine_status"]
    elif current is not None:
        status = current["machine_status"]
    else:
        status = None
    values["quoted"] = 1 if (status or "").strip() == "Quoted" else 0
    return values


#: status_event.source values a business write may pass (v43: 'web', an
#: action logged on the team site); anything else is written as the default
EVENT_SOURCES = ("manual", "scan", "import", "mail", "web")
#: how a follow-up went out (v43: whatsapp, teams, other from the team site)
FOLLOWUP_CHANNELS = ("mail", "call", "whatsapp", "teams", "other")

#: FX4: a MFN member agent - a live party of type Agent with a MFN number;
#: its cases carry the MFN label (it follows the party, no per-case setting)
_MFN_SQL = ("SELECT id FROM party WHERE merged_into IS NULL AND type = 'Agent' "
            "AND trim(COALESCE(mfn_no, '')) <> ''")


def mfn_party_ids(conn) -> set:
    """FX4: the ids of the parties whose cases carry the MFN label."""
    return {r[0] for r in conn.execute(_MFN_SQL)}


def is_mfn_party(conn, party_id) -> bool:
    return bool(party_id) and conn.execute(_MFN_SQL + " AND id = ?", (party_id,)).fetchone() is not None


class Repository:
    """The only place that writes business tables."""

    def __init__(self, conn: sqlite3.Connection, source: str = "ui"):
        self.conn = conn
        self.source = source
        # What the last set_status / requote / quote_again did (13d), so a
        # page can say it: 'written', 'confirmed' (the same answer again,
        # nothing new) or 'repeat' (a Re-quoted double click).
        self.last_status_outcome = None
        # E2 (v41): every change_log row one user action writes carries the
        # same action_id. One action per commit, unless pin_action() makes
        # it one per Repository (the web: one per request, however many
        # commits the request makes).
        self.action_id = None
        self._action_pinned = False
        self._action_column = None
        # RD1b: what place_rules did to a new place ('AIR' taken off, ', IL'
        # put in), for the page to say
        self.place_notes = []

    # -- actions (E2, v41) -----------------------------------------------
    def pin_action(self) -> None:
        """Everything this repository writes from now on is one action,
        across commits (a web request)."""
        self._action_pinned = True
        self.action_id = None

    def new_action(self) -> None:
        """What follows is an action of its own (Strike out, Put back)."""
        self.action_id = None

    def _current_action(self) -> int | None:
        if self._action_column is None:
            self._action_column = "action_id" in {
                r[1] for r in self.conn.execute("PRAGMA table_info(change_log)")}
        if not self._action_column:
            return None
        if self.action_id is not None and self.conn.execute(
                "SELECT 1 FROM change_log WHERE action_id = ? LIMIT 1", (self.action_id,)).fetchone() is None:
            # nothing of it was kept (a rollback): take a fresh number
            self.action_id = None
        if self.action_id is None:
            # the row just written holds the write lock, so MAX + 1 is ours
            self.action_id = int(self.conn.execute(
                "SELECT COALESCE(MAX(action_id), 0) FROM change_log").fetchone()[0]) + 1
        return self.action_id

    # -- low level -------------------------------------------------------
    def _log(self, table: str, row_id: int, field: str, old: Any, new: Any) -> None:
        action = self._current_action()
        if self._action_column:
            self.conn.execute(
                "INSERT INTO change_log (table_name, row_id, field, old_value, new_value, source, action_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (table, row_id, field, _norm(old), _norm(new), self.source, action),
            )
            return
        self.conn.execute(
            "INSERT INTO change_log (table_name, row_id, field, old_value, new_value, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (table, row_id, field, _norm(old), _norm(new), self.source),
        )

    def insert(self, table: str, values: Mapping[str, Any]) -> int:
        if table == "party_domain" and is_internal_domain(self.conn, values.get("domain")):
            # 13g: jojofreight.example once mapped to JoJo JJA and every
            # office's mail matched LAX - our own domain names nobody
            raise ValueError("{} is our own domain; it never belongs to a party".format(values.get("domain")))
        places = {}
        if table == "enquiry_route":
            values, places = self._route_place_values(values)
        if table in ("enquiry", "enquiry_route"):
            values = self._sync_locations(values)
        if table == "vendor_request":
            values = _derive_vendor_quoted(values)
        cols = [c for c in values]
        sql = "INSERT INTO {} ({}) VALUES ({})".format(
            table, ", ".join(cols), ", ".join("?" for _ in cols)
        )
        cur = self.conn.execute(sql, [values[c] for c in cols])
        row_id = cur.lastrowid
        if table in LOGGED_TABLES:
            label = _INSERT_LABEL.get(table)
            self._log(table, row_id, "(insert)", None, values.get(label) if label else None)
        if table == "enquiry_route":
            self._write_route_places(row_id, places, values)
        if table == "party":
            self._settle_placeholder(row_id)
        if table == "enquiry":
            self.activate_requester(values.get("requester_party_id"))
        if table == "enquiry" and _has_table(self.conn, "enquiry_route"):
            # v33: every case has route A, from its own lane, whatever wrote
            # the case (the register form, an import, a test)
            self._route_guard = True
            try:
                self.add_route(row_id, {k: values.get(k) for k in ROUTE_LANE_FIELDS if k in values},
                               source="mail" if values.get("created_by") == "outlook" else "manual", sync=False)
            finally:
                self._route_guard = False
        return row_id

    def _sync_locations(self, values: Mapping[str, Any]) -> dict:
        """enquiry.origin_loc / dest_loc follow the ids once the master layer
        is active: an id given writes the master key into the text column;
        a text given without an id resolves it (key, then alias) and the
        text becomes the master key. Unknown text is left as it is with a
        NULL id, which is what the consistency check and R8 pick up."""
        values = dict(values)
        if not master_active(self.conn):
            return values
        for side in ("origin", "dest"):
            id_col, text_col = "{}_loc_id".format(side), "{}_loc".format(side)
            if id_col in values:
                if values[id_col]:
                    row = self.conn.execute(
                        "SELECT key FROM master_location WHERE id = ?", (values[id_col],)).fetchone()
                    if row is None:
                        raise ValueError("no master_location {}".format(values[id_col]))
                    values[text_col] = row["key"]
                elif text_col not in values:
                    values[text_col] = None
            elif text_col in values:
                found = find_master_location(self.conn, values[text_col]) if values[text_col] else None
                values[id_col] = found["id"] if found is not None else None
                if found is not None:
                    values[text_col] = found["key"]
        return values

    def _settle_placeholder(self, party_id: int) -> bool:
        """A placeholder party that has been given a real type is a real
        party: placeholder goes back to 0 (logged like any change). The
        reserved keys stay placeholders whatever their type says.
        """
        row = self.conn.execute(
            "SELECT key, type, placeholder FROM party WHERE id = ?", (party_id,)
        ).fetchone()
        if row is None or not row["placeholder"] or row["type"] == "Unknown":
            return False
        if (row["key"] or "").strip().casefold() in RESERVED_PLACEHOLDERS:
            return False
        self.update("party", party_id, {"placeholder": 0})
        return True

    def insert_many(self, table: str, rows: Iterable[Mapping[str, Any]]) -> list[int]:
        return [self.insert(table, row) for row in rows]

    def update(self, table: str, row_id: int, values: Mapping[str, Any], pk: str = "id") -> int:
        """Write only the fields that actually changed; log each one.

        Returns the number of changed fields.
        """
        current = self.conn.execute(
            f"SELECT * FROM {table} WHERE {pk} = ?", (row_id,)
        ).fetchone()
        if current is None:
            raise KeyError(f"{table}.{pk} = {row_id!r} not found")
        places = {}
        if table == "enquiry_route":
            values, places = self._route_place_values(values)
        if table in ("enquiry", "enquiry_route"):
            values = self._sync_locations(values)
        if table == "vendor_request":
            values = _derive_vendor_quoted(values, current)

        changed = {k: v for k, v in values.items() if _norm(current[k]) != _norm(v)}
        cols = list(changed)
        if changed:
            sql = "UPDATE {} SET {} WHERE {} = ?".format(
                table, ", ".join(f"{c} = ?" for c in cols), pk
            )
            self.conn.execute(sql, [changed[c] for c in cols] + [row_id])
            if table in LOGGED_TABLES:
                for col in cols:
                    self._log(table, row_id, col, current[col], changed[col])
            if table == "party" and pk == "id" and "placeholder" not in changed:
                self._settle_placeholder(row_id)
            if table == "enquiry" and not getattr(self, "_route_guard", False):
                lane = {k: changed[k] for k in cols if k in ROUTE_LANE_FIELDS}
                if lane and _has_table(self.conn, "enquiry_route"):
                    # v33: the case's lane is route A's copy - a lane written to
                    # the case (older callers, fixers) is written to route A
                    first = self.conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ? AND voided = 0 "
                                              "ORDER BY seq LIMIT 1", (row_id,)).fetchone()
                    if first is not None:
                        self._route_guard = True
                        try:
                            self.update("enquiry_route", first["id"], lane)
                        finally:
                            self._route_guard = False
        if table == "enquiry_route" and pk == "id":
            # v39: the places follow - a list given, or a first place (or a
            # free-text pod) written the old way
            return len(cols) + self._write_route_places(row_id, places, changed)
        return len(cols)

    def commit(self) -> None:
        self.conn.commit()
        if not self._action_pinned:
            self.action_id = None


    # -- business writes --------------------------------------------------
    # Everything the UI changes goes through one of these, so that no route
    # ever runs an UPDATE of its own.

    def touch(self, table: str, row_id: int) -> None:
        self.conn.execute(
            "UPDATE {} SET updated_at = datetime('now','localtime') "
            "WHERE id = ?".format(table), (row_id,))

    def thread_key_for(self, values: Mapping[str, Any],
                       existing=None) -> str | None:
        """The thread key a case should carry, or None to leave it alone.

        The key is the cleaned subject with the punctuation taken
        out, and it is what ties a case to its mail. Typing a subject in by
        hand should give the same key as reading it out of Outlook, so it is
        derived here rather than at one entry point.
        """
        from . import parsing

        subject = values.get("source_subject")
        had = existing["source_subject"] if existing is not None else None
        key = (values.get("thread_key") or "").strip()
        # On a new case there is nothing to have changed: a key that came
        # from the mail is kept, and a missing one is derived.
        changed = (existing is not None and "source_subject" in values
                   and (subject or "") != (had or ""))
        if not subject:
            return None
        if key and not changed:
            return None
        derived = parsing.subject_thread_key(subject)
        return derived or None

    def create_enquiry(self, values: Mapping[str, Any], status_at: str,
                       note: str | None = None, own_thread: bool = True) -> int:
        """Allocate the next case number and open the case with one event.

        AL1: `own_thread=False` - registered from a mail whose subject
        carries another case's number (an agent asking for something new in
        an old thread) - keeps no thread key and no conversation, so the
        two cases never share a thread; the mail stays its registered one."""
        values = dict(values)
        if not own_thread:
            values["thread_key"] = None
            values["conversation_id"] = None
        derived = self.thread_key_for(values) if own_thread else None
        if derived:
            values["thread_key"] = derived
        values["opp_no"] = next_opp_no(self.conn)
        status = values.get("status") or "New"
        values["status"] = status
        if not values.get("scope"):
            # SC1 (2026-11-10): a case is In when it is registered, whoever
            # asked; Out is hers only, under More. Who the reports count is
            # direct_customers_out's rule, read without touching scope.
            values["scope"] = "In"
        enquiry_id = self.insert("enquiry", values)      # route A comes with it (v33)
        source = self.source if self.source in ("manual", "scan", "migration", "import") \
            else "manual"
        if status != "New":
            # A case registered straight as Quoted still began as
            # an enquiry: a New on the received date keeps the timeline whole
            # (the same day's later stage says what the case is, STAGE_RANK).
            self.insert("status_event", {
                "enquiry_id": enquiry_id, "at": values.get("received_on") or status_at,
                "status": "New", "source": source,
            })
        self.insert("status_event", {
            "enquiry_id": enquiry_id, "at": status_at, "status": status,
            "note": note, "source": source,
        })
        return enquiry_id

    def activate_requester(self, party_id) -> bool:
        """FX4: a prospect chosen as a case's requester (registering, the
        case page, Who asked?) becomes active, through update() so the
        change_log has it."""
        if not party_id:
            return False
        row = self.conn.execute("SELECT stage FROM party WHERE id = ?", (party_id,)).fetchone()
        if row is None or row["stage"] != "prospect":
            return False
        self.update("party", party_id, {"stage": "active"})
        return True

    def save_enquiry(self, enquiry_id: int, values: Mapping[str, Any]) -> int:
        existing = self.conn.execute(
            "SELECT source_subject, thread_key FROM enquiry WHERE id = ?",
            (enquiry_id,)).fetchone()
        values = dict(values)
        if "requester_party_id" in values:
            self.activate_requester(values["requester_party_id"])
        derived = self.thread_key_for(values, existing)
        if derived and derived != (existing["thread_key"] if existing else None):
            # The subject was edited, or it never had a key: follow the subject.
            values["thread_key"] = derived
        # v33: lane fields posted to the case (older callers, the register
        # form) reach route A through update()
        changed = self.update("enquiry", enquiry_id, values)
        # with cargo lines on file the quantity fields are the lines' totals
        # (v28; per route since v33)
        if self.sync_cargo_totals(enquiry_id):
            changed += 1
        if changed:
            self.touch("enquiry", enquiry_id)
        return changed

    def add_note(self, enquiry_id: int, at: str, note: str, source: str = "manual") -> int | None:
        """A dated note on the case timeline.

        It is recorded as an event at the status the case is already at: the
        case did not change, but something happened, and the tracking rules
        that ask 'how long since anything happened' should see it. `source`
        is 'mail' when the note was read out of the archive.
        """
        note = (note or "").strip()
        row = self.conn.execute(
            "SELECT status FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone()
        if row is None or not note:
            return None
        return self.insert("status_event", {
            "enquiry_id": enquiry_id, "at": at, "status": row["status"],
            "note": note, "kind": "note",
            "source": source if source in EVENT_SOURCES else "manual",
        })

    def next_quote_seq(self, enquiry_id: int) -> int:
        """The number the next follow-up on the quote takes: the live ones of
        this quote round (on or after the latest live Quoted event) plus one.
        FX7 (v58): a wait's follow-ups (v_wait_followup) are not the quote's."""
        has_view = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'view' AND name = 'v_wait_followup'").fetchone()
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM status_event s WHERE enquiry_id = ? "
            "AND kind = 'followup' AND voided = 0 AND at >= COALESCE("
            "  (SELECT MAX(at) FROM status_event WHERE enquiry_id = ? "
            "   AND kind IN ('status','requote') AND status = 'Quoted' AND voided = 0), '')" +
            (" AND NOT EXISTS (SELECT 1 FROM v_wait_followup x WHERE x.id = s.id)" if has_view else ""),
            (enquiry_id, enquiry_id)).fetchone()[0]) + 1

    def add_followup(self, enquiry_id: int, at: str, channel: str = "mail",
                     note: str | None = None, seq: int | None = None,
                     source: str = "manual") -> int | None:
        """A follow-up on the quote (schema v13): an event of kind
        'followup' at the status the case already has. Like a note it
        changes neither enquiry.status nor status_confirmed, but it is
        activity, so the R3 ladder's clock restarts from it. `seq` is the
        next number when not given, counted within this quote round (v16):
        a case quoted again is chased from FU1, so one case can carry two
        FU1s on different dates. Voided follow-ups do not count."""
        row = self.conn.execute(
            "SELECT status FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone()
        if row is None:
            return None
        channel = channel if channel in FOLLOWUP_CHANNELS else "mail"
        if seq is None:
            seq = self.next_quote_seq(enquiry_id)
        note = (note or "").strip() or "FU{} {}".format(
            seq, {"mail": "sent", "call": "call"}.get(channel, "by " + channel))
        return self.insert("status_event", {
            "enquiry_id": enquiry_id, "at": at, "status": row["status"],
            "note": note, "kind": "followup", "seq": int(seq), "channel": channel,
            "source": source if source in EVENT_SOURCES else "manual",
        })

    def add_ack(self, enquiry_id: int, at: str, note: str | None = None, source: str = "mail") -> int | None:
        """Our acknowledgement of the case to the requester, the case number
        in the subject (AL1, v37): an event of kind 'ack' at the status the
        case already has. Our action, no status change; never a quote, a
        re-quote or a follow-up. Written once per case (case_numbers.scan)."""
        row = self.conn.execute("SELECT status FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone()
        if row is None:
            return None
        return self.insert("status_event", {
            "enquiry_id": enquiry_id, "at": at, "status": row["status"], "kind": "ack",
            "note": (note or "").strip() or "Acknowledged",
            "source": source if source in EVENT_SOURCES else "mail",
        })

    def confirm_status(self, enquiry_id: int) -> bool:
        """Somebody looked at the case: status_confirmed = 1 (logged when it
        changes). Setting a status, a hold, or accepting a suggestion all
        count; a note does not. True when it was 0 before."""
        changed = self.update("enquiry", enquiry_id, {"status_confirmed": 1})
        self.touch("enquiry", enquiry_id)
        return bool(changed)

    def latest_event(self, enquiry_id: int):
        """The event that says what the case is: the latest date, the
        furthest stage on that date, then the newest row (STAGE_RANK)."""
        return self.conn.execute(
            "SELECT * FROM status_event WHERE enquiry_id = ? "
            "AND voided = 0 AND {} ORDER BY {} LIMIT 1".format(
                status_mod.STATUS_KIND_SQL, status_mod.LATEST_EVENT_ORDER),
            (enquiry_id,)).fetchone()

    def lost_closing_event(self, enquiry_id: int) -> int | None:
        """FX1: the id of the event that closes a Lost case (its latest
        live status event, status Lost), whose Edit also carries the case's
        Their price and Our price; None on any other case."""
        latest = self.latest_event(enquiry_id)
        case = self.conn.execute("SELECT status FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone()
        if latest is None or case is None or case["status"] != "Lost" \
                or latest["kind"] != "status" or latest["status"] != "Lost":
            return None
        return latest["id"]

    def closing_refusal(self, enquiry_id: int, status: str, at: str | None) -> str:
        """C1 (6b): a closing (Won, Lost, Cancelled, Dropped) dated before the
        case's latest status event is refused - it would only be history and
        the case would stay as it is while the page said it was set. The
        message for the person, or '' when the closing may go ahead. Quoted
        and Re-quoted keep their history rule; imports and migrations call
        set_status directly and never ask."""
        from . import dates

        if status not in status_mod.CLOSED_STATUSES or not at:
            return ""
        latest = self.latest_event(enquiry_id)
        if latest is None or str(at) >= latest["at"]:
            return ""
        label = "Re-quoted" if latest["kind"] == "requote" else latest["status"]
        return ("{} on {} is before this case's latest event ({}, {}). Use a later date, "
                "or correct the earlier event first.".format(
                    status, dates.month_day(at), label, dates.month_day(latest["at"])))

    def set_status(self, enquiry_id: int, status: str, at: str,
                   note: str | None = None, closed_reason: str | None = None,
                   awarded_vendor_id: int | None = None,
                   awarded_note: str | None = None,
                   source: str = "manual", rate_used: str | None = None,
                   kind: str = "status", reason: str | None = None,
                   keep_hold: bool = False, routes_done: bool = False) -> bool:
        """A status change is always an event; it is also the field on the
        case when the event is the latest one. An event dated before the
        case's latest, or on the same day at an earlier stage, is history
        being filled in: it goes on the timeline and the case stays as it
        is. Returns True when the case's status followed the event.

        `source` says where the answer came from: 'manual' when she set it,
        'scan' when it was read out of a mail thread and applied in bulk,
        'mail' when a suggestion read out of Outlook was accepted (v12).
        """
        kind = kind if kind in status_mod.STATUS_KINDS else "status"
        latest = self.latest_event(enquiry_id)
        history = latest is not None and (at, status_mod.stage_rank(status, kind)) < \
            (latest["at"], status_mod.stage_rank(latest["status"], latest["kind"]))
        if status == "Cancelled" and kind == "status" and not routes_done and not history:
            # C1: with several routes, every route without a result - and a
            # won one: the shipment did not go ahead - is cancelled, and the
            # case follows its routes (a lost route left makes it Lost)
            status, closed_reason, routes_done = self._cancel_routes(enquiry_id, at, closed_reason)
        # The reason rides on the event (v22): the closed reason on a Lost,
        # Cancelled or Dropped event, a requote reason on a Quoted or requote event.
        if status in status_mod.REASON_STATUSES and not (reason or "").strip():
            reason = closed_reason
        if latest is not None and kind != "requote" and latest["at"] == at \
                and latest["status"] == status and latest["kind"] == kind \
                and _same_text(note, latest["note"], blank_is_same=True) \
                and _same_text(reason, latest["reason"], blank_is_same=True):
            # The same answer again, with nothing new - no note or the same
            # note, no reason or the same reason (a double click on Lost, an
            # import run twice in a day) - is not a second event: she looked
            # at the case and agreed with it, which is what confirmed means.
            # A different note or reason is written (13d, 13g). A requote
            # has its own double-click rule in requote().
            self.confirm_status(enquiry_id)
            self.last_status_outcome = "confirmed"
            return True
        self.last_status_outcome = "written"
        self.insert("status_event", {
            "enquiry_id": enquiry_id, "at": at, "status": status, "note": note,
            "kind": kind, "reason": (reason or "").strip() or None,
            "source": source if source in EVENT_SOURCES else "manual",
        })
        # The same-day rule (STAGE_RANK): a re-quote outranks the Quoted it
        # undoes, everything else keeps its stage order (v19).
        if latest is not None and (at, status_mod.stage_rank(status, kind)) < \
                (latest["at"], status_mod.stage_rank(latest["status"], latest["kind"])):
            self.confirm_status(enquiry_id)
            return False
        values = {"status": status, "status_confirmed": 1}
        # Any status change releases a hold (v11) - except a change
        # nobody chose (retire-sourcing moving a case to New), which passes
        # keep_hold.
        held = None if keep_hold else self.conn.execute(
            "SELECT hold_reason FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone()
        if held is not None and held["hold_reason"]:
            values["hold_reason"] = None
            values["hold_until"] = None
            self.add_note(enquiry_id, at, "Hold released")
        if status in status_mod.REASON_STATUSES:
            values["closed_reason"] = closed_reason
        if status == "Won":
            values["awarded_vendor_id"] = awarded_vendor_id
            values["awarded_note"] = awarded_note
            values["rate_used"] = rate_used
            values["closed_reason"] = None
        self.update("enquiry", enquiry_id, values)
        self.touch("enquiry", enquiry_id)
        if not routes_done and kind == "status":
            self._routes_follow_status(enquiry_id, status, at,
                                       closed_reason if status in ("Lost", "Cancelled") else None)
        if status == "Cancelled":
            self._close_shipments_not_shipped(enquiry_id, at, closed_reason)
        return True

    def _cancel_routes(self, enquiry_id: int, at: str, reason: str | None) -> tuple:
        """Cancelled on a case with several routes (C1): the routes without a
        result and the won ones become Cancelled with the reason. Returns
        (status, closed reason, routes_done) for set_status: Cancelled, or
        Lost with that route's reason when a lost route is left (item 4)."""
        from . import routes

        if not _has_table(self.conn, "enquiry_route"):
            return "Cancelled", reason, False
        live = routes.routes_of(self.conn, enquiry_id)
        if len(live) <= 1:
            return "Cancelled", reason, False
        for route in live:
            if not route.get("result") or route.get("result") == "Won":
                self.update("enquiry_route", route["id"], {"result": "Cancelled", "result_on": at,
                                                           "lost_reason": reason})
        live = routes.routes_of(self.conn, enquiry_id)
        if routes.status_target(live) == "Lost":
            lost = next(r for r in live if r.get("result") == "Lost")
            return "Lost", lost.get("lost_reason"), True
        return "Cancelled", reason, True

    def _close_shipments_not_shipped(self, enquiry_id: int, at: str, reason: str | None) -> None:
        """C1 (6d): a cancelled case's open shipment closes as Not shipped,
        with a note saying why; nothing else in the shipment changes."""
        from . import shipments

        if not _has_table(self.conn, "shipment"):
            return
        opp = self.conn.execute("SELECT opp_no FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone()
        for ship in self.conn.execute("SELECT id FROM shipment WHERE enquiry_id = ? AND closed = 0",
                                      (enquiry_id,)).fetchall():
            shipments.close(self, ship["id"], True, "Not shipped")
            shipments.add_note(self, ship["id"], "{} cancelled: closed as Not shipped".format(
                opp["opp_no"] if opp else "The case"), "{} {}".format(at, _dt.datetime.now().strftime("%H:%M")),
                detail=reason)

    def requote(self, enquiry_id: int, at: str, note: str | None = None,
                source: str = "manual", reason: str | None = None) -> bool:
        """Re-quoted: a revised quote went out on `at` (v30 - Re-quote
        means re-quoted, not 'they asked again'). One event, status Quoted,
        kind requote, with the reason from requote_reasons (optional) and the
        note after the 'Re-quote' mark, so the tracking row, the case page
        and an accepted suggestion all leave the same event. The case is
        Quoted afterwards and the round is quote_rounds; the follow-ups start
        again at FU1 from this date (v16)."""
        text = (note or "").strip()
        note = "Re-quote: " + text if text else "Re-quote"
        reason = (reason or "").strip() or None
        # Every Re-quoted is a round (13d), even a second one the same day.
        # Only the exact same click again within a minute - same date,
        # reason and note - is a double click and is not written twice.
        repeat = self.conn.execute(
            "SELECT 1 FROM status_event WHERE enquiry_id = ? AND kind = 'requote' "
            "AND voided = 0 AND at = ? AND COALESCE(note, '') = ? AND COALESCE(reason, '') = ? "
            "AND created_at >= datetime('now', 'localtime', '-60 seconds') LIMIT 1",
            (enquiry_id, at, note, reason or "")).fetchone()
        if repeat is not None:
            self.confirm_status(enquiry_id)
            self.last_status_outcome = "repeat"
            return True
        # kind 'requote' ranks with Quoted (v19): on the same day as another
        # quote the later one says where the case is.
        return self.set_status(enquiry_id, "Quoted", at, note=note,
                               source=source, kind="requote", reason=reason)

    def quote_again(self, enquiry_id: int, at: str, note: str | None = None,
                    reason: str | None = None, source: str = "manual") -> str:
        """Set status Quoted on a case that is already Quoted (13d): that is
        a re-quote (v22), written through requote() whatever the date, with
        its reason and note. Only a repeat with nothing new - the latest
        quote is on this date, and no reason and no note - just confirms.
        Returns last_status_outcome."""
        latest = self.latest_event(enquiry_id)
        if latest is not None and latest["status"] == "Quoted" and latest["at"] == at \
                and not (note or "").strip() and not (reason or "").strip():
            self.confirm_status(enquiry_id)
            self.last_status_outcome = "confirmed"
            return "confirmed"
        self.requote(enquiry_id, at, note=note, source=source, reason=reason)
        return self.last_status_outcome

    def attach_thread(self, enquiry_id: int, kind: str, value: str, added_on: str,
                      note: str | None = None, source: str = "manual") -> int | None:
        """Tie a mail thread to the case by hand (v20): its Outlook
        conversation id or its subject's thread key. A value already on file
        is left as it is (put back if it was struck out); the id of the new
        row, or None when nothing was written."""
        if kind not in ("conversation", "thread_key") or not (value or "").strip():
            return None
        value = value.strip()
        found = self.conn.execute(
            "SELECT id, voided FROM enquiry_thread WHERE kind = ? AND value = ?",
            (kind, value)).fetchone()
        if found is not None:
            if found["voided"]:
                self.update("enquiry_thread", found["id"], {"voided": 0})
            return None
        return self.insert("enquiry_thread", {
            "enquiry_id": enquiry_id, "kind": kind, "value": value, "added_on": added_on,
            "note": (note or "").strip()[:200] or None, "source": source})

    def void_thread(self, thread_id: int, voided: bool = True) -> None:
        """Strike an attached thread out, or put it back. Never deleted."""
        self.update("enquiry_thread", thread_id, {"voided": 1 if voided else 0})

    # -- cargo lines (v28) -------------------------------------
    def add_cargo_line(self, enquiry_id: int, values: Mapping[str, Any]) -> int:
        """One more consignment on the case; the case's totals follow."""
        from . import cargo, packing

        row = cargo.typed({k: values.get(k) for k in cargo.FIELDS})
        if "pack_unit" not in values and row.get("pack_word"):
            # v39: a line that brings only the word as written gets the
            # Packing it means on her list ('plt' -> Pallets); none if it maps to none
            row["pack_unit"] = packing.unit_for(row["pack_word"], packing.units_of(self.conn))
        row["enquiry_id"] = enquiry_id
        row["source"] = values.get("source") if values.get("source") in ("mail", "manual", "migration") else "manual"
        row["seq"] = (self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM enquiry_cargo WHERE enquiry_id = ?",
            (enquiry_id,)).fetchone()[0] or 0) + 1
        line_id = self.insert("enquiry_cargo", row)
        self.sync_cargo_totals(enquiry_id)
        return line_id

    def add_cargo_stated(self, enquiry_id: int, values: Mapping[str, Any]) -> int:
        """What a mail states about a block of cargo as a whole (v34): a
        check on the lines, never counted with them."""
        from . import cargo

        row = {k: values.get(k) for k in cargo.STATED_FIELDS if k in values}
        row["enquiry_id"] = enquiry_id
        return self.insert("enquiry_cargo_stated", row)

    def cargo_answer(self, enquiry_id: int, action: str, value: str = "", line_id: int | None = None) -> str:
        """Apply the answer to a cargo check (cargo_checks, v34). Returns a
        message, '' when the action is unknown. Every change goes through
        edit_cargo_line / update, so it is logged like any edit."""
        from . import cargo, cargo_checks

        if action == "keep_case":
            case = self.conn.execute("SELECT cargo_decided FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone()
            if case is None:
                return ""
            self.update("enquiry", enquiry_id, {"cargo_decided": cargo_checks.add_code(case["cargo_decided"], value)})
            return "Kept as it is."
        line = self.conn.execute("SELECT * FROM enquiry_cargo WHERE id = ? AND enquiry_id = ?",
                                 (line_id, enquiry_id)).fetchone()
        if line is None:
            return ""
        line = dict(line)
        change = {}
        if action == "count":
            change = {"counted": 1 if value == "1" else 0,
                      "decided": cargo_checks.add_code(line["decided"], "repeat")}
            message = "Counted." if value == "1" else "Not counted."
        elif action == "reading":
            try:
                number = float(value)
            except (TypeError, ValueError):
                return ""
            change = {"weight_value": number, "weight_reading": "chosen"}
            message = "Weight read as {} {}.".format(cargo._fmt(number, 3), line["weight_unit"] or "kg")
        elif action == "unit" and value in cargo.DIMS_UNITS:
            change = {"dims_unit": value}
            message = "Size in {}.".format(value)
        elif action == "use_size":
            change = {"cbm_value": None, "cbm": None, "decided": cargo_checks.add_code(line["decided"], "stated_cbm")}
            message = "Volume from the size."
        elif action == "keep" and value:
            change = {"decided": cargo_checks.add_code(line["decided"], value)}
            message = "Kept as it is."
        else:
            return ""
        merged = {k: line.get(k) for k in ("pieces", "dim_l", "dim_w", "dim_h", "dims_unit", "weight_value",
                                           "weight_unit", "weight_basis", "cbm_value", "cbm_basis")}
        merged.update(change)
        self.edit_cargo_line(line["id"], merged)
        return message

    def edit_cargo_line(self, line_id: int, values: Mapping[str, Any]) -> int:
        from . import cargo

        row = self.conn.execute("SELECT enquiry_id FROM enquiry_cargo WHERE id = ?", (line_id,)).fetchone()
        if row is None:
            raise KeyError("cargo line {} not found".format(line_id))
        changed = self.update("enquiry_cargo", line_id, cargo.typed(values))
        if changed:
            self.touch("enquiry_cargo", line_id)
        self.sync_cargo_totals(row["enquiry_id"])
        return changed

    def void_cargo_line(self, line_id: int, voided: bool = True) -> None:
        """Remove = strike out; never deleted. The totals follow."""
        row = self.conn.execute("SELECT enquiry_id FROM enquiry_cargo WHERE id = ?", (line_id,)).fetchone()
        if row is None:
            raise KeyError("cargo line {} not found".format(line_id))
        self.update("enquiry_cargo", line_id, {"voided": 1 if voided else 0})
        self.touch("enquiry_cargo", line_id)
        self.sync_cargo_totals(row["enquiry_id"])

    def sync_cargo_totals(self, enquiry_id: int, typed: bool = False) -> int:
        """Write the case totals (v33, routes.totals): gross_kg and volume_cbm
        from the counted lines, each once per route it applies to (a case
        with no counted line keeps what was typed); air_weight_kg the
        chargeable weight of its Air routes, lcl_cbm the volume of its LCL
        routes, container_qty / container_type from FCL lines. qty_raw is
        the text as received and is not written here. The fields changed."""
        from . import cargo, routes

        if not _has_table(self.conn, "enquiry_cargo") or not _has_table(self.conn, "enquiry_route"):
            if not _has_table(self.conn, "enquiry_cargo"):
                return 0
            fields = cargo.enquiry_fields(cargo.summary(self.conn, enquiry_id))
            return self.update("enquiry", enquiry_id, fields) if fields else 0
        case = self.conn.execute("SELECT * FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone()
        if case is None:
            return 0
        # 15b: a case that never had a line keeps what it carries; once it
        # has lines, its quantities and its commodity are the counted lines'
        # (nothing counted: nothing), whatever was typed before
        if self.conn.execute("SELECT 1 FROM enquiry_cargo WHERE enquiry_id = ? LIMIT 1",
                             (enquiry_id,)).fetchone() is None:
            if not typed:
                return 0
            # the v33 upgrade: the typed numbers recomputed the v33 way
            return self.update("enquiry", enquiry_id, routes.enquiry_fields(
                routes.totals(self.conn, enquiry_id, case=case), case))
        t = routes.totals(self.conn, enquiry_id, case=case)
        fields = routes.enquiry_fields(t, case)
        lines = cargo.lines_of(self.conn, enquiry_id)
        if t["lines_counted"]:
            fields["container_qty"] = t["container_qty"]
            fields["container_type"] = t["container_type"]
        else:
            fields.update({"gross_kg": None, "volume_cbm": None, "air_weight_kg": None, "lcl_cbm": None,
                           "container_qty": None, "container_type": None})
        fields["commodity"] = cargo.commodity_of(lines)
        return self.update("enquiry", enquiry_id, fields)

    sync_totals = sync_cargo_totals

    # -- routes (v33) ------------------------------------------
    def add_route(self, enquiry_id: int, values: Mapping[str, Any], source: str = "manual",
                  sync: bool = True, follow_case: bool = True) -> int:
        """One more route on the case (seq = the next letter); route A's copy
        and the totals follow. Batch 23 (item 7): a route added to a case
        that is already Won, Lost or Cancelled starts with the case's result,
        its reason and the date the case was closed (route_result_from_case)
        - when the caller says nothing about the result, or gives the case's
        own. A result given as empty (the route editor's 'no result') or a
        different one is kept as given. The v33 upgrade (history) passes
        follow_case=False."""
        from . import routes

        row = {k: values.get(k) for k in routes.ROUTE_FIELDS if k in values}
        row.update({k: values[k] for k in ROUTE_PLACE_FIELDS if k in values})
        row["enquiry_id"] = enquiry_id
        row["source"] = source if source in ("manual", "mail", "migration") else "manual"
        row["seq"] = (self.conn.execute("SELECT COALESCE(MAX(seq), 0) FROM enquiry_route WHERE enquiry_id = ?",
                                        (enquiry_id,)).fetchone()[0] or 0) + 1
        if follow_case and row["seq"] > 1 and ("result" not in values or values.get("result")):
            row.update(self.route_result_from_case(enquiry_id, row))
        route_id = self.insert("enquiry_route", row)
        if sync:
            self.sync_route_a(enquiry_id)
            self.sync_cargo_totals(enquiry_id)
        return route_id

    def closing_date(self, enquiry_id: int, status: str) -> str | None:
        """The date of the event the case is closed by: its latest event
        when that is the closing one, else its latest live closing event of
        that status."""
        latest = self.latest_event(enquiry_id)
        if latest is not None and latest["status"] == status:
            return latest["at"]
        row = self.conn.execute(
            "SELECT MAX(at) FROM status_event WHERE enquiry_id = ? AND status = ? AND kind = 'status' "
            "AND voided = 0", (enquiry_id, status)).fetchone()
        return row[0] if row is not None else None

    def route_result_from_case(self, enquiry_id: int, values: Mapping[str, Any]) -> dict:
        """Batch 23 (item 7): what a route added to a closed case starts
        with - the case's result (Won, Lost, Cancelled) when the route
        brings none, the case's reason on a Lost or Cancelled one, and as
        result_on the date of the event the case is closed by. A result
        given that is the case's own gets the same date, and the case's
        reason when it brings none. {} on an open case (or a Dropped one)
        and for a different result."""
        from . import routes

        case = self.conn.execute("SELECT status, closed_reason FROM enquiry WHERE id = ?",
                                 (enquiry_id,)).fetchone()
        if case is None or case["status"] not in routes.RESULTS:
            return {}
        given = values.get("result") or None
        if given is not None and given != case["status"]:
            return {}
        out = {"result": case["status"]}
        if not values.get("result_on"):
            out["result_on"] = self.closing_date(enquiry_id, case["status"])
        if case["status"] in routes.REASON_RESULTS:
            if given is None or not (values.get("lost_reason") or "").strip():
                out["lost_reason"] = case["closed_reason"]
        else:
            out["lost_reason"] = None
        return out

    def _route_place_values(self, values: Mapping[str, Any]) -> tuple:
        """A route write split into its columns and its places (v39): the
        ids of dest_places / pod_places checked against the master; the
        first destination becomes dest_loc_id and dest_loc, the ports the
        readable copy in pod. (values, {'dest': [ids], 'pod': [ids]}) - a
        side absent from the second when the write did not give it."""
        values = dict(values)
        places = {}
        for side, field in (("dest", "dest_places"), ("pod", "pod_places")):
            if field not in values:
                continue
            ids = []
            for raw in values.pop(field) or []:
                if raw in (None, ""):
                    continue
                if int(raw) not in ids:
                    ids.append(int(raw))
            if len(ids) > MAX_ROUTE_PLACES:
                raise ValueError("a route's {} takes {} places at most".format(
                    "Dest. location" if side == "dest" else "Port of discharge", MAX_ROUTE_PLACES))
            found = {}
            if ids:
                found = {r["id"]: r for r in self.conn.execute(
                    "SELECT id, key FROM master_location WHERE id IN ({})".format(",".join("?" for _ in ids)), ids)}
                missing = [i for i in ids if i not in found]
                if missing:
                    raise ValueError("no master_location {}".format(missing[0]))
            places[side] = ids
            if side == "dest" and ids:
                values["dest_loc_id"], values["dest_loc"] = ids[0], found[ids[0]]["key"]
            elif side == "pod" and ids:
                values["pod"] = " / ".join(found[i]["key"] for i in ids)
        return values, places

    def _route_place_ids(self, route_id: int, side: str) -> list:
        return [r["location_id"] for r in self.conn.execute(
            "SELECT location_id FROM enquiry_route_place WHERE route_id = ? AND side = ? AND voided = 0 "
            "ORDER BY seq", (route_id, side))]

    def _write_route_places(self, route_id: int, places: Mapping[str, list], changed: Mapping[str, Any]) -> int:
        """Keep enquiry_route_place in step with a route write (v39): the
        list given; else, when the first destination was written the old way
        (route A from the case, a fixer, a script), the first place follows
        it and the others stay (a destination cleared clears them); a pod
        written as free text has no places behind it any more. The rows
        changed."""
        if not _has_table(self.conn, "enquiry_route_place"):
            return 0
        done = 0
        if "dest" in places or "dest_loc_id" in changed:
            row = self.conn.execute("SELECT dest_loc_id FROM enquiry_route WHERE id = ?", (route_id,)).fetchone()
            first = row["dest_loc_id"] if row is not None else None
            if places.get("dest"):
                ids = places["dest"]
            elif "dest" in places:
                ids = [first] if first else []
            else:
                kept = self._route_place_ids(route_id, "dest")
                ids = [first] + [i for i in kept[1:] if i != first] if first else []
            done += self._set_route_places(route_id, "dest", ids)
        if "pod" in places:
            done += self._set_route_places(route_id, "pod", places["pod"])
        elif "pod" in changed:
            done += self._set_route_places(route_id, "pod", [])
        return done

    def _set_route_places(self, route_id: int, side: str, ids) -> int:
        """One side's places, in order: the row of each position written
        (logged), the positions past the end struck out - never deleted."""
        rows = {r["seq"]: r for r in self.conn.execute(
            "SELECT * FROM enquiry_route_place WHERE route_id = ? AND side = ? ORDER BY seq", (route_id, side))}
        done = 0
        for seq, location_id in enumerate(ids, start=1):
            row = rows.get(seq)
            if row is None:
                self.insert("enquiry_route_place", {"route_id": route_id, "side": side, "seq": seq,
                                                    "location_id": location_id})
                done += 1
            elif self.update("enquiry_route_place", row["id"], {"location_id": location_id, "voided": 0}):
                self.touch("enquiry_route_place", row["id"])
                done += 1
        for seq, row in rows.items():
            if seq > len(ids) and not row["voided"]:
                self.update("enquiry_route_place", row["id"], {"voided": 1})
                self.touch("enquiry_route_place", row["id"])
                done += 1
        return done

    def routes_follow_location(self, location_id: int, keep_id: int | None = None) -> int:
        """Batch 23: the routes that point at a place - their origin, any
        place of their Dest. location, their Port of discharge - after a
        merge (`keep_id`, the place that stays) or a new key: the ids and
        the text written from them (dest_loc, the pod copy) follow, through
        the repository. Live and struck-out routes alike. The routes changed."""
        if not _has_table(self.conn, "enquiry_route"):
            return 0
        target = keep_id or location_id
        found = set()
        for column in ("origin_loc_id", "dest_loc_id"):
            found.update(r["id"] for r in self.conn.execute(
                "SELECT id FROM enquiry_route WHERE {} = ?".format(column), (location_id,)))
        places = _has_table(self.conn, "enquiry_route_place")
        if places:
            found.update(r["route_id"] for r in self.conn.execute(
                "SELECT DISTINCT route_id FROM enquiry_route_place WHERE location_id = ? AND voided = 0",
                (location_id,)))
        done = 0
        for route_id in sorted(found):
            route = self.conn.execute("SELECT * FROM enquiry_route WHERE id = ?", (route_id,)).fetchone()
            values = {}
            if route["origin_loc_id"] == location_id:
                values["origin_loc_id"] = target
            for side, field in (("dest", "dest_places"), ("pod", "pod_places")):
                ids = self._route_place_ids(route_id, side) if places else []
                if location_id in ids:
                    values[field] = []
                    for i in ids:
                        i = target if i == location_id else i
                        if i not in values[field]:
                            values[field].append(i)
            if route["dest_loc_id"] == location_id and "dest_places" not in values:
                values["dest_loc_id"] = target
            if values and self.update("enquiry_route", route_id, values):
                self.touch("enquiry_route", route_id)
                done += 1
        return done

    def edit_route(self, route_id: int, values: Mapping[str, Any], at: str | None = None) -> int:
        """Change a route; a result given here moves the OPP status the way
        the routes say (sync_status_from_routes)."""
        from . import routes

        row = self.conn.execute("SELECT * FROM enquiry_route WHERE id = ?", (route_id,)).fetchone()
        if row is None:
            raise KeyError("route {} not found".format(route_id))
        values = {k: v for k, v in values.items() if k in routes.ROUTE_FIELDS or k in ROUTE_PLACE_FIELDS}
        if "result" in values:
            if values["result"] not in routes.RESULTS:
                values["result"], values["result_on"], values["lost_reason"] = None, None, None
            else:
                values.setdefault("result_on", row["result_on"] or at)
                if values["result"] not in routes.REASON_RESULTS:
                    values["lost_reason"] = None
        changed = self.update("enquiry_route", route_id, values)
        if changed:
            self.touch("enquiry_route", route_id)
            self.sync_route_a(row["enquiry_id"])
            self.sync_cargo_totals(row["enquiry_id"])
            if "result" in values and (values["result"] or None) != (row["result"] or None):
                self.sync_status_from_routes(row["enquiry_id"], values.get("result_on") or at, changed_route=route_id)
            elif "lost_reason" in values and row["result"] in routes.REASON_RESULTS \
                    and (values["lost_reason"] or None) != (row["lost_reason"] or None):
                self._case_reason_from_routes(row["enquiry_id"], values["lost_reason"] or None)
        return changed

    def _case_reason_from_routes(self, enquiry_id: int, reason: str | None) -> None:
        """C1 (6a): a closing reason is one fact. A route's reason changed on
        a closed case, its result unchanged, is the case's reason and its
        closing event's too - when the case has one route, or every closed
        route now carries that reason."""
        from . import routes

        case = self.conn.execute("SELECT status, closed_reason FROM enquiry WHERE id = ?",
                                 (enquiry_id,)).fetchone()
        if case is None or case["status"] not in routes.REASON_RESULTS:
            return
        live = routes.routes_of(self.conn, enquiry_id)
        closed = [r for r in live if r.get("result") in routes.REASON_RESULTS]
        if not (len(live) == 1 or (closed and all((r.get("lost_reason") or None) == reason for r in closed))):
            return
        if (case["closed_reason"] or None) != reason:
            self.update("enquiry", enquiry_id, {"closed_reason": reason})
            self.touch("enquiry", enquiry_id)
        latest = self.latest_event(enquiry_id)
        if latest is not None and latest["status"] == case["status"] and (latest["reason"] or None) != reason:
            self.update("status_event", latest["id"], {"reason": reason})

    def void_route(self, route_id: int, voided: bool = True) -> None:
        """Strike a route out, or put it back. Never deleted. The last live
        route cannot be struck out."""
        row = self.conn.execute("SELECT enquiry_id FROM enquiry_route WHERE id = ?", (route_id,)).fetchone()
        if row is None:
            raise KeyError("route {} not found".format(route_id))
        if voided and self.conn.execute("SELECT COUNT(*) FROM enquiry_route WHERE enquiry_id = ? AND voided = 0",
                                        (row["enquiry_id"],)).fetchone()[0] <= 1:
            raise ValueError("a case keeps at least one route")
        self.update("enquiry_route", route_id, {"voided": 1 if voided else 0})
        self.touch("enquiry_route", route_id)
        self.sync_route_a(row["enquiry_id"])
        self.sync_cargo_totals(row["enquiry_id"])

    def sync_route_a(self, enquiry_id: int) -> int:
        """The enquiry's lane fields, mode and incoterm as a copy of route A
        (the first live route by seq). The fields changed."""
        from . import routes

        first = self.conn.execute("SELECT * FROM enquiry_route WHERE enquiry_id = ? AND voided = 0 "
                                  "ORDER BY seq LIMIT 1", (enquiry_id,)).fetchone()
        if first is None:
            return 0
        case = self.conn.execute("SELECT * FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone()
        differs = case is not None and any(_norm(case[k]) != _norm(first[k]) for k in routes.LANE_FIELDS)
        if differs and first["seq"] == 1 and (case["updated_at"] or "") > (first["updated_at"] or ""):
            # the case's lane was written after route A by something that did
            # not know about routes (an app still on older code, a raw fix):
            # the newer copy wins, so that edit is not overwritten
            self._route_guard = True
            try:
                self.update("enquiry_route", first["id"], {k: case[k] for k in routes.LANE_FIELDS})
            finally:
                self._route_guard = False
            return 0
        values = {k: first[k] for k in routes.LANE_FIELDS}
        self._route_guard = True
        try:
            return self.update("enquiry", enquiry_id, values)
        finally:
            self._route_guard = False

    def record_results(self, enquiry_id: int, at: str, won_ids, lost: Mapping[int, str | None],
                       note: str | None = None, source: str = "manual", cancelled=None, **won_values) -> None:
        """Set status Won with its routes: the ticked routes
        are Won, an unticked one given a reason is Lost (or, C1, Cancelled)
        with it, the rest keep no result. The timeline gets one Lost or
        Cancelled event per reason, then one Won event naming its routes -
        the OPP is Won."""
        from . import routes

        live = {r["id"]: r for r in routes.routes_of(self.conn, enquiry_id)}
        by_reason = {}
        for result, given in (("Lost", lost), ("Cancelled", cancelled or {})):
            for route_id, reason in given.items():
                if route_id in live and route_id not in won_ids:
                    self.update("enquiry_route", route_id, {"result": result, "result_on": at, "lost_reason": reason})
                    by_reason.setdefault((result, reason or ""), []).append(live[route_id])
        for route_id in won_ids:
            if route_id in live:
                self.update("enquiry_route", route_id, {"result": "Won", "result_on": at, "lost_reason": None})
        ev_source = source if source in EVENT_SOURCES else "manual"
        for (result, reason), lost_routes in by_reason.items():
            self.insert("status_event", {
                "enquiry_id": enquiry_id, "at": at, "status": result, "kind": "status",
                "reason": reason or None, "source": ev_source,
                "note": " \u00b7 ".join(p for p in (", ".join(
                    "{} {}".format(r["letter"], routes.place(r, "dest")) for r in lost_routes), reason) if p)})
        won_text = ", ".join("{} {}".format(live[i]["letter"], routes.place(live[i], "dest"))
                             for i in won_ids if i in live)
        self.set_status(enquiry_id, "Won", at, note=" \u00b7 ".join(p for p in (won_text, note) if p) or None,
                        source=source, routes_done=True, **won_values)

    def sync_status_from_routes(self, enquiry_id: int, at: str | None, changed_route: int | None = None) -> None:
        """The OPP status follows its routes (routes.status_target): Won when
        any route is won, Lost or Cancelled once every route has a result,
        otherwise unchanged - a route closed while others are open is a note
        on the timeline. A route won never clears what the case already has
        (Rate used, the awarded vendor and its note; C1 6c)."""
        from . import routes

        at = at or _dt.date.today().isoformat()
        live = routes.routes_of(self.conn, enquiry_id)
        target = routes.status_target(live)
        current = self.conn.execute("SELECT status FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone()["status"]
        changed = next((r for r in live if r["id"] == changed_route), None)
        if target and target != current:
            if target == "Won":
                won = [r for r in live if r.get("result") == "Won"]
                kept = self.conn.execute("SELECT awarded_vendor_id, awarded_note, rate_used FROM enquiry "
                                         "WHERE id = ?", (enquiry_id,)).fetchone()
                self.set_status(enquiry_id, "Won", at, note=", ".join(
                    "{} {}".format(r["letter"], routes.place(r, "dest")) for r in won), routes_done=True,
                    awarded_vendor_id=kept["awarded_vendor_id"], awarded_note=kept["awarded_note"],
                    rate_used=kept["rate_used"])
            else:
                giving = changed if changed is not None and changed.get("result") == target else \
                    next(r for r in live if r.get("result") == target)
                self.set_status(enquiry_id, target, at, closed_reason=giving.get("lost_reason"), routes_done=True,
                                note=", ".join("{} {}".format(r["letter"], routes.place(r, "dest")) for r in live))
        elif changed is not None and changed.get("result"):
            self.add_note(enquiry_id, at, "Route {} {}: {}{}".format(
                changed["letter"], routes.place(changed, "dest"), changed["result"],
                " \u00b7 " + changed["lost_reason"] if changed.get("lost_reason") else ""))

    def _routes_follow_status(self, enquiry_id: int, status: str, at: str, reason: str | None) -> None:
        """Set status without the Won picker (a suggestion, an import, the
        Lost choice): one route follows the case; with several, Lost (or
        Cancelled) marks every route without a result, Won leaves them to be
        ticked."""
        from . import routes

        if not _has_table(self.conn, "enquiry_route"):
            return
        live = routes.routes_of(self.conn, enquiry_id)
        if len(live) == 1:
            route = live[0]
            if status in routes.RESULTS:
                values = {"result": status, "result_on": at,
                          "lost_reason": reason if status in routes.REASON_RESULTS else None}
            else:
                values = {"result": None, "result_on": None, "lost_reason": None}
            if any((route.get(k) or None) != v for k, v in values.items()):
                self.update("enquiry_route", route["id"], values)
        elif status in routes.REASON_RESULTS:
            for route in live:
                if not route.get("result"):
                    self.update("enquiry_route", route["id"], {"result": status, "result_on": at, "lost_reason": reason})

    def split_cargo_line(self, line_id: int, at: str, note: str | None = None) -> int:
        """A line becomes its own case: a new enquiry with the
        same requester, mode, incoterm, lane, received_on and thread fields,
        in the same request group, status New, the line moved over. Returns
        the new enquiry id."""
        line = self.conn.execute("SELECT * FROM enquiry_cargo WHERE id = ?", (line_id,)).fetchone()
        if line is None:
            raise KeyError("cargo line {} not found".format(line_id))
        source = self.conn.execute("SELECT * FROM enquiry WHERE id = ?", (line["enquiry_id"],)).fetchone()
        values = {k: source[k] for k in (
            "requester_party_id", "end_customer", "owner", "service_mode", "incoterm",
            "origin_country", "origin_loc", "origin_loc_id", "origin_alt",
            "dest_country", "dest_loc", "dest_loc_id", "dest_alt", "special_handling",
            "received_on", "entry_id", "conversation_id", "thread_key", "source_subject", "scope")}
        values["group_id"] = source["group_id"] or source["id"]
        values["created_by"] = "manual"
        values["commodity"] = line["commodity"] or source["commodity"]
        if line["deliver_to"]:
            values["dest_alt"] = line["deliver_to"]
        values["status"] = "New"
        text = "split from {}".format(source["opp_no"])
        if note:
            text += ". " + note
        new_id = self.create_enquiry(values, status_at=at, note=text)
        new_no = self.conn.execute("SELECT opp_no FROM enquiry WHERE id = ?", (new_id,)).fetchone()["opp_no"]
        self.update("enquiry_cargo", line_id, {"enquiry_id": new_id, "seq": 1})
        self.touch("enquiry_cargo", line_id)
        self.add_note(source["id"], at, "cargo line {} split into {}".format(
            line["label"] or line["seq"], new_no))
        self.sync_cargo_totals(new_id)
        self.sync_cargo_totals(source["id"])
        self.touch("enquiry", source["id"])
        return new_id

    def join_request(self, enquiry_id: int, other_id: int, at: str) -> bool:
        """This case and another are one request: this case,
        and every case already grouped with it, joins the other's request
        group. A note records it. False when they already share one."""
        mine = self.conn.execute("SELECT id, opp_no, COALESCE(group_id, id) AS request_group "
                                 "FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone()
        other = self.conn.execute("SELECT id, opp_no, COALESCE(group_id, id) AS request_group "
                                  "FROM enquiry WHERE id = ?", (other_id,)).fetchone()
        if mine is None or other is None:
            raise ValueError("no such case")
        if mine["request_group"] == other["request_group"]:
            return False
        members = self.conn.execute(
            "SELECT id FROM enquiry WHERE COALESCE(group_id, id) = ?", (mine["request_group"],)).fetchall()
        for member in members:
            self.update("enquiry", member["id"], {"group_id": other["request_group"]})
            self.touch("enquiry", member["id"])
        self.add_note(enquiry_id, at, "same request as {}".format(other["opp_no"]))
        return True

    def set_hold(self, enquiry_id: int, reason: str, at: str,
                 until: str | None = None) -> None:
        """Park the case: the reason (required) and a date (optional) go on
        the case, and a note event says so. The status is untouched."""
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("a hold needs a reason")
        until = (until or "").strip() or None
        self.update("enquiry", enquiry_id, {"hold_reason": reason, "hold_until": until,
                                            "status_confirmed": 1})
        self.touch("enquiry", enquiry_id)
        self.add_note(enquiry_id, at, "On hold: {}{}".format(
            reason, ", until {}".format(until) if until else ""))

    def release_hold(self, enquiry_id: int, at: str) -> bool:
        """Clear the hold by hand. False when the case was not on hold."""
        row = self.conn.execute(
            "SELECT hold_reason FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone()
        if row is None or not row["hold_reason"]:
            return False
        self.update("enquiry", enquiry_id, {"hold_reason": None, "hold_until": None})
        self.touch("enquiry", enquiry_id)
        self.add_note(enquiry_id, at, "Hold released")
        return True

    def set_rate_used(self, enquiry_id: int, rate_used: str | None,
                      party_id: int | None = None,
                      vendor_request_id: int | None = None,
                      note: str | None = None) -> None:
        """Whose rate a won case used - one of four answers, changeable
        afterwards, every change logged like any other field.

        'agent' names a vendor (a vendor request on the case, which then
        carries the awarded flag, or any party); 'office' names one of our
        office; 'carrier' names the airline / line / trucker booked direct
        (when known); 'own' and 'not_recorded' name nobody.
        """
        from .status import RATE_USED_LABELS

        if rate_used is not None and rate_used not in RATE_USED_LABELS:
            raise ValueError("not a rate_used value: {!r}".format(rate_used))
        if vendor_request_id:
            row = self.conn.execute(
                "SELECT vendor_party_id FROM vendor_request "
                "WHERE id = ? AND enquiry_id = ?",
                (vendor_request_id, enquiry_id)).fetchone()
            if row is None:
                raise ValueError("vendor request {} is not on case {}".format(
                    vendor_request_id, enquiry_id))
            party_id = row["vendor_party_id"]
        if rate_used not in ("agent", "office", "carrier"):
            party_id = None
        self.award_vendor(enquiry_id,
                          vendor_request_id if rate_used == "agent" else None)
        self.update("enquiry", enquiry_id, {
            "rate_used": rate_used, "awarded_vendor_id": party_id,
            "awarded_note": (note or "").strip() or None})
        self.touch("enquiry", enquiry_id)

    def void_vendor_request(self, vendor_request_id: int, voided: bool = True) -> list:
        """Strike a vendor request out, or put it back. Never deleted.

        A struck-out request stays on the case, dimmed, and counts for no
        report, rule or award. E2: the suggestion it was
        accepted from is pending again; Put back writes the strike-out's
        changes back (the suggestion accepted again). What to tell her."""
        from . import undo

        self.new_action()
        if not voided:
            return undo.put_back(self, "vendor_request", vendor_request_id)
        self.update("vendor_request", vendor_request_id, {"voided": 1, "awarded": 0})
        self.touch("vendor_request", vendor_request_id)
        return undo.suggestion_back(self, "vendor_request:{}".format(vendor_request_id))

    def edit_status_event(self, event_id: int, values: Mapping[str, Any]) -> int:
        """Correct one event. Every field change is logged like any other.
        C1 (6a): the event a case is closed by carries the case's closing
        reason - editing its reason or its closing status changes
        enquiry.closed_reason and the routes that carried the old reason."""
        from . import routes

        before = self.conn.execute("SELECT * FROM status_event WHERE id = ?", (event_id,)).fetchone()
        changed = self.update("status_event", event_id, values)
        if changed:
            self._restate(event_id)
        if not changed or before is None:
            return changed
        after = self.conn.execute("SELECT * FROM status_event WHERE id = ?", (event_id,)).fetchone()
        latest = self.latest_event(after["enquiry_id"])
        if latest is None or latest["id"] != event_id or after["kind"] != "status":
            return changed
        old_status, new_status = before["status"], after["status"]
        old_reason, new_reason = before["reason"] or None, after["reason"] or None
        if old_status == new_status and old_reason == new_reason:
            return changed
        enquiry_id = after["enquiry_id"]
        if new_status in status_mod.REASON_STATUSES:
            case = self.conn.execute("SELECT closed_reason FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone()
            if (case["closed_reason"] or None) != new_reason:
                self.update("enquiry", enquiry_id, {"closed_reason": new_reason})
                self.touch("enquiry", enquiry_id)
        if old_status in routes.REASON_RESULTS and new_status in routes.REASON_RESULTS:
            for route in routes.routes_of(self.conn, enquiry_id):
                if route.get("result") == old_status and (route.get("lost_reason") or None) == old_reason:
                    self.update("enquiry_route", route["id"], {"result": new_status, "lost_reason": new_reason})
        return changed

    def void_status_event(self, event_id: int, voided: bool = True) -> list:
        """Strike an event out, or put it back. Nothing is ever deleted.

        A voided event stays on the case and stays on the screen, struck out,
        because it happened - somebody recorded it. What changes is that no
        derived date and no tracking rule counts it any more.
        E2 (v41): striking out a status event undoes the action that wrote
        it, and Put back writes it again (undo.py). Returns the lines to tell
        her (fields left as they were, the suggestion back)."""
        from . import undo

        self.new_action()
        if voided:
            return undo.strike_status_event(self, event_id)
        return undo.put_back_status_event(self, event_id)

    def _restate(self, event_id: int) -> None:
        """Bring enquiry.status back in line with the events that still count.

        The case says what its last event says. Editing or voiding
        an event can change which event that is, so the field follows.
        """
        row = self.conn.execute(
            "SELECT enquiry_id FROM status_event WHERE id = ?", (event_id,)
        ).fetchone()
        if row is None:
            return
        latest = self.latest_event(row["enquiry_id"])
        if latest is None:
            return
        current = self.conn.execute(
            "SELECT status FROM enquiry WHERE id = ?", (row["enquiry_id"],)
        ).fetchone()
        if current is not None and current["status"] != latest["status"]:
            self.update("enquiry", row["enquiry_id"],
                        {"status": latest["status"]})
            self.touch("enquiry", row["enquiry_id"])

    def mark_duplicate(self, enquiry_id: int, of_enquiry_id: int, at: str,
                       note: str | None = None) -> None:
        """This case is the same request as another one.

        The case is dropped rather than removed, keeps its number, and joins
        the surviving case's request group so the two read as one request.
        """
        row = self.conn.execute(
            "SELECT opp_no, COALESCE(group_id, id) AS request_group "
            "FROM enquiry WHERE id = ?", (of_enquiry_id,)).fetchone()
        if row is None:
            raise ValueError("no case {}".format(of_enquiry_id))
        reason = CLOSED_DUPLICATE.format(row["opp_no"])
        text = reason
        if note:
            text += ". " + note
        self.set_status(enquiry_id, "Dropped", at, note=text,
                        closed_reason=reason)
        self.update("enquiry", enquiry_id, {"group_id": row["request_group"]})
        self.touch("enquiry", enquiry_id)

    def mark_registered_in_error(self, enquiry_id: int, at: str,
                                 note: str | None = None) -> None:
        """This case should never have been opened. The number stays used."""
        self.set_status(enquiry_id, "Dropped", at,
                        note=note or "Registered in error",
                        closed_reason=CLOSED_IN_ERROR)

    def award_vendor(self, enquiry_id: int, vendor_request_id: int | None) -> None:
        """Exactly one vendor request per case carries the awarded flag."""
        for row in self.conn.execute(
            "SELECT id, awarded FROM vendor_request WHERE enquiry_id = ?",
            (enquiry_id,)
        ).fetchall():
            wanted = 1 if row["id"] == vendor_request_id else 0
            if row["awarded"] != wanted:
                self.update("vendor_request", row["id"], {"awarded": wanted})

    def remove_award(self, enquiry_id: int, vendor_request_id: int) -> str | None:
        """OP1: the awarded flag off and the case's Rate used back to not
        answered (rate_used, awarded_vendor_id and awarded_note empty),
        every field logged. The vendor's key, or None when that request is
        not the case's awarded one (nothing changed)."""
        row = self.conn.execute(
            "SELECT vr.awarded, p.key FROM vendor_request vr JOIN party p ON p.id = vr.vendor_party_id "
            "WHERE vr.id = ? AND vr.enquiry_id = ?", (vendor_request_id, enquiry_id)).fetchone()
        if row is None or not row["awarded"]:
            return None
        self.set_rate_used(enquiry_id, None)
        return row["key"]

    def merge_party(self, keep_id: int, drop_id: int, today: str | None = None) -> dict:
        """Two rows for one company: keep one, point everything at it.

        Every enquiry, vendor request, contact, event and domain of the
        dropped party moves to the kept one - each move a logged field
        change, because they are business rows. The dropped party's row is
        not deleted: it is marked merged_into and its
        names go into the kept party's aliases, so the next mail from them
        still finds the survivor and the old name is never lost.
        """
        import datetime as dt
        import json

        if keep_id == drop_id:
            raise ValueError("a party cannot be merged into itself")
        keep = self.conn.execute("SELECT * FROM party WHERE id = ?", (keep_id,)).fetchone()
        drop = self.conn.execute("SELECT * FROM party WHERE id = ?", (drop_id,)).fetchone()
        if keep is None or drop is None:
            raise ValueError("no such party")
        if keep["merged_into"] or drop["merged_into"]:
            raise ValueError("one of them was merged already")
        moved = {}
        for table, column in (("enquiry", "requester_party_id"),
                              ("enquiry", "awarded_vendor_id"),
                              ("vendor_request", "vendor_party_id"),
                              ("contact", "party_id"),
                              ("party_event", "party_id")):
            rows = self.conn.execute(
                "SELECT id FROM {} WHERE {} = ?".format(table, column),
                (drop_id,)).fetchall()
            for row in rows:
                self.update(table, row["id"], {column: keep_id})
            moved["{}.{}".format(table, column)] = len(rows)
        domains = self.conn.execute(
            "SELECT domain FROM party_domain WHERE party_id = ?", (drop_id,)).fetchall()
        for row in domains:
            self.update("party_domain", row["domain"], {"party_id": keep_id}, pk="domain")
        moved["party_domain"] = len(domains)

        try:
            aliases = json.loads(keep["aliases"] or "[]")
        except ValueError:
            aliases = []
        for name in (drop["key"], drop["full_name"]):
            if name and name not in aliases and name.casefold() != (keep["key"] or "").casefold():
                aliases.append(name)
        try:
            for name in json.loads(drop["aliases"] or "[]"):
                if name not in aliases:
                    aliases.append(name)
        except ValueError:
            pass
        today = today or dt.date.today().isoformat()
        note = "Merged {} into this party on {}".format(drop["key"], today)
        keep_values = {
            "aliases": json.dumps(aliases, ensure_ascii=False),
            "notes": ((keep["notes"] or "").rstrip() + "\n" + note).strip(),
        }
        if drop["mfn_member"] == 1 and keep["mfn_member"] != 1:
            keep_values["mfn_member"] = 1
        if not keep["full_name"] and drop["full_name"]:
            keep_values["full_name"] = drop["full_name"]
        self.update("party", keep_id, keep_values)
        self.touch("party", keep_id)
        self.update("party", drop_id, {
            "merged_into": keep_id,
            "notes": ((drop["notes"] or "").rstrip() + "\nMerged into {} on {}".format(
                keep["key"], today)).strip()})
        self.touch("party", drop_id)
        return moved

    def rename_party(self, party_id: int, new_key: str) -> bool:
        """Give a party its proper name; the old key becomes an alias so
        the next mail from them still matches. False when nothing changed
        or the new key belongs to another party."""
        new_key = (new_key or "").strip()
        row = self.conn.execute(
            "SELECT key, full_name, aliases FROM party WHERE id = ?", (party_id,)).fetchone()
        if row is None or not new_key or new_key == row["key"]:
            return False
        other = self.conn.execute(
            "SELECT id FROM party WHERE lower(trim(key)) = ? AND id <> ?",
            (new_key.lower(), party_id)).fetchone()
        if other is not None:
            return False
        try:
            aliases = json.loads(row["aliases"] or "[]")
        except ValueError:
            aliases = []
        old = row["key"]
        if old and old not in aliases and old.casefold() != new_key.casefold():
            aliases.append(old)
        values = {"key": new_key, "aliases": json.dumps(aliases, ensure_ascii=False)}
        if not row["full_name"] or row["full_name"] == old:
            values["full_name"] = new_key
        self.update("party", party_id, values)
        self.touch("party", party_id)
        return True

    def attach_domain(self, party_id, domain) -> str:
        """Remember a mail domain for a party (13g: the one way in). Not an
        internal domain, not one somebody already has. The domain when it
        was written, else ''."""
        domain = (domain or "").strip().lower().lstrip("@")
        if not domain or not party_id or is_internal_domain(self.conn, domain):
            return ""
        if self.conn.execute("SELECT 1 FROM party_domain WHERE domain = ?", (domain,)).fetchone():
            return ""
        self.insert("party_domain", {"domain": domain, "party_id": party_id})
        return domain

    def move_domain(self, domain: str, party_id: int) -> int | None:
        """D1: a remembered domain belongs to another party - the row's
        party_id changes (logged), nothing is deleted. The party it was on,
        or None when it was already there."""
        domain = (domain or "").strip().lower()
        row = self.conn.execute("SELECT rowid AS rid, party_id FROM party_domain WHERE domain = ?",
                                (domain,)).fetchone()
        if row is None:
            raise KeyError("no domain {}".format(domain))
        if row["party_id"] == party_id:
            return None
        self.update("party_domain", row["rid"], {"party_id": party_id}, pk="rowid")
        return row["party_id"]

    def add_party(self, key: str, **values) -> int:
        """Create a party, or return the one that already owns the key."""
        key = (key or "").strip()
        existing = self.conn.execute(
            "SELECT id FROM party WHERE lower(trim(key)) = ?", (key.lower(),)
        ).fetchone()
        if existing:
            return existing["id"]
        values.setdefault("full_name", key)
        values.setdefault("type", "Agent")
        values.setdefault("country", "Unknown")
        values.setdefault("source", "manual")
        return self.insert("party", dict(values, key=key))

    def add_location(self, key: str, **values) -> str:
        """location.key is stored upper case and trimmed.

        Once the master layer is active this creates a master_location
        instead (status to_confirm, source intake, type city unless given)
        and returns its key; a string that already names a place, by key
        or alias, returns that place's key and creates nothing.
        """
        if master_active(self.conn):
            text = (key or "").strip()
            if TWO_LETTER_CODE.match(text.upper()) and find_master_location(self.conn, text) is None:
                raise ValueError(
                    "'{}' is a two-letter code - a state or a country (CO, PA, PE, AR, CL "
                    "could be either). Pick the country and leave the location empty, "
                    "or type the place's name.".format(text.upper()))
            return self.add_master_location(
                key, country=values.get("country") or "Unknown",
                type=values.get("type") or "city", note=values.get("notes"),
                status="to_confirm", source="intake")["key"]
        key = (key or "").strip().upper()
        existing = self.conn.execute(
            "SELECT key FROM location WHERE key = ?", (key,)).fetchone()
        if existing:
            return existing["key"]
        values.setdefault("official_name", key)
        values.setdefault("status", "To Confirm")
        values.setdefault("code_system", "Unresolved")
        self.insert("location", dict(values, key=key))
        return key

    # -- master layer -------------------
    def add_master_area(self, key: str, name: str, country_code: str) -> int:
        key = location_key(key)
        row = self.conn.execute("SELECT id FROM master_area WHERE key = ?", (key,)).fetchone()
        if row is not None:
            return row["id"]
        return self.insert("master_area", {"key": key, "name": name or key.title(),
                                           "country_code": country_code or ""})

    def add_master_location(self, text: str, country: str, type: str = "city",
                            name: str | None = None, status: str = "active",
                            source: str = "manual", note: str | None = None,
                            **columns) -> sqlite3.Row:
        """Create a place, or return the one the string already names (by
        key, then alias). The string as written becomes a manual alias when
        it differs from the key it was normalised to."""
        found = find_master_location(self.conn, text)
        if found is not None:
            return found
        # RD1b: the master's naming rules, whatever path creates the place
        rules = place_rules(self.conn, text, country, type)
        if rules["error"]:
            raise ValueError(rules["error"])
        self.place_notes.extend(n for n in rules["notes"] if n not in self.place_notes)
        if rules["found"] is not None:
            return rules["found"]
        if rules["text"] != " ".join((text or "").split()):
            text = rules["text"]
            note = "; ".join([n for n in [note] if n] + rules["notes"])
        key = location_key(text)
        if not key:
            raise ValueError("a location needs a name")
        values = {"key": key, "name": (name or text or "").strip() or key,
                  "type": type if type in ("city", "airport", "seaport", "ramp") else "city",
                  "country": country or "Unknown", "status": status, "source": source, "note": note}
        values.update(columns)
        row_id = self.insert("master_location", values)
        written = (text or "").strip().upper()
        if written and written != key:
            self.add_master_alias(written, row_id, "manual")
        return self.conn.execute("SELECT * FROM master_location WHERE id = ?", (row_id,)).fetchone()

    def add_master_alias(self, alias: str, location_id: int, kind: str = "manual") -> bool:
        """False when the alias already points at that place. Upper case,
        accents kept (that is how a mail writes the place)."""
        alias = " ".join((alias or "").upper().split())
        if not alias:
            return False
        exists = self.conn.execute(
            "SELECT 1 FROM master_location_alias WHERE alias = ? AND location_id = ?",
            (alias, location_id)).fetchone()
        if exists:
            return False
        self.insert("master_location_alias", {"alias": alias, "location_id": location_id, "kind": kind})
        return True

    def remove_master_alias(self, alias: str, location_id: int) -> bool:
        """Take one spelling off a place (the Locations page). An alias is a
        lookup entry the app made, not business data, and its removal is
        logged like any other change."""
        alias = " ".join((alias or "").upper().split())
        row = self.conn.execute(
            "SELECT rowid, kind FROM master_location_alias WHERE alias = ? AND location_id = ?",
            (alias, location_id)).fetchone()
        if row is None:
            return False
        self.conn.execute("DELETE FROM master_location_alias WHERE alias = ? AND location_id = ?",
                          (alias, location_id))
        self._log("master_location_alias", row["rowid"], "(delete)",
                  "{} -> {} ({})".format(alias, location_id, row["kind"]), None)
        return True

    def merge_master_location(self, keep_id: int, drop_id: int, today: str | None = None) -> dict:
        """Two rows for one place: keep one, point everything at it.

        The dropped place's aliases and its own key move to the kept one,
        every case pointing at it moves (the text column follows the id),
        and the dropped row is marked retired with a note - never deleted.
        Every move is a logged field change.
        """
        import datetime as dt

        if keep_id == drop_id:
            raise ValueError("a location cannot be merged into itself")
        keep = self.conn.execute("SELECT * FROM master_location WHERE id = ?", (keep_id,)).fetchone()
        drop = self.conn.execute("SELECT * FROM master_location WHERE id = ?", (drop_id,)).fetchone()
        if keep is None or drop is None:
            raise ValueError("no such location")
        if keep["status"] == "retired" or drop["status"] == "retired":
            raise ValueError("one of them is retired already")
        today = today or dt.date.today().isoformat()
        moved = {"aliases": 0, "enquiry.origin_loc_id": 0, "enquiry.dest_loc_id": 0}
        for row in self.conn.execute(
                "SELECT rowid, alias FROM master_location_alias WHERE location_id = ?",
                (drop_id,)).fetchall():
            exists = self.conn.execute(
                "SELECT 1 FROM master_location_alias WHERE alias = ? AND location_id = ?",
                (row["alias"], keep_id)).fetchone()
            if exists:
                continue                      # the kept place has that spelling already
            self.update("master_location_alias", row["rowid"], {"location_id": keep_id}, pk="rowid")
            moved["aliases"] += 1
        if self.add_master_alias(drop["key"], keep_id, "legacy_key"):
            moved["aliases"] += 1
        for side in ("origin", "dest"):
            column = "{}_loc_id".format(side)
            for case in self.conn.execute(
                    "SELECT id FROM enquiry WHERE {} = ?".format(column), (drop_id,)).fetchall():
                self.update("enquiry", case["id"], {column: keep_id})
                moved["enquiry." + column] += 1
        # batch 23: every route's places follow too (routes after A, each
        # place of a Dest. location, the Port of discharge and its copy)
        self.routes_follow_location(drop_id, keep_id)
        self.update("master_location", drop_id, {
            "status": "retired",
            "note": ((drop["note"] or "").rstrip() + "\nMerged into {} on {}".format(
                keep["key"], today)).strip()})
        self.touch("master_location", drop_id)
        self.update("master_location", keep_id, {
            "note": ((keep["note"] or "").rstrip() + "\nMerged {} into this location on {}".format(
                drop["key"], today)).strip()})
        self.touch("master_location", keep_id)
        return moved

    def add_party_group(self, key: str, name: str | None = None, notes: str | None = None) -> int:
        key = (key or "").strip().upper()
        row = self.conn.execute("SELECT id FROM party_group WHERE key = ?", (key,)).fetchone()
        if row is not None:
            return row["id"]
        return self.insert("party_group", {"key": key, "name": name or key, "notes": notes})


# ------------------------------------------------------------- opp numbering
def next_opp_no(conn: sqlite3.Connection) -> str:
    """Allocate the next case number. Numbers are never reused."""
    n = int(get_setting(conn, "next_opp_number", "1"))
    set_setting(conn, "next_opp_number", n + 1)
    return f"OPP-{n:04d}"


def peek_opp_no(conn: sqlite3.Connection) -> str:
    return f"OPP-{int(get_setting(conn, 'next_opp_number', '1')):04d}"

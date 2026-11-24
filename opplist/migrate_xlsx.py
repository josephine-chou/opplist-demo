"""Migrate the legacy opportunity workbook into the database.

The workbook is opened read only and never written back. The migration is
idempotent: rows are matched on their natural key (opp_no, party key,
location key, country name, and for events the tuple the spec names), so a
re-run updates instead of duplicating.
"""

from __future__ import annotations

import collections
import csv
import datetime as dt
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import openpyxl

from . import db, paths, status as status_mod
from .parsing import parse_qty

# --------------------------------------------------------------- constants --
# Keys the workbook files under 'Agent' that are in
# fact the shipper or consignee themselves.
DIRECT_CUSTOMER_KEYS = {
    "harlow fasteners",
    "brenton engineered solutions",
    "kinetra",
    "genesis",
    "ridgeway parts",
    "pellham group",
}
PLACEHOLDER_UNKNOWN_KEYS = {"(tbc)", "unknown"}
PLACEHOLDER_DIRECT_KEYS = {"colin"}

FREE_MAIL_DOMAINS = {
    "gmail.com",
    "hotmail.com",
    "outlook.com",
    "yahoo.com",
    "qq.com",
    "163.com",
}

_DOMAIN_IN_NOTES = re.compile(
    r"\b([\w-]+\.(?:com|net|org|cl|br|pe|mx|ar|uy|ec|bo|ve|py|cr|gt|hn|sv|ni|pa|do|"
    r"id|ph|in|vn|cn|hk|tw|sg|my|th|kr|jp|pk|lk|ae|sa|jo|za|ro)(?:\.\w{2})?)\b",
    re.IGNORECASE,
)

_CLIENT_TAG = re.compile(r"^client\s*:\s*(.+)$", re.IGNORECASE)

# Container types the workbook used that the app renames.
CONTAINER_RENAME = {"40HC": "40HQ", "20HC": "20GP"}

SHEET_OPPORTUNITIES = "Opportunities"
SHEET_LOCATIONS = "Lookup_Location"
SHEET_AGENTS = "Agent_Master"
SHEET_COUNTRIES = "Country_List"
SHEET_LISTS = "Lists"


# ----------------------------------------------------------------- report --
@dataclass
class MigrationReport:
    countries_inserted: int = 0
    countries_updated: int = 0
    locations_inserted: int = 0
    locations_updated: int = 0
    locations_created_to_confirm: list = field(default_factory=list)
    parties_inserted: int = 0
    parties_updated: int = 0
    parties_created_placeholder: list = field(default_factory=list)
    contacts_inserted: int = 0
    domains_inserted: int = 0
    domain_conflicts: list = field(default_factory=list)
    enquiries_inserted: int = 0
    enquiries_updated: int = 0
    #: (sheet row, opp_no, agent) for the rows that had no Enquiry No.
    enquiries_numbered: list = field(default_factory=list)
    status_events_inserted: int = 0
    thread_keys_set: int = 0
    settings_added: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    next_opp_number: int = 0
    dedupe_pairs: int = 0
    dedupe_report_path: Any = None

    def lines(self) -> list:
        out = [
            "country       inserted {:5d}  updated {:5d}".format(
                self.countries_inserted, self.countries_updated),
            "location      inserted {:5d}  updated {:5d}".format(
                self.locations_inserted, self.locations_updated),
            "party         inserted {:5d}  updated {:5d}".format(
                self.parties_inserted, self.parties_updated),
            "contact       inserted {:5d}".format(self.contacts_inserted),
            "party_domain  inserted {:5d}".format(self.domains_inserted),
            "enquiry       inserted {:5d}  updated {:5d}".format(
                self.enquiries_inserted, self.enquiries_updated),
            "status_event  inserted {:5d}".format(self.status_events_inserted),
            "thread_key set from thread-opp-map: {}".format(self.thread_keys_set),
        ]
        if self.enquiries_numbered:
            out.append("numbered rows that had no Enquiry No.: {}".format(
                len(self.enquiries_numbered)))
            for sheet_row, opp_no, agent in self.enquiries_numbered:
                out.append("   row {:<5} -> {}  {}".format(
                    sheet_row, opp_no, agent))
        out += [
            "next_opp_number: {}".format(self.next_opp_number),
        ]
        if self.dedupe_report_path:
            out.append("probable duplicate pairs: {}  ->  {}".format(
                self.dedupe_pairs, self.dedupe_report_path))
        if self.locations_created_to_confirm:
            out.append("locations created as 'To Confirm': "
                       + ", ".join(self.locations_created_to_confirm))
        if self.parties_created_placeholder:
            out.append("parties created as placeholders: "
                       + ", ".join(self.parties_created_placeholder))
        if self.settings_added:
            out.append("setting values added from Lists: " + ", ".join(self.settings_added))
        for line in self.domain_conflicts:
            out.append("domain conflict: " + line)
        for line in self.notes:
            out.append("note: " + line)
        return out


# ------------------------------------------------------------- cell helpers --
def _text(value: Any):
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip() or None


_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d")


def _date(value: Any):
    """Excel dates arrive as datetime; text dates get a couple of chances."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _number(value: Any):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace(",", ""))
    except ValueError:
        return None


def _int(value: Any):
    n = _number(value)
    return int(n) if n is not None else None


def _yn(value: Any) -> int:
    return 1 if str(value or "").strip().upper() == "Y" else 0


def _rows(ws, key_col: int = 0) -> Iterable:
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row[key_col] not in (None, ""):
            yield row


#: Iris half-registers a case in the sheet - agent, date, lane, no number
#: yet. Those rows are real enquiries, so the migration numbers them and says
#: so on the row.
NUMBERED_NOTE = "numbered at migration 2026-09-09"


def _numbered_marker(sheet_row: int) -> str:
    """The note that also says which row it was, so a re-run finds it again."""
    return "{} (Opportunities row {})".format(NUMBERED_NOTE, sheet_row)


def _opportunity_rows(ws) -> Iterable:
    """(sheet row number, row) for every row with anything in it.

    Unlike _rows this keeps the ones with no Enquiry No.: they are cases too,
    and the row number is what makes numbering them repeatable.
    """
    for number, row in enumerate(ws.iter_rows(min_row=2, values_only=True),
                                 start=2):
        if any(value not in (None, "") for value in row):
            yield number, row


# ------------------------------------------------------------- upsert utils --
def _upsert(repo, table, where_sql, where_args, values, pk="id"):
    """Insert or update by natural key.

    Returns (row_id, inserted, changed). `changed` is the number of fields the
    update actually touched, so a re-run of an unchanged workbook reports
    zeroes rather than pretending it rewrote everything.
    """
    row = repo.conn.execute(
        "SELECT * FROM {} WHERE {}".format(table, where_sql), where_args
    ).fetchone()
    if row is None:
        return repo.insert(table, values), True, 0
    return row[pk], False, repo.update(table, row[pk], values, pk=pk)


# ------------------------------------------------------------------ stages --
def _migrate_countries(repo, ws, report) -> None:
    for row in _rows(ws):
        name = _text(row[0])
        values = {
            "iso2": _text(row[1]),
            "iso3": _text(row[2]),
            "region": _text(row[3]) or "Undefined",
        }
        existing = repo.conn.execute(
            "SELECT name FROM country WHERE name = ?", (name,)
        ).fetchone()
        if existing is None:
            repo.insert("country", dict({"name": name}, **values))
            report.countries_inserted += 1
        elif repo.update("country", name, values, pk="name"):
            report.countries_updated += 1


def _known_countries(conn) -> set:
    return set(r["name"] for r in conn.execute("SELECT name FROM country"))


def _safe_country(value, known, label, report):
    """A country the Country_List sheet does not carry becomes 'Unknown'.

    The foreign key would otherwise reject the row, and dropping a whole
    agent or location because of one spelling is worse than flagging it.
    """
    if not value:
        return None
    if value in known:
        return value
    report.notes.append("{}: country {!r} is not in Country_List, "
                        "stored as 'Unknown'".format(label, value))
    return "Unknown"


def _migrate_locations(repo, ws, report) -> None:
    known = _known_countries(repo.conn)
    for row in _rows(ws):
        key = str(row[0]).strip().upper()
        values = {
            "official_name": _text(row[1]),
            "type": _text(row[2]),
            "country": _safe_country(_text(row[3]), known,
                                     "location " + key, report),
            "status": _text(row[5]) or "OK",
            "notes": _text(row[6]),
            "code_system": _text(row[7]),
            "code_official": _text(row[8]),
            "parent_city": _text(row[9]),
            "is_seaport": _yn(row[11]),
            "is_airport": _yn(row[12]),
        }
        existing = repo.conn.execute(
            "SELECT key FROM location WHERE key = ?", (key,)
        ).fetchone()
        if existing is None:
            repo.insert("location", dict({"key": key}, **values))
            report.locations_inserted += 1
        elif repo.update("location", key, values, pk="key"):
            report.locations_updated += 1


def _migrate_lists(conn, ws, report) -> None:
    """Add values the workbook offers that schema.sql did not pre-fill. Never remove."""
    columns = {
        "container_types": 5,   # F  Container Type
        "special_handling": 9,  # J  Special Handling
        "closed_reasons": 3,    # D  Loss Reason
        "latam_countries": 8,   # I  LATAM Countries
    }
    sheet = list(ws.iter_rows(min_row=2, values_only=True))
    for setting_key, col in columns.items():
        current = db.get_setting_list(conn, setting_key)
        lowered = set(str(v).strip().lower() for v in current)
        added = []
        for row in sheet:
            value = _text(row[col]) if col < len(row) else None
            if value and value.lower() not in lowered:
                current.append(value)
                lowered.add(value.lower())
                added.append(value)
        if added:
            db.set_setting(conn, setting_key, current)
            report.settings_added.extend(
                "{}: {}".format(setting_key, v) for v in added)


def _split_semicolons(value: Any) -> list:
    if not value:
        return []
    return [part.strip() for part in str(value).split(";") if part.strip()]


def _party_type(key: str, source_type):
    """(type, placeholder) from the Agent key and the Source Type cell."""
    low = key.strip().lower()
    if low in PLACEHOLDER_UNKNOWN_KEYS:
        return "Unknown", 1
    if low in PLACEHOLDER_DIRECT_KEYS:
        return "Direct Customer", 1
    if low in DIRECT_CUSTOMER_KEYS:
        return "Direct Customer", 0
    if (source_type or "").strip().lower() == "office":
        return "Office", 0
    return "Agent", 0


def _add_domain(repo, domain, party_id, party_key, report) -> None:
    domain = domain.strip().lower().lstrip("@")
    if not domain or domain in FREE_MAIL_DOMAINS:
        return
    row = repo.conn.execute(
        "SELECT party_id FROM party_domain WHERE domain = ?", (domain,)
    ).fetchone()
    if row is None:
        if not repo.attach_domain(party_id, domain):
            return                            # our own domain (13g)
        report.domains_inserted += 1
    elif row["party_id"] != party_id:
        owner = repo.conn.execute(
            "SELECT key FROM party WHERE id = ?", (row["party_id"],)
        ).fetchone()
        report.domain_conflicts.append(
            "{} already belongs to '{}', not '{}'".format(
                domain, owner["key"], party_key))


def _migrate_parties(repo, ws, report) -> None:
    known = _known_countries(repo.conn)
    for row in _rows(ws):
        key = str(row[0]).strip()
        ptype, placeholder = _party_type(key, _text(row[2]))
        values = {
            "full_name": _text(row[1]) or key,
            "type": ptype,
            "country": _safe_country(_text(row[3]), known,
                                     "party " + key, report) or "Unknown",
            "city": _text(row[5]),
            "mfn_no": _text(row[6]),
            "mfn_member": 1 if str(row[21] or "").strip().upper() == "Y" else None,
            "placeholder": placeholder,
            "notes": _text(row[19]),
            "source": "xlsx",
        }
        party_id, inserted, changed = _upsert(
            repo, "party", "lower(trim(key)) = ?", (key.lower(),),
            dict({"key": key}, **values))
        if inserted:
            report.parties_inserted += 1
        elif changed:
            report.parties_updated += 1

        names = _split_semicolons(row[7])
        emails = _split_semicolons(row[8])
        if names and emails and len(names) == len(emails):
            pairs = list(zip(names, emails))
        else:
            pairs = [(None, e) for e in emails] + [(n, None) for n in names]

        for index, (name, email) in enumerate(pairs):
            existing = repo.conn.execute(
                "SELECT id FROM contact WHERE party_id = ? "
                "AND COALESCE(lower(email), '') = COALESCE(lower(?), '') "
                "AND COALESCE(name, '') = COALESCE(?, '')",
                (party_id, email, name),
            ).fetchone()
            if existing is None:
                repo.insert("contact", {
                    "party_id": party_id,
                    "name": name,
                    "email": email,
                    "is_primary": 1 if index == 0 else 0,
                })
                report.contacts_inserted += 1
            if email and "@" in email:
                _add_domain(repo, email.split("@", 1)[1], party_id, key, report)

        notes = _text(row[19])
        if notes:
            for match in _DOMAIN_IN_NOTES.finditer(notes):
                _add_domain(repo, match.group(1), party_id, key, report)


# ------------------------------------------------------------- enquiries ----
def _special_handling(raw, allowed):
    """Split the Special Handling cell into (tags, end_customer, leftovers)."""
    if not raw:
        return None, None, []
    raw = raw.strip()
    client = _CLIENT_TAG.match(raw)
    if client:
        return None, client.group(1).strip(), []

    lookup = dict((value.lower(), value) for value in allowed)
    tags = []
    leftovers = []
    for token in re.split(r"[/,]", raw):
        token = token.strip()
        if not token:
            continue
        canonical = lookup.get(token.lower())
        if canonical:
            if canonical not in tags:
                tags.append(canonical)
        else:
            leftovers.append(token)
    if leftovers:
        # A value that is not a tag at all ('4 TEUs/month') stays whole.
        return (";".join(tags) or None), None, [raw]
    return (";".join(tags) or None), None, []


def _append(existing, addition):
    return "{}\n{}".format(existing, addition) if existing else addition


@dataclass
class _Event:
    status: str
    at: str
    approx: int
    note: Any


def _build_events(final, received, quoted, closed, quote_ref, today) -> list:
    """The event carrying the final status comes last.

    When Date Received is blank the New event falls back to the earliest date
    the row does know about, never later than today: a case that was quoted in
    August cannot have arrived in September.
    """
    fallback = min(d for d in (today, quoted, closed) if d)
    events = [_Event("New", received or fallback, 0 if received else 1, None)]

    quote_note = "quote ref: {}".format(quote_ref) if quote_ref else None
    if quoted:
        events.append(_Event("Quoted", quoted, 0, quote_note))
    elif final.status == "Quoted":
        events.append(_Event("Quoted", received or fallback, 1, quote_note))

    latest = max(e.at for e in events)
    if final.status in ("Won", "Lost", "Dropped"):
        at = closed or quoted or received or fallback
        events.append(_Event(final.status, max(at, latest),
                             0 if closed else 1, final.event_note))
    elif events[-1].status != final.status:
        # 'Sent to Vendor' rows that also carry a Date Quoted, and the like:
        # the workbook's own status stays the current one.
        at = max(received, latest) if received else latest
        events.append(_Event(final.status, at, 1,
                             "status per workbook (migrated)"))
    return events


def _migrate_enquiries(repo, ws, report, today) -> None:
    conn = repo.conn
    allowed_tags = db.get_setting_list(conn, "special_handling")
    closed_reasons = set(v.lower() for v in db.get_setting_list(conn, "closed_reasons"))

    party_by_key = dict(
        (r["k"], r["id"])
        for r in conn.execute("SELECT lower(trim(key)) AS k, id FROM party"))
    known_locations = set(r["key"] for r in conn.execute("SELECT key FROM location"))
    location_country = dict(
        (r["key"], r["country"])
        for r in conn.execute("SELECT key, country FROM location"))
    known_countries = set(r["name"] for r in conn.execute("SELECT name FROM country"))

    def resolve_party(name):
        key = (name or "").strip() or "Unknown"
        pid = party_by_key.get(key.lower())
        if pid is not None:
            return pid
        pid = repo.insert("party", {
            "key": key,
            "full_name": key,
            "type": "Unknown",
            "country": "Unknown",
            "placeholder": 1,
            "source": "xlsx",
            "notes": "Created during migration: no Agent_Master row.",
        })
        party_by_key[key.lower()] = pid
        report.parties_inserted += 1
        report.parties_created_placeholder.append(key)
        return pid

    def resolve_location(value):
        if value in (None, ""):
            return None
        key = str(value).strip().upper()
        if not key:
            return None
        if key not in known_locations:
            repo.insert("location", {
                "key": key,
                "official_name": key,
                "country": "Unknown",
                "status": "To Confirm",
                "code_system": "Unresolved",
                "notes": "Created during migration: not in Lookup_Location.",
            })
            known_locations.add(key)
            location_country[key] = "Unknown"
            report.locations_inserted += 1
            report.locations_created_to_confirm.append(key)
        return key

    # Numbers for the half-registered rows carry on from the highest number
    # the workbook already uses, and are never reused.
    highest = 0
    for _, row in _opportunity_rows(ws):
        if row[0] not in (None, ""):
            digits = str(row[0]).strip()[4:]
            if digits.isdigit():
                highest = max(highest, int(digits))
    row_db = conn.execute(
        "SELECT MAX(CAST(substr(opp_no, 5) AS INTEGER)) AS n FROM enquiry"
    ).fetchone()
    next_number = max(highest, row_db["n"] or 0) + 1

    for sheet_row, row in _opportunity_rows(ws):
        opp_no = str(row[0]).strip() if row[0] not in (None, "") else ""
        numbered_here = ""
        if not opp_no:
            # A re-run must find the row it numbered last time rather than
            # spend a second number on it.
            marker = _numbered_marker(sheet_row)
            found = conn.execute(
                "SELECT opp_no FROM enquiry WHERE system_notes LIKE ?",
                ("%" + marker + "%",)).fetchone()
            if found is not None:
                opp_no = found["opp_no"]
            else:
                opp_no = "OPP-{:04d}".format(next_number)
                next_number += 1
            numbered_here = marker
        legacy = status_mod.from_legacy(_text(row[27]))

        received = _date(row[1])
        quoted = _date(row[28])
        closed = _date(row[30])
        system_notes = _text(row[38])
        remarks = _text(row[37])

        if row[1] not in (None, "") and received is None:
            system_notes = _append(system_notes,
                                   "unparsed Date Received: {!r}".format(row[1]))

        tags, end_customer, leftovers = _special_handling(_text(row[26]), allowed_tags)
        for leftover in leftovers:
            remarks = _append(remarks,
                              "Special handling (as recorded): {}".format(leftover))

        container_type = _text(row[9])
        container_type = CONTAINER_RENAME.get(container_type, container_type)
        container_qty = _int(row[10])
        air_weight = _number(row[12])
        lcl_cbm = _number(row[13])
        qty_raw = _text(row[36])

        if qty_raw and not any((container_type, container_qty, air_weight, lcl_cbm)):
            parsed = parse_qty(qty_raw)
            if not parsed.is_empty():
                container_type = parsed.container_type
                container_qty = parsed.container_qty
                air_weight = parsed.air_weight_kg
                lcl_cbm = parsed.lcl_cbm
                system_notes = _append(system_notes, "qty parsed from raw (migration)")

        # A blank Loss Reason stays blank. Writing 'Other' would turn 'nobody
        # recorded this' into 'someone chose Other'; the consistency check and
        # the app both surface the blanks instead.
        closed_reason = _text(row[32])
        if legacy.closed_reason:
            closed_reason = legacy.closed_reason
        if closed_reason and closed_reason.lower() not in closed_reasons:
            system_notes = _append(
                system_notes, "closed reason as recorded: {}".format(closed_reason))
            closed_reason = "Other"

        origin_loc = resolve_location(row[15])
        dest_loc = resolve_location(row[19])
        origin_country = _text(row[14])
        dest_country = _text(row[18])
        if not origin_country and origin_loc:
            origin_country = location_country.get(origin_loc)
        if not dest_country and dest_loc:
            dest_country = location_country.get(dest_loc)
        if origin_country and origin_country not in known_countries:
            system_notes = _append(
                system_notes, "unknown origin country: {}".format(origin_country))
            origin_country = "Unknown"
        if dest_country and dest_country not in known_countries:
            system_notes = _append(
                system_notes, "unknown dest country: {}".format(dest_country))
            dest_country = "Unknown"

        if numbered_here:
            system_notes = _append(system_notes, numbered_here)

        quote_ref = _text(row[29])
        events = _build_events(legacy, received, quoted, closed, quote_ref, today)
        if quote_ref and not any(e.status == "Quoted" for e in events):
            system_notes = _append(system_notes, "quote ref: {}".format(quote_ref))

        values = {
            "received_on": received,
            "requester_party_id": resolve_party(_text(row[4])),
            "end_customer": end_customer,
            "owner": _text(row[7]),
            "service_mode": _text(row[8]),
            "container_type": container_type,
            "container_qty": container_qty,
            "air_weight_kg": air_weight,
            "lcl_cbm": lcl_cbm,
            "qty_raw": qty_raw,
            "origin_country": origin_country,
            "origin_loc": origin_loc,
            "origin_alt": _text(row[16]),
            "dest_country": dest_country,
            "dest_loc": dest_loc,
            "dest_alt": _text(row[20]),
            "incoterm": _text(row[24]),
            "commodity": _text(row[25]),
            "special_handling": tags,
            "status": legacy.status,
            "status_confirmed": 1 if legacy.confirmed else 0,
            "closed_reason": (closed_reason
                              if legacy.status in ("Lost", "Dropped") else None),
            "legacy": 1 if legacy.legacy else 0,
            "remarks": remarks,
            "system_notes": system_notes,
            "created_by": "migration",
        }

        enquiry_id, inserted, changed = _upsert(
            repo, "enquiry", "opp_no = ?", (opp_no,),
            dict({"opp_no": opp_no}, **values))
        if inserted:
            report.enquiries_inserted += 1
        elif changed:
            report.enquiries_updated += 1
        if numbered_here and inserted:
            report.enquiries_numbered.append(
                (sheet_row, opp_no, _text(row[4]) or "?"))

        for event in events:
            existing = conn.execute(
                "SELECT id FROM status_event WHERE enquiry_id = ? AND at = ? "
                "AND status = ? AND source = 'migration'",
                (enquiry_id, event.at, event.status),
            ).fetchone()
            if existing is None:
                repo.insert("status_event", {
                    "enquiry_id": enquiry_id,
                    "at": event.at,
                    "status": event.status,
                    "note": event.note,
                    "source": "migration",
                    "approx": event.approx,
                })
                report.status_events_inserted += 1


def _apply_thread_map(repo, map_path, report) -> None:
    """Only unambiguous cases: one ThreadKey for the case, and nothing set yet."""
    map_path = Path(map_path)
    if not map_path.exists():
        report.notes.append("thread map not found: {}".format(map_path))
        return
    with map_path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    by_opp = collections.defaultdict(set)
    for row in rows:
        opp = (row.get("OppNo") or "").strip()
        key = (row.get("ThreadKey") or "").strip()
        if opp and key:
            by_opp[opp].add(key)

    for opp, keys in by_opp.items():
        if len(keys) != 1:
            continue
        row = repo.conn.execute(
            "SELECT id, thread_key FROM enquiry WHERE opp_no = ?", (opp,)
        ).fetchone()
        if row is None or row["thread_key"]:
            continue
        repo.update("enquiry", row["id"], {"thread_key": next(iter(keys))})
        report.thread_keys_set += 1


def _finish(repo, report) -> None:
    conn = repo.conn
    # stage: a party that has ever sent an enquiry is active, the rest dormant
    for row in conn.execute("SELECT id FROM party").fetchall():
        has = conn.execute(
            "SELECT 1 FROM enquiry WHERE requester_party_id = ? LIMIT 1", (row["id"],)
        ).fetchone()
        repo.update("party", row["id"], {"stage": "active" if has else "dormant"})

    # opp numbering continues from the highest number in the workbook
    row = conn.execute(
        "SELECT MAX(CAST(substr(opp_no, 5) AS INTEGER)) AS n FROM enquiry"
    ).fetchone()
    highest = row["n"] or 0
    db.set_setting(conn, "next_opp_number", highest + 1)
    report.next_opp_number = highest + 1


# -------------------------------------------------------------------- entry --
def write_dedupe_report(conn, path) -> int:
    """List the probable pairs, group nothing.

    The migration never merges cases by itself. Iris decides in the app,
    where 'Same request, add as lane' is one click.
    """
    from . import dedupe

    rows = dedupe.report_rows(conn)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = ["OppNoA", "OppNoB", "Score", "Band", "RequesterA", "RequesterB",
               "LaneA", "LaneB", "Reasons"]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, lineterminator="\r\n",
                                quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def migrate(conn: sqlite3.Connection, workbook=None, thread_map=None,
            today=None, dedupe_report=None) -> MigrationReport:
    workbook = Path(workbook or paths.SOURCE_WORKBOOK)
    thread_map = Path(thread_map or paths.THREAD_OPP_MAP)
    if dedupe_report is None:
        dedupe_report = paths.EXPORT_PREVIEW_DIR / "migration-dedupe-report.csv"
    today = today or dt.date.today().isoformat()

    report = MigrationReport()
    repo = db.Repository(conn, source="migration")

    wb = openpyxl.load_workbook(workbook, read_only=True, data_only=True)
    try:
        _migrate_countries(repo, wb[SHEET_COUNTRIES], report)
        _migrate_locations(repo, wb[SHEET_LOCATIONS], report)
        if SHEET_LISTS in wb.sheetnames:
            _migrate_lists(conn, wb[SHEET_LISTS], report)
        _migrate_parties(repo, wb[SHEET_AGENTS], report)
        _migrate_enquiries(repo, wb[SHEET_OPPORTUNITIES], report, today)
    finally:
        wb.close()

    _apply_thread_map(repo, thread_map, report)
    _finish(repo, report)
    repo.commit()

    if dedupe_report is not False:
        report.dedupe_pairs = write_dedupe_report(conn, dedupe_report)
        report.dedupe_report_path = str(dedupe_report)
    return report

"""Place pickers and the places of a route (batch 23, schema v39).

One picker for Origin location, Dest. location and Port of discharge on the
case page and the register page. A route's Dest. location and its Port of
discharge take up to four places each, shown with 'or': any of them will do,
so it is one route and one quote. The places live in enquiry_route_place;
the repository keeps enquiry_route.dest_loc / dest_loc_id as the first
destination and enquiry_route.pod as a readable copy of the ports, so every
reader that used them keeps working.

A picker posts one field: the keys it holds, joined with '|' (a single
key, or the text a route had before it named a place). A place added in its
small form rides along in '<field>_new' (JSON) and is created, To confirm,
when the route is saved; '+ Mark as a port' rides in '<field>_mark' (JSON)
and sets the flag on the master place when the route is saved.
"""

from __future__ import annotations

import json
import sqlite3

from . import db

#: Places a Dest. location or a Port of discharge holds at most (the
#: repository's own limit).
MAX_PLACES = db.MAX_ROUTE_PLACES
#: Between the keys of one picker's field.
SEP = "|"


# ------------------------------------------------------------- the keys --
def split(text) -> list:
    """'SAN ANTONIO|VALPARAISO' -> ['SAN ANTONIO', 'VALPARAISO']: blanks
    dropped, a repeat (any case) kept once, the order kept."""
    out, seen = [], set()
    for part in str(text or "").split(SEP):
        part = " ".join(part.split())
        if part and part.casefold() not in seen:
            seen.add(part.casefold())
            out.append(part)
    return out


def join(keys) -> str:
    return SEP.join(k for k in (keys or []) if k)


def is_port(row) -> bool:
    return bool(_get(row, "is_seaport") or _get(row, "type") == "seaport")


def is_airport(row) -> bool:
    return bool(_get(row, "is_airport") or _get(row, "type") == "airport")


def kind(row) -> str:
    """What a place is, from its flags: 'port', 'airport', 'port, airport',
    else 'city' ('rail ramp' for a ramp)."""
    port, air = is_port(row), is_airport(row)
    if port and air:
        return "port, airport"
    if port:
        return "port"
    if air:
        return "airport"
    return "rail ramp" if _get(row, "type") == "ramp" else "city"


def _get(row, key):
    if row is None:
        return None
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


# ---------------------------------------------------------- resolution --
def resolve_one(conn: sqlite3.Connection, text, retired: bool = False):
    """The master place `text` names: its key, else an alias of exactly one
    live place; with `retired`, a retired place's own key as the last
    resort (a route still pointing at one keeps it). None otherwise."""
    if not db.master_active(conn):
        return None
    key = db.location_key(text)
    if not key:
        return None
    row = conn.execute("SELECT * FROM master_location WHERE key = ? AND status <> 'retired'", (key,)).fetchone()
    if row is not None:
        return row
    written = " ".join(str(text or "").upper().split())
    found = conn.execute(
        "SELECT DISTINCT l.* FROM master_location_alias a JOIN master_location l ON l.id = a.location_id "
        "WHERE a.alias IN (?, ?) AND l.status <> 'retired' ORDER BY l.id", (key, written)).fetchall()
    if len(found) == 1:
        return found[0]
    if retired and not found:
        return conn.execute("SELECT * FROM master_location WHERE key = ?", (key,)).fetchone()
    return None


def pick(row, legacy_text=None) -> dict:
    """One chip of a picker: a master place, or the text a route holds that
    names no place (legacy)."""
    if row is None:
        return {"key": legacy_text or "", "name": "", "kind": "", "country": "", "status": "",
                "port": 0, "airport": 0, "legacy": True}
    return {"key": row["key"], "name": _get(row, "name") or "", "kind": kind(row),
            "country": _get(row, "country") or "", "status": _get(row, "status") or "",
            "port": 1 if is_port(row) else 0, "airport": 1 if is_airport(row) else 0,
            "legacy": False}


def picks_from_text(conn, text) -> list:
    """The chips of a field's value ('SAN ANTONIO|VALPARAISO')."""
    out = []
    for key in split(text):
        row = resolve_one(conn, key, retired=True)
        out.append(pick(row, key))
    return out


def picks_from_rows(rows, legacy_text=None) -> list:
    if rows:
        return [pick(r) for r in rows]
    return [pick(None, legacy_text)] if (legacy_text or "").strip() else []


# ------------------------------------------------------- the picker list --
def picker_data(conn) -> dict:
    """{'places': [...], 'countries': [...]} for the pickers of a page: every
    live place with what the search matches (key, name, aliases, UN/LOCODE,
    IATA) and what a row says (its flags, its country, To confirm). On a
    database still on the old location table: its keys."""
    countries = [r["name"] for r in conn.execute(
        "SELECT name FROM country ORDER BY (name = 'Unknown') DESC, name")]
    places = []
    if db.master_active(conn):
        for r in conn.execute(
                "SELECT l.id, l.key, l.name, l.type, l.country, l.subdivision, l.unlocode, l.iata, "
                "l.is_seaport, l.is_airport, l.status, "
                "(SELECT group_concat(alias, '|') FROM master_location_alias x WHERE x.location_id = l.id) AS aliases "
                "FROM master_location l WHERE l.status <> 'retired' ORDER BY l.key"):
            places.append({
                "id": r["id"], "key": r["key"], "name": r["name"] or "", "type": r["type"] or "",
                "country": r["country"] or "", "unlocode": r["unlocode"] or "", "iata": r["iata"] or "",
                "port": 1 if is_port(r) else 0, "airport": 1 if is_airport(r) else 0,
                "status": r["status"] or "active",
                "aliases": [a for a in (r["aliases"] or "").split("|") if a]})
    else:
        types = {"Seaport": "seaport", "Airport": "airport"}
        for r in conn.execute("SELECT key, official_name, type, country, status, code_official, "
                              "is_seaport, is_airport, aliases FROM location ORDER BY key"):
            places.append({
                "id": None, "key": r["key"], "name": r["official_name"] or "", "type": types.get(r["type"], "city"),
                "country": r["country"] or "", "unlocode": r["code_official"] or "", "iata": "",
                "port": 1 if (r["is_seaport"] or r["type"] == "Seaport") else 0,
                "airport": 1 if (r["is_airport"] or r["type"] == "Airport") else 0,
                "status": "to_confirm" if r["status"] == "To Confirm" else "active",
                "aliases": [a.strip().upper() for a in (r["aliases"] or "").split(",") if a.strip()]})
    return {"places": places, "countries": countries, "max": MAX_PLACES if db.master_active(conn) else 1}


# ------------------------------------------------------ reading a form --
def _json_list(text) -> list:
    try:
        value = json.loads(text) if (text or "").strip() else []
    except ValueError:
        return []
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def parse(form, name: str, label: str, errors: dict, max_places: int = 1, conn=None, country=None) -> dict:
    """What a picker posted under `name` - the keys in order, the places
    typed in its Add form, the places to mark - checked but not written:
    {'keys', 'new', 'mark', 'notes'}. A refused value lands in
    `errors[name]`. RD1b: with `conn`, a place typed in the Add form obeys
    the master's naming rules (db.place_rules) under its country, else
    `country` (the side's): a mode word taken off and a state's comma put
    in, with a note; a country name or a US city without its state
    refused; one that turns out to be on file is picked, not added."""
    keys = split(form.get(name))
    notes = []
    picked = {db.location_key(k) for k in keys}
    new = []
    for entry in _json_list(form.get(name + "_new")):
        written = " ".join(str(entry.get("key") or "").split()).upper()
        key = db.location_key(written)
        if not key or key not in picked:              # added, then taken off the box again
            continue
        if db.TWO_LETTER_CODE.match(key):
            errors[name] = ("'{}' is a two-letter code - a state or a country. Pick the country and leave "
                            "the location empty, or type the place's name.".format(key))
            continue
        if len(key) > 80:
            errors[name] = "A location's key is 80 characters at most."
            continue
        if conn is not None and db.master_active(conn):
            own = str(entry.get("country") or "").strip()
            rules = db.place_rules(conn, written, own if own not in ("", "Unknown") else country)
            if rules["error"]:
                errors[name] = "; ".join(rules["notes"] + [rules["error"]])
                continue
            notes.extend(rules["notes"])
            if rules["text"] != written:
                new_key = db.location_key(rules["text"])
                keys = [new_key if db.location_key(k) == key else k for k in keys]
                picked.add(new_key)
                if rules["found"] is not None:
                    continue                          # on file: picked, not added
                written, key = rules["text"].upper(), new_key
        typed_name = " ".join(str(entry.get("name") or "").split())[:80]
        if typed_name.upper() == " ".join(str(entry.get("key") or "").split()).upper() != written:
            typed_name = ""                           # the name was the text the rules changed
        new.append({"key": key, "written": written[:80], "name": typed_name,
                    "country": str(entry.get("country") or "").strip(),
                    "port": 1 if entry.get("port") else 0, "airport": 1 if entry.get("airport") else 0})
    mark = []
    for entry in _json_list(form.get(name + "_mark")):
        key = " ".join(str(entry.get("key") or "").split())
        if key and db.location_key(key) in picked:
            mark.append({"key": key, "kind": "airport" if entry.get("kind") == "airport" else "port"})
    if len(keys) > max_places:
        errors[name] = "{} takes {} place{} at most".format(label, max_places, "" if max_places == 1 else "s")
    return {"keys": keys, "new": new, "mark": mark, "notes": notes}


def apply(conn, repo, parsed: dict, country=None, create_unknown: bool = False) -> dict:
    """Write what a picker asked for and read its places: the new places
    (status To confirm, source intake, like a place read from a mail), the
    flags it marks, then each key resolved to its master place. A key that
    names no place is created To confirm with `create_unknown` (the
    register page, as a lane from the mail always was); otherwise, alone,
    it is the text the route already held. {'ids', 'keys', 'rows', 'text',
    'unknown'}."""
    known_countries = {r[0] for r in conn.execute("SELECT name FROM country")}
    for entry in parsed["new"]:
        # the country chosen in the small form; left Unknown (or not a country
        # on file: the form's select cannot post one, so a garbled post), the
        # route's own for that side
        if entry["country"] not in known_countries or entry["country"] in ("", "Unknown"):
            entry = dict(entry, country=country if country in known_countries else "Unknown")
        if db.master_active(conn):
            # the key as she typed it (accents and all) is kept as an alias
            # when it differs from the master key, as for a place from a mail
            flags = {"is_seaport": entry["port"], "is_airport": entry["airport"]}
            repo.add_master_location(entry.get("written") or entry["key"], entry["country"] or country or "Unknown",
                                     "city", name=entry["name"] or None, status="to_confirm", source="intake",
                                     note="Added from the inquiry form.", **flags)
        else:
            repo.add_location(entry.get("written") or entry["key"], country=entry["country"] or country or "Unknown",
                              notes="Added from the inquiry form.")
    for entry in parsed["mark"]:
        mark(repo, entry["key"], entry["kind"])
    rows, unknown = [], []
    kept = {str(k).upper() for k in (parsed.get("kept") or ())}
    for key in parsed["keys"]:
        # a retired place is never offered, so the case page takes one only
        # when the route already holds it; the register page keeps a mail's
        # lane as it always did (its key is taken, so nothing is created)
        row = resolve_one(conn, key, retired=create_unknown or key.upper() in kept)
        if row is None and create_unknown:
            try:
                key = repo.add_location(key, country=country or "Unknown",
                                        notes="Added when registering a case from a mail subject.") or key
            except ValueError:                    # a two-letter code: refused before it gets here
                pass
            row = resolve_one(conn, key)
        if row is None:
            unknown.append(key)
        else:
            rows.append(row)
    out = {"ids": [], "keys": [], "rows": rows, "text": None, "unknown": unknown}
    if unknown:
        if len(parsed["keys"]) == 1:
            out["text"] = unknown[0]                  # the text it held, kept as it is
        return out
    ids = []
    for row in rows:
        if row["id"] not in ids:
            ids.append(row["id"])
    out["ids"] = ids
    out["keys"] = [r["key"] for r in rows]
    return out


# ------------------------------------------ RG2a (layer 2): the mail's words --
def _route_a_place(conn, enquiry_id, side):
    """The live master place route A holds on that side, or None."""
    row = conn.execute("SELECT {0}_loc_id AS lid, {0}_loc AS loc FROM enquiry_route WHERE enquiry_id = ? AND voided = 0 "
                       "ORDER BY seq LIMIT 1".format(side), (enquiry_id,)).fetchone()
    if row is None:
        row = conn.execute("SELECT {0}_loc_id AS lid, {0}_loc AS loc FROM enquiry WHERE id = ?".format(side),
                           (enquiry_id,)).fetchone()
    if row is None:
        return None
    if row["lid"]:
        return conn.execute("SELECT * FROM master_location WHERE id = ? AND status <> 'retired'",
                            (row["lid"],)).fetchone()
    return resolve_one(conn, row["loc"]) if row["loc"] else None


def remember_offers(conn, enquiry_id) -> list:
    """RG2a (layer 2): [{'side', 'words', 'key', 'location_id', 'read'}] - a
    side of a case registered from a mail where route A now holds another
    place than the one the reader offered for the mail's words. Not offered
    when the words already name that place (its key, its name or an alias)."""
    if not db.master_active(conn) or not db._has_table(conn, "register_read"):
        return []
    out = []
    for r in conn.execute("SELECT field, value_read, words FROM register_read WHERE enquiry_id = ? "
                          "AND field IN ('origin_loc', 'dest_loc') AND words IS NOT NULL AND words <> '' "
                          "ORDER BY field DESC", (enquiry_id,)):
        side = r["field"][:-4]
        place = _route_a_place(conn, enquiry_id, side)
        if place is None or db.location_key(place["key"]) == db.location_key(r["value_read"] or ""):
            continue
        words = " ".join(r["words"].upper().split())
        if words in (place["key"], db.location_key(place["name"] or "")):
            continue
        if conn.execute("SELECT 1 FROM master_location_alias WHERE alias IN (?, ?) AND location_id = ?",
                        (words, db.location_key(words), place["id"])).fetchone():
            continue
        out.append({"side": side, "words": words, "key": place["key"], "location_id": place["id"],
                    "read": r["value_read"]})
    return out


def remember(repo, offer) -> bool:
    """Write the offer's words as a manual alias (upper case) of its place.
    ValueError, saying why, when the words already name another live place
    (its key or an alias) - one spelling never points at two places."""
    conn = repo.conn
    words = " ".join(str(offer["words"]).upper().split())
    if not words:
        raise ValueError("There are no words to remember.")
    other = conn.execute("SELECT key FROM master_location WHERE key = ? AND id <> ? AND status <> 'retired'",
                         (db.location_key(words), offer["location_id"])).fetchone()
    if other is None:
        other = conn.execute(
            "SELECT l.key FROM master_location_alias a JOIN master_location l ON l.id = a.location_id "
            "WHERE a.alias IN (?, ?) AND a.location_id <> ? AND l.status <> 'retired' ORDER BY l.id LIMIT 1",
            (words, db.location_key(words), offer["location_id"])).fetchone()
    if other is not None:
        raise ValueError("'{}' already names {}: it cannot name {} as well. Change it on the Locations page "
                         "if {} is wrong.".format(words, other["key"], offer["key"], other["key"]))
    return repo.add_master_alias(words, offer["location_id"], "manual")


def mark(repo, key, which: str) -> bool:
    """'+ Mark as a port' (or as an airport): the flag set on the master
    place, through the repository. False when nothing changed."""
    row = resolve_one(repo.conn, key)
    if row is None:
        return False
    flag = "is_airport" if which == "airport" else "is_seaport"
    if row[flag]:
        return False
    repo.update("master_location", row["id"], {flag: 1})
    repo.touch("master_location", row["id"])
    return True

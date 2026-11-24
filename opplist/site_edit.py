"""The team site changes a case's Cargo and Route (batch D7b,
INTERFACE.md section 8): OppList's half.

Push: cases/{OPP}.ed (the editable values of each live cargo line and
route) on the cases the site may change, lists/places, meta/sync.editLists.
Pull: the action 'edit' - change, add or strike a cargo line or a route.
A changed field is written when OppList's value is still the one the
person saw (`from`); when she changed it in OppList in between, it goes
into one pending suggestion of kind 'edit' and hers stays until she
decides. Everything is written through the repository, so change_log has
it (source web, one action id per site action), and the Timeline line of
an applied action is read from there - never a status_event note.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from . import cargo, db, routes as routes_mod

EDITABLE = ("New", "Quoted")
TEXT_MAX = 200
FIELDS_MAX = 30
#: ed's cargo line fields -> enquiry_cargo columns
CARGO = {"pieces": "pieces", "pack": "pack_unit", "l": "dim_l", "w": "dim_w", "h": "dim_h", "du": "dims_unit",
         "wt": "weight_value", "wu": "weight_unit", "wb": "weight_basis", "cbm": "cbm_value", "cb": "cbm_basis",
         "ct": "container_type", "cq": "container_qty", "com": "commodity", "hs": "hs_code",
         "val": "cargo_value", "cur": "cargo_value_cur"}
#: ed's route fields (the places apart) -> enquiry_route columns
ROUTE = {"oc": "origin_country", "dc": "dest_country", "mode": "service_mode", "inc": "incoterm",
         "scope": "service_scope"}
PLACES = ("o", "d", "pod")
#: the words a result, a suggestion and the Timeline use for each field
LABELS = {"pieces": "Pieces", "pack": "Packing", "l": "Length", "w": "Width", "h": "Height", "du": "Size unit",
          "wt": "Weight", "wu": "Weight unit", "wb": "Weight basis", "cbm": "Volume", "cb": "Volume basis",
          "ct": "Container type", "cq": "Containers", "com": "Commodity", "hs": "HS code", "val": "Cargo value",
          "cur": "Currency", "oc": "Origin country", "dc": "Destination country", "o": "Origin",
          "d": "Destination", "pod": "Port of discharge", "mode": "Mode", "inc": "Incoterm", "scope": "Service"}
#: a change_log column of a line or route -> the ed field it shows as
_COLUMN_FIELD = {v: k for k, v in CARGO.items()}
_COLUMN_FIELD.update({v: k for k, v in ROUTE.items()})
_COLUMN_FIELD.update({"origin_loc": "o", "dest_loc": "d", "pod": "pod"})
_MARK = re.compile(r"\[site:([^\]]+)\]")


class Refused(Exception):
    """The action does not fit; the message is for the person."""


# --------------------------------------------------------------- which --
def editable(row) -> bool:
    """A case the site may change: New or Quoted, not another office's,
    not legacy (INTERFACE.md section 8)."""
    return (row["status"] in EDITABLE and not (row["handled_by"] or "").strip()
            and not int(row["legacy"] or 0))


# ----------------------------------------------------------------- push --
def _num(value):
    if value is None:
        return None
    value = float(value)
    return int(value) if value == int(value) else value


def _place_of(conn, location_id, text):
    if location_id:
        row = conn.execute("SELECT key FROM master_location WHERE id = ?", (location_id,)).fetchone()
        if row is not None:
            return {"k": row["key"]}
    return {"t": text} if (text or "").strip() else None


def line_ed(line, n) -> dict:
    out = {"id": line["id"], "n": n}
    for field, column in CARGO.items():
        value = line.get(column) if hasattr(line, "get") else line[column]
        if column in ("pieces", "container_qty"):
            value = int(value) if value is not None else None
        elif column in ("dim_l", "dim_w", "dim_h", "weight_value", "cbm_value", "cargo_value"):
            value = _num(value)
        else:
            value = (value or None) if value != 0 else value
        out[field] = value
    return out


def route_ed(conn, route) -> dict:
    out = {"id": route["id"], "n": routes_mod.letter(route["seq"]),
           "oc": route["origin_country"] or None, "dc": route["dest_country"] or None,
           "o": _place_of(conn, route["origin_loc_id"], route["origin_loc"]),
           "mode": route["service_mode"] or None, "inc": route["incoterm"] or None,
           "scope": route["service_scope"] or None}
    dests = [{"k": r[0]} for r in conn.execute(
        "SELECT m.key FROM enquiry_route_place p JOIN master_location m ON m.id = p.location_id "
        "WHERE p.route_id = ? AND p.side = 'dest' AND p.voided = 0 ORDER BY p.seq", (route["id"],))]
    if not dests:
        first = _place_of(conn, route["dest_loc_id"], route["dest_loc"])
        dests = [first] if first else []
    pods = [{"k": r[0]} for r in conn.execute(
        "SELECT m.key FROM enquiry_route_place p JOIN master_location m ON m.id = p.location_id "
        "WHERE p.route_id = ? AND p.side = 'pod' AND p.voided = 0 ORDER BY p.seq", (route["id"],))]
    if not pods and (route["pod"] or "").strip():
        pods = [{"t": route["pod"]}]
    out["d"], out["pod"] = dests, pods
    return out


def live_lines(conn, case_id) -> list:
    return cargo.lines_of(conn, case_id)


def live_routes(conn, case_id) -> list:
    return conn.execute("SELECT * FROM enquiry_route WHERE enquiry_id = ? AND voided = 0 ORDER BY seq",
                        (case_id,)).fetchall()


def ed_of(conn, row):
    """cases/{OPP}.ed, or None when the site may not change the case."""
    if not editable(row):
        return None
    return {"cargo": [line_ed(l, n) for n, l in enumerate(live_lines(conn, row["id"]), start=1)],
            "routes": [route_ed(conn, r) for r in live_routes(conn, row["id"])]}


def places_doc(conn) -> dict:
    """lists/places: the master locations that are active or To Confirm,
    and every country OppList knows. No case data."""
    aliases = {}
    for r in conn.execute("SELECT location_id, alias FROM master_location_alias ORDER BY alias"):
        aliases.setdefault(r["location_id"], []).append(r["alias"])
    # D7c: maps, not arrays - Firestore takes no array inside an array
    rows = [{"k": r["key"], "name": r["name"] or r["key"], "country": r["country"] or "", "type": r["type"] or "city",
             "code": r["unlocode"] or r["iata"] or "", "aliases": ", ".join(aliases.get(r["id"], []))}
            for r in conn.execute("SELECT * FROM master_location WHERE status IN ('active', 'to_confirm') ORDER BY key")]
    countries = [{"name": r["name"], "iso2": r["iso2"] or ""}
                 for r in conn.execute("SELECT name, iso2 FROM country ORDER BY name")]
    return {"rows": rows, "countries": countries}


def edit_lists(conn) -> dict:
    """meta/sync.editLists: the lists the forms offer."""
    from . import packing
    from .web import forms
    return {"packing": packing.units_of(conn), "containers": db.get_setting_list(conn, "container_types") or [],
            "scopes": db.get_setting_list(conn, "service_scopes") or list(routes_mod.DEFAULT_SCOPES),
            "modes": list(forms.SERVICE_MODES), "incoterms": list(forms.INCOTERMS)}


# ------------------------------------------------- the official lists --
_OFFICIAL = {}


def reset_official() -> None:
    """Each pull reads the two files once, when an action needs them."""
    _OFFICIAL.clear()


def official_codes(conn) -> tuple:
    """(UN/LOCODE codes 'BRSAO', IATA codes) in the files Settings names;
    empty sets when a file is not there."""
    if "codes" not in _OFFICIAL:
        from . import coordinates
        locodes, iatas = set(), set()
        unlocode = (db.get_setting(conn, "official_unlocode_path", "") or "").strip()
        airports = (db.get_setting(conn, "official_airports_path", "") or "").strip()
        try:
            if unlocode and Path(unlocode).exists():
                locodes = {r["code"] for r in coordinates.read_unlocode(unlocode)
                           if r.get("change", "").upper() != "X" and r.get("status", "").upper() != "XX"}
        except (OSError, FileNotFoundError, ValueError):
            locodes = set()
        try:
            if airports and Path(airports).exists():
                iatas = {r["iata"] for r in coordinates.read_airports(airports) if r.get("iata")}
        except (OSError, ValueError):
            iatas = set()
        _OFFICIAL["codes"] = (locodes, iatas)
    return _OFFICIAL["codes"]


# --------------------------------------------------------- the values --
def _text(value, what):
    if value is None:
        return None
    if not isinstance(value, str):
        raise Refused("{} is not text".format(what))
    value = " ".join(value.split())
    if len(value) > TEXT_MAX:
        raise Refused("{} is longer than {} characters".format(what, TEXT_MAX))
    return value or None


def _number(value, what, whole=False, low=0):
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Refused("{} is not a number".format(what))
    if value < low:
        raise Refused("{} is below {}".format(what, low))
    if whole and value != int(value):
        raise Refused("{} is not a whole number".format(what))
    return int(value) if whole else float(value)


def _one_of(value, allowed, what):
    value = _text(value, what)
    if value is not None and value not in allowed:
        raise Refused("{} '{}' is not one OppList offers".format(what, value))
    return value


def _check_place(value, what):
    if value is None:
        return None
    if not isinstance(value, dict) or not (isinstance(value.get("k"), str) or isinstance(value.get("t"), str)):
        raise Refused("{} is not a place".format(what))
    out = {}
    if value.get("k"):
        out["k"] = _text(value["k"], what)
    if value.get("t"):
        out["t"] = _text(value["t"], what)
    off = value.get("off")
    if off is not None:
        if not isinstance(off, dict):
            raise Refused("{}: the official pick is not readable".format(what))
        out["off"] = {
            "sys": _one_of(off.get("sys"), ("UN/LOCODE", "IATA"), what + " system"),
            "cc": (_text(off.get("cc"), what + " country code") or "").upper(),
            "code": (_text(off.get("code"), what + " code") or "").upper(),
            "name": _text(off.get("name"), what + " name"), "sub": _text(off.get("sub"), what + " subdivision"),
            "kind": _one_of(off.get("kind"), ("seaport", "airport", "city"), what + " kind") or "city",
            "lat": _number(off.get("lat"), what + " latitude", low=-90),
            "lon": _number(off.get("lon"), what + " longitude", low=-180)}
        if not re.fullmatch(r"[A-Z]{2}", out["off"]["cc"]) or not re.fullmatch(r"[A-Z0-9]{3}", out["off"]["code"]):
            raise Refused("{}: the official code is not readable".format(what))
    if not out:
        raise Refused("{} is empty".format(what))
    return out


def check_value(conn, part, field, value):
    """The value in ed's format, checked; raises Refused."""
    what = LABELS.get(field, field)
    if part == "cargo":
        if field in ("pieces", "cq"):
            return _number(value, what, whole=True, low=1 if field == "cq" else 0)
        if field in ("l", "w", "h", "wt", "cbm", "val"):
            return _number(value, what)
        if field == "du":
            return _one_of(value, tuple(cargo.DIMS_UNITS), what)
        if field == "wu":
            return _one_of(value, tuple(cargo.WEIGHT_UNITS), what)
        if field == "wb":
            return _one_of(value, cargo.WEIGHT_BASES, what)
        if field == "cb":
            return _one_of(value, ("total", "each"), what)
        if field == "pack":
            from . import packing
            return _one_of(value, tuple(packing.units_of(conn)), what)
        if field == "ct":
            return _one_of(value, tuple(db.get_setting_list(conn, "container_types") or ()), what)
        if field == "hs":
            text = _text(value, what)
            codes, why = cargo.hs_code_of(text)
            if why:
                raise Refused("HS code: " + why)
            return codes or None
        if field == "cur":
            text = _text(value, what)
            if text is not None and not re.fullmatch(r"[A-Za-z]{3}", text):
                raise Refused("the currency is not three letters")
            return text.upper() if text else None
        if field == "com":
            return _text(value, what)
    else:
        if field in ("oc", "dc"):
            names = {r[0] for r in conn.execute("SELECT name FROM country")}
            return _one_of(value, names, what)
        if field == "mode":
            from .web import forms
            return _one_of(value, tuple(forms.SERVICE_MODES), what)
        if field == "inc":
            from .web import forms
            return _one_of(value, tuple(forms.INCOTERMS), what)
        if field == "scope":
            lists = db.get_setting_list(conn, "service_scopes") or routes_mod.DEFAULT_SCOPES
            return _one_of(value, tuple(lists), what)
        if field == "o":
            return _check_place(value, what)
        if field in ("d", "pod"):
            if value is None:
                value = []
            if not isinstance(value, list) or len(value) > db.MAX_ROUTE_PLACES:
                raise Refused("{} takes at most {} places".format(what, db.MAX_ROUTE_PLACES))
            return [_check_place(v, what) for v in value]
    raise Refused("OppList does not know the {} field '{}'".format(part, field))


def _same(a, b) -> bool:
    """ed values compared as the person saw them: numbers as numbers,
    text without case or spacing, places by key or text."""
    if isinstance(a, list) or isinstance(b, list):
        a, b = a or [], b or []
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) or isinstance(b, dict):
        return _place_ref(a) == _place_ref(b)
    if a in (None, "") or b in (None, ""):
        return a in (None, "") and b in (None, "")
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return abs(float(a) - float(b)) < 1e-9
    return " ".join(str(a).split()).casefold() == " ".join(str(b).split()).casefold()


def _off_code(off) -> str:
    return (off.get("cc") or "") + (off.get("code") or "") if off.get("sys") == "UN/LOCODE" else (off.get("code") or "")


def _place_ref(p) -> str:
    """D7c: a place as compared - an official pick by its system, country
    and code ('off:UN/LOCODE:BRSSZ', never the same as a {k} or a {t}, so
    typing the old To Confirm words and picking the official place is a
    change), else its key, else its words."""
    if not p:
        return ""
    if p.get("off"):
        return "off:{}:{}".format(p["off"].get("sys") or "", _off_code(p["off"]).upper())
    if p.get("k"):
        return "k:" + p["k"].casefold()
    return "t:" + " ".join(str(p.get("t") or "").split()).casefold()


def _pick_on_file(conn, now, new) -> bool:
    """D7c: an official pick that is the master location the route has
    already (its key carries that code): writing it would change nothing."""
    def one(a, b):
        if not (isinstance(b, dict) and b.get("off")):
            return _same(a, b)
        if not (isinstance(a, dict) and a.get("k")):
            return False
        column = "unlocode" if b["off"].get("sys") == "UN/LOCODE" else "iata"
        row = conn.execute("SELECT {} AS code FROM master_location WHERE key = ?".format(column), (a["k"],)).fetchone()
        return row is not None and (row["code"] or "").upper() == _off_code(b["off"]).upper()
    if isinstance(new, list) or isinstance(now, list):
        now, new = now or [], new or []
        return len(now) == len(new) and all(one(a, b) for a, b in zip(now, new))
    return one(now, new)


def show(field, value) -> str:
    """A value as a sentence writes it: '7200', 'CHICAGO, IL', '-'."""
    if value in (None, "", []):
        return "-"
    if isinstance(value, list):
        return " / ".join(show(field, v) for v in value)
    if isinstance(value, dict):
        return value.get("k") or value.get("t") or "-"
    if isinstance(value, float):
        return "{:g}".format(value)
    return str(value)


# --------------------------------------------------------- the places --
def _country_of(conn, cc):
    row = conn.execute("SELECT name FROM country WHERE iso2 = ?", (cc,)).fetchone()
    return row["name"] if row else None


def place_id(repo, value, country, who, day) -> int | None:
    """The master location a place names, made when it is new (point 4)."""
    if value is None:
        return None
    conn = repo.conn
    if value.get("off"):
        off = value["off"]
        code = off["cc"] + off["code"] if off["sys"] == "UN/LOCODE" else off["code"]
        column = "unlocode" if off["sys"] == "UN/LOCODE" else "iata"
        found = conn.execute("SELECT * FROM master_location WHERE {} = ? AND status <> 'retired' ORDER BY id LIMIT 1"
                             .format(column), (code,)).fetchone()
        if found is None:
            locodes, iatas = official_codes(conn)
            listed = code in (locodes if column == "unlocode" else iatas)
            note = "From the team site: picked by {} on {} from the official list ({} {}){}".format(
                who, day, off["sys"], code, "" if listed else "; the code is not in the files Settings names")
            country_name = _country_of(conn, off["cc"]) or country or "Unknown"
            columns = {column: code, "country_code": off["cc"], "subdivision": off.get("sub"),
                       "lat": off.get("lat"), "lon": off.get("lon"),
                       "is_seaport": 1 if off["kind"] == "seaport" else 0,
                       "is_airport": 1 if off["kind"] == "airport" else 0}
            try:
                found = repo.add_master_location(off.get("name") or value.get("t") or off["code"], country_name,
                                                 type=off["kind"], name=off.get("name"),
                                                 status="active" if listed else "to_confirm", source="manual",
                                                 note=note, **{k: v for k, v in columns.items() if v is not None})
            except ValueError as exc:
                raise Refused("the place {} cannot be added: {}".format(off.get("name") or code, exc)) from None
        typed = " ".join(str(value.get("t") or "").split())
        if typed and typed.casefold() not in (found["key"].casefold(), (found["name"] or "").casefold()):
            repo.add_master_alias(typed, found["id"], "manual")
        return found["id"]
    if value.get("k"):
        found = conn.execute("SELECT id FROM master_location WHERE key = ?", (value["k"],)).fetchone() \
            or db.find_master_location(conn, value["k"])
        if found is None:
            raise Refused("OppList does not know the place {}".format(value["k"]))
        return found["id"]
    try:
        key = repo.add_location(value["t"], country=country or "Unknown",
                                notes="Typed on the team site by {} on {}".format(who, day))
    except ValueError as exc:
        raise Refused(str(exc)) from None
    return db.find_master_location(conn, key)["id"]


# -------------------------------------------------------- the writes --
def _line_values(line, changes) -> dict:
    """The line's columns with the changes, as the Cargo card posts a whole
    line (so a weight keeps its unit)."""
    out = {column: line.get(column) for column in cargo.FIELDS if column in line}
    for field, value in changes.items():
        out[CARGO[field]] = value
    return out


def write_cargo(repo, line_id, changes) -> int:
    line = dict(repo.conn.execute("SELECT * FROM enquiry_cargo WHERE id = ?", (line_id,)).fetchone())
    return repo.edit_cargo_line(line_id, _line_values(line, changes))


def _route_values(repo, route, changes, who, day) -> dict:
    values = {}
    for field, value in changes.items():
        if field in ROUTE:
            values[ROUTE[field]] = value
    oc = changes.get("oc", route["origin_country"] if route is not None else None)
    dc = changes.get("dc", route["dest_country"] if route is not None else None)
    if "o" in changes:
        pid = place_id(repo, changes["o"], oc, who, day)
        values["origin_loc_id"] = pid
        values["origin_loc"] = repo.conn.execute("SELECT key FROM master_location WHERE id = ?",
                                                 (pid,)).fetchone()["key"] if pid else None
    if "d" in changes:
        values["dest_places"] = [place_id(repo, p, dc, who, day) for p in changes["d"]]
        if not values["dest_places"]:
            values["dest_loc_id"], values["dest_loc"] = None, None
    if "pod" in changes:
        values["pod_places"] = [place_id(repo, p, dc, who, day) for p in changes["pod"]]
        if not values["pod_places"]:
            values["pod"] = None
    return values


def write_route(repo, route_id, changes, who, day) -> int:
    route = repo.conn.execute("SELECT * FROM enquiry_route WHERE id = ?", (route_id,)).fetchone()
    return repo.edit_route(route_id, _route_values(repo, route, changes, who, day))


# ---------------------------------------------------------- the action --
def _fields_word(fields) -> str:
    words = []
    for f in fields:
        word = LABELS.get(f, f).lower()
        if word not in words:
            words.append(word)
    return ", ".join(words)


def _thing(part, row, conn) -> str:
    if part == "route":
        return "route {}".format(routes_mod.letter(row["seq"]))
    return "cargo line {}".format(_line_number(conn, row))


def _line_number(conn, line) -> int:
    ids = [l["id"] for l in live_lines(conn, line["enquiry_id"])]
    return ids.index(line["id"]) + 1 if line["id"] in ids else int(line["seq"])


def take(repo, case, action, who, today, action_id) -> str:
    """The action 'edit' (section 8 points 1 to 6). Raises Refused."""
    conn = repo.conn
    opp = case["opp_no"]
    if not editable(case):
        why = ("it is {}".format(case["status"]) if case["status"] not in EDITABLE else
               "it is handled by {}".format(case["handled_by"]) if (case["handled_by"] or "").strip() else
               "it is a legacy row")
        raise Refused("{} can no longer be changed on the team site ({}), so nothing was changed.".format(opp, why))
    part = str(action.get("part") or "")
    op = str(action.get("op") or "")
    if part not in ("cargo", "route") or op not in ("change", "add", "strike"):
        raise Refused("{}: '{} {}' is not a change OppList knows, so nothing was changed.".format(opp, part, op))
    day = today
    fields = CARGO if part == "cargo" else dict(ROUTE, **{p: p for p in PLACES})
    to = action.get("to") if op != "strike" else {}
    if op != "strike":
        if not isinstance(to, dict) or not to:
            raise Refused("{}: the change carries no values, so nothing was changed.".format(opp))
        if len(to) > FIELDS_MAX:
            raise Refused("{}: more than {} fields in one change, so nothing was changed.".format(opp, FIELDS_MAX))
        unknown = [f for f in to if f not in fields]
        if unknown:
            raise Refused("{}: OppList does not know the {} field '{}', so nothing was changed.".format(
                opp, part, unknown[0]))
    try:
        to = {f: check_value(conn, part, f, v) for f, v in (to or {}).items()}
    except Refused as exc:
        raise Refused("{}: {}, so nothing was changed.".format(opp, exc)) from None
    table = "enquiry_cargo" if part == "cargo" else "enquiry_route"
    if op == "add":
        if part == "cargo":
            values = _line_values({}, to)
            values["source"] = "manual"
            if cargo.is_blank({k: ("" if v is None else str(v)) for k, v in values.items()}):
                raise Refused("{}: the new cargo line has no pieces, size, weight or volume, so nothing was added."
                              .format(opp))
            line_id = repo.add_cargo_line(case["id"], values)
            line = conn.execute("SELECT * FROM enquiry_cargo WHERE id = ?", (line_id,)).fetchone()
            return "{}: {} added.".format(opp, _thing(part, line, conn))
        if not to.get("dc") and not to.get("d"):
            raise Refused("{}: the new route has no destination, so nothing was added.".format(opp))
        route_id = repo.add_route(case["id"], _route_values(repo, None, to, who, day))
        route = conn.execute("SELECT * FROM enquiry_route WHERE id = ?", (route_id,)).fetchone()
        return "{}: {} added.".format(opp, _thing(part, route, conn))
    try:
        target_id = int(action.get("id"))
    except (TypeError, ValueError):
        raise Refused("{}: the {} to change is not named, so nothing was changed.".format(opp, part)) from None
    row = conn.execute("SELECT * FROM {} WHERE id = ? AND enquiry_id = ?".format(table),
                       (target_id, case["id"])).fetchone()
    if row is None:
        raise Refused("{}: that {} is not on the case, so nothing was changed.".format(opp, part))
    if row["voided"]:
        raise Refused("{}: that {} was struck out in OppList, so nothing was changed.".format(
            opp, "cargo line" if part == "cargo" else "route"))
    thing = _thing(part, row, conn)
    if op == "strike":
        live = live_lines(conn, case["id"]) if part == "cargo" else live_routes(conn, case["id"])
        if len(live) <= 1:
            raise Refused("{}: {} is the case's last {}, so it was not struck out.".format(
                opp, thing, "cargo line" if part == "cargo" else "route"))
        if part == "cargo":
            repo.void_cargo_line(target_id)
        else:
            repo.void_route(target_id)
        return "{}: {} struck out.".format(opp, thing)
    seen = action.get("from")
    if not isinstance(seen, dict) or set(seen) != set(to):
        raise Refused("{}: the change does not say what was seen before, so nothing was changed.".format(opp))
    try:
        seen = {f: check_value(conn, part, f, v) for f, v in seen.items()}
    except Refused as exc:
        raise Refused("{}: {}, so nothing was changed.".format(opp, exc)) from None
    now = line_ed(dict(row), 0) if part == "cargo" else route_ed(conn, row)
    apply = {f: v for f, v in to.items() if _same(now.get(f), seen[f])}
    conflicts = [f for f in to if f not in apply]
    changed = [f for f in apply if not (_pick_on_file(conn, now.get(f), apply[f]) if f in PLACES
                                        else _same(now.get(f), apply[f]))]
    if changed:
        if part == "cargo":
            write_cargo(repo, target_id, {f: apply[f] for f in changed})
        else:
            write_route(repo, target_id, {f: apply[f] for f in changed}, who, day)
    out = []
    if changed:
        out.append("{}: {} changed ({}).".format(opp, thing, _fields_word(changed)))
    if conflicts:
        _suggest(repo, case, part, row, conflicts, now, to, who, day, action_id)
        n = len(conflicts)
        out.append("{}: {} change{} {} waiting for Iris (she changed it in OppList).".format(
            opp, n, "" if n == 1 else "s", "is" if n == 1 else "are"))
    if not out:
        out.append("{}: {} already had those values; nothing to change.".format(opp, thing))
    return " ".join(out)


# --------------------------------------------------- the suggestion --
def mark(action_id) -> str:
    return "[site:{}]".format(action_id)


def action_of(row) -> str:
    found = _MARK.search(row["note"] or "") if row["kind"] == "edit" else None
    return found.group(1) if found else ""


def _suggest(repo, case, part, row, conflicts, now, to, who, day, action_id) -> None:
    lines = ["{}: {} in OppList, {} from {}".format(LABELS[f], show(f, now.get(f)), show(f, to[f]), who)
             for f in conflicts]
    repo.insert("suggestion", {
        "enquiry_id": case["id"], "opp_no": case["opp_no"], "kind": "edit", "state": "pending", "source": "web",
        "route_id": row["id"] if part == "route" else None,
        "proposed_status": ",".join(conflicts), "proposed_date": day, "evidence_sender": who, "evidence_date": day,
        "evidence_quote": "; ".join(lines)[:400], "confidence": "high",
        "note": "Changed on the team site by {} ({}) {}".format(who, _thing(part, row, repo.conn), mark(action_id))})


def _site_action(conn, action_id):
    if not action_id or not db._has_table(conn, "site_action"):
        return None
    return conn.execute("SELECT * FROM site_action WHERE id = ?", (action_id,)).fetchone()


def _fields(action_row) -> dict:
    try:
        return json.loads(action_row["fields"] or "{}") if action_row is not None else {}
    except ValueError:
        return {}


def _target(conn, s, fields):
    table = "enquiry_cargo" if fields.get("part") == "cargo" else "enquiry_route"
    try:
        return table, conn.execute("SELECT * FROM {} WHERE id = ?".format(table), (int(fields.get("id")),)).fetchone()
    except (TypeError, ValueError):
        return table, None


def conflict_lines(conn, s) -> list:
    """The card's lines: 'Weight: 7000 kg in OppList, 7200 kg from Ava',
    OppList's value as it is now."""
    fields = _fields(_site_action(conn, action_of(s)))
    part = fields.get("part")
    _table, row = _target(conn, s, fields)
    if row is None:
        return [s["evidence_quote"] or ""]
    now = line_ed(dict(row), 0) if part == "cargo" else route_ed(conn, row)
    to = fields.get("to") or {}
    who = s["evidence_sender"] or "the team site"
    out = []
    for f in (s["proposed_status"] or "").split(","):
        if f in to:
            out.append("{}: {} in OppList, {} from {}".format(LABELS.get(f, f), _unit_show(part, f, now.get(f), now),
                                                         _unit_show(part, f, to[f], dict(now, **to)), who))
    return out


def _unit_show(part, field, value, line) -> str:
    """'7000 kg', '3.2 cbm', 'USD 12,500' where a unit goes with the number."""
    text = show(field, value)
    if part == "cargo" and text != "-":
        if field == "wt":
            return "{} {}".format(text, line.get("wu") or "kg")
        if field == "cbm":
            return "{} cbm".format(text)
        if field == "val":
            return cargo.value_text(value, line.get("cur") or "USD")
    return text


def accept(repo, s) -> str:
    """Her Accept: the site's values of the waiting fields, written now
    through the repository (her action, so change_log shows hers)."""
    conn = repo.conn
    action = _site_action(conn, action_of(s))
    fields = _fields(action)
    to = fields.get("to") or {}
    part = fields.get("part")
    _table, row = _target(conn, s, fields)
    if row is None or row["voided"]:
        raise ValueError("{}: that {} is gone or struck out; nothing to accept".format(
            s["opp_no"], "cargo line" if part == "cargo" else "route"))
    wanted = [f for f in (s["proposed_status"] or "").split(",") if f in to]
    try:
        values = {f: check_value(conn, part, f, to[f]) for f in wanted}
        who = s["evidence_sender"] or "the team site"
        if part == "cargo":
            write_cargo(repo, row["id"], values)
        else:
            write_route(repo, row["id"], values, who, (s["evidence_date"] or "")[:10])
    except Refused as exc:
        raise ValueError(str(exc)) from None
    from . import suggestions
    suggestions._record(repo, s["id"], None, {"fields": wanted, "action": repo.action_id}, False, "")
    repo.commit()
    return "{}: the team site's {} written ({}).".format(s["opp_no"], _fields_word(wanted), _thing(part, row, conn))


# -------------------------------------------------------- the answer --
def suggestion_of(conn, action_id):
    rows = conn.execute("SELECT * FROM suggestion WHERE kind = 'edit' AND source = 'web' AND instr(note, ?) > 0 "
                        "ORDER BY id", (mark(action_id),)).fetchall()
    return rows[-1] if rows else None


def answer_of(conn, action_row) -> tuple:
    """(ok, msg) for an applied edit action: as taken in while its
    suggestion waits; ok false 'Iris kept OppList's <field> on OPP-nnnn'
    when she dismissed it."""
    s = suggestion_of(conn, action_row["id"])
    if s is None or s["state"] == "pending":
        return True, action_row["message"] or ""
    words = _fields_word((s["proposed_status"] or "").split(","))
    if s["state"] == "dismissed":
        return False, "Iris kept OppList's {} on {}.".format(words, s["opp_no"])
    return True, "Iris accepted the team site's {} on {}.".format(words, s["opp_no"])


def answers(repo) -> int:
    """Each edit action whose suggestion she decided since its answer was
    written: the new message, and actionResults/{id} written again."""
    conn = repo.conn
    if not db._has_table(conn, "site_action"):
        return 0
    changed = 0
    for a in conn.execute("SELECT * FROM site_action WHERE type = 'edit' AND result = 'applied'").fetchall():
        s = suggestion_of(conn, a["id"])
        if s is None or s["state"] == "pending":
            continue
        msg = answer_of(conn, a)[1]
        if msg != (a["message"] or ""):
            repo.update("site_action", a["id"], {"message": msg, "answered_at": None})
            changed += 1
    if changed:
        repo.commit()
    return changed


def answer_ok(conn, action_row) -> bool:
    s = suggestion_of(conn, action_row["id"])
    return s is None or s["state"] != "dismissed"


# ------------------------------------------------------- the Timeline --
def _logged(conn, action_id, table, row_id) -> list:
    if not action_id:
        return []
    return conn.execute("SELECT field, old_value, new_value FROM change_log WHERE action_id = ? AND table_name = ? "
                        "AND row_id = ? ORDER BY id", (action_id, table, row_id)).fetchall()


def _change_text(conn, part, row, logged, order=()) -> str:
    """'Pieces 10 → 12; Weight 7000 kg → 7200 kg' from change_log rows, in
    `order` (ed's field order, the card's)."""
    seen, out = {}, []
    for r in logged:
        if r["field"] in ("(insert)", "voided", "updated_at"):
            continue
        field = _COLUMN_FIELD.get(r["field"])
        if field is None:
            continue
        seen[field] = (r["old_value"], r["new_value"])
    units = {"wt": seen.get("wu"), "cbm": None}
    rank = {f: i for i, f in enumerate(order)}
    for field, (old, new) in sorted(seen.items(), key=lambda kv: rank.get(kv[0], len(rank))):
        if field == "wu" and "wt" in seen:
            continue
        old_t, new_t = old if old not in (None, "") else "-", new if new not in (None, "") else "-"
        if part == "cargo" and field == "wt":
            unit_old = (units["wt"][0] if units["wt"] else None) or row["weight_unit"] or "kg"
            unit_new = (units["wt"][1] if units["wt"] else None) or row["weight_unit"] or "kg"
            old_t = "{} {}".format(_trim(old), unit_old) if old not in (None, "") else "-"
            new_t = "{} {}".format(_trim(new), unit_new) if new not in (None, "") else "-"
        elif part == "cargo" and field == "cbm":
            old_t = "{} cbm".format(_trim(old)) if old not in (None, "") else "-"
            new_t = "{} cbm".format(_trim(new)) if new not in (None, "") else "-"
        else:
            old_t, new_t = _trim(old_t), _trim(new_t)
        out.append("{} {} → {}".format(LABELS[field], old_t, new_t))
    return "; ".join(out)


def _trim(value) -> str:
    try:
        number = float(value)
        return "{:g}".format(number)
    except (TypeError, ValueError):
        return str(value)


def _line_text(conn, action_row, fields, accepted_by=""):
    part, op = fields.get("part"), fields.get("op")
    table = "enquiry_cargo" if part == "cargo" else "enquiry_route"
    action_id = action_row["change_action"]
    if op == "add":
        found = conn.execute("SELECT row_id FROM change_log WHERE action_id = ? AND table_name = ? "
                             "AND field = '(insert)' ORDER BY id LIMIT 1", (action_id, table)).fetchone()
        row = conn.execute("SELECT * FROM {} WHERE id = ?".format(table), (found[0],)).fetchone() if found else None
        what = ("Cargo {}".format(row["seq"]) if part == "cargo" else "Route {}".format(
            routes_mod.letter(row["seq"]))) if row is not None else part.capitalize()
        return "{} added".format(what)
    try:
        row = conn.execute("SELECT * FROM {} WHERE id = ?".format(table), (int(fields.get("id")),)).fetchone()
    except (TypeError, ValueError):
        row = None
    if row is None:
        return ""
    # the Timeline keeps a line's own number (seq), whatever was struck out since
    what = "Cargo {}".format(row["seq"]) if part == "cargo" else "Route {}".format(routes_mod.letter(row["seq"]))
    if op == "strike":
        return "{} struck out".format(what)
    order = list(CARGO) if part == "cargo" else ["oc", "o", "dc", "d", "pod", "mode", "inc", "scope"]
    text = _change_text(conn, part, row, _logged(conn, action_id, table, row["id"]), order)
    return "{}: {}".format(what, text) if text else ""


def timeline(conn, enquiry_id) -> list:
    """[{at, by, text, accepted}] newest first: each applied edit action of
    the case, and each edit suggestion she accepted, read from change_log."""
    if not db._has_table(conn, "site_action"):
        return []
    out = []
    for a in conn.execute("SELECT * FROM site_action WHERE type = 'edit' AND result = 'applied' AND enquiry_id = ? "
                          "AND change_action IS NOT NULL", (enquiry_id,)).fetchall():
        fields = _fields(a)
        text = _line_text(conn, a, fields)
        at = conn.execute("SELECT MIN(at) FROM change_log WHERE action_id = ?", (a["change_action"],)).fetchone()[0]
        if text and at:
            out.append({"at": at[:10], "stamp": at, "by": _who(a), "text": text, "accepted": ""})
    for s in conn.execute("SELECT * FROM suggestion WHERE kind = 'edit' AND state = 'accepted' AND enquiry_id = ?",
                          (enquiry_id,)).fetchall():
        try:
            decided = json.loads(s["decided_values"] or "{}")
        except ValueError:
            decided = {}
        a = _site_action(conn, action_of(s))
        if a is None or not decided.get("action"):
            continue
        fields = _fields(a)
        text = _line_text(conn, {"change_action": decided["action"]}, fields)
        at = conn.execute("SELECT MIN(at) FROM change_log WHERE action_id = ?", (decided["action"],)).fetchone()[0]
        if text and at:
            out.append({"at": at[:10], "stamp": at, "by": _who(a), "text": text, "accepted": "Iris"})
    out.sort(key=lambda r: r["stamp"], reverse=True)
    return out


def _who(action_row) -> str:
    from .firebase_pull import who_of
    return who_of({"name": action_row["by_name"], "owner": action_row["by_owner"], "by": action_row["by_email"]})


def headline(item, on_site=False) -> str:
    """'Changed on the team site by Ava' (', accepted by Iris') on
    8765; Q2c: 'Changed by Ava' on the site, where people read it."""
    return "Changed {}by {}{}".format("" if on_site else "on the team site ", item["by"],
                                      ", accepted by {}".format(item["accepted"]) if item["accepted"] else "")

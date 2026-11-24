"""Routes (schema v33, 2026-09-28): one mail is one OPP, with two
lists inside it - routes (from where, to where, which part we do) and cargo
(what is shipped). This module holds the rules that read them; every write
goes through db.Repository (add_route, edit_route, void_route,
record_results, sync_totals).

Route A is the first live route by seq. The enquiry's own lane fields, mode
and incoterm are a copy of it (Repository.sync_route_a), so the views, the
exports and older readers keep working.

Totals: a counted live cargo line counts once for every live route it
applies to (none recorded = all) - one shipment per route. Gross and volume
are sums; each route is charged in its own mode: Air by the chargeable
weight (the larger of gross and volume x 166.67), LCL by its volume, FCL by
its containers per shipment. A case with no counted line keeps the gross and
volume typed on it.
"""

from __future__ import annotations

import sqlite3

from . import cargo

LANE_FIELDS = ("origin_country", "origin_loc", "origin_loc_id", "origin_alt",
               "dest_country", "dest_loc", "dest_loc_id", "dest_alt", "service_mode", "incoterm")
ROUTE_FIELDS = LANE_FIELDS + ("service_scope", "pod", "result", "result_on", "lost_reason", "notes")
RESULTS = ("Won", "Lost", "Cancelled")
#: The results that carry a reason (enquiry_route.lost_reason, v38).
REASON_RESULTS = ("Lost", "Cancelled")
DEFAULT_SCOPES = ["Door to door", "Door to port", "Port to door", "Port to port"]


def letter(seq) -> str:
    """1 -> 'A', 2 -> 'B', ... 27 -> 'AA'."""
    n, out = int(seq), ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out or "?"


def parse_ids(text) -> list | None:
    """'3,5' -> [3, 5]; None / '' -> None (all routes)."""
    ids = [int(p) for p in str(text or "").split(",") if p.strip().isdigit()]
    return ids or None


def format_ids(ids) -> str | None:
    ids = sorted({int(i) for i in (ids or [])})
    return ",".join(str(i) for i in ids) or None


def has_routes(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'enquiry_route'").fetchone() \
        is not None


def routes_of(conn: sqlite3.Connection, enquiry_id: int, voided: bool | None = False) -> list:
    """The case's routes in seq order as dicts with their letter; live ones
    unless `voided` (True: the struck-out ones, None: all). v39: each with
    'dest_places' and 'pod_places', its master places in order."""
    if not has_routes(conn):
        return []
    sql = "SELECT * FROM enquiry_route WHERE enquiry_id = ?"
    args = [enquiry_id]
    if voided is not None:
        sql += " AND voided = ?"
        args.append(1 if voided else 0)
    out = []
    for row in conn.execute(sql + " ORDER BY seq, id", args):
        route = dict(row)
        route["letter"] = letter(route["seq"])
        out.append(route)
    attach_places(conn, out)
    return out


# ------------------------------------------------------------- places (v39) --
#: what a place row carries for the pages
_PLACE_COLUMNS = ("l.id, l.key, l.name, l.type, l.country, l.subdivision, l.unlocode, l.iata, "
                  "l.is_seaport, l.is_airport, l.status")


def has_places(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'enquiry_route_place'"
                        ).fetchone() is not None


def places_of(conn, route_ids) -> dict:
    """{route id: {'dest': [place], 'pod': [place]}}: the live rows of
    enquiry_route_place in order, each a dict of its master place."""
    route_ids = [i for i in (route_ids or []) if i is not None]
    out = {}
    if not route_ids or not has_places(conn):
        return out
    for r in conn.execute(
            "SELECT p.route_id, p.side, {} FROM enquiry_route_place p JOIN master_location l ON l.id = p.location_id "
            "WHERE p.voided = 0 AND p.route_id IN ({}) ORDER BY p.route_id, p.side, p.seq".format(
                _PLACE_COLUMNS, ",".join("?" for _ in route_ids)), route_ids):
        place = dict(r)
        out.setdefault(place.pop("route_id"), {"dest": [], "pod": []})[place.pop("side")].append(place)
    return out


def attach_places(conn, route_list) -> None:
    """Give each route dict its 'dest_places' and 'pod_places'. The first
    destination is always dest_loc_id (the repository keeps them so; a row
    written around it does not win)."""
    found = places_of(conn, [r.get("id") for r in route_list])
    for route in route_list:
        own = found.get(route.get("id"), {})
        dests = list(own.get("dest", []))
        first = route.get("dest_loc_id")
        if not first:
            dests = []
        elif not dests or dests[0]["id"] != first:
            row = conn.execute("SELECT {} FROM master_location l WHERE l.id = ?".format(_PLACE_COLUMNS),
                               (first,)).fetchone()
            dests = ([dict(row)] if row is not None else []) + [d for d in dests[1:] if d["id"] != first]
        route["dest_places"] = dests
        route["pod_places"] = list(own.get("pod", []))


def pod_text(route, name_of=None) -> str:
    """The Port of discharge as words: its places with 'or' between them,
    else the text the route holds."""
    ports = route.get("pod_places") or []
    if ports:
        return " or ".join((name_of(p["id"], p["key"]) if name_of else p["key"]) or p["key"] for p in ports)
    return route.get("pod") or ""


def route_a(conn, enquiry_id):
    live = routes_of(conn, enquiry_id)
    return live[0] if live else None


def applies_to(ids_text, live_routes) -> list:
    """The live routes a line or a vendor request covers: the recorded ones
    still live, or every live route when none is recorded (or none of the
    recorded ones is live any more)."""
    ids = parse_ids(ids_text)
    if ids:
        chosen = [r for r in live_routes if r["id"] in ids]
        if chosen:
            return chosen
    return list(live_routes)


def letters_of(ids_text, live_routes) -> str:
    """'All' or 'A, C' for a line's or a vendor request's Routes column."""
    ids = parse_ids(ids_text)
    if not ids or len(live_routes) <= 1:
        return "All"
    chosen = [r["letter"] for r in live_routes if r["id"] in ids]
    return ", ".join(chosen) if chosen and len(chosen) < len(live_routes) else "All"


# ------------------------------------------------------------------ totals --
def chargeable(gross, volume):
    if gross is None and volume is None:
        return None
    return max(gross or 0.0, (volume or 0.0) * cargo.CBM_TO_KG)


def totals(conn, enquiry_id: int, lines=None, live_routes=None, case=None) -> dict:
    """{'per_route': [...], 'gross_kg', 'volume_cbm', 'air_weight_kg',
    'lcl_cbm', 'container_qty', 'container_type', 'lines_counted', 'project'}.
    `lines` / `live_routes` / `case` can be handed in (the register form)."""
    if case is None:
        case = conn.execute("SELECT * FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone()
    live_routes = routes_of(conn, enquiry_id) if live_routes is None else live_routes
    if not live_routes:
        live_routes = [{"id": None, "seq": 1, "letter": "A", "service_mode": case["service_mode"] if case else None}]
    lines = cargo.lines_of(conn, enquiry_id) if lines is None else lines
    counted = [l for l in lines if not l.get("voided") and int(l.get("counted", 1) or 0)]
    per = {r["id"]: {"route": r, "gross": None, "volume": None, "containers": 0, "types": [],
                     "per_month": 0.0, "project": 0.0} for r in live_routes}

    def add(bucket, key, value):
        if value is not None:
            bucket[key] = (bucket[key] or 0.0) + value

    project_months = []
    for line in counted:
        d = cargo.derive(line)
        for route in applies_to(line.get("route_ids"), live_routes):
            bucket = per[route["id"]]
            add(bucket, "gross", d["weight_n"])
            add(bucket, "volume", d["volume_cbm"])
            qty = line.get("container_qty")
            if qty:
                bucket["containers"] += int(qty)
            if line.get("container_type"):
                # 15b: a type with no qty yet is still the line's type
                bucket["types"].append(line["container_type"])
            if line.get("per_month"):
                shipments = float(line["per_month"])
                bucket["per_month"] += shipments * (int(qty) if qty else 1)
                if line.get("months"):
                    bucket["project"] += shipments * float(line["months"]) * (int(qty) if qty else 1)
                    project_months.append(float(line["months"]))
    out = {"per_route": [], "lines_counted": len(counted)}
    if counted:
        gross = sum(b["gross"] for b in per.values() if b["gross"] is not None) \
            if any(b["gross"] is not None for b in per.values()) else None
        volume = sum(b["volume"] for b in per.values() if b["volume"] is not None) \
            if any(b["volume"] is not None for b in per.values()) else None
    else:
        gross = case["gross_kg"] if case is not None and "gross_kg" in case.keys() else None
        volume = case["volume_cbm"] if case is not None and "volume_cbm" in case.keys() else None
    air = lcl = None
    containers, types = 0, []
    for bucket in per.values():
        mode = bucket["route"].get("service_mode") or (case["service_mode"] if case is not None else None)
        g, v = (bucket["gross"], bucket["volume"]) if counted else (None, None)
        row = {"route": bucket["route"], "mode": mode, "gross": g, "volume": v,
               "chargeable": chargeable(g, v) if mode in cargo.AIR_MODES else None,
               "wm": max(v or 0, (g or 0) / 1000.0) if mode == "LCL" and (g is not None or v is not None) else None,
               "containers": bucket["containers"], "per_month": bucket["per_month"], "project": bucket["project"]}
        out["per_route"].append(row)
        if counted:
            if mode in cargo.AIR_MODES and row["chargeable"] is not None:
                air = (air or 0.0) + row["chargeable"]
            if mode == "LCL" and v is not None:
                lcl = (lcl or 0.0) + v
            # 15b: the containers a line gives, whatever the route's mode
            # (a RORO or Project case typed them too)
            containers += bucket["containers"]
            types += bucket["types"]
    if not counted:
        # the case's own typed numbers, charged in route A's (the case's) mode
        mode = case["service_mode"] if case is not None else None
        air = chargeable(gross, volume) if mode in cargo.AIR_MODES else None
        lcl = volume if mode == "LCL" else None
    out.update({
        "gross_kg": round(gross, 2) if gross is not None else None,
        "volume_cbm": round(volume, 3) if volume is not None else None,
        "air_weight_kg": round(air, 3) if air is not None else None,
        "lcl_cbm": round(lcl, 3) if lcl is not None else None,
        "container_qty": containers or None,
        "container_type": (types[0] if types and len(set(types)) == 1 else ("Mixed" if types else None)),
        "per_month": sum(r["per_month"] for r in out["per_route"]) or None,
        "project": sum(r["project"] for r in out["per_route"]) or None,
        "months": max(project_months) if project_months else None,
    })
    return out


def enquiry_fields(t: dict, case) -> dict:
    """What the repository writes into the enquiry row from totals()."""
    fields = {"gross_kg": t["gross_kg"], "volume_cbm": t["volume_cbm"],
              "air_weight_kg": t["air_weight_kg"], "lcl_cbm": t["lcl_cbm"]}
    if t["lines_counted"] and t["container_qty"]:
        fields["container_qty"] = t["container_qty"]
        if t["container_type"]:
            fields["container_type"] = t["container_type"]
    return fields


# ------------------------------------------------------------------ status --
def status_target(live_routes) -> str | None:
    """The OPP status its routes say: Won when any route is won; otherwise,
    once every live route has a result, Lost when any of them is lost and
    Cancelled when all are cancelled; otherwise None (unchanged)."""
    results = [r.get("result") for r in live_routes]
    if any(x == "Won" for x in results):
        return "Won"
    if results and all(x in REASON_RESULTS for x in results):
        return "Lost" if "Lost" in results else "Cancelled"
    return None


def result_line(live_routes) -> str:
    """'won A, C · lost B · cancelled D' - empty while no route has a result."""
    parts = []
    for result in RESULTS:
        letters = [r["letter"] for r in live_routes if r.get("result") == result]
        if letters:
            parts.append("{} {}".format(result.lower(), ", ".join(letters)))
    return " · ".join(parts)


def place(route, side, name_of=None) -> str:
    """A route side as words: the place title (or key), else the country.
    v39: a destination of several places reads 'San Antonio or Valparaiso'."""
    dests = route.get("dest_places") or [] if side == "dest" else []
    if len(dests) > 1:
        return " or ".join((name_of(p["id"], p["key"]) if name_of else p["key"]) or p["key"] for p in dests)
    loc = route.get("{}_loc".format(side))
    title = name_of(route.get("{}_loc_id".format(side)), loc) if name_of else (loc or "")
    return title or route.get("{}_country".format(side)) or "?"


def title(live_routes, name_of=None) -> str:
    """The header: 'Santos to Exshaw · Brookfield · Bloomsdale' when the
    routes share an origin, else route A's lane and '+N'."""
    if not live_routes:
        return ""
    origins = {(r.get("origin_loc_id"), r.get("origin_loc"), r.get("origin_country")) for r in live_routes}
    first = live_routes[0]
    if len(live_routes) == 1 or len(origins) == 1:
        dests = []
        for r in live_routes:
            d = place(r, "dest", name_of)
            if d not in dests:
                dests.append(d)
        return "{} to {}".format(place(first, "origin", name_of), " · ".join(dests))
    return "{} to {} +{}".format(place(first, "origin", name_of), place(first, "dest", name_of),
                                 len(live_routes) - 1)

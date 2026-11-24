"""Shipments (2026-09-25, schema v24): one shipment per won
case, and what happens to it - milestones, issues, notes - as events.

Small and honest: nothing here talks to a carrier system. Every event
is something a mail said or Iris typed; the page shows the last
thing recorded, never a live position. The milestone sets, the derived
state (current milestone, open issues, delay) and the prefill from the
case live here so the pages, the list and tracking rule R10 read one
rule.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import db, tracking

#: The milestone names per mode, in order. The stepper, the select and
#: the 'current milestone' rule all read this.
MILESTONES = {
    "ocean": ("Booked", "Picked up", "Departed", "Arrived", "Delivered", "Invoiced"),
    "air": ("Booked", "Picked up", "Departed", "Arrived", "Delivered", "Invoiced"),
    "truck": ("Booked", "Dispatched", "Picked up", "Delivered", "POD", "Invoiced"),
    # SA1 (v48): two kinds of their own. The mail reading keeps its words
    # (Departed reads as Departed by sea); At hub, Departed by air, At border
    # and Crossed are set by hand.
    "sea_air": ("Booked", "Picked up", "Departed by sea", "At hub", "Departed by air", "Arrived",
                "Delivered", "Invoiced"),
    "cross_border_truck": ("Booked", "Dispatched", "Picked up", "At border", "Crossed", "Delivered",
                           "POD", "Invoiced"),
}
MODES = tuple(MILESTONES)
MODE_LABELS = {"ocean": "Ocean", "air": "Air", "truck": "Truckload", "sea_air": "Sea-Air",
               "cross_border_truck": "Cross-border Truck"}
#: SA1: the kinds that run as a truck does (the delivery window is the
#: target, no In transit, POD attached is POD, the US map when it stays home).
TRUCK_KINDS = ("truck", "cross_border_truck")
#: The milestones a shipment is moving between ports after.
DEPARTED = ("Departed", "Departed by sea", "Departed by air")
KINDS = ("milestone", "issue", "note", "info")
#: The milestone that ends the shipment's active life (the list's Active
#: filter and R10 stop at it).
DELIVERED = "Delivered"
#: Editable detail fields (the Edit form), in the order the form shows them.
DETAIL_FIELDS = (
    "shipper_name", "shipper_address", "consignee_name", "consignee_address",
    "carrier", "carrier_detail", "booking_no", "mbl", "hbl", "awb", "our_file", "agent_ref",
    "load_no", "bol", "customer_ref", "price_note", "special", "via_points",
    "planned_pickup", "planned_departure", "planned_arrival", "planned_delivery", "eta", "remarks",
)
REFERENCE_FIELDS = (
    ("booking_no", "Booking"), ("mbl", "MBL"), ("hbl", "HBL"), ("awb", "AWB"), ("our_file", "Our file"),
    ("agent_ref", "Agent ref"), ("load_no", "Load number"), ("bol", "BOL"), ("customer_ref", "Customer ref"),
    ("special", "Special"),
)


# ------------------------------------------------------------ helpers -----
def mode_for(service_mode) -> str:
    """FCL / LCL / RORO / Break Bulk -> ocean, Air -> air, Truck ->
    truck (TK1: the mode that was Domestic), Sea-Air -> sea_air and
    Cross-border Truck -> cross_border_truck (SA1), anything else ocean."""
    text = (service_mode or "").strip().lower()
    if text == "air":
        return "air"
    if text == "sea-air":
        return "sea_air"
    if text == "cross-border truck":
        return "cross_border_truck"
    if text in ("truck", "truckload", "ltl", "ftl"):
        return "truck"
    return "ocean"


def milestone_index(mode: str, name) -> int | None:
    names = MILESTONES.get(mode, MILESTONES["ocean"])
    return names.index(name) if name in names else None


def now_stamp(now=None) -> str:
    return (now or dt.datetime.now()).strftime("%Y-%m-%d %H:%M")


def _today(today=None) -> str:
    return today or dt.date.today().isoformat()


def _day(value) -> str:
    from .dates import month_day
    return month_day(value)


def _days(start, end):
    try:
        return (dt.date.fromisoformat(str(end)[:10]) - dt.date.fromisoformat(str(start)[:10])).days
    except (TypeError, ValueError):
        return None


# -------------------------------------------------------------- start -----
def for_enquiry(conn, enquiry_id: int):
    """The case's first shipment (route A's when there is one)."""
    return conn.execute("SELECT s.* FROM shipment s LEFT JOIN enquiry_route r ON r.id = s.route_id "
                        "WHERE s.enquiry_id = ? ORDER BY COALESCE(r.seq, 0), s.id LIMIT 1", (enquiry_id,)).fetchone()


def for_route(conn, route_id: int):
    """A won route's shipment (v33: one per won route)."""
    return conn.execute("SELECT * FROM shipment WHERE route_id = ?", (route_id,)).fetchone()


def by_route(conn, enquiry_id: int) -> dict:
    """{route_id: shipment id} for the case's routes (the Routes card)."""
    return {r["route_id"]: r["id"] for r in conn.execute(
        "SELECT id, route_id FROM shipment WHERE enquiry_id = ? AND route_id IS NOT NULL", (enquiry_id,))}


def prefill(conn, enquiry_id: int, title: str | None = None, route=None) -> dict:
    """The shipment row a won case starts with: title from the lane, mode
    from the service mode, shipper / consignee addresses from the alt
    fields, the carrier from the awarded vendor when a rate was used.
    Planned dates and our file stay empty; she fills them in."""
    row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (enquiry_id,)).fetchone()
    if row is None:
        raise KeyError("inquiry {} not found".format(enquiry_id))
    if route is not None:
        # v33: the route's own lane and mode
        row = dict(row)
        for key in ("origin_loc", "origin_country", "origin_alt", "dest_loc", "dest_country", "dest_alt",
                    "service_mode"):
            row[key] = route[key] if route[key] is not None else (row[key] if key == "service_mode" else None)
    lane = [row["origin_loc"] or row["origin_country"], row["dest_loc"] or row["dest_country"]]
    lane = [p for p in lane if p]
    carrier = None
    if row["rate_used"] in ("agent", "office", "carrier") and row["awarded_vendor_id"]:
        party = conn.execute("SELECT key FROM party WHERE id = ?", (row["awarded_vendor_id"],)).fetchone()
        carrier = party["key"] if party else None
    return {
        "enquiry_id": enquiry_id,
        "mode": mode_for(row["service_mode"]),
        "title": title or (" to ".join(lane) if len(lane) == 2 else (lane[0] if lane else row["opp_no"])),
        "shipper_address": row["origin_alt"] or None,
        "consignee_address": row["dest_alt"] or None,
        "carrier": carrier,
    }


def place_title(conn, loc_id, key) -> str:
    """'Aspers, PA' for a master row (name, then the subdivision when the
    row has one); the key when the name is blank; '' when nothing."""
    row = None
    if loc_id:
        row = conn.execute("SELECT * FROM master_location WHERE id = ?", (loc_id,)).fetchone()
    if row is None:
        return (key or "").strip()
    name = (row["name"] or "").strip() or (row["key"] or "").strip()
    sub = (row["subdivision"] if "subdivision" in row.keys() else "") or ""
    return "{}, {}".format(name, sub.strip()) if sub.strip() and sub.strip() not in name else name


def route_of(conn, enquiry_id: int, route_id: int | None = None):
    """The given live route of the case, or route A."""
    if route_id:
        return conn.execute("SELECT * FROM enquiry_route WHERE id = ? AND enquiry_id = ?",
                            (route_id, enquiry_id)).fetchone()
    return conn.execute("SELECT * FROM enquiry_route WHERE enquiry_id = ? AND voided = 0 ORDER BY seq LIMIT 1",
                        (enquiry_id,)).fetchone()


def button_title(conn, row) -> str | None:
    """The title the Start shipment button gives (a v_enquiry row):
    'Aspers, PA to San Antonio', or None to let prefill() use the lane."""
    origin = place_title(conn, row["origin_loc_id"], row["origin_loc"]) or row["origin_country"]
    dest = place_title(conn, row["dest_loc_id"], row["dest_loc"]) or row["dest_country"]
    return "{} to {}".format(origin, dest) if origin and dest else None


def start(repo, enquiry_id: int, title: str | None = None, route_id: int | None = None) -> int:
    """Create the one shipment of a won case, prefilled. No 'Booked' event
    is written: she logs it when it is true. The case's mail threads
    (enquiry.conversation_id / thread_key, enquiry_thread) reach the
    shipment through enquiry_id - nothing is copied."""
    route = route_of(repo.conn, enquiry_id, route_id) if _has_routes(repo.conn) else None
    existing = for_route(repo.conn, route["id"]) if route is not None else for_enquiry(repo.conn, enquiry_id)
    if existing is not None:
        return existing["id"]
    status = repo.conn.execute("SELECT status FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone()
    if status is None or status["status"] != "Won":
        raise ValueError("only a won case starts a shipment")
    if route is not None and route_id and route["result"] != "Won":
        raise ValueError("only a won route starts a shipment")
    if route is not None and title is None:
        title = button_title(repo.conn, route)
    values = prefill(repo.conn, enquiry_id, title, route)
    if route is not None:
        values["route_id"] = route["id"]
    values["created_at"] = now_stamp()
    values["updated_at"] = values["created_at"]
    return repo.insert("shipment", values)


# ------------------------------------------------------------- events -----
def events(conn, shipment_id: int, include_voided: bool = True) -> list:
    """Newest first."""
    sql = "SELECT * FROM shipment_event WHERE shipment_id = ?"
    if not include_voided:
        sql += " AND voided = 0"
    return conn.execute(sql + " ORDER BY at DESC, id DESC", (shipment_id,)).fetchall()


def _touch(repo, shipment_id: int) -> None:
    repo.update("shipment", shipment_id, {"updated_at": now_stamp()})


def log_milestone(repo, shipment_id: int, milestone: str, at: str, detail: str | None = None,
                  eta: str | None = None, source: str = "manual", message_id=None, sender=None) -> int:
    ship = repo.conn.execute("SELECT mode FROM shipment WHERE id = ?", (shipment_id,)).fetchone()
    if ship is None:
        raise KeyError("shipment {} not found".format(shipment_id))
    if milestone not in MILESTONES[ship["mode"]]:
        raise ValueError("{} is not a {} milestone".format(milestone, ship["mode"]))
    event_id = repo.insert("shipment_event", {
        "shipment_id": shipment_id, "at": at, "kind": "milestone", "milestone": milestone,
        "title": milestone, "detail": (detail or "").strip() or None, "eta": eta or None,
        "source": source, "message_id": message_id, "sender": sender, "created_at": now_stamp()})
    if eta:
        repo.update("shipment", shipment_id, {"eta": eta})
    _touch(repo, shipment_id)
    return event_id


def log_issue(repo, shipment_id: int, title: str, at: str, detail: str | None = None,
              source: str = "manual", message_id=None, sender=None) -> int:
    event_id = repo.insert("shipment_event", {
        "shipment_id": shipment_id, "at": at, "kind": "issue", "title": title.strip(),
        "detail": (detail or "").strip() or None, "source": source, "message_id": message_id,
        "sender": sender, "created_at": now_stamp()})
    _touch(repo, shipment_id)
    return event_id


def add_note(repo, shipment_id: int, title: str, at: str, detail: str | None = None,
             kind: str = "note", source: str = "manual", message_id=None, sender=None) -> int:
    event_id = repo.insert("shipment_event", {
        "shipment_id": shipment_id, "at": at, "kind": kind, "title": title.strip(),
        "detail": (detail or "").strip() or None, "source": source, "message_id": message_id,
        "sender": sender, "created_at": now_stamp()})
    _touch(repo, shipment_id)
    return event_id


def resolve(repo, shipment_id: int, issue_id: int, at: str | None = None, detail: str | None = None) -> int:
    """Close an issue: an info event 'Resolved: <title>' that points at it."""
    issue = repo.conn.execute(
        "SELECT id, title, kind FROM shipment_event WHERE id = ? AND shipment_id = ?",
        (issue_id, shipment_id)).fetchone()
    if issue is None or issue["kind"] != "issue":
        raise KeyError("issue {} not on shipment {}".format(issue_id, shipment_id))
    event_id = repo.insert("shipment_event", {
        "shipment_id": shipment_id, "at": at or now_stamp(), "kind": "info",
        "title": "Resolved: {}".format(issue["title"]), "detail": (detail or "").strip() or None,
        "source": "manual", "resolves_event_id": issue_id, "created_at": now_stamp()})
    _touch(repo, shipment_id)
    return event_id


def void_event(repo, shipment_id: int, event_id: int, voided: bool = True) -> list:
    """Strike an event out or put it back. E2: the suggestion it was
    accepted from is pending again; Put back makes it accepted again. The
    lines to tell her."""
    from . import undo
    row = repo.conn.execute("SELECT id FROM shipment_event WHERE id = ? AND shipment_id = ?",
                            (event_id, shipment_id)).fetchone()
    if row is None:
        raise KeyError("event {} not on shipment {}".format(event_id, shipment_id))
    repo.new_action()
    if voided:
        repo.update("shipment_event", event_id, {"voided": 1})
        notes = undo.suggestion_back(repo, "shipment_event:{}".format(event_id))
    else:
        notes = undo.put_back(repo, "shipment_event", event_id)
    _touch(repo, shipment_id)
    return notes


def edit(repo, shipment_id: int, values: dict) -> int:
    clean = {k: ((v or "").strip() or None) if isinstance(v, str) else v
             for k, v in values.items() if k in DETAIL_FIELDS}
    changed = repo.update("shipment", shipment_id, clean)
    if changed:
        _touch(repo, shipment_id)
    return changed


#: Why a shipment was closed (5b): Delivered needs the milestone; the other
#: two close a shipment at any point - started by mistake, or never moved -
#: without deleting anything.
CLOSE_REASONS = ("Delivered", "Not shipped", "Registered in error")


def close(repo, shipment_id: int, closed: bool = True, reason: str = "Delivered") -> None:
    """Close with a reason: 'Delivered' only once the milestone is there;
    'Not shipped' / 'Registered in error' at any milestone. Reopen undoes
    it and clears the reason."""
    if closed:
        if reason not in CLOSE_REASONS:
            raise ValueError("pick a reason to close: {}".format(", ".join(CLOSE_REASONS)))
        if reason == "Delivered" and not summary(repo.conn, shipment_id)["delivered"]:
            raise ValueError("a shipment closes as Delivered once it is delivered")
        repo.update("shipment", shipment_id, {"closed": 1, "closed_reason": reason})
    else:
        repo.update("shipment", shipment_id, {"closed": 0, "closed_reason": None})
    _touch(repo, shipment_id)


# ------------------------------------------------------------ summary -----
def summary(conn: sqlite3.Connection, shipment_id: int, today=None) -> dict:
    """The derived state, in one place: current milestone (the last live
    milestone event in set order), open issues (issue events not voided
    and not pointed at by a resolving event), delayed days (eta past the
    planned arrival, or the planned delivery for a truck), the header
    pills, and what the list and R10 need (last event, days since)."""
    ship = conn.execute("SELECT * FROM shipment WHERE id = ?", (shipment_id,)).fetchone()
    if ship is None:
        raise KeyError("shipment {} not found".format(shipment_id))
    today = _today(today)
    mode = ship["mode"] if ship["mode"] in MILESTONES else "ocean"
    names = MILESTONES[mode]
    live = [e for e in events(conn, shipment_id) if not e["voided"]]         # newest first
    milestone_at = {}
    for e in reversed(live):                                                 # oldest first: first date wins
        if e["kind"] == "milestone" and e["milestone"] in names:
            milestone_at.setdefault(e["milestone"], e["at"])
    current_index = max((names.index(n) for n in milestone_at), default=None)
    current = names[current_index] if current_index is not None else None
    resolved = {e["resolves_event_id"] for e in live if e["resolves_event_id"]}
    open_issues = [e for e in live if e["kind"] == "issue" and e["id"] not in resolved]
    target = ship["planned_delivery"] if mode in TRUCK_KINDS else ship["planned_arrival"]
    delayed = _days(target, ship["eta"]) if ship["eta"] and target else None
    delayed_days = delayed if delayed and delayed > 0 else 0
    delivered = current_index is not None and current_index >= names.index(DELIVERED)
    arrived = any(n in milestone_at for n in ("Arrived", "Delivered", "POD", "Invoiced"))

    if current is None:
        status_text = "Not started"
    elif mode not in TRUCK_KINDS and current in DEPARTED:
        status_text = "In transit"
    else:
        status_text = current
    pills = [(MODE_LABELS[mode], "p-mode"), (status_text, "p-transit")]
    if delayed_days:
        pills.append(("Delayed {} day{}".format(delayed_days, "" if delayed_days == 1 else "s"), "p-delay"))
    for issue in open_issues:
        pills.append((issue["title"], "p-delay"))

    last = live[0] if live else None
    since = (last["at"] if last else ship["created_at"] or "")[:10]
    return {
        "ship": ship, "mode": mode, "milestones": names, "milestone_at": milestone_at,
        "current": current, "current_index": current_index, "status_text": status_text,
        "open_issues": open_issues, "delayed_days": delayed_days, "delivered": delivered,
        "arrived": arrived, "target": target, "pills": pills,
        "last_event": last, "days_since_last": _days(since, today) if since else None,
        "working_days_since_last": tracking.business_days_between(since, today) if since else None,
        "active": not ship["closed"] and not delivered,
        "events": live,
    }


def stepper(state: dict) -> list:
    """[(name, state, small line)] for the header's stepper: done / current
    / todo, the small line the milestone's date or the planned date."""
    ship = state["ship"]
    planned = {
        "Picked up": ship["planned_pickup"], "Dispatched": ship["planned_departure"],
        "Departed": ship["planned_departure"], "Departed by sea": ship["planned_departure"],
        "Arrived": ship["eta"] or ship["planned_arrival"],
        "Delivered": ship["planned_delivery"],
    }
    out = []
    for i, name in enumerate(state["milestones"]):
        if state["current_index"] is None:
            rung = "todo"
        elif i < state["current_index"]:
            rung = "done"
        elif i == state["current_index"]:
            rung = "current"
        else:
            rung = "todo"
        if name in state["milestone_at"]:
            sub = _day(state["milestone_at"][name])
        elif planned.get(name):
            sub = "{} {}".format("ETA" if name == "Arrived" and ship["eta"] else "planned", _day(planned[name]))
        else:
            sub = ""
        out.append((name, rung, sub))
    return out


def facts(state: dict) -> list:
    """The four header facts per mode: [(label, value, small)]."""
    ship = state["ship"]
    at = state["milestone_at"]
    if state["mode"] in TRUCK_KINDS:
        return [
            ("Pickup window", _day(ship["planned_pickup"]) or "-", "picked up {}".format(_day(at["Picked up"])) if "Picked up" in at else ""),
            ("Delivery window", _day(ship["planned_delivery"]) or "-", "delivered {}".format(_day(at["Delivered"])) if "Delivered" in at else ""),
            ("Price", ship["price_note"] or "-", ""),
            ("Load number", ship["load_no"] or "none", ""),
        ]
    picked = at.get("Picked up")
    departed = at.get("Departed") or at.get("Departed by sea")
    expected = ship["eta"] or ship["planned_arrival"]
    was = ship["planned_arrival"] if ship["eta"] and ship["planned_arrival"] and ship["eta"] != ship["planned_arrival"] else None
    planned_transit = _days(ship["planned_departure"], ship["planned_arrival"])
    current_transit = _days(departed or ship["planned_departure"], expected)
    return [
        ("Picked up", _day(picked) or (_day(ship["planned_pickup"]) or "-"), "" if picked else ("planned" if ship["planned_pickup"] else "")),
        ("Departure", _day(departed) or (_day(ship["planned_departure"]) or "-"), "" if departed else ("planned" if ship["planned_departure"] else "")),
        ("Expected arrival", _day(expected) or "-", "was {}".format(_day(was)) if was else ("" if ship["eta"] else ("planned" if expected else ""))),
        ("Transit", "{} days".format(current_transit) if current_transit is not None else "-",
         "planned {}".format(planned_transit) if planned_transit is not None else ""),
    ]


def references(ship) -> list:
    return [(label, ship[field]) for field, label in REFERENCE_FIELDS if ship[field]]


# ---------------------------------------------------------------- list ----
def list_rows(conn, which: str = "active", today=None) -> list:
    """The /shipments table, sorted by days since the last event, most
    quiet first. Active = not closed and before Delivered; Delivered =
    delivered or closed; All = everything."""
    rows = []
    for ship in conn.execute(
            "SELECT s.id, s.enquiry_id, e.opp_no, e.requester_key FROM shipment s "
            "JOIN v_enquiry e ON e.id = s.enquiry_id ORDER BY s.id"):
        state = summary(conn, ship["id"], today)
        if which == "active" and not state["active"]:
            continue
        if which == "delivered" and state["active"]:
            continue
        issue = state["open_issues"][0]["title"] if state["open_issues"] else ""
        s = state["ship"]
        rows.append({
            "id": ship["id"], "enquiry_id": ship["enquiry_id"], "opp_no": ship["opp_no"],
            "title": s["title"], "mode": state["mode"], "mode_label": MODE_LABELS[state["mode"]],
            "requester": ship["requester_key"] or "", "current": state["status_text"],
            "eta": s["eta"] or (s["planned_delivery"] if state["mode"] in TRUCK_KINDS else s["planned_arrival"]),
            "issue": issue, "days": state["days_since_last"],
            "last": state["last_event"]["title"] if state["last_event"] else "",
            "closed": bool(s["closed"]),
        })
    rows.sort(key=lambda r: (-(r["days"] if r["days"] is not None else -1), r["title"]))
    return rows


# -------------------------------------------------------------- R10 -------
def attention(conn, today=None) -> list:
    """Active shipments that need a look: an open issue older than one
    working day, an ETA gone by with no arrival / delivery, or nothing
    logged for five working days. [(shipment_id, enquiry_id, title,
    reasons, days, since)]."""
    today = _today(today)
    out = []
    for ship in conn.execute("SELECT id, enquiry_id FROM shipment WHERE closed = 0 ORDER BY id"):
        state = summary(conn, ship["id"], today)
        if not state["active"]:
            continue
        reasons = []
        days = None
        since = ""
        for issue in state["open_issues"]:
            age = tracking.business_days_between(issue["at"][:10], today)
            if age is not None and age > 1:
                reasons.append("issue open {} working days: {}".format(age, issue["title"]))
                days, since = max(days or 0, age), since or issue["at"][:10]
        eta = state["ship"]["eta"] or state["target"]
        if eta and eta < today and not state["arrived"]:
            late = tracking.business_days_between(eta, today) or 0
            reasons.append("ETA {} passed, no arrival logged".format(_day(eta)))
            days, since = max(days or 0, late), since or eta
        quiet = state["working_days_since_last"]
        if quiet is not None and quiet >= 5:
            reasons.append("no event for {} working days".format(quiet))
            days, since = max(days or 0, quiet), since or (state["last_event"]["at"][:10] if state["last_event"] else "")
        if reasons:
            out.append((ship["id"], ship["enquiry_id"], state["ship"]["title"], reasons, days, since))
    return out


# ----------------------------------------------------------- detection ----
#: What a mail says about a shipment (5b): rules only, one table of phrases,
#: read on the sender's own words (mail_archive.top_part), never on the
#: quoted chain. Each row: (kind, name, phrases, modes or None). The first
#: line that carries a phrase decides; on one line an issue wins over a
#: milestone.
PHRASES = (
    ("milestone", "Booked", ("booking confirmed", "booking confirmation", "bk#", "booking number",
                             "we have booked"), None),
    ("milestone", "Picked up", ("picked up", "pickup completed", "cargo collected", "container was pulled"), None),
    ("milestone", "Picked up", ("gate in",), ("ocean", "sea_air")),
    ("milestone", "Departed", ("departed", "sailed", "on board", "sob", "flown", "flight departed", "uplifted"), None),
    ("milestone", "Arrived", ("arrived", "discharged", "landed"), None),
    ("milestone", "Delivered", ("delivered", "delivery completed", "received the cargo"), None),
    ("milestone", "Delivered", ("pod attached",), ("ocean", "air", "sea_air")),
    ("milestone", "POD", ("pod attached",), TRUCK_KINDS),
    ("milestone", "Invoiced", ("invoice", "debit note"), None),
)
#: Issue phrases -> the issue title they raise.
ISSUE_PHRASES = (
    (("cut-off", "cutoff"), "Missed cut-off"),           # 'reached the terminal after the cut-off' (5e)
    (("eta moved", "revised eta", "new eta"), "ETA moved"),
    (("delayed", "delay", "postponed", "rolled"), "Delay"),
    (("customs hold", "exam"), "Customs hold"),
    (("damage",), "Damage"),
    (("no show", "missed"), "Pickup missed"),
    (("driver",), "Driver problem"),
    (("liftgate",), "Liftgate"),
    (("dispute",), "Dispute"),
    (("cannot", "unable", "problem", "issue"), "Problem reported"),
)
#: 'please send us your DN' is a request, not an invoice: a note.
NOTE_PHRASES = (("please send us your dn", "send us your dn", "send your dn", "please send the dn",
                 "dn please", "your debit note"), "Debit note requested")
_DN_WORD = re.compile(r"\bDN\b")
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                        "nov", "dec"), start=1)}
_DATE_DM = re.compile(r"\b(?P<d>\d{1,2})(?:st|nd|rd|th)?[ -](?P<m>[A-Za-z]{3,9})\.?(?:[ ,-]+(?P<y>\d{4}))?")
_DATE_MD = re.compile(r"\b(?P<m>[A-Za-z]{3,9})\.?\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(?P<y>\d{4}))?")
_DATE_ISO = re.compile(r"\b(?P<y>\d{4})-(?P<m>\d\d)-(?P<d>\d\d)")
_DATE_NUM = re.compile(r"\b(?P<m>\d{1,2})/(?P<d>\d{1,2})(?:/(?P<y>\d{2,4}))?\b")
_TIME = re.compile(r"\b(\d{1,2}):(\d\d)\b")
_ETA = re.compile(r"\bETA\b", re.I)
_ETD = re.compile(r"\bETD\b", re.I)


@dataclass
class Proposal:
    """What one mail proposes for a shipment."""
    kind: str                  # milestone | issue | note
    name: str                  # the milestone, or the issue / note title
    at: str                    # 'YYYY-MM-DD HH:MM'
    quote: str                 # the line it was read from
    confidence: str            # high (phrase and a date in the sender's words) | medium
    detail: str = ""           # the mail's first line
    eta: str | None = None     # a new ETA the mail carries
    message_id: str = ""
    sender: str = ""
    phrase: str = ""
    planned: bool = False      # a milestone the mail plans, not one that happened (5e)


def find_date(text: str, year: int) -> str | None:
    """The first date in a line: '25 Oct', 'Oct 25', '2026-10-25', '10/25';
    the mail's year when the text has none. None when there is none."""
    for pattern in (_DATE_ISO, _DATE_DM, _DATE_MD, _DATE_NUM):
        for found in pattern.finditer(text):
            g = found.groupdict()
            try:
                if pattern in (_DATE_DM, _DATE_MD):
                    month = _MONTHS.get(g["m"][:3].lower())
                    if not month:
                        continue
                else:
                    month = int(g["m"])
                y = g.get("y")
                y = int(y) if y else year
                if y < 100:
                    y += 2000
                return dt.date(y, month, int(g["d"])).isoformat()
            except (ValueError, TypeError):
                continue
    return None


_LABELLED = re.compile(r"\b(ETA|ETD|ETS|ATA|ATD)\b[^0-9]{0,16}$", re.I)   # 'ETD ORD 10/06' is an ETD figure, not an event date
NEAR = 48


def date_near(line: str, phrase: str, year: int) -> str | None:
    """A date within NEAR characters of the phrase, unless it is an ETA /
    ETD / ETS figure (those belong to the ETA / ETD rules)."""
    low = line.lower()
    pos = low.find(phrase.lower())
    if pos < 0:
        return find_date(line, year)
    window_start = max(0, pos - NEAR)
    window_end = pos + len(phrase) + NEAR
    while window_start > 0 and not line[window_start - 1].isspace():
        window_start -= 1
    while window_end < len(line) and not line[window_end - 1].isspace():
        window_end += 1
    window = line[window_start:window_end]
    for pattern in (_DATE_ISO, _DATE_DM, _DATE_MD, _DATE_NUM):
        for found in pattern.finditer(window):
            if _LABELLED.search(window[:found.start()]):
                continue
            got = find_date(window[found.start():found.end()], year)
            if got:
                return got
    return None


def date_after(line: str, label, year: int) -> str | None:
    """The date that follows a label (ETA / ETD) on the line - 'ETA
    Singapore 13 Oct' - not the first date the line happens to carry."""
    found = label.search(line)
    if not found:
        return None
    return find_date(line[found.end():found.end() + 40], year)


def _lines(mail) -> list:
    from . import mail_archive
    top = mail_archive.top_part(getattr(mail, "body_excerpt", "") or "")
    return [" ".join(line.split()) for line in top.splitlines() if line.strip()]


def _stamp_of(mail) -> str:
    stamp = (getattr(mail, "received_at", "") or getattr(mail, "received_on", "") or "")[:16]
    return stamp if len(stamp) == 16 else (stamp[:10] + " 00:00" if stamp else now_stamp())


def _first_line(lines) -> str:
    for line in lines:
        if len(line) >= 3 and not re.match(r"^(hi|hello|dear|good (morning|afternoon|evening))\b", line, re.I):
            return line[:200]
    return lines[0][:200] if lines else ""


#: An obligation or a future form right before a milestone phrase makes it
#: a plan, not an event ('has to be picked up before Thursday', 5c).
_PLAN = re.compile(
    r"\b(?:has|have|had|needs?|need|must|will|would|should|shall|can|could|may|might|is|are|was|were|be|"
    r"going)(?:\s+\w+){0,2}?\s+(?:to\s+)?(?:be\s+)?(?:\w+\s+)?$|\bto\s+be\s+(?:\w+\s+)?$", re.I)


def _planned(low: str, phrase: str) -> bool:
    pos = low.find(phrase)
    if pos < 0:
        return False
    sentence = re.split(r"[.;!?]", low[:pos])[-1]
    before = sentence[-40:]
    return bool(re.search(r"\b(?:has to be|have to be|had to be|needs? to be|need to be|must be|will be|would be|"
                          r"should be|shall be|can be|could be|may be|might be|is going to be|are going to be|"
                          r"going to be|is to be|are to be|to be)\s*$", before))


#: Intent, the future or a request in the clause of a milestone phrase
#: makes it a plan (5e, 2026-09-25): 'we arranged for the crates to be
#: picked up this afternoon', 'we must get the pallets picked up asap', 'the
#: booking confirmation follows once the carrier replies', 'the boxes are
#: picked up by next Tuesday'. Past or present perfect wording or a
#: confirmation with none of these is an event ('the vessel has departed',
#: 'booking confirmed', 'was picked up this morning').
_PLAN_WORDS = re.compile(
    r"\b(?:will|won't|shall|going to|gonna|needs? to|has to|have to|had to|must|should|to be|"
    r"arrang(?:ed|ing|e) to|arrangements?|plan|plans|planned|planning|scheduled? for|scheduled|"
    r"expect|expected|expecting|pending|once|asap|please|pls|kindly|can you|could you|would you|"
    r"would like|let (?:me|us) know|awaiting|waiting for|to get|to have)\b|'ll\b", re.I)
_BY_WHEN = re.compile(
    r"\bby\s+(?:the\s+)?(?:end\s+of|eod|eow|tomorrow|next|mon|tue|wed|thu|fri|sat|sun|"
    r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|\d{1,2}(?:st|nd|rd|th)?\b|\d{1,2}/)", re.I)
#: A clause ends at . ; ! ? , : and at 'and' / 'but' - 'Crates departed on
#: 10/2, ETA expected 10/9' and 'The pallets were picked up today and
#: will be delivered on Thursday' are events: the plan sits in the next
#: clause.
_CLAUSE = re.compile(r"[.;!?,:]|\s(?:and|but)\s")
#: Done wording right before the milestone wins over any plan word in the
#: clause: 'Kindly note the pallets have just been picked up', 'was picked
#: up this morning', 'has departed'. ('have arranged to have them picked up'
#: is not: the auxiliary belongs to 'arranged'.)
_DONE_AUX = r"\b(?:has|have|had|was|were|been|got|is now|are now)\s+(?:just\s+|already\s+|now\s+|finally\s+)?(?:been\s+)?$"


def _clause_of(low: str, phrase: str) -> str:
    pos = low.find(phrase)
    if pos < 0:
        return low
    start = max((m.end() for m in _CLAUSE.finditer(low, 0, pos)), default=0)
    after = _CLAUSE.search(low, pos + len(phrase))
    return low[start:after.start() if after else len(low)]


def is_planned(low: str, phrase: str) -> bool:
    """Whether the milestone phrase in this line is a plan (see _PLAN_WORDS);
    the 5c rule ('has to be picked up') is part of it."""
    if _planned(low, phrase):
        return True
    clause = _clause_of(low, phrase)
    pos = clause.find(phrase)
    if pos > 0 and re.search(_DONE_AUX, clause[:pos]):
        return False
    return bool(_PLAN_WORDS.search(clause) or _BY_WHEN.search(clause))


#: Words after 'arrived at' that are not a place name.
_NOT_PLACES = {"CFS", "WAREHOUSE", "PORT", "TERMINAL", "DESTINATION", "AIRPORT", "DOOR", "HUB", "THE",
               "OUR", "YOUR", "THEIR", "FINAL", "CONSIGNEE", "CUSTOMER", "DEPOT", "RAMP", "YARD", "ETA"}
_PLACE_AFTER = re.compile(r"^\s*(?:at|in|into|to)?\s*(?:the\s+)?"
                          r"([A-Z]{3,5}|[A-Z][a-z]+(?:[ -][A-Z][a-z]+)*)\b")


def named_place(text_after: str) -> str:
    """The place named right after an arrival phrase ('arrived at MIA past
    the cut-off' -> 'MIA', 'ETA Singapore 18 Sep' -> 'Singapore'); ''
    when none is named."""
    found = _PLACE_AFTER.match(text_after or "")
    if not found:
        return ""
    name = found.group(1).strip()
    return "" if name.upper() in _NOT_PLACES or name.upper()[:3] in {m.upper() for m in _MONTHS} else name


def _is_destination(name: str, dest) -> bool:
    """No destination known, or no place named: as before (Arrived). A
    named place counts when it is one of the destination's names."""
    if not name or not dest:
        return True
    key = name.casefold()
    for d in dest:
        d = (d or "").casefold()
        if d and (key == d or key in d.split() or d in key or key in d):
            return True
    return False


def destination_names(conn, enquiry_id: int) -> list:
    """What the case's destination is called: the location key, its master
    name and aliases, the country, the shipment title's last part."""
    row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (enquiry_id,)).fetchone()
    if row is None:
        return []
    names = [row["dest_loc"], row["dest_country"]]
    keys = row.keys()
    loc_id = row["dest_loc_id"] if "dest_loc_id" in keys else None
    if loc_id and db._has_table(conn, "master_location"):
        loc = conn.execute("SELECT key, name, iata FROM master_location WHERE id = ?", (loc_id,)).fetchone()
        if loc is not None:
            names += [loc["key"], loc["name"], loc["iata"]]
        if db._has_table(conn, "master_location_alias"):
            names += [r[0] for r in conn.execute(
                "SELECT alias FROM master_location_alias WHERE location_id = ?", (loc_id,))]
    ship = for_enquiry(conn, enquiry_id)
    if ship is not None and " to " in (ship["title"] or ""):
        names.append(ship["title"].rsplit(" to ", 1)[1])
    out = []
    for n in names:
        n = (n or "").strip()
        if n and n not in out:
            out.append(n)
            out.extend(p.strip() for p in n.split(",") if p.strip() and p.strip() not in out)
    return out


def _has(phrase: str, low: str) -> bool:
    if phrase == "sob":
        return re.search(r"\bSOB\b", low.upper()) is not None
    return re.search(r"(?<![a-z])" + re.escape(phrase) + r"(?![a-z])", low) is not None


def detect(mail, ship=None, mode: str | None = None, today=None, dest=None,
           plans: bool = False) -> Proposal | None:
    """A milestone, an issue or a note read off one mail's own words, or
    None. `ship` (a shipment row) gives the mode and the current ETA;
    `mode` alone does when there is no shipment yet. `dest` (the case's
    destination names, destination_names()) keeps an arrival at another
    place - a gateway, a transit port - from reading as Arrived (5e).

    A milestone is done or planned (5e). A done one comes back as a
    milestone. A planned one comes back as a milestone with planned=True
    when `plans` is set (the Start shipment scan); otherwise, as before, as
    a note when the line carries a date ('Picked up planned Sep 8') and
    not at all without one."""
    mode = mode or (ship["mode"] if ship is not None else "ocean")
    lines = _lines(mail)
    if not lines:
        return None
    stamp = _stamp_of(mail)
    mail_date = stamp[:10]
    year = int(mail_date[:4])
    current_eta = (ship["eta"] or ship["planned_arrival"] or ship["planned_delivery"]) if ship is not None else None
    base = dict(detail=_first_line(lines), message_id=getattr(mail, "message_id", "") or "",
                sender=getattr(mail, "sender_name", "") or getattr(mail, "sender_address", "") or "")

    def made(kind, name, line, phrase, when=None, eta=None, dated=False):
        if kind == "milestone" and mode == "sea_air" and name == "Departed":
            name = "Departed by sea"                     # SA1: the words read as before
        found = date_near(line, phrase, year) if when is None else None
        at = when or (found + " " + (_time_in(line) or stamp[11:16]) if found else stamp)
        return Proposal(kind=kind, name=name, at=at, quote=line[:200],
                        confidence="high" if (found or dated or (when and when != stamp)) else "medium",
                        eta=eta, phrase=phrase, **base)

    for line in lines:
        low = line.lower()
        if line.rstrip().endswith("?"):
            continue                                     # a question states nothing
        # a DN asked for is a request, not an invoice
        for phrase in NOTE_PHRASES[0]:
            if _has(phrase, low):
                return made("note", NOTE_PHRASES[1], line, phrase, when=stamp)
        # ETA / ETD with dates: arrival, departure, or the ETA moved
        if _ETA.search(line):
            found = date_after(line, _ETA, year)
            eta_at = _ETA.search(line)
            if found and found <= mail_date and _is_destination(named_place(line[eta_at.end():]), dest):
                return made("milestone", "Arrived", line, "ETA", when=found + " " + stamp[11:16])
            if found and current_eta and found > current_eta:
                return made("issue", "ETA moved to {}".format(_day(found)), line, "ETA", when=stamp, eta=found,
                            dated=True)
        if _ETD.search(line):
            found = date_after(line, _ETD, year)
            if found and found <= mail_date:
                return made("milestone", "Departed", line, "ETD", when=found + " " + stamp[11:16])
        for phrases, title in ISSUE_PHRASES:
            for phrase in phrases:
                if _has(phrase, low):
                    return made("issue", title, line, phrase)
        for kind, name, phrases, modes in PHRASES:
            if modes and mode not in modes:
                continue
            for phrase in phrases:
                if _has(phrase, low) or (name == "Invoiced" and _DN_WORD.search(line)):
                    if name == "Arrived":
                        # arrived somewhere else (a gateway, a transit port) is not Arrived
                        pos = low.find(phrase)
                        if not _is_destination(named_place(line[pos + len(phrase):]), dest):
                            break
                    if kind == "milestone" and is_planned(low, phrase):
                        if plans:
                            got = made(kind, name, line, phrase)
                            got.planned = True
                            return got
                        # a plan, not an event: a note when it carries a date, else nothing
                        found = find_date(line, year)
                        if found:
                            return made("note", "{} planned {}".format(name, _day(found)), line, phrase, when=stamp)
                        break
                    return made(kind, name, line, phrase)
    return None


def _time_in(line: str) -> str | None:
    found = _TIME.search(line)
    if not found:
        return None
    hour, minute = int(found.group(1)), int(found.group(2))
    if 0 <= hour < 24 and 0 <= minute < 60:
        return "{:02d}:{:02d}".format(hour, minute)
    return None


# ------------------------------------------------------------ proposals ---
SUGGESTION_KIND = {"milestone": "shipment_milestone", "issue": "shipment_issue", "note": "shipment_note"}


def suggestion_values(proposal: Proposal, mail, source: str = "scan") -> dict:
    """The suggestion row a proposal becomes (the mail-suggestion columns reused)."""
    return {
        "kind": SUGGESTION_KIND[proposal.kind], "proposed_status": proposal.name,
        "proposed_date": proposal.at, "proposed_eta": proposal.eta, "note": proposal.detail or None,
        "evidence_sender": proposal.sender, "evidence_date": _stamp_of(mail),
        "evidence_quote": proposal.quote[:400], "confidence": proposal.confidence,
        "message_id": proposal.message_id or None, "entry_id": getattr(mail, "entry_id", "") or None,
        "source": source,
    }


def _last_stamp(conn, shipment_id: int) -> str:
    row = conn.execute("SELECT MAX(at) FROM shipment_event WHERE shipment_id = ? AND voided = 0",
                       (shipment_id,)).fetchone()
    # no event yet: nothing to be newer than, so every mail of the threads
    # is read (a shipment is usually started after the booking mail)
    return row[0] if row and row[0] else ""


# ------------------------------------------------ start shipment (5d) ---
#: A 'Start shipment' suggestion (2026-09-25) is a shipment_milestone row
#: with no shipment yet: accepting it starts the shipment the way the
#: button does and logs that milestone. No new kind, so no schema change.
def is_start(row) -> bool:
    return row["kind"] == "shipment_milestone" and not row["shipment_id"]


#: A Start shipment row on a planned milestone carries it in its
#: proposed_status as 'Picked up, planned' (5e): Accept then starts the
#: shipment and logs nothing. No schema change.
PLANNED = ", planned"


def start_label(row) -> str:
    """'Start shipment · Picked up, planned · Aug 17' / 'Start shipment ·
    Departed · Sep 4' (the date month first, dates.month_day)."""
    from .dates import month_day
    when = month_day(row["proposed_date"])
    return "Start shipment · {}{}".format(row["proposed_status"], " · " + when if when else "")


def start_milestones(mode: str) -> tuple:
    """What moving cargo looks like for a mode: every milestone but
    Invoiced (an invoice says nothing about the cargo)."""
    return tuple(m for m in MILESTONES.get(mode, MILESTONES["ocean"]) if m != "Invoiced")


def _sent_on(mail) -> str:
    return ((getattr(mail, "sent_at", "") or "") or _stamp_of(mail))[:10]


def won_on(case) -> str:
    return (case["closed_on"] or case["closed_on_any"] or "")[:10]


def _has_routes(conn) -> bool:
    return db._has_table(conn, "enquiry_route")


def start_candidates(conn) -> list:
    """Won, the home office's (handled_by empty), not legacy, no shipment: the cases
    whose cargo may be moving with nothing on the Shipments page. v33: one
    row per won route without a shipment, the route's id as route_id and
    its lane and mode over the case's."""
    if not _has_routes(conn):
        return conn.execute(
            "SELECT e.*, NULL AS route_id FROM v_enquiry e WHERE e.status = 'Won' AND e.legacy = 0 "
            "AND e.handled_by IS NULL AND NOT EXISTS (SELECT 1 FROM shipment s WHERE s.enquiry_id = e.id) "
            "ORDER BY CAST(substr(e.opp_no, 5) AS INTEGER)").fetchall()
    out = []
    for row in conn.execute(
            "SELECT e.*, r.id AS route_id, r.seq AS route_seq, r.service_mode AS route_mode "
            "FROM v_enquiry e JOIN enquiry_route r ON r.enquiry_id = e.id AND r.voided = 0 AND r.result = 'Won' "
            "WHERE e.status = 'Won' AND e.legacy = 0 AND e.handled_by IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM shipment s WHERE s.route_id = r.id) "
            "ORDER BY CAST(substr(e.opp_no, 5) AS INTEGER), r.seq").fetchall():
        case = dict(row)
        if case["route_mode"]:
            case["service_mode"] = case["route_mode"]
        out.append(case)
    return out


def start_asked(conn, enquiry_id: int, route_id: int | None = None):
    """The Start shipment suggestion of the case (v33: of the route),
    whatever its state: one each, and a dismissed one keeps it from being
    asked again."""
    if route_id and "route_id" in {r[1] for r in conn.execute("PRAGMA table_info(suggestion)")}:
        return conn.execute(
            "SELECT * FROM suggestion WHERE enquiry_id = ? AND kind = 'shipment_milestone' AND shipment_id IS NULL "
            "AND (route_id = ? OR route_id IS NULL) ORDER BY id LIMIT 1", (enquiry_id, route_id)).fetchone()
    return conn.execute(
        "SELECT * FROM suggestion WHERE enquiry_id = ? AND kind = 'shipment_milestone' "
        "AND shipment_id IS NULL ORDER BY id LIMIT 1", (enquiry_id,)).fetchone()


def first_milestone_mail(conn, search, case, today=None):
    """(mail, Proposal) for the earliest mail of the case's threads, sent on
    or after the Won date, that detect() reads as a cargo milestone; None
    when there is none."""
    from . import case_mail
    won = won_on(case)
    if not won:
        return None
    mode = mode_for(case["service_mode"])
    allowed = start_milestones(mode)
    dest = destination_names(conn, case["id"])
    mails = sorted(case_mail.case_mails(conn, search, case), key=lambda m: (_sent_on(m), _stamp_of(m)))
    for mail in mails:
        if _sent_on(mail) < won:
            continue
        # a plan still shows the shipment is starting (5e)
        proposal = detect(mail, mode=mode, today=today, dest=dest, plans=True)
        if proposal is not None and proposal.kind == "milestone" and proposal.name in allowed:
            return mail, proposal
    return None


def propose_starts(conn, search, repo=None, today=None) -> dict:
    """The Start shipment half of the daily scan: {cases, no_mail, asked,
    proposals: [(case, won_on, mail, Proposal)], written}. Writes only
    with a repo (the caller commits or rolls back)."""
    from . import suggestions
    out = {"cases": 0, "no_mail": 0, "asked": 0, "no_date": 0, "proposals": [], "written": 0}
    for case in start_candidates(conn):
        out["cases"] += 1
        if start_asked(conn, case["id"], case.get("route_id") if hasattr(case, "get") else None) is not None:
            out["asked"] += 1
            continue
        if not won_on(case):
            out["no_date"] += 1
            continue
        found = first_milestone_mail(conn, search, case, today)
        if found is None:
            out["no_mail"] += 1
            continue
        mail, proposal = found
        out["proposals"].append((case, won_on(case), mail, proposal))
        if repo is not None:
            values = suggestion_values(proposal, mail, source="scan")
            if proposal.planned:
                values["proposed_status"] = proposal.name + PLANNED
            values["note"] = "Start shipment: {}".format(proposal.detail) if proposal.detail else "Start shipment"
            if hasattr(case, "get") and case.get("route_id"):
                values["route_id"] = case["route_id"]
            if suggestions.record(repo, case["id"], case["opp_no"], values):
                out["written"] += 1
    return out


def accept_start(repo, row, milestone=None, at=None, note=None, planned=None) -> str:
    """Start the shipment as the button does (same prefill and title) and
    log the milestone the mail showed, source mail, its message id. E1:
    Edit can change the milestone, the date and time, the note and
    whether it is planned (then nothing is logged)."""
    conn = repo.conn
    case = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (row["enquiry_id"],)).fetchone()
    if case is None:
        raise ValueError("that case is gone")
    route_id = row["route_id"] if "route_id" in row.keys() else None
    route = route_of(conn, case["id"], route_id) if _has_routes(conn) else None
    existing = for_route(conn, route["id"]) if route is not None else for_enquiry(conn, case["id"])
    shipment_id = existing["id"] if existing is not None else start(
        repo, case["id"], button_title(conn, route if route is not None else case),
        route["id"] if route is not None else None)
    name = milestone or (row["proposed_status"] or "").replace(PLANNED, "")
    if planned is None:
        planned = (row["proposed_status"] or "").endswith(PLANNED)
    if planned:
        # a plan shows the shipment starting, not a milestone that happened
        repo.update("suggestion", row["id"], {"shipment_id": shipment_id})
        return "{}: shipment started ({}{} - no milestone logged)".format(case["opp_no"], name, PLANNED)
    at = at or row["proposed_date"] or now_stamp()
    if len(at) == 10:
        at += " 00:00"
    log_milestone(repo, shipment_id, name, at, row["note"] if note is None else note, row["proposed_eta"],
                  source="mail", message_id=row["message_id"], sender=row["evidence_sender"])
    # the suggestion now belongs to the shipment: its mail is not proposed
    # to it a second time
    repo.update("suggestion", row["id"], {"shipment_id": shipment_id})
    return "{}: shipment started, {} logged on {}".format(case["opp_no"], name, at)


def propose(conn, search, dry_run: bool = True, today=None) -> dict:
    """The daily scan (5b): for every active shipment, the archive mails of
    its case's threads newer than the shipment's last event, one pending
    suggestion per mail at most, never the same mail twice. Returns
    {shipments, mails, proposals: [(shipment_id, title, Proposal)], written}
    and, since 5d, starts: propose_starts()'s report for the Won cases with
    no shipment."""
    from . import case_mail, suggestions
    repo = db.Repository(conn, source="scan")
    out = {"shipments": 0, "mails": 0, "proposals": [], "written": 0, "skipped": 0}
    for ship in conn.execute("SELECT * FROM shipment WHERE closed = 0 ORDER BY id").fetchall():
        state = summary(conn, ship["id"], today)
        if not state["active"]:
            continue
        out["shipments"] += 1
        case = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (ship["enquiry_id"],)).fetchone()
        if case is None:
            continue
        since = _last_stamp(conn, ship["id"])
        dest = destination_names(conn, case["id"])
        for mail in case_mail.case_mails(conn, search, case):
            stamp = _stamp_of(mail)
            if since and stamp <= since:
                continue
            out["mails"] += 1
            message_id = (getattr(mail, "message_id", "") or "").strip()
            if message_id and conn.execute(
                    "SELECT 1 FROM suggestion WHERE shipment_id = ? AND message_id = ?",
                    (ship["id"], message_id)).fetchone():
                out["skipped"] += 1
                continue
            # done milestones only: a plan is at most a dated note (5e)
            proposal = detect(mail, ship, today=today, dest=dest)
            if proposal is None:
                continue
            out["proposals"].append((ship["id"], ship["title"], proposal))
            if not dry_run:
                values = suggestion_values(proposal, mail, source="scan")
                values["shipment_id"] = ship["id"]
                if suggestions.record(repo, ship["enquiry_id"], case["opp_no"], values):
                    out["written"] += 1
    out["starts"] = propose_starts(conn, search, None if dry_run else repo, today)
    if dry_run:
        conn.rollback()
    else:
        repo.commit()
    return out


def accept_suggestion(repo, row, milestone=None, at=None, note=None) -> str:
    """Write the event a shipment suggestion describes, source mail. E1:
    Edit can change the milestone (an issue's or a note's title), the date
    and time and the note."""
    kind = row["kind"]
    shipment_id = row["shipment_id"]
    at = at or row["proposed_date"] or now_stamp()
    if len(at) == 10:
        at += " 00:00"
    name = milestone or row["proposed_status"]
    detail = row["note"] if note is None else note
    common = dict(source="mail", message_id=row["message_id"], sender=row["evidence_sender"])
    if kind == "shipment_milestone":
        log_milestone(repo, shipment_id, name, at, detail, row["proposed_eta"], **common)
        return "shipment: {} logged on {}".format(name, at)
    if kind == "shipment_issue":
        log_issue(repo, shipment_id, name, at, detail, **common)
        if row["proposed_eta"]:
            repo.update("shipment", shipment_id, {"eta": row["proposed_eta"]})
        return "shipment: issue '{}' logged on {}".format(name, at)
    add_note(repo, shipment_id, name, at, detail, **common)
    return "shipment: note '{}' added on {}".format(name, at)


# ------------------------------------------------------------ the map -----
#: The two maps (5c): pre-projected land paths in the static folder, and
#: the projection each was drawn with, fitted to the sample points in the
#: files (residuals under half a pixel). World: Natural Earth 1; US: Albers
#: (d3.geoAlbers defaults: parallels 29.5 / 45.5, rotate 96, centre -0.6 / 38.7).
STATIC = Path(__file__).resolve().parent / "web" / "static"
WORLD_MAP = STATIC / "worldmap.json"
US_MAP = STATIC / "usmap.json"
_WORLD_FIT = (116.2299, 319.944, 233.639)          # scale, tx, ty
#: The world file's land runs y 73 to 347 of 420 (no Antarctica): the card
#: shows that band only.
WORLD_VIEWBOX = "0 68 640 284"
_US_FIT = (672.009, 309.599, 220.664)


def project_world(lat: float, lon: float) -> tuple:
    lam, phi = math.radians(lon), math.radians(lat)
    p2 = phi * phi
    p4 = p2 * p2
    x = lam * (0.8707 - 0.131979 * p2 + p4 * (-0.013791 + p4 * (0.003971 * p2 - 0.001529 * p4)))
    y = phi * (1.007226 + p2 * (0.015085 + p4 * (-0.044475 + 0.028874 * p2 - 0.005916 * p4)))
    scale, tx, ty = _WORLD_FIT
    return round(tx + scale * x, 1), round(ty - scale * y, 1)


def project_us(lat: float, lon: float) -> tuple:
    p0, p1 = math.radians(29.5), math.radians(45.5)
    n = (math.sin(p0) + math.sin(p1)) / 2
    c = math.cos(p0) ** 2 + 2 * n * math.sin(p0)
    rho0 = math.sqrt(c - 2 * n * math.sin(math.radians(38.7))) / n
    rho = math.sqrt(c - 2 * n * math.sin(math.radians(lat))) / n
    theta = n * math.radians(lon + 96.6)
    x, y = rho * math.sin(theta), rho0 - rho * math.cos(theta)
    scale, tx, ty = _US_FIT
    return round(tx + scale * x, 1), round(ty - scale * y, 1)


def load_map(which: str) -> dict:
    with (US_MAP if which == "us" else WORLD_MAP).open(encoding="utf-8") as handle:
        return json.load(handle)


def _location_by_name(conn, text: str):
    """A via point by master_location key, name or alias, case-insensitive."""
    wanted = (text or "").strip()
    if not wanted:
        return None
    row = conn.execute(
        "SELECT id, key, name, country, lat, lon FROM master_location WHERE status <> 'retired' "
        "AND (upper(key) = upper(?) OR lower(name) = lower(?)) ORDER BY id LIMIT 1", (wanted, wanted)).fetchone()
    if row is None and db._has_table(conn, "master_location_alias"):
        row = conn.execute(
            "SELECT l.id, l.key, l.name, l.country, l.lat, l.lon FROM master_location_alias a "
            "JOIN master_location l ON l.id = a.location_id WHERE upper(a.alias) = upper(?) LIMIT 1",
            (wanted,)).fetchone()
    return row


def via_points(text) -> list:
    return [p.strip() for p in (text or "").split(";") if p.strip()]


def route_points(conn, ship, case) -> list:
    """[{role, text, name, location_id, lat, lon, country}] - origin, the
    via points, destination. A point without coordinates has lat None."""
    def place(role, loc_id, text, country):
        row = conn.execute("SELECT id, key, name, country, lat, lon FROM master_location WHERE id = ?",
                           (loc_id,)).fetchone() if loc_id else None
        return {"role": role, "text": text or (row["name"] if row else "") or "",
                "name": (row["name"] or row["key"]) if row else (text or "?"),
                "location_id": row["id"] if row else None,
                "lat": row["lat"] if row else None, "lon": row["lon"] if row else None,
                "country": (row["country"] if row else None) or country}
    points = [place("origin", case["origin_loc_id"], case["origin_loc"], case["origin_country"])]
    for text in via_points(ship["via_points"]):
        row = _location_by_name(conn, text)
        points.append({"role": "via", "text": text, "name": (row["name"] or row["key"]) if row else text,
                       "location_id": row["id"] if row else None,
                       "lat": row["lat"] if row else None, "lon": row["lon"] if row else None,
                       "country": row["country"] if row else None})
    points.append(place("destination", case["dest_loc_id"], case["dest_loc"], case["dest_country"]))
    return points


def marker_index(state: dict, points: list) -> int | None:
    """Where the last confirmed milestone puts the marker: Picked up ->
    origin, Departed -> the departure port (first via) or origin, Arrived
    -> the destination port (last via) or destination, Delivered / POD /
    Invoiced -> destination. Booked / Dispatched: none."""
    current = state["current"]
    last = len(points) - 1
    if current in ("Picked up",):
        return 0
    if current in ("Departed", "Departed by sea"):
        return 1 if last > 1 else 0
    if current in ("At hub", "Departed by air", "At border", "Crossed"):
        return last // 2 if last > 1 else 0          # SA1: somewhere between the ends
    if current == "Arrived":
        return last - 1 if last > 1 else last
    if current in ("Delivered", "POD", "Invoiced"):
        return last
    return None


def map_kind(points: list, mode: str) -> str:
    countries = {p["country"] for p in points if p["role"] != "via"}
    return "us" if mode in TRUCK_KINDS and countries and countries <= {"USA", "United States of America"} else "world"


def end_labels(state: dict) -> tuple:
    """(origin label, destination label): ETS / ETA with the relevant date."""
    ship = state["ship"]
    at = state["milestone_at"]
    if state["mode"] in TRUCK_KINDS:
        origin = "picked up {}".format(_day(at["Picked up"])) if "Picked up" in at else \
            ("pickup {}".format(_day(ship["planned_pickup"])) if ship["planned_pickup"] else "")
        dest = "delivered {}".format(_day(at["Delivered"])) if "Delivered" in at else \
            ("due {}".format(_day(ship["planned_delivery"])) if ship["planned_delivery"] else "")
        return origin, dest
    departed = at.get("Departed") or at.get("Departed by sea")
    origin = "ETS {}".format(_day(departed or ship["planned_departure"])) \
        if (departed or ship["planned_departure"]) else ""
    dest = "ETA {}".format(_day(ship["eta"] or ship["planned_arrival"])) if (ship["eta"] or ship["planned_arrival"]) else ""
    if "Arrived" in at:
        dest = "arrived {}".format(_day(at["Arrived"]))
    return origin, dest


def route_map(conn, ship, case, state) -> dict:
    """Everything the Route card needs: kind (world / us / strip), the
    points, the marker, the SVG (or the strip), and which locations still
    need coordinates."""
    points = route_points(conn, ship, case)
    kind = map_kind(points, state["mode"])
    missing = [p for p in points if p["lat"] is None or p["lon"] is None]
    marker = marker_index(state, points)
    labels = end_labels(state)
    if missing:
        return {"kind": "strip", "points": points, "marker": marker, "missing": missing,
                "svg": strip_svg(points, marker, labels), "labels": labels}
    return {"kind": kind, "points": points, "marker": marker, "missing": [],
            "svg": map_svg(points, marker, labels, kind), "labels": labels}


def map_svg(points, marker, labels, kind="world", amap=None) -> str:
    amap = amap or load_map(kind)
    project = project_us if kind == "us" else project_world
    esc = html.escape
    viewbox = WORLD_VIEWBOX if kind == "world" else "0 0 {} {}".format(amap["width"], amap["height"])
    out = ['<svg viewBox="{}" width="100%" class="wmap">'.format(viewbox)]
    for feature in amap["land"]:
        out.append('<path d="{}" fill="#e6eaef" stroke="#fff" stroke-width="0.6"/>'.format(feature["d"]))
    xy = [project(p["lat"], p["lon"]) for p in points]
    path = " ".join("{}{},{}".format("M" if i == 0 else "L", x, y) for i, (x, y) in enumerate(xy))
    out.append('<path d="{}" fill="none" stroke="#00b3ea" stroke-width="2" stroke-dasharray="5 4" stroke-linejoin="round"/>'.format(path))
    for i, (p, (x, y)) in enumerate(zip(points, xy)):
        is_end = p["role"] != "via"
        colour = "#e8456c" if i == marker else ("#6a7178" if is_end else "#a9b0b8")
        out.append('<circle cx="{}" cy="{}" r="{}" fill="{}" stroke="#fff" stroke-width="2"><title>{}</title></circle>'
                   .format(x, y, 5 if is_end else 3.5, colour, esc(p["name"])))
        if i == marker:
            out.append('<circle cx="{}" cy="{}" r="12" fill="#e8456c" fill-opacity=".16"/>'.format(x, y))
        if is_end:
            label = p["name"] + ("  " + labels[0 if i == 0 else 1] if labels[0 if i == 0 else 1] else "")
            anchor = "end" if i == 0 and len(points) > 1 and xy[-1][0] > x else "start"
            dx = -10 if anchor == "end" else 10
            out.append('<text x="{}" y="{}" text-anchor="{}" class="mlab">{}</text>'.format(
                round(x + dx, 1), round(y - 10 if i == 0 else y + 18, 1), anchor, esc(label)))
    out.append("</svg>")
    return "".join(out)


def strip_svg(points, marker, labels) -> str:
    """The fallback when a point has no coordinates: origin -> via -> destination on one line."""
    esc = html.escape
    n = len(points)
    width, height = 640, 90
    out = ['<svg viewBox="0 0 {} {}" width="100%" class="wmap strip">'.format(width, height)]
    xs = [60 + i * (width - 120) / max(n - 1, 1) for i in range(n)]
    out.append('<line x1="{}" y1="40" x2="{}" y2="40" stroke="#00b3ea" stroke-width="2" stroke-dasharray="5 4"/>'.format(xs[0], xs[-1]))
    for i, (p, x) in enumerate(zip(points, xs)):
        is_end = p["role"] != "via"
        colour = "#e8456c" if i == marker else ("#6a7178" if is_end else "#a9b0b8")
        out.append('<circle cx="{}" cy="40" r="{}" fill="{}" stroke="#fff" stroke-width="2"/>'.format(round(x, 1), 6 if is_end else 4, colour))
        text = p["name"] + ("" if p["lat"] is not None else " (no coordinates)")
        if is_end:
            label = labels[0 if i == 0 else 1]
            if label:
                text += "  " + label
        out.append('<text x="{}" y="{}" text-anchor="middle" class="mlab">{}</text>'.format(round(x, 1), 66 if i % 2 == 0 else 24, esc(text)))
    out.append("</svg>")
    return "".join(out)


# ------------------------------------------------------- carrier links ----
#: Tracking pages by carrier name fragment (the booking / container number
#: goes in), and airlines by AWB prefix. The app never fetches these; the
#: References card only offers the link, which opens in a new tab.
CARRIER_LINKS = (
    ("zim", "ZIM", "https://www.zim.com/tools/track-a-shipment?consnumber={ref}"),
    ("maersk", "Maersk", "https://www.maersk.com/tracking/{ref}"),
    ("hapag", "Hapag-Lloyd", "https://www.hapag-lloyd.com/en/online-business/track/track-by-booking-solution.html?blno={ref}"),
    ("msc", "MSC", "https://www.msc.com/en/track-a-shipment?trackingNumber={ref}"),
    ("cma", "CMA CGM", "https://www.cma-cgm.com/ebusiness/tracking/search?SearchBy=BL&Reference={ref}"),
    ("ocean network", "ONE", "https://ecomm.one-line.com/one-ecom/manage-shipment/cargo-tracking?ctrackNo={ref}"),
    ("one line", "ONE", "https://ecomm.one-line.com/one-ecom/manage-shipment/cargo-tracking?ctrackNo={ref}"),
    ("evergreen", "Evergreen", "https://www.shipmentlink.com/servlet/TDB1_CargoTracking.do?BL={ref}"),
    ("cosco", "COSCO", "https://elines.coscoshipping.com/ebusiness/cargoTracking?trackingType=BOOKING&number={ref}"),
    ("yang ming", "Yang Ming", "https://www.yangming.com/e-service/Track_Trace/track_trace_cargo_tracking.aspx?str={ref}"),
    ("hmm", "HMM", "https://www.hmm21.com/e-service/general/trackNTrace/TrackNTrace.do?number={ref}"),
)
AWB_LINKS = {
    "016": ("United Cargo", "https://www.unitedcargo.com/en/us/track/track-shipment.html?awb=016-{ref}"),
    "001": ("American Airlines Cargo", "https://www.aacargo.com/track/?awb=001-{ref}"),
    "006": ("Delta Cargo", "https://www.deltacargo.com/Cargo/tracking?awb=006-{ref}"),
    "020": ("Lufthansa Cargo", "https://lufthansa-cargo.com/eservices/etracking?awb=020-{ref}"),
    "057": ("Air France Cargo", "https://www.afklcargo.com/mycargo/shipment/detail/057-{ref}"),
    "074": ("KLM Cargo", "https://www.afklcargo.com/mycargo/shipment/detail/074-{ref}"),
    "160": ("Cathay Cargo", "https://www.cathaycargo.com/en-us/track-and-trace.html?awb=160-{ref}"),
    "180": ("Korean Air Cargo", "https://cargo.koreanair.com/en/tracking?awb=180-{ref}"),
    "205": ("EVA Air Cargo", "https://www.brcargo.com/EN/Tracking?awb=205-{ref}"),
    "297": ("China Airlines Cargo", "https://cargo.china-airlines.com/CCNetv2/content/manage/ShipmentTracking.aspx?awb=297-{ref}"),
    "618": ("Singapore Airlines Cargo", "https://www.siacargo.com/ccn/ShipmentTrack.aspx?awb=618-{ref}"),
    "176": ("Emirates SkyCargo", "https://www.skycargo.com/track-shipment/?awb=176-{ref}"),
    "157": ("Qatar Airways Cargo", "https://www.qrcargo.com/s/track-shipment?awb=157-{ref}"),
    "235": ("Turkish Cargo", "https://www.turkishcargo.com/en/online-services/shipment-tracking?awb=235-{ref}"),
}
_AWB = re.compile(r"^\s*(\d{3})\s*-?\s*(\d{7,8})\s*$")


def carrier_links(ship) -> list:
    """[(label, url, reference field)] for the References card: the ocean
    carrier's page with the booking or MBL number, the airline's with the
    AWB. Nothing when the carrier is not in the table."""
    out = []
    carrier = (ship["carrier"] or "").casefold().strip()
    if carrier:
        for fragment, label, url in CARRIER_LINKS:
            # 'ONE' only as the whole name: 'One Planet Logistics' is not the line
            if re.search(r"(?<![a-z])" + re.escape(fragment) + r"(?![a-z])", carrier) or \
                    (label == "ONE" and carrier == "one"):
                for field_name in ("booking_no", "mbl"):
                    ref = (ship[field_name] or "").strip()
                    if ref:
                        out.append(("Track at {}".format(label), url.format(ref=html.escape(ref)), field_name))
                        break
                break
    awb = (ship["awb"] or "").strip()
    found = _AWB.match(awb)
    if found and found.group(1) in AWB_LINKS:
        label, url = AWB_LINKS[found.group(1)]
        out.append(("Track at {}".format(label), url.format(ref=found.group(2)), "awb"))
    return out

"""The Enquiries list's search box (widened 2026-09-16).

`q` used to look at seven columns of the case itself. It now also reaches
what hangs off the case - the vendors asked (their reference, notes, gist,
the vendor's names), the timeline notes, the free-text fields, the
requester's other names and its contacts - each through an EXISTS so a case
with three vendors is still one row.

A reference is compared loosely: spaces and hyphens are dropped on both
sides, so 'USVM 20417', 'USVM-20417' and 'USVM20417' are the same thing.
Everything else is an ordinary case-insensitive LIKE.

When a row is on the list only because of one of the new places, the page
says which ('USVM20417 · Vanta Multimodal their ref'), so the reader knows
why a case that mentions nothing of the sort turned up.

W3 (2026-11-11): the case's origin and destination country, and every live
route's places and countries, are places too ('Lithuania · destination
country', 'GDANSK · route 2 origin').
"""

from __future__ import annotations

import json
import re

#: The case's own columns, as before.
CORE = ("opp_no", "requester_key", "end_customer", "commodity", "origin_loc",
        "dest_loc", "source_subject")
#: Free-text fields on the enquiry row, with the label the hit line uses.
TEXT_FIELDS = (("remarks", "remarks"), ("system_notes", "system notes"),
               ("origin_alt", "origin"), ("dest_alt", "destination"),
               ("qty_raw", "quantity"), ("awarded_note", "rate note"),
               ("container_type", "container"))
#: The case's shipments' references (13e), with the label the hit line
#: uses; compared loosely like their_ref.
SHIPMENT_REFS = (("awb", "AWB"), ("mbl", "MBL"), ("hbl", "HBL"), ("booking_no", "Booking"),
                 ("bol", "BOL"), ("our_file", "Our file"), ("agent_ref", "Agent ref"),
                 ("load_no", "Load"), ("customer_ref", "Customer ref"))
#: W3: the case's countries, with the label the hit line uses
COUNTRIES = (("origin_country", "origin country"), ("dest_country", "destination country"))
#: W3: a live route's places and countries, with the label the hit line uses
ROUTE_PLACES = (("origin_loc", "origin"), ("origin_alt", "origin"), ("origin_country", "origin country"),
                ("dest_loc", "destination"), ("dest_alt", "destination"), ("dest_country", "destination country"))
SNIPPET = 60


def _loose_sql(column: str) -> str:
    return "replace(replace(coalesce({}, ''), ' ', ''), '-', '')".format(column)


def loose(text: str) -> str:
    """'USVM 20417' -> 'USVM20417': the reference with spaces and hyphens gone."""
    return re.sub(r"[\s\-]+", "", text or "")


def clause(query: str, alias: str = "e") -> tuple:
    """(sql, args): the WHERE fragment for one search string. `alias` is
    the v_enquiry alias in the outer query."""
    like = "%{}%".format(query)
    ref = "%{}%".format(loose(query))
    core = " OR ".join("{}.{} LIKE ?".format(alias, column) for column in CORE)
    parts = [
        "({})".format(core),
        # the vendors asked, live rows only
        "EXISTS (SELECT 1 FROM vendor_request v JOIN party vp ON vp.id = v.vendor_party_id "
        "WHERE v.enquiry_id = {a}.id AND v.voided = 0 AND ("
        "replace(replace(coalesce(v.their_ref, ''), ' ', ''), '-', '') LIKE ? "
        "OR v.notes LIKE ? OR v.gist LIKE ? "
        "OR vp.key LIKE ? OR vp.full_name LIKE ? OR vp.aliases LIKE ?))".format(a=alias),
        # timeline notes, live rows only
        "EXISTS (SELECT 1 FROM status_event s WHERE s.enquiry_id = {a}.id "
        "AND s.voided = 0 AND s.note LIKE ?)".format(a=alias),
        # the case's own free text
        "EXISTS (SELECT 1 FROM enquiry x WHERE x.id = {a}.id AND ({fields}))".format(
            a=alias, fields=" OR ".join("x.{} LIKE ?".format(f) for f, _ in TEXT_FIELDS)),
        # the requester's other names and its people
        "EXISTS (SELECT 1 FROM party rp WHERE rp.id = {a}.requester_party_id "
        "AND (rp.full_name LIKE ? OR rp.aliases LIKE ?))".format(a=alias),
        "EXISTS (SELECT 1 FROM contact c WHERE c.party_id = {a}.requester_party_id "
        "AND (c.name LIKE ? OR c.email LIKE ?))".format(a=alias),
        # the case's shipments: AWB, MBL, booking and the other references (13e)
        "EXISTS (SELECT 1 FROM shipment sh WHERE sh.enquiry_id = {a}.id AND ({refs}))".format(
            a=alias, refs=" OR ".join("{} LIKE ?".format(_loose_sql("sh." + f)) for f, _ in SHIPMENT_REFS)),
        # W3: the case's countries and its live routes' places and countries
        "({})".format(" OR ".join("{}.{} LIKE ?".format(alias, f) for f, _ in COUNTRIES)),
        "EXISTS (SELECT 1 FROM enquiry_route r WHERE r.enquiry_id = {a}.id AND r.voided = 0 AND ({places}))".format(
            a=alias, places=" OR ".join("r.{} LIKE ?".format(f) for f, _ in ROUTE_PLACES)),
    ]
    args = ([like] * len(CORE)
            + [ref, like, like, like, like, like]
            + [like]
            + [like] * len(TEXT_FIELDS)
            + [like, like]
            + [like, like]
            + [ref] * len(SHIPMENT_REFS)
            + [like] * len(COUNTRIES)
            + [like] * len(ROUTE_PLACES))
    return "(" + " OR ".join(parts) + ")", args


def core_clause(query: str, alias: str = "e") -> tuple:
    """(sql, args) for the seven old columns alone: 1 when the row would
    have been found before the widening, so no hit line is needed."""
    like = "%{}%".format(query)
    return ("(" + " OR ".join("{}.{} LIKE ?".format(alias, c) for c in CORE) + ")",
            [like] * len(CORE))


def _snip(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= SNIPPET else text[:SNIPPET].rstrip() + "..."


def _has(text, query) -> bool:
    return bool(text) and query.casefold() in str(text).casefold()


def explain(conn, enquiry_id: int, requester_party_id, query: str) -> str:
    """Why a row that matches none of the core columns is on the list: the
    first of the new places that holds the search string, as one short
    line. '' when nothing is found (the caller then shows nothing)."""
    q = query.strip()
    if not q:
        return ""
    wanted = loose(q).casefold()
    # W3: the case's countries, then its live routes' places
    case = conn.execute("SELECT {} FROM v_enquiry WHERE id = ?".format(
        ", ".join(f for f, _ in COUNTRIES)), (enquiry_id,)).fetchone()
    for field, label in COUNTRIES:
        if case is not None and _has(case[field], q):
            return "{} · {}".format(case[field], label)
    for r in conn.execute("SELECT seq, {} FROM enquiry_route WHERE enquiry_id = ? AND voided = 0 ORDER BY seq".format(
            ", ".join(f for f, _ in ROUTE_PLACES)), (enquiry_id,)):
        for field, label in ROUTE_PLACES:
            if _has(r[field], q):
                return "{} · route {} {}".format(r[field], r["seq"], label)
    if wanted:
        for sh in conn.execute("SELECT {} FROM shipment WHERE enquiry_id = ? ORDER BY id".format(
                ", ".join(f for f, _ in SHIPMENT_REFS)), (enquiry_id,)):
            for field, label in SHIPMENT_REFS:
                if sh[field] and wanted in loose(sh[field]).casefold():
                    return "{} {} · shipment".format(label, sh[field])
    for v in conn.execute(
            "SELECT v.their_ref, v.notes, v.gist, vp.key, vp.full_name, vp.aliases "
            "FROM vendor_request v JOIN party vp ON vp.id = v.vendor_party_id "
            "WHERE v.enquiry_id = ? AND v.voided = 0 ORDER BY v.asked_on, v.id", (enquiry_id,)):
        if v["their_ref"] and wanted and wanted in loose(v["their_ref"]).casefold():
            return "{} · {} their ref".format(v["their_ref"], v["key"])
        if _has(v["key"], q) or _has(v["full_name"], q) or _has(v["aliases"], q):
            return "{} · vendor asked".format(v["key"])
        for text in (v["gist"], v["notes"]):
            if _has(text, q):
                return "{} · vendor note: {}".format(v["key"], _snip(text))
    for s in conn.execute(
            "SELECT note FROM status_event WHERE enquiry_id = ? AND voided = 0 "
            "AND note LIKE ? ORDER BY at, id LIMIT 1", (enquiry_id, "%{}%".format(q))):
        return "note: {}".format(_snip(s["note"]))
    row = conn.execute("SELECT {} FROM enquiry WHERE id = ?".format(
        ", ".join(f for f, _ in TEXT_FIELDS)), (enquiry_id,)).fetchone()
    if row is not None:
        for field, label in TEXT_FIELDS:
            if _has(row[field], q):
                return "{}: {}".format(label, _snip(row[field]))
    if requester_party_id:
        rp = conn.execute("SELECT key, full_name, aliases FROM party WHERE id = ?",
                          (requester_party_id,)).fetchone()
        if rp is not None and _has(rp["full_name"], q):
            return "requester also known as: {}".format(rp["full_name"])
        if rp is not None and _has(rp["aliases"], q):
            try:
                aliases = json.loads(rp["aliases"] or "[]")
            except ValueError:
                aliases = [rp["aliases"]]
            hit = next((a for a in aliases if _has(a, q)), rp["aliases"])
            return "requester also known as: {}".format(_snip(str(hit)))
        for c in conn.execute(
                "SELECT name, email FROM contact WHERE party_id = ? AND (name LIKE ? OR email LIKE ?) "
                "ORDER BY is_primary DESC, id LIMIT 1",
                (requester_party_id, "%{}%".format(q), "%{}%".format(q))):
            return "contact: {}".format(" ".join(p for p in (c["name"], c["email"]) if p))
    return ""

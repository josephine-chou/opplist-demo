"""Consistency check.

It lists, it never fixes. Some of these findings are real mistakes and some
are just work that has not been done yet - a case that came in with no
location, a reason nobody recorded. Guessing which is which and writing a
value would destroy the difference, so every finding goes on screen with its
rows and waits for a person.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field


@dataclass
class Finding:
    key: str
    title: str
    count: int
    rows: list = field(default_factory=list)
    note: str = ""
    #: An expected backlog rather than something broken.
    informational: bool = False

    @property
    def clean(self) -> bool:
        return self.count == 0


def _rows(conn, sql, args=()) -> list:
    return [dict(r) for r in conn.execute(sql, args)]


# ------------------------------------------------------------------ 9.1 --
def unknown_locations(conn) -> Finding:
    from . import db

    if db.master_active(conn):
        # Text with no id behind it: written before the master layer, or a
        # spelling nothing resolved. The id is the place; the text follows it.
        rows = _rows(conn, """
            SELECT opp_no, origin_loc, dest_loc FROM enquiry
            WHERE (origin_loc IS NOT NULL AND origin_loc_id IS NULL)
               OR (dest_loc IS NOT NULL AND dest_loc_id IS NULL)
            ORDER BY opp_no""")
        return Finding("unknown_locations",
                       "Cases whose location text has no master_location behind it",
                       len(rows), rows,
                       "Pick the place on the case page; the text follows the id.")
    rows = _rows(conn, """
        SELECT opp_no, origin_loc, dest_loc FROM enquiry
        WHERE (origin_loc IS NOT NULL
               AND origin_loc NOT IN (SELECT key FROM location))
           OR (dest_loc IS NOT NULL
               AND dest_loc NOT IN (SELECT key FROM location))
        ORDER BY opp_no""")
    return Finding("unknown_locations",
                   "Cases pointing at a location that does not exist",
                   len(rows), rows,
                   "The foreign key should stop this; listed in case it ever slips.")


# ------------------------------------------------------------------ 9.2 --
def malformed_keys(conn) -> Finding:
    rows = _rows(conn, """
        SELECT 'location' AS table_name, key AS value FROM location
        WHERE key <> upper(trim(key))
        UNION ALL
        SELECT 'party', key FROM party WHERE key <> trim(key)
        ORDER BY table_name, value""")
    return Finding("malformed_keys",
                   "Keys that are not stored in their normalised form",
                   len(rows), rows,
                   "location.key must be upper case and trimmed; party.key trimmed.")


# ------------------------------------------------------------------ 9.3 --
def duplicate_parties(conn) -> Finding:
    rows = _rows(conn, """
        SELECT lower(trim(key)) AS folded, COUNT(*) AS n,
               group_concat(key, ' | ') AS keys
        FROM party GROUP BY folded HAVING n > 1 ORDER BY folded""")
    return Finding("duplicate_parties", "Parties that differ only by case",
                   len(rows), rows)


# ------------------------------------------------------------------ 9.4 --
def stale_open_cases(conn, days: int = 90, today=None) -> Finding:
    cutoff = (dt.date.fromisoformat(today or dt.date.today().isoformat())
              - dt.timedelta(days=days)).isoformat()
    rows = _rows(conn, """
        SELECT opp_no, requester_key, status, last_event_on
        FROM v_enquiry
        WHERE status IN ('New','Quoted') AND legacy = 0
          AND COALESCE(last_event_on, received_on, '0000-00-00') < ?
        ORDER BY last_event_on, opp_no""", (cutoff,))
    return Finding("stale_open_cases",
                   "Open cases with no event for over {} days".format(days),
                   len(rows), rows, "Nothing has happened since " + cutoff)


# ------------------------------------------------------------------ 9.5 --
def won_without_award(conn) -> Finding:
    rows = _rows(conn, """
        SELECT opp_no, requester_key, vendor_count FROM v_enquiry
        WHERE status = 'Won' AND rate_used IS NULL
          AND vendor_count > 0
        ORDER BY opp_no""")
    return Finding("won_without_award",
                   "Won cases with vendors asked but no record of whose rate won",
                   len(rows), rows)


def closed_without_reason(conn) -> list:
    """Two findings, because they are two different jobs.

    The live cases are worth chasing: somebody knows why they were lost. The
    legacy ones are the previous holder's records with unknown outcomes and
    will most likely stay blank forever, so they are counted apart rather than
    burying the handful that matter.
    """
    live = _rows(conn, """
        SELECT opp_no, requester_key, status, closed_on_any FROM v_enquiry
        WHERE status IN ('Lost','Cancelled','Dropped') AND legacy = 0
          AND (closed_reason IS NULL OR trim(closed_reason) = '')
        ORDER BY closed_on_any DESC, opp_no""")
    legacy = _rows(conn, """
        SELECT opp_no, requester_key, status FROM v_enquiry
        WHERE status IN ('Lost','Cancelled','Dropped') AND legacy = 1
          AND (closed_reason IS NULL OR trim(closed_reason) = '')
        ORDER BY opp_no""")
    return [
        Finding("closed_without_reason",
                "Lost / Cancelled / Dropped cases with no reason recorded",
                len(live), live,
                "Blank means nobody recorded one. Nothing is filled in "
                "automatically; set the reason on the case page."),
        Finding("closed_without_reason_legacy",
                "...of those, inherited legacy records", len(legacy), legacy,
                "Pre-hire rows with unknown outcomes. Expected to stay blank.",
                informational=True),
    ]


# ------------------------------------------------------------------ 9.6 --
def unconfirmed_status(conn) -> Finding:
    rows = _rows(conn, """
        SELECT opp_no, requester_key, received_on FROM v_enquiry
        WHERE status_confirmed = 0 ORDER BY received_on DESC, opp_no""")
    return Finding("unconfirmed_status",
                   "Cases whose status was never confirmed", len(rows), rows,
                   "Migrated with a blank Status; tracking rule R4 asks about these.",
                   informational=True)


# ------------------------------------------------------------------ 9.7 --
def to_confirm_locations(conn) -> Finding:
    from . import db

    if db.master_active(conn):
        # a location whose note starts 'DECIDE:' was put in front of a person
        # on purpose (HAMILTON, 2026-09-24): its cases come first
        rows = _rows(conn, """
            SELECT e.opp_no, e.origin_loc, e.dest_loc FROM v_enquiry e
            LEFT JOIN master_location o ON o.id = e.origin_loc_id
            LEFT JOIN master_location d ON d.id = e.dest_loc_id
            WHERE o.status = 'to_confirm' OR d.status = 'to_confirm'
            ORDER BY (COALESCE(o.note, '') LIKE 'DECIDE:%' OR COALESCE(d.note, '') LIKE 'DECIDE:%') DESC, e.opp_no""")
    else:
        rows = _rows(conn, """
            SELECT opp_no, origin_loc, dest_loc FROM v_enquiry
            WHERE origin_loc IN (SELECT key FROM location WHERE status = 'To Confirm')
               OR dest_loc IN (SELECT key FROM location WHERE status = 'To Confirm')
            ORDER BY opp_no""")
    return Finding("to_confirm_locations",
                   "Cases using a location still marked 'To Confirm'",
                   len(rows), rows)



# ----------------------------------------------------------------- 9.7b --
def gateways_without_parent(conn) -> Finding:
    """RD1: an airport, seaport or ramp serves a market city, its parent.
    Listed until she sets the parent or ticks 'No parent city' with a note;
    a retired row is not asked."""
    from . import db

    if not db.master_active(conn):
        return Finding("gateways_without_parent", "Gateways without a parent city", 0)
    rows = _rows(conn, """
        SELECT key, type, country, status FROM master_location
        WHERE type IN ('airport', 'seaport', 'ramp') AND status IN ('active', 'to_confirm')
          AND parent_id IS NULL AND no_parent = 0
        ORDER BY country, key""")
    return Finding("gateways_without_parent",
                   "Gateways without a parent city",
                   len(rows), rows,
                   "Set the parent city on the Locations page, or tick 'No parent city' and say why in the note.")


def unused_intake_locations(conn) -> Finding:
    """RD1c: a place added To confirm from a mail or the register page that
    nothing points at - no case, route or route place (voided ones count:
    they are history), no shipment's via points, no gateway as its parent.
    What a registration that stopped at the duplicate panel used to leave
    behind (USA, 1 Oct). Listed with the action that created it; she
    retires them on the Locations page."""
    from . import db

    if not db.master_active(conn):
        return Finding("unused_intake_locations", "To confirm places from intake that nothing uses", 0)
    used = set()
    for sql in ("SELECT origin_loc_id FROM enquiry", "SELECT dest_loc_id FROM enquiry",
                "SELECT origin_loc_id FROM enquiry_route", "SELECT dest_loc_id FROM enquiry_route",
                "SELECT location_id FROM enquiry_route_place", "SELECT parent_id FROM master_location"):
        used.update(r[0] for r in conn.execute(sql) if r[0] is not None)
    # the text columns too, for a row written before its id was
    keys = set()
    for sql in ("SELECT origin_loc FROM enquiry", "SELECT dest_loc FROM enquiry",
                "SELECT origin_loc FROM enquiry_route", "SELECT dest_loc FROM enquiry_route"):
        keys.update(str(r[0]).strip().upper() for r in conn.execute(sql) if r[0])
    for r in conn.execute("SELECT via_points FROM shipment WHERE via_points IS NOT NULL"):
        for name in str(r[0]).split(";"):
            found = db.find_master_location(conn, name.strip()) if name.strip() else None
            if found is not None:
                used.add(found["id"])
    rows = []
    for r in conn.execute("""
            SELECT l.id, l.key, l.country, l.created_at, l.note,
                   (SELECT c.action_id FROM change_log c WHERE c.table_name = 'master_location'
                     AND c.row_id = l.id ORDER BY c.id LIMIT 1) AS action_id,
                   (SELECT c.source FROM change_log c WHERE c.table_name = 'master_location'
                     AND c.row_id = l.id ORDER BY c.id LIMIT 1) AS logged_by
            FROM master_location l
            WHERE l.status = 'to_confirm' AND l.source = 'intake'
            ORDER BY l.created_at, l.key"""):
        if r["id"] in used or r["key"] in keys:
            continue
        rows.append({"key": r["key"], "country": r["country"], "created_at": r["created_at"],
                     "action_id": r["action_id"], "logged_by": r["logged_by"], "note": r["note"]})
    return Finding("unused_intake_locations",
                   "To confirm places from intake that nothing uses",
                   len(rows), rows,
                   "Nothing points at these: retire them on the Locations page (or confirm one you want to keep).")

# ------------------------------------------------------------------ 9.8 --
def placeholder_vendors(conn) -> Finding:
    rows = _rows(conn, """
        SELECT e.opp_no, p.key AS vendor FROM vendor_request v
        JOIN party p ON p.id = v.vendor_party_id
        JOIN enquiry e ON e.id = v.enquiry_id
        WHERE p.placeholder = 1 AND v.voided = 0 ORDER BY e.opp_no""")
    return Finding("placeholder_vendors",
                   "Vendor requests pointing at a placeholder party",
                   len(rows), rows)


# ------------------------------------------------------------------ 9.9 --
def vendor_is_requester(conn) -> Finding:
    """A vendor request whose vendor is the case's own requester.

    That is the customer side of the thread recorded as somebody we asked:
    it double-counts outbound work and can never be a quote we used.
    """
    rows = _rows(conn, """
        SELECT e.opp_no, p.key AS party, v.source, v.asked_on
        FROM vendor_request v
        JOIN enquiry e ON e.id = v.enquiry_id
        JOIN party p ON p.id = v.vendor_party_id
        WHERE v.vendor_party_id = e.requester_party_id AND v.voided = 0
        ORDER BY e.opp_no""")
    return Finding("vendor_is_requester",
                   "Vendor requests pointing at the case's own requester",
                   len(rows), rows,
                   "The requester asked us; they are not somebody we asked. "
                   "Strike the row on the case page.")


# ------------------------------------------------------------------ extra --
def placeholder_requesters(conn) -> list:
    """Two findings, because the two placeholders mean different things.

    '(TBC)' means the requester is still being looked for, which is what
    tracking rule R5 chases. 'Unknown' means the search was given up: the case
    stays, the reminder does not.
    """
    searching = _rows(conn, """
        SELECT e.opp_no, p.key AS requester, e.received_on, e.status
        FROM enquiry e JOIN party p ON p.id = e.requester_party_id
        WHERE p.placeholder = 1 AND lower(trim(p.key)) = '(tbc)'
          AND e.legacy = 0
        ORDER BY e.received_on DESC""")
    abandoned = _rows(conn, """
        SELECT p.key AS requester, COUNT(*) AS cases
        FROM enquiry e JOIN party p ON p.id = e.requester_party_id
        WHERE p.placeholder = 1 AND lower(trim(p.key)) <> '(tbc)'
        GROUP BY p.key ORDER BY cases DESC""")
    return [
        Finding("requesters_to_be_confirmed",
                "Cases still parked on '(TBC)'",
                len(searching), searching,
                "Registered before the requester was known. Tracking rule R5 "
                "keeps asking until one is set."),
        Finding("requesters_given_up_on",
                "Cases on a placeholder requester that is not chased",
                sum(r["cases"] for r in abandoned), abandoned,
                "'Unknown' means the search for the requester stopped. R5 "
                "only asks about '(TBC)', so these stay as they are.",
                informational=True),
    ]


def new_cases_off_tracking(conn, today=None) -> Finding:
    """A New case is our turn: each one is on R1, R2 or R9 (2026-09-24)."""
    from . import tracking

    rows = [{"opp_no": r["opp_no"], "requester_key": r["requester_key"],
             "received_on": r["received_on"]}
            for r in tracking.new_cases_off_every_card(conn, today)]
    return Finding("new_cases_off_tracking",
                   "New cases on none of R1, R2, R9 (it is our turn, nothing chases them)",
                   len(rows), rows,
                   "Scope In, confirmed, not held; the rules' waiting days ignored, snoozes counted. "
                   "Every one of these is a rule bug.")


def run(conn: sqlite3.Connection, today=None) -> list:
    findings = [
        unknown_locations(conn),
        malformed_keys(conn),
        duplicate_parties(conn),
        stale_open_cases(conn, today=today),
        new_cases_off_tracking(conn, today=today),
        won_without_award(conn),
    ]
    findings.extend(closed_without_reason(conn))
    findings.extend([
        unconfirmed_status(conn),
        to_confirm_locations(conn),
        gateways_without_parent(conn),
        unused_intake_locations(conn),
        placeholder_vendors(conn),
        vendor_is_requester(conn),
    ])
    findings.extend(placeholder_requesters(conn))
    return findings


def render(findings, limit: int = 10) -> list:
    """Plain text for the CLI. The web page renders the same findings."""
    lines = []
    problems = sum(1 for f in findings if not f.clean and not f.informational)
    for finding in findings:
        mark = "ok  " if finding.clean else ("note" if finding.informational
                                             else "LOOK")
        lines.append("[{}] {:<5d} {}".format(mark, finding.count, finding.title))
        if finding.note:
            lines.append("            {}".format(finding.note))
        for row in finding.rows[:limit]:
            lines.append("            " + ", ".join(
                "{}={}".format(k, v) for k, v in row.items() if v is not None))
        if len(finding.rows) > limit:
            lines.append("            ... and {} more".format(
                len(finding.rows) - limit))
    lines.append("")
    lines.append("{} check(s) worth looking at, {} informational".format(
        problems, sum(1 for f in findings if f.informational and not f.clean)))
    return lines

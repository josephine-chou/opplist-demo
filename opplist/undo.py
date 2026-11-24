"""Strike out undoes the action that wrote the event (batch E2, schema v41).

Every change_log row one user action writes carries the same action_id
(Repository._current_action). Striking out a status event finds the action
that inserted it and takes it back on that case: the other events and
shipment notes it inserted are struck out too, and every field it changed
goes back to the value it had - as long as the field still holds what the
action wrote. A field changed since is left and named. Put back takes the
strike-out back the same way, which writes the action's values again.
Nothing is deleted: the undo and the put back are actions of their own.

Events from before v41 have no action; their Strike out restates the case
from the events that are left (`_fallback`). So do events written by an
import or a migration, and the events of the action that registered the
case: undoing those is not what Strike out is for.
"""

from __future__ import annotations

from . import dates
from . import routes as routes_mod
from . import status as status_mod

#: Event kinds whose Strike out undoes their action. A note, a follow-up or
#: an acknowledgement struck out is only struck out (its suggestion aside).
ACTION_KINDS = ("status", "requote")
#: Rows an undone action inserted that are struck out with it.
STRIKE_ALONG = ("status_event", "shipment_event")
#: Never written back: housekeeping stamps.
SKIP_FIELDS = ("updated_at", "created_at")
#: Written back when they still hold the action's value, never named when
#: they do not: the status follows the events anyway.
SILENT = {("enquiry", "status"), ("enquiry", "status_confirmed"), ("enquiry", "quoted")}
#: An action from these sources is not undone as a whole.
FALLBACK_SOURCES = ("import", "migration")

_CASE_SQL = {
    "status_event": "SELECT enquiry_id FROM status_event WHERE id = ?",
    "vendor_request": "SELECT enquiry_id FROM vendor_request WHERE id = ?",
    "enquiry_route": "SELECT enquiry_id FROM enquiry_route WHERE id = ?",
    "shipment": "SELECT enquiry_id FROM shipment WHERE id = ?",
    "suggestion": "SELECT enquiry_id FROM suggestion WHERE id = ?",
    "vendor_rate": "SELECT enquiry_id FROM vendor_rate WHERE id = ?",
    "vendor_rate_line": "SELECT r.enquiry_id FROM vendor_rate_line l JOIN vendor_rate r ON r.id = l.rate_id WHERE l.id = ?",
    "shipment_event": "SELECT s.enquiry_id FROM shipment_event e JOIN shipment s ON s.id = e.shipment_id "
                      "WHERE e.id = ?",
}

_ENQUIRY_LABELS = {
    "closed_reason": "The closing reason",
    "rate_used": "Rate used",
    "awarded_vendor_id": "The awarded vendor",
    "awarded_note": "The awarded vendor's note",
    "lost_their_price": "Their price",
    "lost_our_price": "Our price",
    "hold_reason": "The hold",
    "hold_until": "The hold's date",
}


def has_actions(conn) -> bool:
    return "action_id" in {r[1] for r in conn.execute("PRAGMA table_info(change_log)")}


def inserted_by(conn, table: str, row_id: int):
    """(action_id, source) of the action that inserted the row, or None."""
    if not has_actions(conn):
        return None
    row = conn.execute("SELECT action_id, source FROM change_log WHERE table_name = ? AND row_id = ? "
                       "AND field = '(insert)' ORDER BY id DESC LIMIT 1", (table, row_id)).fetchone()
    if row is None or row["action_id"] is None:
        return None
    return row["action_id"], row["source"]


def struck_by(conn, table: str, row_id: int) -> int | None:
    """The action that last struck the row out, or None (before v41)."""
    if not has_actions(conn):
        return None
    row = conn.execute("SELECT action_id FROM change_log WHERE table_name = ? AND row_id = ? AND field = 'voided' "
                       "AND new_value = '1' ORDER BY id DESC LIMIT 1", (table, row_id)).fetchone()
    return row["action_id"] if row is not None else None


def _case_of(conn, table: str, row_id: int, cache: dict):
    key = (table, row_id)
    if key not in cache:
        if table == "enquiry":
            cache[key] = row_id
        elif table in _CASE_SQL:
            row = conn.execute(_CASE_SQL[table], (row_id,)).fetchone()
            cache[key] = row[0] if row is not None else None
        else:
            cache[key] = None
    return cache[key]


def _same(a, b) -> bool:
    return (None if a is None else str(a)) == (None if b is None else str(b))


def _label(conn, table: str, row_id: int, field: str) -> str:
    if table == "enquiry":
        return _ENQUIRY_LABELS.get(field, field.replace("_", " ").capitalize())
    if table == "enquiry_route":
        row = conn.execute("SELECT seq FROM enquiry_route WHERE id = ?", (row_id,)).fetchone()
        name = "Route {}".format(routes_mod.letter(row["seq"]) if row is not None else "?")
        return {"result": name + "'s result", "result_on": name + "'s result date",
                "lost_reason": name + "'s reason"}.get(field, "{} ({})".format(name, field))
    if table == "vendor_request":
        row = conn.execute("SELECT p.key FROM vendor_request v LEFT JOIN party p ON p.id = v.vendor_party_id "
                           "WHERE v.id = ?", (row_id,)).fetchone()
        who = row["key"] if row is not None and row["key"] else "a vendor"
        return "The award to {}".format(who) if field == "awarded" else "The vendor request to {}".format(who)
    if table == "shipment":
        return "The shipment's {}".format("closing" if field in ("closed", "closed_reason") else field)
    if table == "suggestion":
        return "The suggestion"
    if table == "status_event":
        return "The event's {}".format(field)
    if table == "shipment_event":
        return "The shipment event's {}".format(field)
    return "{} {}".format(table, field)


def _revert(repo, rows, case_id: int, skip=()) -> tuple:
    """Take back what `rows` (one action's change_log rows) did on one
    case: every field to the value it had before the action, when it still
    holds the action's last value. Returns (inserted rows of the case,
    lines naming what was left, the suggestion states written)."""
    conn = repo.conn
    cache = {}
    inserts, changes = [], {}
    for r in rows:
        table, row_id, field = r["table_name"], r["row_id"], r["field"]
        if _case_of(conn, table, row_id, cache) != case_id:
            continue
        if field == "(insert)":
            inserts.append((table, row_id))
            continue
        if field in SKIP_FIELDS or (table, row_id, field) in skip:
            continue
        key = (table, row_id, field)
        if key not in changes:
            changes[key] = [r["old_value"], r["new_value"], r["id"]]
        else:
            changes[key][1], changes[key][2] = r["new_value"], r["id"]
    notes, writes, states = [], {}, []
    for (table, row_id, field), (old, new, last) in changes.items():
        if _same(old, new):
            continue
        current = conn.execute("SELECT {} FROM {} WHERE id = ?".format(field, table), (row_id,)).fetchone()
        if current is None:
            continue
        if not _same(current[0], new):
            if (table, field) not in SILENT:
                notes.append(_changed_since(conn, table, row_id, field, last))
            continue
        writes.setdefault((table, row_id), {})[field] = old
        if table == "suggestion" and field == "state":
            states.append(old)
    for (table, row_id), values in writes.items():
        repo.update(table, row_id, values)
        if table in ("enquiry", "enquiry_route", "vendor_request"):
            repo.touch(table, row_id)
    return inserts, list(dict.fromkeys(notes)), states


def _changed_since(conn, table, row_id, field, after_id) -> str:
    later = conn.execute("SELECT at FROM change_log WHERE table_name = ? AND row_id = ? AND field = ? AND id > ? "
                         "ORDER BY id LIMIT 1", (table, row_id, field, after_id)).fetchone()
    when = " on {}".format(dates.month_day(later["at"])) if later is not None and dates.month_day(later["at"]) else ""
    return "{} was changed after this{}; left as it is.".format(_label(conn, table, row_id, field), when)


def _state_lines(states) -> list:
    if "pending" in states:
        return ["The suggestion is back in Suggested updates."]
    if "accepted" in states:
        return ["The suggestion is accepted again."]
    return []


def _action_rows(conn, action_id):
    return conn.execute("SELECT * FROM change_log WHERE action_id = ? ORDER BY id", (action_id,)).fetchall()


def suggestion_back(repo, ref: str) -> list:
    """The accepted suggestion that wrote `ref` ('status_event:12') is
    pending again; change_log keeps what was decided."""
    done = []
    for row in repo.conn.execute("SELECT id FROM suggestion WHERE event_ref = ? AND state = 'accepted'",
                                 (ref,)).fetchall():
        repo.update("suggestion", row["id"], {"state": "pending", "decided_at": None, "decided_as": None,
                                              "decided_values": None, "edited": 0, "event_ref": None})
        done.append("pending")
    return _state_lines(done)


def strike_status_event(repo, event_id: int) -> list:
    """Strike out one event of the case timeline (item 2): a status or
    re-quote event undoes the action that inserted it; one without an action
    restates the case (item 5). The lines to tell her."""
    conn = repo.conn
    ev = conn.execute("SELECT * FROM status_event WHERE id = ?", (event_id,)).fetchone()
    if ev is None:
        raise KeyError("event {} not found".format(event_id))
    if ev["voided"]:
        return []
    case_id = ev["enquiry_id"]
    action = inserted_by(conn, "status_event", event_id) if ev["kind"] in ACTION_KINDS else None
    rows = _action_rows(conn, action[0]) if action else []
    whole = bool(rows) and action[1] not in FALLBACK_SOURCES and not any(
        r["table_name"] == "enquiry" and r["field"] == "(insert)" and r["row_id"] == case_id for r in rows)
    repo.update("status_event", event_id, {"voided": 1})
    if not whole:
        notes = _fallback(repo, ev) if ev["kind"] in ACTION_KINDS else []
        repo._restate(event_id)
        return notes + suggestion_back(repo, "status_event:{}".format(event_id))
    inserts, notes, states = _revert(repo, rows, case_id, skip={("status_event", event_id, "voided")})
    for table, row_id in inserts:
        if table in STRIKE_ALONG and (table, row_id) != ("status_event", event_id):
            row = conn.execute("SELECT voided FROM {} WHERE id = ?".format(table), (row_id,)).fetchone()
            if row is not None and not row["voided"]:
                repo.update(table, row_id, {"voided": 1})
    repo._restate(event_id)
    repo.touch("enquiry", case_id)
    return notes + _state_lines(states) + suggestion_back(repo, "status_event:{}".format(event_id))


def _fallback(repo, ev) -> list:
    """Item 5: an event without an action. The status follows the events
    left; the closing reason is the latest live closing event's, or empty
    on an open case; a route closed by this event (its result and date)
    has no result again; Rate used and the awarded vendor stay."""
    conn = repo.conn
    case_id = ev["enquiry_id"]
    repo._restate(ev["id"])
    case = conn.execute("SELECT status, closed_reason, rate_used, awarded_vendor_id FROM enquiry WHERE id = ?",
                        (case_id,)).fetchone()
    reason = None
    if case["status"] in status_mod.REASON_STATUSES:
        closing = conn.execute(
            "SELECT reason FROM status_event WHERE enquiry_id = ? AND voided = 0 AND kind = 'status' AND status = ? "
            "ORDER BY at DESC, id DESC LIMIT 1", (case_id, case["status"])).fetchone()
        reason = closing["reason"] if closing is not None else case["closed_reason"]
    if not _same(case["closed_reason"] or None, reason or None):
        repo.update("enquiry", case_id, {"closed_reason": reason or None})
    if ev["status"] in routes_mod.RESULTS and ev["kind"] == "status":
        for route in routes_mod.routes_of(conn, case_id):
            if route.get("result") == ev["status"] and (route.get("result_on") or "") == (ev["at"] or ""):
                repo.update("enquiry_route", route["id"], {"result": None, "result_on": None, "lost_reason": None})
                repo.touch("enquiry_route", route["id"])
    repo.touch("enquiry", case_id)
    if ev["status"] == "Won" and ev["kind"] == "status" and (case["rate_used"] or case["awarded_vendor_id"]):
        return ["Rate used and the awarded vendor were left as they are: this event is older than the record "
                "of what each action wrote."]
    return []


def put_back(repo, table: str, row_id: int) -> list:
    """Put back a struck-out row: the action that struck it is taken back
    on its case (the row live again, what that strike-out undid written
    again, a field changed since left and named). Without one (struck out
    before v41) the row is only put back."""
    conn = repo.conn
    action = struck_by(conn, table, row_id)
    case_id = _case_of(conn, table, row_id, {})
    notes, states = [], []
    if action is not None and case_id is not None:
        _, notes, states = _revert(repo, _action_rows(conn, action), case_id)
    row = conn.execute("SELECT voided FROM {} WHERE id = ?".format(table), (row_id,)).fetchone()
    if row is not None and row["voided"]:
        repo.update(table, row_id, {"voided": 0})
    if table == "vendor_request":
        repo.touch(table, row_id)
    return notes + _state_lines(states)


def put_back_status_event(repo, event_id: int) -> list:
    """Item 3: Put back writes the struck action again."""
    if repo.conn.execute("SELECT 1 FROM status_event WHERE id = ?", (event_id,)).fetchone() is None:
        raise KeyError("event {} not found".format(event_id))
    notes = put_back(repo, "status_event", event_id)
    repo._restate(event_id)
    return notes

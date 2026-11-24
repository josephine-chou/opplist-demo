"""Put the closed reasons on the two vocabularies (2026-09-18).

Since the split there are two questions and two lists:

- **Lost** = there was a shipment and we did not get it. The reason says
  why (price, no answer, another forwarder, we could not handle it).
- **Dropped** = this row should not exist. Two reasons only: it duplicates
  another case, or it was registered in error.

The cases on file were closed under the old single list, so three things
need doing, all of them idempotent and none of them deleting anything:

- **move** the two cases closed as Dropped / 'Unable to Handle' to Lost
  with the reason 'Unable to handle', on their original closing date - we
  could not handle the shipment, but there was a shipment;
- **rename** a reason whose wording changed ('Price' -> 'Price too high');
  a duplicate now names the case it duplicates, read out of the event note
  ('Duplicate' -> 'Duplicate of OPP-1237'), or 'Duplicate of (TBC)' when
  the note never said which;
- **list**, and touch nothing, the closed cases whose reason is blank or
  is not in either list: only a person can say what those were.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from . import db, status as status_mod

#: Dropped, but there really was a shipment: these move to Lost. The value
#: is the Lost reason they take with them.
MOVE_TO_LOST = {
    "OPP-1231": "Unable to handle",
    "OPP-1232": "Unable to handle",
}

#: Old wording -> the wording the settings list uses now, matched without
#: regard to case. Duplicates are not here: they need the case number.
RENAME = {
    "Lost": {
        "price": "Price too high",
        "no response": "No response from customer",
        "no response from customer": "No response from customer",
        "customer cancelled": "Shipment cancelled",
        "unable to handle": "Unable to handle",
    },
    "Dropped": {
        "registered in error": "Registered in error",
    },
}

#: What a duplicate says when the note never named the other case. It is a
#: real reason, not a blank: somebody has to go and find the number.
DUPLICATE_TBC = "Duplicate of (TBC)"
_OPP_NUMBER = re.compile(r"OPP-\d{3,}", re.I)


@dataclass
class Report:
    moved: list = field(default_factory=list)
    renamed: list = field(default_factory=list)
    duplicates: list = field(default_factory=list)
    tbc: list = field(default_factory=list)
    already: list = field(default_factory=list)
    no_reason: list = field(default_factory=list)
    lost_no_reason: list = field(default_factory=list)
    odd: list = field(default_factory=list)
    dry_run: bool = True

    def lines(self) -> list:
        out = [
            "moved to Lost       : {}".format(len(self.moved)),
            "reasons reworded    : {}".format(len(self.renamed)),
            "duplicates numbered : {}".format(len(self.duplicates)),
            "already on the list : {}".format(len(self.already)),
        ]
        for label, rows in (("> ", self.moved), ("= ", self.renamed),
                            ("~ ", self.duplicates)):
            out.extend(label + line for line in rows)
        if self.tbc:
            out.append("")
            out.append("a duplicate whose note never said of what - {} row(s), left as "
                       "'{}' for you to fill in:".format(len(self.tbc), DUPLICATE_TBC))
            out.extend("  " + line for line in self.tbc)
        if self.no_reason:
            out.append("")
            out.append("dropped with no reason at all - {} row(s), nothing changed; only "
                       "you can say whether each one is a duplicate, a mis-registration "
                       "or really a Lost case:".format(len(self.no_reason)))
            out.extend("  " + line for line in self.no_reason)
        if self.lost_no_reason:
            out.append("")
            out.append("lost with no reason recorded: {} row(s), nothing changed (most "
                       "came in from the workbook, which had no reason column for "
                       "them)".format(len(self.lost_no_reason)))
        if self.odd:
            out.append("")
            out.append("a reason that is in neither list - {} row(s), nothing "
                       "changed:".format(len(self.odd)))
            out.extend("  " + line for line in self.odd)
        if self.dry_run:
            out.append("")
            out.append("dry run: nothing was written. Run again with --apply.")
        return out


def _closing_event(conn, enquiry_id: int, status: str):
    """The event that closed the case with this status, latest first."""
    return conn.execute(
        "SELECT id, at, approx FROM status_event WHERE enquiry_id = ? "
        "AND voided = 0 AND kind = 'status' AND status = ? "
        "ORDER BY at DESC, id DESC LIMIT 1", (enquiry_id, status)).fetchone()


def _carry_approx(repo, enquiry_id: int, status: str, approx) -> bool:
    """Give the new closing event the same 'how sure is this date' flag the
    old one had. Returns True when something was written."""
    event = _closing_event(repo.conn, enquiry_id, status)
    if event is None or event["approx"] == approx:
        return False
    repo.update("status_event", event["id"], {"approx": approx})
    return True


def _duplicate_of(conn, enquiry_id: int) -> str | None:
    """The case number a duplicate's own history names, newest first."""
    for row in conn.execute(
        "SELECT note FROM status_event WHERE enquiry_id = ? AND voided = 0 "
        "ORDER BY at DESC, id DESC", (enquiry_id,)
    ).fetchall():
        found = _OPP_NUMBER.search(row["note"] or "")
        if found:
            return found.group(0).upper()
    row = conn.execute(
        "SELECT (SELECT opp_no FROM enquiry g WHERE g.id = e.group_id) AS gno "
        "FROM enquiry e WHERE e.id = ?", (enquiry_id,)).fetchone()
    return row["gno"] if row and row["gno"] else None


def _wanted(conn, row) -> tuple:
    """(status, reason, why) for one closed case, or (None, None, why) when
    there is nothing to do or nothing safe to do."""
    opp_no = row["opp_no"]
    reason = (row["closed_reason"] or "").strip()
    if opp_no in MOVE_TO_LOST and row["status"] == "Dropped":
        return "Lost", MOVE_TO_LOST[opp_no], "a shipment we could not handle is Lost"
    if not reason:
        return None, None, "no reason recorded"
    if reason.casefold().startswith("duplicate"):
        if _OPP_NUMBER.search(reason):
            return None, None, "already names the case"
        other = _duplicate_of(conn, row["id"])
        return ("Dropped",
                "Duplicate of {}".format(other) if other else DUPLICATE_TBC,
                "of {}".format(other) if other else "the note never said of what")
    new = RENAME.get(row["status"], {}).get(reason.casefold())
    if new and new != reason:
        return row["status"], new, "reworded"
    if status_mod.reason_belongs(conn, row["status"], reason):
        return None, None, "already on the list"
    return None, None, "not in the {} list".format(row["status"])


def run(conn, dry_run: bool = True) -> Report:
    """Walk every closed case and put its reason on the new lists."""
    report = Report(dry_run=dry_run)
    repo = db.Repository(conn, source="import")
    rows = conn.execute(
        "SELECT id, opp_no, status, closed_reason, legacy FROM enquiry "
        "WHERE status IN ('Lost', 'Dropped') ORDER BY opp_no").fetchall()
    for row in rows:
        opp_no, was = row["opp_no"], (row["closed_reason"] or "").strip()
        # A case moved by an earlier run of this command, before the approx
        # flag was carried over: the old event is still on the timeline, so
        # the two can still be compared and put right.
        if opp_no in MOVE_TO_LOST and row["status"] == "Lost":
            old_event = _closing_event(conn, row["id"], "Dropped")
            new_event = _closing_event(conn, row["id"], "Lost")
            if old_event is not None and new_event is not None \
                    and old_event["approx"] != new_event["approx"]:
                report.renamed.append(
                    "{}: closing date {} marked approximate, as the Dropped "
                    "event it replaced was".format(opp_no, new_event["at"]))
                if not dry_run:
                    _carry_approx(repo, row["id"], "Lost", old_event["approx"])

        status, reason, why = _wanted(conn, row)
        if status is None:
            if why == "no reason recorded":
                # A legacy row is inherited and its outcome is unknown by
                # definition, so it is not a question. A Dropped row with
                # no reason is: Dropped now means 'should not exist', and
                # only a person can say whether that is what happened. A
                # Lost row with no reason is only counted - the workbook
                # had no reason column for most of them.
                if row["legacy"]:
                    pass
                elif row["status"] == "Dropped":
                    report.no_reason.append("{} ({})".format(opp_no, row["status"]))
                else:
                    report.lost_no_reason.append(opp_no)
            elif why.startswith("not in"):
                report.odd.append("{}: '{}' {}".format(opp_no, was, why))
            else:
                report.already.append("{}: {}".format(opp_no, was or row["status"]))
            continue

        if status != row["status"]:
            # The closing date stands: the case was decided then, not today.
            # So does how sure that date is - a migrated event dated by the
            # workbook's Date Received carries approx = 1, and the new event
            # must too, or the export would gain a closing date the original
            # file never had (the migration's rule).
            old_event = _closing_event(conn, row["id"], row["status"])
            at = old_event["at"] if old_event else dt.date.today().isoformat()
            approx = old_event["approx"] if old_event else 0
            line = "{}: {} '{}' -> {} '{}' on {}{} ({})".format(
                opp_no, row["status"], was, status, reason, at,
                " (approximate)" if approx else "", why)
            report.moved.append(line)
            if not dry_run:
                repo.set_status(
                    row["id"], status, at, closed_reason=reason, source="import",
                    note="Reclassified {} -> {}: {} (2026-09-18)".format(
                        row["status"], status, why))
                _carry_approx(repo, row["id"], status, approx)
            continue

        line = "{}: '{}' -> '{}'".format(opp_no, was, reason)
        if reason == DUPLICATE_TBC:
            report.tbc.append(line)
        elif was.casefold().startswith("duplicate"):
            report.duplicates.append(line)
        else:
            report.renamed.append(line)
        if not dry_run:
            repo.update("enquiry", row["id"], {"closed_reason": reason})
            repo.touch("enquiry", row["id"])
    if not dry_run:
        repo.commit()
    return report

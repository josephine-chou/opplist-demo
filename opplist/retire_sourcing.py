"""Sourcing retired (2026-09-24): the one-off that moves the cases still on
Sourcing back to New.

A case is New until a quote goes out, then Quoted. Nothing writes Sourcing
any more (status.OPEN_STATUSES), so the cases that were left on it are moved
here, and the pending suggestions that still propose it are dismissed:

- each case in Sourcing gets one status event, New, dated today, note
  'Sourcing retired, back to New', through Repository.set_status (change_log
  for the enquiry row and the new event). Its vendor requests are not
  touched, so R2 keeps chasing the vendors that have not replied. A hold
  stays on (keep_hold), and a case nobody confirmed stays unconfirmed.
- each pending suggestion proposing Sourcing is dismissed, its note keeping
  what it said and adding why.

History stays: the old Sourcing events are neither edited nor voided, and
the CHECK constraints still accept the value. A case whose latest status
event is dated today or later is left for another day and listed (a New
dated today would lose to a same-day Sourcing, STAGE_RANK). Re-runnable: a
second run finds nothing in Sourcing.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from . import db
from . import status as status_mod

DISMISS_NOTE = "dismissed: Sourcing retired"


@dataclass
class Report:
    dry_run: bool
    cases: list = field(default_factory=list)          # (opp_no, since, vendor summary, open re-quote round)
    moved: list = field(default_factory=list)          # opp_no
    left: list = field(default_factory=list)           # (opp_no, why)
    suggestions: list = field(default_factory=list)    # (id, opp_no, note)
    dismissed: list = field(default_factory=list)      # id

    def lines(self) -> list:
        head = "dry run: nothing written" if self.dry_run else "applied"
        out = [head,
               "cases in Sourcing                  : {}".format(len(self.cases))]
        for opp_no, since, vendors, requote in self.cases:
            out.append("  {}  Sourcing since {}  vendors: {}{}".format(
                opp_no, since or "?", vendors, "  (open re-quote round)" if requote else ""))
        out.append("pending suggestions proposing it  : {}".format(len(self.suggestions)))
        for sid, opp_no, note in self.suggestions:
            out.append("  #{} {}  {}".format(sid, opp_no, note or ""))
        if not self.dry_run:
            out.append("cases moved to New                 : {}".format(len(self.moved)))
            out.append("suggestions dismissed              : {}".format(len(self.dismissed)))
        for opp_no, why in self.left:
            out.append("  left as it is: {} ({})".format(opp_no, why))
        return out


def _vendor_summary(conn, enquiry_id: int) -> str:
    rows = conn.execute(
        "SELECT p.key, v.replied_on, v.voided FROM vendor_request v "
        "JOIN party p ON p.id = v.vendor_party_id WHERE v.enquiry_id = ? "
        "ORDER BY v.asked_on, v.id", (enquiry_id,)).fetchall()
    live = [r for r in rows if not r["voided"]]
    if not live:
        return "none" if not rows else "none live ({} struck out)".format(len(rows))
    return ", ".join("{} ({})".format(r["key"], "replied {}".format(r["replied_on"])
                                       if r["replied_on"] else "no reply") for r in live)


def run(conn, dry_run: bool = True, today: str | None = None) -> Report:
    today = today or dt.date.today().isoformat()
    repo = db.Repository(conn, source="retire-sourcing")
    report = Report(dry_run=dry_run)
    cases = conn.execute(
        "SELECT e.id, e.opp_no, e.status_confirmed, v.in_requote FROM enquiry e "
        "JOIN v_enquiry v ON v.id = e.id WHERE e.status = 'Sourcing' ORDER BY e.opp_no").fetchall()
    for case in cases:
        latest = repo.latest_event(case["id"])
        since = latest["at"] if latest is not None else None
        report.cases.append((case["opp_no"], since, _vendor_summary(conn, case["id"]),
                             bool(case["in_requote"])))
        if since is not None and since >= today:
            report.left.append((case["opp_no"], "its Sourcing event is dated {}; run again "
                                                "after that day".format(since)))
            continue
        if dry_run:
            continue
        repo.set_status(case["id"], "New", today, note=status_mod.SOURCING_RETIRED_NOTE,
                        source="import", keep_hold=True)
        if not case["status_confirmed"]:
            # nobody confirmed the Sourcing either: R4 still asks
            repo.update("enquiry", case["id"], {"status_confirmed": 0})
        report.moved.append(case["opp_no"])

    for row in conn.execute(
            "SELECT id, opp_no, note FROM suggestion WHERE state = 'pending' "
            "AND kind = 'status' AND proposed_status IN ({}) ORDER BY id".format(
                ",".join("'{}'".format(s) for s in status_mod.RETIRED_STATUSES))).fetchall():
        report.suggestions.append((row["id"], row["opp_no"], row["note"]))
        if dry_run:
            continue
        note = "{} | {}".format(row["note"], DISMISS_NOTE) if row["note"] else DISMISS_NOTE
        repo.update("suggestion", row["id"], {
            "state": "dismissed", "note": note,
            "decided_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M")})
        report.dismissed.append(row["id"])
    if not dry_run:
        repo.commit()
    return report

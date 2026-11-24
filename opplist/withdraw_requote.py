"""Re-quote suggestions follow the button (2026-09-25): the one-off that
takes back the pending suggestions of the retired 'they asked for the rate
again' rule.

That rule read the requester's own mail ('please requote', 'still
quoting') and suggested a re-quote. Since v30 Re-quoted records a revised
quote that went out, so accepting one of those would write a quote nobody
sent; the mail itself is R3's Replied step, which already lists the case
with the Re-quoted button. Each pending one is dismissed the way
retire-sourcing withdrew the Sourcing suggestions - state dismissed, its
note keeping what it said and adding why, change_log through the
repository - never deleted. Re-runnable: a second run finds none.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from . import db, propose

WITHDRAWN_NOTE = "withdrawn: 'they asked for the rate again' is R3's Replied step, not a re-quote"


@dataclass
class Report:
    dry_run: bool
    found: list = field(default_factory=list)          # (id, opp_no, proposed_status, evidence_date, sender)
    withdrawn: list = field(default_factory=list)      # id

    def lines(self) -> list:
        out = ["dry run: nothing written" if self.dry_run else "applied",
               "pending 'they asked for the rate again' suggestions : {}".format(len(self.found))]
        for sid, opp_no, status, at, sender in self.found:
            out.append("  #{} {}  {} {}  {}".format(sid, opp_no, status, at or "?", sender or ""))
        if not self.dry_run:
            out.append("withdrawn                                           : {}".format(len(self.withdrawn)))
        return out


def run(conn, dry_run: bool = True) -> Report:
    repo = db.Repository(conn, source="withdraw-requote")
    report = Report(dry_run=dry_run)
    for row in conn.execute(
            "SELECT id, opp_no, proposed_status, evidence_date, evidence_sender, note FROM suggestion "
            "WHERE state = 'pending' AND kind = 'status' AND note LIKE ? ORDER BY id",
            (propose.REQUOTE_NOTE + "%",)).fetchall():
        report.found.append((row["id"], row["opp_no"], row["proposed_status"], row["evidence_date"],
                             row["evidence_sender"]))
        if dry_run:
            continue
        repo.update("suggestion", row["id"], {
            "state": "dismissed", "note": "{} | {}".format(row["note"], WITHDRAWN_NOTE),
            "decided_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M")})
        report.withdrawn.append(row["id"])
    if not dry_run:
        repo.commit()
    return report

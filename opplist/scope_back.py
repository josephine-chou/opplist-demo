"""scope-back (batch SC1, 2026-11-10): put back In what the rules set Out.

Batch 21, DC1 and WA1 set scope Out on direct customers' and other offices'
cases, which hid them from the work pages too. Since SC1 the reports leave
those cases out by their own rule (direct_customers_out) and scope is hers
alone, so the Out the rules set goes back to In:

    the rules'     an Out case whose latest scope change was not hers:
                   source migration (batch 21's one-off, not-latam-out
                   --apply), or Out from registration (no scope change in
                   change_log) while out_reason still gives a reason for it
    possibly hers  Out from registration that out_reason no longer explains
                   (a country or the requester changed since): listed
                   apart, left alone
    hers           her latest scope change was by hand: listed apart, left
                   Out

The cases still open from before October are marked: they come back to
Tracking, and the New ones to the Assign List, so she can close or keep
them. The dry run writes nothing; --apply sets the rules' cases In through
the repository (change_log source migration, one action; the command takes
the restore point first). The team site picks them up on its next push
(their gone documents are rewritten). Nothing is deleted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import db, direct_customers_out as dco

OPEN = ("New", "Sourcing", "Quoted")
#: the parts of the list, in its order
DC_BEFORE = "direct customers before October"
DC_FROM = "direct customers from October"
OFFICES = "Our offices"
OTHERS = "other requesters"
PARTS = (DC_BEFORE, DC_FROM, OFFICES, OTHERS)


@dataclass
class Item:
    enquiry_id: int
    opp_no: str
    received_on: str
    requester: str
    status: str
    part: str
    why: str            # the reason out_reason gives now, or ''
    set_by: str         # 'migration 2026-11-10 12:08', 'registration', 'ui 2026-10-20 ...'

    @property
    def open_before_october(self) -> bool:
        return self.status in OPEN and (self.received_on or "")[:10] < dco.CUTOFF

    def line(self, back_in: bool = True) -> str:
        mark = ""
        if back_in and self.open_before_october:          # a case left Out stays off Tracking
            mark = "  << open, back on Tracking{}".format(" and the Assign List" if self.status == "New" else "")
        return "  {}  {}  {}  {}  {}{}{}".format(
            self.opp_no, (self.received_on or "(no date)")[:10], self.requester, self.status, self.set_by,
            "  ({})".format(dco.reason_text(self.why)) if self.why else "", mark)


@dataclass
class Report:
    dry_run: bool
    rules: list = field(default_factory=list)       # Items set In (with --apply)
    maybe: list = field(default_factory=list)       # Out from registration, no reason now
    hers: list = field(default_factory=list)        # her own Out

    def open_cases(self) -> list:
        return [i for i in self.rules if i.open_before_october]

    def lines(self) -> list:
        out = ["dry run: nothing written" if self.dry_run else "applied: the cases below are In",
               "{:<52}: {}".format("set Out by the rules, back In" + ("" if not self.dry_run else " with --apply"),
                                   len(self.rules))]
        for part in PARTS:
            mine = [i for i in self.rules if i.part == part]
            if part == OTHERS and not mine:
                continue
            out.append("{:<52}: {}".format("  " + part, len(mine)))
            out.extend("  " + i.line() for i in mine)
        still = self.open_cases()
        by = {}
        for i in still:
            by[i.status] = by.get(i.status, 0) + 1
        out.append("{:<52}: {}{}".format("still open from before October", len(still), "  ({})".format(
            ", ".join("{} {}".format(s, by[s]) for s in OPEN if s in by)) if by else ""))
        out.append("  " + (", ".join(i.opp_no for i in still) or "-"))
        out.append("{:<52}: {}".format("Out from registration, no rule reason now (left Out)", len(self.maybe)))
        out.extend(i.line(back_in=False) for i in self.maybe)
        out.append("{:<52}: {}".format("set Out by hand (left Out)", len(self.hers)))
        out.extend(i.line(back_in=False) for i in self.hers)
        return out


def _part(party_type, received_on) -> str:
    if party_type == dco.OFFICE:
        return OFFICES
    if party_type != "Direct Customer":
        return OTHERS
    return DC_BEFORE if (received_on or "")[:10] < dco.CUTOFF else DC_FROM


def last_scope_change(conn, enquiry_id):
    """The case's latest scope change (old value set), or None."""
    return conn.execute(
        "SELECT at, source FROM change_log WHERE table_name = 'enquiry' AND row_id = ? AND field = 'scope' "
        "AND old_value IS NOT NULL ORDER BY id DESC LIMIT 1", (enquiry_id,)).fetchone()


def find(conn, dry_run=True) -> Report:
    report = Report(dry_run=dry_run)
    rows = conn.execute(
        "SELECT e.id, e.opp_no, e.received_on, e.status, p.key, p.type, {} AS why "
        "FROM v_enquiry e JOIN party p ON p.id = e.requester_party_id WHERE e.scope = 'Out' "
        "ORDER BY COALESCE(e.received_on, ''), CAST(substr(e.opp_no, 5) AS INTEGER)".format(dco.out_sql())).fetchall()
    for r in rows:
        last = last_scope_change(conn, r["id"])
        set_by = "registration" if last is None else "{} {}".format(last["source"], (last["at"] or "")[:16])
        item = Item(r["id"], r["opp_no"], r["received_on"] or "", r["key"] or "", r["status"],
                    _part(r["type"], r["received_on"]), r["why"] or "", set_by)
        if last is not None and last["source"] != "migration":
            report.hers.append(item)
        elif last is None and not item.why:
            report.maybe.append(item)
        else:
            report.rules.append(item)
    return report


def run(conn, dry_run: bool = True) -> Report:
    report = find(conn, dry_run)
    if not dry_run and report.rules:
        repo = db.Repository(conn, source="migration")
        for item in report.rules:
            repo.update("enquiry", item.enquiry_id, {"scope": "In"})
            repo.touch("enquiry", item.enquiry_id)
        repo.commit()
    return report

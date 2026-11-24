"""rekey-threads (batch MF1, TK2): the stored thread keys the subject
rules change. MF1: a leading reference whose removal would leave no letter
stays in the key ('RE: BRN-QT-207' keys as BRNQT207, not 061). TK2
(2026-11-13): one whose removal would leave common words only stays too
('KLX-552018 - IMPORT AIR' keys as KLX552018IMPORTAIR, not IMPORTAIR).

Listed by default; --apply writes them through the repository (one
action, change_log, restore point first in the command). Only a key an
old rule made is changed: a case's thread_key equal to an old key of its
source subject (before MF1, or MF1 without TK2), an attached thread's
(enquiry_thread kind thread_key) value equal to an old key of its note. A
key set any other way is hers and is listed as left. Re-runnable: a
second run finds none.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import db, parsing

SOURCE = "rekey-threads"


def old_keys(subject) -> set:
    """The keys the earlier rules made of a subject: before MF1, and MF1
    without TK2."""
    return {parsing.subject_thread_key(subject, keep_bare_ref=False, keep_common_ref=False),
            parsing.subject_thread_key(subject, keep_bare_ref=True, keep_common_ref=False)}


@dataclass
class Report:
    dry_run: bool
    cases: list = field(default_factory=list)      # (opp_no, subject, old, new)
    threads: list = field(default_factory=list)    # (opp_no, note, old, new)
    left: list = field(default_factory=list)       # (opp_no, what, stored, new, why)

    def lines(self) -> list:
        out = ["dry run: nothing written" if self.dry_run else "applied",
               "cases       : {}".format(len(self.cases))]
        out += ["  {}  {} -> {}   ({})".format(o, old, new, s) for o, s, old, new in self.cases]
        out.append("attached    : {}".format(len(self.threads)))
        out += ["  {}  {} -> {}   ({})".format(o, old, new, n) for o, n, old, new in self.threads]
        if self.left:
            out.append("left as they are: {}".format(len(self.left)))
            out += ["  {}  {} {} (new rule {}): {}".format(*t) for t in self.left]
        if self.dry_run and (self.cases or self.threads):
            out.append("run again with --apply to write them (restore point first)")
        return out


def run(conn, dry_run: bool = True) -> Report:
    repo = db.Repository(conn, source=SOURCE)
    report = Report(dry_run=dry_run)
    for row in conn.execute("SELECT id, opp_no, source_subject, thread_key FROM enquiry "
                            "WHERE COALESCE(source_subject, '') <> '' ORDER BY id").fetchall():
        olds = old_keys(row["source_subject"])
        new = parsing.subject_thread_key(row["source_subject"])
        if olds == {new} or not new or (row["thread_key"] or "") == new:
            continue
        if (row["thread_key"] or "") not in olds:
            report.left.append((row["opp_no"], "case key", row["thread_key"] or "-", new, "not the old rule's key"))
            continue
        report.cases.append((row["opp_no"], row["source_subject"], row["thread_key"], new))
        if not dry_run:
            repo.update("enquiry", row["id"], {"thread_key": new})
            repo.touch("enquiry", row["id"])
    if db._has_table(conn, "enquiry_thread"):
        for row in conn.execute(
                "SELECT t.id, t.value, t.note, e.opp_no FROM enquiry_thread t JOIN enquiry e ON e.id = t.enquiry_id "
                "WHERE t.kind = 'thread_key' AND COALESCE(t.note, '') <> '' ORDER BY t.id").fetchall():
            olds = old_keys(row["note"])
            new = parsing.subject_thread_key(row["note"])
            if olds == {new} or not new or row["value"] == new:
                continue
            if row["value"] not in olds:
                report.left.append((row["opp_no"], "attached key", row["value"], new, "not the old rule's key"))
                continue
            taken = conn.execute("SELECT 1 FROM enquiry_thread WHERE kind = 'thread_key' AND value = ? AND id <> ?",
                                 (new, row["id"])).fetchone()
            if taken:
                report.left.append((row["opp_no"], "attached key", row["value"], new, "another row has the new key"))
                continue
            report.threads.append((row["opp_no"], row["note"], row["value"], new))
            if not dry_run:
                repo.update("enquiry_thread", row["id"], {"value": new})
    if not dry_run:
        repo.commit()
    return report

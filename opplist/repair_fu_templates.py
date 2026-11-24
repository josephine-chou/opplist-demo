"""Put the follow-up templates back on the pool (2026-09-25).

A Settings save on 25 Sep stored the first single-version defaults of 16 Sep
back into fu_template_1 / _2 / _3, with CRLF line breaks, and the reader no
longer recognised them as old defaults. A template equal to an old default
after its line breaks are normalised is set to empty (empty = the pool).
Anything she wrote herself is left alone. Dry run by default; --apply after
a restore point, one change_log row per write (table 'setting').
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import db, followup

KEYS = ("fu_template_1", "fu_template_2", "fu_template_3")


@dataclass
class Report:
    rows: list = field(default_factory=list)       # (key, state, detail)
    changed: int = 0
    applied: bool = False

    def lines(self) -> list:
        out = ["{} fu template(s) {}".format(self.changed, "set to empty" if self.applied else "would be set to empty")]
        for key, state, detail in self.rows:
            out.append("  {:<14} {:<22} {}".format(key, state, detail))
        return out


def run(conn, dry_run: bool = True) -> Report:
    report = Report(applied=not dry_run)
    repo = db.Repository(conn, source="migration")
    for key in KEYS:
        row = conn.execute("SELECT rowid, value FROM setting WHERE key = ?", (key,)).fetchone()
        value = row["value"] if row is not None else None
        text = str(value or "")
        crlf = "CRLF" if "\r" in text else "LF"
        if not followup.normalise(text).strip():
            report.rows.append((key, "empty already", "uses the pool of {} versions".format(
                len(followup.versions(conn, int(key[-1]))))))
            continue
        if not followup.is_old_default(key, text):
            report.rows.append((key, "her own, kept", "{} characters, {}".format(len(text), crlf)))
            continue
        index = [followup.normalise(t).strip() for t in followup.OLD_FU_TEMPLATES[key]].index(
            followup.normalise(text).strip())
        report.rows.append((key, "old default -> empty", "old default #{} ({} characters, {})".format(
            index + 1, len(text), crlf)))
        report.changed += 1
        if not dry_run:
            db.set_setting(conn, key, "")
            repo._log("setting", row["rowid"], key, value, "")
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    return report

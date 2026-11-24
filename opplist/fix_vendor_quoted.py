"""One vendor status, not two (2026-09-25): the one-off that
brings the rows on file in line with the rule the repository now keeps.

`vendor_request.machine_status` (Asked / Acknowledged / Quoted / Declined /
Waiting on us / No reply) and the `quoted` flag said the same thing twice,
and a vendor that quoted was often marked once (OPP-1407: status Quoted,
their ref on the row, quoted = 0, so v_enquiry.vendor_quoted_count said
0). From now on `quoted` is derived: 1 when machine_status is 'Quoted',
0 otherwise, on every insert and update. This run does two things, both
through the repository with change_log, both idempotent:

- quoted = 1 where machine_status = 'Quoted' and quoted = 0;
- machine_status = 'Quoted' where quoted = 1 and machine_status is
  empty or 'Asked' (the flag was the only place the quote was recorded).

Nothing is deleted; voided rows are treated like the rest, so the counts
stay true when a row is put back.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import db


@dataclass
class Report:
    dry_run: bool
    flag_set: int = 0                      # quoted = 1 put on rows whose status is Quoted
    status_set: int = 0                    # machine_status = 'Quoted' put on rows that only had the flag
    rows: list = field(default_factory=list)

    def lines(self) -> list:
        head = "dry run: nothing written" if self.dry_run else "applied"
        return [head,
                "quoted = 1 where machine_status = 'Quoted'      : {}".format(self.flag_set),
                "machine_status = 'Quoted' where only the flag   : {}".format(self.status_set)] + \
               ["  {}".format(r) for r in self.rows]


def run(conn, dry_run: bool = True) -> Report:
    repo = db.Repository(conn, source="fix-vendor-quoted")
    report = Report(dry_run=dry_run)
    for row in conn.execute(
            "SELECT v.id, e.opp_no, p.key FROM vendor_request v "
            "JOIN enquiry e ON e.id = v.enquiry_id JOIN party p ON p.id = v.vendor_party_id "
            "WHERE v.machine_status = 'Quoted' AND v.quoted = 0 ORDER BY v.id").fetchall():
        report.flag_set += 1
        report.rows.append("{} {}: quoted flag set (status Quoted)".format(row["opp_no"], row["key"]))
        if not dry_run:
            repo.update("vendor_request", row["id"], {"quoted": 1})
    for row in conn.execute(
            "SELECT v.id, e.opp_no, p.key, v.machine_status FROM vendor_request v "
            "JOIN enquiry e ON e.id = v.enquiry_id JOIN party p ON p.id = v.vendor_party_id "
            "WHERE v.quoted = 1 AND (v.machine_status IS NULL OR TRIM(v.machine_status) IN ('', 'Asked')) "
            "ORDER BY v.id").fetchall():
        report.status_set += 1
        report.rows.append("{} {}: status '{}' -> Quoted (flag was set)".format(
            row["opp_no"], row["key"], row["machine_status"] or ""))
        if not dry_run:
            repo.update("vendor_request", row["id"], {"machine_status": "Quoted"})
    if not dry_run:
        repo.commit()
    return report

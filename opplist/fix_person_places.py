"""Two rows of the master location list, 2026-09-23 (the person
rule). fix_locations is spent and refuses the live database, so this is
its own small command: dry run by default, --apply after a restore point,
idempotent.

- **TOMAS IBANEZ** is a person - Tolvex's contact - read out of 'responder
  con copia a Tomás Ibáñez' as the destination of a
  lane. The row is retired and its note says why; the case that points at
  it (OPP-1454) is left alone for Iris to change by hand.
- **HAMILTON** (Bermuda) was left to_confirm by the intake and is a real
  place: country code BM, active.
"""

from __future__ import annotations

from . import db
from .fix_locations import Report, _complete, _retire

RETIRE_ROW_ONLY = (
    ("TOMAS IBANEZ", "a person's name - Tolvex's contact, read out of 'responder con copia "
                         "a Tomás Ibáñez' as the destination of a lane "
                         "(the person rule of 2026-09-23)"),
)
COMPLETE = (
    dict(key="HAMILTON", name="Hamilton", type="city", country="Bermuda", country_code="BM",
         status="active", source="manual"),
)


def run(conn, dry_run: bool = True, today: str = "") -> Report:
    if not db.master_active(conn):
        raise ValueError("the master location layer is not active on this database")
    report = Report(dry_run=dry_run)
    repo = db.Repository(conn, source="import")
    for key, why in RETIRE_ROW_ONLY:
        _retire(repo, key, why, report, dry_run, today, touch_cases=False)
    for wanted in COMPLETE:
        _complete(repo, wanted, report, dry_run)
    if not dry_run:
        repo.commit()
    return report

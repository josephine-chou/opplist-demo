"""Shipments switched off (batch FX3, 2026-11-11): she redesigns shipments
before they are used again; until then nothing of them shows or runs.

Setting `shipments` ('1' on, anything else off; off by default), its
switch in Settings. While it is off: the menu's Shipments link and the
Shipments pages (a direct URL says they are switched off), the case page's
shipment card and Start shipment, Tracking's R10, the shipment suggestions
in Suggested updates, the morning run's propose-shipments (one line, done)
and the shipment suggestions the mail scan would make. Nothing is deleted:
the shipments and their events stay, and turning the setting on brings it
all back as it was - except the suggestions switch_off_once() dismissed.
"""

from __future__ import annotations

import csv
import datetime as dt

from . import db, paths

SETTING = "shipments"
#: printed by propose-shipments and recorded with its done run
OFF_LINE = "shipments are switched off"
#: what the switched-off pages say
OFF_PAGE = "Shipments are switched off. Turn them on in Settings to see them again."
#: switch_off_once(): the note on the pending shipment suggestions it
#: dismisses, the folder of the CSV copy (under data\), and its marker
DISMISS_NOTE = "shipments switched off on 11 Nov"
COPY_DIR = "shipments-20261111"
DONE_SETTING = "fx3_shipments_off_done"
TABLES = ("shipment", "shipment_event")


def enabled(conn) -> bool:
    try:
        return str(db.get_setting(conn, SETTING, "0") or "0").strip() == "1"
    except Exception:                         # a half-built database: off
        return False


def copy_tables(conn, folder=None) -> list:
    """A CSV copy of each shipment table (UTF-8 with BOM, a header row), in
    data\\shipments-20261111\\; a file already there is left. The paths
    written."""
    folder = folder or paths.DATA_DIR / COPY_DIR
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for table in TABLES:
        if not db._has_table(conn, table):
            continue
        target = folder / "{}.csv".format(table)
        if target.exists():
            continue
        cur = conn.execute("SELECT * FROM {} ORDER BY id".format(table))
        with open(target, "w", encoding="utf-8-sig", newline="") as fh:
            out = csv.writer(fh)
            out.writerow([c[0] for c in cur.description])
            out.writerows(tuple(row) for row in cur.fetchall())
        written.append(target)
    return written


def switch_off_once(conn) -> list:
    """Once, while the setting is off: the CSV copy, and every pending
    shipment suggestion dismissed through the repository (change_log
    source migration, one action) with DISMISS_NOTE; the decided ones stay.
    The startup lines; [] when it ran before or shipments are on. Commits."""
    from . import suggestions
    if db.get_setting(conn, DONE_SETTING) or enabled(conn):
        return []
    lines = []
    copied = copy_tables(conn)
    if copied:
        counts = ", ".join("{} {}".format(conn.execute("SELECT COUNT(*) FROM {}".format(p.stem)).fetchone()[0],
                                          p.stem) for p in copied)
        lines.append("Shipments copied to {} ({}).".format(copied[0].parent, counts))
    repo = db.Repository(conn, source="migration")
    repo.pin_action()
    marks = ",".join("?" for _ in suggestions.SHIPMENT_KINDS)
    gone = []
    for row in conn.execute("SELECT id, opp_no, note FROM suggestion WHERE state = 'pending' AND kind IN ({}) "
                            "ORDER BY id".format(marks), suggestions.SHIPMENT_KINDS).fetchall():
        suggestions._record(repo, row["id"], "dismissed", None, False, "")
        repo.update("suggestion", row["id"], {
            "note": "{} · {}".format(row["note"], DISMISS_NOTE) if row["note"] else DISMISS_NOTE})
        gone.append(row["opp_no"])
    db.set_setting(conn, DONE_SETTING, dt.date.today().isoformat())
    conn.commit()
    lines.append("Shipments switched off: {} pending shipment suggestion{} dismissed{}.".format(
        len(gone), "" if len(gone) == 1 else "s", " ({})".format(", ".join(gone)) if gone else ""))
    return lines

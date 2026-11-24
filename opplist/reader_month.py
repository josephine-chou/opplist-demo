r"""The reader's monthly check (batch RG2b, layer 3).

The scheduled 08:15 run, on the 1st of a month (or the first run after it,
once), checks the month before: for each field the register reader fills,
the share of the values it read that equal the values saved, and how many
there were. Months from RG2a on are read from register_read (what the
reader put in the form beside what the case was saved with, written at
Register); a month that began before the first register_read row is
measured as reader-check does, on the mail archive. The result is kept with
reader-check's reports (..\docs\reports\reader-month-YYYY-MM.json); the
Settings page shows them as a table, one column a month since September
2026. Nothing is sent anywhere; both databases are only read.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from . import db, logs, paths, reader_check

#: the first month the table shows
FIRST_MONTH = "2026-09"
NAME = "reader-month-{}.json"
#: the table's rows: reader-check's fields, in its order
FIELDS = reader_check.FIELDS
LABELS = reader_check.LABELS
#: register_read's fields as the table's
FROM_REGISTER = {"requester_party_id": "requester", "received_on": "received_on",
                 "origin_country": "origin_country", "origin_loc": "origin_loc", "dest_country": "dest_country",
                 "dest_loc": "dest_loc", "service_mode": "service_mode", "incoterm": "incoterm",
                 "commodity": "commodity"}


def report_dir() -> Path:
    return Path(reader_check.REPORT_DIR)


def _month_before(today: dt.date) -> str:
    first = today.replace(day=1)
    return (first - dt.timedelta(days=1)).strftime("%Y-%m")


def _months(first: str, last: str) -> list:
    out, (y, m) = [], (int(first[:4]), int(first[5:7]))
    while "{:04d}-{:02d}".format(y, m) <= last:
        out.append("{:04d}-{:02d}".format(y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _same(a, b) -> bool:
    return " ".join(str(a or "").split()).casefold() == " ".join(str(b or "").split()).casefold()


def register_start(conn) -> str:
    """The day of the first register_read row ('' when none, or no table)."""
    if not db._has_table(conn, "register_read"):
        return ""
    row = conn.execute("SELECT min(created_at) FROM register_read").fetchone()
    return (row[0] or "")[:10] if row else ""


def from_register(conn, month) -> dict:
    """{field: {'right': n, 'count': n}} from register_read rows written in
    the month: a field the reader filled is right when it equals the value
    saved (without case or extra spaces). Containers: right when both the
    type and the quantity read are the ones saved."""
    out = {f: {"right": 0, "count": 0} for f in FIELDS}
    boxes = {}
    for row in conn.execute("SELECT enquiry_id, field, value_read, value_saved FROM register_read "
                            "WHERE substr(created_at, 1, 7) = ?", (month,)):
        name = FROM_REGISTER.get(row["field"])
        if row["field"] in ("container_type", "container_qty"):
            boxes.setdefault(row["enquiry_id"], []).append(_same(row["value_read"], row["value_saved"]))
            continue
        if name is None:
            continue
        out[name]["count"] += 1
        out[name]["right"] += 1 if _same(row["value_read"], row["value_saved"]) else 0
    for marks in boxes.values():
        out["containers"]["count"] += 1
        out["containers"]["right"] += 1 if all(marks) else 0
    return out


def from_archive(conn, search, month) -> dict:
    """reader-check's verdicts for the month: of the values the reader gave
    (right or wrong; empty ones gave nothing), the right ones."""
    counts = reader_check.counts(reader_check.check(conn, search, [month]))[month]
    return {f: {"right": c["right"], "count": c["right"] + c["wrong"]} for f, c in counts.items()}


def measure(conn, month, search_factory=None) -> dict:
    """One month's result, ready to keep."""
    start = register_start(conn)
    if start and month + "-01" >= start:
        source, fields = "register_read", from_register(conn, month)
    else:
        from . import mail_archive
        factory = search_factory or mail_archive.ArchiveSearch
        with factory() as search:
            source, fields = "reader-check", from_archive(conn, search, month)
    return {"month": month, "source": source, "made_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "fields": fields}


def path_for(month, folder=None) -> Path:
    return Path(folder or report_dir()) / NAME.format(month)


def due(today: dt.date, folder=None) -> list:
    """The months to check now: the month before, and any month since
    FIRST_MONTH not kept yet (each once; a failed one is tried again at the
    next run)."""
    last = _month_before(today)
    if last < FIRST_MONTH:
        return []
    return [m for m in _months(FIRST_MONTH, last) if not path_for(m, folder).exists()]


def run_due(conn, today=None, folder=None, search_factory=None) -> list:
    """The scheduled run's check: every month due, measured and kept. The
    lines for the log; a month that fails is logged and left for the next
    run."""
    today = today or dt.date.today()
    lines = []
    for month in due(today, folder):
        try:
            result = measure(conn, month, search_factory)
        except Exception as exc:
            logs.get("reader-month").exception("reader month %s", month)
            lines.append("reader check for {} failed: {}".format(month, exc))
            continue
        target = path_for(month, folder)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, indent=1, sort_keys=True), encoding="utf-8")
        lines.append("reader check for {} ({}): {}".format(month, result["source"], target.name))
    return lines


def scheduled_check(db_path, today=None) -> list:
    """From main() after a scheduled step: only over the live database (a
    test's or a copy's run checks nothing). Never raises."""
    try:
        if Path(db_path).resolve() != Path(paths.LIVE_DB_PATH).resolve():
            return []
        today = today or dt.date.today()
        if not due(today):
            return []
        conn = db.open_db(db_path)
        try:
            return run_due(conn, today)
        finally:
            conn.close()
    except Exception:
        logs.get("reader-month").exception("reader month")
        return []


def table(folder=None) -> dict:
    """Settings' table: {'months': [...], 'rows': [{'label', 'cells': [{'share', 'count'} | None]}]}."""
    folder = Path(folder or report_dir())
    kept = {}
    for path in sorted(folder.glob(NAME.format("*"))):
        month = path.stem[len("reader-month-"):]
        if month < FIRST_MONTH:
            continue
        try:
            kept[month] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    months = sorted(kept)
    rows = []
    for f in FIELDS:
        cells = []
        for month in months:
            got = (kept[month].get("fields") or {}).get(f) or {}
            count = int(got.get("count") or 0)
            cells.append({"share": round(100.0 * int(got.get("right") or 0) / count), "count": count}
                         if count else None)
        if any(cells):
            rows.append({"label": LABELS.get(f, f), "cells": cells})
    return {"months": months, "sources": [kept[m].get("source", "") for m in months], "rows": rows}

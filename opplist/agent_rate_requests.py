"""The Agent platform's rate-request counts (batch FX4).

`python -m opplist agent-rate-requests` writes
..\\data\\outreach-import\\rate-requests.json, the file she uploads on the
Agent platform's Monthly import page: per MFN office, how many cases it
asked us (theirs) and how many rate requests we sent it (ours). Counts
only: no prices, customers or case numbers. The database is opened read
only; the file it replaces is copied into _backup first, dated.

    {kind "demo-rate-requests", version 1, made, from, source,
     offices: [{agentId, theirs {n, quoted, won, last},
                ours {n, replied, quoted, won, last}}]}

agentId is a live party's mfn_no (two live parties with one mfn_no are
added together). theirs: the cases it asked us - scope In, not registered
in error, not a duplicate - n, quoted (reached Quoted at any time, or
Won: a Won case is quoted even without a Quoted step), won,
last (the latest received_on). ours: the live vendor requests to it - n,
replied (replied_on set, or quoted), quoted (or awarded), won (awarded),
last (the latest asked_on); so on both sides won <= quoted (<= replied) <=
n. An office with nothing on either side is left out.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

KIND = "demo-rate-requests"
VERSION = 1
SOURCE = "OppList (counts only: no prices, customers or case numbers)"
FILE_NAME = "rate-requests.json"


def out_dir() -> Path:
    return paths.DATA_DIR / "outreach-import"


def _theirs() -> dict:
    return {"n": 0, "quoted": 0, "won": 0, "last": ""}


def _ours() -> dict:
    return {"n": 0, "replied": 0, "quoted": 0, "won": 0, "last": ""}


def _sort_key(agent_id: str):
    return (0, int(agent_id), agent_id) if agent_id.isdigit() else (1, 0, agent_id)


def build(conn, today: str) -> dict:
    """The file's content from the database (read only)."""
    mfn = {}                                            # party id -> mfn_no
    for row in conn.execute("SELECT id, trim(mfn_no) FROM party WHERE merged_into IS NULL "
                            "AND trim(COALESCE(mfn_no, '')) <> ''"):
        mfn[row[0]] = row[1]
    offices, dates = {}, []

    def office(agent_id):
        return offices.setdefault(agent_id, {"agentId": agent_id, "theirs": _theirs(), "ours": _ours()})

    for row in conn.execute(
            "SELECT requester_party_id, status, quoted_on_any, received_on FROM v_enquiry "
            "WHERE scope = 'In' AND COALESCE(closed_reason, '') <> 'Registered in error' "
            "AND COALESCE(closed_reason, '') NOT LIKE 'Duplicate%'"):
        if row[0] not in mfn:
            continue
        side = office(mfn[row[0]])["theirs"]
        side["n"] += 1
        won = row[1] == "Won"
        # the counts nest: a Won case counts as quoted even with no Quoted
        # step (the Excel cases, OPP-1665); the rest only once quoted
        side["quoted"] += 1 if row[2] or won else 0
        side["won"] += 1 if won else 0
        day = (row[3] or "")[:10]
        side["last"] = max(side["last"], day)
        if day:
            dates.append(day)
    for row in conn.execute("SELECT vendor_party_id, asked_on, replied_on, quoted, awarded FROM vendor_request "
                            "WHERE voided = 0"):
        if row[0] not in mfn:
            continue
        side = office(mfn[row[0]])["ours"]
        side["n"] += 1
        # the counts nest: awarded is quoted and replied, quoted is replied
        awarded, quoted = bool(row[4]), bool(row[3]) or bool(row[4])
        side["replied"] += 1 if (row[2] or "").strip() or quoted else 0
        side["quoted"] += 1 if quoted else 0
        side["won"] += 1 if awarded else 0
        day = (row[1] or "")[:10]
        side["last"] = max(side["last"], day)
        if day:
            dates.append(day)
    listed = [offices[k] for k in sorted(offices, key=_sort_key)
              if offices[k]["theirs"]["n"] or offices[k]["ours"]["n"]]
    return {"kind": KIND, "version": VERSION, "made": today, "from": min(dates) if dates else "",
            "source": SOURCE, "offices": listed}


def totals(doc) -> dict:
    """{theirs: {n, quoted, won}, ours: {n, replied, quoted, won}} over the offices."""
    out = {"theirs": {"n": 0, "quoted": 0, "won": 0}, "ours": {"n": 0, "replied": 0, "quoted": 0, "won": 0}}
    for o in (doc or {}).get("offices") or []:
        for side, counts in out.items():
            for key in counts:
                counts[key] += int(((o.get(side) or {}).get(key)) or 0)
    return out


@dataclass
class Result:
    path: Path
    backup: Path | None
    doc: dict
    old: dict | None
    lines: list = field(default_factory=list)


def _backup_path(folder: Path, today: str) -> Path:
    stem = Path(FILE_NAME).stem
    target = folder / "_backup" / "{}_{}.json".format(stem, today.replace("-", ""))
    n = 2
    while target.exists():
        target = folder / "_backup" / "{}_{}-{}.json".format(stem, today.replace("-", ""), n)
        n += 1
    return target


def _line(label, counts) -> str:
    return "{}: {}".format(label, ", ".join("{} {}".format(k, v) for k, v in counts.items()))


def run(db_path, today: str | None = None, folder: Path | None = None) -> Result:
    """Build from the database (read only), keep a dated copy of the file
    it replaces, write the new one."""
    today = today or dt.date.today().isoformat()
    folder = Path(folder) if folder is not None else out_dir()
    conn = sqlite3.connect("file:{}?mode=ro".format(Path(db_path).resolve().as_posix()), uri=True)
    try:
        doc = build(conn, today)
    finally:
        conn.close()
    target = folder / FILE_NAME
    old, backup = None, None
    if target.exists():
        try:
            old = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = None
        backup = _backup_path(folder, today)
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup)
    folder.mkdir(parents=True, exist_ok=True)
    # the format of the file the platform took on 3 Nov: indent 1, LF, no final newline
    target.write_bytes(json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8"))
    result = Result(target, backup, doc, old)
    result.lines = report_lines(doc, old)
    return result


def report_lines(doc, old=None) -> list:
    """The offices, the totals, and the change from the file replaced."""
    out = ["offices: {} (from {})".format(len(doc["offices"]), doc["from"] or "-")]
    for o in doc["offices"]:
        t, u = o["theirs"], o["ours"]
        out.append("  {:>8}  theirs n {} quoted {} won {} last {}  |  ours n {} replied {} quoted {} won {} last {}".format(
            o["agentId"], t["n"], t["quoted"], t["won"], t["last"] or "-",
            u["n"], u["replied"], u["quoted"], u["won"], u["last"] or "-"))
    now = totals(doc)
    out.append(_line("total theirs", now["theirs"]))
    out.append(_line("total ours", now["ours"]))
    if old is None:
        out.append("no earlier file to compare with")
        return out
    before = totals(old)
    old_ids = {str(o.get("agentId")) for o in old.get("offices") or []}
    new_ids = {o["agentId"] for o in doc["offices"]}
    out.append("against the file replaced (made {}): offices {} -> {} ({} new, {} gone)".format(
        old.get("made") or "?", len(old_ids), len(new_ids), len(new_ids - old_ids), len(old_ids - new_ids)))
    for side in ("theirs", "ours"):
        out.append("  {}: {}".format(side, ", ".join(
            "{} {} -> {}".format(k, before[side][k], now[side][k]) for k in now[side])))
    return out

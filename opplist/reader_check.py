r"""reader-check (batch RG1): the rule-based reader on the month report's
registered mails, against the cases as they stand today - no Outlook, no
attachments.

    python -m opplist reader-check [--months 2026-09,2026-10,2026-11] [--compare <an earlier report>]

The cases are the month report's base (base_cases). Each
case's registered mail is found in the mail archive
(case_mail.resolve_registered), read by intake.build, and each field is
compared with the case (judge). Read-only on both databases. The report
goes to ..\docs\reports\reader-check-YYYY-MM-DD.md, with the verdicts beside
it in a .json of the same name (values and verdicts, never the mail's
text) so a later run can --compare against it.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from . import intake, monthly, paths

FIELDS = ("requester", "received_on", "origin_country", "origin_loc", "dest_country", "dest_loc",
          "service_mode", "incoterm", "gross_kg", "volume_cbm", "containers", "commodity")
LABELS = {"requester": "Requester", "received_on": "Received on", "origin_country": "Origin country",
          "origin_loc": "Origin place", "dest_country": "Destination country", "dest_loc": "Destination place",
          "service_mode": "Mode", "incoterm": "Incoterm", "gross_kg": "Gross weight", "volume_cbm": "Volume",
          "containers": "Containers", "commodity": "Commodity"}
#: the fields whose wrong and empty cases the report lists one by one
LISTED = ("requester", "origin_country", "origin_loc", "dest_country", "dest_loc")
REPORT_DIR = paths.REPORTS_DIR
#: RG2a: November too
MONTHS = ("2026-09", "2026-10", "2026-11")


# ------------------------------------------------------------- reading --
def _totals(draft) -> dict:
    """The draft's weight, volume and containers: its values, else its lines."""
    from . import cargo
    out = {"gross_kg": draft.values.get("gross_kg"), "volume_cbm": draft.values.get("volume_cbm") or
           draft.values.get("lcl_cbm"), "container_qty": draft.values.get("container_qty"),
           "container_type": draft.values.get("container_type")}
    if draft.cargo_lines:
        weight = cbm = boxes = None
        kind = None
        for line in draft.cargo_lines:
            line = cargo.normalise(dict(line))
            if line.get("weight_kg") is not None:
                weight = (weight or 0) + float(line["weight_kg"])
            if line.get("cbm") is not None:
                cbm = (cbm or 0) + float(line["cbm"])
            if line.get("container_qty"):
                boxes = (boxes or 0) + int(line["container_qty"])
                kind = kind or line.get("container_type")
        out["gross_kg"] = weight if weight is not None else out["gross_kg"]
        out["volume_cbm"] = cbm if cbm is not None else out["volume_cbm"]
        if boxes:
            out["container_qty"], out["container_type"] = boxes, kind
    return out


def values_of(draft) -> dict:
    """What a reader filled, in the evaluation's fields."""
    t = _totals(draft)
    v = draft.values
    return {"requester": v.get("requester_party_id"), "received_on": v.get("received_on"),
            "origin_country": v.get("origin_country"), "origin_loc": v.get("origin_loc"),
            "dest_country": v.get("dest_country"), "dest_loc": v.get("dest_loc"),
            "service_mode": v.get("service_mode"), "incoterm": v.get("incoterm"),
            "gross_kg": t["gross_kg"], "volume_cbm": t["volume_cbm"],
            "containers": "{} x {}".format(t["container_qty"], t["container_type"] or "?") if t["container_qty"] else None,
            "commodity": v.get("commodity") or next((l.get("commodity") for l in draft.cargo_lines if l.get("commodity")), None)}


def case_values(case) -> dict:
    return {"requester": case["requester_party_id"], "received_on": case["received_on"],
            "origin_country": case["origin_country"], "origin_loc": case["origin_loc"],
            "dest_country": case["dest_country"], "dest_loc": case["dest_loc"],
            "service_mode": case["service_mode"], "incoterm": case["incoterm"],
            "gross_kg": case["gross_kg"], "volume_cbm": case["volume_cbm"],
            "containers": "{} x {}".format(case["container_qty"], case["container_type"] or "?")
            if case["container_qty"] else None, "commodity": case["commodity"]}


def judge(field: str, case_value, read_value) -> str:
    """'right', 'wrong', 'empty' (the reader gave nothing) or 'n/a' (the
    case has nothing to compare with)."""
    if case_value in (None, ""):
        return "n/a"
    if read_value in (None, ""):
        return "empty"
    if field in ("gross_kg", "volume_cbm"):
        try:
            a, b = float(case_value), float(read_value)
        except (TypeError, ValueError):
            return "wrong"
        return "right" if abs(a - b) <= 0.05 * max(abs(a), abs(b), 1e-9) else "wrong"
    if field == "containers":
        case_value, read_value = _boxes(case_value), _boxes(read_value)
    a, b = " ".join(str(case_value).split()).casefold(), " ".join(str(read_value).split()).casefold()
    if field == "commodity":
        return "right" if a in b or b in a else "wrong"
    return "right" if a == b else "wrong"


def _boxes(value) -> str:
    """CL1: '2 x 40'HC' -> '2 x 40HQ': the type through the rule reader's
    conversion, so both sides are read the same way."""
    from . import parsing
    text = str(value or "")
    qty, sep, kind = text.partition(" x ")
    if not sep:
        return text
    return "{} x {}".format(qty.strip(), parsing.container_type_of(kind) or kind.strip())


def _plain(value):
    if isinstance(value, float):
        return round(value, 3)
    return value


# ---------------------------------------------------------------- the run --
def base_cases(conn, month: str) -> list:
    """The month report's base: the cases section 1 counts."""
    return [dict(r) for r in conn.execute(
        "SELECT e.id, e.opp_no, e.received_on, e.source_subject, e.entry_id, e.conversation_id, "
        "e.requester_party_id, e.requester_key, e.origin_country, e.origin_loc, e.dest_country, e.dest_loc, "
        "e.service_mode, e.incoterm, e.gross_kg, e.volume_cbm, e.container_qty, e.container_type, e.commodity "
        "{} WHERE e.month = ? AND {} ORDER BY CAST(substr(e.opp_no, 5) AS INTEGER)".format(
            monthly.BASE, monthly.CASES), (month,)).fetchall()]


def check(conn, search, months=MONTHS) -> dict:
    """{month: {opp_no: {'missing': True} | {field: [case value, read value, verdict]}}}.
    The requester is shown by its party key."""
    from . import case_mail
    keys = {r["id"]: r["key"] for r in conn.execute("SELECT id, key FROM party")}
    out = {}
    for month in months:
        found = out.setdefault(month, {})
        for case in base_cases(conn, month):
            mail, _step = case_mail.resolve_registered(conn, case, search)
            if mail is None:
                found[case["opp_no"]] = {"missing": True}
                continue
            truth, read = case_values(case), values_of(intake.build(conn, mail))
            row = {}
            for f in FIELDS:
                verdict = judge(f, truth[f], read[f])
                a, b = truth[f], read[f]
                if f == "requester":
                    a, b = keys.get(a, a), keys.get(b, b)
                row[f] = [_plain(a), _plain(b), verdict]
            found[case["opp_no"]] = row
    return out


def counts(result: dict) -> dict:
    """{month: {field: {'value': n, 'right': n, 'wrong': n, 'empty': n}}}."""
    out = {}
    for month, cases in result.items():
        out[month] = {}
        for f in FIELDS:
            rows = [c[f][2] for c in cases.values() if not c.get("missing") and c[f][2] != "n/a"]
            out[month][f] = {"value": len(rows), "right": rows.count("right"), "wrong": rows.count("wrong"),
                             "empty": rows.count("empty")}
    return out


def _path(today: str) -> Path:
    path = REPORT_DIR / "reader-check-{}.md".format(today)
    n = 2
    while path.exists():
        path = REPORT_DIR / "reader-check-{}-{}.md".format(today, n)
        n += 1
    return path


def load(report) -> dict:
    """The verdicts kept beside an earlier report (its .json)."""
    return json.loads(Path(report).with_suffix(".json").read_text(encoding="utf-8"))


def changes(before: dict, after: dict) -> tuple:
    """([(month, opp, field, before verdict, after verdict)] right -> not right,
    the same for not right -> right)."""
    worse, better = [], []
    for month, cases in after.items():
        for opp, row in cases.items():
            old = (before.get(month) or {}).get(opp)
            if not old or old.get("missing") or row.get("missing"):
                continue
            for f in FIELDS:
                a, b = old[f][2], row[f][2]
                if a == "right" and b != "right":
                    worse.append((month, opp, f, old[f], row[f]))
                elif a != "right" and b == "right":
                    better.append((month, opp, f, old[f], row[f]))
    return worse, better


def _cell(value) -> str:
    return " ".join(str(value if value is not None else "-").split()).replace("|", "/")[:80]


def write_report(result: dict, compare=None, today=None, compared_with="") -> Path:
    today = today or dt.date.today().isoformat()
    now = counts(result)
    then = counts(compare) if compare else {}

    def cell(month, f, k):
        text = str(now[month][f][k])
        if compare and month in then:
            text += " ({})".format(then[month][f][k])
        return text

    lines = ["# Reader check, {}".format(today), "",
             "The rule-based reader (intake.build) on each base case's registered mail from the mail archive, "
             "against the case today. No Outlook, no attachments."]
    if compare:
        lines += ["", "Compared with {}: the earlier count is in brackets.".format(compared_with or "an earlier report")]
    for month, cases in result.items():
        missing = sum(1 for c in cases.values() if c.get("missing"))
        lines += ["", "## {}".format(month), "",
                  "{} cases in the base; {} mails found in the archive, {} not found.".format(
                      len(cases), len(cases) - missing, missing), "",
                  "| Field | Cases with a value | Right | Wrong | Empty |", "|---|---:|---:|---:|---:|"]
        for f in FIELDS:
            n = now[month][f]
            share = " ({:.0%})".format(n["right"] / n["value"]) if n["value"] else ""
            lines.append("| {} | {} | {}{} | {} | {} |".format(
                LABELS[f], cell(month, f, "value"), cell(month, f, "right"), share,
                cell(month, f, "wrong"), cell(month, f, "empty")))
    lines += ["", "## Requester, origin and destination: wrong or empty", "",
              "| Month | Case | Field | The case today | The reader | |", "|---|---|---|---|---|---|"]
    for month, cases in result.items():
        for opp, row in cases.items():
            if row.get("missing"):
                continue
            for f in LISTED:
                if row[f][2] in ("wrong", "empty"):
                    lines.append("| {} | {} | {} | {} | {} | {} |".format(
                        month, opp, LABELS[f], _cell(row[f][0]), _cell(row[f][1]), row[f][2]))
    if compare:
        worse, better = changes(compare, result)
        for title, rows in (("From right to not right", worse), ("From not right to right", better)):
            lines += ["", "## {} ({})".format(title, len(rows)), "",
                      "| Month | Case | Field | The case today | Before | After |", "|---|---|---|---|---|---|"]
            for month, opp, f, old, new in rows:
                lines.append("| {} | {} | {} | {} | {} ({}) | {} ({}) |".format(
                    month, opp, LABELS[f], _cell(new[0]), _cell(old[1]), old[2], _cell(new[1]), new[2]))
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = _path(today)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps(result, indent=1, sort_keys=True), encoding="utf-8")
    return path

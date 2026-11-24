"""python -m opplist cargo-check (v34, 2026-09-29): every case
whose cargo numbers fail a check, or whose stored air_weight_kg differs from
what its lines - or, without lines, its qty_raw - give. Read only: nothing
in the database changes; Iris fixes a case on its page.

The csv has one row per case and check:
    opp, mode, stored air_weight_kg / lcl_cbm / gross_kg / volume_cbm,
    recomputed air_weight_kg / gross_kg / volume_cbm, from ('lines' or
    'qty_raw'), check, what, qty_raw
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from pathlib import Path

from . import cargo, cargo_checks, parsing, routes

COLUMNS = ("opp", "mode", "stored_air_weight_kg", "stored_lcl_cbm", "stored_gross_kg", "stored_volume_cbm",
           "recomputed_air_weight_kg", "recomputed_gross_kg", "recomputed_volume_cbm", "from", "check", "what",
           "qty_raw")
#: stored and recomputed air weight may differ this much (rounding) before a row
AIR_TOLERANCE = 0.01
#: what the app itself once wrote into qty_raw - '(317.5 kg)' after a weight in
#: lbs, ', volumetric 9947 kg', ', chargeable 1666.7 kg' - is not the mail's
_COMPUTED = re.compile(r"\s*\(\s*\d[\d.,]*\s*kg\s*\)|,?\s*\b(?:volumetric|chargeable)\s+\d[\d.,]*\s*kg\b",
                       re.IGNORECASE)


def _qty_lines(case) -> tuple:
    """(lines, stated) read from the case's qty_raw as the mail reader would
    read a body (lines_from_block): 'Air:161', 'LCL:2.5' and 'FCL:...' give
    one line or none."""
    raw = (case["qty_raw"] or "").strip()
    if not raw:
        return [], []
    prefixed = re.match(r"^\s*(Air|LCL|FCL)\s*:\s*(.*)$", raw, re.IGNORECASE)
    if prefixed and (case["gross_kg"] is not None or case["volume_cbm"] is not None):
        return [], []                   # 'Air:100' beside a typed gross and volume: those are the numbers
    if prefixed:
        parsed = parsing.parse_qty(raw)
        line = {"counted": 1}
        if getattr(parsed, "air_weight_kg", None) is not None:
            line.update(weight_value=parsed.air_weight_kg, weight_unit="kg", weight_basis="total")
        if getattr(parsed, "lcl_cbm", None) is not None:
            line.update(cbm_value=parsed.lcl_cbm, cbm_basis="total")
        return ([cargo.normalise(line)] if len(line) > 1 else []), []
    parts = [_COMPUTED.sub("", p).strip(" ,") for p in raw.split(" / ")]
    parts = [p for p in parts if p]
    block = parsing.CargoBlock(label="", lines=parts, packing=parsing.parse_packing(parts))
    if not block.packing.items:
        return [], []
    lines, stated = cargo.lines_from_block(block)
    for n, line in enumerate(lines):
        line["id"] = n
        if stated is not None:
            line["stated_id"] = 1
    return lines, ([dict(stated, id=1)] if stated else [])


def _differs(stored, recomputed) -> bool:
    if stored is None and recomputed is None:
        return False
    if stored is None or recomputed is None:
        return True
    return abs(stored - recomputed) > max(0.5, AIR_TOLERANCE * max(abs(stored), abs(recomputed)))


def rows(conn) -> list:
    lim = cargo_checks.limits(conn)
    out = []
    for case in conn.execute("SELECT * FROM enquiry ORDER BY opp_no").fetchall():
        lines = cargo.lines_of(conn, case["id"])
        if lines:
            source, stated = "lines", cargo_checks.stated_of(conn, case["id"])
            t = routes.totals(conn, case["id"])
        else:
            source = "qty_raw"
            lines, stated = _qty_lines(case)
            fake = {"service_mode": case["service_mode"], "gross_kg": case["gross_kg"],
                    "volume_cbm": case["volume_cbm"]}
            t = routes.totals(conn, None, lines=lines, live_routes=[], case=fake) if lines else \
                routes.totals(conn, case["id"])
        found = [c for c in cargo_checks.for_case(dict(case), lines, stated, t, lim) if c.kind == "ask"]
        if source == "qty_raw":
            # the lines read from the text check themselves; the case's own
            # size, density and mode are checked on the numbers on file
            on_file = [c for c in cargo_checks.for_case(dict(case), [], [], routes.totals(conn, case["id"]), lim)
                       if c.kind == "ask"]
            found = [c for c in found if c.line_id is not None or c.code in ("total", "chargeable")] + \
                [c for c in on_file if c.code not in {f.code for f in found if f.line_id is not None}]
        base = {"opp": case["opp_no"], "mode": case["service_mode"] or "",
                "stored_air_weight_kg": case["air_weight_kg"], "stored_lcl_cbm": case["lcl_cbm"],
                "stored_gross_kg": case["gross_kg"], "stored_volume_cbm": case["volume_cbm"],
                "recomputed_air_weight_kg": t.get("air_weight_kg"), "recomputed_gross_kg": t.get("gross_kg"),
                "recomputed_volume_cbm": t.get("volume_cbm"), "from": source if lines else "case",
                "qty_raw": case["qty_raw"] or ""}
        for check in found:
            out.append(dict(base, check=check.code, what=check.text))
        if (lines or case["air_weight_kg"] is not None) and _differs(case["air_weight_kg"], t.get("air_weight_kg")):
            out.append(dict(base, check="air_weight", what="stored air weight {} kg; {} {}".format(
                case["air_weight_kg"] if case["air_weight_kg"] is not None else "empty",
                "the lines give" if source == "lines" else ("the quantity text gives" if lines else "the typed numbers give"),
                "{} kg".format(round(t["air_weight_kg"], 1)) if t.get("air_weight_kg") is not None else "nothing")))
    return out


def write(conn, path) -> tuple:
    """Write the csv (UTF-8 with BOM, CRLF, like the other exports);
    returns (rows, Counter of checks)."""
    found = rows(conn)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        writer.writeheader()
        for row in found:
            writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in COLUMNS})
    return found, Counter(r["check"] for r in found)

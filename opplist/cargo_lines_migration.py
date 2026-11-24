"""Cargo is lines only (batch 15b, 2026-10-01): migrate-cargo-lines.

Until now a case's quantities could be typed in three places: its cargo
lines, the typed boxes under the Cargo card (gross weight, volume,
container type and qty) and Quantity as received. From now on the lines
are the only place; the case's gross_kg, volume_cbm, air_weight_kg,
lcl_cbm, container_type, container_qty and commodity are written by the
repository from the counted lines (Repository.sync_cargo_totals).

So every case without a live line that carries any of gross_kg,
volume_cbm, container_type or container_qty gets one line holding those
numbers: the weight (kg, in total), the volume as stated (cbm), the
containers (qty x type), the case's commodity, all routes, counted,
source 'migration'. A case with no numbers gets no line. A case that has
lines, none naming a commodity, while the case names one: its first line
gets it.

Nothing is written before it is checked. The whole move is made first on
a copy of the database; for every case that gets a line the seven fields
worked out from the new line must equal today's, and opp-index.csv and
agents.csv must come out byte for byte the same, the workbook cell by cell
(its Export Info time aside). Any difference is listed and nothing is
written. Then a restore point, and the same move on the real file. A rerun
finds nothing to do and writes nothing.

One deliberate detail: v33 rounded gross_kg to two places but worked out
air_weight_kg from the unrounded weight (OPP-style 84.3682 -> 84.37 and
84.368). On an Air case with no volume where air_weight_kg is gross_kg
before rounding (they differ by less than 0.005 kg), the line takes
air_weight_kg, the more precise weight, so both stored values stay exactly
as they are.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import cargo, db

FIELDS = ("gross_kg", "volume_cbm", "air_weight_kg", "lcl_cbm", "container_type", "container_qty", "commodity")
NUMBERS = ("gross_kg", "volume_cbm", "container_type", "container_qty")


@dataclass
class Report:
    created: Counter = field(default_factory=Counter)          # lines made, by mode
    without_lines: list = field(default_factory=list)          # opp numbers left without a line
    first_commodity: list = field(default_factory=list)        # (opp, commodity) given to a first line
    commodity_changes: list = field(default_factory=list)      # (opp, before, after) on cases that had lines
    differences: list = field(default_factory=list)            # (opp, field, before, after)
    export_differences: list = field(default_factory=list)     # what differs in the exports
    written: bool = False
    restore_point: str = ""

    @property
    def ok(self) -> bool:
        return not self.differences and not self.export_differences

    def lines(self) -> list:
        out = ["lines to make: {} ({})".format(sum(self.created.values()), ", ".join(
            "{} {}".format(n, mode or "no mode") for mode, n in sorted(self.created.items(), key=lambda x: str(x[0]))))]
        out.append("cases left without a line (no numbers): {}{}".format(
            len(self.without_lines), (" - " + ", ".join(self.without_lines)) if self.without_lines else ""))
        out.append("first lines given the case's commodity: {}".format(len(self.first_commodity)))
        for opp, text in self.first_commodity:
            out.append("  {}: {}".format(opp, text))
        out.append("commodity changes on cases that already had lines: {}".format(len(self.commodity_changes)))
        for opp, before, after in self.commodity_changes:
            out.append("  {}: {!r} -> {!r}".format(opp, before, after))
        out.append("cases whose numbers would change: {}".format(len(self.differences)))
        for opp, name, before, after in self.differences:
            out.append("  {} {}: {!r} -> {!r}".format(opp, name, before, after))
        out.append("exports: {}".format("the same" if not self.export_differences else "DIFFERENT"))
        for line in self.export_differences:
            out.append("  " + line)
        out.append("written: {}{}".format("yes" if self.written else "no",
                                        " (restore point {})".format(self.restore_point) if self.restore_point else ""))
        return out


def line_from_typed(values, source: str = "manual") -> dict | None:
    """A registration's typed numbers as its first line (15b, item 3) - the
    way the migration makes one; None when nothing was typed."""
    if not any(values.get(f) not in (None, "") for f in NUMBERS):
        return None
    return {"weight_value": values.get("gross_kg"), "weight_unit": "kg" if values.get("gross_kg") is not None else None,
            "weight_basis": "total", "cbm_value": values.get("volume_cbm"), "cbm_basis": "total",
            "container_qty": values.get("container_qty"), "container_type": values.get("container_type"),
            "commodity": values.get("commodity"), "counted": 1, "route_ids": None, "source": source}


def _line_for(case) -> dict:
    weight = case["gross_kg"]
    air = case["air_weight_kg"]
    if (case["service_mode"] == "Air" and case["volume_cbm"] is None and weight is not None and air is not None
            and air != weight and abs(air - weight) < 0.005):
        weight = air                                    # v33's unrounded weight (see the module note)
    return {"weight_value": weight, "weight_unit": "kg" if weight is not None else None, "weight_basis": "total",
            "cbm_value": case["volume_cbm"], "cbm_basis": "total",
            "container_qty": case["container_qty"], "container_type": case["container_type"],
            "commodity": case["commodity"], "counted": 1, "route_ids": None, "source": "migration"}


def plan(conn) -> tuple:
    """([(case, line)], [(case, first line id)], [opp left without a line])."""
    live = {r[0] for r in conn.execute("SELECT DISTINCT enquiry_id FROM enquiry_cargo WHERE voided = 0")}
    make, firsts, none = [], [], []
    for case in conn.execute("SELECT * FROM enquiry ORDER BY id").fetchall():
        if case["id"] in live:
            lines = conn.execute("SELECT id, commodity FROM enquiry_cargo WHERE enquiry_id = ? AND voided = 0 "
                                 "ORDER BY seq, id", (case["id"],)).fetchall()
            if (case["commodity"] or "").strip() and not any((l["commodity"] or "").strip() for l in lines):
                firsts.append((case, lines[0]["id"]))
            continue
        if any(case[f] not in (None, "") for f in NUMBERS):
            make.append((case, _line_for(case)))
        else:
            none.append(case["opp_no"])
    return make, firsts, none


def apply(conn, report: Report | None = None, between=None) -> Report:
    """The move itself, through the repository (change_log, source
    migration), in two steps: the cases that already have lines first (a
    first line takes the case's commodity; every one's commodity follows its
    lines from now on - listed, not checked), then a line for each case with
    typed numbers (checked: nothing may change). `between` is called after
    the first step (the export comparison starts there)."""
    report = report or Report()
    repo = db.Repository(conn, source="migration")
    make, firsts, none = plan(conn)
    report.without_lines = none
    with_lines_before = {}
    for case in conn.execute("SELECT id, opp_no, commodity FROM enquiry WHERE id IN "
                             "(SELECT enquiry_id FROM enquiry_cargo WHERE voided = 0)").fetchall():
        with_lines_before[case["id"]] = (case["opp_no"], case["commodity"])
    for case, line_id in firsts:
        repo.edit_cargo_line(line_id, {"commodity": case["commodity"]})
        report.first_commodity.append((case["opp_no"], case["commodity"]))
    for enquiry_id, (opp, commodity) in with_lines_before.items():
        repo.sync_cargo_totals(enquiry_id)
        after = conn.execute("SELECT commodity FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone()[0]
        if (after or None) != (commodity or None):
            report.commodity_changes.append((opp, commodity, after))
    if between is not None:
        between()
    before = {case["id"]: {f: case[f] for f in FIELDS} for case, _ in make}
    for case, line in make:
        repo.add_cargo_line(case["id"], line)
        report.created[case["service_mode"]] += 1
    for case, _ in make:
        now = conn.execute("SELECT {} FROM enquiry WHERE id = ?".format(", ".join(FIELDS)), (case["id"],)).fetchone()
        for name in FIELDS:
            if not _same(before[case["id"]][name], now[name]):
                report.differences.append((case["opp_no"], name, before[case["id"]][name], now[name]))
    return report


def _same(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) < 1e-9
    return (a if a not in ("",) else None) == (b if b not in ("",) else None)


def _exports(conn, folder: Path) -> dict:
    from . import export_csv, export_xlsx
    folder.mkdir(parents=True, exist_ok=True)
    return {"opp-index.csv": export_csv.export_opp_index(conn, folder / "opp-index.csv"),
            "agents.csv": export_csv.export_agents(conn, folder / "agents.csv"),
            "workbook": export_xlsx.export(conn, folder / "book.xlsx", db_path="")}


def _compare_exports(a: dict, b: dict) -> list:
    from openpyxl import load_workbook
    out = []
    for name in ("opp-index.csv", "agents.csv"):
        if a[name].read_bytes() != b[name].read_bytes():
            out.append("{} differs".format(name))
    wa, wb = load_workbook(a["workbook"]), load_workbook(b["workbook"])
    if wa.sheetnames != wb.sheetnames:
        out.append("workbook sheets differ: {} / {}".format(wa.sheetnames, wb.sheetnames))
        return out
    for sheet in wa.sheetnames:
        sa, sb = wa[sheet], wb[sheet]
        rows = max(sa.max_row, sb.max_row)
        cols = max(sa.max_column, sb.max_column)
        for r in range(1, rows + 1):
            for c in range(1, cols + 1):
                if sheet == "Export Info" and r == 1:
                    continue                            # 'Generated at': the time of the export
                va, vb = sa.cell(r, c).value, sb.cell(r, c).value
                if va != vb:
                    out.append("workbook {}!{}{}: {!r} -> {!r}".format(sheet, sa.cell(r, c).column_letter, r, va, vb))
                    if len(out) > 40:
                        return out
    return out


def run(db_path, write: bool = False, restore_dir=None) -> Report:
    """Try the move on a copy and compare; with `write`, and only when
    nothing differs, a restore point and the same move on the file."""
    from . import backup

    db_path = Path(db_path)
    work = Path(tempfile.mkdtemp(prefix="opplist-cargo-lines-"))
    try:
        copy = work / "copy.db"
        src = sqlite3.connect("file:{}?mode=ro".format(db_path.as_posix()), uri=True)
        dst = sqlite3.connect(str(copy))
        src.backup(dst)
        dst.close()
        src.close()
        conn = db.open_db(copy)
        try:
            exported = {}

            def snapshot():
                conn.commit()
                exported["before"] = _exports(conn, work / "before")
            report = apply(conn, between=snapshot)
            before = exported["before"]
            conn.commit()
            after = _exports(conn, work / "after")
            report.export_differences = _compare_exports(before, after)
        finally:
            conn.close()
        if not write or not report.ok:
            return report
        target = backup.before_task(db_path, "migrate-cargo-lines", restore_dir=restore_dir)
        report.restore_point = str(target)
        conn = db.open_db(db_path)
        try:
            real = apply(conn)
            if not real.ok:
                conn.rollback()
                report.differences = real.differences
                return report
            conn.commit()
            report.written = True
        finally:
            conn.close()
        return report
    finally:
        shutil.rmtree(work, ignore_errors=True)

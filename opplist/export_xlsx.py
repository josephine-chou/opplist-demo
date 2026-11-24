"""Write 'LATAM Opportunity List (auto-export).xlsx'.

The Opportunities sheet keeps the old 41 headers in the old order because
Export-OppIndex.ps1 finds its columns by header text. Columns that used to be
formulas are written as values, read from v_enquiry so that nothing is
computed twice.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

from . import db, report as report_mod, status as status_mod

DATE_FORMAT = "yyyy-mm-dd"

# The workbook's column contract, in order. Do not reorder or rename.
OPPORTUNITY_COLUMNS = [
    "Enquiry No.", "Date Received", "Month", "Source Type", "Agent",
    "Agent Country", "Agent Region", "Handled By", "Service Mode",
    "Container Type", "Container Qty", "TEU", "Air Weight (kgs)",
    "LCL Volume (cbm)", "Origin Country", "Origin Detail", "Origin Alt",
    "Origin Region", "Dest. Country", "Dest. Detail", "Dest. Alt",
    "Dest. Region", "LATAM", "Direction", "Incoterms", "Commodity",
    "Special Handling", "Status", "Date Quoted", "Quote Ref", "Date Closed",
    "Days to Quote", "Loss Reason", "Overdue Flag", "Location Check",
    "FMS Job No.", "Source Qty (raw)", "Remarks", "System Notes",
    "Country/Location Check", "Mode/Location Check",
]

AGENT_COLUMNS = [
    "Agent", "Full Name", "Source Type", "Country", "Region", "City",
    "MFN No.", "Contact", "Email", "First Enquiry", "Last Enquiry",
    "Last Enquiry No.", "Total Enquiries", "Secured", "Conversion Rate",
    "Win Rate", "Enquiry Share", "Secured Share", "Days Since Last", "Notes",
    "Rank Helper", "MFN Member",
]

LOCATION_COLUMNS = [
    "Entry (as used in the file)", "Official Name", "Type", "Country",
    "Region", "Status", "Notes", "Code_System", "Code_Official",
    "Parent_City", "Country_ISO2", "Is_Seaport", "Is_Airport", "Aliases",
]

COUNTRY_COLUMNS = ["Country_Name", "ISO2", "ISO3", "Region"]

LIST_COLUMNS = [
    "Source Type", "Service Mode", "Status", "Loss Reason",
    "(Est. Value Band removed - volume is shown by TEU / kgs / cbm)",
    "Container Type", "TEU Factor", "Location Type",
    "LATAM Countries (drives the LATAM flag)",
    "Special Handling (reference - free text)", "Incoterms", "",
]

LEGACY_STATUS_LIST = [
    "New", "Sent to Vendor", "Quoted - Awaiting Reply", "Secured", "Lost",
    "No Response", "Unable to Handle", "Legacy - Unknown",
    "In Progress / Shipped", "Customer Cancelled",
]
SERVICE_MODES = ["Air", "Sea-Air", "LCL", "FCL", "RORO", "Break Bulk", "Project",
                 "Truck", "Cross-border Truck", "Multi-mode", "Not specified"]
LOCATION_TYPES = ["Country", "State or Province", "City", "Seaport", "Airport"]
TEU_FACTORS = {"20GP": 1, "40GP": 2, "40HQ": 2, "20OT": 1, "40OT": 2,
               "20FR": 1, "40FR": 2, "20RF": 1, "40RF": 2, "40NOR": 2,
               "45HQ": 2}


def _to_date(value):
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _days(later, earlier):
    a, b = _to_date(later), _to_date(earlier)
    return (a - b).days if a and b else None


def _tags(value):
    if not value:
        return None
    return "; ".join(part for part in str(value).split(";") if part)


def _quote_ref(conn, enquiry_id, system_notes):
    """The quote reference lives in the Quoted event's note; a case that never
    got a Quoted event keeps it in system_notes instead."""
    row = conn.execute(
        "SELECT note FROM status_event WHERE enquiry_id = ? AND status = 'Quoted' "
        "AND note LIKE 'quote ref: %' ORDER BY at LIMIT 1", (enquiry_id,)
    ).fetchone()
    if row:
        return row["note"][len("quote ref: "):].strip()
    for line in (system_notes or "").splitlines():
        if line.lower().startswith("quote ref:"):
            return line.split(":", 1)[1].strip()
    return None


def _loss_reason(row):
    """Only closed cases carry one, and a blank stays blank."""
    if row["status"] not in ("Lost", "Cancelled", "Dropped"):
        return None
    return row["closed_reason"] or None


# ------------------------------------------------------------------ sheets --
def _write_header(ws, columns) -> None:
    ws.append(list(columns))
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"


def _autofilter_and_widths(ws, widths) -> None:
    if ws.max_row >= 1:
        ws.auto_filter.ref = "A1:{}{}".format(
            get_column_letter(ws.max_column), max(ws.max_row, 1))
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width


def opportunities_rows(conn: sqlite3.Connection) -> list:
    rows = conn.execute(
        "SELECT * FROM v_enquiry ORDER BY CAST(substr(opp_no, 5) AS INTEGER)"
    ).fetchall()
    routes_by_case = {}
    if _has_routes(conn):
        for route in conn.execute("SELECT * FROM v_route ORDER BY enquiry_id, seq"):
            routes_by_case.setdefault(route["enquiry_id"], []).append(route)
    out = []
    for row in rows:
        # quoted_on / closed_on are the exact dates only; the approximate ones
        # the migration had to invent stay inside the app (schema.sql v2).
        received = _to_date(row["received_on"])
        quoted = _to_date(row["quoted_on"])
        out.append([
            row["opp_no"],
            received,
            row["month"],
            row["source_type_legacy"],
            row["requester_key"],
            row["requester_country"],
            row["requester_region"],
            row["owner"],
            row["service_mode"],
            row["container_type"],
            row["container_qty"],
            row["teu"],
            row["air_weight_kg"],
            row["lcl_cbm"],
            row["origin_country"],
            row["origin_loc"],
            row["origin_alt"],
            row["origin_region"],
            row["dest_country"],
            row["dest_loc"],
            row["dest_alt"],
            row["dest_region"],
            row["latam"],
            row["direction"],
            row["incoterm"],
            row["commodity"],
            _tags(row["special_handling"]),
            status_mod.row_to_legacy(row),
            quoted,
            _quote_ref(conn, row["id"], row["system_notes"]),
            _to_date(row["closed_on"]),
            _days(row["quoted_on"], row["received_on"]),
            _loss_reason(row),
            None,   # Overdue Flag        - the app tracks this, not the sheet
            None,   # Location Check      - ditto
            None,   # FMS Job No.         - out of scope in v1
            row["qty_raw"],
            row["remarks"],
            _system_notes(conn, row, routes_by_case.get(row["id"], [])),
            None,   # Country/Location Check
            None,   # Mode/Location Check
        ])
    return out


def _has_routes(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'v_route'").fetchone() is not None


def _route_place(conn, route, side) -> str:
    from . import shipments
    return shipments.place_title(conn, route["{}_loc_id".format(side)], route["{}_loc".format(side)])         or route["{}_country".format(side)] or "?"


def _system_notes(conn, row, case_routes) -> str | None:
    """System Notes (v33): with more than one route, the routes and their
    results after what was there - 'A Santos to Exshaw, AB: Won; B Santos to
    Brookfield, NS: Lost, Price too high'. One route: as before."""
    notes = row["system_notes"]
    if len(case_routes) <= 1:
        return notes
    parts = []
    for r in case_routes:
        text = "{} {} to {}".format(r["letter"], _route_place(conn, r, "origin"), _route_place(conn, r, "dest"))
        if r["result"]:
            text += ": " + r["result"] + (", " + r["lost_reason"] if r["result"] == "Lost" and r["lost_reason"] else "")
        parts.append(text)
    listed = "; ".join(parts)
    return "{}\n{}".format(notes, listed) if notes else listed


ROUTE_COLUMNS = ["Enquiry No.", "Route", "Origin Country", "Origin Location", "Origin Alt",
                 "Dest. Country", "Dest. Location", "Dest. Alt", "Service Mode", "Incoterm", "Service",
                 "Port of Discharge", "Result", "Result Date", "Lost Reason", "Direction", "LATAM", "Notes"]


def _sheet_routes(wb, conn) -> None:
    """One row per live route (v33): the OPP's number and letter,
    the lane, mode, incoterm, service scope, port of discharge and result."""
    ws = wb.create_sheet("Routes")
    _write_header(ws, ROUTE_COLUMNS)
    if _has_routes(conn):
        for r in conn.execute("SELECT * FROM v_route ORDER BY CAST(substr(opp_no, 5) AS INTEGER), seq"):
            ws.append([r["opp_no"], r["letter"], r["origin_country"], r["origin_loc"], r["origin_alt"],
                       r["dest_country"], r["dest_loc"], r["dest_alt"], r["service_mode"], r["incoterm"],
                       r["service_scope"], r["pod"], r["result"], _to_date(r["result_on"]), r["lost_reason"],
                       r["direction"], r["latam"], r["notes"]])
        for cell in ws["N"][1:]:
            cell.number_format = DATE_FORMAT
    _autofilter_and_widths(ws, [11, 7, 14, 16, 20, 14, 16, 22, 12, 9, 14, 16, 8, 12, 20, 12, 7, 24])


def _sheet_opportunities(wb, conn) -> None:
    ws = wb.create_sheet("Opportunities")
    _write_header(ws, OPPORTUNITY_COLUMNS)
    for row in opportunities_rows(conn):
        ws.append(row)
    for column in (2, 29, 31):   # Date Received, Date Quoted, Date Closed
        letter = get_column_letter(column)
        for cell in ws[letter][1:]:
            cell.number_format = DATE_FORMAT
    widths = [11, 13, 9, 11, 20, 14, 15, 11, 13, 14, 13, 7, 14, 15, 15, 15,
              18, 14, 15, 15, 18, 14, 7, 12, 10, 26, 18, 22, 12, 18, 12, 12,
              22, 13, 14, 12, 20, 30, 30, 14, 14]
    _autofilter_and_widths(ws, widths)


def _sheet_agents(wb, conn) -> None:
    ws = wb.create_sheet("Agent_Master")
    _write_header(ws, AGENT_COLUMNS)
    total_enquiries = conn.execute("SELECT COUNT(*) AS n FROM enquiry").fetchone()["n"]
    total_won = conn.execute(
        "SELECT COUNT(*) AS n FROM enquiry WHERE status = 'Won'").fetchone()["n"]
    today = dt.date.today()

    rows = conn.execute("SELECT * FROM v_party_stats ORDER BY id").fetchall()
    for index, row in enumerate(rows, start=2):
        contacts = conn.execute(
            "SELECT name, email FROM contact WHERE party_id = ? "
            "ORDER BY is_primary DESC, id", (row["id"],)
        ).fetchall()
        names = "; ".join(c["name"] for c in contacts if c["name"])
        emails = "; ".join(c["email"] for c in contacts if c["email"])
        total = row["total_enquiries"]
        won = row["won"]
        decided = conn.execute(
            "SELECT COUNT(*) AS n FROM enquiry WHERE requester_party_id = ? "
            "AND status IN ('Won', 'Lost')", (row["id"],)
        ).fetchone()["n"]
        last = _to_date(row["last_enquiry_on"])
        last_no = conn.execute(
            "SELECT opp_no FROM enquiry WHERE requester_party_id = ? "
            "ORDER BY received_on DESC, id DESC LIMIT 1", (row["id"],)
        ).fetchone()
        ws.append([
            row["key"],
            row["full_name"],
            "Office" if row["type"] == "Office" else "Agent",
            row["country"],
            row["region"],
            row["city"],
            row["mfn_no"],
            names or None,
            emails or None,
            _to_date(row["first_enquiry_on"]),
            last,
            last_no["opp_no"] if last_no else None,
            total,
            won,
            (won / total) if total else None,
            (won / decided) if decided else None,
            (total / total_enquiries) if total and total_enquiries else None,
            (won / total_won) if won and total_won else None,
            (today - last).days if last else None,
            row["notes"],
            (total + index / 100000) if total else None,
            "Y" if row["mfn_member"] == 1 else None,
        ])
    for column in (10, 11):
        for cell in ws[get_column_letter(column)][1:]:
            cell.number_format = DATE_FORMAT
    for column in (15, 16, 17, 18):
        for cell in ws[get_column_letter(column)][1:]:
            cell.number_format = "0%"
    _autofilter_and_widths(
        ws, [24, 34, 12, 16, 16, 14, 12, 28, 34, 13, 13, 15, 14, 9, 14, 10,
             13, 13, 14, 40, 12, 12])


def _sheet_locations(wb, conn) -> None:
    ws = wb.create_sheet("Lookup_Location")
    _write_header(ws, LOCATION_COLUMNS)
    rows = conn.execute(
        "SELECT l.*, c.region, c.iso2 FROM location l "
        "LEFT JOIN country c ON c.name = l.country "
        "ORDER BY (l.status = 'To Confirm') DESC, l.key"
    ).fetchall()
    for row in rows:
        ws.append([
            row["key"], row["official_name"], row["type"], row["country"],
            row["region"], row["status"], row["notes"], row["code_system"],
            row["code_official"], row["parent_city"], row["iso2"],
            "Y" if row["is_seaport"] else "N",
            "Y" if row["is_airport"] else "N",
            row["aliases"],
        ])
    _autofilter_and_widths(ws, [26, 26, 20, 18, 20, 12, 34, 15, 14, 16, 13, 12, 12, 24])


def _sheet_countries(wb, conn) -> None:
    ws = wb.create_sheet("Country_List")
    _write_header(ws, COUNTRY_COLUMNS)
    for row in conn.execute(
        "SELECT name, iso2, iso3, region FROM country "
        "ORDER BY (name = 'Unknown') DESC, name"
    ):
        ws.append([row["name"], row["iso2"], row["iso3"], row["region"]])
    _autofilter_and_widths(ws, [28, 8, 8, 22])


def _sheet_lists(wb, conn) -> None:
    ws = wb.create_sheet("Lists")
    _write_header(ws, LIST_COLUMNS)
    containers = db.get_setting_list(conn, "container_types")
    columns = [
        ["Agent", "Office"],
        SERVICE_MODES,
        LEGACY_STATUS_LIST,
        # Both vocabularies, Lost first, in one column: the workbook's
        # validation list has no status to go on (2026-09-18).
        status_mod.closed_reasons(conn),
        [],
        containers,
        [TEU_FACTORS.get(c) for c in containers],
        LOCATION_TYPES,
        db.get_setting_list(conn, "latam_countries"),
        db.get_setting_list(conn, "special_handling"),
        ["EXW", "FCA", "FAS", "FOB", "CFR", "CIF", "CPT", "CIP", "DAP", "DPU",
         "DDP", "DDU"],
        ["(DDU = legacy term, kept for old records)"],
    ]
    height = max(len(c) for c in columns)
    for index in range(height):
        ws.append([col[index] if index < len(col) else None for col in columns])
    for index in range(1, len(LIST_COLUMNS) + 1):
        ws.column_dimensions[get_column_letter(index)].width = 24


def _write_block(ws, row, block) -> int:
    """One titled table. Returns the next free row."""
    ws.cell(row=row, column=1, value=block.title).font = Font(bold=True)
    row += 1
    if block.rows:
        for index, name in enumerate(block.columns, start=1):
            ws.cell(row=row, column=index, value=name).font = Font(italic=True)
        row += 1
        for values in block.rows:
            for index, value in enumerate(values, start=1):
                ws.cell(row=row, column=index, value=value)
            row += 1
    else:
        ws.cell(row=row, column=1, value="(nothing this month)")
        row += 1
    return row + 1


def _sheet_report(wb, conn, months) -> None:
    """The month sections, one report after another.

    The same report.build() feeds the /report page, so the sheet and the
    screen cannot disagree. Trend and Definitions get their own sheets.
    """
    ws = wb.create_sheet("Report")
    row = 1
    reports = [report_mod.build(conn, month) for month in months]
    for report in reports:
        ws.cell(row=row, column=1, value="Month {}".format(report.month)
                ).font = Font(bold=True, size=13)
        row += 1
        ws.cell(row=row, column=1, value=report.summary)
        row += 2
        for block in report.sections:
            row = _write_block(ws, row, block)
        row += 1
    ws.column_dimensions["A"].width = 30
    for letter in "BCDEFGH":
        ws.column_dimensions[letter].width = 16

    ws = wb.create_sheet("Trend")
    row = 1
    # The trend runs up to the latest month asked for; one is enough.
    latest = max(reports, key=lambda r: r.month)
    row = _write_block(ws, row, latest.trend)
    ws.column_dimensions["A"].width = 12
    for letter in "BCDEFGHIJKLMN":
        ws.column_dimensions[letter].width = 14

    ws = wb.create_sheet("Definitions")
    ws.cell(row=1, column=1, value="What each number means").font = Font(bold=True)
    for index, line in enumerate(latest.definitions, start=3):
        ws.cell(row=index, column=1, value=line)
    ws.column_dimensions["A"].width = 120


def _sheet_info(wb, conn, db_path, months) -> None:
    ws = wb.create_sheet("Export Info")
    ws.append(["Generated at", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
    ws.append(["Database", str(db_path)])
    ws.append(["Inquiries", conn.execute(
        "SELECT COUNT(*) AS n FROM enquiry").fetchone()["n"]])
    ws.append(["Parties", conn.execute(
        "SELECT COUNT(*) AS n FROM party").fetchone()["n"]])
    ws.append(["Report months", ", ".join(months)])
    ws.append(["Note", "Generated by OppList. Edit in the app, not here."])
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 70


# ------------------------------------------------------------------- entry --
def export(conn: sqlite3.Connection, path, db_path="", months=None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    months = list(months or report_mod.default_months())

    wb = Workbook()
    wb.remove(wb.active)
    _sheet_opportunities(wb, conn)
    _sheet_routes(wb, conn)
    _sheet_locations(wb, conn)
    _sheet_agents(wb, conn)
    _sheet_countries(wb, conn)
    _sheet_lists(wb, conn)
    _sheet_report(wb, conn, months)
    _sheet_info(wb, conn, db_path, months)
    wb.save(path)
    return path

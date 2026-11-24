"""The month's inquiries as an Excel file for Derek (batch 19).

Her v3 format of 1 Oct, matched cell by cell to her two files
(OppList_Inquiries_2026-09-30.xlsx, ..._2026-10-30.xlsx): one sheet
'Inquiries', row 1 the title and 'As of', row 2 the totals (SUBTOTAL, so
a filter recounts them), row 3 the 22 headers, the cases from row 4.

Rows: every inquiry received in the month (scope In) - agents, JoJo
offices and direct customers together - but not a Dropped case (as the
monthly report), a duplicate, one registered in error, or one another
own office handles (Handled by set; FX5, her answers of 12 and 13
Nov), by Inquiry No. as her files are. Values are read from OppList today;
a column that should have a value and has none writes TBC, and tbc_list()
names each one for the Report page. Nothing is written to disk: build()
returns the bytes.
"""

from __future__ import annotations

import datetime as dt
import io
import re
from dataclasses import dataclass, field

from . import cargo, db, parsing, routes as routes_mod

SHEET = "Inquiries"
HEADERS = ("Inquiry No.", "Date Received", "Agent", "Service Mode", "Container Type", "Qty", "TEU",
           "Air Weight (kgs)", "LCL Volume (cbm)", "Origin Country", "Origin Detail", "Dest. Country",
           "Dest. Detail", "Service Type", "Incoterms", "Commodity", "Special Handling", "Status",
           "Date Quoted", "FU Date", "Date Closed", "Loss Reason")
TBC = "TBC"
#: the columns that always have a value: TBC when the case has nothing
ALWAYS = ("Inquiry No.", "Date Received", "Agent", "Service Mode", "Origin Country", "Origin Detail",
          "Dest. Country", "Dest. Detail", "Service Type", "Incoterms", "Commodity", "Status")
#: what the Report page says to fill, per column
FILL = {"Agent": "the requester", "Service Mode": "the mode", "Origin Country": "the origin country",
        "Origin Detail": "the origin place", "Dest. Country": "the destination country",
        "Dest. Detail": "the destination place", "Service Type": "both countries (export, import or triangle)",
        "Incoterms": "the incoterm", "Commodity": "the commodity", "Status": "the status",
        "Container Type": "the FCL cargo line's container type", "Qty": "the FCL cargo line's container count",
        "TEU": "the FCL cargo line's containers", "Air Weight (kgs)": "the air cargo's weight or size",
        "LCL Volume (cbm)": "the LCL cargo's volume", "Date Quoted": "the date it was quoted",
        "Date Closed": "the date it closed", "Loss Reason": "the reason it was lost",
        "Inquiry No.": "the case number", "Date Received": "the date it came in"}
DATE_COLS = ("Date Received", "Date Quoted", "FU Date", "Date Closed")
CLOSED = ("Won", "Lost", "Cancelled", "Dropped")
SERVICE_TYPES = {"US Export": "Export", "US Import": "Import", "Cross-trade": "Triangle", "US Domestic": "Domestic"}
#: TEU per container: 20 ft 1, 40 ft and 45 ft 2
_FEET = re.compile(r"^(20|40|45)")
_SIZE = re.compile(r"\b(20|40|45)\s*(?:'|\u2019|\u2032|FT|FEET)?", re.I)
_BOX = (("HQ", r"\bH[QC]\b|HIGH\s*CUBE"), ("RF", r"\bRF\b|REEFER"), ("OT", r"\bOT\b|OPEN\s*TOP"),
        ("FR", r"\bFR\b|FLAT\s*RACK"), ("NOR", r"\bNOR\b"))


def container_type(text) -> str:
    """'40'HQ', '20FT DRY', '20 DC', '40' HC DV SOC' as OppList's box
    (40HQ, 20GP, 20GP, 40HQ); text with no size ('Mixed') as written."""
    text = (text or "").strip()
    known = parsing.container_type_of(text)
    if known:
        return known
    size = _SIZE.search(text)
    if not size:
        return text
    rest = text[size.end():].upper()
    box = next((name for name, pattern in _BOX if re.search(pattern, rest)), "")
    return parsing.normalise_container(size.group(1), box)


@dataclass
class Row:
    case_id: int
    values: dict
    tbc: list = field(default_factory=list)          # [(column, what to fill)]


def _day(value):
    text = str(value or "")[:10]
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def _joined(values, sep) -> str:
    out = []
    for v in values:
        v = (v or "").strip()
        if v and v not in out:
            out.append(v)
    return sep.join(out)


def _routes(conn, case_id) -> list:
    return conn.execute("SELECT * FROM v_route WHERE enquiry_id = ? AND voided = 0 ORDER BY seq",
                        (case_id,)).fetchall()


def _teu(box) -> int:
    found = _FEET.match((box or "").upper())
    return 0 if not found else (1 if found.group(1) == "20" else 2)


def _containers(conn, case) -> tuple:
    """(type, qty, teu) from the counted container lines, each once per
    route it covers (as the case's own totals count them); the case's own
    fields when it has no line."""
    boxes = []
    live = routes_mod.routes_of(conn, case["id"])
    for line in cargo.lines_of(conn, case["id"]):
        if not int(line.get("counted", 1) or 0):
            continue
        if line.get("container_type") and line.get("container_qty"):
            times = len(routes_mod.applies_to(line.get("route_ids"), live)) if live else 1
            boxes.append((container_type(line["container_type"]), int(line["container_qty"]) * max(times, 1)))
    if not boxes and case["container_type"] and case["container_qty"]:
        boxes = [(container_type(case["container_type"]), int(case["container_qty"]))]
    if not boxes:
        return None, None, None
    by_type = {}
    for box, n in boxes:
        by_type[box] = by_type.get(box, 0) + n
    kind = next(iter(by_type)) if len(by_type) == 1 else "+".join(
        "{}x{}".format(n, box) for box, n in by_type.items())
    qty = sum(by_type.values())
    if any(not _teu(box) for box in by_type):
        return kind, qty, None                      # a box with no size ('Mixed'): TEU to fill
    return kind, qty, sum(_teu(box) * n for box, n in by_type.items())


#: a route whose service is only customs or only a warehouse is that service in her files
SCOPE_AS_MODE = ("Customs only", "Warehousing")
#: a mode word that says nothing: the cell is TBC
NO_MODE = ("Not specified",)


def _modes(case, routes) -> list:
    """The route modes in alphabetical order, each once ('Air & LCL', as her
    files write them; FX5); a Customs only or Warehousing route is that
    word (no FCL, Air or LCL columns asked for it)."""
    modes = [r["service_scope"] if r["service_scope"] in SCOPE_AS_MODE else r["service_mode"]
             for r in routes if r["service_mode"] or r["service_scope"] in SCOPE_AS_MODE]
    modes = modes or ([case["service_mode"]] if case["service_mode"] else [])
    return sorted({m for m in modes if m not in NO_MODE}, key=str.casefold)


#: a US address's city: 'Chicago, IL 60640' (the state's code in capitals, then the ZIP)
_US_CITY = re.compile(r"([^,\d|#]+),\s*([A-Z]{2})\s+\d{5}")


def _city_of(address) -> str:
    """'4554 North Broadway, Chicago, IL 60640' -> 'CHICAGO, IL'; '' when the
    address has no 'City, ST 12345' (a spelt-out state, no comma, no ZIP)."""
    for m in _US_CITY.finditer(address or ""):
        city = m.group(1).strip(" .-|\t")
        if city and m.group(2) in db.STATE_CODES["USA"]:
            return "{}, {}".format(city, m.group(2)).upper()
    return ""


def _place(loc, alt) -> str:
    """The place as OppList writes it; with no place, the city of a US address."""
    return loc or _city_of(alt)


def _one_decimal(value):
    """Her files keep the weight to one decimal (591.167 -> 591.2)."""
    return None if value is None else round(float(value) + 1e-9, 1)


def _first_followup(conn, case_id):
    row = conn.execute("SELECT MIN(at) FROM status_event WHERE enquiry_id = ? AND kind = 'followup' AND voided = 0",
                       (case_id,)).fetchone()
    return _day(row[0]) if row else None


def case_row(conn, case) -> Row:
    """One case's 22 values, and the TBCs it carries."""
    routes = _routes(conn, case["id"])
    modes = _modes(case, routes)
    mode_text = _joined(modes, " & ")
    status = case["status"] or ""
    kind, qty, teu = _containers(conn, case) if "FCL" in modes else (None, None, None)
    side = {}
    for end in ("origin", "dest"):
        # her files: countries joined with ' & ' ('Canada & USA'), places with ' / '
        side[end + "_country"] = _joined([r[end + "_country"] for r in routes] or [case[end + "_country"]], " & ")
        # the place as OppList writes it ('CHICAGO, IL', 'HKG'); an address alone is no place
        side[end + "_loc"] = _joined([_place(r[end + "_loc"], r[end + "_alt"]) for r in routes]
                                     or [_place(case[end + "_loc"], case[end + "_alt"])], " / ")
    types = _joined([SERVICE_TYPES.get(r["direction"] or "", "") for r in routes]
                    or [SERVICE_TYPES.get(case["direction"] or "", "")], " & ")
    values = {
        "Inquiry No.": case["opp_no"], "Date Received": _day(case["received_on"]),
        "Agent": case["requester_key"], "Service Mode": mode_text,
        "Container Type": kind, "Qty": qty, "TEU": teu,
        "Air Weight (kgs)": _one_decimal(case["chargeable_kg"] if case["chargeable_kg"] is not None
                                         else case["air_weight_kg"]) if "Air" in modes else None,
        "LCL Volume (cbm)": case["lcl_cbm"] if "LCL" in modes else None,
        "Origin Country": side["origin_country"], "Origin Detail": side["origin_loc"],
        "Dest. Country": side["dest_country"], "Dest. Detail": side["dest_loc"],
        "Service Type": types,
        "Incoterms": _joined([r["incoterm"] for r in routes] or [case["incoterm"]], " / "),
        "Commodity": " ".join((case["commodity"] or "").split()),
        "Special Handling": "; ".join(t.strip() for t in (case["special_handling"] or "").replace(",", ";").split(";")
                                      if t.strip()) or None,
        "Status": status, "Date Quoted": _day(case["quoted_on"]), "FU Date": _first_followup(conn, case["id"]),
        "Date Closed": _day(case["closed_on"]) if status in CLOSED else None,
        "Loss Reason": (case["closed_reason"] or None) if status in ("Lost", "Cancelled") else None,
    }
    row = Row(case["id"], values)
    applies = {
        "Container Type": "FCL" in modes, "Qty": "FCL" in modes, "TEU": "FCL" in modes,
        "Air Weight (kgs)": "Air" in modes, "LCL Volume (cbm)": "LCL" in modes,
        "Date Quoted": status == "Quoted" or bool(case["quoted_on_any"]),           # once quoted
        "Date Closed": status in CLOSED, "Loss Reason": status == "Lost",
    }
    for col in HEADERS:
        empty = values[col] in (None, "")
        if empty and (col in ALWAYS or applies.get(col)):
            values[col] = TBC
            row.tbc.append((col, FILL[col]))
    return row


def month_cases(conn, month: str) -> list:
    """The month's cases in scope, not Dropped, not a duplicate or
    registered in error, not handled by another office, by Inquiry No."""
    return conn.execute(
        "SELECT * FROM v_enquiry WHERE substr(received_on, 1, 7) = ? AND scope = 'In' "
        "AND COALESCE(status, '') <> 'Dropped' AND COALESCE(TRIM(handled_by), '') = '' "
        "AND COALESCE(closed_reason, '') <> 'Registered in error' AND COALESCE(closed_reason, '') NOT LIKE 'Duplicate%' "
        "ORDER BY CAST(substr(opp_no, 5) AS INTEGER), opp_no", (month,)).fetchall()


def rows(conn, month: str) -> list:
    return [case_row(conn, c) for c in month_cases(conn, month)]


def tbc_list(conn, month: str) -> list:
    """[{case_id, opp, column, fill}] for the Report page."""
    out = []
    for r in rows(conn, month):
        for col, what in r.tbc:
            out.append({"case_id": r.case_id, "opp": r.values["Inquiry No."], "column": col, "fill": what})
    return out


def file_name(today) -> str:
    return "OppList_Inquiries_{}.xlsx".format(today.isoformat() if hasattr(today, "isoformat") else today)


# ------------------------------------------------------------- the sheet --
NAVY, GREY_TEXT, MUTED, LIGHT, LINE = "1F3864", "3D4650", "6A7178", "EDEDED", "D9D9D9"
WIDTHS = {"A": 11.0, "B": 14.0, "C": 22.0, "D": 12.0, "E": 15.0, "F": 8.5703125, "G": 7.0, "H": 10.0, "J": 13.0,
          "K": 20.0, "L": 13.0, "M": 22.0, "N": 15.0, "O": 10.0, "P": 30.0, "Q": 16.0, "R": 9.0, "S": 11.0,
          "V": 18.0}
NUMBER_FORMATS = {"Qty": "#,##0", "TEU": "#,##0", "Air Weight (kgs)": "#,##0.0", "LCL Volume (cbm)": "#,##0.00"}
DATE_FORMAT = "mm/dd/yyyy"


def build(conn, month: str, today) -> bytes:
    """The xlsx, as bytes."""
    from openpyxl import Workbook
    from openpyxl.formatting.rule import FormulaRule
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    data = rows(conn, month)
    first, last = 4, 3 + max(len(data), 1)
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET
    thin = Side(style="thin", color="FF" + LINE)
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    grey = PatternFill(fill_type="solid", fgColor="FF" + LIGHT)
    year, mon = int(month[:4]), int(month[5:7])
    ws["A1"] = "Inquiries, {} {}".format(dt.date(year, mon, 1).strftime("%B"), year)
    ws["A1"].font = Font(name="Arial", size=13, bold=True, color="FF" + NAVY)
    ws["E1"] = "As of {}".format(today.strftime("%m/%d/%Y"))
    ws["E1"].font = Font(name="Arial", size=10, italic=True, color="FF" + MUTED)
    # row 2: the totals
    for ci in range(1, 23):
        cell = ws.cell(2, ci)
        cell.font = Font(name="Arial", size=10, bold=True, color="FF" + (GREY_TEXT if ci == 1 else NAVY))
        cell.fill = grey
        cell.border = border
    ws["A2"] = "Total"
    ws["B2"] = "=SUBTOTAL(103,A{}:A{})".format(first, last)
    ws["B2"].number_format = '0" inquiries"'
    ws["B2"].alignment = Alignment(horizontal="left")
    for letter, col in zip("FGHI", ("Qty", "TEU", "Air Weight (kgs)", "LCL Volume (cbm)")):
        cell = ws["{}2".format(letter)]
        cell.value = "=SUBTOTAL(109,{0}{1}:{0}{2})".format(letter, first, last)
        cell.number_format = NUMBER_FORMATS[col]
        cell.alignment = Alignment(horizontal="right")
    # row 3: the headers
    for ci, head in enumerate(HEADERS, 1):
        cell = ws.cell(3, ci, head)
        cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFFFF")
        cell.fill = PatternFill(fill_type="solid", fgColor="FF" + NAVY)
        cell.border = border
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    # the cases
    for ri, row in enumerate(data, first):
        for ci, head in enumerate(HEADERS, 1):
            value = row.values[head]
            cell = ws.cell(ri, ci, value)
            cell.font = Font(name="Arial", size=10)
            cell.border = border
            if head in DATE_COLS:
                cell.number_format = DATE_FORMAT
                cell.alignment = Alignment(horizontal="center", vertical="top")
            elif head in NUMBER_FORMATS:
                cell.number_format = NUMBER_FORMATS[head]
                cell.alignment = Alignment(horizontal="right", vertical="top")
            else:
                cell.alignment = Alignment(horizontal="left", vertical="top")
            if head == "TEU":
                cell.fill = grey
    for letter, width in WIDTHS.items():
        ws.column_dimensions[letter].width = width
    ws.row_dimensions[1].height = 16.5
    ws.row_dimensions[3].height = 39.75
    ws.freeze_panes = "B4"
    ws.auto_filter.ref = "A3:V{}".format(last)
    ws.sheet_view.showGridLines = False
    status = "R{}:R{}".format(first, last)
    ws.conditional_formatting.add(status, FormulaRule(formula=['R{}="Won"'.format(first)],
                                                      font=Font(bold=True, color="FF2E7D32")))
    ws.conditional_formatting.add(status, FormulaRule(formula=['R{}="Lost"'.format(first)],
                                                      font=Font(bold=False, color="FF9B2C3C")))
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins.header = 0.511811023622047
    ws.page_margins.footer = 0.511811023622047
    ws.print_title_rows = "3:3"
    ws.print_area = "A1:V{}".format(last)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()

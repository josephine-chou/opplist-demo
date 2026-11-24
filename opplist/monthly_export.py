"""The month report as files: {MONTH} REPORT.html / .pdf / .xlsx in
docs\\reports.

The HTML is the report page's own markup (the five pages, without the
check box) with the stylesheet inlined; the PDF is that file printed by
Edge headless; the xlsx is one Report sheet with the KPIs and the eighteen
sections. Nothing here queries the database - monthly.build()
does, once.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import favicon, monthly, paths

TEMPLATES = Path(__file__).resolve().parent / "web" / "templates"

#: The report's stylesheet (sample v11, batch 17a). `{p}` is a prefix for the page (".mreport ")
#: so the app's own styles are left alone; the file uses it bare.
CSS = """
{body}{{margin:0;background:#eee;font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:#0B0B0B;font-size:13.5px}}
{p}.page{{width:297mm;height:210mm;box-sizing:border-box;background:#FCFCFB;margin:8mm auto;padding:13mm 15mm;overflow:hidden;position:relative;page-break-after:always}}
{p}.page:last-child{{page-break-after:auto}}
{p}h1{{font-size:23px;margin:0 0 24px;letter-spacing:.5px;text-transform:none;color:#0B0B0B}} {p}h1 small{{font-size:11.5px;font-weight:normal;color:#52514E;margin-left:12px;letter-spacing:0}}
{p}h2{{font-size:15px;margin:0 0 10px;text-transform:none;letter-spacing:0;color:#0B0B0B}} {p}h2 small{{font-weight:normal;color:#52514E;font-size:10.5px;margin-left:6px}}
{p}h2 .sn{{display:inline-block;min-width:14px;padding-right:5px}}
{p}.kpis{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:0 0 28px}}
{p}.kpi{{border:1px solid #E1E0D9;border-radius:6px;padding:10px 14px}} {p}.kpi b{{display:block;font-size:26px;font-weight:600;line-height:1.1;margin-bottom:2px}} {p}.kpi span{{color:#52514E;font-size:11.5px}} {p}.kpi small{{display:block;color:#52514E;font-size:10px;margin:-2px 0 3px}}
{p}.cols2{{display:grid;grid-template-columns:1fr 1fr;gap:40px;align-items:start}} {p}.cols2>div{{min-width:0}}
{p}table{{border-collapse:collapse;width:100%;margin-bottom:34px;font-size:13.5px}} {p}th,{p}td{{padding:5px 8px;border-bottom:1px solid #E1E0D9;text-align:left;vertical-align:top;white-space:nowrap}}
{p}th{{color:#52514E;font-weight:600;font-size:11.5px;white-space:normal;line-height:1.15;vertical-align:bottom;border-bottom:1px solid #E1E0D9}} {p}td.num,{p}th.num{{text-align:right;font-variant-numeric:tabular-nums}}
{p}tr.total td{{font-weight:600;border-top:1px solid #C3C2B7}} {p}tr.sub td{{font-weight:600}}
{p}tr.head td{{font-weight:600;color:#52514E;font-size:10.5px;background:#F3F2EE;padding-top:4px}}
{p}tr.note td{{color:#3d6384;font-size:10.5px;padding-top:6px;border-bottom:0}}
{p}tr.indent td:first-child{{padding-left:18px}}
{p}table.mid{{font-size:12.5px;margin-bottom:22px}} {p}table.mid td,{p}table.mid th{{padding:3px 8px}} {p}table.mid.s1{{margin-bottom:16px}}
{p}.hbars{{display:flex;flex-direction:column;gap:7px;margin-top:4px}}
{p}.hb{{display:grid;grid-template-columns:185px 1fr 26px;gap:8px;align-items:center;font-size:12.5px}}
{p}.hb-t{{height:10px;background:#ECEBE6;border-radius:5px;overflow:hidden}}
{p}.hb-f{{display:block;height:100%;border-radius:5px}}
{p}.hb-n{{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}}
{p}.lostnotes{{margin-top:12px;font-size:11px;line-height:1.4;color:#2b2b2a}} {p}.lostnotes .ln-h{{font-weight:600;font-size:11.5px;margin-bottom:3px}} {p}.lostnotes .ln{{margin:0 0 4px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}} {p}.lostnotes .ln-k{{color:#52514E}} {p}.lostnotes .ln-more{{color:#52514E}}
{p}.none{{color:#52514E;font-size:11.5px;margin:0 0 12px}}
{p}.chartbox svg{{width:100%;height:auto;display:block}} {p}.chartbox{{margin-bottom:14px}}
{p}.glegend{{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:10.5px;color:#52514E;margin:-6px 0 0}}
{p}.glegend i{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}}
{p}.chart .ax{{font-size:10.5px;fill:#52514E}}
{p}table.cmp{{font-size:12.5px;margin-bottom:0}} {p}table.cmp td,{p}table.cmp th{{padding:3px 8px}} {p}td.up{{color:#3f7f1f}} {p}td.down{{color:#9b2c3c}}
{p}.p2grid{{display:grid;grid-template-columns:1.6fr 1fr;gap:28px;align-items:start}} {p}.p2grid>div{{min-width:0}}
{p}.mapzone{{position:relative}}
{p}.mapbox svg{{width:100%;height:auto;display:block}}
{p}.maptable{{position:absolute;left:19px;bottom:0;width:auto;background:rgba(252,252,251,.88)}}
{p}.maptable table.mini{{font-size:11px;margin-bottom:0;width:auto}} {p}.maptable table.mini td.num,{p}.maptable table.mini th.num{{padding-left:16px}} {p}.maptable table.mini td,{p}.maptable table.mini th{{padding:2px 5px}}
{p}.map text.mlab{{font-size:11px;font-weight:600;fill:#2b3138;paint-order:stroke;stroke:#fff;stroke-width:3px}}
{p}.map .mnum{{fill:#e8456c}}
{p}td.won,{p}th.won{{color:#4f9a24;font-weight:600}}
{p}table.ta{{font-size:12.5px;margin-bottom:0}} {p}table.ta td,{p}table.ta th{{padding:3px 8px}}
{p}table.ta td:first-child{{width:40%}} {p}table.ta tr.note td{{font-size:11.5px;padding:4px 8px 6px;white-space:normal}}
{p}table.ta td.dir,{p}table.ta th.dir{{color:#3d4650}}
{p}.p3low{{display:grid;grid-template-columns:1.9fr 1fr;gap:30px;align-items:start;margin-top:40px}} {p}.p3low>div{{min-width:0}}
{p}.donutwrap{{display:flex;gap:18px;align-items:center;margin-top:6px}}
{p}.page.dense h1{{margin-bottom:10px}} {p}.page.dense .kpis{{margin-bottom:12px}} {p}.page.dense .kpi{{padding:6px 12px}} {p}.page.dense table.mid.s1{{font-size:11.5px;margin-bottom:8px}} {p}.page.dense table.mid.s1 td,{p}.page.dense table.mid.s1 th{{padding:1px 8px}} {p}.page.dense table.mid.s1 tr.head td{{padding-top:2px;font-size:10px}} {p}.page.dense table.mid.s1 tr.indent td:first-child{{padding-left:18px}} {p}.page.dense table.mid.s1 tr.note td{{padding-top:3px}} {p}.page.dense h2{{margin-bottom:6px}} {p}.page.dense .hbars{{gap:3px}} {p}.page.dense .hb{{font-size:11.5px}} {p}.page.dense .lostnotes{{margin-top:6px}} {p}.page.dense .lostnotes .ln{{margin-bottom:2px}}
{p}.page.o6 .p2grid table.mid{{margin-bottom:12px}}
{p}table.mid.s1.m5,{p}.page.dense table.mid.s1.m5{{font-size:11.5px}} {p}table.mid.s1.m5 td,{p}table.mid.s1.m5 th,{p}.page.dense table.mid.s1.m5 td,{p}.page.dense table.mid.s1.m5 th{{padding-left:5px;padding-right:5px}}
{p}table.mid.s1.m6,{p}.page.dense table.mid.s1.m6{{font-size:10.5px}} {p}table.mid.s1.m6 td,{p}table.mid.s1.m6 th,{p}.page.dense table.mid.s1.m6 td,{p}.page.dense table.mid.s1.m6 th{{padding-left:3px;padding-right:3px}} {p}table.mid.s1.m6 th{{font-size:10.5px}}
{p}table.mid.s1.m5 tr.indent td:first-child,{p}table.mid.s1.m6 tr.indent td:first-child,{p}.page.dense table.mid.s1.m5 tr.indent td:first-child,{p}.page.dense table.mid.s1.m6 tr.indent td:first-child{{padding-left:12px}}
{p}.page.dc .p3low{{margin-top:20px}} {p}.page.dc table.ta td,{p}.page.dc table.ta th{{padding:2px 8px}} {p}.page.dc .p3low .chartbox svg{{max-height:165px}}
{p}.page.dense .g{{gap:8px;margin-bottom:6px}} {p}.page.dense .card{{padding:6px 12px 4px}} {p}.page.dense .hl b{{font-size:22px}} {p}.page.dense ul.kp{{font-size:11px;line-height:1.3;margin-top:3px;padding-top:3px}}
{p}.page.dense .p3low{{margin-top:16px}} {p}.page.dense table.ta td,{p}.page.dense table.ta th{{padding:2px 8px}} {p}.page.dense table.ta tr.note td{{font-size:10.5px;line-height:1.3}} {p}.page.dense .p3low .chartbox svg{{max-height:150px}}
{p}.page.dc4 h1{{margin-bottom:12px}} {p}.page.dc4 h2{{margin-bottom:6px}} {p}.page.dc4 .p3low{{margin-top:12px}} {p}.page.dc4 table.ta td,{p}.page.dc4 table.ta th{{padding:1px 8px}} {p}.page.dc4 table.ta tr.head td{{padding-top:1px}} {p}.page.dc4 table.ta td,{p}.page.dc4 table.ta th{{line-height:1.2}} {p}.page.dc4 .p3low .chartbox svg{{max-height:none}}
{p}.donut .dn{{font-size:23px;font-weight:600;fill:#0B0B0B}}
{p}.donut .dl{{font-size:11px;fill:#52514E}}
{p}.dlegend div{{display:flex;justify-content:space-between;gap:12px;font-size:12.5px;padding:3px 0;min-width:160px}}
{p}.dlegend i{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}}
{p}.g{{display:grid;gap:10px;align-items:stretch;margin-bottom:10px}} {p}.g>div{{min-width:0}}
{p}.g1{{grid-template-columns:0.82fr 1.18fr}} {p}.g2{{grid-template-columns:1.32fr 0.68fr}} {p}.g3{{grid-template-columns:1fr 1fr}} {p}.g4{{grid-template-columns:1.2fr 0.8fr}} {p}.g5{{grid-template-columns:1fr}} {p}.g6{{grid-template-columns:1.08fr 0.92fr}} {p}.g7{{grid-template-columns:1.26fr 0.74fr}}
{p}.card{{border:1px solid #E1E0D9;border-radius:8px;background:#fff;padding:8px 13px 6px}}
{p}.card h3{{margin:0;font-size:12.5px;font-weight:600;color:#52514E;letter-spacing:.2px}} {p}.card h3 .cn{{color:#898781;margin-right:6px;font-weight:600}}
{p}.hl{{display:flex;align-items:baseline;gap:8px;margin:1px 0 4px;flex-wrap:wrap}} {p}.hl b{{font-size:27px;font-weight:600;color:#0B0B0B;line-height:1.15}}
{p}.hl .cap{{font-size:11.5px;color:#52514E}}
{p}.pill{{font-size:10.5px;padding:1px 7px;border-radius:9px;white-space:nowrap}} {p}.pill.up{{background:#e7f2df;color:#2f6b14}} {p}.pill.down{{background:#f6e3e7;color:#8a2338}}
{p}.pill.flat{{background:#efeee9;color:#52514E}} {p}.pill.warn{{background:#fdf1d6;color:#7a5200}}
{p}.card svg{{display:block;max-width:100%;height:auto}}
{p}.lg{{display:flex;flex-wrap:wrap;gap:3px 12px;font-size:10.5px;color:#52514E;margin:4px 0 2px}} {p}.lg i{{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px;vertical-align:-1px}}
{p}ul.kp{{margin:5px 0 0;padding:5px 0 0 15px;border-top:1px solid #EFEEE9;font-size:11.5px;line-height:1.4;color:#2b2b2a}} {p}ul.kp li{{margin-bottom:1px}} {p}ul.kp b{{color:#0B0B0B;font-weight:600}}
{p}.card .sub{{font-size:10.5px;color:#52514E;margin:4px 0 1px}}
{p}.dots{{display:flex;gap:16px;align-items:flex-end;flex-wrap:nowrap}}
{p}.dwrap{{display:flex;align-items:center;gap:18px;margin:4px 0}}
{p}.precheck{{width:297mm;box-sizing:border-box;margin:8mm auto 0;padding:10px 15mm 12px;background:#fff;border:1px solid #f0d58a;border-radius:6px}}
{p}.precheck h2{{font-size:14px;margin:0 0 8px}} {p}.precheck h2 small{{font-size:10.5px}}
{p}.pc-g{{font-size:12px;margin:0 0 6px}} {p}.pc-n{{display:inline-block;min-width:18px;padding:0 5px;border-radius:9px;background:#f6e3b0;color:#6b5200;font-size:10.5px;text-align:center;margin-left:4px}}
{p}.pc-l{{display:flex;flex-wrap:wrap;gap:2px 16px;margin-top:2px;color:#2b2b2a}} {p}.pc-i em{{color:#6a7178;font-style:normal;font-size:11px}} {p}.pc-i a{{color:#2A78D6;text-decoration:none}}
{p}.pc-ok{{color:#4f9a24;font-size:12px;margin:0}}
@media print{{ {body}{{background:#fff}} {p}.page{{margin:0}} {p}.precheck{{display:none}} @page{{size:A4 landscape;margin:0}} }}
"""


def css(prefix: str = "") -> str:
    """The stylesheet, bare for the file or scoped under `.mreport` on the page."""
    p = prefix + " " if prefix else ""
    body = prefix if prefix else "body"
    return CSS.format(p=p, body=body).strip()


def _env() -> Environment:
    return Environment(loader=FileSystemLoader(str(TEMPLATES)),
                       autoescape=select_autoescape(["html"]))


def fragments(report: monthly.Monthly, amap=None) -> dict:
    """The SVG and list pieces the body drops in, built once for the page
    and the file (dashboard's helpers, monthly's colours)."""
    from . import dashboard
    colours = monthly.colours()
    weeks = report.weeks
    return {
        "chart": monthly.chart_svg(report.per_month["labels"], report.per_month["latam"], report.per_month["row"]),
        "bars": dashboard.hbar_list(report.lost_bars, lambda label: monthly.MUTED if label == "Other reasons"
                                    else monthly.LOST_COLOUR) if report.lost_bars else "",
        "map": dashboard.map_svg(report.map_rows, amap),
        # 17a-2: 180 high with one extra group in section 9; 17i: 130 with both
        "weeks": dashboard.line_chart(weeks["labels"], weeks["series"], colours, w=640,
                                      h={"": 200, " dc": 180}.get(monthly.page3_class(report), 130),
                                      last_anchor_end=True) if weeks.get("labels") else "",
        "week_legend": "".join('<span><i style="background:{}"></i>{}</span>'.format(colours[name], name)
                               for name in weeks.get("series", {})),
        "donut": dashboard.donut(report.mix, size=120, thick=20, centre_label="requests")
        if monthly.page3_class(report) == " dc dc4" else
        dashboard.donut(report.mix, size=150, thick=23, centre_label="requests"),
        "donut_legend": dashboard.legend(report.mix),
    }


def render_html(report: monthly.Monthly) -> str:
    """The file: the five pages, never the check box."""
    return _env().get_template("monthly_standalone.html").render(
        report=report, frag=fragments(report), css=css(), precheck=False,
        favicon=favicon.data_uri())


def out_dir_for(out_dir=None) -> Path:
    out_dir = Path(out_dir or paths.REPORTS_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def write_html(report: monthly.Monthly, out_dir=None, record=None) -> Path:
    path = out_dir_for(out_dir) / "{}.html".format(monthly.file_stem(report.month))
    path.write_text(render_html(report), encoding="utf-8")
    # W1a: the team site takes this month's report at its next push
    try:
        from . import firebase_sync
        firebase_sync.record_export(report.month, report.title, path, record=record)
    except OSError:
        pass
    return path


# --------------------------------------------------------------- the PDF --
class EdgeNotFound(RuntimeError):
    pass


def find_edge() -> Path | None:
    """msedge.exe from the App Paths registry key, else the usual folders."""
    if sys.platform == "win32":
        try:
            import winreg

            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion"
                                              r"\App Paths\msedge.exe") as key:
                        value, _ = winreg.QueryValueEx(key, "")
                        if value and Path(value).exists():
                            return Path(value)
                except OSError:
                    continue
        except ImportError:
            pass
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            candidate = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if candidate.exists():
                return candidate
    return None


def write_pdf(report: monthly.Monthly, out_dir=None, edge=None, timeout=90) -> Path:
    """Print the HTML to PDF with Edge headless. Raises EdgeNotFound."""
    edge = Path(edge) if edge else find_edge()
    if edge is None:
        raise EdgeNotFound("Microsoft Edge was not found (App Paths, Program Files); "
                           "the PDF needs it. The HTML file can be printed from a browser.")
    out_dir = out_dir_for(out_dir)
    html_path = write_html(report, out_dir)
    pdf_path = out_dir / "{}.pdf".format(monthly.file_stem(report.month))
    with tempfile.TemporaryDirectory(prefix="opplist-edge-") as profile:
        subprocess.run(
            [str(edge), "--headless=new", "--disable-gpu", "--no-first-run",
             "--user-data-dir={}".format(profile), "--no-pdf-header-footer",
             "--print-to-pdf={}".format(pdf_path), html_path.resolve().as_uri()],
            check=True, timeout=timeout, capture_output=True)
    if not pdf_path.exists():
        raise RuntimeError("Edge returned but wrote no PDF at {}".format(pdf_path))
    return pdf_path


# -------------------------------------------------------------- the xlsx --
def write_xlsx(report: monthly.Monthly, out_dir=None) -> Path:
    """One Report sheet: the KPIs, then the eighteen sections as tables, one
    under another, in the report's order."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    path = out_dir_for(out_dir) / "{}.xlsx".format(monthly.file_stem(report.month))
    wb = Workbook()
    ws = wb.active
    ws.title = "Report"
    ws.cell(row=1, column=1, value=report.title).font = Font(bold=True, size=14)
    row = 3
    for value, label in report.kpis:
        ws.cell(row=row, column=1, value=label)
        ws.cell(row=row, column=2, value=value)
        if report.kpi_notes.get(label):
            ws.cell(row=row, column=3, value=report.kpi_notes[label])
        row += 1
    row += 1
    for number, title, table in monthly.tables(report):
        ws.cell(row=row, column=1, value="{}. {}".format(number, title)).font = Font(bold=True)
        row += 1
        for index, name in enumerate(table.columns, start=1):
            ws.cell(row=row, column=index, value=name).font = Font(italic=True)
        row += 1
        for line in table.rows:
            ws.cell(row=row, column=1, value=line.label).font = Font(
                bold=line.cls in ("sub", "total", "head"))
            for index, cell in enumerate(line.cells, start=2):
                ws.cell(row=row, column=index, value=_number(cell))
            row += 1
        row += 1
    ws.column_dimensions["A"].width = 40
    for letter in "BCDEFGHIJK":
        ws.column_dimensions[letter].width = 13
    wb.save(path)
    return path


def _number(text):
    """'1,529' -> 1529, '7.9' -> 7.9, '' -> None, anything else as is."""
    if text in ("", None):
        return None
    try:
        cleaned = str(text).replace(",", "")
        return int(cleaned) if cleaned.lstrip("-").isdigit() else float(cleaned)
    except ValueError:
        return text


def write(conn: sqlite3.Connection, month: str, kinds=("html",), out_dir=None, today=None) -> dict:
    # 17j: the running month closes on today
    report = monthly.build(conn, month, today or dt.date.today())
    written = {}
    for kind in kinds:
        if kind == "html":
            written["html"] = write_html(report, out_dir)
        elif kind == "pdf":
            written["pdf"] = write_pdf(report, out_dir)
        elif kind == "xlsx":
            written["xlsx"] = write_xlsx(report, out_dir)
    return written

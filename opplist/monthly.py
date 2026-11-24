"""The month report (batch 17a, 2026-10-07): three A4 landscape
pages as sample v11 - Overview, Regions, Agents; 17f adds a fourth,
Analysis · Agents (sections 12 to 14, monthly_analysis), 17g a fifth,
Analysis · Requests (sections 15 to 18, monthly_requests).

One base for every count (except the lane sections, which count routes):

    cases     SC1 (2026-11-10): direct_customers_out.counted_sql, the one
              definition every report reads - scope In, not Dropped (a
              dropped row should not exist), the requester's party type is
              Agent, Office or Direct Customer (anybody else -
              Unknown, a placeholder, a carrier - is on no page and in no
              total; the check box lists them), and the report rule leaves
              it in (a direct customer before October is out; from October
              a direct customer's or an office's case that is not LATAM is
              out). Cases another of our offices handles count like any
              other; the note under section 1 says how many. The month is
              by received_on.
    LATAM     v_enquiry.latam (her choice when she made one; else the lane
              touches a country of the Settings list, or the requester is a
              LATAM agent or customer); else Rest of world
    groups    section 1: LATAM agents, MFN / LATAM agents, not MFN /
              Other agents / Our offices / Direct customers;
              sections 10 and 11 fold them into four
    Won       status Won;  Lost status Lost;  Cancelled is in neither
    modes     SC1: section 1 has a column per mode - Air, Sea-Air, FCL, LCL,
              RORO, Truck, Cross-border Truck, Other (Project, Break Bulk,
              Multi-mode, and a first route whose service is Customs only
              or Warehousing, whatever its mode) - Air, FCL and LCL always,
              the others in a month that has one; a blank mode or Not
              specified counts in Total only. Elsewhere Ocean = FCL + LCL +
              RORO, Other = Sea-Air, Truck, Cross-border Truck and Other.
              A quote-both case (live routes of more than one mode) counts
              under its Won route's mode; without one, route A's.

The page, the HTML file, the PDF and the xlsx all read one Monthly object,
so a number is the same number everywhere.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
import sqlite3
from dataclasses import dataclass, field

from . import direct_customers_out as dco, words

# Dropped = the row should not exist, so it is out of the denominator
# altogether (2026-09-18). SC1: another office's case counts too.
REAL = "e.scope = 'In' AND e.status <> 'Dropped'"
#: 17a: only requesters the report can name a group for
TYPED = "p.type IN ('Agent', 'Office', 'Direct Customer')"
#: SC1: the report rule (direct_customers_out), the one definition
CASES = dco.counted_sql("e", "p")
# Section 3 asks how much came in, whatever became of it: a legacy row is
# Dropped only because the workbook never recorded how it ended, so the line
# keeps it; a duplicate or a mis-registration is still out.
VOLUME = dco.counted_sql("e", "p", volume=True)
#: the same for the lane sections, which read v_route (the route's
#: countries, so the rule reads the case's)
R_CASES = "e.enquiry_id IN " + dco.counted_ids_sql()
LATAM = "(e.latam = 'Y')"
#: SC1: was 'Non-LATAM agents'; the colour stays
OTHER_AGENTS = "Other agents"
GROUP = """CASE
  WHEN p.type = 'Office' THEN 'Our offices'
  WHEN e.latam_agent = 'Y' AND p.mfn_member = 1 THEN 'LATAM agents, MFN'
  WHEN e.latam_agent = 'Y' THEN 'LATAM agents, not MFN'
  WHEN p.type = 'Agent' THEN 'Other agents'
  ELSE 'Direct customers' END"""
GROUPS = ("LATAM agents, MFN", "LATAM agents, not MFN", OTHER_AGENTS, "Our offices",
          "Direct customers")
#: sections 10 and 11: the five folded into four, with their colours
FOUR = (("LATAM agents", "#e8456c"), (OTHER_AGENTS, "#83cd48"), ("Our offices", "#00b3ea"),
        ("Direct customers", "#f2a93b"))
FOLD = {"LATAM agents, MFN": "LATAM agents", "LATAM agents, not MFN": "LATAM agents",
        OTHER_AGENTS: OTHER_AGENTS, "Our offices": "Our offices",
        "Direct customers": "Direct customers"}
WON = "e.status = 'Won'"
LOST = "e.status = 'Lost'"


# 17j (2026-10-21): a month's report closes on its last day. Every outcome
# in it - Quoted, Won, Lost, Cancelled and the closing reason - is the
# case's state on that day, read from its timeline as
# monthly_requests.status_on reads it (the latest live status event dated
# on or before the day; New with none). For the month still running the
# day is today. Corrections are not outcomes and apply to every month
# whenever they are made: REAL, TYPED and VOLUME keep reading the case as
# it is now (Dropped as a duplicate or in error, scope, handled_by, the
# requester, mode and lanes). The Report page's check box stays live.
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ON_ORDER = ("s.at DESC, CASE WHEN s.kind = 'requote' THEN 3 WHEN s.status = 'New' THEN 1 "
             "WHEN s.status = 'Sourcing' THEN 2 WHEN s.status = 'Quoted' THEN 3 ELSE 4 END DESC, s.id DESC")


def month_end(month) -> dt.date:
    year, mon = int(month[:4]), int(month[5:7])
    return dt.date(year, mon, calendar.monthrange(year, mon)[1])


def close_day(month, today=None) -> str:
    """The day the month's report closes on: its last day, or today while
    the month is still running (no today given: the last day)."""
    end = month_end(month)
    if today is not None:
        today = today if isinstance(today, dt.date) else dt.date.fromisoformat(str(today)[:10])
        end = min(end, today)
    return end.isoformat()


def _event_on(day, column, case) -> str:
    if not _DAY.match(str(day)):
        raise ValueError("not a day: {!r}".format(day))
    return ("(SELECT s.{col} FROM status_event s WHERE s.enquiry_id = {case} AND s.voided = 0 "
            "AND s.kind IN ('status','requote') AND substr(s.at, 1, 10) <= '{day}' ORDER BY {order} LIMIT 1)"
            ).format(col=column, case=case, day=day, order=_ON_ORDER)


def status_on_sql(day, case="e.id") -> str:
    """SQL: the case's status on the day (New when no status event by then)."""
    return "COALESCE({}, 'New')".format(_event_on(day, "status", case))


def reason_on_sql(day, case="e.id", current="e") -> str:
    """SQL: the reason on the event that sets the status on the day; when
    it has none and that status is the case's status now, the case's
    closed_reason. `current` is the alias whose status and closed_reason
    are the case's own (a v_enquiry row)."""
    return ("COALESCE(NULLIF(TRIM({ev}), ''), CASE WHEN {st} = {cur}.status THEN {cur}.closed_reason END)"
            ).format(ev=_event_on(day, "reason", case), st=status_on_sql(day, case), cur=current)


def won_on(day, case="e.id") -> str:
    return "({} = 'Won')".format(status_on_sql(day, case))


def lost_on(day, case="e.id") -> str:
    return "({} = 'Lost')".format(status_on_sql(day, case))


def cancelled_on(day, case="e.id") -> str:
    return "({} = 'Cancelled')".format(status_on_sql(day, case))


def quoted_on(day, case="e.id") -> str:
    """17k: a quote still without an answer on the day."""
    return "({} = 'Quoted')".format(status_on_sql(day, case))
#: 17a-2: the services that are not a lane with a mode (Settings' service_scopes)
OTHER_SERVICES = ("Customs only", "Warehousing")
#: a case's service: its first live route's (v_enquiry carries route A's mode only)
CASE_SERVICE = ("LOWER(TRIM(COALESCE((SELECT r.service_scope FROM enquiry_route r WHERE r.enquiry_id = e.id "
                "AND r.voided = 0 ORDER BY r.seq LIMIT 1), '')))")
#: a route's own service (v_route)
ROUTE_SERVICE = "LOWER(TRIM(COALESCE(e.service_scope, '')))"
_SERVICES_SQL = "('customs only', 'warehousing')"
#: SC1: section 1's mode columns in their order; the first three always,
#: the others in a month that has a case of them. Other is Project, Break
#: Bulk, Multi-mode, and the two services whatever the mode
MODE_COLUMNS = ("Air", "Sea-Air", "FCL", "LCL", "RORO", "Truck", "Cross-border Truck", "Other")
ALWAYS_COLUMNS = ("Air", "FCL", "LCL")
#: where a figure is Air / Ocean / Other (the KPI, section 6, section 8)
OCEAN_MODES = ("FCL", "LCL", "RORO")
OTHER_MODES = ("Sea-Air", "Truck", "Cross-border Truck", "Other")
#: SC1: the mode a case counts under. A quote-both case (its live routes
#: have more than one mode) with a Won route counts under that route's
#: mode; any other case under route A's (v_enquiry's service_mode)
CASE_MODE = ("COALESCE((SELECT w.service_mode FROM enquiry_route w WHERE w.enquiry_id = e.id AND w.voided = 0 "
             "AND w.result = 'Won' AND (SELECT COUNT(DISTINCT COALESCE(m.service_mode, '')) FROM enquiry_route m "
             "WHERE m.enquiry_id = e.id AND m.voided = 0) > 1 ORDER BY w.seq LIMIT 1), e.service_mode)")


def _column(service, mode) -> str:
    """SQL: the MODE_COLUMNS name a case or a route counts under, '' for a
    blank mode or Not specified."""
    return ("(CASE WHEN {svc} IN {services} THEN 'Other' "
            "WHEN {mode} IN ({own}) THEN {mode} "
            "WHEN COALESCE({mode}, '') IN ('', 'Not specified') THEN '' ELSE 'Other' END)").format(
        svc=service, services=_SERVICES_SQL, mode=mode,
        own=", ".join("'{}'".format(m) for m in MODE_COLUMNS[:-1]))


def _not_service(service) -> str:
    return "{} NOT IN {}".format(service, _SERVICES_SQL)


def _in(column, modes) -> str:
    return "({} IN ({}))".format(column, ", ".join("'{}'".format(m) for m in modes))


CASE_COLUMN = _column(CASE_SERVICE, CASE_MODE)
ROUTE_COLUMN = _column(ROUTE_SERVICE, "e.service_mode")
AIR = _in(CASE_COLUMN, ("Air",))
FCL = _in(CASE_COLUMN, ("FCL",))
LCL = _in(CASE_COLUMN, ("LCL",))
OCEAN = _in(CASE_COLUMN, OCEAN_MODES)
#: the KPI's Other (and the cheat sheet's): any mode that is neither Air nor Ocean
OTHER = _in(CASE_COLUMN, OTHER_MODES)
#: the same for a route (sections 6 to 8 count routes, each under its own mode)
R_AIR = _in(ROUTE_COLUMN, ("Air",))
R_OCEAN = _in(ROUTE_COLUMN, OCEAN_MODES)
R_OTHER = _in(ROUTE_COLUMN, OTHER_MODES)
#: 17a-2: section 4's win rate and section 9's third group
DIRECT = "p.type = 'Direct Customer'"
AGENTS_OFFICES = "p.type IN ('Agent', 'Office')"
#: section 9: the direct customers shown
TOP_DIRECT = 3
#: 17i: section 9's Our offices group, the offices shown
OFFICE = "p.type = 'Office'"
TOP_OFFICES = 3
#: section 4's two rows
WHICH_DIRECT = "of which direct customers"
#: section 6's columns: the stored direction and its heading
DIRECTIONS = (("US Export", "Export"), ("US Import", "Import"), ("Cross-trade", words.direction_label("Cross-trade")))

BASE = "FROM v_enquiry e JOIN party p ON p.id = e.requester_party_id"
#: v33: the lane sections count routes (v_route), one row per
#: live route with its case's month, scope, status and requester
RBASE = "FROM v_route e JOIN party p ON p.id = e.requester_party_id"

LATAM_COLOUR = "#EB6834"
ROW_COLOUR = "#2A78D6"
LOST_COLOUR = "#b3405a"
MUTED = "#6a7178"
#: section 2: bars at most; past that the last is 'Other reasons'
MAX_BARS = 6
#: section 2's cancelled lines: how many by the number of bars
CANCEL_LINES = {0: 3, 1: 3, 2: 3, 3: 3, 4: 3, 5: 2, 6: 1}
NOTE_CUT = 140
#: section 5's table: rows at most; past that the last is 'Others (n)'
MAX_COUNTRIES = 13
#: page 3 tightens its spacing when the quiet-agents note is longer than
#: this (twenty long names overflowed the page, 847 px of 794)
DENSE_NOTE_CHARS = 160
#: 17g: section 9's quiet row names this many on the page, then 'and n
#: more'; the xlsx and the text keep every name (twenty long names took
#: page 3 to 800 px of 794)
QUIET_NAMES = 12
#: 17e: page 1 is tightened when sections 1 and 2 together pass this many
#: rows. Measured in Edge (2026-10-08): a table row and a lost bar are 23 px
#: each, a line of the cancelled block 15.4 px (2/3 of a row; a note that
#: wraps is two lines). September on the live copy is 18.3 rows at 743 px of
#: the page's 745; one more row of any kind would cross it.
DENSE_OVERVIEW_ROWS = 18.5
#: a cancelled line longer than this wraps to its second line
CANCEL_LINE_CHARS = 80


@dataclass
class Row:
    label: str
    cells: list                 # already formatted strings
    cls: str = ""               # sub / indent / total / head / note
    marks: list = field(default_factory=list)   # per cell: '' / 'up' / 'down' / 'dir' / 'won'
    page: str = ""              # 17g: a head or note row's text on the page when it differs


@dataclass
class Table:
    columns: list               # header labels; the first is the row label
    rows: list = field(default_factory=list)
    marks: list = field(default_factory=list)   # per column after the first: '' / 'dir' / 'won'


@dataclass
class Monthly:
    month: str
    title: str
    month_name: str = ""
    previous_name: str = ""
    kpis: list = field(default_factory=list)          # (value, label)
    kpi_notes: dict = field(default_factory=dict)     # label -> small text under the number (C1)
    by_scope: Table | None = None                     # 1
    lost_bars: list = field(default_factory=list)     # 2: (reason, n)
    cancelled: list = field(default_factory=list)     # 2: dicts opp_no, requester, reason, note
    cancelled_shown: int = 0
    per_month: dict = field(default_factory=dict)     # 3: labels, latam, row
    compare: Table | None = None                      # 4
    map_rows: list = field(default_factory=list)      # 5: (country, {'n', 'won'}) every one
    countries: Table | None = None                    # 5: the table over the map
    direction: Table | None = None                    # 6
    top_lanes: Table | None = None                    # 7
    top_pods: Table | None = None                     # 8
    top_agents: Table | None = None                   # 9
    weeks: dict = field(default_factory=dict)         # 10: labels, spans, series {group: [n]}
    mix: list = field(default_factory=list)           # 11: (group, n, colour)
    checks: list = field(default_factory=list)        # the check box: dicts title, items
    dense_agents: bool = False                        # page 3 tightened: a long quiet-agents note
    dense_overview: bool = False                      # page 1 tightened: sections 1 and 2 run long (17e)
    direct_group: bool = False                        # 17a-2: section 9 has the Direct customers group (page 3 tightened)
    office_group: bool = False                        # 17i: section 9 has the Our offices group
    close_day: str = ""                               # 17j: the day the outcomes are read on

    @property
    def page3_class(self) -> str:
        return page3_class(self)

    @property
    def s1_class(self) -> str:
        return s1_class(self)                         # SC1: section 1's width by its mode columns

    @property
    def win_rate_rule(self) -> str:
        return WIN_RATE_RULE                          # 17k: on section 4's heading
    direction_other: bool = False                     # 17a-2: section 6 has the Other row (page 2 tightened)
    analysis: list = field(default_factory=list)      # 17f: page 4, monthly_analysis.Card for 12, 13, 14
    dense_analysis: bool = False                      # 17f: page 4 tightened: long bullets
    requests: list = field(default_factory=list)      # 17g: page 5, monthly_analysis.Card for 15 to 18
    dense_requests: bool = False                      # 17g: page 5 tightened


def page3_class(report) -> str:
    """17i: page 3's tightening for section 9's extra groups: ' dc' with
    one of the offices and the direct customers, ' dc dc4' with both."""
    extra = int(report.office_group) + int(report.direct_group)
    return " dc dc4" if extra == 2 else (" dc" if extra else "")


# -------------------------------------------------------------- helpers --
def title_for(month: str) -> str:
    return "{} REPORT".format(calendar.month_name[int(month[5:7])].upper())


def file_stem(month: str) -> str:
    return title_for(month)


def default_month(today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    year, mon = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    return "{:04d}-{:02d}".format(year, mon)


def previous_month(month: str) -> str:
    year, mon = int(month[:4]), int(month[5:7])
    year, mon = (year, mon - 1) if mon > 1 else (year - 1, 12)
    return "{:04d}-{:02d}".format(year, mon)


def _n(value) -> str:
    """Whole numbers with thousands separators; a zero is a blank."""
    if not value:
        return ""
    value = float(value)
    if value.is_integer():
        return "{:,}".format(int(value))
    return "{:,.1f}".format(value)


def _cbm(value) -> str:
    """Two significant digits: 13.823 reads 14, 7.9 stays 7.9; a zero is a blank."""
    if not value:
        return ""
    text = "{:.2g}".format(float(value))
    return text if "e" not in text else _n(value)


def _count(conn, where, params) -> int:
    return conn.execute("SELECT COUNT(*) {} WHERE {}".format(BASE, where), params).fetchone()[0]


def _order_by_case():
    return "MIN(CAST(substr(e.opp_no, 5) AS INTEGER))"


# ----------------------------------------------------------------- KPIs --
def kpis(conn, month, day=None) -> list:
    day = day or close_day(month)
    where = "e.month = ? AND " + CASES
    p = (month,)
    total = _count(conn, where, p)
    latam = _count(conn, where + " AND " + LATAM, p)
    air = _count(conn, where + " AND " + AIR, p)
    ocean = _count(conn, where + " AND " + OCEAN, p)
    other = _count(conn, where + " AND " + OTHER, p)
    won = _count(conn, where + " AND " + won_on(day), p)
    lost = _count(conn, where + " AND " + lost_on(day), p)
    modes = ("{} / {} / {}".format(air, ocean, other), OTHER_LABEL) if other else \
        ("{} / {}".format(air, ocean), "Air / Ocean")
    # 17l: the base by requester, the three types it holds (TYPED); the
    # total is in sections 1 and 4. No direct customer in the base (before
    # October 2026, the 17a-2 rule): two numbers.
    agents = _count(conn, where + " AND p.type = 'Agent'", p)
    direct = _count(conn, where + " AND " + DIRECT, p)
    offices = _count(conn, where + " AND " + OFFICE, p)
    requesters = ("{} / {} / {}".format(agents, direct, offices), REQUESTERS_LABEL) if direct else \
        ("{} / {}".format(agents, offices), "Agents / Our offices")
    return [
        requesters,
        ("{} / {}".format(latam, total - latam), "LATAM / Rest of world"),
        modes,
        (str(won), "Won"),
        (str(lost), "Lost"),
    ]


OTHER_LABEL = "Air / Ocean / Other"
#: 17l: page 1's first tile
REQUESTERS_LABEL = "Agents / Direct customers / Our offices"


def kpi_notes(conn, month, day=None) -> dict:
    """'N cancelled' under Lost (C1), nothing when the month cancelled
    nothing. (17l: no line under Air / Ocean / Other any more.)"""
    cancelled = _count(conn, "e.month = ? AND " + CASES + " AND " + cancelled_on(day or close_day(month)), (month,))
    return {"Lost": "{} cancelled".format(cancelled)} if cancelled else {}


# ------------------------------------------- 1. by scope and requester --
#: 17e: section 1's rows By direction; Domestic only in a month that has one
BY_DIRECTION = DIRECTIONS + (("US Domestic", "Domestic"),)


def by_scope(conn, month, day=None) -> Table:
    """Section 1. 17e: under the Total, the same columns By direction; a
    case with no direction (or To Confirm) counts in Total only. SC1: one
    column per mode (MODE_COLUMNS) - Air, FCL and LCL always, each other
    one only in a month that has a case of it; a blank mode or Not
    specified counts in Total only."""
    day = day or close_day(month)
    cases = conn.execute(
        "SELECT {latam} AS latam, {grp} AS grp, e.direction AS d, {col} AS col, {won} AS won, {lost} AS lost "
        "{base} WHERE e.month = ? AND {cases}".format(
            latam=LATAM, grp=GROUP, col=CASE_COLUMN, won=won_on(day), lost=lost_on(day), base=BASE, cases=CASES),
        (month,)).fetchall()
    present = {c["col"] for c in cases}
    modes = [m for m in MODE_COLUMNS if m in ALWAYS_COLUMNS or m in present]

    def cells(rows):
        won, lost = sum(1 for r in rows if r["won"]), sum(1 for r in rows if r["lost"])
        return [len(rows)] + [sum(1 for r in rows if r["col"] == m) for m in modes] + [won, lost]

    table = Table(["Scope / requester", "Total"] + modes + ["Won", "Lost"])
    grand = [0] * (len(modes) + 3)
    for flag, label in ((1, "LATAM"), (0, "Rest of world")):
        mine = [c for c in cases if c["latam"] == flag]
        subtotal = cells(mine)
        table.rows.append(Row(label, [str(v) for v in subtotal], "sub"))
        for g in GROUPS:
            part = [c for c in mine if c["grp"] == g]
            if part:
                table.rows.append(Row(g, [str(v) for v in cells(part)], "indent"))
        grand = [a + b for a, b in zip(grand, subtotal)]
    table.rows.append(Row("Total", [str(v) for v in grand], "total"))
    table.rows.append(Row("By direction", [], "head"))
    directions = {c["d"] for c in cases}
    for key, label in BY_DIRECTION:
        if key == "US Domestic" and key not in directions:
            continue
        table.rows.append(Row(label, [str(v) for v in cells([c for c in cases if c["d"] == key])], "indent"))
    table.rows.append(Row(handled_elsewhere(conn, month), [], "note"))
    return table


def s1_class(report) -> str:
    """SC1: section 1's width class by its mode columns - '' up to four
    (September 2026's Air, FCL, LCL, Other filled the half page), ' m5'
    with five, ' m6' with six or more (Cross-border Truck's header on two
    lines throughout)."""
    modes = len(report.by_scope.columns) - 4 if report.by_scope else 0
    return "" if modes <= 4 else (" m5" if modes == 5 else " m6")


def overview_rows(report) -> float:
    """17e: how long page 1's left column runs, in table rows: section 1's
    rows, the lost bars (or the 'No lost cases' line), and the cancelled
    block's lines at 2/3 of a row each (DENSE_OVERVIEW_ROWS)."""
    lines = 0
    if report.cancelled:
        shown = report.cancelled[:report.cancelled_shown]
        lines = 1 + (1 if len(report.cancelled) > len(shown) else 0)
        for c in shown:
            text = "{} · {}: {}, {}".format(c["opp_no"], c["requester"], c["reason"], c["note"])
            lines += 2 if len(text) > CANCEL_LINE_CHARS else 1
    return len(report.by_scope.rows) + max(len(report.lost_bars), 1) + lines * 2 / 3


def handled_elsewhere(conn, month) -> str:
    """The note under section 1 (SC1): of the month's counted cases, the
    ones another of our offices handles, by office."""
    from . import db
    return db.handled_elsewhere_line(r[0] for r in conn.execute(
        "SELECT e.handled_by {base} WHERE e.month = ? AND {cases} AND e.handled_by IS NOT NULL".format(
            base=BASE, cases=CASES), (month,)))


# ------------------------------------------------------- 2. why we lost --
def lost_bars(conn, month, day=None) -> list:
    """[(reason, n)] of the month's Lost cases, most first, ties in the
    Settings order; a case with no reason is not a bar (the check box lists
    it); past MAX_BARS the last bar is 'Other reasons' with the rest."""
    from . import status as status_mod
    order = {r.casefold(): i for i, r in enumerate(status_mod.closed_reasons(conn, "Lost"))}
    counts = {}
    day = day or close_day(month)
    for r in conn.execute(
            "SELECT TRIM(reason) AS reason, COUNT(*) AS n FROM (SELECT {why} AS reason {base} "
            "WHERE e.month = ? AND {cases} AND {lost}) WHERE COALESCE(TRIM(reason), '') <> '' "
            "GROUP BY TRIM(reason)".format(why=reason_on_sql(day), base=BASE, cases=CASES, lost=lost_on(day)),
            (month,)):
        counts[r["reason"]] = counts.get(r["reason"], 0) + r["n"]
    bars = sorted(counts.items(), key=lambda kv: (-kv[1], order.get(kv[0].casefold(), 10 ** 6), kv[0].casefold()))
    if len(bars) > MAX_BARS:
        rest = sum(n for _, n in bars[MAX_BARS - 1:])
        bars = bars[:MAX_BARS - 1] + [("Other reasons", rest)]
    return bars


_NAME_ADDRESS = re.compile(r"^\s*[^<>\n]{0,80}<[^<>@\s]+@[^<>\s]+>\s*")


def note_text(text) -> str:
    """A note for one line: a leading 'Name <address>' stripped, the mail
    tag hidden, whitespace collapsed, NOTE_CUT characters at most."""
    from . import parsing
    text = parsing.hide_mid(text or "")
    text = _NAME_ADDRESS.sub("", text)
    text = " ".join(text.split())
    if len(text) > NOTE_CUT:
        text = text[:NOTE_CUT].rstrip() + "..."
    return text


def cancelled_cases(conn, month, day=None) -> list:
    """The month's Cancelled cases (on the closing day, 17j), oldest case
    first: the reason, and the note of the Cancelled event, else the latest
    note written on or after the cancelling day."""
    out = []
    day = day or close_day(month)
    for r in conn.execute(
            "SELECT e.id, e.opp_no, e.requester_key, {why} AS closed_reason {base} WHERE e.month = ? AND {cases} "
            "AND {cancelled} ORDER BY CAST(substr(e.opp_no, 5) AS INTEGER)".format(
                why=reason_on_sql(day), base=BASE, cases=CASES, cancelled=cancelled_on(day)), (month,)).fetchall():
        event = conn.execute(
            "SELECT at, note FROM status_event WHERE enquiry_id = ? AND voided = 0 AND kind = 'status' "
            "AND status = 'Cancelled' AND substr(at, 1, 10) <= ? ORDER BY at DESC, id DESC LIMIT 1",
            (r["id"], day)).fetchone()
        note = (event["note"] or "") if event is not None else ""
        if not note.strip() and event is not None:
            later = conn.execute(
                "SELECT note FROM status_event WHERE enquiry_id = ? AND voided = 0 AND kind = 'note' "
                "AND COALESCE(note, '') <> '' AND at >= ? ORDER BY at DESC, id DESC LIMIT 1",
                (r["id"], event["at"])).fetchone()
            note = later["note"] if later is not None else ""
        out.append({"opp_no": r["opp_no"], "requester": r["requester_key"] or "",
                    "reason": r["closed_reason"] or "", "note": note_text(note)})
    return out


def cancelled_line(c) -> str:
    text = c["reason"]
    if c["note"]:
        text = "{}, {}".format(text, c["note"]) if text else c["note"]
    return text


# ------------------------------------------------ 3. requests per month --
def per_month(conn, month) -> dict:
    year = month[:4]
    months = ["{}-{:02d}".format(year, m) for m in range(1, int(month[5:7]) + 1)]
    counts = {(r["m"], r["latam"]): r["n"] for r in conn.execute(
        "SELECT e.month AS m, {latam} AS latam, COUNT(*) AS n {base} "
        "WHERE e.month >= ? AND e.month <= ? AND {volume} AND {typed} GROUP BY m, latam".format(
            latam=LATAM, base=BASE, volume=VOLUME, typed=TYPED), (months[0], month))}
    return {
        "labels": [calendar.month_abbr[int(m[5:7])] for m in months],
        "months": months,
        "latam": [counts.get((m, 1), 0) for m in months],
        "row": [counts.get((m, 0), 0) for m in months],
    }


# ---------------------------------------------- 4. this month vs the last --
def _figures(conn, month, day=None) -> dict:
    """Section 4's numbers of a month, its outcomes on `day` (17j: the
    month's own closing day unless a caller asks for another)."""
    where = "e.month = ? AND " + CASES
    p = (month,)
    day = day or close_day(month)
    won_sql, lost_sql, quoted_sql = won_on(day), lost_on(day), quoted_on(day)
    total = _count(conn, where, p)
    latam = _count(conn, where + " AND " + LATAM, p)
    won = _count(conn, where + " AND " + won_sql, p)
    lost = _count(conn, where + " AND " + lost_sql, p)
    quoted = _count(conn, where + " AND " + quoted_sql, p)
    direct = _count(conn, where + " AND " + DIRECT, p)
    direct_won = _count(conn, where + " AND " + DIRECT + " AND " + won_sql, p)
    won_ao = _count(conn, where + " AND " + AGENTS_OFFICES + " AND " + won_sql, p)
    lost_ao = _count(conn, where + " AND " + AGENTS_OFFICES + " AND " + lost_sql, p)
    quoted_ao = _count(conn, where + " AND " + AGENTS_OFFICES + " AND " + quoted_sql, p)
    active = [r[0] for r in conn.execute(
        "SELECT DISTINCT e.requester_party_id {base} WHERE {where} AND p.type = 'Agent' "
        "AND e.latam_agent = 'Y'".format(base=BASE, where=where), p)]
    new = 0
    for party_id in active:
        first = conn.execute("SELECT MIN(received_on) FROM enquiry WHERE requester_party_id = ? "
                             "AND COALESCE(received_on, '') <> ''", (party_id,)).fetchone()[0]
        if (first or "")[:7] == month:
            new += 1
    return {"total": total, "latam": latam, "won": won, "lost": lost, "active": len(active), "new": new,
            "direct": direct, "direct_won": direct_won, "won_ao": won_ao, "lost_ao": lost_ao,
            "quoted": quoted, "quoted_ao": quoted_ao}


def _pct(part, whole):
    return round(100.0 * part / whole) if whole else None


#: 17k: section 4's rule, stated on its heading and in the xlsx and the text
WIN_RATE_RULE = "win rate = won \u00f7 quoted, open quotes included"
WIN_RATE_NOTE = ("Win rate = won \u00f7 quoted on each month's closing day; "
                 "a quote still without an answer counts as not won.")


def win_rate(f, agents_offices=False):
    """17k: won \u00f7 (won + lost + quoted) of a month's _figures - a quote
    still without an answer on the day counts as not won; Cancelled stays
    out. None when nothing is quoted."""
    if agents_offices:
        return _pct(f["won_ao"], f["won_ao"] + f["lost_ao"] + f["quoted_ao"])
    return _pct(f["won"], f["won"] + f["lost"] + f["quoted"])


def compare(conn, month, today=None) -> Table:
    """Section 4: Requests, LATAM share, Won, Win rate, Active and New
    LATAM agents, the previous month beside, the change coloured. 17a-2:
    when this month or the previous one has a direct customer's case, a row
    'of which direct customers' under Requests and under Won, and the win
    rate on agents' and offices' cases only."""
    # 17j: each month on its own closing day - the previous one on its last day
    now = _figures(conn, month, close_day(month, today))
    before = _figures(conn, previous_month(month), close_day(previous_month(month), today))
    direct = bool(now["direct"] or before["direct"])
    has_before = before["total"] > 0
    name, prev = calendar.month_name[int(month[5:7])], calendar.month_name[int(previous_month(month)[5:7])]
    table = Table(["", prev, name, "Change"])

    def row(label, a, b, pct=False, coloured=True):
        def fmt(v):
            return "" if v is None else ("{}%".format(v) if pct else str(v))
        change, mark = "", ""
        if has_before and a is not None and b is not None:
            delta = b - a
            change = ("{:+d} pts" if pct else "{:+d}").format(delta) if delta else ("0 pts" if pct else "0")
            if coloured and delta:
                mark = "up" if delta > 0 else "down"
        table.rows.append(Row(label, [fmt(a) if has_before else "", fmt(b), change],
                              "indent" if label == WHICH_DIRECT else "", ["", "", mark]))

    row("Requests", before["total"], now["total"])
    if direct:
        row(WHICH_DIRECT, before["direct"], now["direct"])
    row("LATAM share", _pct(before["latam"], before["total"]), _pct(now["latam"], now["total"]), pct=True,
        coloured=False)
    row("Won", before["won"], now["won"])
    if direct:
        row(WHICH_DIRECT, before["direct_won"], now["direct_won"])
        row("Win rate, agents and offices", win_rate(before, True), win_rate(now, True), pct=True)
    else:
        row("Win rate", win_rate(before), win_rate(now), pct=True)
    row("Active LATAM agents", before["active"], now["active"])
    row("New LATAM agents", before["new"], now["new"])
    return table


# --------------------------------------------- 5. where the LATAM traffic is --
def latam_map(conn, month, day=None) -> tuple:
    """([(country, {'n', 'won'})] every LATAM country the month's routes
    touch, the table): each route once per LATAM country it touches,
    origin or destination; the won core from the routes whose result is Won."""
    from . import dashboard
    latam = dashboard.latam_countries(conn)
    seen = {}
    for r in conn.execute(
            # 17j: a route's Won counts when its case was Won on the closing day
            "SELECT e.origin_country, e.dest_country, CASE WHEN {won} THEN e.result END AS result "
            "{base} WHERE e.month = ? AND {cases}".format(
                won=won_on(day or close_day(month), "e.enquiry_id"), base=RBASE, cases=R_CASES), (month,)):
        for country in {r["origin_country"], r["dest_country"]} & latam:
            t = seen.setdefault(country, {"n": 0, "won": 0})
            t["n"] += 1
            if r["result"] == "Won":
                t["won"] += 1
    rows = sorted(seen.items(), key=lambda kv: (-kv[1]["n"], -kv[1]["won"], kv[0].casefold()))
    table = Table(["Country", "Inq."])
    shown = rows if len(rows) <= MAX_COUNTRIES else rows[:MAX_COUNTRIES - 1]
    for country, t in shown:
        table.rows.append(Row(country, [str(t["n"])]))
    if len(rows) > MAX_COUNTRIES:
        rest = rows[MAX_COUNTRIES - 1:]
        table.rows.append(Row("Others ({})".format(len(rest)), [str(sum(t["n"] for _, t in rest))]))
    return rows, table


# --------------------------------------------- 6. LATAM by service type --
def direction(conn, month) -> Table:
    """Section 6. 17a-2: a row Other under Ocean when a LATAM route of the
    month is Customs only, Warehousing or another mode. SC1: Ocean is FCL,
    LCL and RORO; Sea-Air, Truck and Cross-border Truck are Other."""
    rows = conn.execute(
        "SELECT e.direction AS d, COUNT(*) AS n, SUM({air}) AS air, SUM({ocean}) AS ocean, SUM({other}) AS other "
        "{base} WHERE e.month = ? AND {cases} AND {latam} AND e.direction IN ('US Export', 'US Import', "
        "'Cross-trade') GROUP BY d".format(air=R_AIR, ocean=R_OCEAN, other=R_OTHER, base=RBASE, cases=R_CASES,
                                           latam=LATAM),
        (month,)).fetchall()
    found = {r["d"]: r for r in rows}
    with_other = conn.execute("SELECT COUNT(*) {base} WHERE e.month = ? AND {cases} AND {latam} AND {other}".format(
        base=RBASE, cases=R_CASES, latam=LATAM, other=R_OTHER), (month,)).fetchone()[0] > 0
    table = Table(["LATAM"] + [label for _, label in DIRECTIONS] + ["Total"])
    lines = (("Total", "n", "sub"), ("Air", "air", ""), ("Ocean", "ocean", ""))
    for label, key, cls in lines + ((("Other", "other", ""),) if with_other else ()):
        values = [(found[d][key] or 0) if d in found else 0 for d, _ in DIRECTIONS]
        table.rows.append(Row(label, [str(v) for v in values] + [str(sum(values))], cls))
    return table


# -------------------------------------------------------- 7. top lanes --
def top_lanes(conn, month) -> Table:
    table = Table(["Lane", "Inquiries"])
    for head, flag, limit in (("LATAM", 1, 5), ("Rest of world", 0, 3)):
        rows = conn.execute(
            "SELECT e.origin_country || ' to ' || e.dest_country AS lane, COUNT(*) AS n {base} "
            "WHERE e.month = ? AND {cases} AND {latam} = ? AND COALESCE(e.origin_country, '') <> '' "
            "AND COALESCE(e.dest_country, '') <> '' "
            "GROUP BY lane ORDER BY n DESC, {first} LIMIT ?".format(
                base=RBASE, cases=R_CASES, latam=LATAM, first=_order_by_case()), (month, flag, limit)).fetchall()
        table.rows.append(Row(head, [], "head"))
        for r in rows:
            table.rows.append(Row(r["lane"], [str(r["n"])]))
    return table


# ---------------------------------------- 8. top LATAM ocean destinations --
def top_pods(conn, month, limit=5) -> Table:
    table = Table(["Port", "Inquiries"])
    for r in conn.execute(
            "SELECT COALESCE(md.key, e.dest_loc) AS port, COUNT(*) AS n {base} "
            "LEFT JOIN master_location md ON md.id = e.dest_loc_id "
            "WHERE e.month = ? AND {cases} AND {latam} AND {ocean} "
            "AND COALESCE(md.key, e.dest_loc) IS NOT NULL "
            "GROUP BY port ORDER BY n DESC, {first} LIMIT ?".format(
                base=RBASE, cases=R_CASES, latam=LATAM, ocean=R_OCEAN, first=_order_by_case()), (month, limit)):
        table.rows.append(Row(r["port"], [str(r["n"])]))
    return table


# ------------------------------------------------------- 9. top agents --
def top_agents(conn, month, limit=5, day=None) -> Table:
    prev = previous_month(month)
    table = Table(["Agent", "Inquiries", "Export", "Import", words.direction_label("Cross-trade"),
                   calendar.month_abbr[int(prev[5:7])], "Won", "TEU", "Air kg", "LCL cbm"])
    marks = ["", "dir", "dir", "dir", "", "won", "", "", ""]
    table.marks = list(marks)
    groups = [("LATAM agents", "p.type = 'Agent' AND e.latam_agent = 'Y'", limit),
              (OTHER_AGENTS, "p.type = 'Agent' AND e.latam_agent = 'N'", limit)]
    # 17i: the offices with the most inquiries, when the month has one (FOUR's order)
    if _count(conn, "e.month = ? AND " + CASES + " AND " + OFFICE, (month,)):
        groups.append(("Our offices", OFFICE, TOP_OFFICES))
    # 17a-2: the direct customers with the most inquiries, when the month has one
    if _count(conn, "e.month = ? AND " + CASES + " AND " + DIRECT, (month,)):
        groups.append(("Direct customers", DIRECT, TOP_DIRECT))
    for head, who, most in groups:
        rows = conn.execute(
            "SELECT e.requester_party_id AS pid, e.requester_key AS agent, COUNT(*) AS n, "
            "SUM(e.direction = 'US Export') AS ex, SUM(e.direction = 'US Import') AS im, "
            "SUM(e.direction = 'Cross-trade') AS tri, SUM({won}) AS won, SUM(e.teu) AS teu, "
            "SUM(CASE WHEN e.service_mode = 'Air' THEN e.air_weight_kg END) AS kg, SUM(e.lcl_cbm) AS cbm {base} "
            "WHERE e.month = ? AND {cases} AND {who} "
            "GROUP BY pid ORDER BY n DESC, {first} LIMIT ?".format(
                won=won_on(day or close_day(month)), base=BASE, cases=CASES, who=who, first=_order_by_case()),
            (month, most)).fetchall()
        table.rows.append(Row(head, [], "head"))
        for r in rows:
            before = _count(conn, "e.month = ? AND " + CASES + " AND e.requester_party_id = ?", (prev, r["pid"]))
            table.rows.append(Row(r["agent"], [str(r["n"]), _n(r["ex"]), _n(r["im"]), _n(r["tri"]), _n(before),
                                               _n(r["won"]), _n(r["teu"]), _n(round(r["kg"] or 0)), _cbm(r["cbm"])],
                                  "", list(marks)))
        if head == "LATAM agents":
            quiet = quiet_agents(conn, month)
            if quiet:
                head = "Requested in {}, not in {}: ".format(calendar.month_name[int(prev[5:7])],
                                                             calendar.month_name[int(month[5:7])])
                page = "" if len(quiet) <= QUIET_NAMES else head + "{} and {} more".format(
                    ", ".join(quiet[:QUIET_NAMES]), len(quiet) - QUIET_NAMES)
                table.rows.append(Row(head + ", ".join(quiet), [], "note", page=page))
    return table


def quiet_agents(conn, month) -> list:
    """The LATAM agents with cases last month and none this month, by name."""
    def agents(m):
        return {r["pid"]: r["agent"] for r in conn.execute(
            "SELECT DISTINCT e.requester_party_id AS pid, e.requester_key AS agent {base} WHERE e.month = ? "
            "AND {cases} AND p.type = 'Agent' AND e.latam_agent = 'Y'".format(base=BASE, cases=CASES), (m,))}
    now, before = agents(month), agents(previous_month(month))
    return sorted((name for pid, name in before.items() if pid not in now), key=lambda s: (s or "").casefold())


# ------------------------------------------------- 10. requests per week --
def week_spans(month) -> list:
    """[(first, last)] dates: Monday to Sunday, cut at the month's edges; an
    edge piece with two working days or fewer joins its neighbour."""
    year, mon = int(month[:4]), int(month[5:7])
    first = dt.date(year, mon, 1)
    last = dt.date(year, mon, calendar.monthrange(year, mon)[1])
    spans, start = [], first
    while start <= last:
        end = min(start + dt.timedelta(days=6 - start.weekday()), last)
        spans.append([start, end])
        start = end + dt.timedelta(days=1)

    def working(span):
        return sum(1 for i in range((span[1] - span[0]).days + 1) if (span[0] + dt.timedelta(days=i)).weekday() < 5)

    if len(spans) > 1 and working(spans[0]) <= 2:
        spans[1][0] = spans[0][0]
        spans.pop(0)
    if len(spans) > 1 and working(spans[-1]) <= 2:
        spans[-2][1] = spans[-1][1]
        spans.pop()
    return [(a, b) for a, b in spans]


def weekly(conn, month) -> dict:
    spans = week_spans(month)
    labels = ["{} {}-{}".format(calendar.month_abbr[a.month], a.day, b.day) for a, b in spans]
    series = {name: [0] * len(spans) for name, _ in FOUR}
    for r in conn.execute("SELECT e.received_on, {grp} AS grp {base} WHERE e.month = ? AND {cases}".format(
            grp=GROUP, base=BASE, cases=CASES), (month,)):
        day = dt.date.fromisoformat(r["received_on"][:10])
        for i, (a, b) in enumerate(spans):
            if a <= day <= b:
                series[FOLD[r["grp"]]][i] += 1
                break
    if not any(series["Direct customers"]):
        del series["Direct customers"]
    return {"labels": labels, "spans": spans, "series": series}


def mix(conn, month) -> list:
    """Section 11: [(group, n, colour)], the first three always, Direct
    customers only when it has a case."""
    counts = {name: 0 for name, _ in FOUR}
    for r in conn.execute("SELECT {grp} AS grp, COUNT(*) AS n {base} WHERE e.month = ? AND {cases} "
                          "GROUP BY grp".format(grp=GROUP, base=BASE, cases=CASES), (month,)):
        counts[FOLD[r["grp"]]] += r["n"]
    return [(name, counts[name], colour) for name, colour in FOUR
            if name != "Direct customers" or counts[name]]


def colours() -> dict:
    return dict(FOUR)


# ------------------------------------------------- the check box (page only) --
def _lane(r) -> str:
    return "{} to {}".format(r["origin_country"] or "?", r["dest_country"] or "?")


def checks(conn, month) -> list:
    """What to fix before printing: [{'title', 'items': [{'ids', 'opp_nos',
    'who', 'text'}]}], in the report's order; only the groups that have
    something."""
    def cases(extra, params=()):
        return conn.execute("SELECT e.* {base} WHERE e.month = ? AND {cases} AND {extra} "
                            "ORDER BY CAST(substr(e.opp_no, 5) AS INTEGER)".format(
                                base=BASE, cases=CASES, extra=extra), (month,) + tuple(params)).fetchall()

    def item(r, text=""):
        return {"ids": [r["id"]], "opp_nos": [r["opp_no"]], "who": r["requester_key"] or "", "text": text}

    groups = []

    def add(title, items):
        if items:
            groups.append({"title": title, "items": items})

    # 17a-2: a Customs only or Warehousing case has one place, so it is in
    # neither of these two. TK1: a Truck case, crossing a border or not,
    # needs both countries like any other
    service = _not_service(CASE_SERVICE)
    add("No origin or destination country", [item(r, _lane(r)) for r in cases(
        "(COALESCE(e.origin_country, '') = '' OR COALESCE(e.dest_country, '') = '') AND {}".format(service))])
    add("Country unknown", [item(r, _lane(r)) for r in cases(
        "(e.origin_country = 'Unknown' OR e.dest_country = 'Unknown')")])
    add("No mode", [item(r, _lane(r)) for r in cases("COALESCE(e.service_mode, '') = '' AND " + service)])
    # 17h: the cases section 15 leaves out, this month's and the previous
    # month's (15 shows both); the same reading as monthly_requests.sizes
    from . import monthly_requests
    prev = previous_month(month)
    add("No size for section 15", [
        item(r, "{}{}, {}".format("({}) ".format(calendar.month_name[int(prev[5:7])]) if r["month"] == prev else "",
                                  _lane(r), monthly_requests.MISSING_WORDS[mode]))
        for mode, r in monthly_requests.left_out(conn, month)])
    air = []
    for r in cases("e.service_mode = 'Air'"):
        boxes = conn.execute("SELECT container_qty, container_type FROM enquiry_cargo WHERE enquiry_id = ? "
                             "AND voided = 0 AND COALESCE(container_type, '') <> ''", (r["id"],)).fetchall()
        if boxes:
            air.append(item(r, ", ".join("{} x {}".format(b["container_qty"], b["container_type"])
                                         if b["container_qty"] else b["container_type"] for b in boxes)))
    add("Air with containers", air)
    add("Lost with no reason", [item(r) for r in cases("e.status = 'Lost' AND COALESCE(TRIM(e.closed_reason), '') = ''")])
    add("Cancelled with no reason", [item(r) for r in cases(
        "e.status = 'Cancelled' AND COALESCE(TRIM(e.closed_reason), '') = ''")])
    # the last two read the month's cases whatever the requester's type
    untyped = conn.execute(
        "SELECT e.id, e.opp_no, e.requester_key, p.type {base} WHERE e.month = ? AND {real} AND NOT ({typed}) "
        "ORDER BY CAST(substr(e.opp_no, 5) AS INTEGER)".format(base=BASE, real=REAL, typed=TYPED),
        (month,)).fetchall()
    add("Not on the report, requester type is not Agent, own office or Direct customer",
        [item(r, r["type"] or "no type") for r in untyped])
    agents = {}
    for r in conn.execute(
            "SELECT e.id, e.opp_no, e.requester_key, e.requester_party_id {base} WHERE e.month = ? AND {real} "
            "AND p.type = 'Agent' AND COALESCE(NULLIF(p.country, 'Unknown'), '') = '' "
            "ORDER BY CAST(substr(e.opp_no, 5) AS INTEGER)".format(base=BASE, real=REAL), (month,)):
        a = agents.setdefault(r["requester_party_id"], {"ids": [], "opp_nos": [], "who": r["requester_key"] or "",
                                                        "text": "", "party_id": r["requester_party_id"]})
        a["ids"].append(r["id"])
        a["opp_nos"].append(r["opp_no"])
    add("Agent's country unknown", list(agents.values()))
    # SC1: the 21b and DC1 groups went - the report rule leaves those cases
    # out by itself (CASES), whatever their scope
    return groups


# ------------------------------------------------------------ the chart --
def chart_svg(labels, latam, row) -> str:
    """The requests-per-month line chart: two lines, a value at every
    non-zero point, the higher of the two labelled above its point."""
    left, right, top, bottom = 28.0, 488.0, 22.0, 208.0
    peak = max([1] + latam + row)
    ymax = max(20, -(-peak // 20) * 20)
    n = max(1, len(labels) - 1)

    def x(i):
        return left + (right - left) * i / n

    def y(v):
        return bottom - (bottom - top) * v / ymax

    font = 'font-size="9" fill="{c}" text-anchor="{a}" font-weight="normal" ' \
           'font-family="system-ui,-apple-system,Segoe UI,sans-serif"'
    out = ['<svg viewBox="0 0 500 230" width="500" height="230" role="img">']
    tick = 0
    while tick <= ymax:
        out.append('<line x1="28" y1="{y:.1f}" x2="488" y2="{y:.1f}" stroke="#E1E0D9"/>'.format(y=y(tick)))
        out.append('<text x="23.0" y="{y:.1f}" {f}>{v}</text>'.format(
            y=y(tick) + 3, f=font.format(c="#52514E", a="end"), v=tick))
        tick += 20
    for values, other, colour in ((latam, row, LATAM_COLOUR), (row, latam, ROW_COLOUR)):
        points = " ".join("{:.1f},{:.1f}".format(x(i), y(v)) for i, v in enumerate(values))
        out.append('<polyline points="{}" fill="none" stroke="{}" stroke-width="2"/>'.format(points, colour))
        for i, v in enumerate(values):
            out.append('<circle cx="{:.1f}" cy="{:.1f}" r="3" fill="{}" stroke="#FCFCFB" '
                       'stroke-width="1.5"/>'.format(x(i), y(v), colour))
            if v:
                below = other[i] and other[i] > v
                out.append('<text x="{:.1f}" y="{:.1f}" {}>{}</text>'.format(
                    x(i), y(v) + (14 if below else -7), font.format(c=colour, a="middle"), v))
    for i, label in enumerate(labels):
        out.append('<text x="{:.1f}" y="223.0" {}>{}</text>'.format(
            x(i), font.format(c="#52514E", a="middle"), label))
    out.append('<rect x="338" y="2" width="8" height="8" rx="2" fill="{}"/>'.format(LATAM_COLOUR))
    out.append('<text x="349.0" y="10.0" {}>LATAM</text>'.format(font.format(c="#52514E", a="start")))
    out.append('<rect x="390.5" y="2" width="8" height="8" rx="2" fill="{}"/>'.format(ROW_COLOUR))
    out.append('<text x="401.5" y="10.0" {}>Rest of world</text>'.format(font.format(c="#52514E", a="start")))
    out.append("</svg>")
    return "\n".join(out)


# ---------------------------------------------------------------- entry --
def build(conn: sqlite3.Connection, month: str, today: dt.date | None = None) -> Monthly:
    from . import monthly_analysis, monthly_requests
    report = Monthly(month=month, title=title_for(month), month_name=calendar.month_name[int(month[5:7])],
                     previous_name=calendar.month_name[int(previous_month(month)[5:7])])
    day = close_day(month, today)                      # 17j: the outcomes on the closing day
    report.close_day = day
    report.kpis = kpis(conn, month, day)
    report.kpi_notes = kpi_notes(conn, month, day)
    report.by_scope = by_scope(conn, month, day)
    report.lost_bars = lost_bars(conn, month, day)
    report.cancelled = cancelled_cases(conn, month, day)
    report.cancelled_shown = CANCEL_LINES.get(len(report.lost_bars), 1)
    report.dense_overview = overview_rows(report) > DENSE_OVERVIEW_ROWS
    report.per_month = per_month(conn, month)
    report.compare = compare(conn, month, today)
    report.map_rows, report.countries = latam_map(conn, month, day)
    report.direction = direction(conn, month)
    report.direction_other = any(r.label == "Other" for r in report.direction.rows)
    report.top_lanes = top_lanes(conn, month)
    report.top_pods = top_pods(conn, month)
    report.top_agents = top_agents(conn, month, day=day)
    report.dense_agents = any(r.cls == 'note' and len(r.page or r.label) > DENSE_NOTE_CHARS
                              for r in report.top_agents.rows)
    report.direct_group = any(r.cls == "head" and r.label == "Direct customers" for r in report.top_agents.rows)
    report.office_group = any(r.cls == "head" and r.label == "Our offices" for r in report.top_agents.rows)
    report.weeks = weekly(conn, month)
    report.mix = mix(conn, month)
    report.checks = checks(conn, month)
    report.analysis, report.dense_analysis = monthly_analysis.build(conn, month, today)
    report.requests, report.dense_requests = monthly_requests.build(conn, month, today)
    return report


def tables(report: Monthly) -> list:
    """(number, title, table) for the eighteen sections, in the report's order,
    for the xlsx and the text; 12 to 18 (17f, 17g) carry their bullets as note rows."""
    from . import monthly_analysis
    lost = Table(["Reason", "Lost"], [Row(label, [str(n)]) for label, n in report.lost_bars])
    if report.cancelled:
        lost.rows.append(Row("Cancelled: {}".format(len(report.cancelled)), [], "head"))
        for c in report.cancelled:
            lost.rows.append(Row("{} · {}".format(c["opp_no"], c["requester"]), [cancelled_line(c)]))
    weeks = Table(["Week"] + list(report.weeks.get("series", {})))
    for i, label in enumerate(report.weeks.get("labels", [])):
        weeks.rows.append(Row(label, [str(v[i]) for v in report.weeks["series"].values()]))
    return [
        ("1", "By scope, requester and direction", report.by_scope),
        ("2", "Why we lost", lost),
        ("3", "Requests per month, {}".format(report.month[:4]), Table(
            ["Month", "LATAM", "Rest of world"],
            [Row(label, [str(a), str(b)]) for label, a, b in zip(
                report.per_month["labels"], report.per_month["latam"], report.per_month["row"])])),
        # 17k: the rule as a last note row (the page states it on the heading)
        ("4", "{} vs {}".format(report.month_name, report.previous_name),
         Table(list(report.compare.columns), list(report.compare.rows) + [Row(WIN_RATE_NOTE, [], "note")])),
        ("5", "Where the LATAM traffic is", report.countries),
        ("6", "LATAM by service type", report.direction),
        ("7", "Top lanes", report.top_lanes),
        ("8", "Top LATAM ocean destinations (POD)", report.top_pods),
        ("9", "Top 5 agents", report.top_agents),
        ("10", "Requests per week", weeks),
        ("11", "Who sends the requests", Table(["Group", "Requests"],
                                                [Row(label, [str(n)]) for label, n, _ in report.mix])),
    ] + [(card.number, card.title, monthly_analysis.table_for(card))
         for card in report.analysis + report.requests]


def render_text(report: Monthly) -> list:
    lines = [report.title, ""]
    # 17l: a label longer than the column still gets a space before its number
    lines.extend("{}{}{}".format((label + " ").ljust(28), value, " ({})".format(report.kpi_notes[label])
                                     if report.kpi_notes.get(label) else "")
                 for value, label in report.kpis)
    for number, title, table in tables(report):
        lines.append("")
        lines.append("{}. {}".format(number, title))
        lines.append("  " + " | ".join(table.columns))
        for row in table.rows:
            lines.append("  " + " | ".join([row.label] + row.cells))
    return lines

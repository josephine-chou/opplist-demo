"""The monthly report (2026-09-09 version).

One set of queries feeds three places - the /report page, the Report sheet
and the Trend sheet of the export - so a number on the screen is the number
in the file. Everything reads v_enquiry, so Month, Region and the rest have
exactly one definition.

Every section groups cases by who asked, in four buckets, and leaves out the
legacy rows and the dropped ones. Dropped means the row should not exist (a
duplicate, a mis-registration), so no dropped case is in any number here
(2026-09-18). The Definitions section prints the rules in words, so the
reader of a printout can see what each number means without opening this.
"""

from __future__ import annotations

import calendar
import datetime as dt
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from . import direct_customers_out as dco


@dataclass
class Block:
    title: str
    columns: list
    rows: list = field(default_factory=list)


@dataclass
class Report:
    month: str
    today: str
    summary: str = ""
    sections: list = field(default_factory=list)
    trend: Block | None = None
    #: The trend's first five columns: the table that sits beside the line.
    trend_volume: Block | None = None
    definitions: list = field(default_factory=list)


# ----------------------------------------------------------- the groups ----
#: Who asked, in six lines. One of our offices is a type and wins over the
#: LATAM list, so an office in a LATAM country counts as an office. A LATAM
#: agent is an Agent in a country of the LATAM list in Settings (v36).
#: Direct customers are a group of their own (batch 21); the last two are
#: 'Other', and the summary tables print them and then their subtotal.
GROUPS = ("MFN agents", "LATAM agents", "Our offices", "Direct customers",
          "Other agents", "Unidentified")
#: SC1: 'Other agents' was 'Non-LATAM agents'; Unidentified stays a line,
#: empty since the report rule counts only the three requester types
OTHER_GROUPS = ("Other agents", "Unidentified")
#: The groups the trend and the summary name one by one, before Other.
MAIN_GROUPS = GROUPS[:4]
OTHER = "Other"

GROUP_SQL = """
CASE
  WHEN {p}.type = 'Office' THEN 'Our offices'
  WHEN {latam} AND {p}.mfn_member = 1 THEN 'MFN agents'
  WHEN {latam} THEN 'LATAM agents'
  WHEN {p}.type = 'Direct Customer' THEN 'Direct customers'
  WHEN {p}.type = 'Unknown' OR {p}.placeholder = 1 THEN 'Unidentified'
  ELSE 'Other agents'
END"""


def group_rows(cell_for):
    """The rows of a summary table: six groups, the Other subtotal, Total.

    `cell_for(group)` returns the numeric cells for one group; the subtotal
    and the total are sums of those, so they cannot drift from the lines.
    """
    rows = []
    for group in GROUPS:
        rows.append((group,) + tuple(cell_for(group)))
    per_group = {r[0]: r[1:] for r in rows}
    other = tuple(sum(per_group[g][i] for g in OTHER_GROUPS)
                  for i in range(len(rows[0]) - 1))
    total = tuple(sum(per_group[g][i] for g in GROUPS)
                  for i in range(len(rows[0]) - 1))
    rows.append((OTHER,) + other)
    rows.append(("Total",) + total)
    return rows


def group_of_requester(alias: str = "e", party: str = "p") -> str:
    return GROUP_SQL.format(p=party, latam="{}.latam_agent = 'Y'".format(alias))


def group_of_vendor(party: str = "vp", country: str = "vc") -> str:
    # the vendor's own country against the list (`country` is kept for the callers' joins)
    return GROUP_SQL.format(p=party, latam="({p}.type = 'Agent' AND {p}.country IN "
                                           "(SELECT name FROM v_latam_country))".format(p=party))


#: A dropped case is bookkeeping, not business: the row should not exist
#: (a duplicate, a mis-registration), so it is in no number on the report.
#: Since 2026-09-18 that is the whole test - the reason is not consulted,
#: because every Dropped reason now means 'not a real case'.
REAL_CASE_SQL = "{alias}.status <> 'Dropped'"


def real_cases(alias: str = "e") -> str:
    return REAL_CASE_SQL.format(alias=alias)


#: The cases a month is about: received in it, not legacy, and counted by
#: the report rule (SC1, direct_customers_out.counted_sql: scope In, not
#: Dropped, an Agent, Office or Direct Customer asked, and the
#: rule leaves it in). `party` is the requester's alias.
def in_month(alias: str = "e", party: str = "p") -> str:
    return "{a}.month = ? AND {a}.legacy = 0 AND {counted}".format(
        a=alias, counted=dco.counted_sql(alias, party))


#: The month's volume, legacy included: how much came in, whatever became of
#: it. Only the outcome columns leave legacy out. A legacy row
#: is Dropped only because the workbook never said how it ended, so the
#: volume line keeps it; a duplicate or a mis-registration is still out.
def volume_month(alias: str = "e", party: str = "p") -> str:
    return "{a}.month = ? AND {counted}".format(a=alias, counted=dco.counted_sql(alias, party, volume=True))


_BASE = """
FROM v_enquiry e
JOIN party p ON p.id = e.requester_party_id
LEFT JOIN master_location md ON md.id = e.dest_loc_id
"""

#: How long a quoted case may sit before it is 'no response'.
STALE_DAYS = 30

#: Outcome columns are only meaningful from here on: before it, statuses
#: came out of the workbook with dates guessed by the migration.
OUTCOME_FROM = "2026-08"
TREND_FROM = "2026-01"


# -------------------------------------------------------------- helpers ----
def month_bounds(month: str):
    year, mon = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(year, mon)[1]
    return "{}-01".format(month), "{}-{:02d}".format(month, last)


def previous_month(month: str) -> str:
    year, mon = int(month[:4]), int(month[5:7])
    if mon == 1:
        return "{:04d}-12".format(year - 1)
    return "{:04d}-{:02d}".format(year, mon - 1)


def next_month(month: str) -> str:
    year, mon = int(month[:4]), int(month[5:7])
    if mon == 12:
        return "{:04d}-01".format(year + 1)
    return "{:04d}-{:02d}".format(year, mon + 1)


def default_months(today: dt.date | None = None) -> list:
    """The export writes last month and this month, in that order."""
    today = today or dt.date.today()
    this_month = "{:04d}-{:02d}".format(today.year, today.month)
    return [previous_month(this_month), this_month]


def months_between(first: str, last: str) -> list:
    out = []
    month = first
    while month <= last:
        out.append(month)
        month = next_month(month)
    return out


def _today(today) -> str:
    return today or dt.date.today().isoformat()


def _by_group(rows, key="grp"):
    return {r[key]: r for r in rows}


def _pct(part, whole) -> str:
    return "{:.0%}".format(part / whole) if whole else ""


# ------------------------------------------------------------ 1 inbound ----
def inbound(conn, month) -> Block:
    rows = conn.execute(
        "SELECT {} AS grp, COUNT(DISTINCT e.request_group) AS groups, "
        "COUNT(*) AS lanes {} WHERE {} GROUP BY grp".format(
            group_of_requester(), _BASE, in_month()), (month,)).fetchall()
    found = _by_group(rows)
    out = group_rows(lambda g: (found[g]["groups"] if g in found else 0,
                                found[g]["lanes"] if g in found else 0))
    return Block("1. Inbound requests", ["Group", "Request groups", "Rows"], out)


# -------------------------------------------------------------- 2 top 5 ----
def _top_by_group(conn, month, column, title, limit=5) -> Block:
    rows = conn.execute(
        "SELECT {grp} AS grp, {col} AS value, COUNT(*) AS n {base} "
        "WHERE {m} AND {col} IS NOT NULL AND {col} <> '' "
        "GROUP BY grp, value ORDER BY grp, n DESC, value".format(
            grp=group_of_requester(), col=column, base=_BASE, m=in_month()),
        (month,)).fetchall()
    overall = Counter()
    for r in rows:
        overall[r["value"]] += r["n"]
    out = [("All", value, n) for value, n in
           sorted(overall.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]
    for group in GROUPS:
        for r in [r for r in rows if r["grp"] == group][:limit]:
            out.append((group, r["value"], r["n"]))
    return Block(title, ["Group", "Value", "Rows"], out)


def top_tables(conn, month) -> list:
    return [
        _top_by_group(conn, month, "e.origin_country",
                      "2a. Top 5 origin countries"),
        _top_by_group(conn, month, "e.dest_country",
                      "2b. Top 5 destination countries"),
        _top_by_group(conn, month, "COALESCE(md.key, e.dest_loc)",
                      "2c. Top 5 destination locations"),
        _top_by_group(conn, month, "e.requester_key", "2d. Top 5 requesters"),
    ]


# ---------------------------------------------------------------- 3 mode ---
def _mode_sql() -> str:
    # SC1: the month report's reading (monthly.CASE_COLUMN: a quote-both
    # case under its Won route's mode); RORO is Ocean
    from . import monthly
    return "CASE WHEN {c} = 'Air' THEN 'Air' WHEN {c} IN ({ocean}) THEN 'Ocean' ELSE 'Other' END".format(
        c=monthly.CASE_COLUMN, ocean=", ".join("'{}'".format(m) for m in monthly.OCEAN_MODES))


MODE_SQL = _mode_sql()


def mode(conn, month) -> Block:
    rows = conn.execute(
        "SELECT {} AS grp, {} AS mode, COUNT(*) AS n {} WHERE {} "
        "GROUP BY grp, mode".format(group_of_requester(), MODE_SQL, _BASE,
                                    in_month()), (month,)).fetchall()
    counts = Counter()
    for r in rows:
        counts[(r["grp"], r["mode"])] = r["n"]
    def cells(group):
        parts = [counts[(group, m)] for m in ("Air", "Ocean", "Other")]
        return parts + [sum(parts)]

    return Block("3. Mode", ["Group", "Air", "Ocean", "Other", "Total"],
                 group_rows(cells))


# ------------------------------------------------------------- 4 outcome ---
OUTCOMES = ("Won", "Lost", "Cancelled", "Quoted, awaiting customer",
            "In progress (not quoted)", "No response", "Stale", "Unknown")


def outcome_sql(today: str) -> str:
    """One expression, used by the month section and the trend alike."""
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=STALE_DAYS)
              ).isoformat()
    return """
CASE
  WHEN e.status_confirmed = 0 THEN 'Unknown'
  WHEN e.status = 'Won' THEN 'Won'
  WHEN e.status = 'Lost' THEN 'Lost'
  WHEN e.status = 'Cancelled' THEN 'Cancelled'
  WHEN e.status = 'Quoted' AND COALESCE(e.last_event_on, e.received_on) < '{c}'
       THEN 'No response'
  WHEN e.status = 'Quoted' THEN 'Quoted, awaiting customer'
  WHEN COALESCE(e.last_event_on, e.received_on) >= '{c}'
       THEN 'In progress (not quoted)'
  ELSE 'Stale'
END""".format(c=cutoff)


def outcome(conn, month, today) -> Block:
    rows = conn.execute(
        "SELECT {} AS grp, {} AS outcome, COUNT(*) AS n {} WHERE {} "
        "GROUP BY grp, outcome".format(
            group_of_requester(), outcome_sql(today), _BASE, in_month()),
        (month,)).fetchall()
    counts = Counter()
    for r in rows:
        counts[(r["grp"], r["outcome"])] = r["n"]
    def cells(group):
        parts = [counts[(group, o)] for o in OUTCOMES]
        return parts + [sum(parts)]

    return Block("4. Outcome (awarded = Won)",
                 ["Group"] + list(OUTCOMES) + ["Total"], group_rows(cells))


# ------------------------------------------------------------ 5 outbound ---
_VENDOR_BASE = """
FROM vendor_request v
JOIN v_enquiry e ON e.id = v.enquiry_id
JOIN party vp ON vp.id = v.vendor_party_id
LEFT JOIN country vc ON vc.name = vp.country
"""


def outbound(conn, month) -> list:
    where = ("substr(v.asked_on, 1, 7) = ? AND v.voided = 0 AND e.legacy = 0 AND "
             + real_cases())
    by_group = _by_group(conn.execute(
        "SELECT {} AS grp, COUNT(*) AS n {} WHERE {} GROUP BY grp".format(
            group_of_vendor(), _VENDOR_BASE, where), (month,)).fetchall())
    counts = group_rows(lambda g: (by_group[g]["n"] if g in by_group else 0,))
    vendors = conn.execute(
        "SELECT vp.key AS value, COUNT(*) AS n {} WHERE {} "
        "GROUP BY value ORDER BY n DESC, value LIMIT 5".format(
            _VENDOR_BASE, where), (month,)).fetchall()
    dests = conn.execute(
        "SELECT e.dest_country AS value, COUNT(*) AS n {} WHERE {} "
        "AND e.dest_country IS NOT NULL GROUP BY value "
        "ORDER BY n DESC, value LIMIT 5".format(_VENDOR_BASE, where),
        (month,)).fetchall()
    return [
        Block("5a. Vendor requests sent, by vendor group",
              ["Vendor group", "Requests"], counts),
        Block("5b. Top 5 vendors asked", ["Vendor", "Requests"],
              [(r["value"], r["n"]) for r in vendors]),
        Block("5c. Top 5 destination countries asked about",
              ["Country", "Requests"], [(r["value"], r["n"]) for r in dests]),
    ]


# ------------------------------------------------------------ 6 adoption ---
def adoption(conn, month) -> list:
    """Of the vendor quotes on the month's won cases, how many were used.

    Only an agent's rate counts as adopted (rate_used = 'agent'): a case won
    on our own rate or an office's has adopted no vendor quote, however
    many were received (v8).
    """
    rows = conn.execute(
        "SELECT {} AS grp, SUM(v.quoted) AS quoted, "
        "SUM(CASE WHEN v.awarded = 1 AND e.rate_used = 'agent' THEN 1 ELSE 0 END) AS awarded "
        "FROM vendor_request v JOIN v_enquiry e ON e.id = v.enquiry_id "
        "JOIN party p ON p.id = e.requester_party_id "
        "WHERE e.status = 'Won' AND v.voided = 0 AND {} GROUP BY grp".format(
            group_of_requester(), in_month()), (month,)).fetchall()
    found = _by_group(rows)
    out = [(g, q, a, _pct(a, q)) for g, q, a in group_rows(
        lambda g: ((found[g]["quoted"] or 0) if g in found else 0,
                   (found[g]["awarded"] or 0) if g in found else 0))]

    won = conn.execute(
        "SELECT e.opp_no, e.requester_key, e.rate_used, e.awarded_note, "
        "       ap.key AS whose "
        "FROM v_enquiry e JOIN party p ON p.id = e.requester_party_id "
        "LEFT JOIN party ap ON ap.id = e.awarded_vendor_id "
        "WHERE e.status = 'Won' AND {} ORDER BY e.opp_no".format(in_month()),
        (month,)).fetchall()
    return [
        Block("6a. Vendor quote adoption on won cases",
              ["Group", "Quoted", "Awarded", "Adoption"], out),
        Block("6b. Whose rate each won case used", ["Case", "Requester", "Rate used"],
              [(r["opp_no"], r["requester_key"], rate_used_label(r))
               for r in won]),
    ]


def rate_used_label(row) -> str:
    """'Agent rate: Seavio', 'Office rate: JoJo JQI', 'Own rate',
    'Not recorded', or 'not answered yet' when nobody has said."""
    from .status import RATE_USED_LABELS
    rate = row["rate_used"]
    if not rate:
        return "not answered yet" + (" ({})".format(row["awarded_note"])
                                     if row["awarded_note"] else "")
    label = RATE_USED_LABELS.get(rate, rate)
    if rate in ("agent", "office") and row["whose"]:
        label += ": " + row["whose"]
    if row["awarded_note"]:
        label += " ({})".format(row["awarded_note"])
    return label


# --------------------------------------------------------------- 7 trend ---
def trend(conn, upto, today) -> Block:
    """Month by month from TREND_FROM; outcomes only from OUTCOME_FROM."""
    months = months_between(TREND_FROM, upto)
    trend_groups = MAIN_GROUPS + (OTHER,)
    columns = ["Month", "Requests", "Rows", "Air %", "Ocean %"] + \
        ["{} %".format(g) for g in trend_groups] + list(OUTCOMES)
    out = []
    for month in months:
        # Volume counts everything that came in, legacy included: the size
        # of a month does not depend on whether its outcome was recorded.
        base = conn.execute(
            "SELECT COUNT(DISTINCT e.request_group) AS groups, COUNT(*) AS lanes "
            "{} WHERE {}".format(_BASE, volume_month()), (month,)).fetchone()
        lanes = base["lanes"] or 0
        modes = Counter({r["mode"]: r["n"] for r in conn.execute(
            "SELECT {} AS mode, COUNT(*) AS n {} WHERE {} GROUP BY mode".format(
                MODE_SQL, _BASE, volume_month()), (month,))})
        groups = Counter({r["grp"]: r["n"] for r in conn.execute(
            "SELECT {} AS grp, COUNT(*) AS n {} WHERE {} GROUP BY grp".format(
                group_of_requester(), _BASE, volume_month()), (month,))})
        groups[OTHER] = sum(groups[g] for g in OTHER_GROUPS)
        row = [month, base["groups"] or 0, lanes,
               _pct(modes["Air"], lanes), _pct(modes["Ocean"], lanes)]
        row.extend(_pct(groups[g], lanes) for g in trend_groups)
        if month >= OUTCOME_FROM:
            outcomes = Counter({r["o"]: r["n"] for r in conn.execute(
                "SELECT {} AS o, COUNT(*) AS n {} WHERE {} GROUP BY o".format(
                    outcome_sql(today), _BASE, in_month()), (month,))})
            row.extend(outcomes[o] for o in OUTCOMES)
        else:
            row.extend("" for _ in OUTCOMES)
        out.append(tuple(row))
    return Block("7. Trend from {}".format(TREND_FROM), columns, out)


# --------------------------------------------------------- 8 definitions ---
def definitions(today: str) -> list:
    return [
        "Groups, by who asked (the requester): MFN agents = region Latin "
        "America and MFN member; LATAM agents = region Latin America, not a "
        "MFN member; Our offices = party type Office (a type, so "
        "it wins over region); Direct customers = party type Direct Customer, a "
        "group of its own; Other agents = agents anywhere else; "
        "Unidentified = placeholder or unknown party ('(TBC)', 'Unknown'), "
        "not counted. The last two add up to Other.",
        "A month = cases received in it (received_on). Sections 1 to 6 leave "
        "out legacy rows (inherited, outcome unknown); every section leaves "
        "out Dropped cases (the row should not exist: a duplicate, a "
        "mis-registration), cases with scope Out (set by hand, kept as a "
        "record only), a direct customer's case before October 2026 (they "
        "were not registered systematically), and from October a direct "
        "customer's or one of our offices's case that is not LATAM - their "
        "vendor requests still count in section 5. Cases another JoJo "
        "office handles count like any other.",
        "1. Inbound: request groups (a multi-lane request counts once) and rows "
        "(one per lane).",
        "2. Top 5 within each group: origin country, destination country, "
        "destination location, requester.",
        "3. Mode: Air; Ocean = FCL + LCL + RORO; Other = everything else. "
        "A case quoted in two modes counts under its Won route's mode.",
        "4. Outcome, judged on {today}: Unknown = status never confirmed; "
        "Won; Lost = the shipment went ahead without us; Cancelled = the "
        "shipment did not go ahead; "
        "No response = Quoted with no event for {days} "
        "days; Quoted, awaiting customer = Quoted with an event in the last "
        "{days} days; In progress (not quoted) = New with an event "
        "in the last {days} days; Stale = New with no event for "
        "{days} days. Awarded means Won.".format(today=today, days=STALE_DAYS),
        "5. Outbound: vendor requests sent (asked_on) in the month, grouped by "
        "the vendor's group under the same four buckets; top 5 vendors; top 5 "
        "destination countries of the cases asked about.",
        "6. Adoption: on the month's won cases, agent rates used (rate used = "
        "Agent rate, the vendor request marked awarded) divided by vendor "
        "quotes received - a case won on our own rate or one of our offices's "
        "adopted no vendor quote; then whose rate each won case used: Agent "
        "rate, Office rate, Own rate, Not recorded, or not answered "
        "yet. Struck-out vendor requests count for nothing.",
        "7. Trend from {}: requests and rows per month, Air and Ocean share, "
        "each group's share of rows - all of these include legacy cases, "
        "because volume is volume whatever became of it; the outcome columns "
        "leave legacy out and only start from {}, because earlier statuses "
        "came out of the workbook with guessed dates."
        .format(TREND_FROM, OUTCOME_FROM),
    ]


# -------------------------------------------------------------- summary ----
def summary_text(month, sections) -> str:
    """A paragraph for the top of the page, built from the numbers below it."""
    by_title = {s.title[:2]: s for s in sections}
    inbound_rows = dict((r[0], r) for r in by_title["1."].rows)
    total = inbound_rows["Total"]
    parts = ["{}: {} inbound requests ({} rows)".format(
        month, total[1], total[2])]
    shares = ", ".join("{} {}".format(g, inbound_rows[g][1])
                       for g in MAIN_GROUPS + (OTHER,) if inbound_rows[g][1])
    if shares:
        parts[0] += " - " + shares
    modes = dict((r[0], r) for r in by_title["3."].rows)["Total"]
    if modes[4]:
        parts.append("Air {} / Ocean {} / Other {}".format(
            _pct(modes[1], modes[4]), _pct(modes[2], modes[4]),
            _pct(modes[3], modes[4])))
    outcome_total = dict((r[0], r) for r in by_title["4."].rows)["Total"]
    parts.append("Outcome so far: " + ", ".join(
        "{} {}".format(name.lower(), value)
        for name, value in zip(OUTCOMES, outcome_total[1:-1]) if value))
    sent = dict((r[0], r) for r in by_title["5a"].rows)["Total"][1]
    parts.append("{} vendor requests sent".format(sent))
    adoption_total = dict((r[0], r) for r in by_title["6a"].rows)["Total"]
    if adoption_total[1]:
        parts.append("adoption {} of {} quotes ({})".format(
            adoption_total[2], adoption_total[1], adoption_total[3]))
    return ". ".join(parts) + "."


# ---------------------------------------------------------------- entry ----
def build(conn: sqlite3.Connection, month: str, today=None) -> Report:
    """The whole report for one month, from one set of queries."""
    today = _today(today)
    sections = [inbound(conn, month)]
    sections.extend(top_tables(conn, month))
    sections.append(mode(conn, month))
    sections.append(outcome(conn, month, today))
    sections.extend(outbound(conn, month))
    sections.extend(adoption(conn, month))
    the_trend = trend(conn, month, today)
    report = Report(month=month, today=today, sections=sections,
                    trend=the_trend,
                    trend_volume=Block("7a. Volume per month, legacy included",
                                       the_trend.columns[:5],
                                       [row[:5] for row in the_trend.rows]),
                    definitions=definitions(today))
    report.summary = summary_text(month, sections)
    return report


def render(report: Report) -> list:
    """Plain text, for the CLI and for tests."""
    lines = ["Month {}   (outcomes judged on {})".format(report.month, report.today),
             "", report.summary, ""]
    for block in report.sections + [report.trend]:
        lines.append(block.title)
        lines.append("  " + " | ".join(str(c) for c in block.columns))
        for row in block.rows:
            lines.append("  " + " | ".join(str(c) for c in row))
        lines.append("")
    lines.append("Definitions")
    lines.extend("  " + line for line in report.definitions)
    return lines

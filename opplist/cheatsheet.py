"""The cheat sheet (batch 17c): a one-page digest of a month for
Iris - who asked, in what mode, from where to where - as the Report
page's second tab, sample v2.

The base, the groups and the colours are the month report's exactly
(monthly.CASES, monthly.GROUP folded to monthly.FOUR). A lane is
'<origin country> -> <destination country>' from the case's first route
(v_enquiry carries route A); a case with one country reads 'from USA' or
'to Chile' and is in no lane table. Nothing is written anywhere.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field

from . import monthly

ARROW = "→"
TOP_REQUESTERS = 5
TOP_LANES = 12
LANES_PER_REQUESTER = 3
PAIRS_PER_LANE = 2
WHO_PER_LANE = 3
CARGO_CUT = 40
MODES = ("Air", "FCL", "LCL")


@dataclass
class CheatSheet:
    month: str
    title: str
    glance: dict = field(default_factory=dict)
    groups: list = field(default_factory=list)      # dicts: name, colour, row (summary), top, also, n, requesters
    lanes: list = field(default_factory=list)       # page 2: dicts lane, n, mode, who, won
    won: list = field(default_factory=list)         # page 2: dicts opp_no, who, where, pair, mode, cargo


#: CS1: a case's reading, the month report's own: Air, FCL or LCL as section
#: 1 counts them, 'other' for any other mode, '' (no mode) otherwise. SC1:
#: section 1's column (monthly.CASE_COLUMN, a quote-both case under its Won
#: route's mode); RORO, Sea-Air, Truck and Cross-border Truck are Other
#: here, named - the sheet has no Ocean figure
READING = ("CASE WHEN {air} THEN 'Air' WHEN {fcl} THEN 'FCL' WHEN {lcl} THEN 'LCL' WHEN {col} <> '' THEN 'other' "
           "ELSE '' END").format(air=monthly.AIR, fcl=monthly.FCL, lcl=monthly.LCL, col=monthly.CASE_COLUMN)
#: an Other case's name: its service when that is one of the two, else its mode
OTHER_PART = "CASE {svc} {services} ELSE {mode} END".format(
    svc=monthly.CASE_SERVICE, mode=monthly.CASE_MODE,
    services=" ".join("WHEN '{}' THEN '{}'".format(s.lower(), s) for s in monthly.OTHER_SERVICES))


def _cases(conn, month) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT e.id, e.opp_no, e.status, e.service_mode, e.direction, e.latam, e.origin_country, e.dest_country, "
        "e.origin_loc, e.dest_loc, e.commodity, e.requester_party_id AS pid, e.requester_key AS who, "
        "p.country AS party_country, {grp} AS grp, {reading} AS reading, {part} AS part "
        "{base} WHERE e.month = ? AND {cases} "
        "ORDER BY CAST(substr(e.opp_no, 5) AS INTEGER)".format(
            grp=monthly.GROUP, reading=READING, part=OTHER_PART, base=monthly.BASE, cases=monthly.CASES), (month,))]


def _country(value) -> str:
    value = (value or "").strip()
    return "" if value in ("", "Unknown") else value


def lane_of(c) -> str:
    """'USA -> Chile'; '' when a country is missing (see where_of)."""
    origin, dest = _country(c["origin_country"]), _country(c["dest_country"])
    return "{} {} {}".format(origin, ARROW, dest) if origin and dest else ""


def where_of(c) -> str:
    """The lane, or 'from USA' / 'to Chile' with one country, or ''."""
    lane = lane_of(c)
    if lane:
        return lane
    origin, dest = _country(c["origin_country"]), _country(c["dest_country"])
    if origin:
        return "from {}".format(origin)
    if dest:
        return "to {}".format(dest)
    return ""


def pair_of(c) -> str:
    """'MIA -> SCL': the location keys, only when both ends are known."""
    origin, dest = (c["origin_loc"] or "").strip(), (c["dest_loc"] or "").strip()
    return "{} {} {}".format(origin, ARROW, dest) if origin and dest else ""


def mode_key(c) -> str:
    """CS1: 'Air', 'FCL', 'LCL', 'other' or '' (no mode), read by the query."""
    return c["reading"]


def other_parts(cases) -> list:
    """[(name, n)] of the Other cases, most first (ties on the first case)."""
    return [(name, len(cs)) for name, cs in _ranked(_group([c for c in cases if c["reading"] == "other"],
                                                           lambda c: c["part"]))]


def mode_text(cases) -> str:
    """'Air 1 · FCL 2 · LCL 4 · Truck 1 · no mode 1': the parts with a case;
    the Other cases by name, most first, no mode last."""
    parts = [(m, sum(1 for c in cases if c["reading"] == m)) for m in MODES] + other_parts(cases)
    parts.append(("no mode", sum(1 for c in cases if not c["reading"])))
    return " · ".join("{} {}".format(m, n) for m, n in parts if n)


def _ranked(items) -> list:
    """[(key, [cases])] by count, ties on the lowest case number (the cases
    are in case order, so the first case's position decides)."""
    return sorted(items.items(), key=lambda kv: (-len(kv[1]), kv[1][0]["_order"]))


def _group(cases, key) -> dict:
    out = {}
    for c in cases:
        k = key(c)
        if k:
            out.setdefault(k, []).append(c)
    return out


def _pairs(cases) -> str:
    """The two most common point pairs: 'MIA -> SCL ×2, DTW -> SCL'."""
    ranked = _ranked(_group(cases, pair_of))[:PAIRS_PER_LANE]
    return ", ".join("{} ×{}".format(p, len(cs)) if len(cs) > 1 else p for p, cs in ranked)


def _requester(cases) -> dict:
    first = cases[0]
    places = _ranked(_group(cases, where_of))
    lines = [{"lane": lane, "n": len(cs), "pairs": _pairs(cs)} for lane, cs in places[:LANES_PER_REQUESTER]]
    return {"who": first["who"] or "", "country": _country(first["party_country"]), "n": len(cases),
            "mode": mode_text(cases), "lines": lines, "more": max(0, len(places) - LANES_PER_REQUESTER),
            "won": sum(1 for c in cases if c["status"] == "Won")}


def _summary(cases, total) -> dict:
    def n(test):
        return sum(1 for c in cases if test(c))
    by_who = _ranked(_group(cases, lambda c: c["pid"]))
    top = by_who[0][1] if by_who else []
    return {"n": len(cases), "share": round(100.0 * len(cases) / total) if total else 0,
            "air": n(lambda c: mode_key(c) == "Air"), "fcl": n(lambda c: mode_key(c) == "FCL"),
            "lcl": n(lambda c: mode_key(c) == "LCL"), "other": n(lambda c: mode_key(c) == "other"),
            "no_mode": n(lambda c: not mode_key(c)),
            "export": n(lambda c: c["direction"] == "US Export"), "import": n(lambda c: c["direction"] == "US Import"),
            "triangle": n(lambda c: c["direction"] == "Cross-trade"), "won": n(lambda c: c["status"] == "Won"),
            "most": "{} ({})".format(top[0]["who"], len(top)) if top else ""}


def build(conn, month: str) -> CheatSheet:
    cases = _cases(conn, month)
    for i, c in enumerate(cases):
        c["_order"] = i
        c["four"] = monthly.FOLD[c["grp"]]
    total = len(cases)
    sheet = CheatSheet(month=month, title="{} {}".format(calendar.month_name[int(month[5:7])], month[:4]))
    s = _summary(cases, total)
    sheet.glance = dict(s, latam=sum(1 for c in cases if c["latam"] == "Y"),
                        row=sum(1 for c in cases if c["latam"] != "Y"),
                        lost=sum(1 for c in cases if c["status"] == "Lost"),
                        cancelled=sum(1 for c in cases if c["status"] == "Cancelled"))
    # CS1: Other is the report's Other, named by its parts; the rest is no
    # mode, so Air + FCL + LCL + Other + no mode is the inquiries
    sheet.glance["other_parts"] = ", ".join("{} {}".format(m, n) for m, n in other_parts(cases))
    for name, colour in monthly.FOUR:
        mine = [c for c in cases if c["four"] == name]
        if not mine:
            continue
        ranked = _ranked(_group(mine, lambda c: c["pid"]))
        sheet.groups.append({
            "name": name, "colour": colour, "row": _summary(mine, total), "n": len(mine),
            "requesters": len(ranked),
            "top": [_requester(cs) for _, cs in ranked[:TOP_REQUESTERS]],
            "also": ", ".join("{} {}".format(cs[0]["who"], len(cs)) for _, cs in ranked[TOP_REQUESTERS:]),
        })
    for lane, cs in _ranked(_group(cases, lane_of))[:TOP_LANES]:
        who = _ranked(_group(cs, lambda c: c["pid"]))
        text = ", ".join("{} {}".format(w[0]["who"], len(w)) for _, w in who[:WHO_PER_LANE])
        if len(who) > WHO_PER_LANE:
            text += " +{} more".format(len(who) - WHO_PER_LANE)
        sheet.lanes.append({"lane": lane, "n": len(cs), "mode": mode_text(cs), "who": text,
                            "won": sum(1 for c in cs if c["status"] == "Won")})
    for c in cases:
        if c["status"] == "Won":
            sheet.won.append({"id": c["id"], "opp_no": c["opp_no"], "who": c["who"] or "", "where": where_of(c),
                              "pair": pair_of(c), "mode": c["service_mode"] or "",
                              "cargo": (c["commodity"] or "").strip()[:CARGO_CUT]})
    return sheet


#: The cheat sheet's own rules, under the page's .mreport scope (sample v2).
CSS = """
.mreport .cs.page{height:auto;min-height:0;overflow:visible}
.mreport .cs h1 small{font-size:12px}
.mreport .glance{display:flex;flex-wrap:wrap;gap:6px 26px;font-size:13px;color:#2b2b2a;margin:-8px 0 20px;padding:10px 14px;border:1px solid #E1E0D9;border-radius:6px}
.mreport .glance b{font-size:14px}
.mreport table.cs-sum{font-size:12.5px;margin-bottom:24px} .mreport table.cs-sum td,.mreport table.cs-sum th{padding:3px 8px}
.mreport i.dot{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:7px;vertical-align:0}
.mreport .grp{break-inside:avoid;margin-bottom:22px} .mreport .grp h2 small{font-size:11px}
.mreport table.cs-top{font-size:12px;margin-bottom:0;table-layout:fixed} .mreport table.cs-top td,.mreport table.cs-top th{padding:4px 8px;white-space:normal;vertical-align:top}
.mreport th.c-who{width:22%} .mreport th.c-ctry{width:11%} .mreport th.c-n{width:5%} .mreport th.c-mode{width:20%} .mreport th.c-where{width:37%} .mreport th.c-won{width:5%}
.mreport .ln{line-height:1.4} .mreport .ln b{font-weight:600} .mreport .pt{color:#6a7178;font-size:11px;margin-left:4px} .mreport .ln.more{color:#6a7178;font-size:11px}
.mreport tr.rest td{color:#52514E;font-size:11px;border-bottom:0;padding-top:5px}
.mreport .sub{color:#6a7178;font-size:10.5px}
.mreport .nomode{color:#9b2c3c}
.mreport table.cs-lanes,.mreport table.cs-won{font-size:12px} .mreport table.cs-lanes td,.mreport table.cs-lanes th,.mreport table.cs-won td,.mreport table.cs-won th{padding:3px 6px;white-space:normal;vertical-align:top}
.mreport td.cg{color:#3d4650} .mreport table.cs-won td:first-child,.mreport table.cs-lanes td:first-child{white-space:nowrap}
.report-tabs{display:flex;gap:4px;margin:6px 0 0} .report-tabs a{padding:6px 14px;border:1px solid #d8dce0;border-bottom:0;border-radius:6px 6px 0 0;color:#2b3138;text-decoration:none;background:#f6f7f9}
.report-tabs a.on{background:#fff;font-weight:600}
"""
